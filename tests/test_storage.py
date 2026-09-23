import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from courtbot.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "court.db"
        self.storage = Storage(self.database_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_profile_upsert(self):
        self.storage.save_profile(1, "John Smith", "Судья", "https://example.com/a.png")
        self.storage.save_profile(1, "Jane Smith", "Судья", "https://example.com/b.png")

        profile = self.storage.get_profile(1)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.full_name, "Jane Smith")
        self.assertEqual(profile.signature_url, "https://example.com/b.png")

    def test_signature_upsert(self):
        self.storage.save_signature(7, "https://example.com/a.png")
        self.storage.save_signature(7, "https://example.com/b.png")
        self.assertEqual(self.storage.get_signature_url(7), "https://example.com/b.png")
        self.assertEqual(self.storage.get_signature_url(8), "")

    def test_documents_are_isolated_by_user(self):
        first_id = self.storage.save_document(1, "test", "Test", "1-SJ/2026", "first")
        self.storage.save_document(2, "test", "Test", "2-SJ/2026", "second")

        self.assertEqual(len(self.storage.list_documents(1)), 1)
        self.assertEqual(self.storage.get_document(1, first_id).content, "first")
        self.assertIsNone(self.storage.get_document(2, first_id))

    def test_parallel_document_writes(self):
        def save(index: int) -> int:
            return self.storage.save_document(
                1,
                "test",
                "Test",
                f"{index}-SJ/2026",
                str(index),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(save, range(20)))

        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(len(self.storage.list_documents(1, limit=20)), 20)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are not available on Windows")
    def test_database_is_private(self):
        self.assertEqual(self.database_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
