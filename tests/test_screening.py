"""放映厅状态机与存储的单元测试（不依赖 SDK）。"""

from __future__ import annotations

import pathlib
import shutil
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle import config as config_mod  # noqa: E402
from chronicle import diary, screening, screening_review, store as store_mod  # noqa: E402
from chronicle.models import ROLE_ASSISTANT, ROLE_USER, Turn  # noqa: E402

BASE = 1_760_000_000.0
DAY = "2025-10-06"


def make_turns() -> list[Turn]:
    return [
        Turn(ts=BASE, text="第一句", role=ROLE_USER, speaker="主人", source="test"),
        Turn(ts=BASE + 30, text="第二句", role=ROLE_ASSISTANT, speaker="猫娘", source="test"),
        Turn(ts=BASE + 60, text="第三句", role=ROLE_USER, speaker="主人", source="test"),
        Turn(ts=BASE + 3600 * 5, text="第四句", role=ROLE_ASSISTANT, speaker="猫娘", source="test"),
        Turn(ts=BASE + 3600 * 5 + 30, text="第五句", role=ROLE_USER, speaker="主人", source="test"),
    ]


class ScreeningLogicTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod.ChronicleConfig()
        self.turns = make_turns()
        self.scenes = screening.build_scenes(self.turns, self.cfg.scene_gap_minutes * 60)

    def test_default_session_is_at_the_beginning(self):
        session = screening.default_session(DAY, len(self.turns))
        self.assertEqual(session["status"], screening.STATUS_PAUSED)
        self.assertEqual(session["cursor"], 0)
        self.assertEqual(session["cat_cursor"], 0)
        self.assertEqual(session["stop_at"], 0)

    def test_scene_boundaries_match_the_gap(self):
        self.assertEqual(len(self.scenes), 2)
        self.assertEqual(self.scenes[0]["start_index"], 1)
        self.assertEqual(self.scenes[0]["end_index"], 3)
        self.assertEqual(self.scenes[1]["start_index"], 4)
        self.assertEqual(self.scenes[1]["end_index"], 5)

    def test_advance_line_moves_both_cursors(self):
        session = screening.default_session(DAY, len(self.turns))
        session, changed = screening.advance(session, len(self.turns), self.scenes)
        self.assertTrue(changed)
        self.assertEqual(session["cursor"], 1)
        self.assertEqual(session["cat_cursor"], 1)
        self.assertEqual(session["status"], screening.STATUS_PLAYING)

    def test_scene_advance_reaches_scene_end(self):
        session = screening.default_session(DAY, len(self.turns))
        session, changed = screening.advance(
            session, len(self.turns), self.scenes, mode="scene"
        )
        self.assertTrue(changed)
        self.assertEqual(session["cursor"], 3)

    def test_planned_stop_pauses_at_boundary(self):
        session = screening.default_session(DAY, len(self.turns))
        session["cursor"] = 1
        session["cat_cursor"] = 1
        session["status"] = screening.STATUS_PLAYING
        session["stop_at"] = 2
        session, changed = screening.advance(
            session, len(self.turns), self.scenes, count=5
        )
        self.assertTrue(changed)
        self.assertEqual(session["cursor"], 2)
        self.assertEqual(session["status"], screening.STATUS_PAUSED)
        self.assertEqual(session["last_pause_reason"], "planned_stop")

    def test_resume_after_planned_stop_extends_the_plan(self):
        session = screening.default_session(DAY, len(self.turns))
        session["cursor"] = 2
        session["cat_cursor"] = 2
        session["stop_at"] = 2
        session["status"] = screening.STATUS_PAUSED
        session, changed = screening.resume(session, len(self.turns), driver="big")
        self.assertTrue(changed)
        self.assertEqual(session["stop_at"], 0)
        self.assertEqual(session["driver"], "big")
        self.assertEqual(session["status"], screening.STATUS_PLAYING)

    def test_comment_from_catgirl_pauses_playback(self):
        session = screening.default_session(DAY, len(self.turns))
        session["status"] = screening.STATUS_PLAYING
        session["cursor"] = 2
        session["cat_cursor"] = 2
        session, changed = screening.comment(
            session, len(self.turns), "这里我记得喵～", index=2, who="catgirl"
        )
        self.assertTrue(changed)
        self.assertEqual(session["status"], screening.STATUS_PAUSED)
        self.assertEqual(session["last_pause_reason"], "catgirl_comment")
        self.assertEqual(session["marks"][-1]["kind"], "comment")
        self.assertEqual(session["marks"][-1]["who"], "catgirl")

    def test_pause_sets_stop_and_mark(self):
        session = screening.default_session(DAY, len(self.turns))
        session["status"] = screening.STATUS_PLAYING
        session["cursor"] = 3
        session = screening.pause(
            session, len(self.turns), index=3, stop_at=3, note="今天就看到这"
        )
        self.assertEqual(session["status"], screening.STATUS_PAUSED)
        self.assertEqual(session["stop_at"], 3)
        self.assertEqual(session["marks"][-1]["kind"], "pause")
        self.assertEqual(session["marks"][-1]["text"], "今天就看到这")

    def test_seek_pauses_and_marks(self):
        session = screening.default_session(DAY, len(self.turns))
        session, changed = screening.seek(
            session, len(self.turns), self.scenes, 4, who="catgirl"
        )
        self.assertTrue(changed)
        self.assertEqual(session["cursor"], 4)
        self.assertEqual(session["cat_cursor"], 4)
        self.assertEqual(session["status"], screening.STATUS_PAUSED)
        self.assertEqual(session["marks"][-1]["kind"], "seek")

    def test_auto_advance_ignores_other_driver(self):
        session = screening.default_session(DAY, len(self.turns))
        session["status"] = screening.STATUS_PLAYING
        session["driver"] = "mini"
        session, changed = screening.advance(
            session, len(self.turns), self.scenes, auto=True, driver="big"
        )
        self.assertFalse(changed)
        self.assertEqual(session["driver"], "mini")

    def test_normalize_keeps_covers_and_reviews(self):
        raw = {
            "covers": {"1": "cover.png", "2": "../bad.png", "x": "ignored.png"},
            "reviews": [
                {
                    "text": "这一段我记得。\n\n下次还想看。",
                    "watched_to": 3,
                    "total": 5,
                    "source": "local",
                    "warning": "模型不可用",
                }
            ],
        }
        session = screening.normalize_session(raw, DAY, len(self.turns))
        self.assertEqual(session["covers"], {"1": "cover.png"})
        self.assertEqual(len(session["reviews"]), 1)
        self.assertEqual(session["reviews"][0]["watched_to"], 3)
        self.assertEqual(session["reviews"][0]["source"], "local")

    def test_add_review_keeps_latest_twenty(self):
        session = screening.default_session(DAY, len(self.turns))
        for index in range(25):
            session, changed = screening.add_review(
                session,
                f"第 {index} 篇",
                watched_to=2,
                total=5,
            )
            self.assertTrue(changed)
        self.assertEqual(len(session["reviews"]), screening.MAX_REVIEWS)
        self.assertEqual(session["reviews"][-1]["text"], "第 24 篇")

    def test_local_review_and_markdown(self):
        stats = diary.analyze_day(DAY, self.turns, self.cfg)
        text = screening_review.local_review(
            DAY, self.turns, {}, self.cfg, stats, watched_to=3
        )
        self.assertIn("主人", text)
        self.assertIn(screening_review.LOCAL_MARK, text)
        review = {
            "text": text,
            "watched_to": 3,
            "total": len(self.turns),
            "created_at": 1760000000,
            "source": "local",
            "warning": "模型不可用",
        }
        markdown = screening_review.review_markdown(DAY, review, self.cfg)
        self.assertIn("放映厅观后感", markdown)
        self.assertIn("本地整理", markdown)
        self.assertIn("看到第 3 / 5 条", markdown)

    def test_normalize_clamps_and_keeps_marks(self):
        raw = {
            "status": "unknown",
            "cursor": 99,
            "cat_cursor": -1,
            "stop_at": 99,
            "marks": [
                {"index": 1, "who": "catgirl", "kind": "comment", "text": "喵"},
                {"index": 2, "who": "ghost", "kind": "unknown", "text": ""},
            ],
        }
        session = screening.normalize_session(raw, DAY, len(self.turns))
        self.assertEqual(session["status"], screening.STATUS_PAUSED)
        self.assertEqual(session["cursor"], len(self.turns))
        self.assertEqual(session["cat_cursor"], 0)
        self.assertEqual(len(session["marks"]), 1)
        self.assertEqual(session["marks"][0]["who"], "catgirl")


class ScreeningStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ".tmp-tests" / f"screening-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.cfg = config_mod.ChronicleConfig()
        self.store = store_mod.ChronicleStore(self.root, self.cfg)
        self.store.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_roundtrip(self):
        session = screening.default_session(DAY, 5)
        session["cursor"] = 3
        session["marks"] = [{"id": "x", "at": 1.0, "index": 3, "who": "catgirl", "kind": "comment", "text": "喵"}]
        self.store.save_screening(DAY, session)
        loaded = self.store.load_screening(DAY)
        self.assertEqual(loaded["cursor"], 3)
        self.assertEqual(loaded["marks"][0]["text"], "喵")
        self.assertIn(DAY, self.store.list_screening_days())

    def test_missing_file_is_empty(self):
        self.assertEqual(self.store.load_screening("1999-01-01"), {})


if __name__ == "__main__":
    unittest.main()
