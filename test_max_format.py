import unittest
from types import SimpleNamespace

from max_notify_format import format_header, message_time
from patch_max_client import OLD_HEADER, OLD_KEYBOARD, patch_source


class MaxFormatTests(unittest.TestCase):
    def test_personal_message_hides_numeric_chat_and_uses_moscow_time(self):
        msg = SimpleNamespace(chat_id=374259168, timestamp=1792016520000)
        header = format_header(msg, "Инь 🖤🤍", "374259168", False)
        self.assertEqual(header,
            "📩 <b>MAX</b>\nОтправитель: Инь 🖤🤍\nВремя: 15 окт в 01:22\n")
        self.assertNotIn("374259168", header)

    def test_group_title_and_escaped_labels_are_preserved(self):
        msg = SimpleNamespace(chat_id=-123, timestamp=None)
        header = format_header(msg, "Анна &amp; Иван", "ИТ &lt;чат&gt;", False)
        self.assertIn("Чат: ИТ &lt;чат&gt;", header)
        self.assertIn("Анна &amp; Иван", header)
        self.assertNotIn("Время:", header)

    def test_dm_placeholders_are_hidden(self):
        msg = SimpleNamespace(chat_id=123, timestamp=None)
        for label, is_dm in (("DM:456", False), ("Инь", True), ("Инь", False)):
            self.assertNotIn("Чат:", format_header(msg, "Инь", label, is_dm))

    def test_seconds_milliseconds_and_invalid_timestamps(self):
        self.assertEqual(message_time(1792016520), message_time(1792016520000))
        for value in (None, "", "bad", 0, -1, float("inf"), float("nan")):
            self.assertEqual(message_time(value), "")

    def test_patch_fails_closed_when_pinned_source_changes(self):
        source = OLD_HEADER + "\n" + OLD_KEYBOARD
        result = patch_source(source)
        self.assertIn("format_header as _header", result)
        self.assertIn("else open_max_keyboard()", result)
        with self.assertRaises(RuntimeError):
            patch_source(result)
        with self.assertRaises(RuntimeError):
            patch_source("unexpected upstream")


if __name__ == "__main__":
    unittest.main()
