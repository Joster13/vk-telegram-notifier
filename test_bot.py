import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import Seen, deliver_attachment, deliver_message

class FakeVK:
    def get_message(self, ident):
        return ({'id': ident, 'peer_id': -123, 'from_id': -123, 'out': 0,
                 'text': 'Привет <&>!', 'date': 1700000000,
                 'attachments': [{'type': 'photo', 'photo': {'sizes': [
                     {'url': 'https://sun9-1.userapi.com/p.jpg', 'width': 512, 'height': 512}
                 ]}}]}, [], [{'id': 123, 'name': 'Магазин & тест'}])

class FakeTG:
    def __init__(self):
        self.messages = []
        self.attachments = []
    def message(self, text, **kwargs):
        self.messages.append((text, kwargs))
    def attachment(self, method, arg, value, caption):
        self.attachments.append((method, arg, value, caption))

class DeliveryTests(unittest.TestCase):
    def test_community_notification_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = Seen(str(Path(tmp) / 'seen.sqlite3'))
            tg = FakeTG()
            deliver_message(FakeVK(), tg, 72, seen)
            deliver_message(FakeVK(), tg, 72, seen)
            self.assertEqual(len(tg.messages), 1)
            self.assertEqual(len(tg.attachments), 1)
            self.assertIn('Магазин &amp; тест', tg.messages[0][0])
            self.assertIn('Привет &lt;&amp;&gt;!', tg.messages[0][0])
            self.assertIn('sel=-123&msgid=72', tg.messages[0][1]['link'])
            self.assertEqual(tg.attachments[0][0], 'sendPhoto')
            seen.db.close()

    def test_video_without_mp4_has_fallback_link(self):
        tg = FakeTG()
        deliver_attachment(tg, {'type': 'video', 'video': {'id': 44, 'owner_id': -7, 'title': 'Новости'}},
                           'https://vk.com/im?sel=-123&msgid=1')
        self.assertEqual(len(tg.messages), 1)
        self.assertIn('https://vk.com/video-7_44', tg.messages[0][0])

    def test_sticker_image(self):
        tg = FakeTG()
        deliver_attachment(tg, {'type': 'sticker', 'sticker': {
            'images': [{'url':'https://cdn.vk.com/1.png','width': 256,'height':256}]}},
            'https://vk.com/im?sel=1')
        self.assertEqual(tg.attachments[0][0], 'sendPhoto')

if __name__ == '__main__':
    unittest.main()
