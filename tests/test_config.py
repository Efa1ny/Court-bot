import os
import unittest
from unittest.mock import patch

from courtbot.config import BASE_DIR, Settings


class ConfigTests(unittest.TestCase):
    def test_settings_from_env(self):
        values = {
            "DISCORD_TOKEN": "test-token",
            "TEST_GUILD_ID": "123",
            "ALLOWED_ROLE_IDS": "10, 20,10",
            "TEMPLATES_DIR": "templates",
            "DATABASE_PATH": "data/test.db",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.test_guild_id, 123)
        self.assertEqual(settings.allowed_role_ids, frozenset({10, 20}))
        self.assertEqual(settings.templates_dir, BASE_DIR / "templates")
        self.assertEqual(settings.database_path, BASE_DIR / "data/test.db")
        self.assertFalse(settings.forum_monitor_enabled)
        self.assertEqual(settings.forum_alert_channel_id, 0)
        self.assertEqual(settings.google_sheets_id, "")

    def test_monitoring_requires_explicit_discord_ids(self):
        with (
            patch.dict(
                os.environ,
                {"DISCORD_TOKEN": "test-token", "FORUM_MONITOR_ENABLED": "1"},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "FORUM_ALERT_CHANNEL_ID"),
        ):
            Settings.from_env()

    def test_monitoring_uses_ids_from_environment(self):
        values = {
            "DISCORD_TOKEN": "test-token",
            "FORUM_MONITOR_ENABLED": "1",
            "FORUM_ALERT_CHANNEL_ID": "1",
            "ROLE_SUPREME_CHAIR_ID": "2",
            "ROLE_SUPREME_JUDGE_ID": "3",
            "ROLE_FEDERAL_CHAIR_ID": "4",
            "ROLE_FEDERAL_JUDGE_ID": "5",
            "ROLE_JUDICIAL_CORPS_ID": "6",
            "GOOGLE_SHEETS_ID": "example-spreadsheet",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.from_env()

        self.assertTrue(settings.forum_monitor_enabled)
        self.assertEqual(settings.forum_alert_channel_id, 1)
        self.assertEqual(settings.role_judicial_corps_id, 6)
        self.assertEqual(settings.google_sheets_id, "example-spreadsheet")

    def test_missing_token_is_rejected(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(RuntimeError),
        ):
            Settings.from_env()

    def test_invalid_discord_id_is_rejected(self):
        with (
            patch.dict(
                os.environ,
                {"DISCORD_TOKEN": "test-token", "ALLOWED_ROLE_IDS": "not-a-number"},
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            Settings.from_env()

    def test_non_positive_discord_id_is_rejected(self):
        with (
            patch.dict(
                os.environ,
                {"DISCORD_TOKEN": "test-token", "ALLOWED_ROLE_IDS": "-1"},
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            Settings.from_env()


if __name__ == "__main__":
    unittest.main()
