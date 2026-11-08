"""用真实代码生成一份示例章节与日记，放到 examples/ 下。

用途：
* 让阅读 README 的人直接看到产出物长什么样，而不用先装插件；
* 改动章节/日记排版后，可以用它对比效果。

    python tools/make_demo.py
"""

from __future__ import annotations

import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 这里必须用 importlib：先设置 sys.path 再 import 会触发 E402，而发布检查禁止 noqa。
chapter = importlib.import_module("chronicle.chapter")
config_mod = importlib.import_module("chronicle.config")
diary = importlib.import_module("chronicle.diary")
Turn = importlib.import_module("chronicle.models").Turn

DAY = "2026-01-01"
BASE_TS = 1_767_225_600.0  # 2026-01-01 08:00 UTC

DEMO_TURNS = [
    Turn(ts=BASE_TS, text="早安呀，今天想出去走走", role="user", speaker="主人"),
    Turn(ts=BASE_TS + 40, text="早安～那我们去河边吧，今天有太阳", role="assistant", speaker="喵喵"),
    Turn(ts=BASE_TS + 4_000, text="河边的风好大，但是很舒服", role="user", speaker="主人"),
    Turn(ts=BASE_TS + 4_060, text="嘿嘿，我给你拍了照片！今天真开心", role="assistant", speaker="喵喵"),
    Turn(ts=BASE_TS + 26_000, text="有点累了，回家吧", role="user", speaker="主人"),
    Turn(ts=BASE_TS + 26_080, text="好，回去给你煮热奶茶，明天再出来", role="assistant", speaker="喵喵"),
]


def main() -> int:
    config = config_mod.ChronicleConfig.from_mapping(
        {"chronicle": {"master_name": "主人", "catgirl_name": "喵喵"}}
    )
    out_dir = ROOT / "examples"
    (out_dir / "chapters").mkdir(parents=True, exist_ok=True)
    (out_dir / "diary").mkdir(parents=True, exist_ok=True)

    chapter_text = chapter.build_chapter(DAY, DEMO_TURNS, config, index=1)
    (out_dir / "chapters" / f"{DAY}.md").write_text(chapter_text, encoding="utf-8")

    diary_text, info = diary.compose_diary(DAY, DEMO_TURNS, config)
    (out_dir / "diary" / f"{DAY}.md").write_text(diary_text, encoding="utf-8")

    print(f"章节：examples/chapters/{DAY}.md（{len(chapter_text)} 字）")
    print(f"日记：examples/diary/{DAY}.md（{len(diary_text)} 字，引擎 {info['engine']}，心情 {info['mood_label']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
