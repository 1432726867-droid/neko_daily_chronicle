"""序号（编号）与读取工具的测试。

序号是"当天第几条记录"，章节、界面表格与给猫娘的工具必须完全一致，
所以这里把规则钉死：按时间排序、从 1 开始、同秒稳定。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle import config as config_mod, reader  # noqa: E402
from chronicle.models import ROLE_ASSISTANT, ROLE_USER, Turn, numbered_turns  # noqa: E402

BASE = 1_760_000_000.0


def sample_turns() -> list[Turn]:
    # 故意乱序给出，验证编号按时间而不是按输入顺序
    return [
        Turn(ts=BASE + 120, text="第三句", role=ROLE_ASSISTANT, source="host.store:YUI"),
        Turn(ts=BASE, text="第一句", role=ROLE_USER, source="bus.memory"),
        Turn(ts=BASE + 60, text="第二句", role=ROLE_ASSISTANT, source="host.store:YUI"),
    ]


class NumberingTests(unittest.TestCase):
    def test_numbered_by_time_from_one(self):
        pairs = numbered_turns(sample_turns())
        self.assertEqual([index for index, _ in pairs], [1, 2, 3])
        self.assertEqual([turn.text for _, turn in pairs], ["第一句", "第二句", "第三句"])

    def test_same_timestamp_keeps_input_order(self):
        same = [
            Turn(ts=BASE, text="先说的", role=ROLE_USER, source="bus.memory"),
            Turn(ts=BASE, text="后说的", role=ROLE_ASSISTANT, source="bus.memory"),
        ]
        self.assertEqual(
            [turn.text for _, turn in numbered_turns(same)], ["先说的", "后说的"]
        )


class DayRowsTests(unittest.TestCase):
    def test_rows_are_numbered_and_windowed(self):
        rows = reader.day_rows(sample_turns(), start=2, count=1, text_limit=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["index"], 2)
        self.assertEqual(rows[0]["text"], "第二句")
        # 时刻按本地时区渲染，只校验形状（HH:MM）与其余字段
        self.assertRegex(rows[0]["time"], r"^\d{2}:\d{2}$")
        self.assertEqual(rows[0]["speaker"], "猫娘")
        self.assertTrue(rows[0]["date"].count("-") == 2)

    def test_window_past_the_end_is_empty(self):
        self.assertEqual(reader.day_rows(sample_turns(), start=99, count=5), [])

    def test_count_zero_means_to_the_end(self):
        rows = reader.day_rows(sample_turns(), start=2, count=0)
        self.assertEqual([row["index"] for row in rows], [2, 3])

    def test_long_text_is_truncated_with_notice(self):
        long_turn = Turn(ts=BASE, text="字" * 500, role=ROLE_USER, source="bus.memory")
        rows = reader.day_rows([long_turn], text_limit=50)
        self.assertIn("已截断", rows[0]["text"])
        self.assertEqual(len(reader.day_rows([long_turn], text_limit=0)[0]["text"]), 500)


class FindTurnTests(unittest.TestCase):
    def test_find_by_index(self):
        found = reader.find_turn(sample_turns(), 3)
        self.assertIsNotNone(found)
        index, turn = found
        self.assertEqual(index, 3)
        self.assertEqual(turn.text, "第三句")

    def test_out_of_range_returns_none(self):
        self.assertIsNone(reader.find_turn(sample_turns(), 0))
        self.assertIsNone(reader.find_turn(sample_turns(), 99))


class SearchTests(unittest.TestCase):
    def test_search_returns_index_and_snippet(self):
        hits = reader.search_turns(
            [("2026-10-06", sample_turns())], "第二", limit=5, snippet_limit=40
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["index"], 2)
        self.assertEqual(hits[0]["date"], "2026-10-06")
        self.assertIn("第二句", hits[0]["snippet"])

    def test_search_respects_limit_and_empty_query(self):
        self.assertEqual(reader.search_turns([("d", sample_turns())], "句", limit=2).__len__(), 2)
        self.assertEqual(reader.search_turns([("d", sample_turns())], "   "), [])

    def test_search_prefers_earlier_days_first(self):
        hits = reader.search_turns(
            [("2026-10-06", sample_turns()), ("2026-10-05", sample_turns())], "第一句", limit=5
        )
        self.assertEqual([hit["date"] for hit in hits], ["2026-10-06", "2026-10-05"])

    def test_balance_days_keeps_old_records_visible(self):
        # 最近 20 天都命中、名额只有 5 条：仍要在整段历史上抽到更早的日期，
        # 否则浏览页会出现"搜不到 10 月初记录"的现象。
        day_pairs = [(f"2026-10-{n:02d}", sample_turns()) for n in range(20, 0, -1)]
        hits = reader.search_turns(day_pairs, "第一句", limit=5, balance_days=True)
        self.assertEqual(len(hits), 5)
        self.assertIn("2026-10-01", [hit["date"] for hit in hits])


class DigestTests(unittest.TestCase):
    def test_day_digest_has_numbering_friendly_fields(self):
        data = reader.day_digest("2026-10-06", sample_turns(), config_mod.ChronicleConfig())
        self.assertEqual(data["total"], 3)
        self.assertIn("猫娘", data["speaker_order"])
        self.assertIn("keywords", data)
        self.assertIn("–", data["time_span"])


class RenderLineTests(unittest.TestCase):
    def test_render_line_is_compact_and_numbered(self):
        rows = reader.day_rows(sample_turns(), start=1, count=1, text_limit=0)
        line = reader.render_line(rows[0])
        self.assertTrue(line.startswith("#1 "))
        self.assertIn("第一句", line)
        self.assertIn("主人", line)
        with_date = reader.render_line(rows[0], with_date=True)
        self.assertTrue(with_date.startswith(rows[0]["date"]))

    def test_describe_rows_joins(self):
        rows = reader.day_rows(sample_turns(), text_limit=0)
        text = reader.describe_rows(rows)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("#3 ", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
