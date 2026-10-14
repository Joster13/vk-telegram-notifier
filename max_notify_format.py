"""Dragon Notify presentation helpers for the pinned MAX adapter.

The upstream listener already HTML-escapes sender and chat labels.
No credentials or message state are stored here.
"""
from datetime import datetime, timedelta, timezone
from html import unescape

MOSCOW = timezone(timedelta(hours=3))
MONTHS = ("янв", "фев", "мар", "апр", "май", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек")
_reply_registrar = None


def configure_replies(registrar):
    global _reply_registrar
    _reply_registrar = registrar


def message_time(value):
    try:
        stamp = float(value)
        if stamp <= 0:
            return ""
        if stamp > 100_000_000_000:
            stamp /= 1000
        moment = datetime.fromtimestamp(stamp, MOSCOW)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return f"{moment.day} {MONTHS[moment.month - 1]} в {moment:%H:%M}"


def format_header(msg, sender_label, chat_label, is_dm):
    sender = sender_label.strip() or "Пользователь MAX"
    lines = ["📩 <b>MAX</b>", f"Отправитель: {sender}"]
    # A numeric chat ID and the resolver's DM placeholder are not titles.
    if (not is_dm and chat_label and chat_label != str(msg.chat_id)
            and chat_label != sender_label and not chat_label.startswith("DM:")):
        lines.append(f"Чат: {chat_label}")
    stamp = message_time(msg.timestamp)
    if stamp:
        lines.append(f"Время: {stamp}")
    # The upstream listener adds another newline before the message body.
    return "\n".join(lines) + "\n"


def open_max_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # The public web entry is reliable; no guessed private-chat deep link.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть MAX ↗", url="https://web.max.ru/")
    ]])


def notification_keyboard(msg, sender_label, chat_label, is_dm):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = list(open_max_keyboard().inline_keyboard)
    if _reply_registrar is not None:
        label = sender_label
        if (not is_dm and chat_label and chat_label != str(msg.chat_id)
                and not chat_label.startswith("DM:") and chat_label != sender_label):
            label = "Группа: " + chat_label
        token = _reply_registrar(msg.chat_id, unescape(label), msg.message_id)
        rows.insert(0, [InlineKeyboardButton("Ответить в MAX", callback_data="maxreply:" + token)])
    return InlineKeyboardMarkup(rows)
