"""每日备忘录与章节标题：按天存进章节目录，支持"只追加新增部分"、可修改。

文件放 ``chapters/`` 目录，和当天章节并列：

* ``备忘录-<日期>.md``   —— 抓取到的"先前对话的备忘录"新增段，逐条追加
* ``章节标题-<日期>.txt`` —— 可覆盖的今日章节标题（留空则自动从备忘录取随机一条）
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from .store import ChronicleStore

MEMO_PREFIX = "备忘录-"
TITLE_PREFIX = "章节标题-"
MEMO_SUFFIX = ".md"
TITLE_SUFFIX = ".txt"
TIDY_MARK_SUFFIX = ".tidied.json"

#: 宿主 ``recent.json`` 里 system 备忘录的标志语
SYSTEM_MEMO_MARK = "先前对话的备忘录"

#: 自动标题的最大长度（截断到一行，避免标题过长）
TITLE_MAX_CHARS = 24


def memo_path(store: ChronicleStore, day: str) -> Path:
    return store.chapters_dir / f"{MEMO_PREFIX}{day}{MEMO_SUFFIX}"


def title_path(store: ChronicleStore, day: str) -> Path:
    return store.chapters_dir / f"{TITLE_PREFIX}{day}{TITLE_SUFFIX}"


def tidy_mark_path(store: ChronicleStore, day: str) -> Path:
    return store.chapters_dir / f"{MEMO_PREFIX}{day}{TIDY_MARK_SUFFIX}"


def read(store: ChronicleStore, day: str) -> str:
    """读当天备忘录的已存内容（空则 ``""``）。"""

    return store.read_text(memo_path(store, day)) or ""


def write(store: ChronicleStore, day: str, text: str) -> None:
    """整体写回（界面"保存"时用；会覆盖）。写入后旧的"已整理"标记失效。"""

    with store._lock:
        store._atomic_write(memo_path(store, day), text)
    clear_tidy_mark(store, day)


def read_tidy_mark(store: ChronicleStore, day: str) -> str:
    """读当天最近一次"整理备忘录"的时间；没有标记返回 ``""``。"""

    raw = (store.read_text(tidy_mark_path(store, day)) or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(data, dict):
        return str(data.get("tidied_at") or "").strip()
    return ""


def write_tidy_mark(store: ChronicleStore, day: str, tidied_at: str) -> None:
    with store._lock:
        store._atomic_write(
            tidy_mark_path(store, day),
            json.dumps({"tidied_at": tidied_at}, ensure_ascii=False),
        )


def clear_tidy_mark(store: ChronicleStore, day: str) -> None:
    with store._lock:
        try:
            tidy_mark_path(store, day).unlink(missing_ok=True)
        except OSError:
            pass


def append_new(store: ChronicleStore, day: str, new_text: str, *, mode: str = "tail") -> dict:
    """只把**新增部分**追加到当天备忘录尾部；已有内容不修改。

    判定逻辑（按 ``mode`` 选择）：

    * ``tail``（默认）——**末段锚点定位**：以已有内容最后一段作为锚点，
      在新内容里定位该锚点，把锚点之后新增的段落追加进去。这样宿主对
      备忘录**整体重写**（条目顺序/措辞微调）时也不会重复堆积，只抓真正新出现
      的部分；找不到锚点（宿主彻底重写）才整段追加、保留旧的。
    * ``line``——逐行去重：只追加已有内容里没有的新行。
    * ``append``——旧行为：新内容以已有内容开头就追加尾段，否则整段追加。

    返回 ``{"added": bool, "tail": str, ...}``。
    """

    new_text = (new_text or "").strip()
    if not new_text:
        return {"added": False, "reason": "empty"}
    existing = read(store, day).strip()

    if not existing:
        write(store, day, new_text)
        return {"added": True, "tail": new_text}

    if mode == "append":
        return _append_prefix(store, day, existing, new_text)

    if mode == "line":
        return _append_lines(store, day, existing, new_text)

    return _append_tail_anchor(store, day, existing, new_text)


def _append_tail_anchor(
    store: ChronicleStore, day: str, existing: str, new_text: str
) -> dict:
    """末段锚点定位：以已有最后一段为锚，只追加其后新增的段落。"""

    def segments(text: str) -> list[str]:
        return [seg.strip() for seg in re.split(r"\n\s*\n|\n", text) if seg.strip()]

    old_segs = segments(existing)
    new_segs = segments(new_text)
    if not old_segs or not new_segs:
        return {"added": False, "reason": "empty"}

    anchor = old_segs[-1]
    needle = anchor if len(anchor) >= 4 else existing
    idx = new_text.find(needle)
    if idx < 0:
        write(store, day, existing + "\n" + new_text)
        return {"added": True, "tail": new_text, "appended_full": True}

    tail = new_text[idx + len(needle):].strip()
    if not tail:
        return {"added": False, "reason": "unchanged"}
    write(store, day, existing + "\n" + tail)
    return {"added": True, "tail": tail, "appended_tail": True}


def _append_lines(
    store: ChronicleStore, day: str, existing: str, new_text: str
) -> dict:
    """逐行去重：只追加已有内容里没有的新行。"""

    old_lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
    old_set = set(old_lines)
    fresh = [ln.strip() for ln in new_text.splitlines() if ln.strip() and ln.strip() not in old_set]
    if not fresh:
        return {"added": False, "reason": "unchanged"}
    write(store, day, existing + "\n" + "\n".join(fresh))
    return {"added": True, "tail": "\n".join(fresh), "appended_lines": True}


def _append_prefix(
    store: ChronicleStore, day: str, existing: str, new_text: str
) -> dict:
    """旧行为：新内容以已有内容开头 → 追加尾段；前缀不一致 → 整段追加。"""

    if new_text.startswith(existing):
        tail = new_text[len(existing):].strip()
        if not tail:
            return {"added": False, "reason": "unchanged"}
        write(store, day, existing + "\n" + tail)
        return {"added": True, "tail": tail, "appended_tail": True}
    write(store, day, existing + "\n" + new_text)
    return {"added": True, "tail": new_text, "appended_full": True}


def read_title(store: ChronicleStore, day: str) -> str:
    return (store.read_text(title_path(store, day)) or "").strip()


def write_title(store: ChronicleStore, day: str, text: str) -> None:
    with store._lock:
        store._atomic_write(title_path(store, day), (text or "").strip())


def _compact(line: str, limit: int = TITLE_MAX_CHARS) -> str:
    line = " ".join(str(line).split())
    if len(line) > limit:
        return line[:limit].rstrip("，。；、,. ") + "…"
    return line


def random_title(store: ChronicleStore, day: str, *, default: str = "") -> str:
    """从当天备忘录取一条随机非空行当标题；没有则返回 ``default``。"""

    text = read(store, day)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return default
    return _compact(random.choice(lines))


def effective_title(
    store: ChronicleStore,
    day: str,
    *,
    auto_from_memo: bool,
    default: str,
) -> str:
    """今日章节标题：用户设置的非空则优先；否则按配置从备忘录随机取；再否则默认。"""

    manual = read_title(store, day)
    if manual:
        return manual
    if auto_from_memo:
        picked = random_title(store, day)
        if picked:
            return picked
    return default


__all__ = [
    "MEMO_PREFIX",
    "MEMO_SUFFIX",
    "SYSTEM_MEMO_MARK",
    "TIDY_MARK_SUFFIX",
    "TITLE_MAX_CHARS",
    "TITLE_PREFIX",
    "TITLE_SUFFIX",
    "append_new",
    "clear_tidy_mark",
    "effective_title",
    "memo_path",
    "random_title",
    "read",
    "read_title",
    "read_tidy_mark",
    "tidy_mark_path",
    "title_path",
    "write",
    "write_tidy_mark",
    "write_title",
]