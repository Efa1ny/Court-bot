import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from courtbot.config import Settings
from courtbot.sheets import (
    CourtSheets,
    SheetGrid,
    SheetsError,
    append_comment,
    build_fine_create_values,
    build_fine_payment_values,
    build_fine_status_values,
    build_payout_create_values,
    build_payout_treasury_values,
    build_value_ranges,
    collect_bonuses,
    collect_overdue_fines,
    collect_staff,
    collect_unpaid_treasury_fines,
    collect_unpaid_treasury_payouts,
    fine_bonus_amount,
    format_bonus_description,
    is_fine_ledger_sheet,
    is_payout_ledger_sheet,
    payout_bonus_amount,
    resolve_bonus_period,
    column_letter,
    find_next_empty_slot,
    find_slot_by_number,
    is_archive_sheet,
    is_fine_sheet,
    is_payout_sheet,
    parse_amount,
    parse_sheet_date,
    resolve_create_slot,
    resolve_payout_amount,
    treasury_description,
)

MOSCOW = ZoneInfo("Europe/Moscow")


FINE_HEADER = [
    "№",
    "Дата назначения штрафа",
    "Имя и Фамилия",
    "Срок уплаты",
    "Кто выписал штраф",
    "Кто принял штраф",
    "Оплачено",
    "Внесено в казну",
]
PAYOUT_HEADER = [
    "№",
    "Дата получения выплаты",
    "Имя и Фамилия",
    "Кто принял выплату",
    "Внесено в казну",
]
STAFF_HEADER = ["Имя и Фамилия", "Должность", "Discord", "Discord ID"]
FINE_WRITE_HEADER = [
    "№",
    "Дата назначения штрафа",
    "Имя и Фамилия",
    "Паспорт",
    "Сумма",
    "Срок уплаты",
    "Вид штрафа",
    "Кто выписал штраф",
    "Кто принял штраф",
    "Оплачено",
    "Внесено в казну",
    "Дата внесения в казну",
    "Иск",
    "Статьи",
    "Комментарий",
]
PAYOUT_WRITE_HEADER = [
    "№",
    "Дата получения выплаты",
    "Имя и Фамилия",
    "Паспорт",
    "Сумма",
    "Вид выплаты",
    "Кто принял выплату",
    "Кто рассматривал",
    "Внесено в казну",
    "Дата внесения в казну",
    "Иск / статьи",
    "Комментарий",
]


def empty_fine(number: str) -> list[str]:
    row = [""] * len(FINE_WRITE_HEADER)
    row[0] = number
    row[9] = "Нет"
    row[10] = "Нет"
    return row


def empty_payout(number: str) -> list[str]:
    row = [""] * len(PAYOUT_WRITE_HEADER)
    row[0] = number
    row[8] = "Нет"
    return row


def writable_fine_sheet(*rows: list[str]) -> SheetGrid:
    return SheetGrid("Штрафы (88-1000)", [FINE_WRITE_HEADER, *rows])


def writable_payout_sheet(*rows: list[str]) -> SheetGrid:
    return SheetGrid("Выплаты (343-1000)", [PAYOUT_WRITE_HEADER, *rows])


def test_settings() -> Settings:
    return Settings(
        token="t",
        test_guild_id=None,
        allowed_role_ids=frozenset(),
        templates_dir=Path("."),
        database_path=Path("."),
        forum_monitor_enabled=False,
        forum_alert_channel_id=1,
        role_supreme_chair_id=2,
        role_supreme_judge_id=3,
        role_federal_chair_id=4,
        role_federal_judge_id=5,
        role_judicial_corps_id=6,
    )


def fine_sheet(*rows: list[str]) -> SheetGrid:
    return SheetGrid("Штрафы (88-1000)", [FINE_HEADER, *rows])


def payout_sheet(*rows: list[str]) -> SheetGrid:
    return SheetGrid("Выплаты (343-1000)", [PAYOUT_HEADER, *rows])


