import tempfile
import unittest
from pathlib import Path

from courtbot.renderer import (
    TemplateRepository,
    ValidationError,
    claim_proceeding_variables,
    format_initial_surname,
    format_russian_date,
    format_russian_hearing_date,
    format_signature_markup,
    make_asc_order_variables,
    make_document_variables,
    make_ia_order_variables,
    make_rehab_accept_variables,
    make_rehab_grant_variables,
    validate_profile,
    validate_signature_url,
    validate_time,
)


class RendererTests(unittest.TestCase):
    def test_russian_date(self):
        self.assertEqual(format_russian_date("11.07.2026"), "11 июля 2026 года")

    def test_invalid_date(self):
        with self.assertRaises(ValidationError):
            format_russian_date("31.02.2026")

    def test_hearing_date_and_time(self):
        self.assertEqual(
            format_russian_hearing_date("01.07.2026"),
            "«01» июля 2026 года",
        )
        self.assertEqual(validate_time("18:30"), "18:30")
        with self.assertRaises(ValidationError):
            validate_time("25:70")

    def test_profile_requires_https(self):
        with self.assertRaises(ValidationError):
            validate_signature_url("http://example.com/sign.png")
        with self.assertRaises(ValidationError):
            validate_profile("Pasha Moreno", "Судья", "http://example.com/sign.png")

    def test_profile_allows_empty_signature(self):
        full_name, position, signature_url = validate_profile("Pasha Moreno", "Судья", "")
        self.assertEqual(full_name, "Pasha Moreno")
        self.assertEqual(position, "Судья")
        self.assertEqual(signature_url, "")
        self.assertEqual(format_initial_surname(full_name), "P.Moreno")
        self.assertEqual(format_signature_markup(full_name, ""), "P.Moreno")
        self.assertEqual(
            format_signature_markup(full_name, "https://example.com/sign.png"),
            '[IMG width="248px" size="688x316"]https://example.com/sign.png[/IMG]',
        )

    def test_document_variables(self):
        values = make_document_variables(
            registration_number="12",
            document_date="11.07.2026",
            claim_number="456",
            name_player="John Smith",
            respondent_name="сотрудник прокуратуры Ivan Ivanov",
            case_suffix="SJ",
            author_full_name="Pasha Moreno",
            author_position="Верховный Судья штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
            current_year=2026,
        )
        self.assertEqual(values["case_reference"], "456-SJ/2026")
        self.assertEqual(values["case_suffix"], "SJ")
        self.assertEqual(values["registration_number"], "12")
        self.assertEqual(values["name_player"], "John Smith")
        self.assertEqual(values["respondent_name"], "сотрудник прокуратуры Ivan Ivanov")
        self.assertEqual(
            values["author_signature"],
            '[IMG width="248px" size="688x316"]https://example.com/sign.png[/IMG]',
        )

    def test_claim_proceeding_variables_use_correct_statuses_and_cases(self):
        administrative = claim_proceeding_variables("административное")
        self.assertEqual(administrative["claimant_status_genitive"], "административного истца")
        self.assertEqual(administrative["respondent_status_dative"], "административному ответчику")
        self.assertIn("самостоятельное разбирательство", administrative["proceeding_investigation"])

        criminal = claim_proceeding_variables("уголовное")
        self.assertEqual(criminal["claimant_status_genitive"], "потерпевшего")
        self.assertEqual(criminal["respondent_status_dative"], "обвиняемому")
        self.assertIn("материалы уголовного расследования", criminal["proceeding_investigation"])

        with self.assertRaises(ValidationError):
            claim_proceeding_variables("гражданское")

    def test_repository_replaces_repeated_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                '{"templates":[{"key":"test","name":"Test","filename":"test.txt"}]}',
                encoding="utf-8",
            )
            (root / "test.txt").write_text(
                "№{{claim_number}} / №{{claim_number}}", encoding="utf-8"
            )
            repository = TemplateRepository(root)
            _, result = repository.render("test", {"claim_number": "777"})
            self.assertEqual(result, "№777 / №777")

    def test_asc_order_variables(self):
        values = make_asc_order_variables(
            registration_number="6",
            document_date="11.07.2026",
            introduction="Ваша вводная часть",
            decision_date="28.06.2026",
            punishment_order="Назначить штраф и дать 96 часов на оплату.",
            punished_name="Serega Sidorov",
            passport_number="309903",
            violation_articles="16.12,15.5",
            imprisonment_years="3",
            author_full_name="Pasha Moreno",
            author_position="Верховный судья штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
        )
        self.assertEqual(values["case_reference"], "ASC-6")
        self.assertEqual(values["decision_date"], "28 июня 2026 года")
        self.assertEqual(values["violation_articles"], "16.12, 15.5")
        self.assertEqual(values["imprisonment_years"], "3")

    def test_asc_templates_have_no_unresolved_variables(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        values = make_asc_order_variables(
            registration_number="6",
            document_date="11.07.2026",
            introduction="Федеральный суд рассматривает исковое заявление №724.",
            decision_date="28.06.2026",
            punishment_order="Признать виновным и назначить штраф.",
            punished_name="Serega Sidorov",
            passport_number="309903",
            violation_articles="16.12, 15.5",
            imprisonment_years="3",
            author_full_name="Pasha Moreno",
            author_position="Верховный судья штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
        )
        for key in ("asc_overdue_supreme", "asc_overdue_federal"):
            with self.subTest(template=key):
                _, result = repository.render(key, values)
                self.assertNotIn("{{", result)
                self.assertIn("ОРДЕР №ASC-6", result)
                self.assertEqual(result.count(values["introduction"]), 2)
                self.assertIn("общим сроком на 3 года", result)

    def test_ia_order_template_has_no_unresolved_variables(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        values = make_ia_order_variables(
            order_number="218s",
            document_date="11.07.2026",
            claim_number="238",
            investigated_name="Sazha Fox",
            passport_number="60232",
            investigated_role="сотрудника прокуратуры штата Сан-Андреас",
            author_full_name="Pasha Moreno",
            author_position="Верховный судья штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
        )
        _, result = repository.render("ia_investigation_supreme", values)
        self.assertNotIn("{{", result)
        self.assertIn("ОРДЕР №IA-218S", result)
        self.assertIn("исковое заявление №238 против лица", result)
        self.assertIn("Sazha Fox (№ID-идентификатора: 60232)", result)
        self.assertNotIn("в составе Верховного судьи", result)
        self.assertIn("https://example.com/sign.png", result)

    def test_real_template_has_no_unresolved_variables(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        values = make_document_variables(
            registration_number="12",
            document_date="11.07.2026",
            claim_number="456",
            name_player="John Smith",
            respondent_name="сотрудник прокуратуры Ivan Ivanov",
            case_suffix="SJ",
            author_full_name="Pasha Moreno",
            author_position="Верховный Судья штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
            current_year=2026,
            proceeding_type="административное",
        )
        _, result = repository.render("accept_claim_supreme", values)
        self.assertNotIn("{{", result)
        self.assertIn("Дело №456-SJ/2026", result)
        self.assertIn("№12-456", result)
        self.assertEqual(
            result.count(
                "№456 от административного истца John Smith к "
                "административному ответчику сотрудник прокуратуры Ivan Ivanov"
            ),
            2,
        )
        self.assertIn("направить административному ответчику копию", result)
        self.assertIn("самостоятельное разбирательство", result)

    def test_accept_claim_statuses_are_declined_in_both_courts(self):
        repository = TemplateRepository(Path(__file__).resolve().parents[1] / "templates")
        for key, suffix in (("accept_claim_supreme", "SJ"), ("accept_claim_federal", "FJ")):
            for proceeding, claimant, respondent in (
                ("административное", "административного истца", "административному ответчику"),
                ("уголовное", "потерпевшего", "обвиняемому"),
            ):
                with self.subTest(template=key, proceeding=proceeding):
                    values = make_document_variables(
                        registration_number="12",
                        document_date="11.07.2026",
                        claim_number="456",
                        name_player="John Smith",
                        respondent_name="Ivan Ivanov",
                        case_suffix=suffix,
                        author_full_name="Pasha Moreno",
                        author_position="Судья",
                        author_signature_url="",
                        current_year=2026,
                        proceeding_type=proceeding,
                    )
                    _, result = repository.render(key, values)
                    self.assertEqual(
                        result.count(f"от {claimant} John Smith к {respondent} Ivan Ivanov"),
                        2,
                    )
                    self.assertIn(f"направить {respondent} копию", result)
                    self.assertNotIn("{{", result)

    def test_empty_signature_renders_initial_surname(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        values = make_document_variables(
            registration_number="12",
            document_date="11.07.2026",
            claim_number="456",
            name_player="John Smith",
            respondent_name="сотрудник прокуратуры Ivan Ivanov",
            case_suffix="SJ",
            author_full_name="Pasha Moreno",
            author_position="Верховный Судья штата Сан-Андреас",
            author_signature_url="",
            current_year=2026,
            proceeding_type="уголовное",
        )
        self.assertEqual(values["author_signature"], "P.Moreno")
        _, result = repository.render("accept_claim_supreme", values)
        self.assertIn("P.Moreno", result)
        self.assertNotIn("[IMG width=\"248px\"", result)
        self.assertNotIn("{{author_signature}}", result)

    def test_templates_use_size_header_and_postanovil(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        for path in sorted(templates_dir.glob("*.txt")):
            with self.subTest(template=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertTrue(source.startswith("[SIZE=5]"), path.name)
                self.assertIn("[/SIZE]", source)
                self.assertIn('width="1000px"', source)
                self.assertNotIn('width="915px"', source)
                self.assertIn("{{author_signature}}", source)
                self.assertNotIn("{{author_signature_url}}", source)
                self.assertNotIn("Р Е Ш И Л:", source)
                if path.name.startswith(("resolutive_decision_", "default_decision_")):
                    self.assertIn("П О С Т А Н О В И Л:", source)

    def test_reject_claim_templates_have_reason_and_no_unresolved_variables(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        for key, suffix, court_name in (
            ("reject_claim_supreme", "SJ", "Верховного Суда"),
            ("reject_claim_federal", "FJ", "Федерального суда"),
        ):
            with self.subTest(template=key):
                values = make_document_variables(
                    registration_number="1",
                    document_date="09.07.2026",
                    claim_number="237",
                    name_player="Rin Vendetta",
                    respondent_name="сотрудник прокуратуры Ivan Ivanov",
                    case_suffix=suffix,
                    author_full_name="Pasha Moreno",
                    author_position="Верховный судья штата Сан-Андреас",
                    author_signature_url="https://example.com/sign.png",
                    current_year=2026,
                    refusal_reason=(
                        "в исковом заявлении отсутствуют доказательства "
                        "подписания договора ее сторонами."
                    ),
                )
                _, result = repository.render(key, values)
                self.assertNotIn("{{", result)
                self.assertIn(f"Дело №237-{suffix}/2026", result)
                self.assertIn("ОБ ОТКАЗЕ В ПРИНЯТИИ", result)
                self.assertIn("отсутствуют доказательства подписания договора", result)
                self.assertIn("ее сторонами. В связи с чем суд", result)
                self.assertNotIn("ее сторонами..", result)
                self.assertEqual(
                    result.count("от Rin Vendetta к сотрудник прокуратуры Ivan Ivanov"),
                    2,
                )
                self.assertIn(f"к производству {court_name}", result)

    def test_resolutive_decision_templates_have_no_unresolved_variables(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        for key, suffix, court_name in (
            ("resolutive_decision_supreme", "SJ", "Верховный суд"),
            ("resolutive_decision_federal", "FJ", "Федеральный суд"),
        ):
            with self.subTest(template=key):
                values = make_document_variables(
                    registration_number="8",
                    document_date="26.06.2026",
                    claim_number="230",
                    name_player="Wizzy Advokatov",
                    respondent_name="сотрудник полиции Oleg Kachan",
                    case_suffix=suffix,
                    author_full_name="Pasha Moreno",
                    author_position="Верховный судья штата Сан-Андреас",
                    author_signature_url="https://example.com/sign.png",
                    current_year=2026,
                    public_prosecution_name="Nia Sebaleti",
                    decision_text=(
                        "1. В удовлетворении исковых требований отказать.\n"
                        "2. Признать действия сотрудников правомерными."
                    ),
                )
                _, result = repository.render(key, values)
                self.assertNotIn("{{", result)
                self.assertIn(f"Дело №230-{suffix}/2026", result)
                self.assertIn(f"{court_name} штата Сан-Андреас", result)
                self.assertIn("с участием стороны государственного обвинения Nia Sebaleti", result)
                self.assertIn("№230 от Wizzy Advokatov к сотрудник полиции Oleg Kachan", result)
                self.assertIn("1. В удовлетворении исковых требований отказать.", result)
                self.assertIn("3. Решение в полном объеме", result)
                if suffix == "FJ":
                    self.assertIn(
                        "4. Настоящее решение вступает в силу по истечении срока "
                        "апелляционного обжалования.",
                        result,
                    )
                else:
                    self.assertIn("4. Решение вступает в силу", result)

    def test_default_decision_templates_have_no_unresolved_variables(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        for key, suffix, court_name in (
            ("default_decision_supreme", "SJ", "Верховный Суд"),
            ("default_decision_federal", "FJ", "Федеральный суд"),
        ):
            with self.subTest(template=key):
                values = make_document_variables(
                    registration_number="6",
                    document_date="03.07.2026",
                    claim_number="234",
                    name_player="Wispiee Tank",
                    respondent_name="директор FIB Faks Psewdo",
                    case_suffix=suffix,
                    author_full_name="Pasha Moreno",
                    author_position="Верховный судья штата Сан-Андреас",
                    author_signature_url="https://example.com/sign.png",
                    current_year=2026,
                    decision_text=(
                        "1. Исковые требования удовлетворить в полном объеме.\n"
                        "2. Признать действия ответчика неправомерными."
                    ),
                )
                _, result = repository.render(key, values)
                self.assertNotIn("{{", result)
                self.assertIn(f"Дело №234-{suffix}/2026", result)
                self.assertIn(f"{court_name} штата Сан-Андреас", result)
                self.assertIn("рассмотрев в заочном производстве", result)
                self.assertIn("1. Исковые требования удовлетворить", result)
                self.assertIn("7. Решение в полном объеме", result)
                if suffix == "FJ":
                    self.assertIn(
                        "8. Настоящее решение вступает в силу по истечении срока "
                        "апелляционного обжалования.",
                        result,
                    )
                else:
                    self.assertIn("8. Настоящее заочное решение вступает в силу", result)

    def test_schedule_hearing_templates_and_types(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        for key, suffix, hearing_type in (
            ("schedule_hearing_supreme", "SJ", "открытому"),
            ("schedule_hearing_federal", "FJ", "закрытому"),
        ):
            with self.subTest(template=key):
                values = make_document_variables(
                    registration_number="5",
                    document_date="01.07.2026",
                    claim_number="728",
                    name_player="Terry Projectski",
                    respondent_name="сотрудник LSPD Antisocial Residenzov",
                    case_suffix=suffix,
                    author_full_name="Pasha Moreno",
                    author_position="Верховный судья штата Сан-Андреас",
                    author_signature_url="https://example.com/sign.png",
                    current_year=2026,
                    hearing_type=hearing_type,
                    hearing_date="01.07.2026",
                    hearing_time="18:30",
                    hearing_address="Капитолий, зал судебных заседаний",
                )
                _, result = repository.render(key, values)
                self.assertNotIn("{{", result)
                self.assertIn(f"Дело №728-{suffix}/2026", result)
                self.assertIn(f"к {hearing_type} судебному заседанию", result)
                self.assertIn("«01» июля 2026 года в 18:30", result)
                self.assertIn("Капитолий, зал судебных заседаний", result)

    def test_blank_ruling_templates_are_customizable(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        for key, suffix in (
            ("blank_ruling_supreme", "SJ"),
            ("blank_ruling_federal", "FJ"),
        ):
            with self.subTest(template=key):
                values = make_document_variables(
                    registration_number="5",
                    document_date="01.07.2026",
                    claim_number="728",
                    name_player="Terry Projectski",
                    respondent_name="сотрудник LSPD Antisocial Residenzov",
                    case_suffix=suffix,
                    author_full_name="Pasha Moreno",
                    author_position="Верховный судья штата Сан-Андреас",
                    author_signature_url="https://example.com/sign.png",
                    current_year=2026,
                    determination_subject="О приобщении материалов дела",
                    determination_text="Приобщить представленные материалы к делу.",
                )
                _, result = repository.render(key, values)
                self.assertNotIn("{{", result)
                self.assertIn("О приобщении материалов дела", result)
                self.assertIn("1. Приобщить представленные материалы к делу.", result)
                self.assertIn("2. Настоящее судебное определение", result)

    def test_rehab_accept_template(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        values = make_rehab_accept_variables(
            registration_number="02",
            document_date="07.08.2026",
            claim_number="505",
            applicant_name="Shini Kveyt",
            fee_amount="$150000",
            passport_number="77602",
            case_suffix="SJ",
            author_full_name="Pasha Moreno",
            author_position="Председатель Верховного Суда штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
            current_year=2026,
        )
        self.assertEqual(values["case_reference"], "LR505-SJ/2026")
        self.assertEqual(values["fee_amount"], "150000")
        _, result = repository.render("rehab_accept_supreme", values)
        self.assertNotIn("{{", result)
        self.assertIn("Дело №LR505-SJ/2026", result)
        self.assertIn("№02-LR505", result)
        self.assertIn("$150000", result)
        self.assertIn("№ ID-идентификатора: 77602", result)
        self.assertIn(
            "Заявление подано в соответствии к установленным требованиям и нормам",
            result,
        )
        self.assertIn("О П Р Е Д Е Л И Л:", result)

    def test_rehab_grant_template(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)
        values = make_rehab_grant_variables(
            registration_number="03",
            document_date="07.08.2026",
            claim_number="505",
            applicant_name="Shini Kveyt",
            violation_articles="15.2,12.7.1",
            conviction_date="07.08.2026",
            case_suffix="SJ",
            author_full_name="Pasha Moreno",
            author_position="Председатель Верховного Суда штата Сан-Андреас",
            author_signature_url="https://example.com/sign.png",
            current_year=2026,
        )
        self.assertEqual(values["violation_articles"], "15.2, 12.7.1")
        self.assertEqual(values["conviction_date"], "07.08.2026")
        _, result = repository.render("rehab_grant_supreme", values)
        self.assertNotIn("{{", result)
        self.assertIn("Дело №LR505-SJ/2026", result)
        self.assertIn("№03-LR505", result)
        self.assertIn("по статьям 15.2, 12.7.1 УК СА от 07.08.2026", result)
        self.assertIn(
            "Заявитель оплатил установленную государственную пошлину в полном объеме",
            result,
        )
        self.assertIn("П О С Т А Н О В И Л:", result)

    def test_every_manifest_template_renders(self):
        templates_dir = Path(__file__).resolve().parents[1] / "templates"
        repository = TemplateRepository(templates_dir)

        for template in repository.list():
            with self.subTest(template=template.key):
                if template.form_type == "asc_order":
                    values = make_asc_order_variables(
                        registration_number="6",
                        document_date="11.07.2026",
                        introduction="Суд рассмотрел материалы искового заявления.",
                        decision_date="28.06.2026",
                        punishment_order="Назначить штраф.",
                        punished_name="Serega Sidorov",
                        passport_number="309903",
                        violation_articles="16.12, 15.5",
                        imprisonment_years="3",
                        author_full_name="Pasha Moreno",
                        author_position="Верховный судья штата Сан-Андреас",
                        author_signature_url="https://example.com/sign.png",
                    )
                elif template.form_type == "ia_order":
                    values = make_ia_order_variables(
                        order_number="218S",
                        document_date="11.07.2026",
                        claim_number="238",
                        investigated_name="Sazha Fox",
                        passport_number="60232",
                        investigated_role="сотрудника прокуратуры",
                        author_full_name="Pasha Moreno",
                        author_position="Верховный судья штата Сан-Андреас",
                        author_signature_url="https://example.com/sign.png",
                    )
                elif template.form_type == "rehab_accept":
                    values = make_rehab_accept_variables(
                        registration_number="02",
                        document_date="07.08.2026",
                        claim_number="505",
                        applicant_name="Shini Kveyt",
                        fee_amount="150000",
                        passport_number="77602",
                        case_suffix=template.case_suffix,
                        author_full_name="Pasha Moreno",
                        author_position="Председатель Верховного Суда штата Сан-Андреас",
                        author_signature_url="https://example.com/sign.png",
                        current_year=2026,
                    )
                elif template.form_type == "rehab_grant":
                    values = make_rehab_grant_variables(
                        registration_number="03",
                        document_date="07.08.2026",
                        claim_number="505",
                        applicant_name="Shini Kveyt",
                        violation_articles="15.2, 12.7.1",
                        conviction_date="07.08.2026",
                        case_suffix=template.case_suffix,
                        author_full_name="Pasha Moreno",
                        author_position="Председатель Верховного Суда штата Сан-Андреас",
                        author_signature_url="https://example.com/sign.png",
                        current_year=2026,
                    )
                else:
                    values = make_document_variables(
                        registration_number="5",
                        document_date="11.07.2026",
                        claim_number="728",
                        name_player="Terry Projectski",
                        respondent_name="сотрудник LSPD Antisocial Residenzov",
                        case_suffix=template.case_suffix,
                        author_full_name="Pasha Moreno",
                        author_position="Верховный судья штата Сан-Андреас",
                        author_signature_url="https://example.com/sign.png",
                        current_year=2026,
                        refusal_reason="отсутствуют необходимые доказательства",
                        public_prosecution_name="Nia Sebaleti",
                        decision_text="1. Исковые требования удовлетворить.",
                        determination_subject="О приобщении материалов дела",
                        determination_text="Приобщить материалы к делу.",
                        hearing_type="открытому",
                        hearing_date="12.07.2026",
                        hearing_time="18:30",
                        hearing_address="Капитолий, зал судебных заседаний",
                        proceeding_type="административное",
                    )

                _, result = repository.render(template.key, values)
                self.assertNotIn("{{", result)


if __name__ == "__main__":
    unittest.main()
