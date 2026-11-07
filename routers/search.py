"""对话原文的检索、按序号取原文、按天读取与手动补记。"""

from __future__ import annotations

import asyncio
import time

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr, ui
from plugin.sdk.shared.core.router import PluginRouter

from .. import _parse_turn_index
from ..chronicle import reader as reader_mod
from ..chronicle.models import Turn, local_day_key


class SearchRouter(PluginRouter):
    """对话原文的检索、按序号取原文、按天读取与手动补记。"""

    def __init__(self):
        super().__init__(name="search")

    @plugin_entry(
        id="recent",
        name=tr("entries.recent.name", default="最近的对话记录"),
        description=tr(
            "entries.recent.description",
            default="返回最近的几条对话原文，带当天序号、时间与说话人。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": tr("fields.count", default="要几条（默认 10）"),
                    "default": 10,
                },
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
            },
        },
        llm_result_fields=["date", "items", "total"],
    )
    async def entry_recent(self, count: int = 10, date: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        wanted = self.main_plugin._normalize_day(date)
        day = wanted or local_day_key()
        turns = self.main_plugin._turns_for(day)
        if not turns and not wanted:
            latest = self.main_plugin.store.list_raw_days()
            if latest:
                day = latest[-1]
                turns = self.main_plugin._turns_for(day)
        size = max(1, min(int(count or 10), 200))
        total = len(turns)
        rows = reader_mod.day_rows(
            turns,
            master_name=self.main_plugin.cfg.master_name,
            catgirl_name=self.main_plugin.cfg.catgirl_name,
            start=max(1, total - size + 1),
            count=size,
        )
        return Ok({"date": day, "count": len(rows), "total": total, "items": rows})

    @plugin_entry(
        id="get_turn",
        name=tr("entries.getTurn.name", default="按序号取一条对话"),
        description=tr(
            "entries.getTurn.description",
            default="按当天序号取出某一条对话的原文。日期留空表示今天。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": tr("fields.index", default="当天第几条（从 1 开始）"),
                },
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
            },
            "required": ["index"],
        },
        llm_result_fields=["index", "time", "speaker", "text", "date"],
    )
    async def entry_get_turn(self, index: int = 0, date: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        turns = self.main_plugin._turns_for(day)
        # 兼容"13""第十三""第13条""最后一条""倒数第二条"
        wanted = _parse_turn_index(index, len(turns)) or 0
        found = reader_mod.find_turn(turns, wanted)
        if found is None:
            return Err(
                SdkError(f"{day} 没有第 {wanted} 条记录（这一天共 {len(turns)} 条）")
            )
        position, turn = found
        row = reader_mod.turn_row(
            position,
            turn,
            master_name=self.main_plugin.cfg.master_name,
            catgirl_name=self.main_plugin.cfg.catgirl_name,
            text_limit=reader_mod.SINGLE_TEXT_LIMIT,
        )
        row["total"] = len(turns)
        return Ok(row)

    @plugin_entry(
        id="read_day",
        name=tr("entries.readDay.name", default="读取某天的对话记录"),
        description=tr(
            "entries.readDay.description",
            default="按序号区间读取某一天的对话原文（默认从第 1 条开始，最多 50 条）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "start": {
                    "type": "integer",
                    "description": tr("fields.start", default="从第几条开始（默认 1）"),
                    "default": 1,
                },
                "count": {
                    "type": "integer",
                    "description": tr("fields.count", default="要几条（默认 50）"),
                    "default": 50,
                },
            },
        },
        llm_result_fields=["date", "items", "total", "start"],
    )
    async def entry_read_day(self, date: str = "", start: int = 1, count: int = 50, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        turns = self.main_plugin._turns_for(day)
        rows = reader_mod.day_rows(
            turns,
            master_name=self.main_plugin.cfg.master_name,
            catgirl_name=self.main_plugin.cfg.catgirl_name,
            start=int(start or 1),
            count=max(1, min(int(count or 50), 200)),
        )
        return Ok(
            {
                "date": day,
                "start": int(start or 1),
                "count": len(rows),
                "total": len(turns),
                "items": rows,
            }
        )

    @ui.action(
        label=tr("actions.searchChat.label", default="检索对话原文"),
        tone="primary",
        group="chronicle",
        order=50,
        refresh_context=False,
    )
    @plugin_entry(
        id="search_chat",
        name=tr("entries.searchChat.name", default="检索对话原文"),
        description=tr(
            "entries.searchChat.description",
            default="在对话原文里搜索关键词，返回命中的日期、序号、时间与片段。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": tr("fields.query", default="要查找的文字")},
                "limit": {
                    "type": "integer",
                    "description": tr("fields.searchLimit", default="最多返回几条（0 = 用插件配置）"),
                    "default": 0,
                },
                "days": {
                    "type": "integer",
                    "description": tr("fields.days", default="往回查几天（0 = 用插件配置）"),
                    "default": 0,
                },
            },
            "required": ["query"],
        },
        llm_result_fields=["query", "count", "days_searched", "impressions", "results"],
    )
    async def entry_search_chat(
        self,
        query: str = "",
        limit: int = 0,
        days: int = 0,
        per_day: int = 0,
        skip_meta: bool = False,
        **_,
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        text = str(query or "").strip()
        if not text:
            return Err(SdkError("query 不能为空"))
        # 缺省时用配置里的检索默认值（任务2：可自由配置）
        size = max(1, min(int(limit or self.main_plugin.cfg.search_default_limit), 200))
        day_span = max(1, min(int(days or self.main_plugin.cfg.search_default_days), 3650))
        # 抽稀：给猫娘的检索每天最多留几条（0 = 不抽稀，浏览页要看到全部命中）
        per_day_cap = max(0, min(int(per_day or 0), 10))
        payload = await asyncio.to_thread(
            self.main_plugin._search_chat_payload,
            text,
            size,
            day_span,
            per_day_cap,
            bool(skip_meta),
        )
        return Ok(payload)

    @plugin_entry(
        id="search",
        name=tr("entries.search.name", default="检索章节与日记"),
        description=tr(
            "entries.search.description",
            default="在已生成的章节和日记里做全文检索，返回命中的日期与片段。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": tr("fields.query", default="要查找的文字")},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        llm_result_fields=["results", "count"],
    )
    async def entry_search(self, query: str = "", limit: int = 10, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        if not str(query).strip():
            return Err(SdkError("query 不能为空"))
        results = await asyncio.to_thread(self.main_plugin.store.search, str(query), limit=int(limit))
        return Ok({"count": len(results), "results": results})

    @plugin_entry(
        id="capture",
        name=tr("entries.capture.name", default="手动补记一句话"),
        description=tr(
            "entries.capture.description",
            default="手动往当天记录里补一句话（用于测试或记下不在聊天里的内容）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": tr("fields.text", default="要记下的内容")},
                "role": {
                    "type": "string",
                    "enum": ["user", "assistant", "system"],
                    "default": "user",
                },
                "speaker": {"type": "string", "description": tr("fields.speaker", default="说话人显示名")},
            },
            "required": ["text"],
        },
        llm_result_fields=["added", "day"],
    )
    async def entry_capture(self, text: str = "", role: str = "user", speaker: str = "", **_):
        content = str(text or "").strip()
        if not content:
            return Err(SdkError("text 不能为空"))
        turn = Turn(ts=time.time(), text=content, role=str(role or "user"), speaker=speaker, source="manual")
        added = self.main_plugin._ingest([turn])
        await self.main_plugin._refresh_today_chapter(force=True)
        return Ok({"added": sum(added.values()), "day": turn.day})

    @plugin_entry(
        id="inspect_capture",
        name=tr("entries.inspectCapture.name", default="查看采集来源"),
        description=tr(
            "entries.inspectCapture.description",
            default="排障用：显示消息总线返回的记录字段名与角色识别结果，不写入任何记录。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sample": {
                    "type": "integer",
                    "description": tr("fields.sample", default="取样条数（1-5）"),
                    "default": 2,
                }
            },
        },
        llm_result_fields=["available", "memory_keys", "conversation_keys", "detected", "errors"],
    )
    async def entry_inspect_capture(self, sample: int = 2, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        return Ok(await self.main_plugin._probe_bus(sample=int(sample or 2)))
