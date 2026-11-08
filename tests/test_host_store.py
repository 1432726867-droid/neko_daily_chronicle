"""宿主对话存储、跨来源合并、跨来源持久去重的测试。

这些都对应真机上的一次故障：总线只给"用户说过的话"，角色的回复只在
``memory/<角色>/time_indexed.db`` 里；而且 SDK 记录对象要穿四层包装才见得到内容。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import time
import unittest
import uuid

from chronicle import config as config_mod
from chronicle import harvest, host_store
from chronicle import store as store_mod
from chronicle.models import ROLE_ASSISTANT, ROLE_UNKNOWN, ROLE_USER, Turn

ROOT = pathlib.Path(__file__).resolve().parents[1]

TMP_ROOT = ROOT / ".tmp-tests"


def make_temp_dir() -> pathlib.Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    target.mkdir()
    return target


def build_memory_root(root: pathlib.Path, characters: dict[str, list[dict]]) -> pathlib.Path:
    """按宿主的结构造一个 memory/<角色>/ 目录（带 recent.json + time_indexed.db）。"""

    memory = root / "memory"
    for name, rows in characters.items():
        store = memory / name
        store.mkdir(parents=True, exist_ok=True)
        (store / "recent.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        connection = sqlite3.connect(store / "time_indexed.db")
        connection.execute(
            "CREATE TABLE time_indexed_original (id TEXT, session_id TEXT, message TEXT, timestamp TEXT)"
        )
        for index, row in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO time_indexed_original VALUES (?, ?, ?, ?)",
                (str(index), "session", json.dumps(row, ensure_ascii=False), "2026-10-06 17:06:18.320311"),
            )
        connection.commit()
        connection.close()
    return memory


def human(text: str) -> dict:
    return {"type": "human", "data": {"content": [{"type": "text", "text": text}]}}


def ai(text: str) -> dict:
    return {"type": "ai", "data": {"content": [{"type": "text", "text": text}]}}


class ParseTests(unittest.TestCase):
    def test_strip_host_stamp(self):
        self.assertEqual(host_store.strip_host_stamp("[20261006 Tue 17:06]喵呜~"), "喵呜~")
        self.assertEqual(host_store.strip_host_stamp("没有前缀"), "没有前缀")

    def test_parse_message_blob_shapes(self):
        self.assertEqual(host_store.parse_message_blob(human("你好")), (ROLE_USER, "你好"))
        self.assertEqual(host_store.parse_message_blob(ai("喵")), (ROLE_ASSISTANT, "喵"))
        self.assertEqual(
            host_store.parse_message_blob(json.dumps(ai("[20261006 Tue 17:06]喵呜~"))),
            (ROLE_ASSISTANT, "喵呜~"),
        )
        # bytes 也要能解析（SQLite 里可能取到 bytes）
        self.assertEqual(
            host_store.parse_message_blob(json.dumps(human("字节")).encode("utf-8")),
            (ROLE_USER, "字节"),
        )
        self.assertIsNone(host_store.parse_message_blob({"type": "human", "data": {}}))
        self.assertIsNone(host_store.parse_message_blob("不是 JSON"))


class HostStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        build_memory_root(self.root, {"YUI": [human("你看你，这么激动"), ai("[20261006 Tue 17:06]喵呜~")]})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_find_and_select_store(self):
        stores = host_store.find_character_stores(self.root)
        self.assertEqual([store.name for store in stores], ["YUI"])
        self.assertEqual(host_store.select_store(self.root).name, "YUI")
        self.assertIsNone(host_store.select_store(self.root, "不存在"))

    def test_read_time_indexed_both_sides(self):
        store = self.root / "memory" / "YUI"
        turns = host_store.read_time_indexed(store, limit=10)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].role, ROLE_USER)
        self.assertEqual(turns[0].text, "你看你，这么激动")
        self.assertEqual(turns[1].role, ROLE_ASSISTANT)
        self.assertEqual(turns[1].text, "喵呜~")
        self.assertTrue(turns[0].source.startswith("host.store:"))

    def test_read_recent_json_fallback(self):
        store = self.root / "memory" / "YUI"
        (store / "time_indexed.db").unlink()
        turns, source = host_store.read_store(store, limit=10, now=1_760_000_000.0)
        self.assertEqual(source, "recent.json")
        self.assertEqual([turn.role for turn in turns], [ROLE_USER, ROLE_ASSISTANT])

    def test_select_store_prefers_newest(self):
        build_memory_root(self.root, {"花蕾": [human("另一个角色")]})
        old = self.root / "memory" / "YUI" / "time_indexed.db"
        new = self.root / "memory" / "花蕾" / "time_indexed.db"
        past = time.time() - 3600
        import os

        os.utime(old, (past, past))
        os.utime(new, (time.time(), time.time()))
        self.assertEqual(host_store.select_store(self.root).name, "花蕾")
        self.assertEqual(host_store.select_store(self.root, "YUI").name, "YUI")

    def test_read_active_store_reports_info(self):
        turns, info = host_store.read_active_store(self.root, limit=10)
        self.assertEqual(info["used"], "YUI")
        self.assertEqual(info["source"], "time_indexed.db")
        self.assertEqual(len(turns), 2)

    def test_missing_root_is_harmless(self):
        self.assertEqual(host_store.find_character_stores(self.root / "nope"), [])
        self.assertEqual(host_store.read_active_store(self.root / "nope")[0], [])


class CrossSourceMergeTests(unittest.TestCase):
    def test_merge_duplicates_across_sources(self):
        """总线记发言时刻、宿主存储记落盘时刻 —— 相差两分钟也要合并成一条。"""

        base = 1_760_000_000.0
        turns = [
            Turn(ts=base, text="你看你，这么激动", role=ROLE_USER, source="bus.memory"),
            Turn(ts=base + 120, text="你看你，这么激动", role=ROLE_USER, source="host.store:YUI"),
        ]
        merged = harvest.merge_duplicates(turns)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].role, ROLE_USER)

    def test_merge_duplicates_keeps_same_source_repeats(self):
        """同一来源真的连说两次相同的话，必须留下两条。"""

        base = 1_760_000_000.0
        turns = [
            Turn(ts=base, text="嗯", role=ROLE_USER, source="bus.memory"),
            Turn(ts=base + 120, text="嗯", role=ROLE_USER, source="bus.memory"),
        ]
        self.assertEqual(len(harvest.merge_duplicates(turns)), 2)

    def test_merge_prefers_known_role(self):
        base = 1_760_000_000.0
        turns = [
            Turn(ts=base, text="同一句", role=ROLE_UNKNOWN, source="bus.conversations"),
            Turn(ts=base + 60, text="同一句", role=ROLE_USER, source="host.store:YUI"),
        ]
        merged = harvest.merge_duplicates(turns)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].role, ROLE_USER)


class PersistentDedupeTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.store = store_mod.ChronicleStore(self.root, config_mod.ChronicleConfig())
        self.store.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_same_text_from_other_source_is_not_stored_twice(self):
        base = 1_760_000_000.0
        first = self.store.append_turns(
            [Turn(ts=base, text="你看你，这么激动", role=ROLE_USER, source="bus.memory")]
        )
        self.assertEqual(sum(first.values()), 1)
        # 两分钟后宿主存储又报同一句话 → 不再新增
        second = self.store.append_turns(
            [Turn(ts=base + 120, text="你看你，这么激动", role=ROLE_USER, source="host.store:YUI")]
        )
        self.assertEqual(sum(second.values()), 0)

    def test_same_source_repeats_are_kept(self):
        base = 1_760_000_000.0
        self.store.append_turns([Turn(ts=base, text="嗯", role=ROLE_USER, source="bus.memory")])
        again = self.store.append_turns(
            [Turn(ts=base + 120, text="嗯", role=ROLE_USER, source="bus.memory")]
        )
        self.assertEqual(sum(again.values()), 1)

    def test_dedupe_survives_reload(self):
        base = 1_760_000_000.0
        self.store.append_turns(
            [Turn(ts=base, text="同一句话", role=ROLE_USER, source="bus.memory")]
        )
        fresh = store_mod.ChronicleStore(self.root, config_mod.ChronicleConfig())
        added = fresh.append_turns(
            [Turn(ts=base + 300, text="同一句话", role=ROLE_USER, source="host.store:YUI")]
        )
        self.assertEqual(sum(added.values()), 0)


class UnwrapTests(unittest.TestCase):
    def test_lanlan_is_not_a_speaker(self):
        """``lanlan`` 是角色归属，不是说话人；拿来当说话人会把用户的话标成角色名。"""

        turn = harvest.turn_from_payload(
            {"type": "user_message", "content": "你看你，这么激动", "lanlan": "YUI", "timestamp": 1.0},
            source="bus.memory",
        )
        self.assertIsNotNone(turn)
        self.assertEqual(turn.role, ROLE_USER)
        self.assertEqual(turn.speaker, "")
        self.assertEqual(turn.meta.get("lanlan"), "YUI")

    def test_unwrap_nested_wrapper_object(self):
        """真实链路：SDK 记录对象 → .raw = {"value": MemoryRecord} → MemoryRecord。

        修之前只剥一层，7 条用户消息全部被当成"未识别"丢掉。
        """

        class MemoryRecord:
            def __init__(self):
                self.kind = "memory"
                self.type = "user_message"
                self.timestamp = 1_760_000_000.0
                self.content = "你看你，这么激动"

        class SdkRecord:
            def __init__(self):
                self.timestamp = 1_760_000_000.0
                self.raw = {"value": MemoryRecord()}

        turn = harvest.turn_from_payload(SdkRecord(), source="bus.memory")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "你看你，这么激动")
        self.assertEqual(turn.role, ROLE_USER)
        self.assertAlmostEqual(turn.ts, 1_760_000_000.0)

    def test_unwrap_plain_shapes_still_work(self):
        self.assertEqual(harvest.unwrap_record({"text": "hi"})["text"], "hi")
        self.assertEqual(harvest.unwrap_record({"value": {"text": "hi"}})["text"], "hi")
        self.assertEqual(harvest.unwrap_record({"payload": {"content": "hi"}})["content"], "hi")
        self.assertEqual(harvest.unwrap_record("字符串"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
