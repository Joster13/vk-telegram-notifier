"""Forward VK notification emails from Gmail IMAP to one Telegram chat.

Requires a Gmail app password. Does not access the VK API.
"""
from __future__ import annotations

import email
import hashlib
import html
import imaplib
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from email import policy
from email.utils import parseaddr
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
LOG = logging.getLogger("vk-notify")
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
GMAIL = os.getenv("GMAIL_ADDRESS", "").strip()
APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
IMAP_HOST = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
VK_SENDER = os.getenv("GMAIL_SENDER", "admin@notify.vk.com").lower()
POLL = max(30, int(os.getenv("POLL_SECONDS", "60")))
DB_FILE = os.getenv("DB_FILE", "vk_mail_state.sqlite3")
API_URL = "https://api.telegram.org/bot" + TG_TOKEN + "/"
STATE = {"check": "ещё не проверено", "error": "", "count": 0}


def clean_vk_link(link: str) -> str | None:
    """Unwrap VK email tracking links without clicking them."""
    url = urlsplit(link or "")
    if url.scheme != "https" or url.hostname not in ("vk.ru", "vk.com", "m.vk.ru", "m.vk.com"):
        return None
    if url.path == "/email.php":
        target = parse_qs(url.query).get("u", [""])[0]
        return clean_vk_link(target) if target and target != link else None
    if url.path == "/im":
        q = parse_qs(url.query)
        fields = [(key, q[key][0]) for key in ("sel", "msgid", "cmid") if q.get(key)]
        return "https://vk.com/im?" + urlencode(fields) if fields else None
    if url.path.startswith("/email") or url.path in ("", "/"):
        return None
    return "https://vk.com" + url.path


def extract_messages(data: bytes) -> list[dict]:
    mail = email.message_from_bytes(data, policy=policy.default)
    if parseaddr(mail.get("From", ""))[1].lower() != VK_SENDER:
        return []
    part = mail.get_body(preferencelist=("html",))
    if part is None:
        return []
    soup = BeautifulSoup(part.get_content(), "html.parser")
    parsed = []
    sender = "Пользователь ВКонтакте"
    for a in soup.find_all("a", href=True):
        target = clean_vk_link(a.get("href", ""))
        if not target:
            continue
        caption = a.get_text(" ", strip=True)
        if not caption:
            continue
        if urlsplit(target).path == "/im":
            if not (("msgid=" in target) or ("cmid=" in target)):
                continue
            timestamp = ""
            for node in (a.parent, getattr(a.parent, "parent", None)):
                if node is None:
                    continue
                match = re.search(r"\d{1,2}\s+[а-яё]{3,}\s+в\s+\d{1,2}:\d{2}",
                                  node.get_text(" ", strip=True), re.I)
                if match:
                    timestamp = match.group(0)
                    break
            parsed.append({"sender": sender, "text": caption, "url": target, "time": timestamp})
        elif (urlsplit(target).path not in ("/settings", "/feed")
              and len(caption) < 140):
            sender = caption
    return parsed


