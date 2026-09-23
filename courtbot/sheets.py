from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from .config import BASE_DIR, Settings

LOGGER = logging.getLogger("courtbot")
MOSCOW = ZoneInfo("Europe/Moscow")
EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=MOSCOW)
CACHE_TTL = timedelta(minutes=5)
HEADER_SEARCH_DEPTH = 10
STAFF_SHEET = "кадровая выписка"
ARCHIVE_RE = re.compile(r"архив", re.IGNORECASE)
PAYOUT_SHEET_RE = re.compile(r"^выплаты\s+\((\d+)-(\d+)\)$", re.IGNORECASE)
FINE_SHEET_RE = re.compile(r"^штрафы\s+\((\d+)-(\d+)\)$", re.IGNORECASE)
PAYOUT_LEDGER_RE = re.compile(r"^выплаты\s+\((\d+)-(\d+)\)", re.IGNORECASE)
FINE_LEDGER_RE = re.compile(r"^штрафы\s+\((\d+)-(\d+)\)", re.IGNORECASE)
DATE_RE = re.compile(r"^(\d{1,2})[.](\d{1,2})[.](\d{4})$")
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
SHEETS_SCOPE = ("https://www.googleapis.com/auth/spreadsheets",)
LedgerKind = Literal["fine", "payout"]
FINE_KINDS = ("Уголовный", "Административный")
PAYOUT_KINDS = ("УДО", "Иск ВС", "Иск ФС", "Амнистия")
AUTO_PAYOUT_AMOUNTS = {"Иск ВС": "40000", "Иск ФС": "30000"}
PAYMENT_TERMS = (24, 48, 72, 96, 120, 144, 168)
FINE_HEADER_GROUPS = (
    ("№",),
    ("Дата назначения штрафа",),
    ("Имя и Фамилия",),
    ("Оплачено",),
)
PAYOUT_HEADER_GROUPS = (
    ("№",),
    ("Дата получения выплаты",),
    ("Имя и Фамилия",),
    ("Кто принял выплату",),
    ("Внесено в казну",),
)
FINE_BONUS_HEADER_GROUPS = (
    ("Сумма",),
    ("Внесено в казну", "Внесен"),
    ("Кто выписал штраф", "Сотрудник"),
)
PAYOUT_BONUS_HEADER_GROUPS = (
    ("Сумма",),
    ("Внесено в казну", "Внесен"),
    ("Кто рассматривал", "Рассматривал"),
)
FEE_BONUS_RATE = 0.7
AMNESTY_BONUS_RATE = 0.5
ADMIN_FINE_BONUS_RATE = 0.5
CRIMINAL_FINE_BONUS_RATE = 0.3
PAROLE_BONUS = 5000


class SheetsError(RuntimeError):
    pass


@dataclass(frozen=True)
class StaffMember:
    full_name: str
    position: str
    discord_id: int


@dataclass(frozen=True)
class SheetGrid:
    title: str
    rows: list[list[str]]


@dataclass(frozen=True)
class LedgerEntry:
    number: str
    person: str


@dataclass(frozen=True)
class LedgerSlot:
    kind: LedgerKind
    title: str
    number: str
    sheet_row: int
    row_index: int
    columns: dict[str, int]
    row: list[str]

    @property
    def filled(self) -> bool:
        return not is_vacant_slot(self.kind, self.columns, self.row)


@dataclass(frozen=True)
class LedgerWriteResult:
    kind: LedgerKind
    number: str
    title: str
    summary: str


@dataclass(frozen=True)
class BonusTotal:
    person: str
    amount: int


def normalize_header(value: str) -> str:
    return " ".join(value.split()).casefold()


def cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        return text[:-2]
    return text


def status_key(value: str) -> str:
    text = normalize_header(value)
    if text in {"", "нет"}:
        return "no"
    if text == "да":
        return "yes"
    if text.startswith("ордер"):
        return "order"
    if text.startswith("отмен"):
        return "cancelled"
    return text


def parse_hours(value: str) -> int | None:
    text = cell_text(value).replace(",", ".")
    if not text:
        return None
    try:
        hours = int(float(text))
    except ValueError:
        return None
    if hours <= 0:
        return None
    return hours


def parse_sheet_date(value: str, tz=MOSCOW) -> datetime | None:
    text = cell_text(value)
    if not text:
        return None
    match = DATE_RE.fullmatch(text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        return datetime(year, month, day, tzinfo=tz)
    iso = ISO_DATE_RE.match(text)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        return datetime(year, month, day, tzinfo=tz)
    try:
        serial = float(text.replace(",", "."))
    except ValueError:
        return None
    if serial < 20000 or serial > 80000:
        return None
    parsed = EXCEL_EPOCH + timedelta(days=serial)
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)


