"""收藏清单（回忆录底层状态）的测试。"""

from __future__ import annotations

import pathlib
import shutil
import unittest
import uuid

from chronicle import config as config_mod
from chronicle import store as store_mod
from chronicle.memory import MemoryStore

ROOT = pathlib.Path(__file__).resolve().parents[1]

TMP_ROOT = ROOT / ".tmp-tests"


def new_ms() -> tuple[pathlib.Path, MemoryStore]:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    root = TMP_ROOT / f"mem-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    s = store_mod.ChronicleStore(root, config_mod.ChronicleConfig())
    s.ensure_dirs()
    return root, MemoryStore(s)


def row(day: str, index: int, text: str = "内容") -> dict:
    return {
        "date": day,
        "index": index,
        "time": "14:00",
        "speaker": "猫娘",
        "role": "assistant",
        "text": text,
    }


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.root, self.ms = new_ms()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_toggle_collects_and_uncollects(self):
        self.assertTrue(self.ms.toggle(row("2026-10-06", 3))["saved"])
        self.assertTrue(self.ms.has("2026-10-06", 3))
        self.assertFalse(self.ms.toggle(row("2026-10-06", 3))["saved"])
        self.assertFalse(self.ms.has("2026-10-06", 3))
        self.assertEqual(self.ms.count(), 0)

    def test_forced_on_does_not_duplicate(self):
        self.assertTrue(self.ms.toggle(row("2026-10-06", 1), on=True)["saved"])
        result = self.ms.toggle(row("2026-10-06", 1), on=True)
        # saved 表示操作后的收藏状态：已收藏时强制收藏应返回 True，且不重复追加
        self.assertTrue(result["saved"])
        self.assertEqual(result["total"], 1)
        self.assertFalse(self.ms.toggle(row("2026-10-06", 1), on=False)["saved"])
        self.assertEqual(self.ms.count(), 0)

    def test_list_is_newest_first(self):
        self.ms.toggle(row("2026-10-05", 1))
        self.ms.toggle(row("2026-10-06", 2))
        items = self.ms.list()
        self.assertEqual([item["index"] for item in items], [2, 1])

    def test_persists_to_disk(self):
        self.ms.toggle(row("2026-10-06", 7, "记得这一天"))
        self.assertTrue(self.ms.path.is_file())
        reloaded = MemoryStore(self.ms.store)
        self.assertTrue(reloaded.has("2026-10-06", 7))

    def test_bad_row_raises(self):
        with self.assertRaises(ValueError):
            self.ms.toggle({"date": "", "index": 0})

    def test_toggle_off_for_missing_is_safe(self):
        result = self.ms.toggle(row("2026-10-06", 9), on=False)
        self.assertEqual(result, {"saved": False, "total": 0})

    def test_corrupt_file_falls_back_to_empty(self):
        self.ms.path.write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(self.ms.list(), [])
        self.assertEqual(self.ms.count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)