import hashlib
import sqlite3
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import gmail_bot
from vk_mail_parser import MISSING_TEXT, MAX_MAIL_BYTES, clean_vk_link, extract_messages

FIXTURES = Path(__file__).parent / 'fixtures'
AUTH = ('mx.google.com; dkim=pass header.i=@notify.vk.com header.s=mail; '
        'spf=pass smtp.mailfrom=bounce@notify.vk.com; '
        'dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=vk.com')


def fixture(name, ext='html'):
    return (FIXTURES / (name + '.' + ext)).read_text(encoding='utf-8')


def mail(html=None, plain=None, auth=AUTH, sender='admin@notify.vk.com', cte='quoted-printable'):
    msg = EmailMessage()
    msg['From'] = sender
    msg['Subject'] = 'A subject is not a sender or message timestamp'
    msg['Date'] = 'Sat, 10 Oct 2026 15:25:17 +0300'
    if auth is not None:
        msg['Authentication-Results'] = auth
    msg.set_content(plain or 'plain alternative', cte=cte)
    if html is not None:
        msg.add_alternative(html, subtype='html', cte=cte)
    return msg.as_bytes()


class TemplateTests(unittest.TestCase):
    def test_single_screenshot_layouts(self):
        for name, text, stamp, mid, cmid in [
            ('single_1302', 'Да, встречаемся около 8-9', '10 окт в 13:02', 201, 301),
            ('single_1503', 'К нам на встречу приедешь к пол 9?', '10 окт в 15:03', 202, 302),
        ]:
            for cte in ('base64', 'quoted-printable'):
                with self.subTest(name=name, cte=cte):
                    self.assertEqual(extract_messages(mail(fixture(name), fixture(name, 'txt'), cte=cte)), [{
                        'sender': 'Анна Примерова', 'text': text, 'time': stamp,
                        'url': f'https://vk.com/im?sel=101&msgid={mid}&cmid={cmid}'}])

    def test_digest_screenshot_layout(self):
        items = extract_messages(mail(fixture('digest'), fixture('digest', 'txt')))
        self.assertEqual([i['sender'] for i in items], ['Анна Примерова', 'Мария Тестова', 'Мария Тестова'])
        self.assertEqual([i['text'] for i in items], [MISSING_TEXT, 'Сервис недоступен', 'Ты сейчас на занятиях?'])
        self.assertEqual([i['time'] for i in items], ['10 окт в 11:01', '10 окт в 12:02', '10 окт в 12:02'])
        self.assertEqual([i['url'] for i in items], [
            'https://vk.com/im?sel=101&msgid=203&cmid=303',
            'https://vk.com/im?sel=102&msgid=204&cmid=304',
            'https://vk.com/im?sel=102&msgid=205&cmid=305'])

    def test_literal_service_word_and_timestamp_are_valid_message_text(self):
        for text in ['сообщение', 'Ответить', '10 окт в 13:02']:
            items = extract_messages(mail(fixture('digest').replace('Сервис недоступен', text)))
            self.assertEqual(items[1]['text'], text)
            items = extract_messages(mail(fixture('single_1302').replace('Да, встречаемся около 8-9', text)))
            self.assertEqual(items[0]['text'], text)

    def test_community_and_negative_peer(self):
        items = extract_messages(mail(fixture('community')))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sender'], 'Тестовое сообщество')
        self.assertIn('Тестовое сообщение сообщества', items[0]['text'])
        self.assertEqual(items[0]['time'], '9 окт в 18:44')
        self.assertIn('sel=-103&msgid=206', items[0]['url'])

    def test_plain_single_has_no_invented_timestamp(self):
        items = extract_messages(mail(plain=fixture('single_1302', 'txt')))
        self.assertEqual(items[0]['sender'], 'Анна Примерова')
        self.assertEqual(items[0]['text'], 'Да, встречаемся около 8-9')
        self.assertEqual(items[0]['time'], '')
        self.assertEqual(extract_messages(mail(plain=fixture('digest', 'txt'))), [])

    def test_plain_community_removes_repeated_permalink(self):
        item = extract_messages(mail(plain=fixture('community', 'txt')))[0]
        self.assertEqual(item['text'], 'Тестовое сообщение сообщества')

    def test_multiline_entities_inline_and_user_links(self):
        content = fixture('single_1302').replace('Да, встречаемся около 8-9',
            'Привет <b>мир</b>!<br>2 &lt; 3 &amp; <a href="https://example.org">ссылка</a>')
        self.assertEqual(extract_messages(mail(content))[0]['text'], 'Привет мир!\n2 < 3 & ссылка')

    def test_no_cross_card_sender_or_time(self):
        content = fixture('digest').replace('Мария Тестова</a>', '</a>', 1)
        items = extract_messages(mail(content))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[-1]['text'], 'Ты сейчас на занятиях?')
        self.assertEqual(items[-1]['sender'], 'Мария Тестова')

    def test_duplicate_cards_and_reply_buttons(self):
        items = extract_messages(mail(fixture('single_1302') * 2))
        self.assertEqual(len(items), 1)
        conflicting = fixture('single_1302') + fixture('single_1302').replace('Анна Примерова', 'Другой человек')
        self.assertEqual(extract_messages(mail(conflicting)), [])

    def test_attachments_are_not_notification_bodies(self):
        msg = EmailMessage()
        msg['From'] = 'admin@notify.vk.com'
        msg['Authentication-Results'] = AUTH
        msg.set_content('No notification')
        msg.add_attachment(fixture('single_1302'), subtype='html', filename='forwarded.html')
        self.assertEqual(extract_messages(msg.as_bytes()), [])

    def test_unknown_html_is_not_guessed_from_plain(self):
        self.assertEqual(extract_messages(mail('<p>Unknown layout</p>', fixture('single_1302', 'txt'))), [])

    def test_bad_charset_and_size(self):
        raw = mail(fixture('single_1302')).replace(b'charset="utf-8"', b'charset="unknown-codec"')
        self.assertEqual(extract_messages(raw), [])
        self.assertEqual(extract_messages(b'x' * (MAX_MAIL_BYTES + 1)), [])
        self.assertEqual(extract_messages(b'not an email'), [])


