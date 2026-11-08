"""放映厅观后感：提示词、模型不可用时的本地兜底、Markdown 视图。

观后感只针对「已经看到的那一段」生成，不会把未放映的对话喂给模型，
避免剧透，也避免一次把整天内容塞进上下文。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from . import diary as diary_mod
from .models import Turn, format_clock, format_span

REVIEW_TITLE_FORMAT = "{date} · 放映厅观后感"
LOCAL_MARK = "（模型不可用，这是本地整理的版本）"


def _watched_marks(session: Any, watched_to: int, limit: int = 8) -> list[dict]:
    marks = session.get("marks") if isinstance(session, dict) else None
    if not isinstance(marks, list):
        return []
    picked = [
        item for item in marks
        if isinstance(item, dict) and int(item.get("index") or 0) <= watched_to
    ]
    return picked[-limit:]


def _mark_lines(marks: Sequence[dict], *, master_name: str, catgirl_name: str) -> str:
    lines: list[str] = []
    for item in marks:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        who = catgirl_name if item.get("who") == "catgirl" else master_name
        lines.append(f"- #{item.get('index')} {who}：{text}")
    return "\n".join(lines)


def build_prompt(
    day: str,
    watched_turns: Sequence[Turn],
    session: Any,
    config: Any,
    stats: diary_mod.DayStats,
    *,
    watched_to: int,
) -> str:
    """组织给模型的观后感提示词。"""

    master = getattr(config, "master_name", "主人") or "主人"
    catgirl = getattr(config, "catgirl_name", "猫娘") or "猫娘"
    word_limit = int(getattr(config, "screening_review_word_limit", 300) or 300)
    max_chars = min(int(getattr(config, "llm_max_chars", 24000) or 24000), 16000)
    transcript = diary_mod.build_transcript(
        watched_turns,
        config,
        max_chars=max_chars,
        per_turn_chars=300,
    )
    marks = _mark_lines(
        _watched_marks(session, watched_to),
        master_name=master,
        catgirl_name=catgirl,
    )
    span = ""
    if watched_turns:
        span = f"{format_clock(watched_turns[0].ts)} – {format_clock(watched_turns[-1].ts)}"
        span = f"{span}（{format_span(watched_turns[-1].ts - watched_turns[0].ts)}）"
    keyword_text = "、".join(term for term, _ in stats.keywords[:6]) or "无"
    mood_text = f"{stats.mood_emoji} {stats.mood_label}"

    lines = [
        f"你是「{catgirl}」，刚刚和{master}一起在放映厅回看了 {day} 的对话。",
        f"这一次看到第 {watched_to} 条（已经看到 {stats.total} 句，时间 {span or '未知'}）。",
        "请写一篇「观后感」，写给主人看。",
        "要求：",
        "1. 用第一人称、猫娘自己的口吻，可以带一点自然的语气词；",
        "2. 只依据下面已经看到的对话和放映手记，不要编造没发生过的事；",
        "3. 不要逐句复述，也不要罗列时间戳；写当时的感受、现在回看的感受、印象最深的片段；",
        f"4. 正文控制在 {word_limit} 字以内；",
        "5. 只输出正文，不要标题、不要解释、不要 Markdown 代码块。",
        "",
        f"【本地统计】看到 {stats.total} 句；心情推测 {mood_text}；关键词 {keyword_text}",
    ]
    if marks:
        lines.extend(["", "【放映手记】", marks])
    lines.extend(["", "【已经看到的对话】", transcript or "（这段没有原话记录）"])
    return "\n".join(lines)


def local_review(
    day: str,
    watched_turns: Sequence[Turn],
    session: Any,
    config: Any,
    stats: diary_mod.DayStats,
    *,
    watched_to: int,
) -> str:
    """模型不可用时的本地观后感：只复述少量原文与手记，不编造。"""

    master = getattr(config, "master_name", "主人") or "主人"
    lines: list[str] = []
    if watched_turns:
        first = watched_turns[0]
        last = watched_turns[-1]
        span = f"{format_clock(first.ts)} – {format_clock(last.ts)}"
        lines.append(
            f"今天和{master}一起在放映厅回看到了第 {watched_to} 条，从 {span} 的那些话，"
            f"一共 {stats.total} 句。"
        )
    else:
        lines.append(f"今天和{master}一起在放映厅回看了 {day}。")

    marks = _watched_marks(session, watched_to, limit=5)
    mark_texts = [
        str(item.get("text") or "").strip()
        for item in marks
        if str(item.get("text") or "").strip()
    ]
    if mark_texts:
        lines.append("放映时留下的手记里写着：" + "；".join(mark_texts) + "。")

    highlights = []
    for turn in reversed(list(watched_turns)):
        text = " ".join(turn.text.split())
        if not text:
            continue
        if len(text) > 90:
            text = text[:90] + "…"
        if text not in highlights:
            highlights.append(text)
        if len(highlights) >= 3:
            break
    if highlights:
        lines.append("我还会想起这些话：")
        for text in reversed(highlights):
            lines.append(f"「{text}」")

    if stats.keywords:
        words = "、".join(term for term, _ in stats.keywords[:5])
        lines.append(f"这一天留给我的关键词是：{words}。")
    lines.append("下次还想和主人一起，从停下来的地方继续看下去喵～")
    lines.append("")
    lines.append(LOCAL_MARK)
    return "\n".join(lines)


def strip_model_text(text: str) -> str:
    """去掉模型偶尔带出的标题、代码围栏和多余引号。"""

    body = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if body.startswith("```"):
        body = body.strip("`").strip()
        if body.startswith("markdown"):
            body = body[len("markdown"):].strip()
    for prefix in ("观后感：", "观后感:", "# 观后感", "## 观后感"):
        if body.startswith(prefix):
            body = body[len(prefix):].strip()
    if body.startswith("\"") and body.endswith("\"") and len(body) > 2:
        body = body[1:-1].strip()
    return body


def review_markdown(day: str, review: dict, config: Any) -> str:
    """把一条观后感渲染成可读的 Markdown（最新一条）。"""

    title_format = str(getattr(config, "screening_review_title_format", REVIEW_TITLE_FORMAT))
    try:
        title = title_format.format(date=day)
    except (KeyError, IndexError, ValueError):
        title = f"{day} · 放映厅观后感"
    created = time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(float(review.get("created_at") or time.time()))
    )
    watched = int(review.get("watched_to") or 0)
    total = int(review.get("total") or 0)
    source = "本地整理" if review.get("source") == "local" else "模型"
    lines = [
        f"# {title}",
        "",
        f"> 看到第 {watched} / {total} 条 ｜ {created} ｜ {source}",
    ]
    warning = str(review.get("warning") or "").strip()
    if warning:
        lines.append(f"> 说明：{warning}")
    lines.extend(["", str(review.get("text") or "").strip(), ""])
    return "\n".join(lines)


__all__ = [
    "REVIEW_TITLE_FORMAT",
    "build_prompt",
    "local_review",
    "review_markdown",
    "strip_model_text",
]