def column_map(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, raw in enumerate(header_row):
        key = normalize_header(raw)
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def find_header(
    rows: list[list[str]], *groups: tuple[str, ...]
) -> tuple[int, dict[str, int]] | None:
    """Locate the header row: sheets may start with a merged title line."""
    for index, row in enumerate(rows[:HEADER_SEARCH_DEPTH]):
        columns = column_map(row)
        if all(any(normalize_header(name) in columns for name in group) for group in groups):
            return index, columns
    return None


def row_value(row: list[str], columns: dict[str, int], *names: str) -> str:
    for name in names:
        index = columns.get(normalize_header(name))
        if index is None or index >= len(row):
            continue
        text = cell_text(row[index])
        if text:
            return text
    return ""


def is_archive_sheet(title: str) -> bool:
    return ARCHIVE_RE.search(title) is not None


def is_payout_sheet(title: str) -> bool:
    return PAYOUT_SHEET_RE.fullmatch(title.strip()) is not None and not is_archive_sheet(title)


def is_fine_sheet(title: str) -> bool:
    return FINE_SHEET_RE.fullmatch(title.strip()) is not None and not is_archive_sheet(title)


def is_payout_ledger_sheet(title: str) -> bool:
    return PAYOUT_LEDGER_RE.match(title.strip()) is not None


def is_fine_ledger_sheet(title: str) -> bool:
    return FINE_LEDGER_RE.match(title.strip()) is not None


def is_staff_sheet(title: str) -> bool:
    return normalize_header(title) == STAFF_SHEET


def format_grouped_section(title: str, entries: list[LedgerEntry]) -> str:
    if not entries:
        return f"**{title}:**\nнет"
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        grouped.setdefault(entry.person, []).append(entry.number)

    def sort_key(number: str) -> tuple[int, str]:
        digits = re.sub(r"\D", "", number)
        return (int(digits) if digits else 0, number)

    lines = [f"**{title}:**"]
    for index, person in enumerate(grouped, start=1):
        numbers = ", ".join(sorted(grouped[person], key=sort_key))
        lines.append(f"**{index}. {person}** - {numbers}")
    return "\n".join(lines)


def collect_staff(sheets: list[SheetGrid]) -> list[StaffMember]:
    members: list[StaffMember] = []
    seen: set[int] = set()
    for sheet in sheets:
        if not is_staff_sheet(sheet.title):
            continue
        header = find_header(
            sheet.rows,
            ("Должность",),
            ("Имя Фамилия", "Имя и Фамилия"),
            ("Discord ID", "Discord"),
        )
        if header is None:
            continue
        header_index, columns = header
        for row in sheet.rows[header_index + 1 :]:
            discord_raw = row_value(row, columns, "Discord ID", "Discord")
            discord_id = _parse_discord_id(discord_raw)
            name = row_value(row, columns, "Имя и Фамилия", "Имя Фамилия")
            position = row_value(row, columns, "Должность")
            if discord_id is None or not name or not position or discord_id in seen:
                continue
            seen.add(discord_id)
            members.append(StaffMember(name, position, discord_id))
    return members


def collect_overdue_fines(sheets: list[SheetGrid], now: datetime) -> list[LedgerEntry]:
    local_now = now.astimezone(MOSCOW)
    entries: list[LedgerEntry] = []
    for sheet in sheets:
        if not is_fine_sheet(sheet.title):
            continue
        header = find_header(
            sheet.rows,
            ("№",),
            ("Дата назначения штрафа",),
            ("Срок уплаты", "Срок"),
            ("Оплачено",),
        )
        if header is None:
            continue
        header_index, columns = header
        for row in sheet.rows[header_index + 1 :]:
            assigned = parse_sheet_date(row_value(row, columns, "Дата назначения штрафа"))
            hours = parse_hours(row_value(row, columns, "Срок уплаты", "Срок"))
            if assigned is None or hours is None:
                continue
            if status_key(row_value(row, columns, "Оплачено")) != "no":
                continue
            if local_now < assigned + timedelta(hours=hours):
                continue
            issuer = row_value(row, columns, "Кто выписал штраф")
            number = row_value(row, columns, "№")
            if not issuer or not number:
                continue
            entries.append(LedgerEntry(number, issuer))
    return entries


def collect_unpaid_treasury_fines(sheets: list[SheetGrid]) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    for sheet in sheets:
        if not is_fine_sheet(sheet.title):
            continue
        header = find_header(
            sheet.rows,
            ("№",),
            ("Оплачено",),
            ("Внесено в казну",),
            ("Кто принял штраф",),
        )
        if header is None:
            continue
        header_index, columns = header
        for row in sheet.rows[header_index + 1 :]:
            if status_key(row_value(row, columns, "Оплачено")) != "yes":
                continue
            if status_key(row_value(row, columns, "Внесено в казну")) == "yes":
                continue
            acceptor = row_value(row, columns, "Кто принял штраф")
            number = row_value(row, columns, "№")
            if not acceptor or not number:
                continue
            entries.append(LedgerEntry(number, acceptor))
    return entries


def collect_unpaid_treasury_payouts(sheets: list[SheetGrid]) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    for sheet in sheets:
        if not is_payout_sheet(sheet.title):
            continue
        header = find_header(
            sheet.rows,
            ("№",),
            ("Кто принял выплату",),
            ("Внесено в казну",),
        )
        if header is None:
            continue
        header_index, columns = header
        for row in sheet.rows[header_index + 1 :]:
            acceptor = row_value(row, columns, "Кто принял выплату")
            number = row_value(row, columns, "№")
            if not acceptor or not number:
                continue
            if status_key(row_value(row, columns, "Внесено в казну")) == "yes":
                continue
            if not row_value(row, columns, "Имя и Фамилия", "Дата получения выплаты"):
                continue
            entries.append(LedgerEntry(number, acceptor))
    return entries


def treasury_description(sheets: list[SheetGrid], now: datetime | None = None) -> str:
    now = now or datetime.now(tz=MOSCOW)
    return "\n\n".join(
        (
            "Сводка по штрафам и выплатам",
            format_grouped_section("Просроченные штрафы", collect_overdue_fines(sheets, now)),
            format_grouped_section(
                "Отсутствует выплата на фракцию (штраф)",
                collect_unpaid_treasury_fines(sheets),
            ),
            format_grouped_section(
                "Отсутствует выплата на фракцию (выплаты)",
                collect_unpaid_treasury_payouts(sheets),
            ),
        )
    )


def _parse_discord_id(value: str) -> int | None:
    text = cell_text(value)
    if "|" in text:
        text = text.rsplit("|", 1)[-1].strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) < 15:
        return None
    try:
        parsed = int(digits)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _quote_sheet(title: str) -> str:
    return f"'{title.replace(chr(39), chr(39) * 2)}'"


