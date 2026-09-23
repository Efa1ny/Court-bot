from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

FORUM_ID_FIELDS = (
    "FORUM_ALERT_CHANNEL_ID",
    "ROLE_SUPREME_CHAIR_ID",
    "ROLE_SUPREME_JUDGE_ID",
    "ROLE_FEDERAL_CHAIR_ID",
    "ROLE_FEDERAL_JUDGE_ID",
    "ROLE_JUDICIAL_CORPS_ID",
)


def _parse_ids(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError(f"Некорректный Discord ID в конфигурации: {part}") from exc
        if value <= 0:
            raise ValueError(f"Discord ID должен быть положительным числом: {part}")
        values.add(value)
    return frozenset(values)


def _parse_id(raw: str, field: str) -> int:
    value = raw.strip()
    if not value:
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} должен содержать только цифры") from exc
    if parsed <= 0:
        raise ValueError(f"{field} должен быть положительным числом")
    return parsed


def _resolve_path(raw: str, default: str) -> Path:
    path = Path(raw or default)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    token: str
    test_guild_id: int | None
    allowed_role_ids: frozenset[int]
    templates_dir: Path
    database_path: Path
    forum_monitor_enabled: bool
    forum_alert_channel_id: int
    role_supreme_chair_id: int
    role_supreme_judge_id: int
    role_federal_chair_id: int
    role_federal_judge_id: int
    role_judicial_corps_id: int
    google_sheets_id: str = ""
    google_service_account_json: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        # ``replace_me`` is the public marker used by .env.example.
        if not token or token == "replace_me":  # nosec B105
            raise RuntimeError("Укажите DISCORD_TOKEN в файле .env")

        guild_raw = os.getenv("TEST_GUILD_ID", "").strip()
        try:
            test_guild_id = int(guild_raw) if guild_raw else None
        except ValueError as exc:
            raise ValueError("TEST_GUILD_ID должен содержать только цифры") from exc
        if test_guild_id is not None and test_guild_id <= 0:
            raise ValueError("TEST_GUILD_ID должен быть положительным числом")

        enabled_raw = os.getenv("FORUM_MONITOR_ENABLED", "0").strip().casefold()
        forum_monitor_enabled = enabled_raw not in {"0", "false", "no", "off"}
        if forum_monitor_enabled:
            missing = [field for field in FORUM_ID_FIELDS if not os.getenv(field, "").strip()]
            if missing:
                raise ValueError(
                    "Для мониторинга форума задайте Discord ID: " + ", ".join(missing)
                )

        return cls(
            token=token,
            test_guild_id=test_guild_id,
            allowed_role_ids=_parse_ids(os.getenv("ALLOWED_ROLE_IDS", "")),
            templates_dir=_resolve_path(os.getenv("TEMPLATES_DIR", ""), "templates"),
            database_path=_resolve_path(os.getenv("DATABASE_PATH", ""), "data/court_bot.db"),
            forum_monitor_enabled=forum_monitor_enabled,
            forum_alert_channel_id=_parse_id(
                os.getenv("FORUM_ALERT_CHANNEL_ID", ""),
                "FORUM_ALERT_CHANNEL_ID",
            ),
            role_supreme_chair_id=_parse_id(
                os.getenv("ROLE_SUPREME_CHAIR_ID", ""),
                "ROLE_SUPREME_CHAIR_ID",
            ),
            role_supreme_judge_id=_parse_id(
                os.getenv("ROLE_SUPREME_JUDGE_ID", ""),
                "ROLE_SUPREME_JUDGE_ID",
            ),
            role_federal_chair_id=_parse_id(
                os.getenv("ROLE_FEDERAL_CHAIR_ID", ""),
                "ROLE_FEDERAL_CHAIR_ID",
            ),
            role_federal_judge_id=_parse_id(
                os.getenv("ROLE_FEDERAL_JUDGE_ID", ""),
                "ROLE_FEDERAL_JUDGE_ID",
            ),
            role_judicial_corps_id=_parse_id(
                os.getenv("ROLE_JUDICIAL_CORPS_ID", ""),
                "ROLE_JUDICIAL_CORPS_ID",
            ),
            google_sheets_id=os.getenv("GOOGLE_SHEETS_ID", "").strip(),
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        )
