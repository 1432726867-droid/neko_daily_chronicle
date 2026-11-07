"""时间胶囊：写给未来某一天的话。

设计要点：

* 一枚胶囊一个 JSON：``capsules/<id>.json``，写入后不覆盖；
* 到期前只返回元信息（作者、解锁日期、字数），**正文绝不下发**；
* 到期后（本地日期 >= unlock_day）才能读到正文；
* 作者分 ``master`` / ``catgirl``：主人可在面板/浏览页封存，猫娘可经
  ``@llm_tool`` 用自己的口吻写。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .models import local_day_key

CAPSULE_VERSION = 1
CAPSULE_MAX_CHARS = 4000
CAPSULE_SUFFIX = ".json"
CAPSULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CAPSULE_AUTHORS = ("master", "catgirl")
#: 已提醒过的胶囊 id 单独存一个文件，避免和 state.json 里的结算状态互相覆盖
NOTIFIED_FILE_NAME = "capsule_notified.json"
NOTIFIED_MAX_ITEMS = 1000


def normalize_day(value: Any) -> str:
    """把 ``YYYY-MM-DD`` / ``YYYY/MM/DD`` 收敛成标准日期；非法返回空串。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    for separator in ("-", "/", "."):
        parts = raw.split(separator)
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            try:
                return time.strftime(
                    "%Y-%m-%d",
                    time.strptime(f"{int(parts[0])}-{int(parts[1])}-{int(parts[2])}", "%Y-%m-%d"),
                )
            except (ValueError, OverflowError):
                return ""
    return ""


