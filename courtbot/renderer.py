from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PLACEHOLDER_RE = re.compile(r"{{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*}}")
RUSSIAN_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
VALID_FORM_TYPES = {
    "accept_claim",
    "claim",
    "reject_claim",
    "resolutive_decision",
    "default_decision",
    "schedule_hearing",
    "blank_ruling",
    "asc_order",
    "ia_order",
    "rehab_accept",
    "rehab_grant",
}

CLAIM_PROCEEDINGS = {
    "административное": {
        "claimant_status_genitive": "административного истца",
        "respondent_status_dative": "административному ответчику",
        "proceeding_investigation": (
            "Обязать Корпус судебных приставов штата Сан-Андреас провести "
            "самостоятельное разбирательство по обстоятельствам дела с составлением "
            "протокола его проведения, изложением установленных обстоятельств, их "
            "правовой оценки и итогового заключения, после чего направить материалы "
            "разбирательства в суд."
        ),
    },
    "уголовное": {
        "claimant_status_genitive": "потерпевшего",
        "respondent_status_dative": "обвиняемому",
        "proceeding_investigation": (
            "Обязать Корпус судебных приставов штата Сан-Андреас провести "
            "самостоятельное расследование по обстоятельствам дела с составлением "
            "протокола его проведения, изложением установленных обстоятельств, их "
            "правовой оценки и итогового заключения по результатам расследования, "
            "после чего направить материалы уголовного расследования в Прокуратуру "
            "штата Сан-Андреас для решения вопроса об утверждении обвинительного "
            "заключения и последующего направления материалов дела в суд."
        ),
    },
}


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateInfo:
    key: str
    name: str
    filename: str
    case_suffix: str
    form_type: str


def require_digits(value: str, label: str, exact_length: int | None = None) -> str:
    normalized = value.strip()
    if not normalized.isdigit():
        raise ValidationError(f"Поле «{label}» должно содержать только цифры.")
    if exact_length is not None and len(normalized) != exact_length:
        raise ValidationError(f"Поле «{label}» должно содержать ровно {exact_length} цифр.")
    return normalized


def format_russian_date(value: str) -> str:
    normalized = value.strip()
    parsed: datetime | None = None
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValidationError("Дата должна быть указана в формате ДД.ММ.ГГГГ.")
    return f"{parsed.day} {RUSSIAN_MONTHS[parsed.month - 1]} {parsed.year} года"


def format_dotted_date(value: str, label: str = "Дата") -> str:
    normalized = value.strip()
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValidationError(f"{label} должна быть указана в формате ДД.ММ.ГГГГ.")


def format_russian_hearing_date(value: str) -> str:
    normalized = value.strip()
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return f"«{parsed.day:02d}» {RUSSIAN_MONTHS[parsed.month - 1]} {parsed.year} года"
        except ValueError:
            continue
    raise ValidationError("Дата заседания должна быть указана в формате ДД.ММ.ГГГГ.")


def validate_time(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized):
        raise ValidationError("Время заседания должно быть указано в формате ЧЧ:ММ.")
    return normalized


def format_initial_surname(full_name: str) -> str:
    parts = full_name.split()
    return f"{parts[0][0].upper()}.{parts[-1]}"


def format_signature_markup(full_name: str, signature_url: str) -> str:
    url = signature_url.strip()
    if not url:
        return format_initial_surname(full_name)
    return f'[IMG width="248px" size="688x316"]{url}[/IMG]'


def author_signature_variables(
    full_name: str, position: str, signature_url: str
) -> dict[str, str]:
    return {
        "author_full_name": full_name,
        "author_position": position,
        "author_signature_url": signature_url.strip(),
        "author_signature": format_signature_markup(full_name, signature_url),
    }


def validate_signature_url(signature_url: str) -> str:
    signature_url = signature_url.strip()
    if not signature_url:
        return ""
    parsed = urlparse(signature_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or any(char in signature_url for char in " []{}\t\r\n")
    ):
        raise ValidationError("Ссылка на подпись должна быть полным HTTPS-адресом.")
    return signature_url


