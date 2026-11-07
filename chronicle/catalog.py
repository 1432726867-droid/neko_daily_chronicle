"""拾光对话目录：命令解析、引用格式、行数据整理（纯逻辑，不碰 Tk）。"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

DEFAULT_COMMAND = "#拾光"
QUOTE_MARKER = "【拾光 · 回忆片段】"
DAY_PROMPT_MARKER = "【拾光 · 日期回顾】"
QUOTE_FOOTER = "（这是从拾光对话目录里翻出来的，想和你一起看看。）"
TOPIC_LEAD = "我翻到这一段，想和你聊聊："
TOPIC_FOOTER = "（不用马上接话，看到就好。）"
QUOTE_TEMPLATES = ("quote", "topic")
MAX_SEND_LINES = 16
MAX_SEND_CHARS = 2400
MAX_LINE_CHARS = 320


def mark_query(text: str, query: str, *, limit: int = 80) -> str:
    """在按钮/列表里用 ⟦ ⟧ 标出命中的关键词；不改变原文内容。"""

    body = " ".join(str(text or "").split())
    needle = str(query or "").strip()
    if not needle:
        return body[:limit] + ("…" if len(body) > limit else "")
    lowered = body.casefold()
    needle_lower = needle.casefold()
    position = lowered.find(needle_lower)
    if position < 0:
        return body[:limit] + ("…" if len(body) > limit else "")
    begin = max(0, position - max(10, limit // 2))
    end = min(len(body), position + len(needle) + max(10, limit // 2))
    snippet = body[begin:end]
    local = snippet.casefold().find(needle_lower)
    if local >= 0:
        snippet = (
            snippet[:local] + "⟦" + snippet[local:local + len(needle)] + "⟧" +
            snippet[local + len(needle):]
        )
    if begin > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet += "…"
    return snippet


def normalize_command(text: str) -> str:
    return " ".join(str(text or "").strip().split())


#: 「打开拾光」这类自然说法里，允许出现在拾光前面的动词（长的排前面）。
COMMAND_VERBS = (
    "麻烦帮我打开", "麻烦打开", "帮我打开", "帮我开一下", "帮我开",
    "打开一下", "开启一下", "启动一下", "打开", "开启", "启动", "开一下",
)

#: 不带动词、直接说「拾光」时，只认这么短的句子，免得正常聊天里提到就误触。
COMMAND_BARE_LIMIT = 15


def parse_command(text: str, command: str = DEFAULT_COMMAND) -> str | None:
    """返回命令后面的搜索词；不匹配时返回 ``None``。

    除了配置里的 ``#拾光``，也认「打开拾光」「拾光 蛋糕」这两种自然说法，
    只在消息开头匹配，普通聊天里提到拾光不会误触。
    """

    body = normalize_command(text)
    if not body:
        return None
    prefix = normalize_command(command) or DEFAULT_COMMAND
    core = prefix.lstrip("#").strip() or DEFAULT_COMMAND.lstrip("#")
    lowered = body.casefold()
    if lowered.startswith(prefix.casefold()):
        return body[len(prefix):].lstrip(" ：:，,、-—")
    for verb in COMMAND_VERBS:
        if lowered.startswith(verb):
            rest = body[len(verb):].lstrip(" ：:，,、-—")
            if rest.casefold().startswith(core.casefold()):
                return rest[len(core):].lstrip(" ：:，,、-—")
    if lowered.startswith(core.casefold()) and len(body) <= COMMAND_BARE_LIMIT:
        return body[len(core):].lstrip(" ：:，,、-—")
    return None


def is_catalog_quote(text: str) -> bool:
    return normalize_command(text).startswith(QUOTE_MARKER)


def is_catalog_injection(text: str) -> bool:
    """拾光送进聊天的引用/日期引子：不再写回原始记录，避免重复。"""

    body = normalize_command(text)
    return body.startswith(QUOTE_MARKER) or body.startswith(DAY_PROMPT_MARKER)


def _clean_line(text: Any, limit: int = MAX_LINE_CHARS) -> str:
    line = " ".join(str(text or "").split())
    if limit and len(line) > limit:
        line = line[:limit] + "……"
    return line


def _sort_rows(rows: Iterable[dict]) -> list[dict]:
    return sorted(
        (row for row in rows if isinstance(row, dict)),
        key=lambda item: (str(item.get("date") or ""), int(item.get("index") or 0)),
    )


def format_quote(
    rows: Sequence[dict],
    *,
    master_name: str = "主人",
    catgirl_name: str = "猫娘",
    template: str = "quote",
    lead: str | None = None,
    footer: str | None = None,
) -> str:
    """把选中的对话行拼成送入聊天的一段引用。

    ``lead`` / ``footer`` 留空时用内置文案（引用模板只有结尾，话题模板有开头和结尾）；
    传空字符串表示"不要这一段"，方便在配置里把多余的提示语关掉。
    """

    ordered = _sort_rows(rows)
    if not ordered:
        return ""
    mode = template if template in QUOTE_TEMPLATES else "quote"
    if lead is None:
        lead = TOPIC_LEAD if mode == "topic" else ""
    if footer is None:
        footer = TOPIC_FOOTER if mode == "topic" else QUOTE_FOOTER
    kept: list[str] = []
    used_chars = len(QUOTE_MARKER) + len(footer) + len(lead) + 32
    previous = ""
    for row in ordered:
        text = _clean_line(row.get("text") or row.get("snippet"))
        if not text:
            continue
        role = str(row.get("role") or "")
        speaker = str(row.get("speaker") or (catgirl_name if role == "assistant" else master_name))
        date = str(row.get("date") or "")
        clock = str(row.get("time") or "")
        line = f"{date} {clock} {speaker}：{text}".strip()
        if line == previous:
            continue
        if len(kept) >= MAX_SEND_LINES or used_chars + len(line) > MAX_SEND_CHARS:
            break
        kept.append(line)
        previous = line
        used_chars += len(line) + 1
    if not kept:
        return ""
    lines = [QUOTE_MARKER]
    if lead:
        lines.append(lead)
    lines.extend(kept)
    omitted = len(ordered) - len(kept)
    if omitted > 0:
        lines.append(f"（后面还有 {omitted} 条没有放进来。）")
    lines.append(footer)
    return "\n".join(lines)


def format_day_prompt(day: str, *, turns: int = 0) -> str:
    """只选中一天时送入聊天的引子。"""

    suffix = f"（这一天有 {turns} 句）" if turns else ""
    return (
        f"{DAY_PROMPT_MARKER}\n"
        f"我想回顾 {day} 这一天的对话{suffix}，陪我一起看看好吗？"
    )


def row_key(row: dict) -> str:
    return f"{row.get('date') or ''}|{int(row.get('index') or 0)}|{row.get('time') or ''}"


def summarize_day_rows(rows: Sequence[dict]) -> str:
    if not rows:
        return "这一天没有记录。"
    first = rows[0]
    last = rows[-1]
    return (
        f"{first.get('date') or ''} 共 {len(rows)} 句，"
        f"从 {first.get('time') or '--:--'} 到 {last.get('time') or '--:--'}。\n"
        "双击日期行可以打开这一天的对话；也可以直接搜索关键词。"
    )


__all__ = [
    "DAY_PROMPT_MARKER",
    "DEFAULT_COMMAND",
    "MAX_SEND_CHARS",
    "MAX_SEND_LINES",
    "QUOTE_FOOTER",
    "QUOTE_MARKER",
    "QUOTE_TEMPLATES",
    "format_day_prompt",
    "format_quote",
    "is_catalog_injection",
    "is_catalog_quote",
    "mark_query",
    "normalize_command",
    "parse_command",
    "row_key",
    "summarize_day_rows",
]
