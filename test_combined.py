import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_combined as app


class CombinedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {
            "BOT_TOKEN": "fake_for_test",
            "TG_CHAT_ID": "11111111",
            "GMAIL_ADDRESS": "noreply@example.invalid",
            "GMAIL_APP_PASSWORD": "fake_for_test",
        }

    def test_env_is_safely_prepared_and_gmail_state_migrates(self):
        (self.root / "vk_mail_state.sqlite3").write_bytes(b"old-state")
        with mock.patch.object(app, "ROOT", self.root), mock.patch.dict(os.environ, self.env, clear=True):
            result = app.prepare_environment()
        self.assertEqual(result["TG_BOT_TOKEN"], "fake_for_test")
        self.assertEqual(result["REPLY_ENABLED"], "false")
        self.assertEqual(result["DEBUG"], "false")
        self.assertEqual(
            (self.root / "data" / "vk_mail_state.sqlite3").read_bytes(),
            b"old-state",
        )
        self.assertEqual(result["DB_FILE"], str(self.root / "data" / "vk_mail_state.sqlite3"))

    def test_missing_credentials_does_not_print_secret_values(self):
        with mock.patch.dict(os.environ, {"TG_CHAT_ID": "111"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TG_BOT_TOKEN"):
                app.prepare_environment()

    def test_gmail_runs_alone_without_max_credentials(self):
        with mock.patch.object(app, "ROOT", self.root):
            specs = app.service_specs(dict(self.env))
        self.assertEqual(set(specs), {"gmail"})
        self.assertTrue(specs["gmail"][0][2].endswith("gmail_bot.py"))

    def test_partial_max_credentials_does_not_launch_max(self):
        with mock.patch.object(app, "ROOT", self.root):
            specs = app.service_specs({**self.env, "MAX_TOKEN": "example_only"})
        self.assertEqual(set(specs), {"gmail"})

    def test_max_does_not_consume_telegram_updates(self):
        (self.root / "app").mkdir()
        (self.root / "app" / "main.py").write_text("# stub", encoding="utf-8")
        with mock.patch.object(app, "ROOT", self.root), mock.patch.object(app, "MAX_ROOT", self.root):
            specs = app.service_specs({
                **self.env, "MAX_TOKEN": "fake_for_test", "MAX_DEVICE_ID": "device_for_test"
            })
        self.assertEqual(set(specs), {"gmail", "max"})
        self.assertEqual(specs["max"][0][1:], ["-u", "-m", "app.main"])


if __name__ == "__main__":
    unittest.main()
