"""存储层与模型直连层的单元测试（同样不依赖 SDK）。"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import sys
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle import config as config_mod, llm, models, store as store_mod  # noqa: E402
from chronicle.models import ROLE_ASSISTANT, ROLE_USER, Turn  # noqa: E402

# 固定时间戳，但日期键用本地时区算出来，避免测试依赖机器时区。
TS1 = 1_760_000_000.0
TS2 = TS1 + 2 * 86400
DAY1 = models.local_day_key(TS1)
DAY2 = models.local_day_key(TS2)

#: 临时目录放在插件目录下，并且用普通 mkdir 创建。
#: 不用 ``tempfile.mkdtemp``：它在部分受限环境下会生成无法继续写入的目录。
TMP_ROOT = ROOT / ".tmp-tests"


def make_temp_dir() -> pathlib.Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True)
    return target


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir()
        self.cfg = config_mod.ChronicleConfig()
        self.store = store_mod.ChronicleStore(self.root, self.cfg)
        self.store.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_append_and_reload_turns(self):
        added = self.store.append_turns(
            [
                Turn(ts=1_760_000_060.0, text="第二句", role=ROLE_ASSISTANT, speaker="猫娘"),
                Turn(ts=1_760_000_000.0, text="第一句", role=ROLE_USER, speaker="主人"),
            ]
        )
        self.assertEqual(sum(added.values()), 2)
        turns = self.store.load_turns(DAY1)
        self.assertEqual([turn.text for turn in turns], ["第一句", "第二句"])

        # 重复追加同一条（含几秒钟漂移）不再新增
        again = self.store.append_turns(
            [
                Turn(ts=1_760_000_000.0, text="第一句", role=ROLE_USER, speaker="主人"),
                Turn(ts=1_760_000_004.0, text="第一句", role=ROLE_USER, speaker="主人"),
            ]
        )
        self.assertEqual(sum(again.values()), 0)
        self.assertEqual(len(self.store.load_turns(DAY1)), 2)

    def test_dedupe_survives_reload_from_disk(self):
        self.store.append_turns([Turn(ts=1_760_000_000.0, text="持久化的一句", role=ROLE_USER)])
        fresh = store_mod.ChronicleStore(self.root, self.cfg)
        added = fresh.append_turns([Turn(ts=1_760_000_005.0, text="持久化的一句", role=ROLE_USER)])
        self.assertEqual(sum(added.values()), 0)

    def test_redaction_applied_before_write(self):
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"redact": "秘密"}})
        store = store_mod.ChronicleStore(self.root / "redacted", cfg)
        store.ensure_dirs()
        store.append_turns([Turn(ts=1_760_000_000.0, text="这是秘密内容", role=ROLE_USER)])
        turns = store.load_turns(DAY1)
        self.assertEqual(turns[0].text, "这是＊＊＊内容")

    def test_chapter_and_diary_roundtrip(self):
        self.store.write_chapter(DAY1, "# 章节\n正文\n")
        self.store.write_diary(DAY1, "# 日记\n正文\n")
        self.assertTrue(self.store.has_chapter(DAY1))
        self.assertTrue(self.store.has_diary(DAY1))
        self.assertIn("章节", self.store.read_chapter(DAY1))
        self.assertIn("日记", self.store.read_diary(DAY1))
        self.assertIsNone(self.store.read_chapter("1999-01-01"))

    def test_day_index_and_indexes(self):
        self.store.append_turns([Turn(ts=TS1, text="第一天", role=ROLE_USER)])
        self.store.append_turns([Turn(ts=TS2, text="第二天", role=ROLE_USER)])
        days = self.store.list_raw_days()
        self.assertEqual(len(days), 2)
        self.assertEqual(days, [DAY1, DAY2])
        self.assertEqual(self.store.day_index(days[0]), 1)
        self.assertEqual(self.store.day_index(days[1]), 2)

        self.store.write_chapter(days[0], "# 第一章")
        self.store.write_diary(days[0], "# 第一篇日记")
        self.store.write_indexes(mood_by_day={days[0]: "开心"})
        index = self.store.read_text(self.store.chapters_dir / "README.md")
        self.assertIn(days[0], index)
        diary_index = self.store.read_text(self.store.diary_dir / "README.md")
        self.assertIn("开心", diary_index)

    def test_search(self):
        self.store.write_chapter(DAY1, "# 章节\n今天一起复习考试，还喝了奶茶。\n")
        self.store.write_diary(DAY1, "# 日记\n奶茶真好喝。\n")
        hits = self.store.search("奶茶")
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(hit["day"] == DAY1 for hit in hits))
        self.assertEqual(self.store.search(""), [])
        self.assertEqual(self.store.search("不存在的词"), [])

    def test_state_roundtrip_and_stats(self):
        self.store.save_state({"finalized": [DAY1]})
        self.assertEqual(self.store.load_state()["finalized"], [DAY1])
        self.store.append_turns([Turn(ts=TS1, text="x", role=ROLE_USER)])
        stats = self.store.stats()
        self.assertEqual(stats["days"], 1)
        self.assertEqual(stats["chapters"], 0)

    def test_raw_file_is_jsonl(self):
        self.store.append_turns([Turn(ts=TS1, text="一行一条", role=ROLE_USER)])
        path = self.store.raw_path(DAY1)
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "一行一条")


class CatDiaryStoreTests(unittest.TestCase):
    """猫娘日记：条目化 JSON + Markdown 视图 + 迁移/检索/统计/往年今日。"""

    def setUp(self):
        self.root = make_temp_dir()
        self.cfg = config_mod.ChronicleConfig()
        self.store = store_mod.ChronicleStore(self.root, self.cfg)
        self.store.ensure_dirs()
        self.today = models.local_day_key()
        year = int(self.today[:4])
        self.last_year = f"{year - 1}{self.today[4:]}"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_legacy_markdown_migrates_to_entries(self):
        legacy = (
            f"# {self.today} · 猫娘的日记\n\n"
            "> ✎ 13:52 ｜ 😊 开心 ｜ 标签：日常 · 写日记\n\n"
            "第一篇喵～\n\n"
            "> ✎ 18:07 ｜ 😴 疲惫 ｜ 🔒 仅自己可见\n\n"
            "第二篇悄悄话\n"
        )
        self.store._atomic_write(self.store.cat_diary_path(self.today), legacy)
        self.assertFalse(self.store.cat_entries_path(self.today).is_file())

        entries = self.store.cat_diary_entries(self.today)
        self.assertEqual([item["text"] for item in entries], ["第一篇喵～", "第二篇悄悄话"])
        self.assertEqual(entries[0]["mood"], "happy")
        self.assertEqual(entries[0]["tags"], ["日常", "写日记"])
        self.assertEqual(entries[1]["visibility"], "private")
        # 迁移后 JSON 落盘，Markdown 仍在且内容完整
        payload = json.loads(
            self.store.cat_entries_path(self.today).read_text(encoding="utf-8")
        )
        self.assertEqual(len(payload["entries"]), 2)
        self.assertIn("第一篇喵～", self.store.read_cat_diary(self.today))
        # 浏览页视图按可见性过滤
        filtered = self.store.cat_diary_markdown(self.today, include_private=False)
        self.assertIn("第一篇喵～", filtered)
        self.assertNotIn("悄悄话", filtered)

    def test_append_replace_keeps_history(self):
        self.store.append_cat_diary(self.today, "第一段", mood="happy", tags=["日常"])
        self.store.append_cat_diary(self.today, "第二段", visibility="private")
        entries = self.store.cat_diary_entries(self.today)
        self.assertEqual(len(entries), 2)
        self.assertIn("第一段", self.store.read_cat_diary(self.today))
        self.assertFalse(self.store.cat_diary_all_private(self.today))

        replaced = self.store.replace_cat_diary(self.today, "重写版", mood="calm")
        self.assertTrue(replaced["replaced"])
        self.assertEqual(replaced["removed"], 2)
        live = self.store.cat_diary_entries(self.today)
        self.assertEqual([item["text"] for item in live], ["重写版"])
        self.assertEqual(live[0]["mood"], "calm")
        history = self.store.cat_diary_entries(self.today, include_deleted=True)
        self.assertEqual(len(history), 3)
        self.assertEqual(
            sum(1 for item in history if item["deleted"]), 2
        )
        text = self.store.read_cat_diary(self.today)
        self.assertNotIn("第一段", text)
        self.assertIn("重写版", text)

    def test_update_and_delete_entry(self):
        self.store.append_cat_diary(self.today, "旧正文", mood="sad")
        entry = self.store.cat_diary_entries(self.today)[0]

        updated = self.store.update_cat_diary_entry(
            self.today,
            entry["id"],
            text="新正文",
            mood="开心",
            tags="日常, 海边",
            visibility="private",
        )
        self.assertTrue(updated["updated"])
        self.assertEqual(updated["entry"]["text"], "新正文")
        self.assertEqual(updated["entry"]["mood"], "happy")
        self.assertEqual(updated["entry"]["tags"], ["日常", "海边"])
        self.assertEqual(updated["entry"]["visibility"], "private")
        self.assertIn("新正文", self.store.read_cat_diary(self.today))
        self.assertNotIn("旧正文", self.store.read_cat_diary(self.today))
        self.assertFalse(self.store.update_cat_diary_entry(self.today, "nope", text="x")["updated"])

        soft = self.store.delete_cat_diary_entry(self.today, entry["id"])
        self.assertTrue(soft["deleted"])
        self.assertEqual(self.store.cat_diary_entries(self.today), [])
        self.assertEqual(self.store.list_cat_diary_days(), [])
        self.assertFalse(self.store.delete_cat_diary_entry(self.today, entry["id"])["deleted"])
        self.assertEqual(len(self.store.cat_diary_entries(self.today, include_deleted=True)), 1)

        permanent = self.store.delete_cat_diary_entry(
            self.today, entry["id"], permanent=True
        )
        self.assertTrue(permanent["deleted"])
        self.assertEqual(self.store.cat_diary_entries(self.today, include_deleted=True), [])

    def test_search_stats_and_throwback(self):
        self.store.append_cat_diary(
            self.last_year, "去年的今天去看了海。", mood="happy", tags=["海边"]
        )
        self.store.append_cat_diary(
            self.today, "今天复习考试，有点紧张。", mood="anxious", tags=["学习"]
        )
        self.store.append_cat_diary(
            self.today, "晚上喝了奶茶好开心。", mood="happy", tags=["奶茶"]
        )

        hits = self.store.search_cat_diary("奶茶")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["date"], self.today)
        self.assertIn("奶茶", hits[0]["snippet"])
        self.assertEqual(self.store.search_cat_diary(""), [])
        self.assertEqual(self.store.search_cat_diary("面包"), [])

        stats = self.store.cat_diary_stats(days=3650)
        self.assertEqual(stats["entries"], 3)
        self.assertEqual(stats["diary_days"], 2)
        self.assertEqual(stats["moods"][0]["mood"], "happy")
        self.assertEqual(stats["moods"][0]["count"], 2)
        tag_names = {item["tag"] for item in stats["tags"]}
        self.assertTrue({"海边", "学习", "奶茶"} <= tag_names)

        throwback = self.store.cat_diary_throwback(day=self.today)
        self.assertEqual(len(throwback), 1)
        self.assertEqual(throwback[0]["date"], self.last_year)
        self.assertEqual(throwback[0]["years_ago"], 1)
        self.assertEqual(self.store.cat_diary_throwback(day=self.today, years=0)[0]["count"], 1)


class LlmTests(unittest.TestCase):
    def test_explicit_endpoint_wins(self):
        cfg = config_mod.ChronicleConfig.from_mapping(
            {"chronicle": {"llm_base_url": "https://api.example.com/v1", "llm_model": "m1", "llm_api_key": "sk-x"}}
        )
        endpoint, reason = llm.resolve_endpoint(cfg, {})
        self.assertEqual(reason, "explicit")
        self.assertEqual(endpoint.model, "m1")
        self.assertTrue(endpoint.api_key)
        self.assertNotIn("sk-x", repr(endpoint))
        self.assertNotIn("sk-x", json.dumps(endpoint.describe()))

    def test_free_route_is_refused_with_reason(self):
        cfg = config_mod.ChronicleConfig()
        core_cfg = {
            "coreApi": "free",
            "assistApi": "free",
            "resolvedProviderUrls": {
                "core": {"free": "wss://www.lanlan.tech/core"},
                "assist": {"free": "https://www.lanlan.tech/text/v1"},
            },
            "summaryModelProvider": "follow_assist",
            "summaryModelId": "some-model",
        }
        endpoint, reason = llm.resolve_endpoint(cfg, core_cfg)
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "free_route")
        self.assertIn("免费路由", llm.reason_text(reason))

    def test_custom_slot_is_usable(self):
        cfg = config_mod.ChronicleConfig()
        core_cfg = {
            "coreApi": "free",
            "summaryModelProvider": "custom",
            "summaryModelUrl": "http://127.0.0.1:11434/v1",
            "summaryModelId": "qwen2.5",
            "summaryModelApiKey": "",
        }
        endpoint, reason = llm.resolve_endpoint(cfg, core_cfg)
        self.assertEqual(reason, "host_slot")
        self.assertEqual(endpoint.base_url, "http://127.0.0.1:11434/v1")
        self.assertFalse(endpoint.api_key)

    def test_named_provider_uses_saved_key_and_default_url(self):
        cfg = config_mod.ChronicleConfig()
        core_cfg = {
            "coreApi": "qwen",
            "assistApi": "deepseek",
            "assistApiKeyDeepseek": "sk-deepseek",
            "summaryModelProvider": "deepseek",
            "summaryModelId": "deepseek-chat",
        }
        endpoint, reason = llm.resolve_endpoint(cfg, core_cfg)
        self.assertEqual(reason, "host_slot")
        self.assertEqual(endpoint.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(endpoint.api_key, "sk-deepseek")

    def test_saved_provider_url_beats_default(self):
        cfg = config_mod.ChronicleConfig()
        core_cfg = {
            "coreApi": "custom",
            "resolvedProviderUrls": {"assist": {"deepseek": "https://proxy.local/v1"}},
            "assistApiKeyDeepseek": "k",
            "summaryModelProvider": "deepseek",
            "summaryModelId": "deepseek-chat",
        }
        endpoint, _ = llm.resolve_endpoint(cfg, core_cfg)
        self.assertEqual(endpoint.base_url, "https://proxy.local/v1")

    def test_local_engine_disables_llm(self):
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"diary_engine": "local"}})
        endpoint, reason = llm.resolve_endpoint(cfg, {"coreApi": "qwen"})
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "disabled")

    def test_plugin_only_mode_without_endpoint(self):
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"llm_provider": "plugin"}})
        endpoint, reason = llm.resolve_endpoint(cfg, {"coreApi": "qwen"})
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "plugin_only")

    def test_missing_config_reports_no_config(self):
        cfg = config_mod.ChronicleConfig()
        endpoint, reason = llm.resolve_endpoint(cfg, {})
        self.assertIsNone(endpoint)
        self.assertEqual(reason, "no_config")

    def test_locate_core_config_reads_user_data_root(self):
        root = make_temp_dir()
        try:
            (root / "config").mkdir(parents=True)
            (root / "config" / "core_config.json").write_text('{"coreApi": "qwen"}', encoding="utf-8")
            path, data = llm.locate_core_config(root)
            self.assertIsNotNone(path)
            self.assertEqual(data["coreApi"], "qwen")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_locate_core_config_returns_nothing_when_absent(self):
        missing = make_temp_dir()
        try:
            # 把所有兜底候选一起隔离掉，测试才与这台机器上装了什么都无关
            env = {
                "LOCALAPPDATA": str(missing / "local"),
                "APPDATA": str(missing / "roaming"),
                "USERPROFILE": str(missing / "home"),
                "HOME": str(missing / "home"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("NEKO_DATA_DIR", None)
                os.environ.pop("NEKO_USER_DATA_DIR", None)
                path, data = llm.locate_core_config(missing / "nope")
            self.assertIsNone(path)
            self.assertEqual(data, {})
        finally:
            shutil.rmtree(missing, ignore_errors=True)

    def test_completion_url_normalization(self):
        self.assertEqual(llm._completion_url("https://x/v1"), "https://x/v1/chat/completions")
        self.assertEqual(llm._completion_url("https://x/v1/"), "https://x/v1/chat/completions")
        self.assertEqual(
            llm._completion_url("https://x/v1/chat/completions"), "https://x/v1/chat/completions"
        )


class RetryTests(unittest.TestCase):
    def test_retry_until_third_attempt(self):
        calls = []

        async def fake(endpoint, prompt, **kwargs):
            calls.append(prompt)
            if len(calls) < 3:
                return None, "http_error:500"
            return "整理后的内容", ""

        async def run():
            with mock.patch.object(llm, "achat_completion", fake):
                return await llm.achat_completion_with_retry(
                    mock.Mock(), "原始内容", attempts=3, retry_delay=0
                )

        text, reason, attempts = asyncio.run(run())
        self.assertEqual(text, "整理后的内容")
        self.assertEqual(reason, "")
        self.assertEqual(attempts, 3)
        self.assertEqual(len(calls), 3)

    def test_non_retryable_reason_stops_immediately(self):
        calls = []

        async def fake(endpoint, prompt, **kwargs):
            calls.append(prompt)
            return None, "http_error:401"

        async def run():
            with mock.patch.object(llm, "achat_completion", fake):
                return await llm.achat_completion_with_retry(
                    mock.Mock(), "原始内容", attempts=3, retry_delay=0
                )

        text, reason, attempts = asyncio.run(run())
        self.assertIsNone(text)
        self.assertEqual(reason, "http_error:401")
        self.assertEqual(attempts, 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