def telegram(method: str, **kwargs):
    for attempt in range(4):
        try:
            result = requests.post(API_URL + method, json=kwargs, timeout=35).json()
        except (requests.RequestException, ValueError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("Telegram network error")
        if result.get("ok"):
            return result.get("result")
        if result.get("error_code") == 429 and attempt < 3:
            time.sleep(min(60, int(result.get("parameters", {}).get("retry_after", 5))))
            continue
        raise RuntimeError(result.get("description", "Telegram error"))
    raise RuntimeError("Telegram retries exhausted")


def send(text: str, link: str | None = None) -> None:
    for start in range(0, len(text), 3900):
        params = {"chat_id": CHAT_ID, "text": text[start:start + 3900],
                  "disable_web_page_preview": True}
        if link:
            params["reply_markup"] = {"inline_keyboard": [[{"text": "Ответить в ВК ↗", "url": link}]]}
        telegram("sendMessage", **params)


def connect_db():
    db = sqlite3.connect(DB_FILE, timeout=20)
    db.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS sent (hash TEXT PRIMARY KEY)")
    db.commit()
    return db


def config(db, name):
    row = db.execute("SELECT v FROM config WHERE k=?", (name,)).fetchone()
    return row[0] if row else None


def set_config(db, name, value):
    db.execute("INSERT OR REPLACE INTO config (k,v) VALUES (?,?)", (name, str(value)))
    db.commit()


def forward_mail(db, raw):
    for item in extract_messages(raw):
        fingerprint = hashlib.sha256(item["url"].encode()).hexdigest()
        if db.execute("SELECT 1 FROM sent WHERE hash=?", (fingerprint,)).fetchone():
            continue
        body = "📩 ВКонтакте\nОтправитель: " + item["sender"]
        if item["time"]:
            body += "\nВремя: " + item["time"]
        body += "\n\n" + item["text"]
        send(body, item["url"])
        db.execute("INSERT INTO sent (hash) VALUES (?)", (fingerprint,))
        db.commit()
        STATE["count"] += 1


def poll_gmail(db):
    with imaplib.IMAP4_SSL(IMAP_HOST, port=993, timeout=30) as imap:
        imap.login(GMAIL, APP_PASSWORD)
        if imap.select("INBOX", readonly=True)[0] != "OK":
            raise RuntimeError("Cannot select INBOX")
        status, response = imap.uid("search", None, "FROM", '"'+VK_SENDER+'"')
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        ids = [int(s) for s in (response[0] or b"").split()]
        cursor = config(db, "last_uid")
        if cursor is None:
            # Only new messages after the bot's first start, no historical spam.
            set_config(db, "last_uid", max(ids, default=0))
        else:
            for uid in ids:
                if uid <= int(cursor):
                    continue
                status, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError("IMAP fetch failed")
                bodies = [p[1] for p in parts if isinstance(p, tuple) and isinstance(p[1], bytes)]
                if not bodies:
                    raise RuntimeError("IMAP message is empty")
                forward_mail(db, b"".join(bodies))
                set_config(db, "last_uid", uid)
        STATE["check"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        STATE["error"] = ""


def worker():
    db = connect_db()
    while True:
        try:
            poll_gmail(db)
        except Exception as exc:
            STATE["error"] = type(exc).__name__
            LOG.warning("Gmail polling failed: %s", type(exc).__name__)
        time.sleep(POLL)


def command(chat_id, text):
    if str(chat_id) != CHAT_ID:
        return
    cmd = text.split(" ", 1)[0].split("@", 1)[0].lower()
    if cmd == "/start":
        send("VK Notify запущен. Уведомления из Gmail будут приходить сюда.\n/status — статус\n/test — тест\n/help — справка")
    elif cmd == "/test":
        send("✅ Тест пройден: Telegram-бот может отправлять сообщения.")
    elif cmd == "/status":
        send("Gmail: " + ("ошибка ("+STATE["error"]+")" if STATE["error"] else "проверяется")
             + "\nПоследняя успешная проверка: " + STATE["check"]
             + "\nУведомлений отправлено: " + str(STATE["count"]))
    elif cmd == "/help":
        send("Проверяю Gmail и пересылаю только то, что VK включил в почтовые уведомления. "
             "Медиафайлы, не приложенные к письму, получить невозможно. "
             "Для ответа нажми кнопку «Ответить в ВК».")


def main():
    logging.basicConfig(level=logging.INFO)
    if not all((TG_TOKEN, CHAT_ID, GMAIL, APP_PASSWORD)):
        raise SystemExit("Укажите TG_BOT_TOKEN, TG_CHAT_ID, GMAIL_ADDRESS и GMAIL_APP_PASSWORD")
    threading.Thread(target=worker, daemon=True).start()
    send("✅ VK Notify запущен. Ожидаю новые письма от ВКонтакте.")
    offset = None
    while True:
        try:
            changes = telegram("getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
            for event in changes:
                offset = event["update_id"] + 1
                msg = event.get("message", {})
                if msg.get("chat", {}).get("type") == "private":
                    command(msg["chat"]["id"], msg.get("text", ""))
        except KeyboardInterrupt:
            return
        except Exception as exc:
            LOG.warning("Telegram polling problem: %s", type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
