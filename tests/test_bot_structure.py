import tempfile
import unittest
from pathlib import Path

from courtbot.bot import (
    AscOrderModalPartOne,
    BlankRulingModal,
    ClaimProceedingTypeView,
    CourtBot,
    DefaultDecisionModal,
    DocumentModal,
    FineCreateModal,
    HearingDetailsModal,
    HearingTypeView,
    IaOrderModalPartOne,
    PayoutCreateModal,
    RehabAcceptModalPartTwo,
    RehabGrantModalPartTwo,
    RehabModalPartOne,
    RejectClaimReasonModal,
    ResolutiveDecisionModal,
    register_commands,
)
from courtbot.sheets import StaffMember
from courtbot.config import Settings


class BotStructureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        settings = Settings(
            token="test",
            test_guild_id=None,
            allowed_role_ids=frozenset(),
            templates_dir=project_root / "templates",
            database_path=Path(self.directory.name) / "test.db",
            forum_monitor_enabled=False,
            forum_alert_channel_id=1,
            role_supreme_chair_id=2,
            role_supreme_judge_id=3,
            role_federal_chair_id=4,
            role_federal_judge_id=5,
            role_judicial_corps_id=6,
        )
        self.bot = CourtBot(settings)
        register_commands(self.bot)

    def tearDown(self):
        self.directory.cleanup()

    def test_russian_commands_are_registered(self):
        commands = {command.name: command for command in self.bot.tree.get_commands()}
        self.assertEqual(
            set(commands),
            {"история", "создать", "рестарт", "статистика", "сводка", "таблица"},
        )
        self.assertEqual(
            {command.name for command in commands["создать"].commands},
            {"подпись", "документ"},
        )
        self.assertEqual(
            {command.name for command in commands["рестарт"].commands},
            {"шаблонов", "бота"},
        )
        self.assertEqual(
            {command.name for command in commands["таблица"].commands},
            {"штраф", "выплата", "оплата", "казна", "ордер", "отмена", "премии"},
        )

    def test_modal_shapes_match_discord_limits(self):
        self.assertEqual(len(DocumentModal(self.bot, "accept_claim_supreme").children), 5)
        self.assertEqual(len(RejectClaimReasonModal(self.bot, "x", {}).children), 1)
        self.assertEqual(len(ResolutiveDecisionModal(self.bot, "x", {}).children), 2)
        self.assertEqual(len(DefaultDecisionModal(self.bot, "x", {}).children), 1)
        self.assertEqual(len(BlankRulingModal(self.bot, "x", {}).children), 2)
        self.assertEqual(len(HearingDetailsModal(self.bot, "x", {}, "открытому").children), 3)
        self.assertEqual(len(AscOrderModalPartOne(self.bot, "asc_overdue_supreme").children), 5)
        self.assertEqual(len(IaOrderModalPartOne(self.bot, "ia_investigation_supreme").children), 3)
        self.assertEqual(len(RehabModalPartOne(self.bot, "rehab_accept_supreme").children), 4)
        self.assertEqual(
            len(RehabAcceptModalPartTwo(self.bot, "rehab_accept_supreme", {}).children), 2
        )
        self.assertEqual(
            len(RehabGrantModalPartTwo(self.bot, "rehab_grant_supreme", {}).children), 2
        )
        staff = StaffMember("Pasha Moreno", "Председатель Верховного Суда", 1)
        self.assertEqual(len(FineCreateModal(self.bot, staff, "Административный", 72, None, "").children), 5)
        self.assertEqual(len(PayoutCreateModal(self.bot, staff, "УДО", None, "").children), 5)

    def test_hearing_type_is_an_explicit_choice(self):
        view = HearingTypeView(self.bot, "schedule_hearing_supreme", {}, 1)
        self.assertEqual([button.label for button in view.children], ["Открытое", "Закрытое"])

    def test_claim_proceeding_type_is_an_explicit_choice(self):
        view = ClaimProceedingTypeView(self.bot, "accept_claim_supreme", {}, 1)
        self.assertEqual([button.label for button in view.children], ["Административное", "Уголовное"])


if __name__ == "__main__":
    unittest.main()