def column_letter(index: int) -> str:
    if index < 0:
        raise ValueError("Индекс колонки не может быть отрицательным")
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def a1_cell(title: str, column_index: int, sheet_row: int) -> str:
    return f"{_quote_sheet(title)}!{column_letter(column_index)}{sheet_row}"


def today_sheet_date(now: datetime | None = None) -> str:
    current = now or datetime.now(tz=MOSCOW)
    return current.astimezone(MOSCOW).strftime("%d.%m.%Y")


def normalize_number(value: object) -> str:
    text = cell_text(value)
    if not text:
        return ""
    try:
        parsed = float(text.replace(",", "."))
    except ValueError:
        return text
    if parsed.is_integer():
        return str(int(parsed))
    return text


def numbers_equal(left: object, right: object) -> bool:
    return normalize_number(left) == normalize_number(right)


def required_text(value: str, label: str) -> str:
    text = " ".join(cell_text(value).split())
    if not text:
        raise SheetsError(f"Укажите {label}")
    return text


def parse_amount(value: str) -> str:
    text = cell_text(value).replace(" ", "").replace("\u00a0", "").replace("$", "")
    if not text:
        raise SheetsError("Укажите сумму числом, например 10000")
    if not re.fullmatch(r"\d+([.,]\d+)?", text):
        raise SheetsError("Сумма должна быть числом без текста")
    text = text.replace(",", ".")
    if "." in text:
        whole, fraction = text.split(".", 1)
        if set(fraction) <= {"0"}:
            return whole
    return text


def resolve_payout_amount(kind: str, amount: str) -> str:
    if kind in AUTO_PAYOUT_AMOUNTS:
        return AUTO_PAYOUT_AMOUNTS[kind]
    return parse_amount(amount)


def append_comment(existing: str, addition: str) -> str:
    current = cell_text(existing)
    extra = cell_text(addition)
    if not current:
        return extra
    if not extra:
        return current
    return f"{current}; {extra}"


def is_vacant_slot(kind: LedgerKind, columns: dict[str, int], row: list[str]) -> bool:
    if not row_value(row, columns, "№"):
        return False
    if kind == "fine":
        return not row_value(row, columns, "Имя и Фамилия", "Дата назначения штрафа")
    return not row_value(row, columns, "Имя и Фамилия", "Дата получения выплаты")


