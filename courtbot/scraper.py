from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp

from .forum import (
    FORUM_ORIGIN,
    FORUMS,
    LIST_SPLIT,
    USER_AGENT,
    ForumThread,
    max_page_number,
    parse_forum_page,
)

LOGGER = logging.getLogger("courtbot")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
PAGE_DELAY = 0.4


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.text()


def forum_page_url(path: str, page: int) -> str:
    if page <= 1:
        return f"{FORUM_ORIGIN}{path}"
    trimmed = path.rstrip("/")
    return f"{FORUM_ORIGIN}{trimmed}/page-{page}"


async def fetch_forum_section(
    session: aiohttp.ClientSession,
    *,
    section: str,
    stats_group: str,
    path: str,
    max_pages: int | None = None,
    strict: bool = False,
) -> list[ForumThread]:
    first_html = await fetch_html(session, forum_page_url(path, 1))
    if strict and LIST_SPLIT not in first_html:
        raise ValueError(f"Не найдена лента тем форума: {path}")
    pages = max_page_number(first_html)
    if max_pages is not None:
        pages = min(pages, max_pages)
    threads = parse_forum_page(first_html, section, stats_group)
    for page in range(2, pages + 1):
        await asyncio.sleep(PAGE_DELAY)
        html = await fetch_html(session, forum_page_url(path, page))
        if strict and LIST_SPLIT not in html:
            raise ValueError(f"Не найдена лента тем форума: {path}, страница {page}")
        threads.extend(parse_forum_page(html, section, stats_group))
    return _unique_threads(threads)


async def fetch_all_forums(
    max_pages: int | None = None,
    *,
    strict: bool = False,
) -> list[ForumThread]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    collected: list[ForumThread] = []
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=headers) as session:
        for forum in FORUMS:
            try:
                collected.extend(
                    await fetch_forum_section(
                        session,
                        section=forum["section"],
                        stats_group=forum["stats_group"],
                        path=forum["path"],
                        max_pages=max_pages,
                        strict=strict,
                    )
                )
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                LOGGER.exception("Не удалось прочитать раздел форума %s", forum["path"])
                if strict:
                    raise
    return _unique_threads(collected)


def ping_is_due(ping_count: int, last_ping_at: datetime | None, now: datetime) -> bool:
    if ping_count >= 3:
        return False
    if ping_count == 0:
        return True
    if last_ping_at is None:
        return True
    delay = timedelta(hours=2 if ping_count == 1 else 3)
    return now >= last_ping_at + delay


def _unique_threads(threads: list[ForumThread]) -> list[ForumThread]:
    unique: dict[int, ForumThread] = {}
    for thread in threads:
        unique[thread.thread_id] = thread
    return list(unique.values())


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