class SheetsLogicTests(unittest.TestCase):
    def test_sheet_name_rules(self):
        self.assertTrue(is_fine_sheet("Штрафы (88-1000)"))
        self.assertTrue(is_payout_sheet("Выплаты (1001-2000)"))
        self.assertFalse(is_fine_sheet("Штрафы (1-87) [АРХИВ]"))
        self.assertFalse(is_payout_sheet("Выплаты (1-342) АРХИВ"))
        self.assertTrue(is_archive_sheet("Штрафы (1-87) [АРХИВ]"))
        self.assertFalse(is_fine_sheet("Памятки"))

    def test_parse_dates(self):
        parsed = parse_sheet_date("20.08.2026")
        self.assertEqual(parsed, datetime(2026, 8, 20, tzinfo=MOSCOW))
        serial = parse_sheet_date("45957")
        self.assertIsNotNone(serial)
        self.assertEqual(serial.year, 2025)

    def test_overdue_fines_ignore_paid_order_and_empty_slots(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=MOSCOW)
        sheets = [
            fine_sheet(
                ["88", "", "", "", "", "", "Нет", "Нет"],
                ["89", "20.08.2026", "A B", "72", "Pasha Moreno", "", "Нет", "Нет"],
                ["90", "20.08.2026", "A B", "72", "Pasha Moreno", "", "Да", "Нет"],
                ["91", "20.08.2026", "A B", "72", "Diana LakeFox", "", "Ордер", "Нет"],
                ["92", "20.08.2026", "A B", "72", "Diana LakeFox", "", "Отменено", "Нет"],
                ["93", "22.08.2026", "A B", "72", "Pasha Moreno", "", "", "Нет"],
            )
        ]
        entries = collect_overdue_fines(sheets, now)
        self.assertEqual([(item.person, item.number) for item in entries], [("Pasha Moreno", "89")])

    def test_treasury_fines_need_paid_and_acceptor(self):
        sheets = [
            fine_sheet(
                ["88", "20.08.2026", "A B", "72", "Pasha Moreno", "", "Да", "Нет"],
                ["89", "20.08.2026", "A B", "72", "Pasha Moreno", "Diana LakeFox", "Да", "Нет"],
                ["90", "20.08.2026", "A B", "72", "Pasha Moreno", "Diana LakeFox", "Да", "Да"],
                ["91", "20.08.2026", "A B", "72", "Pasha Moreno", "Diana LakeFox", "Нет", "Нет"],
            )
        ]
        entries = collect_unpaid_treasury_fines(sheets)
        self.assertEqual([(item.person, item.number) for item in entries], [("Diana LakeFox", "89")])

    def test_treasury_payouts_need_acceptor_and_real_row(self):
        sheets = [
            payout_sheet(
                ["343", "", "", "", "Нет"],
                ["344", "21.08.2026", "A B", "Lukas Vandal", "Нет"],
                ["345", "21.08.2026", "A B", "Lukas Vandal", "Да"],
                ["346", "", "", "Lukas Vandal", "Нет"],
            )
        ]
        entries = collect_unpaid_treasury_payouts(sheets)
        self.assertEqual([(item.person, item.number) for item in entries], [("Lukas Vandal", "344")])

    def test_staff_lookup_by_discord_id(self):
        sheets = [
            SheetGrid(
                "Кадровая выписка",
                [
                    STAFF_HEADER,
                    ["Pasha Moreno", "Председатель Верховного Суда", "efa1ny", "123456789012345678"],
                    ["Black List", "Черный список", "evil | 111", ""],
                ],
            )
        ]
        members = collect_staff(sheets)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].full_name, "Pasha Moreno")
        self.assertEqual(members[0].discord_id, 123456789012345678)

    def test_staff_header_below_title_row(self):
        sheets = [
            SheetGrid(
                "Кадровая выписка",
                [
                    ["Список сотрудников"],
                    STAFF_HEADER,
                    [],
                    [
                        "Pasha Moreno",
                        "Председатель Верховного Суда",
                        "efa1ny",
                        "123456789012345678",
                    ],
                    ["", "Судебный пристав", "", ""],
                ],
            )
        ]
        members = collect_staff(sheets)
        self.assertEqual(
            [(member.full_name, member.position) for member in members],
            [("Pasha Moreno", "Председатель Верховного Суда")],
        )

    def test_treasury_text_and_dynamic_sheet_names(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=MOSCOW)
        sheets = [
            SheetGrid(
                "Штрафы (1001-2000)",
                [
                    FINE_HEADER,
                    ["1005", "01.08.2026", "A B", "24", "Pasha Moreno", "Diana LakeFox", "Нет", "Нет"],
                    ["1006", "01.08.2026", "A B", "24", "Pasha Moreno", "Diana LakeFox", "Да", "Нет"],
                ],
            ),
            SheetGrid(
                "Выплаты (1001-2000)",
                [
                    PAYOUT_HEADER,
                    ["1500", "01.08.2026", "A B", "Lukas Vandal", "Нет"],
                ],
            ),
            SheetGrid(
                "Штрафы (1-87) [АРХИВ]",
                [
                    FINE_HEADER,
                    ["1", "01.08.2026", "A B", "24", "Old Judge", "Old Judge", "Нет", "Нет"],
                ],
            ),
        ]
        text = treasury_description(sheets, now)
        self.assertIn("**1. Pasha Moreno** - 1005", text)
        self.assertIn("**1. Diana LakeFox** - 1006", text)
        self.assertIn("**1. Lukas Vandal** - 1500", text)
        self.assertNotIn("Old Judge", text)

    def test_court_sheets_static_lookup(self):
        import asyncio

        from courtbot.config import Settings

        grids = [
            SheetGrid(
                "Кадровая выписка",
                [
                    STAFF_HEADER,
                    [
                        "Pasha Moreno",
                        "Председатель Верховного Суда",
                        "efa1ny",
                        "123456789012345678",
                    ],
                ],
            )
        ]
        settings = Settings(
            token="t",
            test_guild_id=None,
            allowed_role_ids=frozenset(),
            templates_dir=Path("."),
            database_path=Path("."),
            forum_monitor_enabled=False,
            forum_alert_channel_id=1,
            role_supreme_chair_id=2,
            role_supreme_judge_id=3,
            role_federal_chair_id=4,
            role_federal_judge_id=5,
            role_judicial_corps_id=6,
        )
        client = CourtSheets(settings, grids=grids)
        staff = asyncio.run(client.find_staff(123456789012345678))
        self.assertIsNotNone(staff)
        self.assertEqual(staff.full_name, "Pasha Moreno")


