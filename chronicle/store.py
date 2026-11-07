"""落盘、去重与检索。

目录布局（相对于插件 data 目录）::

    raw/2026-10-06.jsonl      原始记录，只追加、不修改 —— 唯一的事实来源
    chapters/2026-10-06.md    小说式章节
    chapters/README.md        章节目录
    diary/2026-10-06.md       日记总结
    diary/README.md           日记目录
    state.json                运行状态（已结算的日期等）

原始记录先行、章节与日记随后重新生成，因此改配置或换模型后可以随时
``build_all`` 重算，而不会丢掉任何原话。

所有写操作都加进程内锁：定时器各自跑在独立线程里（官方文档明确说明），
``@message`` 处理器也可能和它们并发，因此不能假设单线程。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .chapter import build_index
from .diary import (
    CAT_DIARY_VISIBILITY_LABELS,
    MOODS,
    NEUTRAL_MOOD,
    normalize_mood,
    normalize_visibility,
)
from .models import (
    CROSS_SOURCE_WINDOW_SECONDS,
    Turn,
    dedupe_turns,
    local_day_key,
    roles_compatible,
    sort_turns,
)

DAY_FILE_SUFFIX = ".jsonl"
MARKDOWN_SUFFIX = ".md"

#: 按天缓存（轮次/去重指纹/条数）最多保留多少天。超出就淘汰最久未用的那天，
#: 避免常年运行后把所有日期的全文一直留在内存里；被淘汰后下次读取会重新解析文件，
#: 结果不变，只是慢一点。
DAY_CACHE_LIMIT = 16

#: 猫娘日记的条目化存储：每天一个 JSON（真正的数据源），Markdown 只是生成视图。
CAT_ENTRY_DIR_NAME = ".entries"
CAT_ENTRY_SUFFIX = ".json"
CAT_ENTRY_VERSION = 1

#: 条目在 Markdown 里的元信息行：``> ✎ 13:52 ｜ 😊 开心 ｜ 标签：日常 · 写日记 ｜ 🔒 仅自己可见``
CAT_STAMP_RE = re.compile(r"^>\s*✎\s*([0-2]?\d:[0-5]\d)\s*(?:｜\s*(.*))?$")
CAT_META_SPLIT_RE = re.compile(r"[｜|]")
CAT_TAG_SPLIT_RE = re.compile(r"[·,，、;；/]+")

#: 同一句话在不同来源之间对齐用的文本键（不看角色、不看时间）。
CONTENT_KEY_SEPARATOR = "\x1f"
#: 每个文本键最多记住多少条来源记录（防止索引无限增长）。
CONTENT_INDEX_MAX = 8


def text_key(turn: Turn) -> str:
    """跨来源对齐用的文本键：只看文本，角色另存并做相容判断。"""

    return hashlib.sha1(turn.text.encode("utf-8")).hexdigest()[:20]


class ChronicleStore:
    """管理一个插件实例的全部文件读写。"""

    def __init__(self, root: Path | str, config: Any) -> None:
        self.root = Path(root)
        self.config = config
        self.raw_dir = self.root / str(getattr(config, "raw_dir", "raw"))
        self.chapters_dir = self.root / str(getattr(config, "chapters_dir", "chapters"))
        self.diary_dir = self.root / str(getattr(config, "diary_dir", "diary"))
        # 时间胶囊：一枚一个 JSON，未到期前正文不外泄
        self.capsule_dir = self.root / str(getattr(config, "capsule_dir", "capsules"))
        # 猫娘日记（任务3）：与自动 diary 完全隔离，只追加不覆盖
        self.cat_diary_dir = self.root / str(getattr(config, "cat_diary_dir", "neko_diary"))
        # 放映厅：每天的观看位置与手记，没有对话原文的副本
        self.screening_dir = self.root / str(getattr(config, "screening_dir", "screenings"))
        self.state_path = self.root / "state.json"
        self._lock = threading.RLock()
        self._seen: OrderedDict[str, set[str]] = OrderedDict()
        self._turn_cache: OrderedDict[str, list[Turn]] = OrderedDict()
        #: day -> (mtime_ns, size, 行数)，用于不解析全文就能给出条数
        self._count_cache: dict[str, tuple[int, int, int]] = {}
        #: day -> 内容键 -> [(来源, 时间戳)]，用于跨来源去重。
        #: 只保留最近若干天（与 ``_seen`` 同一上限），否则长期运行会按天无限增长。
        self._text_index: OrderedDict[str, dict[str, list[tuple[str, float]]]] = OrderedDict()
        #: 目录扫描缓存：签名（文件名 + mtime + 大小）→ 日期列表。
        #: 面板/检索会反复问"有哪些天"，缓存后就不必每次重扫、重解析猫娘日记 JSON。
        self._raw_days_cache: tuple[tuple, list[str]] | None = None
        self._cat_days_cache: tuple[tuple, list[str]] | None = None

    # ── 目录与路径 ──────────────────────────────────────────────────

    def ensure_dirs(self) -> None:
        for directory in (
            self.root,
            self.raw_dir,
            self.chapters_dir,
            self.diary_dir,
            self.capsule_dir,
            self.cat_diary_dir,
            self.screening_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def raw_path(self, day: str) -> Path:
        return self.raw_dir / f"{day}{DAY_FILE_SUFFIX}"

    def chapter_path(self, day: str) -> Path:
        return self.chapters_dir / f"{day}{MARKDOWN_SUFFIX}"

    def diary_path(self, day: str) -> Path:
        return self.diary_dir / f"{day}{MARKDOWN_SUFFIX}"

    def cat_diary_path(self, day: str) -> Path:
        """猫娘自己写的那篇日记的路径（任务3，单独存放）。"""

        return self.cat_diary_dir / f"{day}{MARKDOWN_SUFFIX}"

    def cat_entries_path(self, day: str) -> Path:
        """猫娘日记的条目化数据（JSON，真正的事实来源）。"""

        return self.cat_diary_dir / CAT_ENTRY_DIR_NAME / f"{day}{CAT_ENTRY_SUFFIX}"

    def screening_path(self, day: str) -> Path:
        """放映厅状态：观看位置、停靠点与手记。"""

        return self.screening_dir / f"{day}.json"

    def screening_review_path(self, day: str) -> Path:
        """最新一篇放映厅观后感的可读 Markdown。"""

        return self.screening_dir / "reviews" / f"{day}.md"

    def write_screening_review(self, day: str, markdown: str) -> Path:
        with self._lock:
            path = self.screening_review_path(day)
            self._atomic_write(path, markdown)
            return path

    def read_screening_review(self, day: str) -> str | None:
        return self.read_text(self.screening_review_path(day))

    def load_screening(self, day: str) -> dict:
        """读取某天的放映状态；没有或损坏时返回空字典。"""

        with self._lock:
            text = self.read_text(self.screening_path(day))
            if not text:
                return {}
            try:
                data = json.loads(text)
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

    def save_screening(self, day: str, session: Mapping[str, Any]) -> None:
        """原子写入放映状态。"""

        with self._lock:
            self._atomic_write(
                self.screening_path(day),
                json.dumps(dict(session), ensure_ascii=False, indent=2) + "\n",
            )

    def list_screening_days(self) -> list[str]:
        """已有放映状态的日期（升序）。"""

        if not self.screening_dir.is_dir():
            return []
        return sorted(
            path.stem
            for path in self.screening_dir.glob("*.json")
            if len(path.stem) == 10
        )

    # ── 原子写 ──────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, path)

    @staticmethod
    def read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None

    # ── 去重索引 ────────────────────────────────────────────────────

    def _redact(self, text: str) -> str:
        patterns = getattr(self.config, "redact", None) or []
        result = text
        for pattern in patterns:
            if pattern:
                result = result.replace(str(pattern), "＊＊＊")
        return result

    def _load_seen(self, day: str) -> set[str]:
        cached = self._seen.get(day)
        if cached is not None:
            self._seen.move_to_end(day)
            return cached
        seen: set[str] = set()
        path = self.raw_path(day)
        text = self.read_text(path)
        if text:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                turn = Turn.from_json(data) if isinstance(data, Mapping) else None
                if turn is None:
                    continue
                seen.add(turn.fingerprint())
                seen.add(turn.loose_key())
                self._remember_content(day, turn)
        self._seen[day] = seen
        while len(self._seen) > DAY_CACHE_LIMIT:
            self._seen.popitem(last=False)
        return seen

    # ── 跨来源去重（同一句话的时间戳口径不同）────────────────────────

    def _remember_content(self, day: str, turn: Turn) -> None:
        day_index = self._text_index.setdefault(day, {})
        self._text_index.move_to_end(day)
        entries = day_index.setdefault(text_key(turn), [])
        row = (turn.role, turn.source, float(turn.ts))
        # 缓存被淘汰后重新扫同一份文件时，不要重复记入同样的来源记录
        if row in entries:
            return
        entries.append(row)
        if len(entries) > CONTENT_INDEX_MAX:
            del entries[:-CONTENT_INDEX_MAX]
        while len(self._text_index) > DAY_CACHE_LIMIT:
            self._text_index.popitem(last=False)

    def _seen_same_content_elsewhere(self, day: str, turn: Turn) -> bool:
        """同一句话是否已由**别的来源**在窗口内记过。

        只看来源不同、且角色相容（相同或一边未识别）的副本：角色真的连说两次
        一样的话（同一来源）必须保留。
        """

        for role, source, moment in self._text_index.get(day, {}).get(text_key(turn), []):
            if source == turn.source:
                continue
            if not roles_compatible(role, turn.role):
                continue
            if abs(moment - float(turn.ts)) <= CROSS_SOURCE_WINDOW_SECONDS:
                return True
        return False

    # ── 写入 ────────────────────────────────────────────────────────

    def append_turns(self, turns: Iterable[Turn]) -> dict[str, int]:
        """按日期追加轮次，返回 ``{日期: 新增条数}``。"""

        added: dict[str, int] = {}
        prepared: dict[str, list[Turn]] = {}
        for turn in turns:
            if turn.is_empty():
                continue
            turn.text = self._redact(turn.text)
            if not turn.text:
                continue
            prepared.setdefault(turn.day or local_day_key(turn.ts), []).append(turn)

        if not prepared:
            return added

        with self._lock:
            self.ensure_dirs()
            for day, items in prepared.items():
                seen = self._load_seen(day)
                fresh: list[Turn] = []
                for turn in sort_turns(items):
                    exact = turn.fingerprint()
                    loose = turn.loose_key()
                    if exact in seen or loose in seen:
                        continue
                    if self._seen_same_content_elsewhere(day, turn):
                        continue
                    seen.add(exact)
                    seen.add(loose)
                    self._remember_content(day, turn)
                    fresh.append(turn)
                if not fresh:
                    added[day] = 0
                    continue
                path = self.raw_path(day)
                with open(path, "a", encoding="utf-8", newline="\n") as handle:
                    for turn in fresh:
                        handle.write(json.dumps(turn.to_json(), ensure_ascii=False) + "\n")
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                self._turn_cache.pop(day, None)
                added[day] = len(fresh)
        return added

    def load_turns(self, day: str) -> list[Turn]:
        """读取某天的全部轮次（按时间排序）。"""

        with self._lock:
            cached = self._turn_cache.get(day)
            if cached is not None:
                self._turn_cache.move_to_end(day)
                return list(cached)
            turns: list[Turn] = []
            text = self.read_text(self.raw_path(day))
            if text:
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(data, Mapping):
                        continue
                    turn = Turn.from_json(data)
                    if turn is not None:
                        turns.append(turn)
            ordered = sort_turns(dedupe_turns(turns))
            self._turn_cache[day] = ordered
            while len(self._turn_cache) > DAY_CACHE_LIMIT:
                self._turn_cache.popitem(last=False)
            return list(ordered)

    def count_turns(self, day: str) -> int:
        """只数条数、不解析 JSON —— 面板一次列很多天时用它。"""

        text = self.read_text(self.raw_path(day))
        if not text:
            return 0
        return sum(1 for line in text.splitlines() if line.strip())

    def turn_count(self, day: str) -> int:
        """条数的快速版本：已解析过就直接用缓存，否则按文件签名缓存行数。"""

        with self._lock:
            cached = self._turn_cache.get(day)
            if cached is not None:
                return len(cached)
            path = self.raw_path(day)
            try:
                stat = path.stat()
            except OSError:
                return 0
            signature = (stat.st_mtime_ns, stat.st_size)
            hit = self._count_cache.get(day)
            if hit is not None and hit[:2] == signature:
                return hit[2]
            count = self.count_turns(day)
            self._count_cache[day] = (signature[0], signature[1], count)
            return count

    # ── 章节 / 日记 ─────────────────────────────────────────────────
    def write_chapter(self, day: str, markdown: str) -> Path:
        with self._lock:
            path = self.chapter_path(day)
            self._atomic_write(path, markdown)
            return path

    def write_diary(self, day: str, markdown: str) -> Path:
        with self._lock:
            path = self.diary_path(day)
            self._atomic_write(path, markdown)
            return path

    def read_chapter(self, day: str) -> str | None:
        return self.read_text(self.chapter_path(day))

    def read_diary(self, day: str) -> str | None:
        return self.read_text(self.diary_path(day))

    def has_chapter(self, day: str) -> bool:
        return self.chapter_path(day).is_file()

    def has_diary(self, day: str) -> bool:
        return self.diary_path(day).is_file()

    # ── 猫娘日记（任务3）────────────────────────────────────────────
    #
    # 存储采用「条目化 JSON + Markdown 展示」：
    #   neko_diary/.entries/2026-10-23.json  真正的数据源，每段日记一条
    #   neko_diary/2026-10-23.md             由 JSON 自动生成的阅读视图
    # 旧版本只有 Markdown 文件，首次读取时会自动解析并迁移成条目。

    @staticmethod
    def _iso_moment(now: float | None = None) -> str:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(float(now) if now else time.time())
        )

    @staticmethod
    def _clock_of(now: float | None = None) -> str:
        return time.strftime(
            "%H:%M", time.localtime(float(now) if now else time.time())
        )

    @staticmethod
    def _tag_list(value: Any) -> list[str]:
        """把标签输入规整成去重后的字符串列表。"""

        if isinstance(value, str):
            parts: list[str] = CAT_TAG_SPLIT_RE.split(value)
        elif isinstance(value, (list, tuple, set)):
            parts = [str(item) for item in value]
        elif value is None:
            parts = []
        else:
            parts = [str(value)]
        out: list[str] = []
        for part in parts:
            tag = part.strip()
            if tag and tag not in out:
                out.append(tag)
        return out[:6]

    def _clean_cat_entry(self, item: Mapping, day: str) -> dict:
        """把一条原始条目规整成固定结构（容错旧数据/外部写入）。"""

        mood = normalize_mood(item.get("mood"), "neutral")
        spec = MOODS.get(mood, NEUTRAL_MOOD)
        created = str(item.get("created_at") or "") or f"{day}T00:00:00"
        return {
            "id": str(item.get("id") or uuid.uuid4().hex[:12]),
            "time": str(item.get("time") or created[11:16] or ""),
            "mood": mood,
            "mood_label": str(item.get("mood_label") or spec["label"]),
            "mood_emoji": str(item.get("mood_emoji") or spec["emoji"]),
            "tags": self._tag_list(item.get("tags")),
            "visibility": normalize_visibility(item.get("visibility"), "public"),
            "text": str(item.get("text") or "").strip(),
            "created_at": created,
            "updated_at": str(item.get("updated_at") or created),
            "deleted": bool(item.get("deleted")),
            "deleted_at": str(item.get("deleted_at") or ""),
        }

    def _parse_cat_meta_text(self, meta: str) -> dict:
        """解析 Markdown 元信息行里 ｜ 之后的片段（心情/标签/可见性）。"""

        mood = ""
        tags: list[str] = []
        visibility = "public"
        for part in CAT_META_SPLIT_RE.split(meta or ""):
            text = part.strip()
            if not text:
                continue
            if "🔒" in text or CAT_DIARY_VISIBILITY_LABELS["private"] in text:
                visibility = "private"
                continue
            if text.startswith("标签：") or text.startswith("标签:") or text.lower().startswith("tags"):
                raw = re.split(r"[：:]", text, maxsplit=1)
                tags = self._tag_list(raw[1] if len(raw) > 1 else text)
                continue
            for key, spec in MOODS.items():
                if spec["emoji"] in text or spec["label"] in text:
                    mood = key
                    break
        return {"mood": mood, "tags": tags, "visibility": visibility}

    def _parse_cat_markdown(self, day: str, text: str | None) -> list[dict]:
        """把旧版 Markdown 拆成条目（用于首次读取时的迁移）。"""

        if not text:
            return []
        blocks: list[dict] = []
        current: dict | None = None
        for raw in text.splitlines():
            stamp = CAT_STAMP_RE.match(raw.strip())
            if stamp:
                if current is not None:
                    blocks.append(current)
                meta = self._parse_cat_meta_text(stamp.group(2) or "")
                current = {
                    "time": stamp.group(1),
                    **meta,
                    "lines": [],
                    "created_at": f"{day}T{stamp.group(1).zfill(5)}:00",
                }
                continue
            if current is not None:
                current["lines"].append(raw)
        if current is not None:
            blocks.append(current)

        if not blocks:
            # 没有任何元信息行：整篇当作一条，保留原有内容不丢字
            body = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("#")
            ).strip()
            if not body:
                return []
            blocks = [{"time": "", "lines": body.splitlines(), "created_at": f"{day}T00:00:00"}]

        entries: list[dict] = []
        for block in blocks:
            body = "\n".join(block.pop("lines", [])).strip()
            if not body:
                continue
            entries.append(self._clean_cat_entry({**block, "text": body}, day))
        return entries

    def _entry_meta_text(self, entry: Mapping) -> str:
        """把条目拼回 Markdown 元信息行（不含 ``> ✎ 时间``）。"""

        parts: list[str] = []
        if entry.get("mood_label"):
            parts.append(f"{entry.get('mood_emoji') or ''} {entry['mood_label']}".strip())
        tags = self._tag_list(entry.get("tags"))
        if tags:
            parts.append("标签：" + " · ".join(tags))
        if entry.get("visibility") == "private":
            parts.append("🔒 " + CAT_DIARY_VISIBILITY_LABELS["private"])
        return " ｜ ".join(parts)

    def _render_cat_markdown(
        self, day: str, entries: Sequence[Mapping], *, include_private: bool = True
    ) -> str:
        """由条目生成阅读用的 Markdown。"""

        live = [
            entry
            for entry in entries
            if not entry.get("deleted")
            and (include_private or entry.get("visibility") != "private")
        ]
        if not live:
            return ""
        blocks = [f"# {day} · 猫娘的日记"]
        for entry in live:
            stamp = f"> ✎ {entry.get('time') or '--:--'}"
            meta = self._entry_meta_text(entry)
            if meta:
                stamp = f"{stamp} ｜ {meta}"
            blocks.append(f"{stamp}\n\n{entry.get('text') or ''}")
        return "\n\n".join(blocks) + "\n"

    def _load_cat_entries(self, day: str) -> list[dict]:
        """读取某天的条目；没有 JSON 但有旧 Markdown 时自动迁移。"""

        with self._lock:
            payload = self.read_text(self.cat_entries_path(day))
            if payload:
                try:
                    data = json.loads(payload)
                except ValueError:
                    data = None
                if isinstance(data, Mapping) and isinstance(data.get("entries"), list):
                    return [
                        self._clean_cat_entry(item, day)
                        for item in data["entries"]
                        if isinstance(item, Mapping)
                    ]
            markdown = self.read_text(self.cat_diary_path(day))
            entries = self._parse_cat_markdown(day, markdown)
            if entries:
                self._save_cat_entries(day, entries)
            return entries

    def _save_cat_entries(self, day: str, entries: Sequence[Mapping]) -> None:
        """写回 JSON，并同步重新生成 Markdown 视图。"""

        payload = {
            "day": day,
            "version": CAT_ENTRY_VERSION,
            "entries": list(entries),
        }
        self._atomic_write(
            self.cat_entries_path(day),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        self._atomic_write(
            self.cat_diary_path(day), self._render_cat_markdown(day, entries)
        )

    def cat_diary_entries(self, day: str, *, include_deleted: bool = False) -> list[dict]:
        """某天猫娘日记的全部条目（默认不含已删除）。"""

        entries = self._load_cat_entries(day)
        if include_deleted:
            return [dict(entry) for entry in entries]
        return [dict(entry) for entry in entries if not entry.get("deleted")]

    def cat_diary_markdown(self, day: str, *, include_private: bool = True) -> str:
        """按可见性过滤后生成的 Markdown（浏览页用）。"""

        return self._render_cat_markdown(
            day, self._load_cat_entries(day), include_private=include_private
        )

    def cat_diary_all_private(self, day: str) -> bool:
        """当天是否**所有**未删除的条目都是「仅自己可见」。"""

        entries = self.cat_diary_entries(day)
        return bool(entries) and all(self.is_private_entry(entry) for entry in entries)

    @staticmethod
    def is_private_entry(entry: Mapping[str, Any]) -> bool:
        """猫娘日记条目的「仅自己可见」判定（统一口径，避免各处各写一遍）。"""

        return str(entry.get("visibility") or "").lower() == "private"

    @classmethod
    def visible_entries(
        cls, entries: Sequence[Mapping[str, Any]], *, include_private: bool
    ) -> list[dict]:
        """按可见性过滤条目；``include_private=False`` 时去掉私密条目。"""

        items = [dict(item) for item in entries]
        if include_private:
            return items
        return [item for item in items if not cls.is_private_entry(item)]

    def read_cat_diary(self, day: str) -> str | None:
        """读取猫娘自己写的那篇日记（没有则 ``None``）。

        返回的是含全部条目（含私密）的 Markdown 视图；数据本身在条目 JSON 里。
        """

        entries = self._load_cat_entries(day)
        text = self.read_text(self.cat_diary_path(day))
        if entries and text is None:
            return self._render_cat_markdown(day, entries)
        return text

    def has_cat_diary(self, day: str) -> bool:
        return bool(self.cat_diary_entries(day))

    def append_cat_diary(
        self,
        day: str,
        text: str,
        *,
        mood: str = "",
        tags: Any = None,
        visibility: str = "",
        meta: str = "",
        now: float | None = None,
    ) -> dict:
        """追加一段猫娘日记（新增一条条目，不覆盖已有条目）。

        ``meta`` 为兼容旧调用的元信息字符串；结构化参数优先于它。
        返回 ``{"added", "existing_chars", "chars", "path", "entry_id", "count"}``。
        """

        body = (text or "").strip()
        if not body:
            return {"added": False, "reason": "empty"}
        fallback = self._parse_cat_meta_text(meta) if meta else {}
        mood_key = normalize_mood(mood or fallback.get("mood"), "neutral")
        tags_list = self._tag_list(tags if tags is not None else fallback.get("tags"))
        vis = normalize_visibility(
            visibility or fallback.get("visibility"), "public"
        )
        moment = self._clock_of(now)
        created = self._iso_moment(now)
        with self._lock:
            entries = self._load_cat_entries(day)
            existing = self.read_text(self.cat_diary_path(day)) or ""
            entry = self._clean_cat_entry(
                {
                    "id": uuid.uuid4().hex[:12],
                    "time": moment,
                    "mood": mood_key,
                    "tags": tags_list,
                    "visibility": vis,
                    "text": body,
                    "created_at": created,
                    "updated_at": created,
                },
                day,
            )
            entries.append(entry)
            self._save_cat_entries(day, entries)
        return {
            "added": True,
            "existing_chars": len(existing),
            "chars": len(body),
            "path": str(self.cat_diary_path(day)),
            "entry_id": entry["id"],
            "count": len([item for item in entries if not item.get("deleted")]),
            "entry": dict(entry),
        }

    def replace_cat_diary(
        self,
        day: str,
        text: str,
        *,
        mood: str = "",
        tags: Any = None,
        visibility: str = "",
        meta: str = "",
        now: float | None = None,
    ) -> dict:
        """整篇重写当天猫娘日记：旧条目软删除（保留历史），再追加一条新的。"""

        body = (text or "").strip()
        if not body:
            return {"added": False, "reason": "empty"}
        fallback = self._parse_cat_meta_text(meta) if meta else {}
        mood_key = normalize_mood(mood or fallback.get("mood"), "neutral")
        tags_list = self._tag_list(tags if tags is not None else fallback.get("tags"))
        vis = normalize_visibility(
            visibility or fallback.get("visibility"), "public"
        )
        moment = self._clock_of(now)
        created = self._iso_moment(now)
        with self._lock:
            entries = self._load_cat_entries(day)
            existing = self.read_text(self.cat_diary_path(day)) or ""
            removed = 0
            for item in entries:
                if not item.get("deleted"):
                    item["deleted"] = True
                    item["deleted_at"] = created
                    item["updated_at"] = created
                    removed += 1
            entry = self._clean_cat_entry(
                {
                    "id": uuid.uuid4().hex[:12],
                    "time": moment,
                    "mood": mood_key,
                    "tags": tags_list,
                    "visibility": vis,
                    "text": body,
                    "created_at": created,
                    "updated_at": created,
                },
                day,
            )
            entries.append(entry)
            self._save_cat_entries(day, entries)
        return {
            "added": True,
            "replaced": True,
            "removed": removed,
            "existing_chars": len(existing),
            "chars": len(body),
            "path": str(self.cat_diary_path(day)),
            "entry_id": entry["id"],
            "count": 1,
            "entry": dict(entry),
        }

    def update_cat_diary_entry(
        self,
        day: str,
        entry_id: str,
        *,
        text: str | None = None,
        mood: str | None = None,
        tags: Any = None,
        visibility: str | None = None,
        now: float | None = None,
    ) -> dict:
        """按 id 修改一条日记（只改传进来的字段）。"""

        target = str(entry_id or "").strip()
        if not target:
            return {"updated": False, "reason": "missing_id"}
        with self._lock:
            entries = self._load_cat_entries(day)
            for item in entries:
                if item.get("id") != target or item.get("deleted"):
                    continue
                if text is not None:
                    body = str(text).strip()
                    if not body:
                        return {"updated": False, "reason": "empty"}
                    item["text"] = body
                if mood is not None and str(mood).strip():
                    item["mood"] = normalize_mood(mood, item.get("mood") or "neutral")
                    spec = MOODS.get(item["mood"], NEUTRAL_MOOD)
                    item["mood_label"] = spec["label"]
                    item["mood_emoji"] = spec["emoji"]
                if tags is not None:
                    item["tags"] = self._tag_list(tags)
                if visibility is not None and str(visibility).strip():
                    item["visibility"] = normalize_visibility(
                        visibility, item.get("visibility") or "public"
                    )
                item["updated_at"] = self._iso_moment(now)
                item = self._clean_cat_entry(item, day)
                self._save_cat_entries(day, entries)
                return {"updated": True, "day": day, "entry": dict(item)}
        return {"updated": False, "reason": "not_found"}

    def delete_cat_diary_entry(
        self, day: str, entry_id: str, *, permanent: bool = False, now: float | None = None
    ) -> dict:
        """删除一条日记：默认软删除（可保留历史），``permanent=True`` 时彻底移除。"""

        target = str(entry_id or "").strip()
        if not target:
            return {"deleted": False, "reason": "missing_id"}
        with self._lock:
            entries = self._load_cat_entries(day)
            for index, item in enumerate(entries):
                if item.get("id") != target:
                    continue
                if permanent:
                    entries.pop(index)
                elif item.get("deleted"):
                    return {"deleted": False, "reason": "already_deleted"}
                else:
                    item["deleted"] = True
                    item["deleted_at"] = self._iso_moment(now)
                    item["updated_at"] = item["deleted_at"]
                self._save_cat_entries(day, entries)
                return {
                    "deleted": True,
                    "permanent": permanent,
                    "day": day,
                    "entry_id": target,
                    "count": len([row for row in entries if not row.get("deleted")]),
                }
        return {"deleted": False, "reason": "not_found"}

    def search_cat_diary(
        self,
        keyword: str,
        *,
        limit: int = 50,
        days: Sequence[str] | None = None,
    ) -> list[dict]:
        """在猫娘日记里按关键词检索（正文 / 标签 / 心情）。"""

        query = str(keyword or "").strip().lower()
        if not query:
            return []
        cap = max(1, min(int(limit or 50), 500))
        hits: list[dict] = []
        day_list = sorted(days) if days else self.list_cat_diary_days()
        for day in reversed(day_list):
            for entry in self.cat_diary_entries(day):
                haystack = " ".join(
                    [entry.get("text") or "", " ".join(entry.get("tags") or []), entry.get("mood_label") or ""]
                ).lower()
                if query not in haystack:
                    continue
                hits.append(
                    {
                        "date": day,
                        "id": entry["id"],
                        "time": entry["time"],
                        "mood": entry["mood"],
                        "mood_label": entry["mood_label"],
                        "mood_emoji": entry["mood_emoji"],
                        "tags": entry["tags"],
                        "visibility": entry["visibility"],
                        "private": entry["visibility"] == "private",
                        "text": entry["text"],
                        "snippet": self._cat_snippet(entry["text"], query),
                    }
                )
                if len(hits) >= cap:
                    return hits
        return hits

    @staticmethod
    def _cat_snippet(text: str, query: str, *, width: int = 40) -> str:
        """取命中位置附近的一段文字做摘要。"""

        body = " ".join((text or "").split())
        index = body.lower().find(query)
        if index < 0:
            return body[: width * 2]
        start = max(0, index - width)
        end = min(len(body), index + len(query) + width)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(body) else ""
        return f"{prefix}{body[start:end]}{suffix}"

    def cat_diary_stats(self, *, days: int = 30) -> dict:
        """最近 N 天的心情与写作统计。"""

        span = max(1, min(int(days or 30), 3650))
        end = local_day_key()
        start_ts = time.mktime(time.strptime(end, "%Y-%m-%d")) - (span - 1) * 86400
        start = time.strftime("%Y-%m-%d", time.localtime(start_ts))

        mood_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        daily: list[dict] = []
        total_entries = 0
        total_chars = 0
        for day in self.list_cat_diary_days():
            if day < start or day > end:
                continue
            entries = self.cat_diary_entries(day)
            if not entries:
                continue
            day_moods: dict[str, int] = {}
            chars = 0
            for entry in entries:
                mood_counts[entry["mood"]] = mood_counts.get(entry["mood"], 0) + 1
                day_moods[entry["mood"]] = day_moods.get(entry["mood"], 0) + 1
                for tag in entry.get("tags") or []:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                chars += len(entry.get("text") or "")
            total_entries += len(entries)
            total_chars += chars
            top_mood = max(day_moods.items(), key=lambda item: item[1])[0]
            spec = MOODS.get(top_mood, NEUTRAL_MOOD)
            daily.append(
                {
                    "date": day,
                    "entries": len(entries),
                    "chars": chars,
                    "mood": top_mood,
                    "mood_label": spec["label"],
                    "mood_emoji": spec["emoji"],
                }
            )

        moods = []
        for mood, count in sorted(mood_counts.items(), key=lambda item: (-item[1], item[0])):
            spec = MOODS.get(mood, NEUTRAL_MOOD)
            moods.append(
                {
                    "mood": mood,
                    "label": spec["label"],
                    "emoji": spec["emoji"],
                    "count": count,
                    "ratio": round(count / total_entries, 3) if total_entries else 0.0,
                }
            )
        tags = [
            {"tag": tag, "count": count}
            for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        ]
        return {
            "start": start,
            "end": end,
            "days": span,
            "entries": total_entries,
            "diary_days": len(daily),
            "chars": total_chars,
            "moods": moods,
            "tags": tags,
            "daily": daily,
        }

    def cat_diary_throwback(self, *, day: str | None = None, years: int = 0) -> list[dict]:
        """往年今日：找出去年/更早同月同日的猫娘日记。"""

        target = str(day or local_day_key()).strip()
        if len(target) != 10:
            return []
        month_day = target[5:]
        limit = max(0, int(years or 0))
        result: list[dict] = []
        for other in reversed(self.list_cat_diary_days()):
            if len(other) != 10 or other == target or other[5:] != month_day:
                continue
            try:
                gap = int(target[:4]) - int(other[:4])
            except ValueError:
                continue
            if gap <= 0 or (limit and gap > limit):
                continue
            entries = self.cat_diary_entries(other)
            if not entries:
                continue
            result.append(
                {
                    "date": other,
                    "years_ago": gap,
                    "count": len(entries),
                    "entries": entries,
                }
            )
        return result

    # ── 日期集合 ────────────────────────────────────────────────────

    @staticmethod
    def _dir_signature(directory: Path, pattern: str) -> tuple:
        """目录扫描签名：文件名 + mtime + 大小。

        任一文件新增/删除/改写都会让签名变化，从而自动让日期缓存失效
        （外部手工改文件也能被感知，不需要额外的失效钩子）。
        """

        if not directory.is_dir():
            return ()
        items: list[tuple[str, int, int]] = []
        for path in directory.glob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(items))

    def list_raw_days(self) -> list[str]:
        signature = self._dir_signature(self.raw_dir, f"*{DAY_FILE_SUFFIX}")
        cached = self._raw_days_cache
        if cached is not None and cached[0] == signature:
            return list(cached[1])
        if not self.raw_dir.is_dir():
            days: list[str] = []
        else:
            days = [
                path.stem
                for path in self.raw_dir.glob(f"*{DAY_FILE_SUFFIX}")
                if path.stem and len(path.stem) == 10
            ]
            days.sort()
        self._raw_days_cache = (signature, days)
        return list(days)

    def day_index(self, day: str) -> int:
        """章节序号（第 N 章），按已有原始记录的第一天起算。"""

        days = self.list_raw_days()
        if day in days:
            return days.index(day) + 1
        earlier = [item for item in days if item < day]
        return len(earlier) + 1

    def list_cat_diary_days(self) -> list[str]:
        """已有猫娘日记的日期（升序，只算还有未删除条目的日子）。"""

        signature = (
            self._dir_signature(self.cat_diary_dir, f"*{MARKDOWN_SUFFIX}"),
            self._dir_signature(
                self.cat_diary_dir / CAT_ENTRY_DIR_NAME, f"*{CAT_ENTRY_SUFFIX}"
            ),
        )
        cached = self._cat_days_cache
        if cached is not None and cached[0] == signature:
            return list(cached[1])

        days: set[str] = set()
        for path in self.cat_diary_dir.glob(f"*{MARKDOWN_SUFFIX}"):
            if len(path.stem) == 10:
                days.add(path.stem)
        entries_dir = self.cat_diary_dir / CAT_ENTRY_DIR_NAME
        if entries_dir.is_dir():
            for path in entries_dir.glob(f"*{CAT_ENTRY_SUFFIX}"):
                if len(path.stem) == 10:
                    days.add(path.stem)
        result = sorted(day for day in days if self.cat_diary_entries(day))
        # 注意：首次读取旧 Markdown 会触发迁移写盘，签名会因此变化；
        # 下次调用检测到不一致会重算一次，此后命中缓存。
        self._cat_days_cache = (signature, result)
        return list(result)

    def write_indexes(self, *, mood_by_day: Mapping[str, str] | None = None) -> None:
        """重建 chapters/README.md 与 diary/README.md。"""

        days = self.list_raw_days()
        moods = dict(mood_by_day or {})
        chapter_entries = []
        diary_entries = []
        for order, day in enumerate(days, start=1):
            has_chapter = self.has_chapter(day)
            has_diary = self.has_diary(day)
            # 没有章节也没有日记的日子不会出现在两个目录里，没必要解析它的全文
            if not has_chapter and not has_diary:
                continue
            base = {"day": day, "index": order, "turns": self.turn_count(day)}
            if has_chapter:
                chapter_entries.append(dict(base))
            if has_diary:
                diary_entries.append(dict(base, mood=moods.get(day, "")))
        with self._lock:
            self._atomic_write(
                self.chapters_dir / f"README{MARKDOWN_SUFFIX}",
                build_index(chapter_entries, self.config, kind="chapter"),
            )
            self._atomic_write(
                self.diary_dir / f"README{MARKDOWN_SUFFIX}",
                build_index(diary_entries, self.config, kind="diary"),
            )

    # ── 状态 ────────────────────────────────────────────────────────

    def load_state(self) -> dict:
        text = self.read_text(self.state_path)
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: Mapping[str, Any]) -> None:
        with self._lock:
            try:
                self._atomic_write(
                    self.state_path, json.dumps(dict(state), ensure_ascii=False, indent=2)
                )
            except OSError:
                pass

    # ── 检索 ────────────────────────────────────────────────────────

    def search(self, query: str, *, limit: int = 20, context_chars: int = 120) -> list[dict]:
        """在章节与日记里做大小写不敏感的全文检索。"""

        needle = (query or "").strip().lower()
        if not needle:
            return []
        results: list[dict] = []
        for kind, directory, suffix in (
            ("chapter", self.chapters_dir, MARKDOWN_SUFFIX),
            ("diary", self.diary_dir, MARKDOWN_SUFFIX),
        ):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob(f"*{suffix}")):
                if path.stem == "README":
                    continue
                text = self.read_text(path)
                if not text:
                    continue
                index = text.lower().find(needle)
                if index < 0:
                    continue
                start = max(0, index - context_chars // 2)
                snippet = " ".join(text[start : index + context_chars].split())
                results.append(
                    {
                        "day": path.stem,
                        "kind": kind,
                        "snippet": snippet,
                        "hits": text.lower().count(needle),
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    # ── 概览 ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        days = self.list_raw_days()
        chapters = sum(1 for day in days if self.has_chapter(day))
        diaries = sum(1 for day in days if self.has_diary(day))
        return {
            "days": len(days),
            "first_day": days[0] if days else "",
            "last_day": days[-1] if days else "",
            "chapters": chapters,
            "diaries": diaries,
            "root": str(self.root),
        }


__all__ = ["ChronicleStore"]
