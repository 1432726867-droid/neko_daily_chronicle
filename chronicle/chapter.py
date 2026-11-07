"""把一天的对话编排成小说式章节（纯函数，只依赖标准库）。

编排只做三件事：分场景、排时间、标说话人。**不生成任何原话之外的内容**，
唯一由程序写出的文字是斜体的编排说明（时间与间隔），因此章节可以当作
当天对话的忠实底本，而不是二次创作。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .models import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    Turn,
    format_span,
    index_map,
    speaker_order,
    strip_urls,
    visible_turns,
)

#: 单个场景最多容纳多少条消息，超过就续一段，避免标题层级失衡。
MAX_TURNS_PER_SCENE = 150

#: 场景标题里的时段名（右边界为开区间小时）。
SCENE_BUCKETS: tuple[tuple[int, str], ...] = (
    (5, "凌晨"),
    (9, "清晨"),
    (11, "上午"),
    (13, "中午"),
    (17, "下午"),
    (19, "傍晚"),
    (23, "晚上"),
    (24, "深夜"),
)

#: 需要在行首转义的 Markdown 控制字符。
_ESCAPE_LEADING = "#>-*+|`~"


def cn_number(number: int) -> str:
    """1 → 一，11 → 十一，21 → 二十一；超出 99 直接返回阿拉伯数字。"""

    digits = "零一二三四五六七八九"
    value = int(number)
    if value <= 0 or value > 99:
        return str(value)
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    prefix = "十" if tens == 1 else f"{digits[tens]}十"
    return prefix if ones == 0 else f"{prefix}{digits[ones]}"


def scene_label(ts: float) -> str:
    """时间戳 → 时段名。"""

    import time as _time

    hour = _time.localtime(ts).tm_hour
    for boundary, label in SCENE_BUCKETS:
        if hour < boundary:
            return label
    return "深夜"


def escape_line(text: str) -> str:
    """转义行首的 Markdown 控制字符，保证原话不会被渲染成结构。"""

    if not text:
        return ""
    if text[0] in _ESCAPE_LEADING:
        return "\\" + text
    return text


def _render_turn(
    turn: Turn,
    *,
    master_name: str,
    catgirl_name: str,
    max_chars: int,
    index: int = 0,
) -> str:
    """渲染一句对话；``index`` 是当天序号，写成 ``**#12 猫娘**：…``。"""

    speaker = turn.display_speaker(master_name=master_name, catgirl_name=catgirl_name)
    body = turn.text
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + "……（原文过长，已截断）"
    lines = [escape_line(line) for line in body.split("\n")]
    joined = "\n".join(lines)
    label = f"#{index} {speaker}" if index else speaker
    return f"**{label}**：{joined}"


def scene_groups(turns: Sequence[Turn], gap_seconds: float) -> list[list[Turn]]:
    """按时间间隔切分场景（放映厅与章节共用同一套切分规则）。"""

    return _scene_groups(turns, gap_seconds)


def _scene_groups(turns: Sequence[Turn], gap_seconds: float) -> list[list[Turn]]:
    """按时间间隔切分场景。gap_seconds<=0 表示整天只用一个场景。"""

    groups: list[list[Turn]] = []
    current: list[Turn] = []
    previous_ts: float | None = None
    for turn in turns:
        new_scene = False
        if previous_ts is not None:
            if gap_seconds > 0 and (turn.ts - previous_ts) >= gap_seconds:
                new_scene = True
            elif len(current) >= MAX_TURNS_PER_SCENE:
                new_scene = True
        if new_scene and current:
            groups.append(current)
            current = []
        current.append(turn)
        previous_ts = turn.ts
    if current:
        groups.append(current)
    return groups