class LedgerWriteTests(unittest.TestCase):
    def test_column_letter_and_amount(self):
        self.assertEqual(column_letter(0), "A")
        self.assertEqual(column_letter(25), "Z")
        self.assertEqual(column_letter(26), "AA")
        self.assertEqual(parse_amount("10 000"), "10000")
        self.assertEqual(parse_amount("40000.00"), "40000")
        self.assertEqual(resolve_payout_amount("Иск ВС", "1"), "40000")
        self.assertEqual(resolve_payout_amount("Иск ФС", ""), "30000")
        self.assertEqual(resolve_payout_amount("УДО", "5000"), "5000")
        self.assertEqual(append_comment("уже есть", "10.1 АК"), "уже есть; 10.1 АК")
        with self.assertRaises(SheetsError):
            parse_amount("десять тысяч")

    def test_next_empty_slot_skips_filled_and_uses_manual_number(self):
        sheets = [
            writable_fine_sheet(
                ["88", "20.08.2026", "A B"] + [""] * 12,
                empty_fine("89"),
                empty_fine("90"),
            )
        ]
        empty = find_next_empty_slot(sheets, "fine")
        self.assertIsNotNone(empty)
        self.assertEqual(empty.number, "89")
        self.assertEqual(empty.sheet_row, 3)
        occupied = find_slot_by_number(sheets, "fine", 88)
        self.assertTrue(occupied.filled)
        slot = resolve_create_slot(sheets, "fine", 90)
        self.assertEqual(slot.number, "90")
        with self.assertRaises(SheetsError):
            resolve_create_slot(sheets, "fine", 88)
        with self.assertRaises(SheetsError):
            resolve_create_slot(sheets, "fine", 999)

    def test_fine_payloads_and_status_transitions(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=MOSCOW)
        created = build_fine_create_values(
            issuer="Pasha Moreno",
            name="Ivan Petrov",
            passport="12345",
            amount="25000",
            term=72,
            kind="Административный",
            claim="12",
            articles="10.1 АК",
            comment="первичный",
            now=now,
        )
        self.assertEqual(created["Дата назначения штрафа"], "23.08.2026")
        self.assertEqual(created["Кто выписал штраф"], "Pasha Moreno")
        self.assertEqual(created["Кто принял штраф"], "")
        self.assertEqual(created["Оплачено"], "Нет")
        row = empty_fine("88")
        sheet = writable_fine_sheet(row)
        slot = find_slot_by_number([sheet], "fine", 88)
        ranges = build_value_ranges(slot, created)
        self.assertTrue(any(item["range"] == "'Штрафы (88-1000)'!B2" for item in ranges))
        self.assertFalse(any("!A2" in str(item["range"]) for item in ranges))

        filled = empty_fine("89")
        filled[1] = "20.08.2026"
        filled[2] = "Ivan Petrov"
        filled[7] = "Pasha Moreno"
        filled[9] = "Нет"
        filled[14] = "первичный"
        paid_slot = find_slot_by_number([writable_fine_sheet(filled)], "fine", 89)
        paid = build_fine_payment_values(paid_slot, "Diana LakeFox", now)
        self.assertEqual(paid["Оплачено"], "Да")
        self.assertEqual(paid["Внесено в казну"], "Да")
        self.assertEqual(paid["Кто принял штраф"], "Diana LakeFox")
        self.assertEqual(paid["Дата внесения в казну"], "23.08.2026")

        order = build_fine_status_values(paid_slot, "Ордер", "10.1 АК")
        self.assertEqual(order["Оплачено"], "Ордер")
        self.assertEqual(order["Комментарий"], "первичный; 10.1 АК")
        self.assertNotIn("Внесено в казну", order)

        paid_slot.row[9] = "Да"
        with self.assertRaises(SheetsError):
            build_fine_payment_values(paid_slot, "Diana LakeFox", now)
        with self.assertRaises(SheetsError):
            build_fine_status_values(paid_slot, "Отменено", "НПА")

    def test_payout_payload_and_treasury(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=MOSCOW)
        created = build_payout_create_values(
            acceptor="Pasha Moreno",
            reviewer="Lukas Vandal",
            name="Ivan Petrov",
            passport="12345",
            amount="ignored",
            kind="Иск ВС",
            claim_articles="иск 12",
            now=now,
        )
        self.assertEqual(created["Сумма"], "40000")
        self.assertEqual(created["Кто принял выплату"], "Pasha Moreno")
        self.assertEqual(created["Внесено в казну"], "Нет")
        row = empty_payout("343")
        row[1] = "21.08.2026"
        row[2] = "Ivan Petrov"
        row[6] = "Pasha Moreno"
        slot = find_slot_by_number([writable_payout_sheet(row)], "payout", 343)
        treasury = build_payout_treasury_values(slot, now)
        self.assertEqual(treasury["Внесено в казну"], "Да")
        self.assertEqual(treasury["Дата внесения в казну"], "23.08.2026")
        slot.row[8] = "Да"
        with self.assertRaises(SheetsError):
            build_payout_treasury_values(slot, now)

    def test_court_sheets_static_write_flow(self):
        import asyncio

        grids = [
            writable_fine_sheet(empty_fine("88"), empty_fine("89")),
            writable_payout_sheet(empty_payout("343")),
        ]
        client = CourtSheets(test_settings(), grids=grids)
        fine = asyncio.run(
            client.create_fine(
                issuer="Pasha Moreno",
                name="Ivan Petrov",
                passport="12345",
                amount="10000",
                term=48,
                kind="Уголовный",
                articles="2.1 УК",
                now=datetime(2026, 8, 23, tzinfo=MOSCOW),
            )
        )
        self.assertEqual(fine.number, "88")
        self.assertIn("№88", fine.summary)
        self.assertEqual(grids[0].rows[1][2], "Ivan Petrov")
        self.assertEqual(grids[0].rows[1][7], "Pasha Moreno")
        self.assertEqual(grids[0].rows[1][8], "")

        paid = asyncio.run(client.mark_fine_paid(number=88, acceptor="Diana LakeFox"))
        self.assertIn("оплаченный", paid.summary)
        self.assertEqual(grids[0].rows[1][9], "Да")
        self.assertEqual(grids[0].rows[1][10], "Да")

        second = asyncio.run(
            client.create_fine(
                issuer="Pasha Moreno",
                name="Other Person",
                passport="1",
                amount="1",
                term=24,
                kind="Административный",
            )
        )
        self.assertEqual(second.number, "89")
        asyncio.run(client.mark_fine_status(number=89, status="Отменено", npa="ст. 24"))
        self.assertEqual(grids[0].rows[2][9], "Отменено")
        self.assertEqual(grids[0].rows[2][14], "ст. 24")

        asyncio.run(
            client.create_payout(
                acceptor="Pasha Moreno",
                reviewer="Lukas Vandal",
                name="Ivan Petrov",
                passport="12345",
                amount="9000",
                kind="УДО",
            )
        )
        self.assertEqual(grids[1].rows[1][4], "9000")
        asyncio.run(client.mark_payout_treasury(number=343))
        self.assertEqual(grids[1].rows[1][8], "Да")

        with self.assertRaises(SheetsError):
            asyncio.run(client.mark_fine_status(number=88, status="Ордер", npa="НПА"))