def validate_profile(full_name: str, position: str, signature_url: str) -> tuple[str, str, str]:
    full_name = " ".join(full_name.split())
    position = " ".join(position.split())
    signature_url = validate_signature_url(signature_url)

    if len(full_name.split()) < 2:
        raise ValidationError("Укажите имя и фамилию через пробел.")
    if any(char in full_name + position for char in "[]{}"):
        raise ValidationError("Имя и должность не должны содержать BB-теги или фигурные скобки.")
    if not position:
        raise ValidationError("Укажите должность персонажа.")
    return full_name, position, signature_url


def validate_player_name(value: str, label: str = "Наименование истца") -> str:
    normalized = " ".join(value.split())
    if len(normalized.split()) < 2:
        raise ValidationError(f"В поле «{label}» укажите имя и фамилию через пробел.")
    if any(char in normalized for char in "[]{}"):
        raise ValidationError(f"Поле «{label}» не должно содержать BB-теги или фигурные скобки.")
    return normalized


def claim_proceeding_variables(proceeding_type: str) -> dict[str, str]:
    try:
        values = CLAIM_PROCEEDINGS[proceeding_type]
    except KeyError as exc:
        raise ValidationError(
            "Выберите уголовное или административное судопроизводство."
        ) from exc
    return {"proceeding_type": proceeding_type, **values}


