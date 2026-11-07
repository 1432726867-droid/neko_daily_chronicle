"""把宿主的各种消息形态归一成 :class:`~chronicle.models.Turn`。

宿主的消息来源至少有四种形态，字段名并不统一：

1. ``@message`` 处理器收到的 kwargs；
2. ``ctx.bus.memory`` 里的 ``SdkBusMemoryRecord``（``.payload`` / ``.raw`` / 裸 dict）；
3. ``ctx.bus.conversations`` 的只读快照（本轮对话轮次）；
4. 插件自己或用户手动补录的纯 dict。

本模块只做"尽量取到 text / speaker / 时间 / 角色"，取不到就跳过，
绝不抛异常——采集失败不应该影响宿主对话本身。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .models import (
    CROSS_SOURCE_WINDOW_SECONDS,
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_UNKNOWN,
    ROLE_USER,
    Turn,
    coerce_timestamp,
    local_day_key,
    normalize_text,
    roles_compatible,
)

TEXT_KEYS = (
    "text", "content", "message", "msg", "body", "utterance",
    "transcript", "spoken_text", "translation", "reply",
)
SPEAKER_KEYS = (
    "speaker", "sender", "sender_name", "name", "character",
    "from", "display_name", "role_name", "nickname",
    "display", "speaker_name", "character_name", "nick", "user_name", "ai_name",
)
#: 只记进 meta、不当作说话人的字段。``lanlan`` 是宿主给消息标的**角色归属**
#: （哪个角色档案收到/发出），不是"谁在说话"，拿来显示会把用户的话标成角色名。
META_KEYS = ("is_voice", "turn_id", "generation", "channel", "priority", "lanlan")
TS_KEYS = ("ts", "timestamp", "_ts", "time", "created_at", "at", "occurred_at", "datetime")
ROLE_KEYS = (
    "role", "type", "kind", "event_type", "message_type",
    "sender_type", "from_type", "speaker_type", "who", "author", "origin",
)
ID_KEYS = ("turn_id", "id", "message_id", "msg_id", "uuid", "event_id", "seq")

#: 角色字段的**完全匹配**表（大小写不敏感）。
ROLE_ALIASES: dict[str, str] = {
    # 用户
    "user": ROLE_USER, "user_message": ROLE_USER, "human": ROLE_USER, "master": ROLE_USER,
    "client": ROLE_USER, "input": ROLE_USER, "please": ROLE_USER,
    "主人": ROLE_USER, "用户": ROLE_USER,
    # 角色 / 模型
    "assistant": ROLE_ASSISTANT, "ai": ROLE_ASSISTANT, "ai_message": ROLE_ASSISTANT,
    "assistant_message": ROLE_ASSISTANT, "neko": ROLE_ASSISTANT, "neko_message": ROLE_ASSISTANT,
    "bot": ROLE_ASSISTANT, "bot_message": ROLE_ASSISTANT, "character": ROLE_ASSISTANT,
    "catgirl": ROLE_ASSISTANT, "output": ROLE_ASSISTANT, "reply": ROLE_ASSISTANT,
    "response": ROLE_ASSISTANT, "model": ROLE_ASSISTANT, "model_message": ROLE_ASSISTANT,
    "猫娘": ROLE_ASSISTANT, "角色": ROLE_ASSISTANT,
    # 系统 / 提示词
    "system": ROLE_SYSTEM, "system_message": ROLE_SYSTEM, "tool": ROLE_SYSTEM,
    "tool_result": ROLE_SYSTEM, "function": ROLE_SYSTEM, "notice": ROLE_SYSTEM,
    "environment": ROLE_SYSTEM, "environment_hint": ROLE_SYSTEM, "prompt": ROLE_SYSTEM,
    "system_prompt": ROLE_SYSTEM, "instruction": ROLE_SYSTEM, "context": ROLE_SYSTEM,
    "系统": ROLE_SYSTEM, "环境提示": ROLE_SYSTEM, "提示": ROLE_SYSTEM,
}

#: 组合型标识只做**包含匹配**（例如 ``user_message_v2``）。
#: 这里刻意只收"带词缀"的长标识：像 ``ai`` / ``in`` 这种短词做包含匹配会把
#: ``main_logic.core`` 这类来源字段误判成角色。
ROLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("user_message", ROLE_USER),
    ("user_turn", ROLE_USER),
    ("human_message", ROLE_USER),
    ("ai_message", ROLE_ASSISTANT),
    ("assistant_message", ROLE_ASSISTANT),
    ("neko_message", ROLE_ASSISTANT),
    ("bot_message", ROLE_ASSISTANT),
    ("model_message", ROLE_ASSISTANT),
    ("system_message", ROLE_SYSTEM),
    ("tool_result", ROLE_SYSTEM),
    ("tool_call", ROLE_SYSTEM),
    ("environment_hint", ROLE_SYSTEM),
    ("system_prompt", ROLE_SYSTEM),
)

#: 宿主往对话流里塞的提示词包裹标记。这类内容不是"对话"，而是喂给模型的指令，
#: 出现在章节/日记里会很难看，因此按系统消息处理（默认不记录）。
HOST_PROMPT_MARKERS = (
    "======以下为",
    "======以上为",
    "不要生成思考过程",
    "以下为环境提示",
)


def looks_like_host_prompt(text: str) -> bool:
    """判断一段文本是否宿主自动拼给模型的提示词，而不是角色真的说的话。"""

    if not text:
        return False
    return any(marker in text for marker in HOST_PROMPT_MARKERS)


def infer_role(value: Any, default: str = ROLE_UNKNOWN) -> str:
    """把宿主的 role/type 字段映射成三种角色之一。"""

    if value is None:
        return default
    key = str(value).strip().lower()
    if not key:
        return default
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    for pattern, role in ROLE_PATTERNS:
        if pattern in key:
            return role
    return default


def infer_role_exact(value: Any) -> str:
    """只认完全匹配——用于"扫一遍记录里所有字段值找角色"这种宽松场景。"""

    if isinstance(value, str):
        return ROLE_ALIASES.get(value.strip().lower(), ROLE_UNKNOWN)
    return ROLE_UNKNOWN


def infer_role_from_mapping(mapping: Any) -> str:
    """记录里没有明确的角色字段时，扫所有字段值找角色线索。

    只做完全匹配，避免把 ``main_logic.core`` 这类来源字段误判成角色。
    """

    if not isinstance(mapping, Mapping):
        return ROLE_UNKNOWN
    for key, value in mapping.items():
        if key in TEXT_KEYS or key in TS_KEYS or key in ID_KEYS:
            continue
        role = infer_role_exact(value)
        if role != ROLE_UNKNOWN:
            return role
    return ROLE_UNKNOWN


def _lookup(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value not in (None, ""):
                return value
    return None


def _text_from_parts(value: Any) -> str:
    """``parts=[{"type":"text","text":...}]`` 形态的文本提取。"""

    chunks: list[str] = []
    for part in value:
        if isinstance(part, Mapping):
            text = _lookup(part, ("text", "content", "value"))
            if text:
                chunks.append(str(text))
        elif isinstance(part, str):
            chunks.append(part)
    return "\n".join(chunk for chunk in chunks if chunk)


#: 只包一层的包装键：``{"payload": {...}}`` 这类形态需要剥开。
WRAPPER_KEYS = ("payload", "raw", "data", "value", "record", "item")


SELF_KEYS = (*TEXT_KEYS, *SPEAKER_KEYS, *TS_KEYS, *ROLE_KEYS, *ID_KEYS)


def _mapping_from_object(obj: Any) -> dict[str, Any]:
    """把"对象自身字段 + 内层 raw/payload 字段"合并成一个 dict。

    宿主真实的 ``bus.memory`` 记录长这样::

        {"value": MemoryRecord(type='user_message', content='…',
                               timestamp=…, raw={'type': 'user_message', …})}

    对象自己的 ``content`` 才是解析好的文本，而 ``.raw`` 里往往只有原始字段名，
    所以**对象自身优先**、``.raw`` 只补充缺失的键；直接钻进 ``.raw`` 会把
    ``content`` 弄丢。
    """

    collected: dict[str, Any] = {}

    def absorb(container: Any) -> None:
        if container is None:
            return
        if isinstance(container, Mapping):
            for key, value in container.items():
                if value not in (None, ""):
                    collected.setdefault(str(key), value)
            return
        for key in SELF_KEYS:
            if hasattr(container, key):
                value = getattr(container, key)
                if value not in (None, ""):
                    collected.setdefault(key, value)

    absorb(obj)
    for attribute in ("raw", "payload", "data"):
        nested = getattr(obj, attribute, None)
        if nested is not None and nested is not obj:
            absorb(nested)

    if "ts" not in collected and "timestamp" in collected:
        collected["ts"] = collected["timestamp"]
    return collected


#: 标量：包装键下面如果是这些，就不再当成包装往下钻。
SCALAR_TYPES = (str, bytes, bytearray, int, float, bool)


def _is_single_wrapper(mapping: Any) -> tuple[bool, Any]:
    """整条 dict 就是一个包装键（``{"value": …}``）时返回 ``(True, 内层值)``。"""

    if isinstance(mapping, Mapping) and len(mapping) == 1:
        key = next(iter(mapping))
        if key in WRAPPER_KEYS:
            inner = mapping[key]
            if inner is not None and not isinstance(inner, SCALAR_TYPES):
                return True, inner
    return False, None


def unwrap_record(record: Any, _depth: int = 0) -> Mapping[str, Any]:
    """剥掉宿主 / SDK 的包装层，拿到真正的字段。

    必须**反复**剥，一条真实的 ``bus.memory`` 记录要穿过四层才见得到内容::

        SdkBusRecord(对象)  →  .raw = {"value": MemoryRecord(…)}
                            →  MemoryRecord(对象，content/type 在属性上)

    任何一层停下来都会把 ``content`` / ``type`` 吞掉（实测：7 条用户消息因此
    全部被当成"未识别"丢掉）。所以这里对"对象 → dict"和"单键包装"两种形态
    交替处理，直到拿到一个普通映射为止。
    """

    if _depth > 6:
        return record if isinstance(record, Mapping) else {}

    raw: Any = record
    for _ in range(6):
        wrapped, inner = _is_single_wrapper(raw)
        if wrapped:
            raw = inner
            continue

        if not isinstance(raw, Mapping):
            transformed = _mapping_from_object(raw)
            if not transformed:
                return {}
            raw = transformed
            continue

        # 已经是映射，但里面可能还夹着包装键（例如对象 .raw 的键被合并进来）
        nested_key = ""
        for key in raw:
            if key in WRAPPER_KEYS:
                value = raw[key]
                if value is not None and not isinstance(value, SCALAR_TYPES):
                    nested_key = key
                    break
        if nested_key and len(raw) > 1:
            inner_mapping = unwrap_record(raw[nested_key], _depth + 1)
            if inner_mapping:
                merged = dict(inner_mapping)
                for key, value in raw.items():
                    if key != nested_key and value not in (None, ""):
                        merged.setdefault(key, value)
                raw = merged
                continue
        return raw

    return raw if isinstance(raw, Mapping) else {}


def describe_record(record: Any, *, limit: int = 120, max_fields: int = 24) -> dict[str, str]:
    """把一条记录的字段摊开成 ``{字段名: 截断后的值}``，用于排障。

    宿主版本之间总线载荷的字段名并不统一，靠猜不如把真实字段打出来。
    """

    mapping = unwrap_record(record)
    described: dict[str, str] = {}
    if isinstance(mapping, Mapping):
        for key, value in list(mapping.items())[:max_fields]:
            described[str(key)] = str(value)[:limit]
        return described

    for name in dir(record):
        if name.startswith("_"):
            continue
        if len(described) >= max_fields:
            break
        try:
            value = getattr(record, name)
        except Exception:  # noqa: BLE001 - 取不到的属性跳过
            continue
        if callable(value):
            continue
        described[name] = str(value)[:limit]
    return described


def turn_from_payload(
    payload: Any,
    *,
    source: str = "unknown",
    default_role: str = ROLE_UNKNOWN,
    default_speaker: str = "",
    fallback_ts: float | None = None,
) -> Turn | None:
    """把一个 payload 变成 Turn；拿不到文本就返回 None。"""

    if isinstance(payload, str):
        text = normalize_text(payload)
        if not text:
            return None
        return Turn(
            ts=coerce_timestamp(None, fallback_ts),
            text=text,
            role=default_role,
            speaker=default_speaker,
            source=source,
        )

    mapping = unwrap_record(payload)
    if not isinstance(mapping, Mapping):
        return None

    text_value = _lookup(mapping, TEXT_KEYS)
    if text_value is None:
        # 少数形态把文本直接放在 value 里（对象包装已在 unwrap 阶段剥掉）
        scalar = mapping.get("value")
        if isinstance(scalar, str):
            text_value = scalar
    if isinstance(text_value, (list, tuple)):
        text = _text_from_parts(text_value)
    else:
        text = "" if text_value is None else str(text_value)
    text = normalize_text(text)
    if not text:
        return None

    # 角色推断顺序：明确字段 → 提示词特征 → 扫描其他字段值 → 说话人 → 调用方默认值
    role = infer_role(_lookup(mapping, ROLE_KEYS), ROLE_UNKNOWN)
    if role == ROLE_UNKNOWN and looks_like_host_prompt(text):
        role = ROLE_SYSTEM
    if role == ROLE_UNKNOWN:
        role = infer_role_from_mapping(mapping)
    speaker = str(_lookup(mapping, SPEAKER_KEYS) or default_speaker or "").strip()
    if role == ROLE_UNKNOWN and speaker:
        # 说话人字段有时直接给出角色（例如 sender="user"）
        role = infer_role(speaker, ROLE_UNKNOWN)
    if role == ROLE_UNKNOWN:
        role = default_role
    ts_value = _lookup(mapping, TS_KEYS)
    ts = coerce_timestamp(ts_value, fallback_ts)
    turn_id = str(_lookup(mapping, ID_KEYS) or "").strip()

    meta: dict[str, Any] = {}
    for key in META_KEYS:
        if key in mapping and mapping[key] not in (None, ""):
            meta[key] = mapping[key]

    return Turn(
        ts=ts,
        text=text,
        role=role,
        speaker=speaker,
        source=source,
        turn_id=turn_id,
        meta=meta,
    )


def turns_from_records(
    records: Iterable[Any] | None,
    *,
    source: str = "unknown",
    default_role: str = ROLE_UNKNOWN,
    default_speaker: str = "",
) -> list[Turn]:
    """批量转换，跳过无法识别的记录。"""

    turns: list[Turn] = []
    if not records:
        return turns
    if isinstance(records, (str, bytes, Mapping)):
        records = [records]
    try:
        iterator = list(records)
    except TypeError:
        return turns

    for record in iterator:
        fallback = None
        timestamp_attr = getattr(record, "timestamp", None)
        if timestamp_attr:
            fallback = coerce_timestamp(timestamp_attr)
        turn = turn_from_payload(
            record,
            source=source,
            default_role=default_role,
            default_speaker=default_speaker,
            fallback_ts=fallback,
        )
        if turn is not None:
            turns.append(turn)
    return turns


def turns_from_message_kwargs(
    kwargs: Mapping[str, Any] | None,
    *,
    source: str = "message",
    default_speaker: str = "",
) -> list[Turn]:
    """处理 ``@message`` 处理器收到的 kwargs（可能带嵌套的 data/message）。"""

    if not kwargs:
        return []
    payload = dict(kwargs)
    turn = turn_from_payload(payload, source=source, default_speaker=default_speaker)
    if turn is not None:
        return [turn]

    # 嵌套形态：{"data": {...}} / {"message": {...}}
    for key in ("data", "payload", "detail", "message", "event"):
        nested = payload.get(key)
        if isinstance(nested, (Mapping, list, tuple)):
            nested_turn = turn_from_payload(
                nested, source=source, default_speaker=default_speaker
            )
            if nested_turn is not None:
                return [nested_turn]
    return []


def merge_duplicates(
    turns: Iterable[Turn],
    *,
    text_window_seconds: float = CROSS_SOURCE_WINDOW_SECONDS,
) -> list[Turn]:
    """把同一句话的多来源副本合成一条，优先保留信息更全的那条。

    两级合并：

    1. **时间桶指纹**（30 秒）——同一来源在几秒内重复上报同一句话；
    2. **同文本 + 角色相容 + 来源不同 + 时间接近**——不同来源的时间戳口径不同
       （``bus.memory`` 记发言时刻，宿主存储记这一轮落盘时刻），所以要给一个
       更宽的窗口。角色相容表示"相同或其中一边还没识别出来"（``bus.conversations``
       不带角色，宿主存储带角色，同一句话要对得上）。

    **同一来源**的重复发言不会被合并：角色真的连说两次"嗯"必须留下。
    """

    def score(turn: Turn) -> tuple:
        # 已知角色优先 → 有说话人优先 → 时间早的优先
        return (
            1 if turn.role != ROLE_UNKNOWN else 0,
            1 if turn.speaker else 0,
            -float(turn.ts),
        )

    kept: list[Turn] = []
    index_time: dict[str, int] = {}
    index_text: dict[str, list[int]] = {}

    for turn in turns:
        if turn.is_empty():
            continue

        position = index_time.get(turn.loose_key())
        if position is not None:
            kept[position] = max((kept[position], turn), key=score)
            continue

        merged = False
        for candidate_position in index_text.get(turn.text, []):
            existing = kept[candidate_position]
            if not roles_compatible(existing.role, turn.role):
                continue
            if existing.source != turn.source and abs(existing.ts - turn.ts) <= text_window_seconds:
                chosen = max((existing, turn), key=score)
                kept[candidate_position] = chosen
                index_time.setdefault(chosen.loose_key(), candidate_position)
                merged = True
                break
        if merged:
            continue

        index_time[turn.loose_key()] = len(kept)
        index_text.setdefault(turn.text, []).append(len(kept))
        kept.append(turn)

    return kept


def resolve_roles(
    turns: Iterable[Turn],
    *,
    master_name: str = "主人",
    catgirl_name: str = "猫娘",
) -> list[Turn]:
    """补齐缺失的角色，返回新的 Turn 列表（不修改输入）。

    宿主的总线并不总是给出角色：``bus.memory`` 带 ``type="user_message"``，
    但 ``bus.conversations`` 的轮次只有 ``type="conversation_turn"`` ——
    同一句话在不同总线上角色信息并不一致。因此在"角色未知"时按对话本身
    的规律推断：

    * 紧跟在系统提示词（宿主的环境提示）后面的，是角色的回应；
    * 用户说完轮到角色，角色说完轮到用户（一问一答的交替）；
    * 序列开头的未知项，按后面第一个已知角色反向推断。

    推断只作用于**展示与存储的角色标注**，原话一个字都不改；仍然无从判断的
    保持 unknown（章节里显示为"记录"）。
    """

    merged = merge_duplicates(turns)
    ordered = sorted(merged, key=lambda item: item.ts)
    if not ordered:
        return []

    forward: list[str] = []
    for turn in ordered:
        role = turn.role
        if role == ROLE_UNKNOWN:
            previous = forward[-1] if forward else ""
            if previous == ROLE_SYSTEM:
                role = ROLE_ASSISTANT
            elif previous == ROLE_USER:
                role = ROLE_ASSISTANT
            elif previous == ROLE_ASSISTANT:
                role = ROLE_USER
        forward.append(role)

    # 反向补齐开头连续无法判断的部分（例如第一条就是角色的话）
    resolved = list(forward)
    for index in range(len(resolved) - 1, -1, -1):
        if resolved[index] != ROLE_UNKNOWN:
            continue
        following = resolved[index + 1] if index + 1 < len(resolved) else ""
        if following == ROLE_ASSISTANT:
            resolved[index] = ROLE_USER
        elif following == ROLE_USER:
            resolved[index] = ROLE_ASSISTANT

    result: list[Turn] = []
    for turn, role in zip(ordered, resolved):
        if role == turn.role:
            result.append(turn)
            continue
        result.append(
            Turn(
                ts=turn.ts,
                text=turn.text,
                role=role,
                speaker=turn.speaker,
                source=turn.source,
                turn_id=turn.turn_id,
                meta=dict(turn.meta),
            )
        )
    return result


def group_by_day(turns: Iterable[Turn]) -> dict[str, list[Turn]]:
    """按本地日期分组，返回按 key 排序的 dict。"""

    grouped: dict[str, list[Turn]] = {}
    for turn in turns:
        grouped.setdefault(turn.day or local_day_key(turn.ts), []).append(turn)
    return {day: grouped[day] for day in sorted(grouped)}


__all__ = [
    "HOST_PROMPT_MARKERS",
    "META_KEYS",
    "ROLE_ALIASES",
    "ROLE_PATTERNS",
    "WRAPPER_KEYS",
    "describe_record",
    "group_by_day",
    "infer_role",
    "infer_role_exact",
    "infer_role_from_mapping",
    "looks_like_host_prompt",
    "merge_duplicates",
    "resolve_roles",
    "roles_compatible",
    "turn_from_payload",
    "turns_from_message_kwargs",
    "turns_from_records",
    "unwrap_record",
]