class BonusTests(unittest.TestCase):
    def test_rates_and_period(self):
        self.assertEqual(payout_bonus_amount("Иск ВС", 40000), 28000)
        self.assertEqual(payout_bonus_amount("Иск ФС", 30000), 21000)
        self.assertEqual(payout_bonus_amount("Гос. Пошлина", 30000), 21000)
        self.assertEqual(payout_bonus_amount("УДО", 1), 5000)
        self.assertEqual(payout_bonus_amount("Амнистия", 20000), 10000)
        self.assertEqual(fine_bonus_amount("Уголовный", 10000, archive=False), 3000)
        self.assertEqual(fine_bonus_amount("Административный", 10000, archive=False), 5000)
        self.assertEqual(fine_bonus_amount("Уголовный кодекс", 10000, archive=True), 3000)
        self.assertEqual(fine_bonus_amount("Административный кодекс", 10000, archive=True), 5000)
        self.assertEqual(fine_bonus_amount("Этический кодекс", 8000, archive=True), 4000)
        self.assertIsNone(fine_bonus_amount("", 10000, archive=False))
        sunday = datetime(2026, 8, 23, 15, 0, tzinfo=MOSCOW)
        start, end = resolve_bonus_period(None, None, sunday)
        self.assertEqual(start.date().isoformat(), "2026-08-17")
        self.assertEqual(end.date().isoformat(), "2026-08-23")
        start, end = resolve_bonus_period("20.08.2026", None, sunday)
        self.assertEqual(start.date().isoformat(), "2026-08-17")
        self.assertEqual(end.date().isoformat(), "2026-08-23")
        start, end = resolve_bonus_period("01.08.2026", "10.08.2026", sunday)
        self.assertEqual(start.date().isoformat(), "2026-08-01")
        self.assertEqual(end.date().isoformat(), "2026-08-10")
        with self.assertRaises(SheetsError):
            resolve_bonus_period("10.08.2026", "01.08.2026", sunday)

    def test_bonus_collection_uses_treasury_date_and_archives(self):
        self.assertTrue(is_fine_ledger_sheet("Штрафы (1-87) [АРХИВ]"))
        self.assertTrue(is_payout_ledger_sheet("Выплаты (1-342) АРХИВ"))
        self.assertFalse(is_fine_sheet("Штрафы (1-87) [АРХИВ]"))
        start = datetime(2026, 8, 17, tzinfo=MOSCOW)
        end = datetime(2026, 8, 23, tzinfo=MOSCOW)
        sheets = [
            SheetGrid(
                "Штрафы (88-1000)",
                [
                    FINE_WRITE_HEADER,
                    [
                        "88",
                        "10.08.2026",
                        "A B",
                        "1",
                        "10000",
                        "72",
                        "Административный",
                        "Pasha Moreno",
                        "Diana LakeFox",
                        "Да",
                        "Да",
                        "20.08.2026",
                        "",
                        "",
                        "",
                    ],
                    [
                        "89",
                        "10.08.2026",
                        "A B",
                        "1",
                        "10000",
                        "72",
                        "Уголовный",
                        "Pasha Moreno",
                        "Diana LakeFox",
                        "Да",
                        "Нет",
                        "",
                        "",
                        "",
                        "",
                    ],
                    [
                        "90",
                        "10.08.2026",
                        "A B",
                        "1",
                        "10000",
                        "72",
                        "Уголовный",
                        "Diana LakeFox",
                        "Pasha Moreno",
                        "Да",
                        "Да",
                        "10.08.2026",
                        "",
                        "",
                        "",
                    ],
                ],
            ),
            SheetGrid(
                "Выплаты (343-1000)",
                [
                    PAYOUT_WRITE_HEADER,
                    [
                        "343",
                        "18.08.2026",
                        "A B",
                        "1",
                        "40000",
                        "Иск ВС",
                        "Pasha Moreno",
                        "Lukas Vandal",
                        "Да",
                        "21.08.2026",
                        "",
                        "",
                    ],
                    [
                        "344",
                        "18.08.2026",
                        "A B",
                        "1",
                        "9000",
                        "УДО",
                        "Pasha Moreno",
                        "Lukas Vandal",
                        "Да",
                        "21.08.2026",
                        "",
                        "",
                    ],
                ],
            ),
            SheetGrid(
                "Штрафы (1-87) [АРХИВ]",
                [
                    [
                        "№",
                        "Сотрудник",
                        "НПА",
                        "Сумма",
                        "Внесен",
                        "Дата оплаты",
                    ],
                    ["1", "Old Judge", "Уголовный кодекс", "10000", "Да", "19.08.2026"],
                    ["2", "Old Judge", "Административный кодекс", "10000", "Да", "01.07.2026"],
                ],
            ),
            SheetGrid(
                "Выплаты (1-342) [АРХИВ]",
                [
                    [
                        "№",
                        "Причина",
                        "Сумма",
                        "Рассматривал",
                        "Внесен",
                        "Дата оплаты",
                    ],
                    ["3", "Гос. Пошлина", "30000", "Old Judge", "Да", "18.08.2026"],
                    ["4", "Гос. Пошлина", "40000", "Old Judge", "Нет", "18.08.2026"],
                ],
            ),
        ]
        totals = {item.person: item.amount for item in collect_bonuses(sheets, start, end)}
        self.assertEqual(totals["Pasha Moreno"], 5000)
        self.assertEqual(totals["Lukas Vandal"], 33000)
        self.assertEqual(totals["Old Judge"], 24000)
        self.assertNotIn("Diana LakeFox", totals)
        text = format_bonus_description(collect_bonuses(sheets, start, end), start, end)
        self.assertIn("17.08.2026 — 23.08.2026", text)
        self.assertIn("Lukas Vandal", text)
        self.assertIn("$62000", text)


if __name__ == "__main__":
    unittest.main()