class AuthenticationTests(unittest.TestCase):
    def test_spoofed_or_unverified_mail(self):
        for auth in [None, AUTH.replace('dkim=pass', 'dkim=fail'), AUTH.replace('dmarc=pass', 'dmarc=fail'),
                     AUTH.replace('mx.google.com', 'attacker.example'), AUTH.replace('@notify.vk.com', '@attacker.example'),
                     AUTH.replace('header.from=vk.com', 'header.from=vk.com.attacker.example'),
                     'mx.google.com; spf=pass (dkim=pass header.i=@notify.vk.com; dmarc=pass header.from=vk.com)']:
            with self.subTest(auth=auth):
                self.assertEqual(extract_messages(mail(fixture('single_1302'), auth=auth)), [])
        for sender in ['VK <evil@example.org>', 'VK <admin@notify.vk.com.evil>',
                       'admin@notify.vk.com, evil@example.org']:
            self.assertEqual(extract_messages(mail(fixture('single_1302'), sender=sender)), [])

    def test_duplicate_headers_and_arc_only(self):
        raw = mail(fixture('single_1302'))
        self.assertEqual(extract_messages(b'From: admin@notify.vk.com\n' + raw), [])
        self.assertEqual(extract_messages(('Authentication-Results: ' + AUTH + '\n').encode() + raw), [])
        self.assertEqual(extract_messages(raw.replace(b'Authentication-Results:', b'ARC-Authentication-Results:')), [])


class LinkTests(unittest.TestCase):
    def test_strict_links(self):
        for link in ['http://vk.com/im?sel=1&msgid=2', 'https://vk.com.evil/im?sel=1&msgid=2',
                     'https://vk.com@evil/im?sel=1&msgid=2', 'https://evil@vk.com/im?sel=1&msgid=2',
                     'https://vk.com:invalid/im?sel=1&msgid=2', 'https://[broken',
                     'https://vk.com/im?sel=1&msgid=2&msgid=3', 'https://vk.com/im?sel=1',
                     'https://vk.com/im?msgid=2', 'https://vk.com/im?sel=1&msgid=oops',
                     'https://vk.com/im?sel=1&msgid=2#evil', 'javascript:alert(1)',
                     'https://vk.com/away.php?to=https://evil', 'https://vk.com/im?sel=1&msgid=2\n']:
            with self.subTest(link=link):
                self.assertIsNone(clean_vk_link(link))

    def test_tracking_depth_and_external_target(self):
        link = 'https://m.vk.ru/im?cmid=3&sel=-1&msgid=2&tracking=removed'
        expected = 'https://vk.com/im?sel=-1&msgid=2&cmid=3'
        self.assertEqual(clean_vk_link(link), expected)
        self.assertEqual(clean_vk_link('https://vk.ru/email.php?u=' + quote(link, safe='')), expected)
        self.assertIsNone(clean_vk_link('https://vk.ru/email.php?u=https%3A%2F%2Fevil.example'))
        for _ in range(6):
            link = 'https://vk.ru/email.php?u=' + quote(link, safe='')
        self.assertIsNone(clean_vk_link(link))


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE sent (hash TEXT PRIMARY KEY)')
        self.addCleanup(self.db.close)

    @patch.object(gmail_bot, 'VK_SENDER', 'admin@notify.vk.com')
    @patch.object(gmail_bot, 'send')
    def test_forward_once_and_keep_old_hashes(self, send):
        raw = mail(fixture('digest'))
        gmail_bot.forward_mail(self.db, raw)
        gmail_bot.forward_mail(self.db, raw)
        self.assertEqual(send.call_count, 3)
        self.assertIn('Время: 10 окт в 11:01', send.call_args_list[0].args[0])
        self.assertNotIn('EXAMPLE', str(send.call_args_list))
        # Existing single-message hashes continue suppressing already-sent mail.
        url = 'https://vk.com/im?sel=101&msgid=201&cmid=301'
        self.db.execute('INSERT INTO sent VALUES (?)', (hashlib.sha256(url.encode()).hexdigest(),))
        gmail_bot.forward_mail(self.db, mail(fixture('single_1302')))
        self.assertEqual(send.call_count, 3)

    @patch.object(gmail_bot, 'send', side_effect=RuntimeError('offline'))
    def test_failed_delivery_is_not_marked_sent(self, send):
        with self.assertRaises(RuntimeError):
            gmail_bot.forward_mail(self.db, mail(fixture('single_1302')))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sent').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
