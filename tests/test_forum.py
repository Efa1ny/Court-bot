import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from courtbot.forum import ForumThread, format_forum_date, max_page_number, parse_forum_page
from courtbot.monitor import period_start
from courtbot.storage import Storage


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "forum_list.html"
MOSCOW = ZoneInfo("Europe/Moscow")


class ForumParserTests(unittest.TestCase):
    def test_parses_non_sticky_threads_and_statuses(self):
        html = FIXTURE.read_text(encoding="utf-8")
        threads = parse_forum_page(html, "supreme", "claims")
        self.assertEqual([item.thread_id for item in threads], [3418114, 3417517, 3419001])
        self.assertEqual(threads[0].status, "pending")
        self.assertEqual(threads[0].title, "Исковое заявление №248")
        self.assertEqual(threads[0].last_poster, "di__")
        self.assertEqual(format_forum_date(threads[0].started_at), "12.08.2026")
        self.assertEqual(threads[1].status, "rejected")
        self.assertEqual(threads[2].status, "none")
        self.assertEqual(threads[2].last_poster, "new_user")
        self.assertEqual(max_page_number(html), 15)

    def test_stats_group_claims_separately_from_rehab(self):
        storage = Storage(Path(self._test_dir()) / "forum.db")
        now = datetime(2026, 8, 18, 15, 0, tzinfo=MOSCOW)
        storage.upsert_forum_thread(
            thread_id=1,
            section="supreme",
            stats_group="claims",
            url="https://example.com/1",
            title="Иск 1",
            status="reviewed",
            started_at=now.isoformat(),
            last_poster="di__",
            last_post_at=now.isoformat(),
            is_baseline=True,
            now=now.isoformat(),
        )
        storage.upsert_forum_thread(
            thread_id=2,
            section="rehab",
            stats_group="rehab",
            url="https://example.com/2",
            title="Реабилитация 1",
            status="rejected",
            started_at=now.isoformat(),
            last_poster="Efa1ny",
            last_post_at=now.isoformat(),
            is_baseline=True,
            now=now.isoformat(),
        )
        since = period_start(now, "day").isoformat()
        self.assertEqual(storage.count_processed("claims", since), 1)
        self.assertEqual(storage.count_processed("rehab", since), 1)
        self.assertEqual(storage.top_reviewers("claims", since), [("di__", 1)])
        self.assertEqual(storage.top_reviewers("rehab", since), [("Efa1ny", 1)])

    def test_stats_text_includes_day_week_month(self):
        from courtbot.config import Settings
        from courtbot.monitor import ForumMonitor

        storage = Storage(Path(self._test_dir()) / "stats.db")
        now = datetime(2026, 8, 18, 15, 0, tzinfo=MOSCOW)
        storage.upsert_forum_thread(
            thread_id=10,
            section="federal",
            stats_group="claims",
            url="https://example.com/10",
            title="Иск 10",
            status="reviewed",
            started_at=now.isoformat(),
            last_poster="oxyblade666",
            last_post_at=now.isoformat(),
            is_baseline=True,
            now=now.isoformat(),
        )
        settings = Settings(
            token="test",
            test_guild_id=None,
            allowed_role_ids=frozenset(),
            templates_dir=Path("."),
            database_path=Path(self._test_dir()) / "unused.db",
            forum_monitor_enabled=False,
            forum_alert_channel_id=1,
            role_supreme_chair_id=2,
            role_supreme_judge_id=3,
            role_federal_chair_id=4,
            role_federal_judge_id=5,
            role_judicial_corps_id=6,
        )
        text = ForumMonitor(object(), settings, storage).stats_text(now)
        self.assertIn("За день: **1**", text)
        self.assertIn("За неделю: **1**", text)
        self.assertIn("За месяц: **1**", text)
        self.assertIn("Топ за день: oxyblade666 — 1", text)

    def test_midnight_summary_uses_numbered_embed_without_stats(self):
        from courtbot.config import Settings
        from courtbot.monitor import ALERT_EMBED_COLOR, ForumMonitor

        storage = Storage(Path(self._test_dir()) / "summary.db")
        now = datetime(2026, 8, 19, 0, 5, tzinfo=MOSCOW)
        storage.upsert_forum_thread(
            thread_id=21,
            section="supreme",
            stats_group="claims",
            url="https://example.com/21",
            title="Иск без тега",
            status="none",
            started_at=now.isoformat(),
            last_poster="user",
            last_post_at=now.isoformat(),
            is_baseline=True,
            now=now.isoformat(),
        )
        storage.upsert_forum_thread(
            thread_id=22,
            section="federal",
            stats_group="claims",
            url="https://example.com/22",
            title="Иск на рассмотрении",
            status="pending",
            started_at=now.isoformat(),
            last_poster="judge",
            last_post_at=now.isoformat(),
            is_baseline=True,
            now=now.isoformat(),
        )
        storage.upsert_forum_thread(
            thread_id=23,
            section="supreme",
            stats_group="claims",
            url="https://example.com/23",
            title="Рассмотренный иск",
            status="reviewed",
            started_at=now.isoformat(),
            last_poster="oxyblade666",
            last_post_at=now.isoformat(),
            is_baseline=True,
            now=now.isoformat(),
        )
        settings = Settings(
            token="test",
            test_guild_id=None,
            allowed_role_ids=frozenset(),
            templates_dir=Path("."),
            database_path=Path(self._test_dir()) / "unused.db",
            forum_monitor_enabled=False,
            forum_alert_channel_id=1,
            role_supreme_chair_id=2,
            role_supreme_judge_id=3,
            role_federal_chair_id=4,
            role_federal_judge_id=5,
            role_judicial_corps_id=6,
        )
        monitor = ForumMonitor(object(), settings, storage)
        description = monitor.midnight_summary_description(now)
        self.assertIn("Сводка суда за 19.08.2026", description)
        self.assertIn("**Не взяты в работу:**", description)
        self.assertIn("**На рассмотрении:**", description)
        self.assertIn("**1. [Иск без тега](https://example.com/21)** — 19.08.2026", description)
        self.assertIn(
            "**1. [Иск на рассмотрении](https://example.com/22)** — 19.08.2026",
            description,
        )
        self.assertNotIn("Статистика рассмотрения", description)
        self.assertNotIn("Топ за", description)
        self.assertNotIn("oxyblade666", description)
        embed = monitor.alert_embeds(description)[0]
        self.assertEqual(embed.colour, ALERT_EMBED_COLOR)
        self.assertEqual(embed.description, description)

    def test_forced_summary_requires_alert_channel(self):
        import asyncio

        from courtbot.config import Settings
        from courtbot.monitor import ForumMonitor

        storage = Storage(Path(self._test_dir()) / "unconfigured.db")
        settings = Settings(
            token="test",
            test_guild_id=None,
            allowed_role_ids=frozenset(),
            templates_dir=Path("."),
            database_path=Path(self._test_dir()) / "unused.db",
            forum_monitor_enabled=False,
            forum_alert_channel_id=0,
            role_supreme_chair_id=0,
            role_supreme_judge_id=0,
            role_federal_chair_id=0,
            role_federal_judge_id=0,
            role_judicial_corps_id=0,
        )
        monitor = ForumMonitor(object(), settings, storage)
        with self.assertRaisesRegex(ValueError, "FORUM_ALERT_CHANNEL_ID"):
            asyncio.run(monitor.run_forced_summary())

    def test_new_claim_is_mentioned_only_once(self):
        import asyncio

        from courtbot.config import Settings
        from courtbot.monitor import ForumMonitor

        storage = Storage(Path(self._test_dir()) / "one-ping.db")
        settings = Settings(
            token="test",
            test_guild_id=None,
            allowed_role_ids=frozenset(),
            templates_dir=Path("."),
            database_path=Path(self._test_dir()) / "unused.db",
            forum_monitor_enabled=False,
            forum_alert_channel_id=1,
            role_supreme_chair_id=2,
            role_supreme_judge_id=3,
            role_federal_chair_id=4,
            role_federal_judge_id=5,
            role_judicial_corps_id=6,
        )
        monitor = ForumMonitor(object(), settings, storage)
        channel = AsyncMock()
        monitor.get_alert_channel = AsyncMock(return_value=channel)
        claim = ForumThread(
            thread_id=42,
            section="supreme",
            stats_group="claims",
            title="Исковое заявление №42",
            url="https://example.com/42",
            status="none",
            started_at=datetime(2026, 8, 18, tzinfo=MOSCOW),
            last_poster="claimant",
            last_post_at=datetime(2026, 8, 18, tzinfo=MOSCOW),
        )

        asyncio.run(monitor.process_threads([], full_scan=True))
        asyncio.run(monitor.process_threads([claim], full_scan=False))
        asyncio.run(monitor.process_threads([claim], full_scan=False))

        self.assertEqual(channel.send.await_count, 1)
        stored = storage.get_forum_thread(claim.thread_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.ping_count, 1)

        # A short poll cannot establish that an older claim was deleted.
        asyncio.run(monitor.process_threads([], full_scan=False))
        self.assertIn("Исковое заявление №42", monitor.midnight_summary_description())

        # A complete scan can remove it from reports, while preserving its
        # record and original one-time notification if it reappears.
        asyncio.run(monitor.process_threads([], full_scan=True))
        self.assertEqual(storage.get_forum_thread(claim.thread_id).status, "missing")
        self.assertNotIn("Исковое заявление №42", monitor.midnight_summary_description())
        asyncio.run(monitor.process_threads([claim], full_scan=True))
        self.assertEqual(storage.get_forum_thread(claim.thread_id).status, "none")
        self.assertEqual(channel.send.await_count, 1)

    def test_incomplete_full_scan_fails_before_reconciliation(self):
        import asyncio

        from courtbot.scraper import fetch_all_forums

        with patch("courtbot.scraper.fetch_forum_section", new_callable=AsyncMock) as fetch:
            fetch.side_effect = OSError("forum unavailable")
            with self.assertRaises(OSError):
                asyncio.run(fetch_all_forums(strict=True))

    def _test_dir(self) -> str:
        directory = getattr(self, "directory", None)
        if directory is None:
            import tempfile

            self.directory = tempfile.TemporaryDirectory()
        return self.directory.name

    def tearDown(self):
        directory = getattr(self, "directory", None)
        if directory is not None:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