def _days_between(start: str, end: str) -> int:
    """两个标准日期相差多少天（end 在后为正）。"""

    try:
        first = time.strptime(start, "%Y-%m-%d")
        second = time.strptime(end, "%Y-%m-%d")
    except (ValueError, OverflowError):
        return 0
    return int((time.mktime(second) - time.mktime(first)) // 86400)


def capsule_path(store: Any, capsule_id: str) -> Path | None:
    """校验 id 后返回文件路径；非法返回 ``None``（防止路径穿越）。"""

    raw = str(capsule_id or "").strip()
    if not CAPSULE_ID_RE.match(raw):
        return None
    return Path(store.capsule_dir) / f"{raw}{CAPSULE_SUFFIX}"


def _load(store: Any, capsule_id: str) -> dict | None:
    path = capsule_path(store, capsule_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    record = dict(data)
    if not str(record.get("text") or "").strip():
        return None
    record["id"] = str(record.get("id") or capsule_id)
    return record


def _author_name(store: Any, author: str) -> str:
    config = getattr(store, "config", None)
    if author == "catgirl":
        return str(getattr(config, "catgirl_name", "") or "猫娘")
    return str(getattr(config, "master_name", "") or "主人")


def _public(record: dict, today: str, *, include_text: bool, store: Any) -> dict:
    """对外视图：未到期时正文一律不下发。"""

    unlock_day = str(record.get("unlock_day") or "")
    locked = bool(unlock_day) and unlock_day > today
    text = str(record.get("text") or "")
    author = str(record.get("author") or "master")
    item = {
        "id": str(record.get("id") or ""),
        "author": author,
        "author_name": _author_name(store, author),
        "created_day": str(record.get("created_day") or ""),
        "created_at": float(record.get("created_at") or 0),
        "unlock_day": unlock_day,
        "locked": locked,
        "days_left": max(0, _days_between(today, unlock_day)) if locked else 0,
        "chars": len(text.strip()),
    }
    if include_text and not locked:
        item["text"] = text
    return item


def write(
    store: Any,
    *,
    text: str,
    unlock_day: str,
    author: str = "master",
    now: float | None = None,
) -> tuple[dict | None, str]:
    """封存一枚胶囊。返回 ``(记录, 错误码)``；成功时错误码为空串。"""

    content = str(text or "").strip()
    if not content:
        return None, "empty_text"
    if len(content) > CAPSULE_MAX_CHARS:
        return None, "too_long"
    day = normalize_day(unlock_day)
    if not day:
        return None, "bad_date"
    today = local_day_key(now)
    if day < today:
        return None, "unlock_in_past"
    who = str(author or "master").strip()
    if who not in CAPSULE_AUTHORS:
        return None, "bad_author"

    created = float(now) if now is not None else time.time()
    capsule_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(created)) + "-" + uuid.uuid4().hex[:8]
    record = {
        "version": CAPSULE_VERSION,
        "id": capsule_id,
        "created_at": created,
        "created_day": local_day_key(created),
        "unlock_day": day,
        "author": who,
        "text": content,
    }
    path = capsule_path(store, capsule_id)
    if path is None:
        return None, "bad_id"
    with store._lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        store._atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
    return _public(record, today, include_text=True, store=store), ""


def list_all(
    store: Any,
    *,
    today: str = "",
    include_text: bool = True,
    include_locked: bool = True,
    limit: int = 0,
) -> list[dict]:
    """列出全部胶囊（按解锁日期升序）；未到期的正文不下发。"""

    today = today or local_day_key()
    directory = Path(store.capsule_dir)
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in directory.glob(f"*{CAPSULE_SUFFIX}"):
        data = _load(store, path.stem)
        if data is None:
            continue
        item = _public(data, today, include_text=include_text, store=store)
        if not include_locked and item["locked"]:
            continue
        records.append(item)
    # 已到期的按最近解锁倒序在前；未到期的按最快到期在前
    readable = sorted(
        (item for item in records if not item["locked"]),
        key=lambda item: (item["unlock_day"], item["created_at"]),
        reverse=True,
    )
    locked = sorted(
        (item for item in records if item["locked"]),
        key=lambda item: (item["unlock_day"], item["created_at"]),
    )
    records = readable + locked
    if limit and limit > 0:
        records = records[: int(limit)]
    return records


def author_counts(items: list[dict]) -> dict[str, int]:
    """按作者统计条目数：``{"master": n, "catgirl": n}``。

    主人和猫娘写的是两回事：主人自己封存的那枚不能当成「猫娘写好了」，
    所以对外返回时要给出分作者的计数。
    """

    counts = {name: 0 for name in CAPSULE_AUTHORS}
    for item in items:
        who = str(item.get("author") or "master")
        if who in counts:
            counts[who] += 1
    return counts


def read(store: Any, capsule_id: str, *, today: str = "") -> tuple[dict | None, str]:
    """读一枚胶囊。未到期返回 ``(元信息, "locked")``，正文不会带出。"""

    today = today or local_day_key()
    data = _load(store, capsule_id)
    if data is None:
        return None, "not_found"
    item = _public(data, today, include_text=True, store=store)
    if item["locked"]:
        return item, "locked"
    return item, ""


def notified_ids(store: Any) -> list[str]:
    """已经递过到期提醒的胶囊 id（按写入顺序）。"""

    path = Path(store.root) / NOTIFIED_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values = data.get("ids") if isinstance(data, Mapping) else data
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value) for value in values if value]


def save_notified_ids(store: Any, ids: list[str]) -> None:
    """写回已提醒 id；只保留最近 ``NOTIFIED_MAX_ITEMS`` 个。"""

    path = Path(store.root) / NOTIFIED_FILE_NAME
    with store._lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        store._atomic_write(
            path,
            json.dumps(
                {"ids": [str(value) for value in ids[-NOTIFIED_MAX_ITEMS:] if value]},
                ensure_ascii=False,
                indent=2,
            ),
        )


__all__ = [
    "CAPSULE_AUTHORS",
    "CAPSULE_MAX_CHARS",
    "CAPSULE_SUFFIX",
    "NOTIFIED_FILE_NAME",
    "author_counts",
    "capsule_path",
    "list_all",
    "normalize_day",
    "notified_ids",
    "read",
    "save_notified_ids",
    "write",
]
