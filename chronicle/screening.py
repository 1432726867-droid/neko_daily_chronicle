"""放映厅：和猫娘一起回看某一天对话的纯逻辑。

这里只做状态机，不读写文件、不调用模型：

* ``cursor``：银幕当前放到第几条（0 表示还没开始）；
* ``cat_cursor``：猫娘自己已经看到第几条；
* ``stop_at``：她给这一场选的终点（0 表示不设终点）；
* ``status``：``playing`` / ``paused`` / ``ended``；
* ``driver``：当前由哪个窗口负责自动推进，避免大窗和小窗重复播放。

放映厅不新增任何对话事实，只保存“看到哪、停在哪、留了什么话”。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Mapping, Sequence

from . import chapter as chapter_mod
from .models import Turn, numbered_turns

SCREENING_VERSION = 1
STATUS_PLAYING = "playing"
STATUS_PAUSED = "paused"
STATUS_ENDED = "ended"
STATUSES = (STATUS_PLAYING, STATUS_PAUSED, STATUS_ENDED)
MODES = ("line", "scene")
WHO_VALUES = ("master", "catgirl")
MARK_KINDS = ("pause", "auto_pause", "comment", "seek")
MAX_MARKS = 500
MARK_TEXT_LIMIT = 300
SCREENING_TEXT_LIMIT = 800
MAX_REVIEWS = 20
REVIEW_TEXT_LIMIT = 4000


def _moment(now: float | None = None) -> float:
    return round(float(now) if now is not None else time.time(), 3)


def _clamp(value: Any, total: int) -> int:
    if total <= 0:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 0:
        number = 0
    if number > total:
        number = total
    return number


def _clean_text(value: Any, limit: int = MARK_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if limit and len(text) > limit:
        text = text[:limit] + "……"
    return text


def _clean_filename(value: Any) -> str:
    """只接受图片文件名，拒绝路径分隔符与上跳目录。"""

    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        return ""
    return text[:200]


def _clean_review_text(value: Any, limit: int = REVIEW_TEXT_LIMIT) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if limit and len(text) > limit:
        text = text[:limit] + "……"
    return text


def default_session(day: str, total: int = 0, *, now: float | None = None) -> dict:
    """新建一场放映。默认停在开头，等主人点「开始放映」。"""

    moment = _moment(now)
    return {
        "version": SCREENING_VERSION,
        "day": str(day or ""),
        "total": max(0, int(total)),
        "status": STATUS_PAUSED,
        "mode": "line",
        "cursor": 0,
        "cat_cursor": 0,
        "stop_at": 0,
        "driver": "",
        "last_pause_reason": "",
        "last_pause_note": "",
        "created_at": moment,
        "updated_at": moment,
        "marks": [],
        # 场景封面：{场景号: 图片文件名}；不存图片本体。
        "covers": {},
        # 观后感：每写一次追加一条，保留历史，不覆盖。
        "reviews": [],
    }


def normalize_session(
    data: Mapping[str, Any] | None,
    day: str,
    total: int,
    *,
    now: float | None = None,
    max_marks: int = MAX_MARKS,
) -> dict:
    """把磁盘上的放映状态规整成固定结构；损坏时回落到新场次。"""

    if not isinstance(data, Mapping):
        return default_session(day, total, now=now)

    moment = _moment(now)
    session = default_session(day, total, now=now)
    session["created_at"] = data.get("created_at") or moment
    session["updated_at"] = data.get("updated_at") or moment
    session["total"] = max(0, int(total))

    status = str(data.get("status") or "")
    session["status"] = status if status in STATUSES else STATUS_PAUSED
    mode = str(data.get("mode") or "")
    session["mode"] = mode if mode in MODES else "line"

    session["cursor"] = _clamp(data.get("cursor"), total)
    session["cat_cursor"] = _clamp(data.get("cat_cursor"), total)
    session["stop_at"] = _clamp(data.get("stop_at"), total)
    session["driver"] = _clean_text(data.get("driver"), 80)
    session["last_pause_reason"] = _clean_text(data.get("last_pause_reason"), 80)
    session["last_pause_note"] = _clean_text(data.get("last_pause_note"), 300)

    marks: list[dict] = []
    raw_marks = data.get("marks")
    if isinstance(raw_marks, list):
        for item in raw_marks:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "")
            who = str(item.get("who") or "master")
            text = _clean_text(item.get("text"))
            if not text and kind not in ("pause", "auto_pause", "seek"):
                continue
            marks.append(
                {
                    "id": str(item.get("id") or uuid.uuid4().hex[:12]),
                    "at": _moment(item.get("at")),
                    "index": _clamp(item.get("index"), total),
                    "who": who if who in WHO_VALUES else "master",
                    "kind": kind if kind in MARK_KINDS else "comment",
                    "text": text,
                }
            )
    cap = max(1, int(max_marks or MAX_MARKS))
    session["marks"] = marks[-cap:]

    covers = data.get("covers")
    clean_covers: dict[str, str] = {}
    if isinstance(covers, Mapping):
        for scene_no, filename in covers.items():
            try:
                key = str(int(scene_no))
            except (TypeError, ValueError):
                continue
            name = _clean_filename(filename)
            if key and name:
                clean_covers[key] = name
    session["covers"] = clean_covers

    reviews: list[dict] = []
    raw_reviews = data.get("reviews")
    if isinstance(raw_reviews, list):
        for item in raw_reviews:
            if not isinstance(item, Mapping):
                continue
            text = _clean_review_text(item.get("text"))
            if not text:
                continue
            reviews.append(
                {
                    "id": str(item.get("id") or uuid.uuid4().hex[:12]),
                    "created_at": _moment(item.get("created_at")),
                    "watched_to": _clamp(item.get("watched_to"), total),
                    "total": max(0, int(total)),
                    "text": text,
                    "source": _clean_text(item.get("source"), 20) or "llm",
                    "model": _clean_text(item.get("model"), 120),
                    "warning": _clean_text(item.get("warning"), 300),
                }
            )
    session["reviews"] = reviews[-MAX_REVIEWS:]
    return session


def build_scenes(turns: Sequence[Turn], gap_seconds: float) -> list[dict]:
    """把一天切成场景，边界与章节完全一致。"""

    numbered = numbered_turns(turns)
    if not numbered:
        return []
    positions = {id(turn): index for index, turn in numbered}
    groups = chapter_mod.scene_groups([turn for _, turn in numbered], gap_seconds)
    scenes: list[dict] = []
    for no, group in enumerate(groups, start=1):
        head = group[0]
        tail = group[-1]
        scenes.append(
            {
                "no": no,
                "label": f"{chapter_mod.scene_label(head.ts)} · {head.clock}",
                "start_index": positions.get(id(head), 0),
                "end_index": positions.get(id(tail), 0),
                "count": len(group),
                "start_clock": head.clock,
                "end_clock": tail.clock,
                "start_ts": round(float(head.ts), 3),
                "end_ts": round(float(tail.ts), 3),
            }
        )
    return scenes


def _scene_for_index(scenes: Sequence[Mapping], index: int) -> Mapping | None:
    for scene in scenes:
        if int(scene.get("start_index") or 0) <= index <= int(scene.get("end_index") or 0):
            return scene
    return scenes[-1] if scenes else None


def _next_scene_target(scenes: Sequence[Mapping], cursor: int, total: int) -> int:
    """下一段的终点：当前未看的第一句所在场景的结尾。"""

    next_index = cursor + 1
    for scene in scenes:
        if int(scene.get("start_index") or 0) <= next_index <= int(scene.get("end_index") or 0):
            return int(scene.get("end_index") or 0)
    return total


def _previous_scene_target(scenes: Sequence[Mapping], cursor: int) -> int:
    """上一段的起点：优先回到当前场景开头，否则回到上一场景开头。"""

    current = _scene_for_index(scenes, cursor)
    if current is not None:
        start = int(current.get("start_index") or 0)
        if cursor > start:
            return start
        no = int(current.get("no") or 1)
        for scene in scenes:
            if int(scene.get("no") or 0) == no - 1:
                return int(scene.get("start_index") or 0)
    return 1


def progress(session: Mapping, total: int, scenes: Sequence[Mapping]) -> dict:
    """给界面/工具用的进度摘要。"""

    cursor = _clamp(session.get("cursor"), total)
    cat_cursor = _clamp(session.get("cat_cursor"), total)
    stop_at = _clamp(session.get("stop_at"), total)
    scene = _scene_for_index(scenes, cursor) if scenes else None
    percent = round((cursor / total) * 100, 1) if total else 0.0
    return {
        "cursor": cursor,
        "cat_cursor": cat_cursor,
        "stop_at": stop_at,
        "planned_stop": stop_at or total,
        "status": str(session.get("status") or STATUS_PAUSED),
        "mode": str(session.get("mode") or "line"),
        "driver": str(session.get("driver") or ""),
        "scene_no": int(scene.get("no") or 0) if scene else 0,
        "scene_label": str(scene.get("label") or "") if scene else "",
        "percent": percent,
        "remaining": max(0, total - cursor),
        "next_index": min(total, cursor + 1) if total else 0,
        "resume_from": min(total, cat_cursor + 1) if total else 0,
        "at_end": bool(total and cursor >= total),
        "last_pause_reason": str(session.get("last_pause_reason") or ""),
        "last_pause_note": str(session.get("last_pause_note") or ""),
    }


def _append_mark(
    session: dict,
    *,
    index: int,
    who: str,
    kind: str,
    text: str = "",
    now: float | None = None,
    max_marks: int = MAX_MARKS,
) -> None:
    marks = session.get("marks")
    if not isinstance(marks, list):
        marks = []
        session["marks"] = marks
    marks.append(
        {
            "id": uuid.uuid4().hex[:12],
            "at": _moment(now),
            "index": max(0, int(index)),
            "who": who if who in WHO_VALUES else "master",
            "kind": kind if kind in MARK_KINDS else "comment",
            "text": _clean_text(text),
        }
    )
    cap = max(1, int(max_marks or MAX_MARKS))
    if len(marks) > cap:
        del marks[:-cap]


def advance(
    session: dict,
    total: int,
    scenes: Sequence[Mapping],
    *,
    to: int | None = None,
    count: int = 1,
    mode: str = "line",
    who: str = "master",
    auto: bool = False,
    driver: str = "",
    now: float | None = None,
) -> tuple[dict, bool]:
    """推进放映。返回 ``(session, changed)``。

    ``auto=True`` 是窗口自动播放的心跳：只有状态为 playing、且 driver 匹配时才推进；
    猫娘开口后后端会把状态改成 paused，下一次心跳自然会被忽略。
    """

    if total <= 0:
        session["total"] = 0
        session["status"] = STATUS_ENDED
        return session, False
    if auto:
        if str(session.get("status") or "") != STATUS_PLAYING:
            return session, False
        current_driver = str(session.get("driver") or "")
        if current_driver and driver and current_driver != driver:
            return session, False

    cursor = _clamp(session.get("cursor"), total)
    if to is not None:
        target = _clamp(to, total)
    elif str(mode or "") == "scene":
        target = _next_scene_target(scenes, cursor, total)
    else:
        target = min(total, cursor + max(1, int(count or 1)))

    stop_at = _clamp(session.get("stop_at"), total)
    if stop_at and target > stop_at:
        target = stop_at

    if target <= cursor:
        if auto and driver:
            session["driver"] = driver
        return session, False

    session["cursor"] = target
    session["cat_cursor"] = max(_clamp(session.get("cat_cursor"), total), target)
    if driver:
        session["driver"] = _clean_text(driver, 80)
    session["updated_at"] = _moment(now)

    if target >= total:
        session["status"] = STATUS_ENDED
        session["last_pause_reason"] = "end"
        session["last_pause_note"] = ""
    elif stop_at and target >= stop_at:
        session["status"] = STATUS_PAUSED
        session["last_pause_reason"] = "planned_stop"
        session["last_pause_note"] = f"说好看到第 {stop_at} 条就停"
    else:
        session["status"] = STATUS_PLAYING
        session["last_pause_reason"] = ""
        session["last_pause_note"] = ""
    return session, True


def seek(
    session: dict,
    total: int,
    scenes: Sequence[Mapping],
    index: int,
    *,
    who: str = "master",
    driver: str = "",
    now: float | None = None,
) -> tuple[dict, bool]:
    """跳到某一条并暂停。"""

    target = _clamp(index, total)
    previous = _clamp(session.get("cursor"), total)
    session["cursor"] = target
    session["cat_cursor"] = target
    session["status"] = STATUS_PAUSED
    session["last_pause_reason"] = "seek"
    session["last_pause_note"] = ""
    session["updated_at"] = _moment(now)
    if driver:
        session["driver"] = _clean_text(driver, 80)
    _append_mark(session, index=target, who=who, kind="seek", now=now)
    return session, target != previous


def previous_scene(
    session: dict,
    total: int,
    scenes: Sequence[Mapping],
    *,
    who: str = "master",
    driver: str = "",
    now: float | None = None,
) -> tuple[dict, bool]:
    cursor = _clamp(session.get("cursor"), total)
    return seek(
        session,
        total,
        scenes,
        _previous_scene_target(scenes, cursor),
        who=who,
        driver=driver,
        now=now,
    )


def pause(
    session: dict,
    total: int,
    *,
    index: int | None = None,
    note: str = "",
    stop_at: int | None = None,
    who: str = "master",
    reason: str = "manual",
    now: float | None = None,
    add_mark: bool = True,
) -> dict:
    """暂停放映。``index`` 提供时表示“就看到这一条”。"""

    if index is not None:
        target = _clamp(index, total)
        session["cursor"] = target
        session["cat_cursor"] = target
    if stop_at is not None:
        session["stop_at"] = _clamp(stop_at, total)
    session["status"] = STATUS_PAUSED
    session["last_pause_reason"] = _clean_text(reason, 80)
    session["last_pause_note"] = _clean_text(note)
    session["updated_at"] = _moment(now)
    if add_mark:
        _append_mark(
            session,
            index=_clamp(session.get("cursor"), total),
            who=who,
            kind="auto_pause" if reason == "catgirl_spoke" else "pause",
            text=note,
            now=now,
        )
    return session


def resume(
    session: dict,
    total: int,
    *,
    driver: str = "",
    restart: bool = False,
    now: float | None = None,
) -> tuple[dict, bool]:
    """继续放映；``restart=True`` 从头开始（保留已经写过的手记）。"""

    if restart:
        session["cursor"] = 0
        session["cat_cursor"] = 0
        session["stop_at"] = 0
        session["last_pause_reason"] = ""
        session["last_pause_note"] = ""
    if total <= 0:
        session["status"] = STATUS_ENDED
        session["total"] = 0
        return session, False

    cursor = _clamp(session.get("cursor"), total)
    if cursor >= total:
        session["status"] = STATUS_ENDED
        return session, False
    stop_at = _clamp(session.get("stop_at"), total)
    # 已经到达上一场约定的终点：继续就等于把终点顺延，否则会立刻再次暂停。
    if stop_at and cursor >= stop_at:
        session["stop_at"] = 0
    if cursor < _clamp(session.get("cat_cursor"), total):
        session["cursor"] = _clamp(session.get("cat_cursor"), total)

    session["status"] = STATUS_PLAYING
    session["last_pause_reason"] = ""
    session["last_pause_note"] = ""
    session["driver"] = _clean_text(driver, 80)
    session["updated_at"] = _moment(now)
    return session, True


def comment(
    session: dict,
    total: int,
    text: str,
    *,
    index: int | None = None,
    who: str = "catgirl",
    auto_pause: bool = True,
    now: float | None = None,
) -> tuple[dict, bool]:
    """留一句放映手记；猫娘说话时默认把放映停下来。"""

    body = _clean_text(text)
    if not body:
        return session, False
    target = _clamp(index if index is not None else session.get("cursor"), total)
    _append_mark(
        session, index=target, who=who, kind="comment", text=body, now=now
    )
    session["updated_at"] = _moment(now)
    if auto_pause and str(session.get("status") or "") == STATUS_PLAYING:
        session["status"] = STATUS_PAUSED
        session["last_pause_reason"] = (
            "catgirl_comment" if who == "catgirl" else "master_comment"
        )
        session["last_pause_note"] = body
    return session, True


def add_review(
    session: dict,
    text: str,
    *,
    watched_to: int,
    total: int,
    source: str = "llm",
    model: str = "",
    warning: str = "",
    now: float | None = None,
) -> tuple[dict, bool]:
    """追加一条观后感；历史保留，最多保留最近 ``MAX_REVIEWS`` 条。"""

    body = _clean_review_text(text)
    if not body:
        return session, False
    reviews = session.get("reviews")
    if not isinstance(reviews, list):
        reviews = []
        session["reviews"] = reviews
    reviews.append(
        {
            "id": uuid.uuid4().hex[:12],
            "created_at": _moment(now),
            "watched_to": _clamp(watched_to, total),
            "total": max(0, int(total)),
            "text": body,
            "source": _clean_text(source, 20) or "llm",
            "model": _clean_text(model, 120),
            "warning": _clean_text(warning, 300),
        }
    )
    del reviews[:-MAX_REVIEWS]
    session["updated_at"] = _moment(now)
    return session, True


def finish(session: dict, *, now: float | None = None) -> dict:
    """结束这一场（不会删除任何手记）。"""

    session["status"] = STATUS_ENDED
    session["last_pause_reason"] = "finished"
    session["updated_at"] = _moment(now)
    return session


__all__ = [
    "MAX_MARKS",
    "MAX_REVIEWS",
    "MARK_TEXT_LIMIT",
    "REVIEW_TEXT_LIMIT",
    "MODES",
    "SCREENING_TEXT_LIMIT",
    "SCREENING_VERSION",
    "STATUSES",
    "STATUS_ENDED",
    "STATUS_PAUSED",
    "STATUS_PLAYING",
    "add_review",
    "advance",
    "build_scenes",
    "comment",
    "default_session",
    "finish",
    "normalize_session",
    "pause",
    "previous_scene",
    "progress",
    "resume",
    "seek",
]
