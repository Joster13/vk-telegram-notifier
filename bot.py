"""Личные сообщения ВК -> уведомления в личном Telegram-чате.

Python 3.10+. VK требует *пользовательский* токен с доступом к messages API.
Обычный токен сообщества НЕ читает личную страницу.
"""
from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

LOG = logging.getLogger("vk-to-tg")
VK_API = "https://api.vk.com/method/"
TG_API = "https://api.telegram.org/bot"
CHAT_BASE = 2_000_000_000
# Leave headroom below Telegram's standard 50 MB file limit.
MAX_MEDIA_BYTES = 45 * 1024 * 1024


class APIError(RuntimeError):
    pass


def vk_dialog_url(peer_id: int, message_id: int | None = None) -> str:
    """Ссылка на диалог; параметр msgid работает не во всех клиентах VK."""
    sel = f"c{peer_id - CHAT_BASE}" if peer_id >= CHAT_BASE else str(peer_id)
    link = f"https://vk.com/im?sel={quote(sel, safe='-')}"
    return f"{link}&msgid={message_id}" if message_id else link


def vk_object_url(kind: str, obj: dict) -> str | None:
    owner, ident = obj.get("owner_id"), obj.get("id")
    if owner is None or ident is None:
        return None
    prefix = {
        "photo": "photo", "video": "video", "audio": "audio",
        "doc": "doc", "wall": "wall", "story": "story",
    }.get(kind)
    if not prefix:
        return None
    url = f"https://vk.com/{prefix}{owner}_{ident}"
    if obj.get("access_key"):
        url += "?access_key=" + quote(str(obj["access_key"]))
    return url


def best_image(obj: dict) -> str | None:
    """Фото, миниатюра ролика или статичное изображение VK-стикера."""
    images = (
        obj.get("sizes") or obj.get("images_with_background")
        or obj.get("images") or obj.get("image") or []
    )
    if isinstance(images, dict):
        images = list(images.values())
    if isinstance(images, list) and images:
        choices = [i for i in images if isinstance(i, dict) and (i.get("url") or i.get("src"))]
        if choices:
            i = max(choices, key=lambda a: int(a.get("width") or 0) * int(a.get("height") or 0))
            return i.get("url") or i.get("src")
    for key in ("photo_256", "photo_128", "photo_100", "url"):
        if obj.get(key):
            return obj[key]
    return None


def sender_name(from_id: int, profiles: list[dict], groups: list[dict]) -> str | None:
    if from_id > 0:
        for p in profiles:
            if int(p.get("id", 0)) == from_id:
                return (str(p.get("first_name", "")) + " " + str(p.get("last_name", ""))).strip()
    elif from_id < 0:
        for g in groups:
            if int(g.get("id", 0)) == -from_id:
                return str(g.get("name", ""))
    return None