class TemplateRepository:
    def __init__(self, directory: Path):
        self.directory = directory
        self._templates: dict[str, TemplateInfo] = {}
        self.reload()

    def _template_path(self, filename: str) -> Path:
        root = self.directory.resolve()
        template_path = (self.directory / filename).resolve()
        if template_path.parent != root or template_path.suffix != ".txt":
            raise RuntimeError(f"Недопустимый путь шаблона: {filename}")
        return template_path

    def reload(self) -> None:
        manifest_path = self.directory / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Не найден файл шаблонов: {manifest_path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ошибка JSON в {manifest_path}: {exc}") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("templates"), list):
            raise RuntimeError("manifest.json должен содержать массив templates")

        loaded: dict[str, TemplateInfo] = {}
        for index, item in enumerate(payload["templates"], start=1):
            if not isinstance(item, dict):
                raise RuntimeError(f"Шаблон №{index} в manifest.json должен быть объектом")
            try:
                info = TemplateInfo(
                    key=item["key"],
                    name=item["name"],
                    filename=item["filename"],
                    case_suffix=item.get("case_suffix", "SJ"),
                    form_type=item.get("form_type", "claim"),
                )
            except KeyError as exc:
                raise RuntimeError(
                    f"В описании шаблона №{index} отсутствует поле {exc.args[0]}"
                ) from exc

            if not all(
                isinstance(value, str)
                for value in (
                    info.key,
                    info.name,
                    info.filename,
                    info.case_suffix,
                    info.form_type,
                )
            ):
                raise RuntimeError(f"Поля шаблона №{index} должны быть строками")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", info.key):
                raise RuntimeError(f"Некорректный ключ шаблона: {info.key}")
            if info.key in loaded:
                raise RuntimeError(f"Повторяющийся ключ шаблона: {info.key}")
            if not info.name.strip():
                raise RuntimeError(f"У шаблона {info.key} отсутствует название")
            if not re.fullmatch(r"[A-Z]{2}", info.case_suffix):
                raise RuntimeError(f"Некорректный суффикс дела у шаблона {info.key}")
            if info.form_type not in VALID_FORM_TYPES:
                raise RuntimeError(f"Неизвестный тип формы у шаблона {info.key}: {info.form_type}")

            template_path = self._template_path(info.filename)
            if not template_path.is_file():
                raise RuntimeError(f"Не найден шаблон: {info.filename}")
            loaded[info.key] = info
        if not loaded:
            raise RuntimeError("В manifest.json нет доступных шаблонов")
        self._templates = loaded

    def list(self) -> list[TemplateInfo]:
        return sorted(self._templates.values(), key=lambda item: item.name)

    def get(self, key: str) -> TemplateInfo:
        try:
            return self._templates[key]
        except KeyError as exc:
            raise ValidationError("Выбранный шаблон не найден.") from exc

    def render(self, key: str, variables: dict[str, str]) -> tuple[TemplateInfo, str]:
        info = self.get(key)
        template_path = self._template_path(info.filename)
        try:
            source = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Не удалось прочитать шаблон: {info.filename}") from exc
        required = set(PLACEHOLDER_RE.findall(source))
        missing = sorted(name for name in required if name not in variables)
        if missing:
            raise RuntimeError("Не переданы переменные шаблона: " + ", ".join(missing))

        rendered = PLACEHOLDER_RE.sub(lambda match: variables[match.group(1)], source)
        unresolved = sorted(set(PLACEHOLDER_RE.findall(rendered)))
        if unresolved:
            raise RuntimeError("Остались незаполненные переменные: " + ", ".join(unresolved))
        return info, rendered


def make_document_variables(
    *,
    registration_number: str,
    document_date: str,
    claim_number: str,
    name_player: str,
    respondent_name: str,
    case_suffix: str,
    author_full_name: str,
    author_position: str,
    author_signature_url: str,
    current_year: int | None = None,
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
) -> dict[str, str]:
    year = current_year or datetime.now().year
    claim_digits = require_digits(claim_number, "Номер искового заявления")
    registration_digits = require_digits(registration_number, "Номер документа")
    variables = {
        "case_reference": f"{claim_digits}-{case_suffix}/{year}",
        "case_suffix": case_suffix,
        "current_year": str(year),
        "registration_number": registration_digits,
        "document_date": format_russian_date(document_date),
        "claim_number": claim_digits,
        "name_player": validate_player_name(name_player),
        "respondent_name": require_text(respondent_name, "Наименование ответчика(ов)"),
        **author_signature_variables(author_full_name, author_position, author_signature_url),
    }
    if refusal_reason is not None:
        variables["refusal_reason"] = require_text(refusal_reason, "Мотивировка отказа").rstrip(".")
    if public_prosecution_name is not None:
        variables["public_prosecution_name"] = require_text(
            public_prosecution_name, "Наименование государственного обвинения"
        )
    if decision_text is not None:
        variables["decision_text"] = require_text(decision_text, "Решение")
    if determination_subject is not None:
        variables["determination_subject"] = require_text(
            determination_subject, "Наименование определения"
        )
    if determination_text is not None:
        variables["determination_text"] = require_text(determination_text, "Текст определения")
    if hearing_type is not None:
        if hearing_type not in {"открытому", "закрытому"}:
            raise ValidationError("Выберите открытое или закрытое судебное заседание.")
        variables["hearing_type"] = hearing_type
    if hearing_date is not None:
        variables["hearing_date"] = format_russian_hearing_date(hearing_date)
    if hearing_time is not None:
        variables["hearing_time"] = validate_time(hearing_time)
    if hearing_address is not None:
        variables["hearing_address"] = require_text(hearing_address, "Место проведения заседания")
    if proceeding_type is not None:
        variables.update(claim_proceeding_variables(proceeding_type))
    return variables


def require_text(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"Заполните поле «{label}».")
    return normalized


def validate_articles(value: str) -> str:
    articles = [item.strip() for item in value.split(",")]
    if not articles or any(not re.fullmatch(r"\d+(?:\.\d+)*", item) for item in articles):
        raise ValidationError("Статьи нарушений укажите через запятую, например: 16.12, 15.5.")
    return ", ".join(articles)


def validate_order_number(value: str, prefix: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]+", normalized):
        raise ValidationError(
            f"Номер ордера {prefix} должен содержать только латинские буквы, цифры или дефис."
        )
    return normalized


def make_asc_order_variables(
    *,
    registration_number: str,
    document_date: str,
    introduction: str,
    decision_date: str,
    punishment_order: str,
    punished_name: str,
    passport_number: str,
    violation_articles: str,
    imprisonment_years: str,
    author_full_name: str,
    author_position: str,
    author_signature_url: str,
) -> dict[str, str]:
    registration_digits = require_digits(registration_number, "Номер ордера ASC")
    passport_digits = require_digits(passport_number, "Номер паспорта")
    years = require_digits(imprisonment_years, "Срок заключения")
    if int(years) <= 0:
        raise ValidationError("Срок заключения должен быть больше нуля.")

    return {
        "case_reference": f"ASC-{registration_digits}",
        "registration_number": registration_digits,
        "document_date": format_russian_date(document_date),
        "introduction": require_text(introduction, "Вводная часть"),
        "decision_date": format_russian_date(decision_date),
        "punishment_order": require_text(punishment_order, "Назначение наказания"),
        "punished_name": validate_player_name(punished_name, "Имя и фамилия наказуемого"),
        "passport_number": passport_digits,
        "violation_articles": validate_articles(violation_articles),
        "imprisonment_years": years,
        **author_signature_variables(author_full_name, author_position, author_signature_url),
    }


def make_ia_order_variables(
    *,
    order_number: str,
    document_date: str,
    claim_number: str,
    investigated_name: str,
    passport_number: str,
    investigated_role: str,
    author_full_name: str,
    author_position: str,
    author_signature_url: str,
) -> dict[str, str]:
    normalized_order_number = validate_order_number(order_number, "IA")
    claim_digits = require_digits(claim_number, "Номер искового заявления")
    passport_digits = require_digits(passport_number, "Номер паспорта")

    return {
        "case_reference": f"IA-{normalized_order_number}",
        "order_number": normalized_order_number,
        "document_date": format_russian_date(document_date),
        "claim_number": claim_digits,
        "investigated_name": validate_player_name(
            investigated_name, "Имя и фамилия подозреваемого"
        ),
        "passport_number": passport_digits,
        "investigated_role": require_text(investigated_role, "Склонённая должность подозреваемого"),
        **author_signature_variables(author_full_name, author_position, author_signature_url),
    }


def validate_fee_amount(value: str) -> str:
    normalized = value.strip().replace(" ", "").replace("$", "").replace(",", "")
    digits = require_digits(normalized, "Размер пошлины")
    if int(digits) <= 0:
        raise ValidationError("Размер пошлины должен быть больше нуля.")
    return digits


def make_rehab_base_variables(
    *,
    registration_number: str,
    document_date: str,
    claim_number: str,
    applicant_name: str,
    case_suffix: str,
    author_full_name: str,
    author_position: str,
    author_signature_url: str,
    current_year: int | None = None,
) -> dict[str, str]:
    year = current_year or datetime.now().year
    claim_digits = require_digits(claim_number, "Номер заявления")
    registration_digits = require_digits(registration_number, "Номер документа")
    return {
        "case_reference": f"LR{claim_digits}-{case_suffix}/{year}",
        "case_suffix": case_suffix,
        "current_year": str(year),
        "registration_number": registration_digits,
        "document_date": format_russian_date(document_date),
        "claim_number": claim_digits,
        "applicant_name": validate_player_name(applicant_name, "ФИО заявителя"),
        **author_signature_variables(author_full_name, author_position, author_signature_url),
    }


def make_rehab_accept_variables(
    *,
    registration_number: str,
    document_date: str,
    claim_number: str,
    applicant_name: str,
    fee_amount: str,
    passport_number: str,
    case_suffix: str,
    author_full_name: str,
    author_position: str,
    author_signature_url: str,
    current_year: int | None = None,
) -> dict[str, str]:
    variables = make_rehab_base_variables(
        registration_number=registration_number,
        document_date=document_date,
        claim_number=claim_number,
        applicant_name=applicant_name,
        case_suffix=case_suffix,
        author_full_name=author_full_name,
        author_position=author_position,
        author_signature_url=author_signature_url,
        current_year=current_year,
    )
    variables["fee_amount"] = validate_fee_amount(fee_amount)
    variables["passport_number"] = require_digits(passport_number, "Номер ID")
    return variables


def make_rehab_grant_variables(
    *,
    registration_number: str,
    document_date: str,
    claim_number: str,
    applicant_name: str,
    violation_articles: str,
    conviction_date: str,
    case_suffix: str,
    author_full_name: str,
    author_position: str,
    author_signature_url: str,
    current_year: int | None = None,
) -> dict[str, str]:
    variables = make_rehab_base_variables(
        registration_number=registration_number,
        document_date=document_date,
        claim_number=claim_number,
        applicant_name=applicant_name,
        case_suffix=case_suffix,
        author_full_name=author_full_name,
        author_position=author_position,
        author_signature_url=author_signature_url,
        current_year=current_year,
    )
    variables["violation_articles"] = validate_articles(violation_articles)
    variables["conviction_date"] = format_dotted_date(conviction_date, "Дата судимости")
    return variables
