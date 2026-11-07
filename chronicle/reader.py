"""读取与编号：把记录编上序号、按序号取用、跨天检索。

三个使用者共用这里的实现，保证"序号"到处一致：

* 章节渲染（``chapter.py``）——每句前面标 ``#12``；
* 静态浏览页（``static/index.html``）——表格里的序号列；
* 给猫娘的工具（``chronicle_get_turn`` 等）——按序号取原文。

纯函数，只依赖标准库与同包模块，便于测试。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from . import diary as diary_mod
from .models import ROLE_UNKNOWN, Turn, format_clock, numbered_turns

#: 单条正文在列表类结果里的最大长度（避免一次塞爆模型上下文）。
LIST_TEXT_LIMIT = 400
#: 按序号取单条时的最大长度。
SINGLE_TEXT_LIMIT = 2000
#: 检索命中的片段长度。
SNIPPET_LIMIT = 120
#: 浏览页检索时每天最多收集多少条候选（防止某个词在单日命中上千条撑爆内存）。
BROWSE_DAY_CANDIDATES = 24


def clamp_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"……（原文 {len(text)} 字，已截断）"


def turn_row(
    index: int,
    turn: Turn,
    *,
    master_name: str = "主人",
    catgirl_name: str = "猫娘",
    text_limit: int = LIST_TEXT_LIMIT,
) -> dict:
    """一条记录的行表示（界面表格与工具返回值共用）。"""

    return {
        "index": index,
        "date": turn.day,
        "time": turn.clock,
        "timestamp": round(float(turn.ts), 3),
        "role": turn.role,
        "speaker": turn.display_speaker(master_name=master_name, catgirl_name=catgirl_name),
        "text": clamp_text(turn.text, text_limit),
        "source": turn.source,
    }


def day_rows(
    turns: Sequence[Turn],
    *,
    master_name: str = "主人",
    catgirl_name: str = "猫娘",
    start: int = 1,
    count: int = 0,
    text_limit: int = LIST_TEXT_LIMIT,
) -> list[dict]:
    """某一天的记录行，可按序号区间截取（``count<=0`` 表示到结尾）。"""

    rows = [
        turn_row(index, turn, master_name=master_name, catgirl_name=catgirl_name, text_limit=text_limit)
        for index, turn in numbered_turns(turns)
    ]
    begin = max(1, int(start or 1)) - 1
    if begin >= len(rows):
        return []
    end = len(rows) if int(count or 0) <= 0 else begin + int(count)
    return rows[begin:end]


def find_turn(turns: Sequence[Turn], index: int) -> tuple[int, Turn] | None:
    """按序号取一条（序号按时间顺序，从 1 开始）。"""

    for position, turn in numbered_turns(turns):
        if position == int(index):
            return position, turn
    return None


# ── 检索：排序、去重、抽稀 ──────────────────────────────────────────

#: 这些词出现在原话里，说明那一轮是在"讨论怎么查"，而不是"一起经历过"。
#: 命中后降权——否则猫娘一"想起"就先想起自己查过记录。
META_HINTS = (
    "插件",
    "搜索",
    "搜到",
    "搜出来",
    "查到",
    "记录",
    "编年史",
    "第几",
    "几条",
    "条目",
    "数据库",
    "系统提示",
)

#: 同一天里相邻序号的两句，文本相似度到这个值就算同一件事说了两遍。
#: 实测"替换几个字+换了动作描写"的重复句相似度在 0.6 上下，所以取 0.6 而不是 0.8。
DUPLICATE_RATIO = 0.6
#: 判定重复时容许的最大序号间隔。
DUPLICATE_GAP = 2
#: 太短的话（"第一句"这种）相似度天然偏高，不参与去重。
DUPLICATE_MIN_CHARS = 10
#: 打分权重：像"聊检索"的扣分，前后成对（一来一回）的加分，
#: 以及"整段对话都在聊怎么查"时整对下沉的扣分。
META_PENALTY = -6
PAIR_BONUS = 2
RETRIEVAL_PAIR_PENALTY = 3


def _normalized(text: str) -> str:
    return " ".join((text or "").split()).lower()


#: 这些句子本身就是在"调取记录"，不是被调取的记忆——检索时整条跳过。
#: 例：「我说过油菜花吗」「10月13号第3条」「把第13条念给我听」。
#: 不跳的话，她"想起"的会是"我们聊过要查油菜花"，而不是那片油菜花。
INVOCATION_PATTERNS = (
    re.compile(r"第\s*[0-9零〇一二三四五六七八九十百]+\s*条"),
    re.compile(r"念(?:给|一下|出来|一遍)"),
    re.compile(r"(?:说过|提过|讲过|聊过|记得|记不记得)[^。！？!?]{0,24}[吗么嘛?？]\s*$"),
)


def _is_invocation(text: str) -> bool:
    """这一句是不是"在调取某条记录"，而不是当时发生的事。"""

    body = " ".join((text or "").split())
    return any(pattern.search(body) for pattern in INVOCATION_PATTERNS)


def _is_meta(text: str) -> bool:
    """这一句是不是在"聊检索"本身，而不是在聊当时的经历。"""

    lowered = _normalized(text)
    return any(word in lowered for word in META_HINTS)


def _meta_penalty(text: str) -> int:
    lowered = _normalized(text)
    hits = sum(1 for word in META_HINTS if word in lowered)
    if not hits:
        return 0
    return META_PENALTY - 2 * (hits - 1)


def _similarity(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _normalized(left), _normalized(right)).ratio()


def _drop_near_duplicates(day_hits: Sequence[dict]) -> list[dict]:
    """同一天内相邻且几乎重复的两句只留一句（分数高的优先）。"""

    kept: list[dict] = []
    for hit in sorted(day_hits, key=lambda item: item["index"]):
        for old in kept:
            same_spot = abs(int(hit["index"]) - int(old["index"])) <= DUPLICATE_GAP
            if not same_spot:
                continue
            left, right = hit["_body"], old["_body"]
            if min(len(left), len(right)) < DUPLICATE_MIN_CHARS:
                continue
            if _similarity(left, right) >= DUPLICATE_RATIO:
                if hit["_score"] > old["_score"]:
                    kept.remove(old)
                    kept.append(hit)
                break
        else:
            kept.append(hit)
    return kept


def _pick_hits(day_hits: Sequence[dict], max_per_day: int) -> list[dict]:
    """每天只留几条：先凑"主人+猫娘"的一对，再按分数补齐。"""

    if max_per_day <= 0 or not day_hits:
        return list(day_hits)
    ordered = sorted(day_hits, key=lambda item: (-item["_score"], item["index"]))
    picked: list[dict] = [ordered[0]]
    if max_per_day > 1:
        opposite = [hit for hit in ordered[1:] if hit["role"] != picked[0]["role"]]
        if opposite:
            picked.append(opposite[0])
    if max_per_day > len(picked):
        for hit in ordered:
            if hit in picked:
                continue
            picked.append(hit)
            if len(picked) >= max_per_day:
                break
    return sorted(picked, key=lambda item: item["index"])


def _spread_across_days(by_day: dict[str, list[dict]], limit: int) -> list[dict]:
    """浏览页检索：让结果覆盖整段时间，而不是被最近几天占满。

    只按分数/日期倒序截前 ``limit`` 条时，最近十几天命中一多，10 月的旧记录
    就会被挤出结果（这正是"搜不到以前的对话"的根因）。命中日期比名额多时，
    在整段历史上等距抽日期，保证最老的日子也有代表；日期不多时按天轮转，
    每个日子至少出一条。最后仍按日期倒序展示。
    """

    pools = {
        day: _pick_hits(_drop_near_duplicates(day_hits), 0)
        for day, day_hits in by_day.items()
    }
    days = sorted(pools, reverse=True)  # 新 → 旧
    if not days:
        return []
    kept: list[dict] = []
    if len(days) > limit:
        # 命中日期太多：在整段历史上等距抽 limit 个日期，每个日期取最优的一条。
        gap = (len(days) - 1) / max(1, limit - 1) if limit > 1 else 0
        for position in range(limit if limit > 1 else 1):
            day = days[round(position * gap)]
            kept.append(pools[day][0])
    else:
        while len(kept) < limit:
            progressed = False
            for day in days:
                pool = pools[day]
                if not pool:
                    continue
                kept.append(pool.pop(0))
                progressed = True
                if len(kept) >= limit:
                    break
            if not progressed:
                break
    # 展示顺序：日期倒序，同一天按序号正序（负号配合 reverse 实现）
    kept.sort(key=lambda item: (item["date"], -int(item["index"])), reverse=True)
    return kept


def search_turns(
    days: Iterable[tuple[str, Sequence[Turn]]],
    query: str,
    *,
    limit: int = 20,
    master_name: str = "主人",
    catgirl_name: str = "猫娘",
    snippet_limit: int = SNIPPET_LIMIT,
    max_per_day: int = 0,
    skip_meta: bool = False,
    balance_days: bool = False,
) -> list[dict]:
    """在**对话原文**里检索，返回带序号与时间的命中（已排序、去重、抽稀）。

    排序不是"日期倒序"这么简单：先按"像不像一段能想起来的经历"打分
    （聊检索本身的对话降权、前后成对的话题升权），再按天、按序号排。
    同一天里相邻且几乎重复的两句只留一句。
    ``max_per_day`` > 0 时每天最多留这么多条，并优先凑成主人+猫娘的一对。
    ``skip_meta`` 为真时整条剔掉"聊检索"的句子——给猫娘用；浏览页仍看全部。
    ``balance_days`` 为真时按日期轮流取（浏览页用），避免最近几天命中太多、
    把 10 月这类更早的记录挤出 ``limit``。

    返回体只给 ``snippet``：整段原文喂回去，猫娘只会把它复述一遍。
    """

    needle = (query or "").strip().lower()
    if not needle:
        return []

    # 候选池留够排序的余量，避免"够 limit 就提前 return"把更该想起的旧日子切掉
    pool_cap = max(int(limit) * 4, 40)
    candidates: list[dict] = []
    matched: dict[str, set[int]] = {}

    for day_rank, (day, turns) in enumerate(days):
        numbers = matched.setdefault(day, set())
        day_candidates = 0
        for index, turn in numbered_turns(turns):
            if _is_invocation(turn.text):
                continue
            lowered = turn.text.lower()
            position = lowered.find(needle)
            if position < 0:
                continue
            numbers.add(index)
            # 浏览页要扫完整段窗口；同一天命中再多也只需留若干条，
            # 否则一个"的"字就能把内存和结果都挤满。
            if balance_days and day_candidates >= BROWSE_DAY_CANDIDATES:
                continue
            day_candidates += 1
            begin = max(0, position - snippet_limit // 2)
            snippet = " ".join(turn.text[begin : position + snippet_limit].split())
            candidates.append(
                {
                    "date": day,
                    "index": index,
                    "time": turn.clock,
                    "speaker": turn.display_speaker(master_name=master_name, catgirl_name=catgirl_name),
                    "role": turn.role,
                    "snippet": snippet,
                    "_body": turn.text,
                    "_day_rank": day_rank,
                    "_is_meta": _is_meta(turn.text),
                    "_score": _meta_penalty(turn.text),
                }
            )
        # 浏览页必须扫完所有日期，否则旧记录永远没机会进候选池。
        if not balance_days and len(candidates) >= pool_cap:
            break

    meta_flags = {(hit["date"], hit["index"]): hit["_is_meta"] for hit in candidates}
    kept_keys = {
        (hit["date"], hit["index"])
        for hit in candidates
        if not (skip_meta and hit["_is_meta"])
    }
    if skip_meta:
        candidates = [hit for hit in candidates if not hit["_is_meta"]]

    # 前后成对：相邻序号也命中，说明那是"一来一回"的场景，不是孤零零一句查询；
    # 但如果这一对本身就在聊"怎么查的"，整对一起下沉，别让它冒充回忆。
    for hit in candidates:
        day = hit["date"]
        numbers = matched.get(day, set())
        neighbors = [n for n in (hit["index"] - 1, hit["index"] + 1) if n in numbers]
        if any(meta_flags.get((day, n)) for n in neighbors):
            hit["_score"] -= RETRIEVAL_PAIR_PENALTY
        elif any((day, n) in kept_keys for n in neighbors):
            hit["_score"] += PAIR_BONUS

    by_day: dict[str, list[dict]] = {}
    for hit in candidates:
        by_day.setdefault(hit["date"], []).append(hit)

    if balance_days:
        kept = _spread_across_days(by_day, max(1, int(limit)))
    else:
        kept = []
        for day_hits in by_day.values():
            kept.extend(_pick_hits(_drop_near_duplicates(day_hits), max_per_day))

        kept.sort(key=lambda item: (-item["_score"], item["_day_rank"], item["index"]))
        kept = kept[: max(1, int(limit))]
    return [
        {key: value for key, value in hit.items() if not key.startswith("_")}
        for hit in kept
    ]


def day_impressions(hits: Sequence[dict]) -> list[dict]:
    """每个命中日期各留一条"最该被想起来"的原话——优先她自己说过的那句。

    只捞一个点：整段铺开她就会复述，捞一句她才像"忽然想起来"。
    """

    best: dict[str, dict] = {}
    for hit in hits:
        date = str(hit.get("date") or "")
        if not date:
            continue
        current = best.get(date)
        if current is None or (current.get("role") != "assistant" and hit.get("role") == "assistant"):
            best[date] = hit
    return [
        {
            "date": date,
            "index": hit.get("index"),
            "time": hit.get("time"),
            "speaker": hit.get("speaker"),
            "snippet": hit.get("snippet"),
        }
        for date, hit in best.items()
    ]


def day_digest(day: str, turns: Sequence[Turn], config: Any) -> dict:
    """一天的概览（句数、时间跨度、说话人、关键词、心情）——给界面与工具用。"""

    stats = diary_mod.analyze_day(day, turns, config)
    data = stats.to_dict()
    data["time_span"] = f"{stats.start_ts and format_clock(stats.start_ts)} – {stats.end_ts and format_clock(stats.end_ts)}"
    data["speaker_order"] = [name for name, _ in stats.speakers]
    return data


def render_line(row: dict, *, with_date: bool = False) -> str:
    """把一行拼成给模型看的紧凑文本，例如 ``#12 16:59 哥哥：…``。"""

    prefix = f"{row['date']} " if with_date else ""
    return f"{prefix}#{row['index']} {row['time']} {row['speaker']}：{row['text']}"


def describe_rows(rows: Sequence[dict], *, with_date: bool = False) -> str:
    return "\n".join(render_line(row, with_date=with_date) for row in rows)


__all__ = [
    "BROWSE_DAY_CANDIDATES",
    "DUPLICATE_GAP",
    "DUPLICATE_MIN_CHARS",
    "DUPLICATE_RATIO",
    "LIST_TEXT_LIMIT",
    "INVOCATION_PATTERNS",
    "META_HINTS",
    "META_PENALTY",
    "PAIR_BONUS",
    "RETRIEVAL_PAIR_PENALTY",
    "SINGLE_TEXT_LIMIT",
    "SNIPPET_LIMIT",
    "clamp_text",
    "day_digest",
    "day_impressions",
    "day_rows",
    "describe_rows",
    "find_turn",
    "render_line",
    "search_turns",
    "turn_row",
]
