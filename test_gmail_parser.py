"""Synthetic VK mail layouts: no real messages, contacts or tracking tokens."""
import hashlib
import sqlite3
import unittest
from email.message import EmailMessage
from unittest.mock import patch
from urllib.parse import quote

import gmail_bot as app


def tracked(url):
    return "https://vk.ru/email.php?u=" + quote(url, safe="") + "&act=click"


def link(message=101, peer=7):
    return f"https://vk.ru/im?sel={peer}&msgid={message}&cmid={message}"


def mail(body, sender="admin@notify.vk.com"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = "Тестовое уведомление"
    msg.set_content("HTML notification")
    msg.add_alternative(body, subtype="html")
    return msg.as_bytes()


def single(body, *, message=101, sender="Тестовый отправитель", peer=7):
    target = tracked(link(message, peer))
    return f'''<table><tr><td><table>
      <tr><td><table><tr><td><a href="https://vk.ru/testsender">{sender}</a></td></tr>
      <tr><td><a href="{target}"><span class="nolinks">14 окт в 16:11</span></a></td></tr>
      </table></td></tr><tr><td class="links">{body}</td></tr>
      </table></td></tr></table>
      <table><tr><td><a href="{target}">Ответить</a></td></tr></table>'''


def digest_preview(text, *, message=101, sender="Тестовый отправитель", placeholder=False):
    phrase = "отправил вам" if placeholder else "отправил вам сообщение"
    return f'''<tr><td><table><tr><td><a href="https://vk.ru/testsender">{sender}</a>
      {phrase} <a href="{tracked(link(message))}">{text}</a></td></tr>
      <tr><td>14 окт в 16:11</td></tr></table></td></tr>'''


class GmailParserTests(unittest.TestCase):
    def test_single_message_reads_body_not_date_and_keeps_inline_emoji(self):
        items = app.extract_messages(mail(single('Спасибо <img alt="🙏" src="https://vk.ru/emoji/test.png">')))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "Спасибо 🙏")
        self.assertEqual(items[0]["time"], "14 окт в 16:11")
        self.assertEqual(items[0]["sender"], "Тестовый отправитель")
        self.assertEqual(items[0]["url"], "https://vk.com/im?sel=7&msgid=101&cmid=101")

    def test_truncated_community_preview_does_not_replace_sender_with_mention(self):
        body = ('Привет, <a href="https://vk.ru/id42">Алексей</a>!<br>Вторая строка'
                f'<br><br><a href="{tracked(link())}">показать полностью...</a>')
        item = app.extract_messages(mail(single(body, sender="Тестовое сообщество")))[0]
        self.assertEqual(item["sender"], "Тестовое сообщество")
        self.assertEqual(item["text"], "Привет, Алексей!\nВторая строка")
        self.assertTrue(item["truncated"])

    def test_digest_keeps_separate_senders_dates_and_emoji(self):
        body = '<table>' + digest_preview('Первое', sender='Первый')
        body += digest_preview('Второе <img alt="🐾">', message=102, sender='Второй') + '</table>'
        items = app.extract_messages(mail(body))
        self.assertEqual([i["sender"] for i in items], ["Первый", "Второй"])
        self.assertEqual([i["text"] for i in items], ["Первое", "Второе 🐾"])
        self.assertEqual([i["time"] for i in items], ["14 окт в 16:11"] * 2)

    def test_missing_preview_is_explicit(self):
        item = app.extract_messages(mail('<table>' + digest_preview('сообщение', placeholder=True) + '</table>'))[0]
        self.assertEqual(item["text"], app.NO_MAIL_TEXT)

    def test_real_message_can_be_date_or_button_caption(self):
        for text in ("Ответить", "показать полностью...", "14 окт в 16:11", "сообщение"):
            with self.subTest(text=text):
                item = app.extract_messages(mail('<table>' + digest_preview(text) + '</table>'))[0]
                self.assertEqual(item["text"], text)
        item = app.extract_messages(mail(single("Ответить")))[0]
        self.assertEqual(item["text"], "Ответить")

    def test_ignores_unrelated_mail_and_non_message_links(self):
        self.assertEqual(app.extract_messages(mail(single("Текст"), sender="other@example.invalid")), [])
        self.assertEqual(app.extract_messages(mail('<a href="https://vk.ru/im?tab=unread">Показать все</a>')), [])

    def test_forwarding_preserves_deduplication_and_marks_truncation(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE sent (hash TEXT PRIMARY KEY)")
        body = f'Текст<br><a href="{tracked(link())}">показать полностью...</a>'
        raw = mail(single(body))
        with patch.object(app, "send") as send:
            app.forward_mail(db, raw)
            app.forward_mail(db, raw)
        send.assert_called_once()
        self.assertIn("\n\nТекст", send.call_args.args[0])
        self.assertIn("только часть сообщения", send.call_args.args[0])
        fingerprint = hashlib.sha256(send.call_args.args[1].encode()).hexdigest()
        self.assertEqual(db.execute("SELECT hash FROM sent").fetchone()[0], fingerprint)