class VkClient:
    def __init__(self, token: str, api_version: str = "5.199") -> None:
        self.token = token
        self.version = api_version
        self.http = requests.Session()

    def call(self, method: str, **params):
        try:
            res = self.http.post(
                VK_API + method,
                data={**params, "access_token": self.token, "v": self.version},
                timeout=25,
            )
            res.raise_for_status()
            body = res.json()
        except (requests.RequestException, ValueError) as exc:
            raise APIError(f"Сбой запроса VK {method}: {type(exc).__name__}") from exc
        if "error" in body:
            e = body["error"]
            code = e.get("error_code", "?")
            msg = e.get("error_msg", "unknown")
            hint = (
                " Необходим разрешённый пользовательский токен с правом messages."
                if code in (7, 15, 20, 27, 28) else ""
            )
            raise APIError(f"VK {method}: ошибка {code}: {msg}.{hint}")
        return body.get("response")

    def get_message(self, message_id: int) -> tuple[dict | None, list[dict], list[dict]]:
        response = self.call("messages.getById", message_ids=message_id, extended=1)
        items = response.get("items", []) if isinstance(response, dict) else []
        return (
            items[0] if items else None,
            response.get("profiles", []) if isinstance(response, dict) else [],
            response.get("groups", []) if isinstance(response, dict) else [],
        )

    def resolve_sender(self, sender_id: int) -> str:
        """Запасной запрос, если VK не вернул profiles/groups в getById."""
        try:
            if sender_id > 0:
                p = self.call("users.get", user_ids=str(sender_id))
                if p:
                    return (p[0].get("first_name", "") + " " + p[0].get("last_name", "")).strip()
            elif sender_id < 0:
                g = self.call("groups.getById", group_ids=str(-sender_id))
                if isinstance(g, dict):
                    g = g.get("groups", [])
                if g:
                    return str(g[0].get("name", ""))
        except APIError as exc:
            LOG.warning("Имя отправителя недоступно: %s", exc)
        return f"Пользователь {sender_id}" if sender_id > 0 else f"Сообщество {-sender_id}"

    def conversation_title(self, peer_id: int) -> str | None:
        """Return title of a group conversation when API permits it."""
        if peer_id < CHAT_BASE:
            return None
        try:
            result = self.call("messages.getConversationsById", peer_ids=str(peer_id))
            items = result.get("items") or [] if isinstance(result, dict) else []
            return (items[0].get("chat_settings") or {}).get("title") if items else None
        except APIError as exc:
            LOG.debug("Can't resolve chat title: %s", exc)
            return None

    def events(self):
        """Новый Long Poll сеанс. Старые сообщения при новом запуске не загружаем."""
        info = self.call("messages.getLongPollServer", lp_version=3, need_pts=0)
        server = str(info["server"])
        if not server.startswith(("https://", "http://")):
            server = "https://" + server
        if not server.startswith("https://"):
            raise APIError("VK выдал небезопасный адрес Long Poll")
        key, ts = info["key"], info["ts"]
        while True:
            try:
                res = self.http.get(
                    server,
                    params={"act": "a_check", "key": key, "ts": ts, "wait": 25, "mode": 2, "version": 3},
                    timeout=40,
                )
                res.raise_for_status()
                data = res.json()
            except (requests.RequestException, ValueError) as exc:
                # Never print the Long Poll URL: it contains a secret key.
                raise APIError(f"VK Long Poll: {type(exc).__name__}") from exc
            failed = int(data.get("failed", 0))
            if failed == 1:
                ts = data["ts"]
                continue
            if failed:
                LOG.warning("VK Long Poll просит переподключение (код %s)", failed)
                return  # внешний цикл создаст новый сеанс
            ts = data.get("ts", ts)
            for ev in data.get("updates", []):
                # User Long Poll v3: [4, message_id, flags, peer_id, ...]
                if isinstance(ev, list) and len(ev) >= 4 and ev[0] == 4:
                    flags = int(ev[2])
                    if flags & 2:  # исходящее сообщение
                        continue
                    yield int(ev[1])


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.api = TG_API + token + "/"
        self.chat_id = chat_id
        self.http = requests.Session()

    def send(self, method: str, **params):
        payload = {"chat_id": self.chat_id, **params}
        for attempt in range(4):
            try:
                res = self.http.post(self.api + method, json=payload, timeout=45)
                data = res.json()
            except (ValueError, requests.RequestException) as exc:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise APIError(f"Telegram {method}: {type(exc).__name__}") from exc
            if data.get("ok"):
                return data.get("result")
            if data.get("error_code") == 429 and attempt < 3:
                delay = int(data.get("parameters", {}).get("retry_after", 3))
                time.sleep(min(delay, 60))
                continue
            raise APIError(f"Telegram {method}: {data.get('description', 'Unknown error')}")
        raise APIError(f"Telegram {method}: исчерпаны попытки")

    def message(self, text: str, link: str | None = None, html_mode: bool = False):
        markup = None
        if link:
            markup = {"inline_keyboard": [[{"text": "Ответить в ВК ↗", "url": link}]]}
        # HTML в Telegram ограничен 4096 символами. Отдельный фрагмент текста
        # каждого сообщения VK мы отправляем ниже, не обрезая его.
        return self.send(
            "sendMessage", text=text,
            parse_mode="HTML" if html_mode else None,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    def attachment(self, method: str, arg: str, value: str, caption: str):
        """Upload media ourselves: Telegram cannot always download VK URLs.

        VK CDN files may require redirects; we do not follow redirects into local
        or literal IP hosts. Spooled file keeps most downloads out of RAM.
        """
        parsed = urlparse(value or "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname == "localhost":
            raise ValueError("Нет доступного публичного HTTPS URL вложения")
        # VK API controls URL provenance, but never request a literal IP host.
        import ipaddress
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("IP-адрес вместо домена медиа запрещён")

        redirects = 0
        current = value
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as blob:
            while True:
                with self.http.get(current, stream=True, timeout=(10, 40), allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin
                        nxt = urljoin(current, response.headers.get("Location", ""))
                        parsed_next = urlparse(nxt)
                        if parsed_next.scheme != "https" or not parsed_next.hostname:
                            raise ValueError("Небезопасный редирект вложения")
                        try:
                            ipaddress.ip_address(parsed_next.hostname)
                        except ValueError:
                            if parsed_next.hostname == "localhost":
                                raise ValueError("Локальный адрес вложения")
                        else:
                            raise ValueError("Медиа ведёт на IP-адрес")
                        current = nxt
                        redirects += 1
                        if redirects > 3:
                            raise ValueError("Слишком много редиректов")
                        continue
                    response.raise_for_status()
                    claimed_length = int(response.headers.get("Content-Length", "0") or "0")
                    if claimed_length > MAX_MEDIA_BYTES:
                        raise ValueError("Файл больше 45 МБ — отправлю ссылку")
                    size = 0
                    for chunk in response.iter_content(128 * 1024):
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            raise ValueError("Файл больше 45 МБ — отправлю ссылку")
                        blob.write(chunk)
                    break
            blob.seek(0)
            ext = {
                "sendPhoto": ".jpg", "sendVoice": ".ogg", "sendAudio": ".mp3",
                "sendVideo": ".mp4", "sendDocument": ".bin",
                "sendAnimation": ".gif",
            }.get(method, ".bin")
            for attempt in range(3):
                blob.seek(0)
                # Requests supports (filename, fileobj, MIME type) for multipart.
                filename = "vk_attachment" + ext
                try:
                    response = self.http.post(
                        self.api + method,
                        data={"chat_id": self.chat_id, "caption": caption[:900]},
                        files={arg: (filename, blob)}, timeout=(10, 120),
                    )
                    result = response.json()
                except (requests.RequestException, ValueError) as exc:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise APIError(f"Telegram {method}: {type(exc).__name__}") from exc
                if result.get("ok"):
                    return result.get("result")
                if result.get("error_code") == 429 and attempt < 2:
                    time.sleep(min(int(result.get("parameters", {}).get("retry_after", 3)), 60))
                    continue
                raise APIError(f"Telegram {method}: {result.get('description', 'ошибка отправки')}")
        raise APIError(f"Telegram {method}: файл не отправлен")


class Seen:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id INTEGER PRIMARY KEY, seen_at INTEGER NOT NULL)")
        self.db.execute("DELETE FROM seen WHERE seen_at < ?", (int(time.time()) - 30 * 86400,))
        self.db.commit()

    def contains(self, message_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE id=?", (message_id,)).fetchone() is not None

    def add(self, message_id: int):
        self.db.execute("INSERT OR IGNORE INTO seen (id, seen_at) VALUES (?,?)", (message_id, int(time.time())))
        self.db.commit()


def chunk_text(text: str, width: int = 3200):
    # Делим по символам, чтобы экранирование HTML не ломало разметку.
    return [text[i:i + width] for i in range(0, len(text), width)]


def summarize_forward(msg: dict) -> str:
    reply = msg.get("reply_message")
    fwds = msg.get("fwd_messages") or []
    items = []
    if isinstance(reply, dict):
        preview = reply.get("text", "")[:150].replace("\n", " ")
        items.append(f"↪ Ответ на сообщение: {preview or '[вложение]'}")
    if fwds:
        items.append(f"↪ Пересланных сообщений: {len(fwds)} (подробности в VK)")
    if msg.get("action"):
        items.append("Системное событие беседы (подробности в VK)")
    return "\n".join(items)


def deliver_attachment(tg: TelegramClient, att: dict, original_url: str) -> None:
    kind = att.get("type", "unknown")
    obj = att.get(kind, {})
    if not isinstance(obj, dict):
        obj = {}
    permalink = vk_object_url(kind, obj) or original_url
    desc = {
        "photo": "Фотография", "sticker": "Стикер VK", "video": "Видео",
        "audio_message": "Голосовое сообщение", "doc": "Документ", "audio": "Музыка",
        "wall": "Запись со стены", "link": "Ссылка", "poll": "Опрос",
        "gift": "Подарок", "story": "История", "graffiti": "Граффити",
        "market": "Товар", "market_album": "Подборка товаров",
    }.get(kind, f"Вложение VK: {kind}")

    # Если Telegram не смог забрать исходный URL (доступ, размер, формат),
    # просто дадим пользователю ссылку на содержимое в VK.
    try:
        if kind == "photo":
            photo = best_image(obj)
            if photo:
                tg.attachment("sendPhoto", "photo", photo, desc)
                return
        elif kind == "sticker":
            picture = best_image(obj)
            if picture:
                tg.attachment("sendPhoto", "photo", picture, desc + " (изображение)")
                return
        elif kind == "video":
            # Прямой MP4 зачастую отсутствует в VK API. Тогда отправим
            # ссылку и (если есть) миниатюру.
            files = obj.get("files") or {}
            if isinstance(files, dict):
                for field in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240"):
                    direct = files.get(field)
                    if isinstance(direct, str) and direct.startswith("https://"):
                        tg.attachment("sendVideo", "video", direct, desc)
                        return
            preview = best_image(obj)
            if preview:
                try:
                    tg.attachment("sendPhoto", "photo", preview, "Превью видео")
                except (APIError, ValueError, requests.RequestException):
                    pass
        elif kind == "audio_message":
            if obj.get("link_ogg"):
                tg.attachment("sendVoice", "voice", obj["link_ogg"], desc)
                return
            if obj.get("link_mp3"):
                tg.attachment("sendAudio", "audio", obj["link_mp3"], desc)
                return
        elif kind == "doc":
            url = obj.get("url")
            if url:
                is_gif = str(obj.get("ext", "")).lower() == "gif"
                tg.attachment("sendAnimation" if is_gif else "sendDocument",
                              "animation" if is_gif else "document", url, desc)
                return
        elif kind == "graffiti":
            url = obj.get("url") or best_image(obj)
            if url:
                tg.attachment("sendPhoto", "photo", url, desc)
                return
        elif kind == "gift":
            preview = best_image(obj)
            if preview:
                tg.attachment("sendPhoto", "photo", preview, desc)
                return
    except (APIError, ValueError, requests.RequestException) as exc:
        # URLs often expire, or VK requires cookies. Always keep a link back.
        LOG.warning("Не получилось загрузить %s в TG: %s", kind, type(exc).__name__)

    if kind == "link":
        permalink = obj.get("url") or original_url
        title = str(obj.get("title") or "")
        desc += f" — {title}" if title else ""
    elif kind == "video" and obj.get("title"):
        desc += f" — {obj['title']}"
    elif kind == "doc" and obj.get("title"):
        desc += f" — {obj['title']}"
    tg.message(f"{desc}\n{permalink}")


def deliver_message(vk: VkClient, tg: TelegramClient, message_id: int, seen: Seen) -> None:
    if seen.contains(message_id):
        return
    msg = None
    profiles, groups = [], []
    for attempt in range(3):
        msg, profiles, groups = vk.get_message(message_id)
        if msg:
            break
        time.sleep(0.5 * (attempt + 1))
    if not msg:
        LOG.warning("VK не вернул сообщение %s", message_id)
        return
    if msg.get("out"):
        return

    peer_id = int(msg.get("peer_id") or msg.get("from_id") or 0)
    from_id = int(msg.get("from_id") or peer_id)
    sender = sender_name(from_id, profiles, groups) or vk.resolve_sender(from_id)
    link = vk_dialog_url(peer_id, int(msg.get("id") or message_id))

    prefix = f"📩 <b>{html.escape(sender)}</b>"
    if peer_id >= CHAT_BASE:
        title = vk.conversation_title(peer_id)
        prefix += "\n<i>Беседа ВК" + (": " + html.escape(title) if title else "") + "</i>"
    timestamp = msg.get("date")
    if timestamp:
        prefix += "\n" + datetime.fromtimestamp(int(timestamp), tz=timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")
    text = msg.get("text") or ""
    context = summarize_forward(msg)
    if context:
        text = (text + "\n" + context).strip()
    if not text:
        text = "[Сообщение с вложениями]" if msg.get("attachments") else "[Пустое сообщение]"

    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks):
        body = (prefix + "\n\n" if i == 0 else "") + html.escape(chunk)
        tg.message(body, link=link if i == 0 else None, html_mode=True)

    for attachment in msg.get("attachments") or []:
        try:
            deliver_attachment(tg, attachment, link)
        except APIError as exc:
            # Если даже ссылка на вложение не отправилась — повторим все
            # сообщение при восстановлении Telegram, не отмечая его seen.
            raise APIError(f"Вложение {message_id}: {exc}") from exc

    geo = msg.get("geo") or {}
    if isinstance(geo, dict) and geo:
        coords = geo.get("coordinates") or {}
        try:
            if isinstance(coords, dict):
                lat, lon = coords.get("latitude"), coords.get("longitude")
            else:
                lat, lon = str(coords).split()[:2]
            if lat is not None and lon is not None:
                tg.send("sendLocation", latitude=float(lat), longitude=float(lon))
        except (APIError, ValueError, TypeError):
            tg.message("Геолокация — откройте оригинал:\n" + link)

    seen.add(message_id)
    LOG.info("Новое входящее сообщение VK #%s (%s)", message_id, sender)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s: %(message)s")
    vk_token = os.getenv("VK_TOKEN", "").strip()
    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("TG_CHAT_ID", "").strip()
    if not all([vk_token, tg_token, tg_chat]):
        raise SystemExit("Укажите VK_TOKEN, TG_BOT_TOKEN и TG_CHAT_ID в файле .env")
    if not tg_chat.lstrip("-").isdigit():
        raise SystemExit("TG_CHAT_ID должен быть числовым ID вашего личного чата")
    vk = VkClient(vk_token, os.getenv("VK_API_VERSION", "5.199"))
    tg = TelegramClient(tg_token, tg_chat)
    seen = Seen(str(Path(__file__).with_name("seen.sqlite3")))
    # Report authentication failures to the console before claiming success.
    try:
        vk.call("messages.getLongPollServer", lp_version=3, need_pts=0)
    except APIError as exc:
        raise SystemExit(
            f"Не удалось получить доступ к сообщениям VK: {exc}\n"
            "Проверьте, что VK_TOKEN — пользовательский токен с разрешением messages."
        ) from exc
    tg.message("✅ VK → Telegram запущен. Новые входящие сообщения будут появляться здесь.")
    while True:
        try:
            for message_id in vk.events():
                deliver_message(vk, tg, message_id, seen)
        except KeyboardInterrupt:
            LOG.info("Остановлено")
            break
        except (APIError, requests.RequestException, KeyError, ValueError) as exc:
            LOG.error("Ошибка при работе: %s", exc)
            time.sleep(15)
        except Exception:
            LOG.exception("Непредвиденная ошибка")
            time.sleep(15)


if __name__ == "__main__":
    main()
