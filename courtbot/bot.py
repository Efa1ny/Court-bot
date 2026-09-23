from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from datetime import datetime

import discord
from discord import app_commands
from dotenv import load_dotenv

from .config import BASE_DIR, Settings
from .monitor import ForumMonitor
from .renderer import (
    TemplateRepository,
    ValidationError,
    format_initial_surname,
    make_asc_order_variables,
    make_document_variables,
    make_ia_order_variables,
    make_rehab_accept_variables,
    make_rehab_grant_variables,
    validate_signature_url,
)
from .sheets import CourtSheets, SheetsError, StaffMember
from .storage import Storage

LOGGER = logging.getLogger("courtbot")


def log_exception(message: str, error: BaseException) -> None:
    LOGGER.error(
        message,
        exc_info=(type(error), error, error.__traceback__),
    )


async def restart_process(bot: CourtBot) -> None:
    await asyncio.sleep(1)
    await bot.close()
    LOGGER.info("Перезапуск Court bot")
    try:
        # Re-exec the trusted current interpreter without invoking a shell.
        os.execv(sys.executable, [sys.executable, *sys.argv])  # nosec B606
    except OSError:
        LOGGER.exception("Не удалось перезапустить Court bot")
        os._exit(1)


class CourtBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(settings.database_path)
        self.templates = TemplateRepository(settings.templates_dir)
        self.sheets = CourtSheets(settings)
        self.forum_monitor = ForumMonitor(self, settings, self.storage)

    async def setup_hook(self) -> None:
        self.forum_monitor.start()
        if self.settings.test_guild_id:
            guild = discord.Object(id=self.settings.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info("Синхронизировано команд на тестовом сервере: %s", len(synced))
        else:
            synced = await self.tree.sync()
            LOGGER.info("Синхронизировано глобальных команд: %s", len(synced))

    async def on_ready(self) -> None:
        LOGGER.info(
            "Court bot запущен как %s (ID: %s)",
            self.user,
            self.user.id if self.user else "?",
        )

    def has_access(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        allowed = self.settings.allowed_role_ids
        return bool(allowed and any(role.id in allowed for role in member.roles))

    async def require_access(self, interaction: discord.Interaction) -> bool:
        if self.has_access(interaction):
            return True
        await interaction.response.send_message(
            "У вас нет роли, разрешающей использование Court bot. Обратитесь к администратору.",
            ephemeral=True,
        )
        return False


class SignatureModal(discord.ui.Modal, title="Подпись"):
    def __init__(self, bot: CourtBot, staff: StaffMember, existing_url: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.staff = staff
        self.signature_input = discord.ui.TextInput(
            custom_id="signature_url",
            default=existing_url or None,
            placeholder="Необязательно, HTTPS на PNG/JPG",
            required=False,
            max_length=500,
        )
        self.add_item(
            discord.ui.Label(
                text="Ссылка на изображение подписи",
                description="Пусто — подпись текстом И.Фамилия",
                component=self.signature_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            signature_url = validate_signature_url(self.signature_input.value or "")
        except ValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        self.bot.storage.save_signature(interaction.user.id, signature_url)
        if signature_url:
            signature_note = "изображение по ссылке"
        else:
            signature_note = f"текстом **{format_initial_surname(self.staff.full_name)}**"
        await interaction.response.send_message(
            f"Подпись сохранена для **{self.staff.full_name}**. Документы будут с {signature_note}.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы подписи", error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Не удалось сохранить подпись.", ephemeral=True)


async def require_author(bot: CourtBot, interaction: discord.Interaction) -> StaffMember | None:
    try:
        staff = await bot.sheets.find_staff(interaction.user.id)
    except SheetsError as exc:
        log_exception("Не удалось прочитать кадровую выписку", exc)
        await interaction.response.send_message(
            "Кадровый лист недоступен. Обратитесь к администратору.",
            ephemeral=True,
        )
        return None
    if staff is None:
        message = (
            "Вас нет в кадровой выписке."
            if bot.sheets.configured
            else "Кадровый лист недоступен. Обратитесь к администратору."
        )
        await interaction.response.send_message(message, ephemeral=True)
        return None
    return staff


def author_kwargs(bot: CourtBot, staff: StaffMember) -> dict[str, str]:
    return {
        "author_full_name": staff.full_name,
        "author_position": staff.position,
        "author_signature_url": bot.storage.get_signature_url(staff.discord_id),
    }


async def render_claim_document(
    interaction: discord.Interaction,
    bot: CourtBot,
    template_key: str,
    fields: dict[str, str],
    refusal_reason: str | None = None,
    public_prosecution_name: str | None = None,
    decision_text: str | None = None,
    determination_subject: str | None = None,
    determination_text: str | None = None,
    hearing_type: str | None = None,
    hearing_date: str | None = None,
    hearing_time: str | None = None,
    hearing_address: str | None = None,
    proceeding_type: str | None = None,
) -> None:
    staff = await require_author(bot, interaction)
    if staff is None:
        return

    try:
        template = bot.templates.get(template_key)
        variables = make_document_variables(
            **fields,
            case_suffix=template.case_suffix,
            **author_kwargs(bot, staff),
            refusal_reason=refusal_reason,
            public_prosecution_name=public_prosecution_name,
            decision_text=decision_text,
            determination_subject=determination_subject,
            determination_text=determination_text,
            hearing_type=hearing_type,
            hearing_date=hearing_date,
            hearing_time=hearing_time,
            hearing_address=hearing_address,
            proceeding_type=proceeding_type,
        )
        template, content = bot.templates.render(template_key, variables)
    except (ValidationError, RuntimeError) as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    document_id = bot.storage.save_document(
        user_id=interaction.user.id,
        template_key=template.key,
        template_name=template.name,
        case_number=variables["case_reference"],
        content=content,
    )
    filename = f"court-{variables['case_reference'].replace('/', '-')}.txt"
    file = discord.File(io.BytesIO(content.encode("utf-8")), filename=filename)
    await interaction.response.send_message(
        f"Документ **#{document_id}** сформирован по шаблону «{template.name}». "
        "Подстановка выполнена без незаполненных переменных.",
        file=file,
        ephemeral=True,
    )


class DocumentModal(discord.ui.Modal, title="Реквизиты документа"):
    def __init__(self, bot: CourtBot, template_key: str):
        template = bot.templates.get(template_key)
        if template.form_type == "accept_claim":
            modal_title = "Принятие иска: реквизиты"
        elif template.form_type == "reject_claim":
            modal_title = "Отказ в принятии иска"
        elif template.form_type == "resolutive_decision":
            modal_title = "Резолютивное решение: шаг 1 из 2"
        elif template.form_type == "default_decision":
            modal_title = "Заочное решение: шаг 1 из 2"
        elif template.form_type == "schedule_hearing":
            modal_title = "Назначение заседания: шаг 1"
        elif template.form_type == "blank_ruling":
            modal_title = "Определение: шаг 1 из 2"
        else:
            modal_title = "Реквизиты документа"
        super().__init__(title=modal_title, timeout=600)
        self.bot = bot
        self.template_key = template_key
        self.is_accept_claim = template.form_type == "accept_claim"
        self.is_reject_claim = template.form_type == "reject_claim"
        self.is_resolutive_decision = template.form_type == "resolutive_decision"
        self.is_default_decision = template.form_type == "default_decision"
        self.is_schedule_hearing = template.form_type == "schedule_hearing"
        self.is_blank_ruling = template.form_type == "blank_ruling"
        today = datetime.now().strftime("%d.%m.%Y")

        self.registration_input = discord.ui.TextInput(
            custom_id="registration_number",
            placeholder="Только цифры, например: 12",
            max_length=12,
        )
        self.document_date_input = discord.ui.TextInput(
            custom_id="document_date",
            default=today,
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )
        self.claim_number_input = discord.ui.TextInput(
            custom_id="claim_number",
            placeholder="Только цифры",
            max_length=12,
        )
        self.name_player_input = discord.ui.TextInput(
            custom_id="name_player",
            placeholder="Наименование истца",
            max_length=80,
        )
        self.respondent_name_input = discord.ui.TextInput(
            custom_id="respondent_name",
            placeholder="сотрудник прокуратуры Ivan Ivanov",
            max_length=300,
        )

        self.add_item(discord.ui.Label(text="Номер документа", component=self.registration_input))
        self.add_item(discord.ui.Label(text="Дата документа", component=self.document_date_input))
        self.add_item(
            discord.ui.Label(text="Номер искового заявления", component=self.claim_number_input)
        )
        self.add_item(discord.ui.Label(text="Наименование истца", component=self.name_player_input))
        self.add_item(
            discord.ui.Label(
                text="Наименование ответчика(ов)",
                component=self.respondent_name_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        fields = {
            "registration_number": self.registration_input.value,
            "document_date": self.document_date_input.value,
            "claim_number": self.claim_number_input.value,
            "name_player": self.name_player_input.value,
            "respondent_name": self.respondent_name_input.value,
        }
        if self.is_accept_claim:
            await interaction.response.send_message(
                "Выберите вид судопроизводства.",
                view=ClaimProceedingTypeView(
                    self.bot,
                    self.template_key,
                    fields,
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if self.is_reject_claim:
            await interaction.response.send_message(
                "Реквизиты заполнены. Нажмите «Продолжить», чтобы указать мотивировку отказа.",
                view=RejectClaimContinueView(
                    self.bot,
                    self.template_key,
                    fields,
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if self.is_resolutive_decision:
            await interaction.response.send_message(
                "Реквизиты заполнены. Нажмите «Продолжить», чтобы указать обвинение и решение.",
                view=ResolutiveDecisionContinueView(
                    self.bot,
                    self.template_key,
                    fields,
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if self.is_default_decision:
            await interaction.response.send_message(
                "Реквизиты заполнены. Нажмите «Продолжить», чтобы указать решение.",
                view=DefaultDecisionContinueView(
                    self.bot,
                    self.template_key,
                    fields,
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if self.is_schedule_hearing:
            await interaction.response.send_message(
                "Выберите тип судебного заседания.",
                view=HearingTypeView(
                    self.bot,
                    self.template_key,
                    fields,
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if self.is_blank_ruling:
            await interaction.response.send_message(
                "Реквизиты заполнены. Нажмите «Продолжить», чтобы составить определение.",
                view=BlankRulingContinueView(
                    self.bot,
                    self.template_key,
                    fields,
                    interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        await render_claim_document(interaction, self.bot, self.template_key, fields)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы документа", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать документ.", ephemeral=True
            )


class ClaimProceedingTypeView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.owner_id = owner_id

    async def render_for_proceeding(
        self,
        interaction: discord.Interaction,
        proceeding_type: str,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await render_claim_document(
            interaction,
            self.bot,
            self.template_key,
            self.fields,
            proceeding_type=proceeding_type,
        )

    @discord.ui.button(label="Административное", style=discord.ButtonStyle.primary)
    async def administrative(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.render_for_proceeding(interaction, "административное")

    @discord.ui.button(label="Уголовное", style=discord.ButtonStyle.secondary)
    async def criminal(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.render_for_proceeding(interaction, "уголовное")


class RejectClaimContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            RejectClaimReasonModal(self.bot, self.template_key, self.fields)
        )


class RejectClaimReasonModal(discord.ui.Modal, title="Мотивировка отказа"):
    def __init__(self, bot: CourtBot, template_key: str, fields: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.refusal_reason_input = discord.ui.TextInput(
            custom_id="refusal_reason",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(
            discord.ui.Label(
                text="Мотивировка отказа",
                component=self.refusal_reason_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await render_claim_document(
            interaction,
            self.bot,
            self.template_key,
            self.fields,
            self.refusal_reason_input.value,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы мотивировки отказа", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать документ.", ephemeral=True
            )


class ResolutiveDecisionContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ResolutiveDecisionModal(self.bot, self.template_key, self.fields)
        )


class ResolutiveDecisionModal(
    discord.ui.Modal,
    title="Резолютивное решение: шаг 2 из 2",
):
    def __init__(self, bot: CourtBot, template_key: str, fields: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.public_prosecution_input = discord.ui.TextInput(
            custom_id="public_prosecution_name",
            placeholder="Nia Sebaleti",
            max_length=300,
        )
        self.decision_text_input = discord.ui.TextInput(
            custom_id="decision_text",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(
            discord.ui.Label(
                text="Наименование государственного обвинения",
                component=self.public_prosecution_input,
            )
        )
        self.add_item(discord.ui.Label(text="Решение", component=self.decision_text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await render_claim_document(
            interaction,
            self.bot,
            self.template_key,
            self.fields,
            public_prosecution_name=self.public_prosecution_input.value,
            decision_text=self.decision_text_input.value,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы резолютивного решения", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать решение.", ephemeral=True
            )


class DefaultDecisionContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            DefaultDecisionModal(self.bot, self.template_key, self.fields)
        )


class DefaultDecisionModal(discord.ui.Modal, title="Заочное решение: шаг 2 из 2"):
    def __init__(self, bot: CourtBot, template_key: str, fields: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.decision_text_input = discord.ui.TextInput(
            custom_id="decision_text",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(discord.ui.Label(text="Решение", component=self.decision_text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await render_claim_document(
            interaction,
            self.bot,
            self.template_key,
            self.fields,
            decision_text=self.decision_text_input.value,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы заочного решения", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать решение.", ephemeral=True
            )


class HearingTypeView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.owner_id = owner_id

    async def open_details(
        self,
        interaction: discord.Interaction,
        hearing_type: str,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            HearingDetailsModal(
                self.bot,
                self.template_key,
                self.fields,
                hearing_type,
            )
        )

    @discord.ui.button(label="Открытое", style=discord.ButtonStyle.primary)
    async def open_hearing(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.open_details(interaction, "открытому")

    @discord.ui.button(label="Закрытое", style=discord.ButtonStyle.secondary)
    async def closed_hearing(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.open_details(interaction, "закрытому")


class HearingDetailsModal(discord.ui.Modal, title="Параметры судебного заседания"):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        hearing_type: str,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.hearing_type = hearing_type
        self.hearing_date_input = discord.ui.TextInput(
            custom_id="hearing_date",
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )
        self.hearing_time_input = discord.ui.TextInput(
            custom_id="hearing_time",
            placeholder="ЧЧ:ММ, например: 18:30",
            min_length=5,
            max_length=5,
        )
        self.hearing_address_input = discord.ui.TextInput(
            custom_id="hearing_address",
            style=discord.TextStyle.paragraph,
            default=(
                "штат Сан-Андреас, город Лос-Сантос, улица Бертон, "
                "Капитолий, зал судебных заседаний"
            ),
            max_length=1000,
        )
        self.add_item(discord.ui.Label(text="Дата заседания", component=self.hearing_date_input))
        self.add_item(discord.ui.Label(text="Время заседания", component=self.hearing_time_input))
        self.add_item(
            discord.ui.Label(
                text="Место проведения заседания",
                component=self.hearing_address_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await render_claim_document(
            interaction,
            self.bot,
            self.template_key,
            self.fields,
            hearing_type=self.hearing_type,
            hearing_date=self.hearing_date_input.value,
            hearing_time=self.hearing_time_input.value,
            hearing_address=self.hearing_address_input.value,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы назначения заседания", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать определение.", ephemeral=True
            )


class BlankRulingContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        fields: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            BlankRulingModal(self.bot, self.template_key, self.fields)
        )


class BlankRulingModal(discord.ui.Modal, title="Определение: шаг 2 из 2"):
    def __init__(self, bot: CourtBot, template_key: str, fields: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.fields = fields
        self.determination_subject_input = discord.ui.TextInput(
            custom_id="determination_subject",
            placeholder="О таком-то таком",
            max_length=300,
        )
        self.determination_text_input = discord.ui.TextInput(
            custom_id="determination_text",
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(
            discord.ui.Label(
                text="Наименование определения",
                component=self.determination_subject_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Текст определения",
                component=self.determination_text_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await render_claim_document(
            interaction,
            self.bot,
            self.template_key,
            self.fields,
            determination_subject=self.determination_subject_input.value,
            determination_text=self.determination_text_input.value,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы определения-пустышки", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать определение.", ephemeral=True
            )


class AscOrderModalPartOne(discord.ui.Modal, title="Ордер ASC: шаг 1 из 2"):
    def __init__(self, bot: CourtBot, template_key: str):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        today = datetime.now().strftime("%d.%m.%Y")

        self.registration_input = discord.ui.TextInput(
            custom_id="registration_number",
            placeholder="Только номер, например: 6",
            max_length=12,
        )
        self.document_date_input = discord.ui.TextInput(
            custom_id="document_date",
            default=today,
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )
        self.introduction_input = discord.ui.TextInput(
            custom_id="introduction",
            style=discord.TextStyle.paragraph,
            placeholder="ваша вводная часть",
            max_length=4000,
        )
        self.decision_date_input = discord.ui.TextInput(
            custom_id="decision_date",
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )
        self.punishment_order_input = discord.ui.TextInput(
            custom_id="punishment_order",
            style=discord.TextStyle.paragraph,
            placeholder="назначение наказания",
            max_length=4000,
        )

        self.add_item(discord.ui.Label(text="Номер ордера ASC", component=self.registration_input))
        self.add_item(
            discord.ui.Label(text="Дата публикации ордера", component=self.document_date_input)
        )
        self.add_item(discord.ui.Label(text="Вводная часть", component=self.introduction_input))
        self.add_item(
            discord.ui.Label(text="Дата вынесения решения", component=self.decision_date_input)
        )
        self.add_item(
            discord.ui.Label(text="Назначение наказания", component=self.punishment_order_input)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        first_part = {
            "registration_number": self.registration_input.value,
            "document_date": self.document_date_input.value,
            "introduction": self.introduction_input.value,
            "decision_date": self.decision_date_input.value,
            "punishment_order": self.punishment_order_input.value,
        }
        await interaction.response.send_message(
            "Первый шаг заполнен. Нажмите «Продолжить», чтобы указать данные наказуемого.",
            view=AscOrderContinueView(
                self.bot,
                self.template_key,
                first_part,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка первой формы ордера ASC", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось продолжить создание ордера.", ephemeral=True
            )


class AscOrderContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        first_part: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            AscOrderModalPartTwo(self.bot, self.template_key, self.first_part)
        )


class AscOrderModalPartTwo(discord.ui.Modal, title="Ордер ASC: шаг 2 из 2"):
    def __init__(self, bot: CourtBot, template_key: str, first_part: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part

        self.punished_name_input = discord.ui.TextInput(
            custom_id="punished_name",
            placeholder="Serega Sidorov",
            max_length=80,
        )
        self.passport_number_input = discord.ui.TextInput(
            custom_id="passport_number",
            placeholder="309903",
            max_length=12,
        )
        self.violation_articles_input = discord.ui.TextInput(
            custom_id="violation_articles",
            placeholder="16.12, 15.5",
            max_length=200,
        )
        self.imprisonment_years_input = discord.ui.TextInput(
            custom_id="imprisonment_years",
            placeholder="Только число, например: 3",
            max_length=3,
        )

        self.add_item(
            discord.ui.Label(text="Имя и фамилия наказуемого", component=self.punished_name_input)
        )
        self.add_item(discord.ui.Label(text="Номер паспорта", component=self.passport_number_input))
        self.add_item(
            discord.ui.Label(text="Статьи нарушений", component=self.violation_articles_input)
        )
        self.add_item(
            discord.ui.Label(
                text="Срок заключения в годах", component=self.imprisonment_years_input
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff = await require_author(self.bot, interaction)
        if staff is None:
            return

        try:
            variables = make_asc_order_variables(
                **self.first_part,
                punished_name=self.punished_name_input.value,
                passport_number=self.passport_number_input.value,
                violation_articles=self.violation_articles_input.value,
                imprisonment_years=self.imprisonment_years_input.value,
                **author_kwargs(self.bot, staff),
            )
            template, content = self.bot.templates.render(self.template_key, variables)
        except (ValidationError, RuntimeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        document_id = self.bot.storage.save_document(
            user_id=interaction.user.id,
            template_key=template.key,
            template_name=template.name,
            case_number=variables["case_reference"],
            content=content,
        )
        filename = f"court-{variables['case_reference']}.txt"
        file = discord.File(io.BytesIO(content.encode("utf-8")), filename=filename)
        await interaction.response.send_message(
            f"Документ **#{document_id}** сформирован по шаблону «{template.name}».",
            file=file,
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка второй формы ордера ASC", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать ордер.", ephemeral=True
            )


class IaOrderModalPartOne(discord.ui.Modal, title="Ордер IA: шаг 1 из 2"):
    def __init__(self, bot: CourtBot, template_key: str):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        today = datetime.now().strftime("%d.%m.%Y")

        self.order_number_input = discord.ui.TextInput(
            custom_id="order_number",
            placeholder="Например: 218S",
            max_length=20,
        )
        self.document_date_input = discord.ui.TextInput(
            custom_id="document_date",
            default=today,
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )
        self.claim_number_input = discord.ui.TextInput(
            custom_id="claim_number",
            placeholder="Только цифры, например: 238",
            max_length=12,
        )
        self.add_item(discord.ui.Label(text="Номер ордера IA", component=self.order_number_input))
        self.add_item(
            discord.ui.Label(text="Дата публикации ордера", component=self.document_date_input)
        )
        self.add_item(
            discord.ui.Label(text="Номер искового заявления", component=self.claim_number_input)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        first_part = {
            "order_number": self.order_number_input.value,
            "document_date": self.document_date_input.value,
            "claim_number": self.claim_number_input.value,
        }
        await interaction.response.send_message(
            "Первый шаг заполнен. Нажмите «Продолжить», чтобы указать данные расследования.",
            view=IaOrderContinueView(
                self.bot,
                self.template_key,
                first_part,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка первой формы ордера IA", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось продолжить создание ордера.", ephemeral=True
            )


class IaOrderContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        first_part: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            IaOrderModalPartTwo(self.bot, self.template_key, self.first_part)
        )


class IaOrderModalPartTwo(discord.ui.Modal, title="Ордер IA: шаг 2 из 2"):
    def __init__(self, bot: CourtBot, template_key: str, first_part: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part

        self.investigated_name_input = discord.ui.TextInput(
            custom_id="investigated_name",
            placeholder="Sazha Fox",
            max_length=80,
        )
        self.passport_number_input = discord.ui.TextInput(
            custom_id="passport_number",
            placeholder="60232",
            max_length=12,
        )
        self.investigated_role_input = discord.ui.TextInput(
            custom_id="investigated_role",
            placeholder="склонененная должность подозреваемого",
            max_length=300,
        )

        self.add_item(
            discord.ui.Label(
                text="Имя и фамилия подозреваемого",
                component=self.investigated_name_input,
            )
        )
        self.add_item(discord.ui.Label(text="Номер паспорта", component=self.passport_number_input))
        self.add_item(
            discord.ui.Label(
                text="Склонённая должность подозреваемого",
                component=self.investigated_role_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff = await require_author(self.bot, interaction)
        if staff is None:
            return

        try:
            variables = make_ia_order_variables(
                **self.first_part,
                investigated_name=self.investigated_name_input.value,
                passport_number=self.passport_number_input.value,
                investigated_role=self.investigated_role_input.value,
                **author_kwargs(self.bot, staff),
            )
            template, content = self.bot.templates.render(self.template_key, variables)
        except (ValidationError, RuntimeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        document_id = self.bot.storage.save_document(
            user_id=interaction.user.id,
            template_key=template.key,
            template_name=template.name,
            case_number=variables["case_reference"],
            content=content,
        )
        filename = f"court-{variables['case_reference']}.txt"
        file = discord.File(io.BytesIO(content.encode("utf-8")), filename=filename)
        await interaction.response.send_message(
            f"Документ **#{document_id}** сформирован по шаблону «{template.name}».",
            file=file,
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка второй формы ордера IA", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать ордер.", ephemeral=True
            )


async def send_rendered_document(
    interaction: discord.Interaction,
    bot: CourtBot,
    template_key: str,
    variables: dict[str, str],
) -> None:
    template, content = bot.templates.render(template_key, variables)
    document_id = bot.storage.save_document(
        user_id=interaction.user.id,
        template_key=template.key,
        template_name=template.name,
        case_number=variables["case_reference"],
        content=content,
    )
    filename = f"court-{variables['case_reference'].replace('/', '-')}.txt"
    file = discord.File(io.BytesIO(content.encode("utf-8")), filename=filename)
    await interaction.response.send_message(
        f"Документ **#{document_id}** сформирован по шаблону «{template.name}». "
        "Подстановка выполнена без незаполненных переменных.",
        file=file,
        ephemeral=True,
    )


class RehabModalPartOne(discord.ui.Modal, title="Юр. реабилитация: шаг 1 из 2"):
    def __init__(self, bot: CourtBot, template_key: str):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        today = datetime.now().strftime("%d.%m.%Y")

        self.registration_input = discord.ui.TextInput(
            custom_id="registration_number",
            placeholder="Только цифры, например: 02",
            max_length=12,
        )
        self.document_date_input = discord.ui.TextInput(
            custom_id="document_date",
            default=today,
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )
        self.claim_number_input = discord.ui.TextInput(
            custom_id="claim_number",
            placeholder="Только цифры, например: 505",
            max_length=12,
        )
        self.applicant_name_input = discord.ui.TextInput(
            custom_id="applicant_name",
            placeholder="Shini Kveyt",
            max_length=80,
        )

        self.add_item(discord.ui.Label(text="Номер документа", component=self.registration_input))
        self.add_item(discord.ui.Label(text="Дата документа", component=self.document_date_input))
        self.add_item(
            discord.ui.Label(text="Номер заявления", component=self.claim_number_input)
        )
        self.add_item(discord.ui.Label(text="ФИО заявителя", component=self.applicant_name_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        first_part = {
            "registration_number": self.registration_input.value,
            "document_date": self.document_date_input.value,
            "claim_number": self.claim_number_input.value,
            "applicant_name": self.applicant_name_input.value,
        }
        form_type = self.bot.templates.get(self.template_key).form_type
        if form_type == "rehab_accept":
            hint = "Нажмите «Продолжить», чтобы указать пошлину и ID."
        else:
            hint = "Нажмите «Продолжить», чтобы указать статьи и дату судимости."
        await interaction.response.send_message(
            f"Реквизиты заполнены. {hint}",
            view=RehabContinueView(
                self.bot,
                self.template_key,
                first_part,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка первой формы юр. реабилитации", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось продолжить создание документа.", ephemeral=True
            )


class RehabContinueView(discord.ui.View):
    def __init__(
        self,
        bot: CourtBot,
        template_key: str,
        first_part: dict[str, str],
        owner_id: int,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part
        self.owner_id = owner_id

    @discord.ui.button(label="Продолжить", style=discord.ButtonStyle.primary)
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта форма принадлежит другому пользователю.", ephemeral=True
            )
            return
        form_type = self.bot.templates.get(self.template_key).form_type
        if form_type == "rehab_accept":
            modal: discord.ui.Modal = RehabAcceptModalPartTwo(
                self.bot, self.template_key, self.first_part
            )
        else:
            modal = RehabGrantModalPartTwo(self.bot, self.template_key, self.first_part)
        await interaction.response.send_modal(modal)


class RehabAcceptModalPartTwo(discord.ui.Modal, title="Юр. реабилитация: шаг 2 из 2"):
    def __init__(self, bot: CourtBot, template_key: str, first_part: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part

        self.fee_amount_input = discord.ui.TextInput(
            custom_id="fee_amount",
            placeholder="150000",
            max_length=12,
        )
        self.passport_number_input = discord.ui.TextInput(
            custom_id="passport_number",
            placeholder="77602",
            max_length=12,
        )

        self.add_item(
            discord.ui.Label(text="Размер пошлины в $", component=self.fee_amount_input)
        )
        self.add_item(
            discord.ui.Label(text="ID-идентификатор судьи", component=self.passport_number_input)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff = await require_author(self.bot, interaction)
        if staff is None:
            return

        try:
            template = self.bot.templates.get(self.template_key)
            variables = make_rehab_accept_variables(
                **self.first_part,
                fee_amount=self.fee_amount_input.value,
                passport_number=self.passport_number_input.value,
                case_suffix=template.case_suffix,
                **author_kwargs(self.bot, staff),
            )
            await send_rendered_document(interaction, self.bot, self.template_key, variables)
        except (ValidationError, RuntimeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка второй формы принятия реабилитации", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать документ.", ephemeral=True
            )


class RehabGrantModalPartTwo(discord.ui.Modal, title="Юр. реабилитация: шаг 2 из 2"):
    def __init__(self, bot: CourtBot, template_key: str, first_part: dict[str, str]):
        super().__init__(timeout=900)
        self.bot = bot
        self.template_key = template_key
        self.first_part = first_part

        self.violation_articles_input = discord.ui.TextInput(
            custom_id="violation_articles",
            placeholder="15.2, 12.7.1",
            max_length=200,
        )
        self.conviction_date_input = discord.ui.TextInput(
            custom_id="conviction_date",
            placeholder="ДД.ММ.ГГГГ",
            min_length=10,
            max_length=10,
        )

        self.add_item(
            discord.ui.Label(text="Статьи УК СА", component=self.violation_articles_input)
        )
        self.add_item(
            discord.ui.Label(text="Дата судимости", component=self.conviction_date_input)
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff = await require_author(self.bot, interaction)
        if staff is None:
            return

        try:
            template = self.bot.templates.get(self.template_key)
            variables = make_rehab_grant_variables(
                **self.first_part,
                violation_articles=self.violation_articles_input.value,
                conviction_date=self.conviction_date_input.value,
                case_suffix=template.case_suffix,
                **author_kwargs(self.bot, staff),
            )
            await send_rendered_document(interaction, self.bot, self.template_key, variables)
        except (ValidationError, RuntimeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка второй формы удовлетворения реабилитации", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Не удалось сформировать документ.", ephemeral=True
            )


class FineCreateModal(discord.ui.Modal, title="Новый штраф"):
    def __init__(
        self,
        bot: CourtBot,
        staff: StaffMember,
        kind: str,
        term: int,
        number: int | None,
        comment: str,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.staff = staff
        self.kind = kind
        self.term = term
        self.number = number
        self.comment = comment
        self.name_input = discord.ui.TextInput(
            custom_id="fine_name",
            placeholder="Имя Фамилия",
            max_length=80,
        )
        self.passport_input = discord.ui.TextInput(
            custom_id="fine_passport",
            placeholder="Только цифры",
            max_length=20,
        )
        self.amount_input = discord.ui.TextInput(
            custom_id="fine_amount",
            placeholder="10000",
            max_length=16,
        )
        self.claim_input = discord.ui.TextInput(
            custom_id="fine_claim",
            required=False,
            placeholder="Номер иска, если есть",
            max_length=80,
        )
        self.articles_input = discord.ui.TextInput(
            custom_id="fine_articles",
            required=False,
            placeholder="10.1 АК",
            max_length=200,
        )
        self.add_item(discord.ui.Label(text="Имя и фамилия", component=self.name_input))
        self.add_item(discord.ui.Label(text="Паспорт", component=self.passport_input))
        self.add_item(discord.ui.Label(text="Сумма", component=self.amount_input))
        self.add_item(discord.ui.Label(text="Иск", component=self.claim_input))
        self.add_item(discord.ui.Label(text="Статьи", component=self.articles_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            result = await self.bot.sheets.create_fine(
                issuer=self.staff.full_name,
                name=self.name_input.value,
                passport=self.passport_input.value,
                amount=self.amount_input.value,
                term=self.term,
                kind=self.kind,
                claim=self.claim_input.value or "",
                articles=self.articles_input.value or "",
                comment=self.comment,
                number=self.number,
            )
        except SheetsError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(result.summary, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы штрафа", error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Не удалось записать штраф.", ephemeral=True)


class PayoutCreateModal(discord.ui.Modal, title="Новая выплата"):
    def __init__(
        self,
        bot: CourtBot,
        staff: StaffMember,
        kind: str,
        number: int | None,
        comment: str,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.staff = staff
        self.kind = kind
        self.number = number
        self.comment = comment
        auto_amount = kind in {"Иск ВС", "Иск ФС"}
        self.name_input = discord.ui.TextInput(
            custom_id="payout_name",
            placeholder="Имя Фамилия",
            max_length=80,
        )
        self.passport_input = discord.ui.TextInput(
            custom_id="payout_passport",
            placeholder="Только цифры",
            max_length=20,
        )
        self.amount_input = discord.ui.TextInput(
            custom_id="payout_amount",
            required=not auto_amount,
            placeholder="40000" if kind == "Иск ВС" else "30000" if kind == "Иск ФС" else "Только число",
            max_length=16,
        )
        self.reviewer_input = discord.ui.TextInput(
            custom_id="payout_reviewer",
            placeholder="Имя Фамилия судьи",
            max_length=80,
        )
        self.claim_input = discord.ui.TextInput(
            custom_id="payout_claim",
            required=False,
            placeholder="Иск / статьи",
            max_length=200,
        )
        self.add_item(discord.ui.Label(text="Имя и фамилия", component=self.name_input))
        self.add_item(discord.ui.Label(text="Паспорт", component=self.passport_input))
        self.add_item(
            discord.ui.Label(
                text="Сумма",
                description="Для исков ВС/ФС подставится сама",
                component=self.amount_input,
            )
        )
        self.add_item(discord.ui.Label(text="Кто рассматривал", component=self.reviewer_input))
        self.add_item(discord.ui.Label(text="Иск / статьи", component=self.claim_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            result = await self.bot.sheets.create_payout(
                acceptor=self.staff.full_name,
                reviewer=self.reviewer_input.value,
                name=self.name_input.value,
                passport=self.passport_input.value,
                amount=self.amount_input.value or "",
                kind=self.kind,
                claim_articles=self.claim_input.value or "",
                comment=self.comment,
                number=self.number,
            )
        except SheetsError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(result.summary, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_exception("Ошибка формы выплаты", error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Не удалось записать выплату.", ephemeral=True)


def register_commands(bot: CourtBot) -> None:
    create_group = app_commands.Group(
        name="создать",
        description="Создание подписи и судебных документов",
    )
    restart_group = app_commands.Group(
        name="рестарт",
        description="Перезапуск Court bot и его шаблонов",
    )
    table_group = app_commands.Group(
        name="таблица",
        description="Запись штрафов и выплат в Google Таблицу",
    )

    @create_group.command(
        name="подпись", description="Прикрепить изображение подписи или оставить И.Фамилия"
    )
    @app_commands.guild_only()
    async def signature(interaction: discord.Interaction) -> None:
        if not await bot.require_access(interaction):
            return
        staff = await require_author(bot, interaction)
        if staff is None:
            return
        existing = bot.storage.get_signature_url(interaction.user.id)
        await interaction.response.send_modal(SignatureModal(bot, staff, existing))

    @create_group.command(name="документ", description="Сформировать BB-код судебного документа")
    @app_commands.rename(template="шаблон")
    @app_commands.describe(template="Название BB-шаблона")
    @app_commands.guild_only()
    async def document(interaction: discord.Interaction, template: str) -> None:
        if not await bot.require_access(interaction):
            return
        if await require_author(bot, interaction) is None:
            return
        try:
            bot.templates.get(template)
        except ValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        template_info = bot.templates.get(template)
        if template_info.form_type == "asc_order":
            await interaction.response.send_modal(AscOrderModalPartOne(bot, template))
        elif template_info.form_type == "ia_order":
            await interaction.response.send_modal(IaOrderModalPartOne(bot, template))
        elif template_info.form_type in {"rehab_accept", "rehab_grant"}:
            await interaction.response.send_modal(RehabModalPartOne(bot, template))
        else:
            await interaction.response.send_modal(DocumentModal(bot, template))

    @document.autocomplete("template")
    async def document_template_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not bot.has_access(interaction):
            return []
        needle = current.casefold().strip()
        return [
            app_commands.Choice(name=item.name, value=item.key)
            for item in bot.templates.list()
            if not needle or needle in item.name.casefold()
        ][:25]

    @bot.tree.command(name="история", description="Показать историю или скачать прежний документ")
    @app_commands.rename(document_id="номер_документа")
    @app_commands.describe(document_id="ID документа; оставьте пустым для списка")
    @app_commands.guild_only()
    async def history(interaction: discord.Interaction, document_id: int | None = None) -> None:
        if not await bot.require_access(interaction):
            return
        if document_id is not None:
            item = bot.storage.get_document(interaction.user.id, document_id)
            if item is None:
                await interaction.response.send_message(
                    "Документ с таким ID не найден в вашей истории.", ephemeral=True
                )
                return
            filename = f"court-history-{item.id}.txt"
            file = discord.File(io.BytesIO(item.content.encode("utf-8")), filename=filename)
            await interaction.response.send_message(
                f"Документ **#{item.id}** — {item.template_name}, дело №{item.case_number}.",
                file=file,
                ephemeral=True,
            )
            return

        items = bot.storage.list_documents(interaction.user.id)
        if not items:
            await interaction.response.send_message("История пока пуста.", ephemeral=True)
            return
        lines = ["Последние документы:"]
        for item in items:
            created = item.created_at[:16].replace("T", " ") + " UTC"
            lines.append(
                f"`#{item.id}` — {item.template_name}; дело №{item.case_number}; {created}"
            )
        lines.append("Для скачивания: `/история номер_документа:<ID>`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @restart_group.command(name="шаблонов", description="Перечитать изменённые BB-шаблоны")
    @app_commands.guild_only()
    async def reload_templates(interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        try:
            bot.templates.reload()
        except RuntimeError as exc:
            await interaction.response.send_message(f"Ошибка: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"BB-шаблоны перечитаны. Доступно: {len(bot.templates.list())}. "
            "Изменения полей и форм применяются только после перезапуска бота.",
            ephemeral=True,
        )

    @restart_group.command(name="бота", description="Полностью перезапустить Court bot")
    @app_commands.guild_only()
    async def restart(interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.response.send_message(
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Court bot перезапускается. Подключение восстановится через несколько секунд.",
            ephemeral=True,
        )
        asyncio.create_task(restart_process(bot), name="courtbot-restart")

    @bot.tree.command(
        name="сводка",
        description="Принудительно отправить полночную сводку в канал мониторинга",
    )
    @app_commands.guild_only()
    async def force_summary(interaction: discord.Interaction) -> None:
        if not await bot.require_access(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await bot.forum_monitor.run_forced_summary()
        except Exception as exc:
            log_exception("Не удалось отправить принудительную сводку", exc)
            await interaction.followup.send(
                "Не удалось сформировать сводку. Попробуйте ещё раз.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Полночная сводка отправлена в канал мониторинга.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="статистика",
        description="Статистика рассмотрения исков и заявлений на реабилитацию",
    )
    @app_commands.guild_only()
    async def statistics(interaction: discord.Interaction) -> None:
        if not await bot.require_access(interaction):
            return
        await interaction.response.send_message(
            embeds=bot.forum_monitor.alert_embeds(bot.forum_monitor.stats_text()),
            ephemeral=True,
        )

    @table_group.command(name="штраф", description="Выписать штраф в таблицу")
    @app_commands.rename(kind="вид", term="срок", number="номер", comment="комментарий")
    @app_commands.describe(
        kind="Вид штрафа",
        term="Срок уплаты в часах",
        number="Свободный номер; пусто — следующий свободный",
        comment="Необязательный комментарий",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Уголовный", value="Уголовный"),
            app_commands.Choice(name="Административный", value="Административный"),
        ],
        term=[app_commands.Choice(name=str(hours), value=hours) for hours in (24, 48, 72, 96, 120, 144, 168)],
    )
    @app_commands.guild_only()
    async def table_fine(
        interaction: discord.Interaction,
        kind: str,
        term: int,
        number: int | None = None,
        comment: str | None = None,
    ) -> None:
        if not await bot.require_access(interaction):
            return
        staff = await require_author(bot, interaction)
        if staff is None:
            return
        await interaction.response.send_modal(
            FineCreateModal(bot, staff, kind, term, number, comment or "")
        )

    @table_group.command(name="выплата", description="Записать выплату в таблицу")
    @app_commands.rename(kind="вид", number="номер", comment="комментарий")
    @app_commands.describe(
        kind="Вид выплаты",
        number="Свободный номер; пусто — следующий свободный",
        comment="Необязательный комментарий",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="УДО", value="УДО"),
            app_commands.Choice(name="Иск ВС", value="Иск ВС"),
            app_commands.Choice(name="Иск ФС", value="Иск ФС"),
            app_commands.Choice(name="Амнистия", value="Амнистия"),
        ]
    )
    @app_commands.guild_only()
    async def table_payout(
        interaction: discord.Interaction,
        kind: str,
        number: int | None = None,
        comment: str | None = None,
    ) -> None:
        if not await bot.require_access(interaction):
            return
        staff = await require_author(bot, interaction)
        if staff is None:
            return
        await interaction.response.send_modal(
            PayoutCreateModal(bot, staff, kind, number, comment or "")
        )

    @table_group.command(name="оплата", description="Отметить штраф оплаченным и внесённым в казну")
    @app_commands.rename(number="номер")
    @app_commands.describe(number="Номер штрафа")
    @app_commands.guild_only()
    async def table_pay(interaction: discord.Interaction, number: int) -> None:
        if not await bot.require_access(interaction):
            return
        staff = await require_author(bot, interaction)
        if staff is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.sheets.mark_fine_paid(number=number, acceptor=staff.full_name)
        except SheetsError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(result.summary, ephemeral=True)

    @table_group.command(name="казна", description="Отметить выплату внесённой в казну")
    @app_commands.rename(number="номер")
    @app_commands.describe(number="Номер выплаты")
    @app_commands.guild_only()
    async def table_treasury(interaction: discord.Interaction, number: int) -> None:
        if not await bot.require_access(interaction):
            return
        if await require_author(bot, interaction) is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.sheets.mark_payout_treasury(number=number)
        except SheetsError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(result.summary, ephemeral=True)

    @table_group.command(name="ордер", description="Закрыть штраф ордером и указать НПА")
    @app_commands.rename(number="номер", npa="нпа")
    @app_commands.describe(number="Номер штрафа", npa="НПА в комментарий")
    @app_commands.guild_only()
    async def table_order(interaction: discord.Interaction, number: int, npa: str) -> None:
        if not await bot.require_access(interaction):
            return
        if await require_author(bot, interaction) is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.sheets.mark_fine_status(number=number, status="Ордер", npa=npa)
        except SheetsError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(result.summary, ephemeral=True)

    @table_group.command(name="премии", description="Премии судей за неделю или выбранный период")
    @app_commands.rename(start="с", end="по")
    @app_commands.describe(
        start="Начало периода ДД.ММ.ГГГГ; пусто — текущая неделя",
        end="Конец периода ДД.ММ.ГГГГ; одна дата — вся неделя этой даты",
    )
    @app_commands.guild_only()
    async def table_bonuses(
        interaction: discord.Interaction,
        start: str | None = None,
        end: str | None = None,
    ) -> None:
        if not await bot.require_access(interaction):
            return
        if await require_author(bot, interaction) is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text = await bot.sheets.bonus_description(start, end)
        except SheetsError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            embeds=bot.forum_monitor.alert_embeds(text),
            ephemeral=True,
        )

    @table_group.command(name="отмена", description="Отменить штраф и указать НПА")
    @app_commands.rename(number="номер", npa="нпа")
    @app_commands.describe(number="Номер штрафа", npa="НПА в комментарий")
    @app_commands.guild_only()
    async def table_cancel(interaction: discord.Interaction, number: int, npa: str) -> None:
        if not await bot.require_access(interaction):
            return
        if await require_author(bot, interaction) is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.sheets.mark_fine_status(number=number, status="Отменено", npa=npa)
        except SheetsError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(result.summary, ephemeral=True)

    bot.tree.add_command(create_group)
    bot.tree.add_command(restart_group)
    bot.tree.add_command(table_group)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        log_exception("Необработанная ошибка slash-команды", original)
        message = "Произошла внутренняя ошибка. Попробуйте ещё раз или обратитесь к администратору."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def run() -> None:
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    bot = CourtBot(settings)
    register_commands(bot)
    bot.run(settings.token, log_handler=None)