def _sheet_range(title: str) -> tuple[int, int] | None:
    for pattern in (FINE_SHEET_RE, PAYOUT_SHEET_RE):
        match = pattern.fullmatch(title.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _header_groups(kind: LedgerKind) -> tuple[tuple[str, ...], ...]:
    return FINE_HEADER_GROUPS if kind == "fine" else PAYOUT_HEADER_GROUPS


def iter_ledger_slots(sheets: list[SheetGrid], kind: LedgerKind) -> list[LedgerSlot]:
    matcher = is_fine_sheet if kind == "fine" else is_payout_sheet
    selected = [sheet for sheet in sheets if matcher(sheet.title)]
    selected.sort(key=lambda sheet: _sheet_range(sheet.title) or (0, 0))
    slots: list[LedgerSlot] = []
    for sheet in selected:
        header = find_header(sheet.rows, *_header_groups(kind))
        if header is None:
            continue
        header_index, columns = header
        for row_index, row in enumerate(sheet.rows):
            if row_index <= header_index:
                continue
            number = normalize_number(row_value(row, columns, "№"))
            if not number:
                continue
            slots.append(
                LedgerSlot(
                    kind=kind,
                    title=sheet.title,
                    number=number,
                    sheet_row=row_index + 1,
                    row_index=row_index,
                    columns=columns,
                    row=row,
                )
            )
    return slots


def find_slot_by_number(
    sheets: list[SheetGrid], kind: LedgerKind, number: object
) -> LedgerSlot | None:
    wanted = normalize_number(number)
    if not wanted:
        return None
    for slot in iter_ledger_slots(sheets, kind):
        if numbers_equal(slot.number, wanted):
            return slot
    return None


def find_next_empty_slot(sheets: list[SheetGrid], kind: LedgerKind) -> LedgerSlot | None:
    for slot in iter_ledger_slots(sheets, kind):
        if not slot.filled:
            return slot
    return None


def apply_row_values(row: list[str], columns: dict[str, int], values: dict[str, str]) -> None:
    for name, value in values.items():
        if normalize_header(name) == normalize_header("№"):
            continue
        index = columns.get(normalize_header(name))
        if index is None:
            continue
        while len(row) <= index:
            row.append("")
        row[index] = value


def build_value_ranges(slot: LedgerSlot, values: dict[str, str]) -> list[dict[str, object]]:
    ranges: list[dict[str, object]] = []
    for name, value in values.items():
        if normalize_header(name) == normalize_header("№"):
            continue
        index = slot.columns.get(normalize_header(name))
        if index is None:
            continue
        ranges.append({"range": a1_cell(slot.title, index, slot.sheet_row), "values": [[value]]})
    return ranges


def build_fine_create_values(
    *,
    issuer: str,
    name: str,
    passport: str,
    amount: str,
    term: int | str,
    kind: str,
    claim: str = "",
    articles: str = "",
    comment: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    if kind not in FINE_KINDS:
        raise SheetsError("Вид штрафа должен быть «Уголовный» или «Административный»")
    hours = parse_hours(str(term))
    if hours not in PAYMENT_TERMS:
        raise SheetsError("Срок уплаты должен быть 24, 48, 72, 96, 120, 144 или 168")
    return {
        "Дата назначения штрафа": today_sheet_date(now),
        "Имя и Фамилия": required_text(name, "имя и фамилию"),
        "Паспорт": required_text(passport, "паспорт"),
        "Сумма": parse_amount(amount),
        "Срок уплаты": str(hours),
        "Вид штрафа": kind,
        "Кто выписал штраф": required_text(issuer, "кто выписал штраф"),
        "Кто принял штраф": "",
        "Оплачено": "Нет",
        "Внесено в казну": "Нет",
        "Иск": cell_text(claim),
        "Статьи": cell_text(articles),
        "Комментарий": cell_text(comment),
    }


def build_payout_create_values(
    *,
    acceptor: str,
    reviewer: str,
    name: str,
    passport: str,
    amount: str,
    kind: str,
    claim_articles: str = "",
    comment: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    if kind not in PAYOUT_KINDS:
        raise SheetsError("Неизвестный вид выплаты")
    return {
        "Дата получения выплаты": today_sheet_date(now),
        "Имя и Фамилия": required_text(name, "имя и фамилию"),
        "Паспорт": required_text(passport, "паспорт"),
        "Сумма": resolve_payout_amount(kind, amount),
        "Вид выплаты": kind,
        "Кто принял выплату": required_text(acceptor, "кто принял выплату"),
        "Кто рассматривал": required_text(reviewer, "кто рассматривал"),
        "Внесено в казну": "Нет",
        "Иск / статьи": cell_text(claim_articles),
        "Комментарий": cell_text(comment),
    }


def build_fine_payment_values(slot: LedgerSlot, acceptor: str, now: datetime | None = None) -> dict[str, str]:
    _require_filled_fine(slot)
    paid = status_key(row_value(slot.row, slot.columns, "Оплачено"))
    if paid == "yes":
        raise SheetsError(f"Штраф №{slot.number} уже отмечен как оплаченный")
    if paid == "order":
        raise SheetsError(f"Штраф №{slot.number} закрыт ордером")
    if paid == "cancelled":
        raise SheetsError(f"Штраф №{slot.number} отменён")
    if paid != "no":
        raise SheetsError(f"Штраф №{slot.number} нельзя оплатить в текущем статусе")
    return {
        "Кто принял штраф": required_text(acceptor, "кто принял штраф"),
        "Оплачено": "Да",
        "Внесено в казну": "Да",
        "Дата внесения в казну": today_sheet_date(now),
    }


def build_payout_treasury_values(slot: LedgerSlot, now: datetime | None = None) -> dict[str, str]:
    if slot.kind != "payout":
        raise SheetsError("В казну через эту команду вносятся только выплаты")
    if not slot.filled:
        raise SheetsError(f"Выплата №{slot.number} ещё не заполнена")
    if not row_value(slot.row, slot.columns, "Кто принял выплату"):
        raise SheetsError(f"У выплаты №{slot.number} не указано, кто принял")
    if status_key(row_value(slot.row, slot.columns, "Внесено в казну")) == "yes":
        raise SheetsError(f"Выплата №{slot.number} уже внесена в казну")
    return {
        "Внесено в казну": "Да",
        "Дата внесения в казну": today_sheet_date(now),
    }


def build_fine_status_values(slot: LedgerSlot, status: str, npa: str) -> dict[str, str]:
    _require_filled_fine(slot)
    paid = status_key(row_value(slot.row, slot.columns, "Оплачено"))
    if paid == "yes":
        raise SheetsError(f"Штраф №{slot.number} уже оплачен")
    if paid == "order":
        raise SheetsError(f"Штраф №{slot.number} уже закрыт ордером")
    if paid == "cancelled":
        raise SheetsError(f"Штраф №{slot.number} уже отменён")
    if paid != "no":
        raise SheetsError(f"Штраф №{slot.number} нельзя изменить в текущем статусе")
    if status not in {"Ордер", "Отменено"}:
        raise SheetsError("Статус должен быть «Ордер» или «Отменено»")
    return {
        "Оплачено": status,
        "Комментарий": append_comment(row_value(slot.row, slot.columns, "Комментарий"), required_text(npa, "НПА")),
    }


def _require_filled_fine(slot: LedgerSlot) -> None:
    if slot.kind != "fine":
        raise SheetsError("Это действие доступно только для штрафов")
    if not slot.filled:
        raise SheetsError(f"Штраф №{slot.number} ещё не заполнен")


def resolve_create_slot(
    sheets: list[SheetGrid],
    kind: LedgerKind,
    number: object | None,
) -> LedgerSlot:
    if number is None or cell_text(number) == "":
        slot = find_next_empty_slot(sheets, kind)
        if slot is None:
            label = "штрафов" if kind == "fine" else "выплат"
            raise SheetsError(f"Нет свободных номеров на листах {label}")
        return slot
    slot = find_slot_by_number(sheets, kind, number)
    if slot is None:
        raise SheetsError(f"Номер {normalize_number(number)} не найден на рабочих листах")
    if slot.filled:
        raise SheetsError(f"Номер {slot.number} уже заполнен")
    return slot


def resolve_existing_slot(
    sheets: list[SheetGrid],
    kind: LedgerKind,
    number: object,
) -> LedgerSlot:
    slot = find_slot_by_number(sheets, kind, number)
    if slot is None:
        label = "штраф" if kind == "fine" else "выплата"
        raise SheetsError(f"Не найден {label} №{normalize_number(number)}")
    return slot


def parse_money(value: str) -> int | None:
    try:
        text = parse_amount(value)
    except SheetsError:
        return None
    return int(round(float(text)))


def monday_of(day: datetime) -> datetime:
    local = day.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    return local - timedelta(days=local.weekday())


def week_span(day: datetime) -> tuple[datetime, datetime]:
    start = monday_of(day)
    return start, start + timedelta(days=6)


def resolve_bonus_period(
    start_raw: str | None,
    end_raw: str | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current = now or datetime.now(tz=MOSCOW)
    start_text = cell_text(start_raw or "")
    end_text = cell_text(end_raw or "")
    if not start_text and not end_text:
        return week_span(current)
    start_date = parse_sheet_date(start_text) if start_text else None
    end_date = parse_sheet_date(end_text) if end_text else None
    if start_text and start_date is None:
        raise SheetsError("Дата «с» должна быть в формате ДД.ММ.ГГГГ")
    if end_text and end_date is None:
        raise SheetsError("Дата «по» должна быть в формате ДД.ММ.ГГГГ")
    if start_date is not None and end_date is not None:
        if end_date < start_date:
            raise SheetsError("Дата «по» не может быть раньше даты «с»")
        return start_date, end_date
    return week_span(start_date or end_date or current)


def date_in_period(value: datetime, start: datetime, end: datetime) -> bool:
    day = value.astimezone(MOSCOW).date()
    return start.astimezone(MOSCOW).date() <= day <= end.astimezone(MOSCOW).date()


def payout_bonus_amount(kind: str, amount: int) -> int | None:
    key = normalize_header(kind)
    if key == "удо":
        return PAROLE_BONUS
    if key.startswith("амнист"):
        return int(round(amount * AMNESTY_BONUS_RATE))
    if key in {"иск вс", "иск фс"} or key.startswith("гос"):
        return int(round(amount * FEE_BONUS_RATE))
    return None


def fine_bonus_amount(kind: str, amount: int, *, archive: bool) -> int | None:
    key = normalize_header(kind)
    if key.startswith("уголовн"):
        return int(round(amount * CRIMINAL_FINE_BONUS_RATE))
    if key.startswith("административн"):
        return int(round(amount * ADMIN_FINE_BONUS_RATE))
    if archive and key:
        return int(round(amount * ADMIN_FINE_BONUS_RATE))
    return None


def collect_bonuses(
    sheets: list[SheetGrid],
    start: datetime,
    end: datetime,
) -> list[BonusTotal]:
    totals: dict[str, int] = {}
    for sheet in sheets:
        if is_fine_ledger_sheet(sheet.title):
            _collect_fine_bonuses(sheet, start, end, totals)
        elif is_payout_ledger_sheet(sheet.title):
            _collect_payout_bonuses(sheet, start, end, totals)
    return sorted(
        (BonusTotal(person, amount) for person, amount in totals.items() if amount > 0),
        key=lambda item: (-item.amount, item.person.casefold()),
    )


def format_bonus_description(
    totals: list[BonusTotal],
    start: datetime,
    end: datetime,
) -> str:
    period = f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"
    lines = [f"Премии за период {period}", ""]
    if not totals:
        lines.append("нет")
        return "\n".join(lines)
    for index, item in enumerate(totals, start=1):
        lines.append(f"**{index}. {item.person}** — ${item.amount}")
    lines.append("")
    lines.append(f"**Итого:** ${sum(item.amount for item in totals)}")
    return "\n".join(lines)


def _collect_fine_bonuses(
    sheet: SheetGrid,
    start: datetime,
    end: datetime,
    totals: dict[str, int],
) -> None:
    header = find_header(sheet.rows, *FINE_BONUS_HEADER_GROUPS)
    if header is None:
        return
    header_index, columns = header
    archive = is_archive_sheet(sheet.title)
    for row in sheet.rows[header_index + 1 :]:
        if status_key(row_value(row, columns, "Внесено в казну", "Внесен")) != "yes":
            continue
        posted = parse_sheet_date(row_value(row, columns, "Дата внесения в казну", "Дата оплаты"))
        if posted is None or not date_in_period(posted, start, end):
            continue
        person = row_value(row, columns, "Кто выписал штраф", "Сотрудник")
        amount = parse_money(row_value(row, columns, "Сумма"))
        kind = row_value(row, columns, "Вид штрафа", "НПА")
        if not person or amount is None:
            continue
        share = fine_bonus_amount(kind, amount, archive=archive)
        if not share:
            continue
        totals[person] = totals.get(person, 0) + share


def _collect_payout_bonuses(
    sheet: SheetGrid,
    start: datetime,
    end: datetime,
    totals: dict[str, int],
) -> None:
    header = find_header(sheet.rows, *PAYOUT_BONUS_HEADER_GROUPS)
    if header is None:
        return
    header_index, columns = header
    for row in sheet.rows[header_index + 1 :]:
        if status_key(row_value(row, columns, "Внесено в казну", "Внесен")) != "yes":
            continue
        posted = parse_sheet_date(row_value(row, columns, "Дата внесения в казну", "Дата оплаты"))
        if posted is None or not date_in_period(posted, start, end):
            continue
        person = row_value(row, columns, "Кто рассматривал", "Рассматривал")
        amount = parse_money(row_value(row, columns, "Сумма"))
        kind = row_value(row, columns, "Вид выплаты", "Причина")
        if not person or amount is None:
            continue
        share = payout_bonus_amount(kind, amount)
        if not share:
            continue
        totals[person] = totals.get(person, 0) + share


class CourtSheets:
    def __init__(
        self,
        settings: Settings,
        *,
        grids: list[SheetGrid] | None = None,
    ):
        self.settings = settings
        self._static_grids = grids
        self._cache: list[SheetGrid] | None = None
        self._cache_at: datetime | None = None

    @property
    def configured(self) -> bool:
        if self._static_grids is not None:
            return True
        return bool(self.settings.google_sheets_id and self.settings.google_service_account_json)

    async def load_grids(self) -> list[SheetGrid]:
        if self._static_grids is not None:
            return self._static_grids
        if not self.configured:
            raise SheetsError("Не заданы GOOGLE_SHEETS_ID и GOOGLE_SERVICE_ACCOUNT_JSON")
        now = datetime.now(tz=MOSCOW)
        if self._cache is not None and self._cache_at is not None and now - self._cache_at < CACHE_TTL:
            return self._cache
        grids = await asyncio.to_thread(self._fetch_grids)
        self._cache = grids
        self._cache_at = now
        return grids

    async def find_staff(self, discord_id: int) -> StaffMember | None:
        grids = await self.load_grids()
        for member in collect_staff(grids):
            if member.discord_id == discord_id:
                return member
        return None

    async def treasury_description(self, now: datetime | None = None) -> str:
        grids = await self.load_grids()
        return treasury_description(grids, now)

    async def bonus_description(
        self,
        start_raw: str | None = None,
        end_raw: str | None = None,
        now: datetime | None = None,
    ) -> str:
        grids = await self.load_grids()
        start, end = resolve_bonus_period(start_raw, end_raw, now)
        return format_bonus_description(collect_bonuses(grids, start, end), start, end)

    def invalidate_cache(self) -> None:
        self._cache = None
        self._cache_at = None

    async def create_fine(
        self,
        *,
        issuer: str,
        name: str,
        passport: str,
        amount: str,
        term: int | str,
        kind: str,
        claim: str = "",
        articles: str = "",
        comment: str = "",
        number: object | None = None,
        now: datetime | None = None,
    ) -> LedgerWriteResult:
        values = build_fine_create_values(
            issuer=issuer,
            name=name,
            passport=passport,
            amount=amount,
            term=term,
            kind=kind,
            claim=claim,
            articles=articles,
            comment=comment,
            now=now,
        )
        slot = await self._fresh_create_slot("fine", number)
        await self._write_slot(slot, values)
        return LedgerWriteResult(
            kind="fine",
            number=slot.number,
            title=slot.title,
            summary=(
                f"Штраф **№{slot.number}** записан: {values['Имя и Фамилия']}, "
                f"{values['Сумма']}, {values['Вид штрафа']}, срок {values['Срок уплаты']} ч."
            ),
        )

    async def create_payout(
        self,
        *,
        acceptor: str,
        reviewer: str,
        name: str,
        passport: str,
        amount: str,
        kind: str,
        claim_articles: str = "",
        comment: str = "",
        number: object | None = None,
        now: datetime | None = None,
    ) -> LedgerWriteResult:
        values = build_payout_create_values(
            acceptor=acceptor,
            reviewer=reviewer,
            name=name,
            passport=passport,
            amount=amount,
            kind=kind,
            claim_articles=claim_articles,
            comment=comment,
            now=now,
        )
        slot = await self._fresh_create_slot("payout", number)
        await self._write_slot(slot, values)
        return LedgerWriteResult(
            kind="payout",
            number=slot.number,
            title=slot.title,
            summary=(
                f"Выплата **№{slot.number}** записана: {values['Имя и Фамилия']}, "
                f"{values['Сумма']}, {values['Вид выплаты']}."
            ),
        )

    async def mark_fine_paid(
        self,
        *,
        number: object,
        acceptor: str,
        now: datetime | None = None,
    ) -> LedgerWriteResult:
        slot = await self._fresh_existing_slot("fine", number)
        values = build_fine_payment_values(slot, acceptor, now)
        await self._write_slot(slot, values)
        return LedgerWriteResult(
            kind="fine",
            number=slot.number,
            title=slot.title,
            summary=f"Штраф **№{slot.number}** отмечен как оплаченный и внесён в казну.",
        )

    async def mark_payout_treasury(
        self,
        *,
        number: object,
        now: datetime | None = None,
    ) -> LedgerWriteResult:
        slot = await self._fresh_existing_slot("payout", number)
        values = build_payout_treasury_values(slot, now)
        await self._write_slot(slot, values)
        return LedgerWriteResult(
            kind="payout",
            number=slot.number,
            title=slot.title,
            summary=f"Выплата **№{slot.number}** внесена в казну.",
        )

    async def mark_fine_status(
        self,
        *,
        number: object,
        status: str,
        npa: str,
    ) -> LedgerWriteResult:
        slot = await self._fresh_existing_slot("fine", number)
        values = build_fine_status_values(slot, status, npa)
        await self._write_slot(slot, values)
        action = "выписан ордер" if status == "Ордер" else "отменён"
        return LedgerWriteResult(
            kind="fine",
            number=slot.number,
            title=slot.title,
            summary=f"Штраф **№{slot.number}** {action}. Комментарий: {values['Комментарий']}.",
        )

    async def _fresh_create_slot(self, kind: LedgerKind, number: object | None) -> LedgerSlot:
        self.invalidate_cache()
        first = resolve_create_slot(await self.load_grids(), kind, number)
        self.invalidate_cache()
        grids = await self.load_grids()
        if number is None or cell_text(number) == "":
            current = find_slot_by_number(grids, kind, first.number)
            if current is not None and not current.filled:
                return current
        return resolve_create_slot(grids, kind, number)

    async def _fresh_existing_slot(self, kind: LedgerKind, number: object) -> LedgerSlot:
        self.invalidate_cache()
        grids = await self.load_grids()
        return resolve_existing_slot(grids, kind, number)

    async def _write_slot(self, slot: LedgerSlot, values: dict[str, str]) -> None:
        if self._static_grids is not None:
            apply_row_values(slot.row, slot.columns, values)
            return
        await asyncio.to_thread(self._batch_update, slot, values)
        self.invalidate_cache()

    def _batch_update(self, slot: LedgerSlot, values: dict[str, str]) -> None:
        data = build_value_ranges(slot, values)
        if not data:
            return
        service = _sheets_service(self.settings.google_service_account_json)
        try:
            (
                service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=self.settings.google_sheets_id,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                )
                .execute()
            )
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 403:
                raise SheetsError(
                    "Нет прав редактора Google Таблицы. Выдайте сервис-аккаунту доступ «Редактор»."
                ) from exc
            raise SheetsError("Не удалось записать строку в таблицу") from exc

    def _fetch_grids(self) -> list[SheetGrid]:
        service = _sheets_service(self.settings.google_service_account_json)
        spreadsheet_id = self.settings.google_sheets_id
        try:
            meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        except Exception as exc:
            raise SheetsError("Не удалось открыть Google Таблицу") from exc
        titles = [
            str(sheet["properties"]["title"])
            for sheet in meta.get("sheets", [])
            if sheet.get("properties", {}).get("title")
        ]
        wanted = [
            title
            for title in titles
            if is_staff_sheet(title)
            or is_fine_ledger_sheet(title)
            or is_payout_ledger_sheet(title)
        ]
        if not wanted:
            return []
        ranges = [f"{_quote_sheet(title)}" for title in wanted]
        try:
            payload = (
                service.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=ranges,
                    valueRenderOption="FORMATTED_VALUE",
                    dateTimeRenderOption="FORMATTED_STRING",
                )
                .execute()
            )
        except Exception as exc:
            raise SheetsError("Не удалось прочитать листы Google Таблицы") from exc
        value_ranges = payload.get("valueRanges", [])
        grids: list[SheetGrid] = []
        for title, item in zip(wanted, value_ranges, strict=False):
            raw_rows = item.get("values") or []
            rows = [[cell_text(cell) for cell in row] for row in raw_rows]
            grids.append(SheetGrid(title=title, rows=rows))
        return grids


@lru_cache(maxsize=4)
def _sheets_service(raw_credentials: str):
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SheetsError("Не установлены пакеты google-auth и google-api-python-client") from exc

    info = _load_service_account(raw_credentials)
    credentials = Credentials.from_service_account_info(info, scopes=SHEETS_SCOPE)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _load_service_account(raw: str) -> dict:
    text = raw.strip()
    if not text:
        raise SheetsError("Пустой ключ сервис-аккаунта")
    if text.startswith("{"):
        return json.loads(text)
    path = Path(text)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.is_file():
        raise SheetsError("Файл ключа сервис-аккаунта не найден")
    return json.loads(path.read_text(encoding="utf-8"))
