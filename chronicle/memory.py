"""收藏清单（回忆录的底层状态）。

只保存"收藏了哪几条对话"，不生成可读的回忆录文档——回忆录是界面里的视图，
勾选状态落盘到 ``data/memories.json``，免得重启/重载插件后丢失。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from .store import ChronicleStore

MEMORY_FILE_NAME = "memories.json"


class MemoryStore:
    """把收藏清单读写到 ``data/memories.json``。"""

    def __init__(self, store: ChronicleStore) -> None:
        self.store = store
        self.path: Path = store.root / MEMORY_FILE_NAME

    # ── 读写 ────────────────────────────────────────────────────────

    def _load_raw(self) -> dict:
        text = self.store.read_text(self.path)
        if not text:
            return {"items": []}
        try:
            data = json.loads(text)
        except ValueError:
            return {"items": []}
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {"items": []}
        return data

    def _save(self, data: dict) -> None:
        data["updated_at"] = time.time()
        text = json.dumps(data, ensure_ascii=False, indent=2)
        with self.store._lock:
            self.store._atomic_write(self.path, text)

    # ── 查询 ────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        """全部收藏，新的在前。"""

        items = self._load_raw().get("items", [])
        items.sort(key=lambda item: float(item.get("saved_at", 0)), reverse=True)
        return items

    def has(self, day: str, index: int) -> bool:
        return any(
            item.get("date") == day and int(item.get("index", -1)) == int(index)
            for item in self._load_raw().get("items", [])
        )

    def count(self) -> int:
        return len(self._load_raw().get("items", []))

    # ── 写 ──────────────────────────────────────────────────────────

    def toggle(self, row: Mapping[str, Any], on: bool | None = None) -> dict:
        """收藏/取消收藏一条；返回 ``{"saved": bool, "total": int}``。

        ``on`` 为 ``None`` 时按当前状态翻转，为布尔时强制收藏/取消。
        ``saved`` 表示操作完成后的收藏状态（True=已收藏，False=未收藏）。
        """

        day = str(row.get("date") or "")
        index = int(row.get("index", 0))
        if not day or index <= 0:
            raise ValueError("缺少日期或序号，无法收藏")

        # 读-改-写放在同一把锁内，避免并发翻转丢更新（store 锁是可重入的）。
        with self.store._lock:
            data = self._load_raw()
            items = data.get("items", [])
            existing = next(
                (
                    item
                    for item in items
                    if item.get("date") == day and int(item.get("index", -1)) == index
                ),
                None,
            )
            if existing is not None:
                if on is None or not on:
                    items.remove(existing)
                    self._save(data)
                    return {"saved": False, "total": len(items)}
                # 已是收藏状态且被要求收藏：幂等成功（saved 表示收藏后的状态）
                return {"saved": True, "total": len(items)}

            if on is not None and not on:
                return {"saved": False, "total": len(items)}

            items.append(
                {
                    "date": day,
                    "index": index,
                    "time": str(row.get("time") or ""),
                    "speaker": str(row.get("speaker") or ""),
                    "role": str(row.get("role") or ""),
                    "text": str(row.get("text") or ""),
                    "saved_at": time.time(),
                }
            )
            self._save(data)
            return {"saved": True, "total": len(items)}


__all__ = ["MEMORY_FILE_NAME", "MemoryStore"]