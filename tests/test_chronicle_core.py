"""纯逻辑单元测试：不依赖 N.E.K.O SDK，可用 ``python -m unittest`` 直接跑。

覆盖 models / config / chapter / diary / harvest 五个模块。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle import chapter, config as config_mod, diary, harvest, models  # noqa: E402
from chronicle.models import (  # noqa: E402
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_UNKNOWN,
    ROLE_USER,
    Turn,
)

DAY = "2026-10-06"


def make_turns() -> list[Turn]:
    """构造一天：两个场景，含心情词与关键词。"""

    base = 1_760_000_000.0  # 2025-10-06 前后的固定时间戳，避免依赖当前日期
    return [
        Turn(ts=base, text="早安呀，今天要复习考试", role=ROLE_USER, speaker="主人", source="test"),
        Turn(ts=base + 30, text="早安～我陪你复习，别紧张", role=ROLE_ASSISTANT, speaker="猫娘", source="test"),
        Turn(ts=base + 60, text="谢谢你，喝奶茶吗", role=ROLE_USER, speaker="主人", source="test"),
        Turn(ts=base + 90, text="好耶，奶茶！我很开心", role=ROLE_ASSISTANT, speaker="猫娘", source="test"),
        Turn(ts=base + 3600 * 5, text="复习完了，好累，晚安", role=ROLE_USER, speaker="主人", source="test"),
        Turn(ts=base + 3600 * 5 + 20, text="辛苦啦，好好休息，晚安", role=ROLE_ASSISTANT, speaker="猫娘", source="test"),
    ]


class ModelTests(unittest.TestCase):
    def test_normalize_text_collapses_whitespace(self):
        self.assertEqual(models.normalize_text("  a   b \n\n\n c  "), "a b\n\nc")
        self.assertEqual(models.normalize_text(None), "")

    def test_coerce_timestamp_handles_forms(self):
        self.assertAlmostEqual(models.coerce_timestamp(1760000000), 1760000000.0)
        self.assertAlmostEqual(models.coerce_timestamp(1760000000000), 1760000000.0)
        self.assertAlmostEqual(models.coerce_timestamp("1760000000"), 1760000000.0)
        iso = models.coerce_timestamp("2025-10-06T12:30:00Z")
        self.assertGreater(iso, 1_700_000_000)
        fallback = models.coerce_timestamp("nonsense", 123.0)
        self.assertEqual(fallback, 123.0)

    def test_fingerprint_and_loose_key(self):
        a = Turn(ts=1000.0, text="你好", role=ROLE_USER)
        b = Turn(ts=1000.0, text="你好", role=ROLE_USER)
        self.assertEqual(a.fingerprint(), b.fingerprint())
        # 同一条消息由两条来源上报，时间差几秒 → 精确指纹不同，时间桶指纹相同
        c = Turn(ts=1009.0, text="你好", role=ROLE_USER)
        self.assertNotEqual(a.fingerprint(), c.fingerprint())
        self.assertEqual(a.loose_key(), c.loose_key())

    def test_loose_key_ignores_role_and_speaker(self):
        # bus.memory 带角色、bus.conversations 不带：必须能合并成一条
        with_role = Turn(ts=1000.0, text="同一句话", role=ROLE_USER, speaker="主人")
        without_role = Turn(ts=1002.0, text="同一句话", role=ROLE_UNKNOWN, speaker="")
        self.assertEqual(with_role.loose_key(), without_role.loose_key())
        self.assertNotEqual(with_role.fingerprint(), without_role.fingerprint())

    def test_dedupe_turns_drops_duplicate_sources(self):
        turns = [
            Turn(ts=1000.0, text="同一句话", role=ROLE_USER, source="message"),
            Turn(ts=1005.0, text="同一句话", role=ROLE_USER, source="bus.memory"),
            Turn(ts=1010.0, text="另一句话", role=ROLE_USER, source="message"),
            Turn(ts=1011.0, text="   ", role=ROLE_USER),
        ]
        kept = models.dedupe_turns(turns)
        self.assertEqual([t.text for t in kept], ["同一句话", "另一句话"])

    def test_display_speaker_falls_back_to_names(self):
        turn = Turn(ts=1.0, text="hi", role=ROLE_ASSISTANT)
        self.assertEqual(turn.display_speaker(catgirl_name="小喵"), "小喵")

    def test_speaker_order_uses_display_names(self):
        turns = [
            Turn(ts=1.0, text="a", role=models.ROLE_UNKNOWN),
            Turn(ts=2.0, text="b", role=ROLE_USER),
            Turn(ts=3.0, text="c", role=ROLE_ASSISTANT),
        ]
        order = dict(models.speaker_order(turns))
        self.assertEqual(order.get("记录"), 1)
        self.assertEqual(order.get("主人"), 1)
        self.assertEqual(order.get("猫娘"), 1)

    def test_turn_json_roundtrip(self):
        turn = Turn(ts=1000.5, text="你好\n世界", role=ROLE_USER, speaker="主人", source="t", meta={"k": 1})
        restored = Turn.from_json(turn.to_json())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.text, "你好\n世界")
        self.assertEqual(restored.meta, {"k": 1})
        self.assertIsNone(Turn.from_json({"role": "user"}))


class ConfigTests(unittest.TestCase):
    def test_defaults_when_section_missing(self):
        cfg = config_mod.ChronicleConfig.from_mapping({})
        self.assertEqual(cfg.diary_engine, "auto")
        self.assertEqual(cfg.scene_gap_minutes, 30)
        self.assertTrue(cfg.warnings)

    def test_parses_valid_values(self):
        cfg = config_mod.ChronicleConfig.from_mapping(
            {
                "chronicle": {
                    "diary_engine": "llm",
                    "diary_style": "record",
                    "scene_gap_minutes": 45,
                    "master_name": "阿岚",
                    "catgirl_name": "小喵",
                    "redact": "秘密, 私钥",
                    "llm_base_url": "https://example.com/v1",
                    "llm_model": "demo-model",
                }
            }
        )
        self.assertEqual(cfg.diary_engine, "llm")
        self.assertEqual(cfg.diary_style, "record")
        self.assertEqual(cfg.scene_gap_minutes, 45)
        self.assertEqual(cfg.master_name, "阿岚")
        self.assertEqual(cfg.redact, ["秘密", "私钥"])
        self.assertTrue(cfg.llm_configured())
        self.assertEqual(cfg.warnings, [])

    def test_invalid_values_fall_back_with_warning(self):
        cfg = config_mod.ChronicleConfig.from_mapping(
            {
                "chronicle": {
                    "diary_engine": "gpt5",
                    "scene_gap_minutes": -3,
                    "llm_provider": "telepathy",
                    "poll_memory_limit": 99999,
                }
            }
        )
        self.assertEqual(cfg.diary_engine, "auto")
        self.assertEqual(cfg.scene_gap_minutes, 30)
        self.assertEqual(cfg.llm_provider, "auto")
        self.assertEqual(cfg.poll_memory_limit, 50)
        self.assertEqual(len(cfg.warnings), 4)

    def test_alias_section_name_and_public_dict_hides_key(self):
        cfg = config_mod.ChronicleConfig.from_mapping(
            {"neko_daily_chronicle": {"llm_api_key": "sk-secret"}}
        )
        public = cfg.as_public_dict()
        self.assertTrue(public["llm_api_key_set"])
        self.assertNotIn("sk-secret", repr(public))


class ChapterTests(unittest.TestCase):
    def test_build_chapter_structure(self):
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"scene_gap_minutes": 30}})
        text = chapter.build_chapter(DAY, make_turns(), cfg, index=3)
        self.assertIn("# 第 3 章 · 2026-10-06", text)
        # 每句前面带当天序号（#1、#2…），用来让猫娘按号调取原文
        self.assertIn("**#1 主人**：早安呀，今天要复习考试", text)
        self.assertIn("**#2 猫娘**：早安～我陪你复习，别紧张", text)
        self.assertIn("`#编号`", text)
        # 5 小时的空档应切成第二个场景，并写出间隔说明
        self.assertIn("## 二、", text)
        self.assertIn("隔了 4 小时 58 分", text)
        self.assertIn("共 6 句", text)

    def test_build_chapter_single_scene_when_gap_zero(self):
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"scene_gap_minutes": 0}})
        text = chapter.build_chapter(DAY, make_turns(), cfg)
        self.assertNotIn("## 二、", text)

    def test_build_chapter_escapes_markdown_and_handles_empty(self):
        cfg = config_mod.ChronicleConfig()
        turn = Turn(ts=1_760_000_000.0, text="> 引用行\n# 井号", role=ROLE_USER, speaker="主人")
        text = chapter.build_chapter(DAY, [turn], cfg)
        self.assertIn("\\> 引用行", text)
        self.assertIn("\\# 井号", text)
        empty = chapter.build_chapter(DAY, [], cfg)
        self.assertIn("没有记录到对话", empty)

    def test_system_turns_excluded_by_default(self):
        cfg = config_mod.ChronicleConfig()
        turns = [
            Turn(ts=1_760_000_000.0, text="用户说的话", role=ROLE_USER),
            Turn(ts=1_760_000_001.0, text="系统提示", role=ROLE_SYSTEM),
        ]
        text = chapter.build_chapter(DAY, turns, cfg)
        self.assertIn("用户说的话", text)
        self.assertNotIn("系统提示", text)

    def test_cn_number(self):
        self.assertEqual(chapter.cn_number(1), "一")
        self.assertEqual(chapter.cn_number(10), "十")
        self.assertEqual(chapter.cn_number(11), "十一")
        self.assertEqual(chapter.cn_number(21), "二十一")
        self.assertEqual(chapter.cn_number(120), "120")

    def test_build_index(self):
        cfg = config_mod.ChronicleConfig()
        text = chapter.build_index(
            [{"day": DAY, "index": 2, "turns": 12, "mood": "开心"}], cfg, kind="diary"
        )
        self.assertIn("[2026-10-06（第 2 章）](2026-10-06.md)", text)
        self.assertIn("12 句", text)


class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"catgirl_name": "猫娘"}})
        self.turns = make_turns()
        self.stats = diary.analyze_day(DAY, self.turns, self.cfg)

    def test_extract_keywords_filters_stopwords(self):
        keywords = diary.extract_keywords(
            ["我们今天就是这样的，复习考试很重要", "复习考试加油", "这个是我们的计划"], min_count=1
        )
        terms = [term for term, _ in keywords]
        self.assertTrue(any("复习" in term for term in terms))
        for stop in ("我们", "这个", "就是", "这样"):
            self.assertNotIn(stop, terms)

    def test_detect_mood(self):
        mood, scores = diary.detect_mood(["今天好开心呀哈哈", "开心开心"])
        self.assertEqual(mood, "happy")
        self.assertGreater(scores["happy"], 0)
        neutral, _ = diary.detect_mood(["嗯"])
        self.assertEqual(neutral, "neutral")

    def test_analyze_day_statistics(self):
        stats = self.stats
        self.assertEqual(stats.total, 6)
        self.assertEqual(stats.user_turns, 3)
        self.assertEqual(stats.assistant_turns, 3)
        self.assertGreater(stats.span_seconds, 4 * 3600)
        self.assertTrue(stats.keywords)
        self.assertTrue(stats.highlights)
        self.assertIn("total", stats.to_dict())

    def test_render_local_diary_marks_engine(self):
        text = diary.render_local_diary(DAY, self.stats, self.cfg)
        self.assertIn("今日回忆", text)
        self.assertIn("第一人称", text)
        self.assertIn("本地生成，未调用语言模型", text)
        self.assertIn("第一人称写（本地生成", text)
        self.assertIn("今天", text)

    def test_build_llm_prompt_contains_rules_and_transcript(self):
        prompt = diary.build_llm_prompt(DAY, self.turns, self.stats, self.cfg)
        self.assertIn("心情：", prompt)
        self.assertIn("【对话记录】", prompt)
        self.assertIn("主人：早安呀", prompt)
        english = diary.build_llm_prompt(
            DAY, self.turns, self.stats,
            config_mod.ChronicleConfig.from_mapping({"chronicle": {"diary_language": "en"}}),
        )
        self.assertIn("Mood:", english)

    def test_local_diary_first_person_capped(self):
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"diary_word_limit": 200}})
        text = diary.render_local_diary(DAY, self.stats, cfg)
        body_lines = [
            line for line in text.splitlines()
            if line and not line.startswith("#") and not line.startswith(">")
        ]
        body = "".join(body_lines)
        self.assertLessEqual(len(body), 200 + 4)  # 允许结尾的“……”
        self.assertTrue(body.startswith("今天"))
        self.assertIn("我", body)
        self.assertNotIn("## 今日概览", text)

    def test_parse_llm_diary_with_separator_and_fence(self):
        raw = "```\n心情：开心\n关键词：奶茶、复习\n---\n今天很开心，喝了奶茶。\n```"
        meta, body = diary.parse_llm_diary(raw, self.stats)
        self.assertEqual(meta["mood"], "happy")
        self.assertEqual(meta["keywords"], ["奶茶", "复习"])
        self.assertTrue(meta["parsed"])
        self.assertEqual(body, "今天很开心，喝了奶茶。")

    def test_parse_llm_diary_falls_back_without_format(self):
        meta, body = diary.parse_llm_diary("今天过得还不错，没什么特别的。", self.stats)
        self.assertIn("今天过得还不错", body)
        self.assertEqual(meta["mood"], self.stats.mood)

    def test_compose_diary_local_and_llm(self):
        markdown, info = diary.compose_diary(DAY, self.turns, self.cfg, llm_raw=None)
        self.assertEqual(info["engine"], "local")
        self.assertIn("今日回忆", markdown)
        self.assertIn("第一人称", markdown)

        markdown_llm, info_llm = diary.compose_diary(
            DAY,
            self.turns,
            self.cfg,
            llm_raw="心情：开心\n关键词：奶茶, 复习\n---\n今天和主人一起复习，还喝了奶茶，很开心。",
            llm_model="demo-model",
        )
        self.assertEqual(info_llm["engine"], "llm")
        self.assertIn("demo-model", markdown_llm)
        self.assertIn("今天和主人一起复习", markdown_llm)
        self.assertIn("### 当天数据", markdown_llm)

    def test_build_transcript_truncates_long_days(self):
        turns = [
            Turn(ts=1_760_000_000.0 + index, text="x" * 200, role=ROLE_USER, speaker="主人")
            for index in range(50)
        ]
        cfg = config_mod.ChronicleConfig.from_mapping({"chronicle": {"llm_max_chars": 2000}})
        transcript = diary.build_transcript(turns, cfg)
        self.assertIn("中间省略", transcript)
        self.assertLess(len(transcript), 4000)


class HarvestTests(unittest.TestCase):
    def test_turn_from_plain_dict(self):
        turn = harvest.turn_from_payload(
            {"text": "你好", "sender": "主人", "timestamp": 1760000000, "role": "user_message"},
            source="test",
        )
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "你好")
        self.assertEqual(turn.speaker, "主人")
        self.assertEqual(turn.role, ROLE_USER)

    def test_turn_from_wrapped_payload(self):
        record = {"payload": {"value": {"type": "ai_message", "content": "晚安", "lanlan": "猫娘"}}}
        turn = harvest.turn_from_payload(record, source="bus")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "晚安")
        self.assertEqual(turn.role, ROLE_ASSISTANT)
        # lanlan 是角色归属、不是说话人：只记进 meta，显示名由配置决定
        self.assertEqual(turn.speaker, "")
        self.assertEqual(turn.meta.get("lanlan"), "猫娘")
        self.assertEqual(turn.display_speaker(catgirl_name="猫娘"), "猫娘")

    def test_turn_from_memory_record_object(self):
        """宿主真实的 bus.memory 形态：{"value": MemoryRecord(...)}。"""

        class MemoryRecord:
            def __init__(self):
                self.kind = "memory"
                self.type = "user_message"
                self.timestamp = 1760000000.0
                self.plugin_id = None
                self.source = "main_logic.core"
                self.priority = 0
                self.content = "那当然，我们的故事还有好多没写的呢"
                self.metadata: dict = {}
                self.raw = {"type": "user_message", "_ts": 1760000000.0}

        turn = harvest.turn_from_payload({"value": MemoryRecord()}, source="bus.memory")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "那当然，我们的故事还有好多没写的呢")
        self.assertEqual(turn.role, ROLE_USER)
        self.assertAlmostEqual(turn.ts, 1760000000.0)

    def test_infer_role_from_mapping_is_exact_only(self):
        # source 里含 "in"/"ai" 这类片段，不能因此被误判成角色
        self.assertEqual(harvest.infer_role_from_mapping({"source": "main_logic.core"}), ROLE_UNKNOWN)
        self.assertEqual(harvest.infer_role_from_mapping({"who": "assistant"}), ROLE_ASSISTANT)
        self.assertEqual(harvest.infer_role_from_mapping({"content": "user"}), ROLE_UNKNOWN)

    def test_describe_record(self):
        described = harvest.describe_record({"text": "你好", "type": "user_message", "extra": 1})
        self.assertEqual(described["text"], "你好")
        self.assertEqual(described["type"], "user_message")

    def test_turn_from_object_with_attributes(self):
        class Record:
            def __init__(self):
                self.text = "来自对象的问候"
                self.sender = "猫娘"
                self.timestamp = 1760000000.0
                self.role = "assistant"

        turn = harvest.turn_from_payload(Record(), source="obj")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "来自对象的问候")
        self.assertEqual(turn.role, ROLE_ASSISTANT)

    def test_parts_schema_and_multiline(self):
        turn = harvest.turn_from_payload(
            {"role": "assistant", "content": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]},
            source="parts",
        )
        self.assertIsNotNone(turn)
        self.assertIn("第一段", turn.text)
        self.assertIn("第二段", turn.text)

    def test_message_kwargs_and_nested(self):
        direct = harvest.turns_from_message_kwargs({"text": "直接形态", "sender": "主人"})
        self.assertEqual(len(direct), 1)
        nested = harvest.turns_from_message_kwargs({"data": {"content": "嵌套形态", "role": "user"}})
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0].text, "嵌套形态")
        self.assertEqual(harvest.turns_from_message_kwargs({}), [])
        self.assertEqual(harvest.turns_from_message_kwargs({"foo": "没有文本"}), [])

    def test_turns_from_records_skips_junk(self):
        records = [
            {"text": "有效", "role": "user"},
            {"无文本": True},
            None,
            "字符串形态",
        ]
        turns = harvest.turns_from_records(records, source="bus")
        texts = [turn.text for turn in turns]
        self.assertIn("有效", texts)
        self.assertIn("字符串形态", texts)
        self.assertEqual(len(turns), 2)

    def test_infer_role_aliases(self):
        self.assertEqual(harvest.infer_role("user_message"), ROLE_USER)
        self.assertEqual(harvest.infer_role("AI_MESSAGE"), ROLE_ASSISTANT)
        self.assertEqual(harvest.infer_role("tool_result"), ROLE_SYSTEM)
        self.assertEqual(harvest.infer_role(None, ROLE_USER), ROLE_USER)
        # 宿主可能把角色写成这些名字
        self.assertEqual(harvest.infer_role("environment_hint"), ROLE_SYSTEM)
        self.assertEqual(harvest.infer_role("sender_type", ROLE_UNKNOWN), ROLE_UNKNOWN)

    def test_host_prompt_is_classified_as_system(self):
        prompt = (
            "======以下为环境提示======\n"
            "刚才哥哥请你去一旁歇着，你就变成猫咪的样子睡了一小时。\n"
            "不要生成思考过程。\n"
            "======以上为环境提示======"
        )
        self.assertTrue(harvest.looks_like_host_prompt(prompt))
        self.assertFalse(harvest.looks_like_host_prompt("喵呜~我刚醒"))
        # 没有 role 字段时，靠提示词特征兜底判成系统消息
        turn = harvest.turn_from_payload({"text": prompt, "kind": "turn"}, source="bus")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.role, ROLE_SYSTEM)

    def test_speaker_field_can_determine_role(self):
        first = harvest.turn_from_payload({"text": "你好", "sender": "user"}, source="bus")
        self.assertIsNotNone(first)
        self.assertEqual(first.role, ROLE_USER)
        second = harvest.turn_from_payload({"text": "喵呜", "speaker": "猫娘"}, source="bus")
        self.assertIsNotNone(second)
        self.assertEqual(second.role, ROLE_ASSISTANT)

    def test_conversations_shape_with_sender_type(self):
        record = {"sender_type": "assistant", "content": "我陪你", "speaker": "猫娘", "ts": 1760000000}
        turn = harvest.turn_from_payload(record, source="bus.conversations")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.role, ROLE_ASSISTANT)
        self.assertEqual(turn.speaker, "猫娘")

    def test_merge_duplicates_prefers_known_role(self):
        turns = [
            Turn(ts=1000.0, text="同一句话", role=ROLE_UNKNOWN, source="bus.conversations"),
            Turn(ts=1000.5, text="同一句话", role=ROLE_USER, source="bus.memory"),
        ]
        merged = harvest.merge_duplicates(turns)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].role, ROLE_USER)

    def test_resolve_roles_after_host_prompt(self):
        prompt = Turn(ts=1.0, text="======以下为环境提示======\n打个招呼", role=ROLE_SYSTEM)
        reply = Turn(ts=2.0, text="喵呜~我刚醒", role=ROLE_UNKNOWN)
        resolved = harvest.resolve_roles([prompt, reply])
        self.assertEqual(resolved[0].role, ROLE_SYSTEM)
        self.assertEqual(resolved[1].role, ROLE_ASSISTANT)

    def test_resolve_roles_alternates_user_and_character(self):
        turns = [
            Turn(ts=1.0, text="在吗", role=ROLE_USER),
            Turn(ts=2.0, text="在的", role=ROLE_UNKNOWN),
            Turn(ts=3.0, text="陪我聊聊", role=ROLE_UNKNOWN),
            Turn(ts=4.0, text="好呀", role=ROLE_UNKNOWN),
        ]
        resolved = harvest.resolve_roles(turns)
        self.assertEqual(
            [turn.role for turn in resolved],
            [ROLE_USER, ROLE_ASSISTANT, ROLE_USER, ROLE_ASSISTANT],
        )

    def test_resolve_roles_backward_fill(self):
        turns = [
            Turn(ts=1.0, text="开头没有角色", role=ROLE_UNKNOWN),
            Turn(ts=2.0, text="我来回应", role=ROLE_ASSISTANT),
        ]
        resolved = harvest.resolve_roles(turns)
        self.assertEqual([turn.role for turn in resolved], [ROLE_USER, ROLE_ASSISTANT])

    def test_resolve_roles_keeps_unknown_when_no_signal(self):
        resolved = harvest.resolve_roles([Turn(ts=1.0, text="孤立一句", role=ROLE_UNKNOWN)])
        self.assertEqual(resolved[0].role, ROLE_UNKNOWN)

    def test_group_by_day(self):
        turns = [
            Turn(ts=1_760_000_000.0, text="a", role=ROLE_USER),
            Turn(ts=1_760_000_000.0 + 86400, text="b", role=ROLE_USER),
        ]
        grouped = harvest.group_by_day(turns)
        self.assertEqual(len(grouped), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
