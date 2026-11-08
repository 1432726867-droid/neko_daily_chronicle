"""时间胶囊：锁定语义、到期读取与输入校验。"""

from __future__ import annotations

import pathlib
import shutil
import unittest
import uuid

from chronicle import capsule, models
from chronicle import config as config_mod
from chronicle import store as store_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]

TMP_ROOT = ROOT / ".tmp-tests"
NOW = 1_760_000_000.0
TODAY = models.local_day_key(NOW)
FUTURE = models.local_day_key(NOW + 3 * 86400)
PAST = models.local_day_key(NOW - 86400)


def make_temp_dir() -> pathlib.Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True)
    return target


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.store = store_mod.ChronicleStore(self.root, config_mod.ChronicleConfig())
        self.store.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, text="写给未来的话", day=FUTURE, author="master"):
        return capsule.write(self.store, text=text, unlock_day=day, author=author, now=NOW)

    def test_locked_capsule_hides_text_everywhere(self):
        record, error = self._write()
        self.assertEqual(error, "")
        self.assertTrue(record["locked"])
        self.assertNotIn("text", record)

        items = capsule.list_all(self.store, today=TODAY, include_text=True)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["locked"])
        self.assertNotIn("text", items[0])
        self.assertEqual(items[0]["chars"], len("写给未来的话"))

    def test_read_returns_locked_then_unlocks(self):
        record, _ = self._write(text="三天后再读")
        item, error = capsule.read(self.store, record["id"], today=TODAY)
        self.assertEqual(error, "locked")
        self.assertNotIn("text", item)
        self.assertEqual(item["unlock_day"], FUTURE)

        item, error = capsule.read(self.store, record["id"], today=FUTURE)
        self.assertEqual(error, "")
        self.assertEqual(item["text"], "三天后再读")

    def test_list_includes_unlocked_text_and_author(self):
        record, _ = self._write(text="猫娘封存", author="catgirl")
        items = capsule.list_all(self.store, today=FUTURE, include_text=True)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["locked"])
        self.assertEqual(items[0]["text"], "猫娘封存")
        self.assertEqual(items[0]["author"], "catgirl")
        self.assertEqual(items[0]["author_name"], self.store.config.catgirl_name)

    def test_include_locked_can_be_hidden(self):
        self._write()
        self.assertEqual(
            capsule.list_all(self.store, today=TODAY, include_locked=False), []
        )

    def test_rejects_past_date_empty_text_and_too_long(self):
        _, error = self._write(day=PAST)
        self.assertEqual(error, "unlock_in_past")
        _, error = self._write(text="   ")
        self.assertEqual(error, "empty_text")
        _, error = self._write(text="x" * (capsule.CAPSULE_MAX_CHARS + 1))
        self.assertEqual(error, "too_long")

    def test_normalize_day_accepts_separators(self):
        self.assertEqual(capsule.normalize_day("2027/07/12"), "2027-07-12")
        self.assertEqual(capsule.normalize_day("2027.07.12"), "2027-07-12")
        self.assertEqual(capsule.normalize_day("不是日期"), "")

    def test_author_counts_separate_master_and_catgirl(self):
        self._write(text="主人写的")
        self._write(text="猫娘写的", author="catgirl")
        items = capsule.list_all(self.store, today=TODAY)
        counts = capsule.author_counts(items)
        self.assertEqual(counts, {"master": 1, "catgirl": 1})

    def test_corrupt_file_is_ignored(self):
        path = self.root / "capsules" / "broken-file-1234.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(capsule.list_all(self.store, today=TODAY), [])


if __name__ == "__main__":
    unittest.main()