def build_chapter(
    day: str,
    turns: Sequence[Turn],
    config: Any,
    *,
    index: int = 1,
    first_scene_index: int = 1,
    title: str | None = None,
) -> str:
    """生成某一天的章节 Markdown。

    ``index`` 是章节序号（第 N 章），``first_scene_index`` 用于让同一天的
    多个分段继续沿用小节编号。``title`` 提供时作为本章标题覆盖默认格式
    （用于"每日备忘录随机标题/用户自定义标题"）。
    """

    master = getattr(config, "master_name", "主人") or "主人"
    catgirl = getattr(config, "catgirl_name", "猫娘") or "猫娘"
    gap_minutes = getattr(config, "scene_gap_minutes", 30)
    title_format = getattr(config, "chapter_title_format", "第 {index} 章 · {date}")
    max_chars = getattr(config, "max_turn_chars", 4000)

    visible = visible_turns(turns, include_system=bool(getattr(config, "include_system", False)))

    if title:
        title = title.strip()
    else:
        try:
            title = title_format.format(index=index, date=day, chapter=index)
        except (KeyError, IndexError, ValueError):
            title = f"第 {index} 章 · {day}"

    lines: list[str] = [f"# {title}", ""]

    if not visible:
        lines.append("> 这一天没有记录到对话。")
        lines.append("")
        return "\n".join(lines)

    ordered = sorted(visible, key=lambda item: item.ts)
    start_ts = ordered[0].ts
    end_ts = ordered[-1].ts
    span = format_span(end_ts - start_ts)
    speakers = speaker_order(ordered, master_name=master, catgirl_name=catgirl)
    speaker_text = "、".join(f"{name}（{count}）" for name, count in speakers if name)

    user_count = sum(1 for t in ordered if t.role == ROLE_USER)
    assistant_count = sum(1 for t in ordered if t.role == ROLE_ASSISTANT)
    other_count = len(ordered) - user_count - assistant_count

    lines.append(f"> 共 {len(ordered)} 句 ｜ {ordered[0].clock} – {ordered[-1].clock}（{span}）")
    if speaker_text:
        lines.append(f"> 出场：{speaker_text}")
    if other_count:
        lines.append(f"> 其中未标注角色 {other_count} 句（已按原话保留）")
    lines.append("> ")
    lines.append("> 本章由 neko_daily_chronicle 自动编排：原话未做改写，斜体括号内为时间说明。")
    lines.append("> 每句前的 `#编号` 是当天第几条记录，可以直接让猫娘按编号调取原文。")
    lines.append("")

    groups = _scene_groups(ordered, gap_minutes * 60.0)
    number_of = index_map(ordered)
    scene_no = max(1, int(first_scene_index))
    previous_end: float | None = None

    for group in groups:
        head = group[0]
        label = scene_label(head.ts)
        lines.append(f"## {cn_number(scene_no)}、{label} · {head.clock}")
        lines.append("")
        if previous_end is not None and head.ts - previous_end >= 3600:
            lines.append(f"*（和上一段隔了 {format_span(head.ts - previous_end)}）*")
            lines.append("")
        for turn in group:
            lines.append(
                _render_turn(
                    turn,
                    master_name=master,
                    catgirl_name=catgirl,
                    max_chars=max_chars,
                    index=number_of.get(id(turn), 0),
                )
            )
            lines.append("")
        previous_end = group[-1].ts
        scene_no += 1

    lines.append("---")
    lines.append("")
    lines.append(f"*章节结束 ｜ 下一章见。*")
    lines.append("")
    return "\n".join(lines)


def build_index(
    entries: Sequence[dict],
    config: Any,
    *,
    kind: str = "chapter",
) -> str:
    """生成目录页（chapters/README.md 或 diary/README.md）。

    ``entries`` 的每一项形如
    ``{"day": "2026-10-06", "index": 3, "turns": 128, "mood": "开心", "note": ""}``。
    """

    label = "章节" if kind == "chapter" else "日记"
    lines = [f"# 对话编年史 · {label}目录", ""]
    if not entries:
        lines.append("还没有任何记录。")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"共 {len(entries)} 天。")
    lines.append("")
    for item in entries:
        day = item.get("day", "")
        index = item.get("index")
        turns = item.get("turns")
        suffix = f"（第 {index} 章）" if index else ""
        detail = f"{turns} 句" if turns else ""
        mood = item.get("mood") or ""
        extras = " ｜ ".join(part for part in (detail, mood) if part)
        target = f"{day}.md"
        line = f"- [{day}{suffix}]({target})"
        if extras:
            line += f" — {extras}"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def strip_for_preview(text: str, limit: int = 160) -> str:
    """给 UI / 摘要用的单行预览。"""

    flat = strip_urls(text or "").replace("\n", " ")
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


__all__ = [
    "build_chapter",
    "build_index",
    "cn_number",
    "escape_line",
    "scene_label",
    "strip_for_preview",
    "MAX_TURNS_PER_SCENE",
]
