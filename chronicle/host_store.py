"""从宿主自己的对话存储读取轮次（补上总线拿不到的那一半）。

实测（N.E.K.O 0.9.0.1）：

* ``bus.memory``   只有**用户**消息（``type="user_message"``，1 小时 TTL）；
* ``bus.conversations`` 只回一份**不更新**的快照（插件启动时那几条）；
* ``bus.messages`` 为空，``@message(source="chat")`` 也不见触发；
* 但宿主把**双方完整对话**写在自己的记忆目录里：

  ``<用户数据根>\\memory\\<角色>\\recent.json``
      最近若干轮，形如 ``[{"type":"human"|"ai","data":{"content":[{"type":"text","text":"…"}]}}]``

  ``<用户数据根>\\memory\\<角色>\\time_indexed.db``
      SQLite，表 ``time_indexed_original(id, session_id, message, timestamp)``，
      ``message`` 与上面同构，``timestamp`` 是 ISO 时间（本地时区）。

所以这里把它当作**可靠来源**：优先只读 SQLite 的最新若干行（带真实时间戳），
读不到再退回 ``recent.json``。任何异常都静默降级——宿主内部结构会变，
读不到就不读，绝不影响插件其余功能。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    ROLE_ASSISTANT,
    ROLE_UNKNOWN,
    ROLE_USER,
    Turn,
    coerce_timestamp,
    normalize_text,
)

MEMORY_DIR_NAME = "memory"
RECENT_FILE_NAME = "recent.json"
DB_FILE_NAME = "time_indexed.db"
DB_TABLE = "time_indexed_original"

#: 宿主给角色回复加的 ``[20261006 Tue 17:06]`` 前缀（自己的时间标注，章节里已有时间）
HOST_STAMP_RE = re.compile(r"^\s*\[\d{8}[^\]]*\]\s*")

ROLE_MAP = {
    "human": ROLE_USER,
    "user": ROLE_USER,
    "master": ROLE_USER,
    "ai": ROLE_ASSISTANT,
    "neko": ROLE_ASSISTANT,
    "assistant": ROLE_ASSISTANT,
    "bot": ROLE_ASSISTANT,
}


def strip_host_stamp(text: str) -> str:
    """去掉宿主给回复加的 ``[YYYYMMDD …]`` 前缀。"""

    return HOST_STAMP_RE.sub("", text or "", count=1)


def parse_message_blob(blob: Any) -> tuple[str, str] | None:
    """解析 ``{"type": "human"|"ai", "data": {"content": [...]}}``，返回 ``(role, text)``。"""

    if isinstance(blob, (bytes, bytearray)):
        try:
            blob = blob.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except ValueError:
            return None
    if not isinstance(blob, dict):
        return None

    raw_role = str(blob.get("type") or blob.get("role") or "").strip().lower()
    role = ROLE_MAP.get(raw_role, ROLE_UNKNOWN)

    data = blob.get("data")
    container = data if isinstance(data, dict) else blob
    content = container.get("content")

    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                value = part.get("text")
                if value:
                    chunks.append(str(value))
            elif isinstance(part, str):
                chunks.append(part)
        text = "\n".join(chunks)
    elif isinstance(content, dict):
        value = content.get("text")
        text = str(value) if value else ""
    else:
        for key in ("text", "message", "content"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break

    text = normalize_text(strip_host_stamp(text))
    if not text:
        return None
    return role, text


# ── 定位 ────────────────────────────────────────────────────────────


def memory_root(user_data_root: Path | str | None) -> Path | None:
    if not user_data_root:
        return None
    root = Path(user_data_root) / MEMORY_DIR_NAME
    return root if root.is_dir() else None


def find_character_stores(user_data_root: Path | str | None) -> list[Path]:
    """列出可读的角色存储目录（含 recent.json 或 time_indexed.db）。"""

    root = memory_root(user_data_root)
    if root is None:
        return []
    stores = []
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        if (item / DB_FILE_NAME).is_file() or (item / RECENT_FILE_NAME).is_file():
            stores.append(item)
    return stores


#: 签名要看的文件：主库、WAL（宿主若开了 WAL，新行先写在这里）与 recent.json。
SIGNATURE_FILES = (DB_FILE_NAME, f"{DB_FILE_NAME}-wal", RECENT_FILE_NAME)


def store_activity(store: Path) -> float:
    """存储目录的最近活动时间（用于挑"当前角色"）。"""

    newest = 0.0
    for name in SIGNATURE_FILES:
        path = store / name
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def store_signature(store: Path) -> tuple[int, ...]:
    """各对话文件的 ``(mtime_ns, size)`` 平铺元组。

    补录时先比这个签名：没变就说明宿主还没写新对话，不必打开 SQLite
    再把 200 条消息解析一遍，长期运行时能省下大量空转 I/O。
    包含 WAL 文件，避免宿主开着 WAL 时主库 mtime 不更新导致漏读。
    """

    values: list[int] = []
    for name in SIGNATURE_FILES:
        try:
            stat = (store / name).stat()
        except OSError:
            values.extend((0, 0))
            continue
        values.extend((stat.st_mtime_ns, stat.st_size))
    return tuple(values)


def select_store(user_data_root: Path | str | None, wanted: str = "") -> Path | None:
    """选一个角色存储：``wanted`` 指定名字，否则取最近活跃的那个。"""

    stores = find_character_stores(user_data_root)
    if not stores:
        return None
    if wanted:
        name = str(wanted).strip()
        for store in stores:
            if store.name == name:
                return store
        return None
    return max(stores, key=store_activity)


# ── 读取 ────────────────────────────────────────────────────────────


def read_time_indexed(store: Path, *, limit: int = 200, since_ts: float | None = None) -> list[Turn]:
    """只读打开 ``time_indexed.db``，取最新 ``limit`` 条消息。"""

    path = store / DB_FILE_NAME
    if not path.is_file():
        return []
    turns: list[Turn] = []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return []
    try:
        cursor = connection.execute(
            f"SELECT message, timestamp FROM {DB_TABLE} ORDER BY CAST(id AS INTEGER) DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        rows = cursor.fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass

    for message, timestamp in reversed(rows):
        parsed = parse_message_blob(message)
        if parsed is None:
            continue
        role, text = parsed
        moment = coerce_timestamp(timestamp)
        if since_ts is not None and moment < since_ts:
            continue
        turns.append(
            Turn(ts=moment, text=text, role=role, source=f"host.store:{store.name}")
        )
    return turns


def read_recent_json(store: Path, *, limit: int = 200, now: float | None = None) -> list[Turn]:
    """读 ``recent.json``（没有时间戳，只能在读不到数据库时用）。"""

    path = store / RECENT_FILE_NAME
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []

    stamp = coerce_timestamp(None, now)
    turns: list[Turn] = []
    for item in payload[-max(1, int(limit)):]:
        parsed = parse_message_blob(item)
        if parsed is None:
            continue
        role, text = parsed
        turns.append(Turn(ts=stamp, text=text, role=role, source=f"host.store:{store.name}"))
    return turns


#: 宿主 ``recent.json`` 里 system 备忘录的标志语（同一份也定义在 memo.py）
SYSTEM_MEMO_MARK = "先前对话的备忘录"


def read_system_memo(
    user_data_root: Path | str | None, wanted: str = ""
) -> str | None:
    """读 ``<用户数据根>\\memory\\<角色>\\recent.json`` 里那条 ``system``
    "先前对话的备忘录"，返回其完整文本；没有则返回 ``None``。

    这是宿主自己生成的"先前对话的备忘录"，以 ``system`` 消息形式塞进最近上下文。
    官方插件 SDK 的总线拿不到它，只能直接读这个文件（读不到就静默跳过）。
    """

    store = select_store(user_data_root, wanted)
    if store is None:
        return None
    path = store / RECENT_FILE_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).strip().lower() != "system":
            continue
        data = item.get("data")
        content = data.get("content") if isinstance(data, dict) else item.get("content")
        if isinstance(content, list):
            texts = [part.get("text") for part in content if isinstance(part, dict)]
            text = "\n".join(t for t in texts if t)
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        if text and SYSTEM_MEMO_MARK in text:
            return text.strip()
    return None


def read_store(
    store: Path,
    *,
    limit: int = 200,
    since_ts: float | None = None,
    now: float | None = None,
) -> tuple[list[Turn], str]:
    """读一个角色存储，返回 ``(轮次, 来源说明)``。优先数据库，其次 recent.json。"""

    turns = read_time_indexed(store, limit=limit, since_ts=since_ts)
    if turns:
        return turns, "time_indexed.db"
    fallback = read_recent_json(store, limit=limit, now=now)
    if fallback:
        return fallback, "recent.json"
    return [], ""


def read_active_store(
    user_data_root: Path | str | None,
    *,
    wanted: str = "",
    limit: int = 200,
    since_ts: float | None = None,
    now: float | None = None,
) -> tuple[list[Turn], dict]:
    """读取"当前角色"的存储，返回 ``(轮次, 状态信息)``。"""

    info: dict[str, Any] = {"root": str(user_data_root or ""), "stores": [], "used": "", "source": ""}
    stores = find_character_stores(user_data_root)
    info["stores"] = [store.name for store in stores]
    store = select_store(user_data_root, wanted)
    if store is None:
        return [], info
    info["used"] = store.name
    turns, source = read_store(store, limit=limit, since_ts=since_ts, now=now)
    info["source"] = source
    info["count"] = len(turns)
    return turns, info


__all__ = [
    "DB_FILE_NAME",
    "DB_TABLE",
    "RECENT_FILE_NAME",
    "find_character_stores",
    "memory_root",
    "parse_message_blob",
    "read_active_store",
    "read_recent_json",
    "read_store",
    "read_system_memo",
    "read_time_indexed",
    "select_store",
    "store_activity",
    "store_signature",
    "strip_host_stamp",
]
