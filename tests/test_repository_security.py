import json
import tempfile
import unittest
from pathlib import Path

from courtbot.renderer import TemplateRepository


class RepositorySecurityTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            templates.mkdir()
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            (templates / "manifest.json").write_text(
                json.dumps(
                    {
                        "templates": [
                            {
                                "key": "escape",
                                "name": "Escape",
                                "filename": "../secret.txt",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Недопустимый путь"):
                TemplateRepository(templates)

    def test_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.txt").write_text("test", encoding="utf-8")
            item = {"key": "test", "name": "Test", "filename": "test.txt"}
            (root / "manifest.json").write_text(
                json.dumps({"templates": [item, item]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Повторяющийся ключ"):
                TemplateRepository(root)

    def test_unknown_form_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test.txt").write_text("test", encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "templates": [
                            {
                                "key": "test",
                                "name": "Test",
                                "filename": "test.txt",
                                "form_type": "unknown",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Неизвестный тип формы"):
                TemplateRepository(root)


if __name__ == "__main__":
    unittest.main()
