"""对话轮次数据模型与文本工具。

本模块只依赖标准库：脱离 N.E.K.O 运行时也能直接单元测试。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
ROLE_UNKNOWN = "unknown"
ROLES = (ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_UNKNOWN)

#: 去重时两条同内容消息允许的时间偏差（秒）。用于吸收“实时捕获”与
#: “总线轮询补录”两条来源对同一条消息产生的时间戳差异。
DEDUPE_WINDOW_SECONDS = 30

#: 跨来源合并同一句话的时间窗口（秒）。不同来源的时间戳口径不同
#: （总线记发言时刻，宿主存储记这一轮落盘的时刻），所以要给一个更宽的窗口；
#: 只对**来源不同**的副本生效，角色真的连说两次相同的话仍然会保留。
CROSS_SOURCE_WINDOW_SECONDS = 600

_WS_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def normalize_text(text: Any) -> str:
    """压缩空白，用于去重与统计。

    只影响比较用的副本；写进章节文件的原话不做改动。
    """

    if text is None:
        return ""
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    collapsed = _WS_RE.sub(" ", raw)
    lines = [line.strip() for line in collapsed.split("\n")]
    return _MULTI_NL_RE.sub("\n\n", "\n".join(lines)).strip()


def strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text or "")


def local_day_key(ts: float | None = None) -> str:
    """把时间戳换算成本地日期的 ``YYYY-MM-DD``。"""

    moment = float(ts) if ts else time.time()
    return time.strftime("%Y-%m-%d", time.localtime(moment))


def format_clock(ts: float | None) -> str:
    if not ts:
        return "--:--"
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (OSError, ValueError):
        return "--:--"


def format_span(seconds: float) -> str:
    """把秒数写成人话：``2 小时 15 分``。"""

    total = int(max(0.0, float(seconds)))
    if total < 60:
        return f"{total} 秒"
    minutes, _ = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def coerce_timestamp(value: Any, fallback: float | None = None) -> float:
    """把宿主可能给出的各种时间表示统一成 unix 秒。"""

    now = time.time() if fallback is None else float(fallback)
    if value is None or value == "":
        return now
    if isinstance(value, bool):
        return now
    if isinstance(value, (int, float)):
        number = float(value)
        # 毫秒时间戳（13 位）换算回秒
        if number > 1e11:
            number /= 1000.0
        return number if number > 0 else now
    text = str(value).strip()
    if not text:
        return now
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        if number > 1e11:
            number /= 1000.0
        return number if number > 0 else now
    # ISO 8601，允许结尾的 Z
    candidate = text.replace("Z", "+00:00")
    try:
        import datetime as _dt

        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        return now
    if parsed.tzinfo is None:
        return parsed.timestamp()
    return parsed.timestamp()


@dataclass
class Turn:
    """一条对话轮次（用户或角色说的一句话）。"""

    ts: float
    text: str
    role: str = ROLE_UNKNOWN
    speaker: str = ""
    source: str = "unknown"
    turn_id: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ts = coerce_timestamp(self.ts)
        self.text = normalize_text(self.text)
        self.role = self.role if self.role in ROLES else ROLE_UNKNOWN
        self.speaker = str(self.speaker or "").strip()

    # ── 派生信息 ────────────────────────────────────────────────────

    @property
    def day(self) -> str:
        return local_day_key(self.ts)

    @property
    def clock(self) -> str:
        return format_clock(self.ts)

    def is_empty(self) -> bool:
        return not self.text

    def display_speaker(self, *, master_name: str = "主人", catgirl_name: str = "猫娘") -> str:
        if self.speaker:
            return self.speaker
        if self.role == ROLE_USER:
            return master_name
        if self.role == ROLE_ASSISTANT:
            return catgirl_name
        if self.role == ROLE_SYSTEM:
            return "系统"
        return "记录"

    # ── 去重键 ──────────────────────────────────────────────────────

    def fingerprint(self) -> str:
        """精确指纹：同一来源的同一事件重复上报时相等。"""

        if self.turn_id:
            basis = f"id\x1f{self.turn_id}"
        else:
            basis = "\x1f".join(
                [self.role, self.speaker, str(int(self.ts)), self.text]
            )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]

    def loose_key(self) -> str:
        """时间桶指纹：吸收两条来源之间的时间戳漂移，并且**不看角色/说话人**。

        同一条消息可能从 ``bus.memory``（带 ``user_message`` 类型）和
        ``bus.conversations``（不带角色）各来一次，只看内容的指纹才能把它们
        合成一条；否则同一句话会被记两遍。
        """

        bucket = int(self.ts // DEDUPE_WINDOW_SECONDS)
        basis = "\x1f".join([str(bucket), self.text])
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]

    # ── 序列化 ──────────────────────────────────────────────────────

    def to_json(self) -> dict:
        return {
            "ts": round(float(self.ts), 3),
            "role": self.role,
            "speaker": self.speaker,
            "text": self.text,
            "source": self.source,
            "turn_id": self.turn_id,
            "meta": dict(self.meta or {}),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Turn | None":
        if not isinstance(data, Mapping):
            return None
        text = data.get("text")
        if text is None:
            return None
        meta = data.get("meta")
        return cls(
            ts=coerce_timestamp(data.get("ts")),
            text=str(text),
            role=str(data.get("role") or ROLE_UNKNOWN),
            speaker=str(data.get("speaker") or ""),
            source=str(data.get("source") or "unknown"),
            turn_id=str(data.get("turn_id") or ""),
            meta=dict(meta) if isinstance(meta, Mapping) else {},
        )


def sort_turns(turns: Iterable[Turn]) -> list[Turn]:
    """按时间排序；同一秒内保持稳定顺序。"""

    return sorted(turns, key=lambda item: (item.ts, item.source))


def visible_turns(turns: Iterable[Turn], *, include_system: bool = False) -> list[Turn]:
    """"用户视角"的轮次：去掉空文本，默认再滤掉系统/环境提示。

    章节、日记、浏览页、给猫娘的工具**必须**都走这一份，否则同一个 ``#12``
    在不同地方指的不是同一条（实测踩过：界面把环境提示算成第 1 条，章节把它滤掉，
    于是两边的序号整体错开一位）。
    """

    return [
        turn for turn in turns if turn.text and (include_system or turn.role != ROLE_SYSTEM)
    ]


def numbered_turns(turns: Iterable[Turn]) -> list[tuple[int, Turn]]:
    """按时间排序后从 1 编号，返回 ``[(序号, Turn), …]``。

    "序号"是**当天第几条记录**：章节、日记、界面表格、给猫娘调取记录的工具
    全部用这一套编号，所以用户在章节里看到 ``#12`` 就能直接让猫娘取第 12 条。
    编号按时间顺序算，因此回填更早的历史会让后面几条的编号顺延。
    """

    return list(enumerate(sort_turns(turns), start=1))


def index_map(turns: Iterable[Turn]) -> dict[int, int]:
    """``id(turn) -> 序号``，方便渲染时按对象取号。"""

    return {id(turn): index for index, turn in numbered_turns(turns)}


def dedupe_turns(turns: Iterable[Turn]) -> list[Turn]:
    """按精确指纹 + 时间桶指纹去掉重复条目，保持输入顺序。"""

    seen_exact: set[str] = set()
    seen_loose: set[str] = set()
    kept: list[Turn] = []
    for turn in turns:
        if turn.is_empty():
            continue
        exact = turn.fingerprint()
        loose = turn.loose_key()
        if exact in seen_exact or loose in seen_loose:
            continue
        seen_exact.add(exact)
        seen_loose.add(loose)
        kept.append(turn)
    return kept


def roles_compatible(first: str, second: str) -> bool:
    """两个角色标注能否视为"同一句话"：相同，或其中一边还没识别出来。

    用途：``bus.conversations`` 不带角色，宿主对话存储带角色，同一句话在两条
    来源上角色标注不同（``unknown`` vs ``user``），跨来源去重要认得出它们是一条。
    """

    return first == second or first == ROLE_UNKNOWN or second == ROLE_UNKNOWN


def speaker_order(
    turns: Iterable[Turn],
    *,
    master_name: str = "主人",
    catgirl_name: str = "猫娘",
) -> list[tuple[str, int]]:
    """按出现次数返回说话人列表（次数降序）。

    说话人字段为空时回头用角色推断的显示名（主人 / 猫娘 / 记录），
    这样"出场"一行不会出现 ``unknown`` 这种内部标识。
    """

    counts: dict[str, int] = {}
    for turn in turns:
        key = turn.speaker or turn.display_speaker(
            master_name=master_name, catgirl_name=catgirl_name
        )
        if not key:
            key = turn.role
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
