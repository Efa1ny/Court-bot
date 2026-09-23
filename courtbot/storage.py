from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    user_id: int
    full_name: str
    position: str
    signature_url: str


@dataclass(frozen=True)
class Document:
    id: int
    user_id: int
    template_key: str
    template_name: str
    case_number: str
    content: str
    created_at: str


@dataclass(frozen=True)
class StoredForumThread:
    thread_id: int
    section: str
    stats_group: str
    url: str
    title: str
    status: str
    started_at: str
    last_poster: str
    last_post_at: str
    first_seen_at: str
    is_baseline: bool
    ping_count: int
    last_ping_at: str | None
    updated_at: str


class Storage:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        with suppress(OSError):
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    position TEXT NOT NULL,
                    signature_url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    template_key TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    case_number TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_documents_user_created
                ON documents(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS forum_threads (
                    thread_id INTEGER PRIMARY KEY,
                    section TEXT NOT NULL,
                    stats_group TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_poster TEXT NOT NULL,
                    last_post_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    is_baseline INTEGER NOT NULL DEFAULT 0,
                    ping_count INTEGER NOT NULL DEFAULT 0,
                    last_ping_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_forum_threads_status
                ON forum_threads(stats_group, status, last_post_at);

                CREATE TABLE IF NOT EXISTS forum_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def save_signature(self, user_id: int, signature_url: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO profiles(user_id, full_name, position, signature_url, updated_at)
                VALUES (?, '', '', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    signature_url = excluded.signature_url,
                    updated_at = excluded.updated_at
                """,
                (user_id, signature_url, now),
            )

    def get_signature_url(self, user_id: int) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT signature_url FROM profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return str(row["signature_url"]) if row else ""

    def save_profile(self, user_id: int, full_name: str, position: str, signature_url: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO profiles(user_id, full_name, position, signature_url, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    position = excluded.position,
                    signature_url = excluded.signature_url,
                    updated_at = excluded.updated_at
                """,
                (user_id, full_name, position, signature_url, now),
            )

    def get_profile(self, user_id: int) -> Profile | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT user_id, full_name, position, signature_url FROM profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return Profile(**dict(row)) if row else None

    def save_document(
        self,
        user_id: int,
        template_key: str,
        template_name: str,
        case_number: str,
        content: str,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO documents(
                    user_id, template_key, template_name, case_number, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, template_key, template_name, case_number, content, now),
            )
            return int(cursor.lastrowid)

    def list_documents(self, user_id: int, limit: int = 10) -> list[Document]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, user_id, template_key, template_name, case_number, content, created_at
                FROM documents WHERE user_id = ? ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [Document(**dict(row)) for row in rows]

    def get_document(self, user_id: int, document_id: int) -> Document | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, user_id, template_key, template_name, case_number, content, created_at
                FROM documents WHERE user_id = ? AND id = ?
                """,
                (user_id, document_id),
            ).fetchone()
        return Document(**dict(row)) if row else None

    def get_state(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM forum_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO forum_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_forum_thread(self, thread_id: int) -> StoredForumThread | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM forum_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return _row_to_forum_thread(row) if row else None

    def upsert_forum_thread(
        self,
        *,
        thread_id: int,
        section: str,
        stats_group: str,
        url: str,
        title: str,
        status: str,
        started_at: str,
        last_poster: str,
        last_post_at: str,
        is_baseline: bool,
        now: str,
    ) -> StoredForumThread:
        existing = self.get_forum_thread(thread_id)
        if existing is None:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO forum_threads(
                        thread_id, section, stats_group, url, title, status,
                        started_at, last_poster, last_post_at, first_seen_at,
                        is_baseline, ping_count, last_ping_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                    """,
                    (
                        thread_id,
                        section,
                        stats_group,
                        url,
                        title,
                        status,
                        started_at,
                        last_poster,
                        last_post_at,
                        now,
                        int(is_baseline),
                        now,
                    ),
                )
            stored = self.get_forum_thread(thread_id)
            assert stored is not None
            return stored

        with self._connect() as db:
            db.execute(
                """
                UPDATE forum_threads SET
                    section = ?, stats_group = ?, url = ?, title = ?, status = ?,
                    started_at = ?, last_poster = ?, last_post_at = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (
                    section,
                    stats_group,
                    url,
                    title,
                    status,
                    started_at,
                    last_poster,
                    last_post_at,
                    now,
                    thread_id,
                ),
            )
        stored = self.get_forum_thread(thread_id)
        assert stored is not None
        return stored

    def mark_forum_ping(self, thread_id: int, ping_count: int, pinged_at: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE forum_threads
                SET ping_count = ?, last_ping_at = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (ping_count, pinged_at, pinged_at, thread_id),
            )

    def mark_missing_open_forum_threads(self, seen_ids: set[int], scanned_at: str) -> None:
        """Exclude open threads absent from a complete forum scan without losing history."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT thread_id FROM forum_threads
                WHERE status IN ('none', 'pending') AND updated_at < ?
                """,
                (scanned_at,),
            ).fetchall()
            missing_ids = [row["thread_id"] for row in rows if row["thread_id"] not in seen_ids]
            db.executemany(
                """
                UPDATE forum_threads SET status = 'missing', updated_at = ?
                WHERE thread_id = ? AND status IN ('none', 'pending') AND updated_at < ?
                """,
                [(scanned_at, thread_id, scanned_at) for thread_id in missing_ids],
            )

    def list_open_forum_threads(self) -> list[StoredForumThread]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM forum_threads
                WHERE status IN ('none', 'pending')
                ORDER BY started_at ASC
                """
            ).fetchall()
        return [_row_to_forum_thread(row) for row in rows]

    def count_processed(self, stats_group: str, since_iso: str) -> int:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS total FROM forum_threads
                WHERE stats_group = ?
                  AND status IN ('reviewed', 'rejected')
                  AND last_post_at >= ?
                """,
                (stats_group, since_iso),
            ).fetchone()
        return int(row["total"]) if row else 0

    def top_reviewers(self, stats_group: str, since_iso: str, limit: int = 10) -> list[tuple[str, int]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT last_poster, COUNT(*) AS total FROM forum_threads
                WHERE stats_group = ?
                  AND status IN ('reviewed', 'rejected')
                  AND last_post_at >= ?
                GROUP BY last_poster
                ORDER BY total DESC, last_poster ASC
                LIMIT ?
                """,
                (stats_group, since_iso, limit),
            ).fetchall()
        return [(str(row["last_poster"]), int(row["total"])) for row in rows]


def _row_to_forum_thread(row: sqlite3.Row) -> StoredForumThread:
    payload = dict(row)
    payload["is_baseline"] = bool(payload["is_baseline"])
    return StoredForumThread(**payload)
