from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

FORUM_ORIGIN = "https://forum.gta5rp.com"
USER_AGENT = "CourtBot/1.0 (+https://forum.gta5rp.com; lawsuit monitor)"

FORUMS = (
    {
        "section": "supreme",
        "stats_group": "claims",
        "path": "/forums/verkhovnyi-sud.1664/",
        "title": "Верховный суд",
    },
    {
        "section": "federal",
        "stats_group": "claims",
        "path": "/forums/federal-nyi-sud.1660/",
        "title": "Федеральный суд",
    },
    {
        "section": "rehab",
        "stats_group": "rehab",
        "path": "/forums/zayavleniya-na-annulirovaniye-sudimostei.1663/",
        "title": "Юридическая реабилитация",
    },
)

STATUS_ALIASES = {
    "на рассмотрении": "pending",
    "рассмотрено": "reviewed",
    "отказано": "rejected",
}

THREAD_START_RE = re.compile(r'<div class="structItem structItem--thread\b')
THREAD_ID_RE = re.compile(r"js-threadListItem-(\d+)")
LABEL_RE = re.compile(
    r'<span class="label[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
    re.DOTALL,
)
TITLE_RE = re.compile(
    r'<a href="(/threads/[^"]+/)"[^>]*data-tp-primary="on"[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL,
)
START_RE = re.compile(
    r'structItem-startDate.*?<time[^>]*datetime="([^"]+)"',
    re.DOTALL,
)
LATEST_CELL_RE = re.compile(
    r'structItem-cell--latest(.*)$',
    re.DOTALL,
)
LATEST_TIME_RE = re.compile(r'structItem-latestDate[^>]*datetime="([^"]+)"')
USERNAME_RE = re.compile(r'class="username[^"]*"[^>]*>\s*([^<]+?)\s*<')
PAGE_RE = re.compile(r"/page-(\d+)")
LIST_SPLIT = "structItemContainer-group js-threadList"


@dataclass(frozen=True)
class ForumThread:
    thread_id: int
    section: str
    stats_group: str
    title: str
    url: str
    status: str
    started_at: datetime
    last_poster: str
    last_post_at: datetime


def parse_status(label: str | None) -> str:
    if not label:
        return "none"
    return STATUS_ALIASES.get(" ".join(label.split()).casefold(), "none")


def parse_forum_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_forum_date(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")


def max_page_number(html: str) -> int:
    numbers = [int(item) for item in PAGE_RE.findall(html)]
    return max(numbers, default=1)


def _clean_text(value: str) -> str:
    return unescape(re.sub(r"\s+", " ", value).strip())


def _parse_thread_block(block: str, section: str, stats_group: str) -> ForumThread | None:
    id_match = THREAD_ID_RE.search(block)
    title_match = TITLE_RE.search(block)
    start_match = START_RE.search(block)
    if not id_match or not title_match or not start_match:
        return None

    latest_cell = LATEST_CELL_RE.search(block)
    latest_html = latest_cell.group(1) if latest_cell else block
    latest_time = LATEST_TIME_RE.search(latest_html)
    last_poster_match = USERNAME_RE.search(latest_html)
    if latest_time is None or last_poster_match is None:
        return None

    label_match = LABEL_RE.search(block)
    started_at = parse_forum_datetime(start_match.group(1))
    last_post_at = parse_forum_datetime(latest_time.group(1))
    return ForumThread(
        thread_id=int(id_match.group(1)),
        section=section,
        stats_group=stats_group,
        title=_clean_text(title_match.group(2)),
        url=urljoin(FORUM_ORIGIN, title_match.group(1)),
        status=parse_status(label_match.group(1) if label_match else None),
        started_at=started_at,
        last_poster=_clean_text(last_poster_match.group(1)),
        last_post_at=last_post_at,
    )


def parse_forum_page(html: str, section: str, stats_group: str) -> list[ForumThread]:
    list_html = html.split(LIST_SPLIT, 1)[-1] if LIST_SPLIT in html else html
    starts = [match.start() for match in THREAD_START_RE.finditer(list_html)]
    threads: list[ForumThread] = []
    seen: set[int] = set()
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(list_html)
        parsed = _parse_thread_block(list_html[start:end], section, stats_group)
        if parsed is None or parsed.thread_id in seen:
            continue
        seen.add(parsed.thread_id)
        threads.append(parsed)
    return threads
