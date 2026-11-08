"""拾光对话目录的纯逻辑测试（不启动 Tk）。"""

from __future__ import annotations

import pathlib
import unittest

from chronicle import catalog
from chronicle import config as config_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CatalogCommandTests(unittest.TestCase):
    def test_parse_bare_and_query(self):
        self.assertEqual(catalog.parse_command("#拾光"), "")
        self.assertEqual(catalog.parse_command("#拾光 雨天"), "雨天")
        self.assertEqual(catalog.parse_command("#拾光：猫"), "猫")
        self.assertEqual(catalog.parse_command("#拾光  猫"), "猫")
        self.assertIsNone(catalog.parse_command("今天想聊聊"))
        # 自然说法也要能开：主人很少真的敲 #号
        self.assertEqual(catalog.parse_command("拾光"), "")
        self.assertEqual(catalog.parse_command("打开拾光"), "")
        self.assertEqual(catalog.parse_command("帮我打开拾光 雨天"), "雨天")
        # 长句里顺便提到拾光，不算命令
        self.assertIsNone(catalog.parse_command("拾光这个功能还挺好用的我觉得还行"))

    def test_custom_command(self):
        self.assertEqual(catalog.parse_command("/catalog 旧事", "/catalog"), "旧事")
        self.assertIsNone(catalog.parse_command("#拾光", "/catalog"))

    def test_injection_markers(self):
        self.assertTrue(catalog.is_catalog_quote(catalog.QUOTE_MARKER + "\n内容"))
        self.assertTrue(
            catalog.is_catalog_injection(catalog.DAY_PROMPT_MARKER + "\n2026-11-03")
        )
        self.assertFalse(catalog.is_catalog_injection("普通聊天"))


class CatalogFormatTests(unittest.TestCase):
    def test_quote_sorts_and_skips_duplicates(self):
        rows = [
            {"date": "2026-11-03", "index": 2, "time": "18:02", "role": "assistant",
             "speaker": "猫娘", "text": "第二句"},
            {"date": "2026-11-03", "index": 1, "time": "18:01", "role": "user",
             "speaker": "主人", "text": "第一句"},
            {"date": "2026-11-03", "index": 2, "time": "18:02", "role": "assistant",
             "speaker": "猫娘", "text": "第二句"},
        ]
        text = catalog.format_quote(rows)
        self.assertIn(catalog.QUOTE_MARKER, text)
        self.assertLess(text.index("第一句"), text.index("第二句"))
        self.assertEqual(text.count("第二句"), 1)
        self.assertIn(catalog.QUOTE_FOOTER, text)

    def test_quote_caps_lines_and_chars(self):
        rows = [
            {"date": "2026-11-03", "index": index, "time": "18:01",
             "role": "user", "speaker": "主人", "text": "第" * 400}
            for index in range(1, 40)
        ]
        text = catalog.format_quote(rows)
        self.assertIn("没有放进来", text)
        self.assertLessEqual(len(text), catalog.MAX_SEND_CHARS + 200)

    def test_day_prompt(self):
        text = catalog.format_day_prompt("2026-11-03", turns=5)
        self.assertIn(catalog.DAY_PROMPT_MARKER, text)
        self.assertIn("2026-11-03", text)
        self.assertIn("5 句", text)

    def test_topic_template(self):
        rows = [
            {"date": "2026-11-03", "index": 1, "time": "18:01",
             "role": "user", "speaker": "主人", "text": "今天好累。"}
        ]
        text = catalog.format_quote(rows, template="topic")
        self.assertIn(catalog.TOPIC_LEAD, text)
        self.assertIn(catalog.TOPIC_FOOTER, text)
        self.assertNotIn(catalog.QUOTE_FOOTER, text)

    def test_template_text_override(self):
        rows = [
            {"date": "2026-11-03", "index": 1, "time": "18:01",
             "role": "user", "speaker": "主人", "text": "今天好累。"}
        ]
        text = catalog.format_quote(rows, footer="（自定义结尾）")
        self.assertIn("（自定义结尾）", text)
        self.assertNotIn(catalog.QUOTE_FOOTER, text)

        topic = catalog.format_quote(rows, template="topic", lead="（自定义开头）", footer="")
        self.assertIn("（自定义开头）", topic)
        self.assertNotIn(catalog.TOPIC_LEAD, topic)
        self.assertNotIn(catalog.TOPIC_FOOTER, topic)

    def test_mark_query(self):
        text = catalog.mark_query("我今天想起了雨天的那句话", "雨天", limit=20)
        self.assertIn("⟦雨天⟧", text)
        self.assertTrue(text.startswith("…") or len(text) <= 20)
        self.assertEqual(catalog.mark_query("没有命中", "雨天", limit=10), "没有命中")

    def test_empty_quote(self):
        self.assertEqual(catalog.format_quote([]), "")


class CatalogConfigTests(unittest.TestCase):
    def test_defaults_are_silent(self):
        cfg = config_mod.ChronicleConfig()
        self.assertFalse(cfg.catalog_enabled)
        self.assertEqual(cfg.catalog_command, "#拾光")
        self.assertFalse(cfg.catalog_send_respond)
        self.assertEqual(cfg.catalog_max_scan_days, 365)
        self.assertEqual(cfg.catalog_idle_close_minutes, 10)
        self.assertTrue(cfg.catalog_restore_last_search)

    def test_parse_enabled(self):
        cfg = config_mod.ChronicleConfig.from_mapping(
            {
                "chronicle": {
                    "catalog_enabled": True,
                    "catalog_command": "/拾光",
                    "catalog_send_respond": True,
                    "catalog_width": 360,
                    "catalog_ball_size": 56,
                }
            }
        )
        self.assertTrue(cfg.catalog_enabled)
        self.assertEqual(cfg.catalog_command, "/拾光")
        self.assertTrue(cfg.catalog_send_respond)
        self.assertEqual(cfg.catalog_width, 360)
        self.assertEqual(cfg.catalog_ball_size, 56)


if __name__ == "__main__":
    unittest.main()
