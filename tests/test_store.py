import tempfile
import unittest
from pathlib import Path

from app.store import StoreError, UserStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UserStore(Path(self.tmp.name) / "bot.sqlite3")

    def test_defaults(self):
        self.assertEqual(
            self.store.get_preferences(123),
            {"quality": "auto", "upload_mode": "stream", "progress_mode": "standard"},
        )

    def test_saves_valid_preferences(self):
        prefs = self.store.set_preference(123, "quality", "720")
        self.assertEqual(prefs["quality"], "720")
        prefs = self.store.set_preference(123, "upload_mode", "file")
        self.assertEqual(prefs["upload_mode"], "file")
        self.assertEqual(self.store.get_preferences(123)["quality"], "720")

    def test_rejects_invalid_preferences(self):
        with self.assertRaises(StoreError):
            self.store.set_preference(123, "quality", "9999")
        with self.assertRaises(StoreError):
            self.store.set_preference(123, "unknown", "x")

    def test_records_only_completed_downloads(self):
        self.assertEqual(self.store.get_stats(123)["completed"], 0)
        self.store.record_download(123, "youtube", "single", 1, 1024)
        self.store.record_download(123, "hls", "split", 2, 2048)
        stats = self.store.get_stats(123)
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["bytes"], 3072)
        self.assertIsNotNone(stats["last_at"])


if __name__ == "__main__":
    unittest.main()
