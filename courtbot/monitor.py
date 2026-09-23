from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

from .config import Settings
from .forum import ForumThread, format_forum_date
from .scraper import fetch_all_forums, to_utc_iso
from .sheets import SheetsError
from .storage import Storage, StoredForumThread

LOGGER = logging.getLogger("courtbot")
MOSCOW = ZoneInfo("Europe/Moscow")
ALERT_EMBED_COLOR = discord.Color.from_rgb(88, 166, 255)
EMBED_DESCRIPTION_LIMIT = 4096
SECTION_TITLES = {
    "supreme": "Верховный суд",
    "federal": "Федеральный суд",
    "rehab": "Юридическая реабилитация",
}


class ForumMonitor:
    def __init__(self, bot: discord.Client, settings: Settings, storage: Storage):
        self.bot = bot
        self.settings = settings
        self.storage = storage
        self.poll_forums.change_interval(minutes=5)
        self.midnight_summary.change_interval(time=time(hour=0, minute=0, tzinfo=MOSCOW))

    def start(self) -> None:
        if not self.settings.forum_monitor_enabled:
            LOGGER.info("Мониторинг форума отключён")
            return
        if not self.poll_forums.is_running():
            self.poll_forums.start()
        if not self.midnight_summary.is_running():
            self.midnight_summary.start()

    def stop(self) -> None:
        self.poll_forums.cancel()
        self.midnight_summary.cancel()

    def mention(self, role_id: int) -> str:
        return f"<@&{role_id}>"

    def ping_roles(self, section: str) -> str:
        settings = self.settings
        if section == "supreme":
            roles = (settings.role_supreme_judge_id, settings.role_supreme_chair_id)
        elif section == "federal":
            roles = (settings.role_federal_judge_id, settings.role_federal_chair_id)
        else:
            roles = (settings.role_supreme_chair_id,)
        return " ".join(self.mention(role_id) for role_id in roles)

    def forum_date(self, started_at: datetime | str) -> str:
        if isinstance(started_at, str):
            started = datetime.fromisoformat(started_at)
        else:
            started = started_at
        return format_forum_date(started)

    def numbered_thread_lines(self, threads: Sequence[StoredForumThread | ForumThread]) -> str:
        if not threads:
            return "нет"
        lines = []
        for index, item in enumerate(threads, start=1):
            date = self.forum_date(item.started_at)
            lines.append(f"**{index}. [{item.title}]({item.url})** — {date}")
        return "\n".join(lines)

    def split_embed_text(self, text: str, limit: int = EMBED_DESCRIPTION_LIMIT) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        if current:
            chunks.append(current)
        return chunks

    def alert_embeds(self, description: str) -> list[discord.Embed]:
        return [
            discord.Embed(description=chunk, colour=ALERT_EMBED_COLOR)
            for chunk in self.split_embed_text(description)
        ]

    def midnight_summary_description(self, now: datetime | None = None) -> str:
        now = now or datetime.now(tz=MOSCOW)
        today = now.strftime("%d.%m.%Y")
        open_threads = self.storage.list_open_forum_threads()
        none_threads = [item for item in open_threads if item.status == "none"]
        pending_threads = [item for item in open_threads if item.status == "pending"]
        return "\n".join(
            (
                f"Сводка суда за {today}",
                "",
                "**Не взяты в работу:**",
                self.numbered_thread_lines(none_threads),
                "",
                "**На рассмотрении:**",
                self.numbered_thread_lines(pending_threads),
            )
        )

    async def get_alert_channel(self) -> discord.abc.Messageable | None:
        if self.settings.forum_alert_channel_id <= 0:
            return None
        channel = self.bot.get_channel(self.settings.forum_alert_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.settings.forum_alert_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Не удалось открыть канал мониторинга форума")
                return None
        if not isinstance(channel, discord.abc.Messageable):
            LOGGER.error("Канал мониторинга форума не принимает сообщения")
            return None
        return channel

    async def send_alert(
        self,
        channel: discord.abc.Messageable,
        content: str,
        *descriptions: str,
    ) -> None:
        mentions = discord.AllowedMentions(roles=True)
        embeds: list[discord.Embed] = []
        for description in descriptions:
            embeds.extend(self.alert_embeds(description))
        if not embeds:
            return
        for index in range(0, len(embeds), 10):
            batch = embeds[index : index + 10]
            await channel.send(
                content=content if index == 0 else None,
                embeds=batch,
                allowed_mentions=mentions,
            )

    async def process_threads(self, threads: list[ForumThread], *, full_scan: bool) -> None:
        now = datetime.now(tz=MOSCOW)
        now_iso = to_utc_iso(now)
        baseline_done = self.storage.get_state("forum_baseline_done") == "1"
        channel = await self.get_alert_channel()

        for thread in threads:
            stored = self.storage.upsert_forum_thread(
                thread_id=thread.thread_id,
                section=thread.section,
                stats_group=thread.stats_group,
                url=thread.url,
                title=thread.title,
                status=thread.status,
                started_at=to_utc_iso(thread.started_at),
                last_poster=thread.last_poster,
                last_post_at=to_utc_iso(thread.last_post_at),
                is_baseline=not baseline_done,
                now=now_iso,
            )
            if (
                channel is None
                or not baseline_done
                or stored.is_baseline
                or thread.status != "none"
            ):
                continue
            # A new claim should notify the responsible judges once. Repeating
            # role mentions on later polling cycles creates unnecessary Discord
            # notifications while the claim remains open.
            if stored.ping_count > 0:
                continue
            mentions = self.ping_roles(thread.section)
            description = "\n".join(
                (
                    "Иск не взят в работу",
                    "",
                    f"**1. [{thread.title}]({thread.url})** — {format_forum_date(thread.started_at)}",
                )
            )
            await self.send_alert(channel, mentions, description)
            self.storage.mark_forum_ping(thread.thread_id, stored.ping_count + 1, now_iso)
            LOGGER.info(
                "Пинг незакрытого иска %s (%s), попытка %s",
                thread.thread_id,
                thread.section,
                stored.ping_count + 1,
            )

        if full_scan:
            self.storage.mark_missing_open_forum_threads(
                {thread.thread_id for thread in threads}, now_iso
            )

        if full_scan and not baseline_done:
            self.storage.set_state("forum_baseline_done", "1")
            LOGGER.info("Первичная индексация форума завершена, пинги старых тем отключены")

    @tasks.loop(minutes=5)
    async def poll_forums(self) -> None:
        baseline_done = self.storage.get_state("forum_baseline_done") == "1"
        max_pages = None if not baseline_done else 2
        threads = await fetch_all_forums(max_pages=max_pages, strict=not baseline_done)
        await self.process_threads(threads, full_scan=not baseline_done)

    @poll_forums.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=MOSCOW))
    async def midnight_summary(self) -> None:
        threads = await fetch_all_forums(strict=True)
        await self.process_threads(threads, full_scan=True)
        await self.send_midnight_summary()

    @midnight_summary.before_loop
    async def before_midnight(self) -> None:
        await self.bot.wait_until_ready()

    async def send_midnight_summary(self) -> None:
        channel = await self.get_alert_channel()
        if channel is None:
            return
        descriptions = [self.midnight_summary_description()]
        sheets = getattr(self.bot, "sheets", None)
        if sheets is not None:
            try:
                descriptions.append(await sheets.treasury_description())
            except SheetsError as exc:
                LOGGER.exception("Не удалось сформировать сводку по таблице: %s", exc)
                descriptions.append(
                    "Сводка по штрафам и выплатам\n\nНе удалось прочитать таблицу."
                )
        await self.send_alert(
            channel,
            self.mention(self.settings.role_judicial_corps_id),
            *descriptions,
        )

    async def run_forced_summary(self) -> None:
        if self.settings.forum_alert_channel_id <= 0:
            raise ValueError("Для отправки сводки задайте FORUM_ALERT_CHANNEL_ID")
        threads = await fetch_all_forums(strict=True)
        await self.process_threads(threads, full_scan=True)
        await self.send_midnight_summary()

    def stats_text(self, now: datetime | None = None) -> str:
        now = now or datetime.now(tz=MOSCOW)
        periods = (
            ("день", "day"),
            ("неделю", "week"),
            ("месяц", "month"),
            ("год", "year"),
        )
        parts = ["Статистика рассмотрения"]
        for group, title in (("claims", "Иски ВС + ФС"), ("rehab", "Юридическая реабилитация")):
            parts.append(f"\n**{title}**")
            for label, period in periods:
                start = to_utc_iso(period_start(now, period))
                total = self.storage.count_processed(group, start)
                parts.append(f"За {label}: **{total}**")
                top = self.storage.top_reviewers(group, start, limit=5)
                if top:
                    ranking = ", ".join(f"{name} — {count}" for name, count in top)
                    parts.append(f"Топ за {label}: {ranking}")
                else:
                    parts.append(f"Топ за {label}: пока пусто")
        return "\n".join(parts)


def period_start(now: datetime, period: str) -> datetime:
    local = now.astimezone(MOSCOW)
    if period == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        monday = local - timedelta(days=local.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
