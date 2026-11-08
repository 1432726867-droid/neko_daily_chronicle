"""日记生成：本地统计摘要 + 语言模型日记（纯函数，只依赖标准库）。

两条路径共用同一份分析结果 :class:`DayStats`：

* ``local``  —— 抽取式摘要，用词全部来自当天原话，离线可用；
* ``llm``    —— 把原始记录交给语言模型写一篇真日记，程序只负责组织
  提示词、解析回复、补上统计信息。

``compose_diary()`` 根据是否拿到模型回复决定走哪条路：给了 ``llm_raw`` 就用
模型日记，没给（例如调用方不想调模型）就回落到本地摘要。插件在 ``_build_day``
里只在拿到模型回复时才调用它；模型不可用时**不写日记**（留空等模型可用后再生成），
以免把"没调成模型"当成"本地摘要是作者的意图"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .chapter import scene_label
from .models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    Turn,
    format_span,
    numbered_turns,
    speaker_order,
    strip_urls,
    visible_turns,
)

# ── 心情 ────────────────────────────────────────────────────────────

MOODS: dict[str, dict[str, Any]] = {
    "excited": {
        "label": "兴奋",
        "emoji": "🤩",
        "words": ("兴奋", "激动", "期待", "好耶", "太棒", "哇塞", "绝了", "冲鸭", "迫不及待", "燃"),
        "emojis": "🤩😆🔥🚀✨🎉",
    },
    "happy": {
        "label": "开心",
        "emoji": "😊",
        "words": ("开心", "高兴", "哈哈", "嘻嘻", "嘿嘿", "太好了", "喜欢", "可爱", "幸福", "满足", "谢谢", "爱你", "么么", "抱抱", "棒"),
        "emojis": "😊😄😆🥰😍😂🤣❤️💕✨🎉",
    },
    "calm": {
        "label": "平静",
        "emoji": "😌",
        "words": ("还好", "平静", "安静", "慢慢", "没事", "安心", "陪", "嗯嗯", "挺好", "稳"),
        "emojis": "😌🙂☺️🌙",
    },
    "tired": {
        "label": "疲惫",
        "emoji": "😴",
        "words": ("累", "困", "睡觉", "熬夜", "疲惫", "不想动", "歇", "晚安", "休息", "乏"),
        "emojis": "😴🥱😪💤",
    },
    "anxious": {
        "label": "焦虑",
        "emoji": "😰",
        "words": ("紧张", "担心", "焦虑", "害怕", "压力", "怎么办", "不安", "慌", "怕", "来不及"),
        "emojis": "😰😥😓😖",
    },
    "sad": {
        "label": "难过",
        "emoji": "😢",
        "words": ("难过", "伤心", "哭", "委屈", "失望", "孤单", "不开心", "唉", "难受", "舍不得"),
        "emojis": "😢😭💔😞🥲",
    },
    "angry": {
        "label": "生气",
        "emoji": "😠",
        "words": ("生气", "讨厌", "好烦", "气死", "过分", "无语", "火大"),
        "emojis": "😠😡💢",
    },
}

NEUTRAL_MOOD = {"label": "一般", "emoji": "😐"}

# ── 猫娘日记的元信息（心情 / 标签 / 可见性）──────────────────────────
#: 可见性取值：``private`` = 仅自己可见，但**仍可被猫娘自己读到**
#: （与 neko_diary 插件的 private 语义一致）。
CAT_DIARY_VISIBILITIES = ("public", "private")

#: 可见性展示名。
CAT_DIARY_VISIBILITY_LABELS = {"public": "公开", "private": "仅自己可见"}


def normalize_mood(value: Any, default: str = "neutral") -> str:
    """把输入规整成合法心情键（认得出中文标签）；认不出时回落 ``default``。"""

    key = str(value or "").strip()
    if key in MOODS:
        return key
    lowered = key.lower()
    if lowered in MOODS:
        return lowered
    for mood_key, spec in MOODS.items():
        if key and key == spec["label"]:
            return mood_key
    return default if default in MOODS else "neutral"


def normalize_visibility(value: Any, default: str = "public") -> str:
    """把输入规整成 ``public`` / ``private``；认不出时回落 ``default``。"""

    key = str(value or "").strip().lower()
    if key in CAT_DIARY_VISIBILITIES:
        return key
    return default if default in CAT_DIARY_VISIBILITIES else "public"


def cat_diary_is_private(text: str | None) -> bool:
    """判断一篇猫娘日记是否标了「私密」（只看开头的元信息行）。"""

    if not text:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(">"):
            return "🔒" in line or CAT_DIARY_VISIBILITY_LABELS["private"] in line
        return False
    return False


def cat_diary_meta(*, mood: str = "", tags: Sequence[str] | None = None, visibility: str = "public") -> str:
    """把心情/标签/可见性拼成一行元信息；三项都没有时返回空串。"""

    parts: list[str] = []
    if mood:
        spec = MOODS.get(mood, NEUTRAL_MOOD)
        parts.append(f"{spec['emoji']} {spec['label']}")
    clean: list[str] = []
    for tag in tags or []:
        text = str(tag).strip()
        if text and text not in clean:
            clean.append(text)
    if clean:
        parts.append("标签：" + " · ".join(clean[:6]))
    if visibility == "private":
        parts.append("🔒 " + CAT_DIARY_VISIBILITY_LABELS["private"])
    return " ｜ ".join(parts)


# ── 关键词 ──────────────────────────────────────────────────────────

STOPWORDS_ZH = {
    "我们", "你们", "他们", "她们", "这个", "那个", "这些", "那些", "什么", "怎么", "为什么",
    "可以", "一下", "一点", "没有", "就是", "然后", "但是", "因为", "所以", "如果", "已经",
    "还是", "知道", "自己", "现在", "时候", "一起", "觉得", "感觉", "应该", "可能", "其实",
    "有点", "这样", "那么", "真的", "不是", "不会", "不能", "还有", "而且", "或者", "不过",
    "只是", "就是", "要是", "为了", "关于", "对于", "通过", "由于", "虽然", "可是", "只是",
    "一定", "非常", "特别", "比较", "好像", "似乎", "终于", "居然", "竟然", "反正", "大概",
    "今天的", "明天", "昨天", "今天", "现在", "刚刚", "马上", "一直", "总是", "经常", "偶尔",
    "可以的", "什么的", "的话", "来说", "方面", "问题", "事情", "东西", "时间", "地方",
    "什么", "多少", "怎样", "如何", "哪里", "哪个", "这种", "那种", "一次", "两次",
}

#: 单字虚词：一个候选词如果全部由这些字组成，就直接丢掉。
FUNCTION_CHARS = set("的了呢吧啊哦嗯是不我你他她它们这那个有些很就都也还而且但所以么怎样吗呀嘛喔噢哎唉吗啦哟哈呀好的在到和与跟对把被让给从把着过起来去来着")

STOPWORDS_EN = {
    "the", "and", "you", "for", "that", "this", "with", "have", "not", "but", "are", "was",
    "what", "your", "from", "they", "them", "there", "here", "will", "would", "could",
    "should", "can", "just", "now", "then", "than", "too", "very", "much", "many", "some",
    "any", "all", "one", "two", "get", "got", "let", "yes", "okay", "ok", "yeah", "well",
    "http", "https", "www", "com", "html", "png", "jpg",
}

#: 中文没有分词器可用，这里把这些"几乎只作虚词/助动词"的字当成分隔符，
#: 先切出词语片段再取 n-gram，可以避免跨词边界的垃圾候选（例如"了照片"）。
SPLIT_CHARS = set(
    "的了呢吧啊哦嗯吗呀嘛喔噢哎唉"
    "是我你他她它们这那个些"
    "很就都也还而且但如果所以么怎样"
    "在把被让给从对和与跟"
    "有没不会能想要去来说看点太地得"
)

#: 以这些字开头的候选通常是"动词 + 半个词"，不算关键词（例如"去走"）。
VERB_PREFIX_CHARS = set("去来走跑看说想要给拿做吃喝玩睡起过出回煮")

#: 完整片段（2-4 字，未跨分隔符）比片段内部的 n-gram 更可能是真词，给它加权。
WHOLE_SEGMENT_BONUS = 0.5

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{1,}")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)


def split_cjk_segments(run: str) -> list[str]:
    """按虚词把一段中文切成词语片段。"""

    segments: list[str] = []
    current: list[str] = []
    for char in run:
        if char in SPLIT_CHARS or char in FUNCTION_CHARS:
            if current:
                segments.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        segments.append("".join(current))
    return segments


def extract_keywords(
    texts: Iterable[str],
    *,
    top_n: int = 8,
    min_count: int = 2,
    stopwords_zh: set[str] | None = None,
    stopwords_en: set[str] | None = None,
) -> list[tuple[str, int]]:
    """从一堆文本里抽关键词，返回 ``[(词, 出现次数), ...]``。

    没有分词器可用，因此走"虚词切分 + n-gram"的启发式：每个候选统计的是
    **出现在多少条消息里**（文档频率），完整片段额外加权，最后去掉虚词、
    去掉互相包含的冗余项。它的目标是"让人一眼看出今天聊了什么"，不是精确分词。
    """

    stop_zh = STOPWORDS_ZH if stopwords_zh is None else stopwords_zh
    stop_en = STOPWORDS_EN if stopwords_en is None else stopwords_en
    counts: dict[str, int] = {}
    whole_counts: dict[str, int] = {}

    for raw in texts:
        if not raw:
            continue
        text = _CODE_FENCE_RE.sub(" ", str(raw))
        text = strip_urls(text)

        seen: set[str] = set()
        whole: set[str] = set()

        for run in _CJK_RUN_RE.findall(text):
            for segment in split_cjk_segments(run):
                length = len(segment)
                if length < 2:
                    continue
                if length <= 4:
                    seen.add(segment)
                    whole.add(segment)
                for size in (2, 3):
                    if length < size:
                        continue
                    for start in range(length - size + 1):
                        gram = segment[start : start + size]
                        if gram[0] in VERB_PREFIX_CHARS:
                            continue
                        seen.add(gram)

        for word in _ASCII_WORD_RE.findall(text):
            lowered = word.lower().strip("-_'")
            if len(lowered) >= 2:
                seen.add(lowered)

        def keep(term: str) -> bool:
            if term in stop_zh or term in stop_en:
                return False
            if all(char in FUNCTION_CHARS for char in term):
                return False
            if len(term) > 1 and term.isascii() and not term.isalpha():
                return False
            return True

        for term in seen:
            if keep(term):
                counts[term] = counts.get(term, 0) + 1
        for term in whole:
            if keep(term):
                whole_counts[term] = whole_counts.get(term, 0) + 1

    def weight(term: str) -> float:
        return WHOLE_SEGMENT_BONUS * whole_counts.get(term, 0) + 0.1 * (len(term) - 2)

    threshold = min_count if min_count > 0 else 1
    ranked = sorted(
        ((term, count) for term, count in counts.items() if count >= threshold),
        # 先看出现条数，再看"是不是完整词"，最后短的优先
        key=lambda item: (-item[1], -weight(item[0]), len(item[0]), item[0]),
    )

    kept: list[tuple[str, int]] = []
    for term, count in ranked:
        if any(term in chosen or chosen in term for chosen, _ in kept):
            continue
        kept.append((term, count))
        if top_n > 0 and len(kept) >= top_n:
            break
    return kept


# ── 分析 ────────────────────────────────────────────────────────────


@dataclass
class DayStats:
    """一天的统计与摘要素材。"""

    day: str
    total: int = 0
    user_turns: int = 0
    assistant_turns: int = 0
    other_turns: int = 0
    chars: int = 0
    start_ts: float = 0.0
    end_ts: float = 0.0
    mood: str = "neutral"
    mood_scores: dict[str, float] = field(default_factory=dict)
    keywords: list[tuple[str, int]] = field(default_factory=list)
    speakers: list[tuple[str, int]] = field(default_factory=list)
    highlights: list[Turn] = field(default_factory=list)
    #: 精彩片段连同它们当天的序号：``[(12, Turn), …]``
    highlight_rows: list[tuple[int, Turn]] = field(default_factory=list)
    hour_histogram: dict[int, int] = field(default_factory=dict)
    scenes: list[tuple[str, int]] = field(default_factory=list)

    @property
    def span_seconds(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def mood_label(self) -> str:
        return MOODS.get(self.mood, NEUTRAL_MOOD)["label"]

    @property
    def mood_emoji(self) -> str:
        return MOODS.get(self.mood, NEUTRAL_MOOD)["emoji"]

    def busiest_hour(self) -> tuple[int, int] | None:
        if not self.hour_histogram:
            return None
        hour, count = max(self.hour_histogram.items(), key=lambda item: (item[1], -item[0]))
        return hour, count

    def to_dict(self) -> dict:
        busiest = self.busiest_hour()
        return {
            "day": self.day,
            "total": self.total,
            "user_turns": self.user_turns,
            "assistant_turns": self.assistant_turns,
            "other_turns": self.other_turns,
            "chars": self.chars,
            "start": self.start_ts,
            "end": self.end_ts,
            "span_seconds": round(self.span_seconds, 1),
            "mood": self.mood,
            "mood_label": self.mood_label,
            "mood_emoji": self.mood_emoji,
            "keywords": [{"term": term, "count": count} for term, count in self.keywords],
            "speakers": [{"name": name, "count": count} for name, count in self.speakers],
            "busiest_hour": {"hour": busiest[0], "count": busiest[1]} if busiest else None,
            "scenes": [{"label": label, "count": count} for label, count in self.scenes],
        }


def detect_mood(texts: Sequence[str]) -> tuple[str, dict[str, float]]:
    """按词典与表情统计心情，返回 ``(mood_key, 各心情得分)``。"""

    scores: dict[str, float] = {}
    for key, spec in MOODS.items():
        score = 0.0
        for text in texts:
            if not text:
                continue
            for word in spec["words"]:
                hits = text.count(word)
                if hits:
                    score += hits * 1.0
            for char in spec["emojis"]:
                hits = text.count(char)
                if hits:
                    score += hits * 2.0
        scores[key] = score

    if not scores or max(scores.values(), default=0.0) <= 0.0:
        return "neutral", {key: 0.0 for key in MOODS}

    priority = ["excited", "happy", "calm", "sad", "anxious", "tired", "angry"]
    best = max(priority, key=lambda key: (scores.get(key, 0.0), -priority.index(key)))
    return best, scores


def score_turn(turn: Turn, keyword_terms: set[str]) -> float:
    """给一条消息打"值得回味"的分。"""

    text = turn.text
    if not text:
        return -1.0
    score = min(len(text) / 60.0, 1.5)
    hits = sum(1 for term in keyword_terms if term in text)
    score += min(hits, 4) * 1.6
    if "？" in text or "?" in text:
        score += 0.9
    if "！" in text or "!" in text:
        score += 0.4
    if turn.role == ROLE_USER:
        score += 0.6
    if len(text) < 6:
        score -= 1.2
    if _URL_ONLY_RE.match(text):
        score -= 2.0
    return score


def pick_highlights(turns: Sequence[Turn], keywords: Sequence[tuple[str, int]], *, top_n: int) -> list[Turn]:
    """挑出当天最值得回味的几条原话（按时间先后返回）。"""

    if top_n <= 0:
        return []
    terms = {term for term, _ in keywords}
    scored = [(score_turn(turn, terms), index, turn) for index, turn in enumerate(turns)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = [item for item in scored[:top_n] if item[0] > 0]
    chosen.sort(key=lambda item: item[1])
    return [item[2] for item in chosen]


def analyze_day(day: str, turns: Sequence[Turn], config: Any) -> DayStats:
    """把一天的轮次汇总成 :class:`DayStats`。"""

    include_system = bool(getattr(config, "include_system", False))
    visible = visible_turns(turns, include_system=include_system)
    stats = DayStats(day=day)
    if not visible:
        return stats

    ordered = sorted(visible, key=lambda item: item.ts)
    stats.total = len(ordered)
    stats.start_ts = ordered[0].ts
    stats.end_ts = ordered[-1].ts
    stats.chars = sum(len(t.text) for t in ordered)
    stats.user_turns = sum(1 for t in ordered if t.role == ROLE_USER)
    stats.assistant_turns = sum(1 for t in ordered if t.role == ROLE_ASSISTANT)
    stats.other_turns = stats.total - stats.user_turns - stats.assistant_turns
    stats.speakers = speaker_order(
        ordered,
        master_name=getattr(config, "master_name", "主人") or "主人",
        catgirl_name=getattr(config, "catgirl_name", "猫娘") or "猫娘",
    )

    import time as _time

    histogram: dict[int, int] = {}
    scene_counts: dict[str, int] = {}
    for turn in ordered:
        hour = _time.localtime(turn.ts).tm_hour
        histogram[hour] = histogram.get(hour, 0) + 1
        label = scene_label(turn.ts)
        scene_counts[label] = scene_counts.get(label, 0) + 1
    stats.hour_histogram = histogram
    stats.scenes = sorted(scene_counts.items(), key=lambda item: -item[1])

    texts = [t.text for t in ordered]
    min_count = 2 if stats.total >= 10 else 1
    stats.keywords = extract_keywords(
        texts,
        top_n=int(getattr(config, "keyword_top_n", 8)),
        min_count=min_count,
    )
    stats.mood, stats.mood_scores = detect_mood(texts)
    stats.highlights = pick_highlights(
        ordered, stats.keywords, top_n=int(getattr(config, "highlight_top_n", 5))
    )
    wanted = {id(turn) for turn in stats.highlights}
    stats.highlight_rows = [
        (index, turn) for index, turn in numbered_turns(ordered) if id(turn) in wanted
    ]
    return stats


# ── 渲染：本地摘要 ──────────────────────────────────────────────────


def _span_text(stats: DayStats) -> str:
    if stats.total <= 1:
        return _clock(stats.start_ts) if stats.start_ts else "—"
    return f"{_clock(stats.start_ts)} – {_clock(stats.end_ts)}（{format_span(stats.span_seconds)}）"


def _clock(ts: float) -> str:
    if not ts:
        return "--:--"
    import time as _time

    return _time.strftime("%H:%M", _time.localtime(ts))


def _speaker_summary(stats: DayStats, config: Any) -> str:
    if not stats.speakers:
        return "—"
    return " ｜ ".join(f"{name} {count}" for name, count in stats.speakers if name)


def _day_title(day: str, config: Any, *, kind: str) -> str:
    if kind == "local":
        template = "{date} · 今日回忆"
    else:
        template = str(getattr(config, "diary_title_format", "{date} · 日记"))
    try:
        return template.format(date=day, day=day)
    except (KeyError, IndexError, ValueError):
        return f"{day} · 日记"


def _plain(text: str, limit: int) -> str:
    """把原话压成一行、去 URL、截断，用于日记引用。"""

    import re as _re

    text = _re.sub(r"https?://\S+", "", text or "")
    text = " ".join(str(text).split())
    if limit and len(text) > limit:
        text = text[:limit] + "……"
    return text


def _truncate(text: str, limit: int) -> str:
    """按字数截断（中文每字计 1）。"""

    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "……"


def _first_person_narrative(stats: DayStats, config: Any) -> str:
    """用当天对话拼成猫娘第一人称的一段叙述，字数限制在配置内。

    把统计信息（时间、句数、场景、心情）和几段关键原话，以"我今天……"的
    口吻串起来——本地生成，不调用任何语言模型，稳定可复现。
    """

    master = getattr(config, "master_name", "主人") or "主人"
    limit = int(getattr(config, "diary_word_limit", 400) or 400)

    if stats.start_ts and stats.end_ts and stats.start_ts != stats.end_ts:
        span = f"{_clock(stats.start_ts)} 到 {_clock(stats.end_ts)}"
    elif stats.start_ts:
        span = _clock(stats.start_ts)
    else:
        span = "白天"

    sentences: list[str] = []
    if stats.total:
        sentences.append(f"今天我和{master}从{span}聊了{stats.total}句。")
    if stats.scenes:
        top = stats.scenes[0][0]
        sentences.append(f"我们主要在一起{top}。")

    # 关键片段：把主人与猫娘的原话用第一人称转述几句
    quotes: list[str] = []
    for _, turn in stats.highlight_rows:
        text = _plain(turn.text, 42)
        if not text:
            continue
        if turn.role == ROLE_USER:
            quotes.append(f"{master}说“{text}”")
        else:
            quotes.append(f"我说“{text}”")
        if len(quotes) >= 4:
            break
    if quotes:
        sentences.append("还记得" + "，".join(quotes) + "。")

    sentences.append(f"这一天我心里更多是{stats.mood_label}，{stats.mood_emoji}。")

    body = _truncate("".join(sentences), limit)
    return body


def render_local_diary(day: str, stats: DayStats, config: Any) -> str:
    """本地第一人称日记：以猫娘视角记当天发生的事与心情，字数限制在配置内。"""

    catgirl = getattr(config, "catgirl_name", "猫娘") or "猫娘"
    lines: list[str] = [f"# {_day_title(day, config, kind='local')}", ""]

    if stats.total == 0:
        lines.append("> 这一天没有对话记录。")
        lines.append("")
        return "\n".join(lines)

    keyword_text = " · ".join(term for term, _ in stats.keywords) or "—"
    lines.append(f"> {stats.mood_emoji} {stats.mood_label} ｜ 关键词：{keyword_text}")
    lines.append(f"> 以 {catgirl} 的第一人称写（本地生成，未调用语言模型）。")
    lines.append("> ")

    body = _first_person_narrative(stats, config)
    for para in body.split("\n"):
        lines.append(para)
    lines.append("")
    return "\n".join(lines)


# ── 渲染：语言模型日记 ──────────────────────────────────────────────


STYLE_INSTRUCTIONS = {
    "first_person": "用第一人称「我」写，写给{master}看，像睡前把今天讲给他听。",
    "third_person": "用第三人称写，像一篇旁观者的短篇记录。",
    "record": "写成简洁的编年记录，克制、客观，不抒情。",
}

_STYLE_INSTRUCTIONS_EN = {
    "first_person": "Write in the first person as {catgirl}, addressed to {master}.",
    "third_person": "Write in the third person, like a short observational record.",
    "record": "Write a terse, factual chronicle without sentiment.",
}


def build_transcript(
    turns: Sequence[Turn],
    config: Any,
    *,
    max_chars: int | None = None,
    per_turn_chars: int = 400,
) -> str:
    """把一天的轮次压成提示词用的逐句文本，超长时保留头尾。"""

    master = getattr(config, "master_name", "主人") or "主人"
    catgirl = getattr(config, "catgirl_name", "猫娘") or "猫娘"
    budget = int(max_chars or getattr(config, "llm_max_chars", 24000))

    rendered: list[str] = []
    for turn in sorted(turns, key=lambda item: item.ts):
        if not turn.text:
            continue
        speaker = turn.display_speaker(master_name=master, catgirl_name=catgirl)
        text = turn.text.replace("\n", " ")
        if per_turn_chars and len(text) > per_turn_chars:
            text = text[:per_turn_chars] + "…"
        rendered.append(f"{turn.clock} {speaker}：{text}")

    total = sum(len(line) + 1 for line in rendered)
    if total <= budget:
        return "\n".join(rendered)

    # 头尾各留一半，中间用省略提示，避免提示词无限增长
    keep_budget = max(budget // 2 - 40, 200)
    head: list[str] = []
    used = 0
    for line in rendered:
        if used + len(line) + 1 > keep_budget:
            break
        head.append(line)
        used += len(line) + 1

    tail: list[str] = []
    used = 0
    for line in reversed(rendered):
        if used + len(line) + 1 > keep_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()

    omitted = len(rendered) - len(head) - len(tail)
    return "\n".join(head + [f"……（中间省略 {omitted} 条）……"] + tail)


def build_llm_prompt(
    day: str, turns: Sequence[Turn], stats: DayStats, config: Any, *, memo: str = ""
) -> str:
    """组织给语言模型的提示词。

    ``memo`` 为"先前对话的备忘录"内容（来自 ``memo_mod.read``）。当 ``diary_source``
    为 ``memor`` 或 ``auto`` 时，把备忘录作为正文主干，原始对话作补充细节。
    """

    master = getattr(config, "master_name", "主人") or "主人"
    catgirl = getattr(config, "catgirl_name", "猫娘") or "猫娘"
    style = str(getattr(config, "diary_style", "first_person"))
    language = str(getattr(config, "diary_language", "zh-CN"))
    english = language.lower().startswith("en")
    word_limit = int(getattr(config, "diary_word_limit", 400) or 400)
    diary_source = str(getattr(config, "diary_source", "memor"))

    use_memo = bool(memo) and diary_source in ("memor", "auto")
    memo_body = (memo or "").strip()

    transcript = build_transcript(turns, config)
    keyword_text = "、".join(term for term, _ in stats.keywords[:6]) or "无"
    mood_text = f"{stats.mood_emoji} {stats.mood_label}"

    if english:
        style_text = _STYLE_INSTRUCTIONS_EN.get(style, _STYLE_INSTRUCTIONS_EN["first_person"]).format(
            master=master, catgirl=catgirl
        )
        header = [
            f"You are {catgirl}. Write a diary entry about {day} based ONLY on the transcript below.",
            style_text,
            "Rules: never invent events that are not in the transcript; do not list timestamps; "
            "do not paraphrase every line; write about feelings and memorable moments; "
            f"about {word_limit} words.",
            "Reply with EXACTLY this format, the first line must be 'Mood:':",
            "Mood: <one word>",
            "Keywords: <3-5 words, comma separated>",
            "---",
            "<the diary itself>",
            "",
            f"[stats] turns={stats.total} span={_span_text(stats)} local_mood={mood_text} local_keywords={keyword_text}",
        ]
    else:
        style_text = STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS["first_person"]).format(
            master=master, catgirl=catgirl
        )
        header = [
            f"你是「{catgirl}」，请根据下面这一天（{day}）的真实对话记录，写一篇日记。",
            style_text,
            "要求：只使用记录里出现过的事情，不要编造；不要罗列时间戳，也不要逐句复述；"
            f"写感受和印象深刻的片段；正文控制在 {word_limit} 字以内。",
            "严格按下面的格式回复，第一行必须是「心情：」：",
            "心情：<一个词，例如 开心/平静/疲惫/兴奋/焦虑/难过/生气/一般>",
            "关键词：<3-5 个词，用顿号或逗号分隔>",
            "---",
            "<日记正文>",
            "",
            f"【当天统计】共 {stats.total} 句；{_span_text(stats)}；本地心情推测 {mood_text}；本地关键词 {keyword_text}",
        ]

    lines: list[str] = list(header)
    if use_memo:
        lines.append("")
        lines.append("【先前对话的备忘录（正文主干，优先依据它）】")
        lines.append(memo_body)
    lines.append("")
    lines.append("【对话记录】")  # 原始对话作为补充细节
    lines.append(transcript)
    return "\n".join(lines)


_MOOD_LINE_RE = re.compile(r"^\s*(?:心情|情绪|mood)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_KEYWORD_LINE_RE = re.compile(r"^\s*(?:关键词|关键字|keywords?)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$")


def parse_llm_diary(raw: str, stats: DayStats) -> tuple[dict, str]:
    """解析模型回复，返回 ``(meta, 正文)``。

    找不到约定格式时，不报错：整段回复都当正文，元数据用本地统计补。
    """

    meta: dict[str, Any] = {
        "mood": stats.mood,
        "mood_label": stats.mood_label,
        "keywords": [term for term, _ in stats.keywords[:5]],
        "parsed": False,
    }
    if not raw or not raw.strip():
        return meta, ""

    text = raw.strip()
    if text.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        stripped = re.sub(r"\s*```$", "", stripped)
        text = stripped.strip()

    lines = text.split("\n")
    body_start: int | None = None
    for index, line in enumerate(lines[:12]):
        if _SEPARATOR_RE.match(line):
            body_start = index + 1
            break

    head = lines[:body_start] if body_start is not None else lines[:6]
    for line in head:
        mood_match = _MOOD_LINE_RE.match(line)
        if mood_match:
            value = mood_match.group(1).strip()
            for key, spec in MOODS.items():
                if spec["label"] in value or key in value.lower():
                    meta["mood"] = key
                    meta["mood_label"] = spec["label"]
                    break
            else:
                if value:
                    meta["mood_label"] = value
            meta["parsed"] = True
            continue
        keyword_match = _KEYWORD_LINE_RE.match(line)
        if keyword_match:
            parts = re.split(r"[,，、;；|/]+", keyword_match.group(1))
            words = [part.strip() for part in parts if part.strip()]
            if words:
                meta["keywords"] = words[:8]
            meta["parsed"] = True

    if body_start is not None:
        body = "\n".join(lines[body_start:]).strip()
    else:
        # 没有分隔线：试着丢掉开头的元数据行，其余当正文
        remaining = [line for line in lines if not (_MOOD_LINE_RE.match(line) or _KEYWORD_LINE_RE.match(line))]
        body = "\n".join(remaining).strip()
        if body:
            meta["parsed"] = meta["parsed"] or len(remaining) != len(lines)

    if not body:
        body = text
    return meta, body


def render_llm_diary(
    day: str,
    stats: DayStats,
    meta: Mapping[str, Any],
    body: str,
    config: Any,
    *,
    model: str = "",
) -> str:
    """把模型正文与统计信息合成最终的日记 Markdown。"""

    keywords = meta.get("keywords") or [term for term, _ in stats.keywords[:5]]
    mood_label = str(meta.get("mood_label") or stats.mood_label)
    mood_key = str(meta.get("mood") or stats.mood)
    mood_emoji = MOODS.get(mood_key, NEUTRAL_MOOD)["emoji"]
    keyword_text = " · ".join(str(item) for item in keywords) or "—"
    engine = f"语言模型 {'（' + model + '）' if model else ''}".strip()

    lines: list[str] = [f"# {_day_title(day, config, kind='diary')}", ""]
    lines.append(f"> {mood_emoji} {mood_label} ｜ 关键词：{keyword_text}")
    lines.append("> ")
    lines.append(f"> 由 {engine} 依据当天原话生成；原文见 [`../chapters/{day}.md`](../chapters/{day}.md)。")
    lines.append("")
    lines.append(body.strip() or "（模型没有返回正文）")
    lines.append("")

    if stats.total:
        lines.append("---")
        lines.append("")
        lines.append("### 当天数据")
        lines.append("")
        lines.append(f"- 对话 {stats.total} 句（{_speaker_summary(stats, config)}）")
        lines.append(f"- 时间跨度 {_span_text(stats)}")
        busiest = stats.busiest_hour()
        if busiest:
            lines.append(f"- 最活跃时段 {busiest[0]:02d} 点前后（{busiest[1]} 句）")
        lines.append("")
    return "\n".join(lines)


# ── 编排 ────────────────────────────────────────────────────────────


def diary_engine_choice(config: Any, *, llm_available: bool) -> str:
    """决定这一次用哪个引擎：``llm`` 或 ``local``。"""

    engine = str(getattr(config, "diary_engine", "auto"))
    if engine == "local":
        return "local"
    if engine == "llm":
        # 明确要求模型：拿不到就用本地摘要兜底，记录不中断
        return "llm" if llm_available else "local"
    return "llm" if llm_available else "local"


def compose_diary(
    day: str,
    turns: Sequence[Turn],
    config: Any,
    *,
    llm_raw: str | None = None,
    llm_model: str = "",
) -> tuple[str, dict]:
    """生成日记，返回 ``(markdown, info)``。"""

    stats = analyze_day(day, turns, config)
    engine = diary_engine_choice(config, llm_available=bool(llm_raw))
    if engine == "llm" and llm_raw:
        meta, body = parse_llm_diary(llm_raw, stats)
        markdown = render_llm_diary(day, stats, meta, body, config, model=llm_model)
        info = {
            "engine": "llm",
            "model": llm_model,
            "mood": meta.get("mood", stats.mood),
            "mood_label": meta.get("mood_label", stats.mood_label),
            "keywords": list(meta.get("keywords") or [term for term, _ in stats.keywords[:5]]),
            "parsed": bool(meta.get("parsed")),
            "stats": stats.to_dict(),
        }
        return markdown, info

    markdown = render_local_diary(day, stats, config)
    info = {
        "engine": "local",
        "model": "",
        "mood": stats.mood,
        "mood_label": stats.mood_label,
        "keywords": [term for term, _ in stats.keywords],
        "parsed": False,
        "stats": stats.to_dict(),
    }
    return markdown, info


__all__ = [
    "CAT_DIARY_VISIBILITIES",
    "CAT_DIARY_VISIBILITY_LABELS",
    "DayStats",
    "MOODS",
    "NEUTRAL_MOOD",
    "SPLIT_CHARS",
    "VERB_PREFIX_CHARS",
    "analyze_day",
    "build_llm_prompt",
    "build_transcript",
    "cat_diary_is_private",
    "cat_diary_meta",
    "compose_diary",
    "detect_mood",
    "diary_engine_choice",
    "extract_keywords",
    "normalize_mood",
    "normalize_visibility",
    "parse_llm_diary",
    "pick_highlights",
    "render_llm_diary",
    "render_local_diary",
    "split_cjk_segments",
]
