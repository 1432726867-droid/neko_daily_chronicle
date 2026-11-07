"""猫娘自己写的日记：写入、读取、检索、统计、往年今日与条目增删改。"""

from __future__ import annotations

import asyncio
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr, ui
from plugin.sdk.shared.core.router import PluginRouter

from .. import _as_tag_list
from ..chronicle import diary as diary_mod
from ..chronicle.models import local_day_key


class CatDiaryRouter(PluginRouter):
    """猫娘自己写的日记：写入、读取、检索、统计、往年今日与条目增删改。"""

    def __init__(self):
        super().__init__(name="cat_diary")

    @ui.action(
        label=tr("actions.catWriteDiary.label", default="猫娘写日记"),
        tone="primary",
        group="catdiary",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="cat_write_diary",
        name=tr("entries.catWriteDiary.name", default="猫娘自己写日记"),
        description=tr(
            "entries.catWriteDiary.description",
            default="以猫娘第一人称、软萌的口吻（自然地用「喵～」「的说」「哦～」这类语气词，写给主人看）"
            "把想记的事写进她自己那篇日记（只追加、不覆盖已有内容，也不修改自动生成的日记）；"
            "可顺便带上心情、标签，并把 visibility 设为 private 记为「仅自己可见」（私密的日记她自己也读得到）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
                "text": {
                    "type": "string",
                    "description": tr(
                        "fields.catDiaryText",
                        default="猫娘想记的日记内容，以第一人称、猫娘口吻写（可带「喵～」「的说」等语气词）",
                    ),
                },
                "mood": {
                    "type": "string",
                    "description": tr(
                        "fields.catDiaryMood",
                        default="心情：excited/happy/calm/tired/anxious/sad/angry/neutral（或对应中文），留空用默认心情",
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": tr(
                        "fields.catDiaryTags",
                        default="标签（可选，最多 6 个；也可用逗号分隔的字符串）",
                    ),
                },
                "visibility": {
                    "type": "string",
                    "enum": ["public", "private"],
                    "description": tr(
                        "fields.catDiaryVisibility",
                        default="可见性：public=公开，private=仅自己可见（私密日记她自己仍读得到），留空用默认可见性",
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "replace"],
                    "description": tr(
                        "fields.catDiaryMode",
                        default="写入方式：append=追加到当天末尾（默认），replace=整篇重写当天的日记",
                    ),
                },
            },
            "required": ["text"],
        },
        llm_result_fields=["day", "mode", "added", "chars", "mood", "visibility"],
    )
    async def entry_cat_write_diary(
        self,
        date: str = "",
        text: str = "",
        mood: str = "",
        tags: Any = None,
        visibility: str = "",
        mode: str = "append",
        **_,
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        if not self.main_plugin.cfg.cat_diary_enabled:
            return Err(SdkError("猫娘日记功能未启用（cat_diary_enabled=false）"))
        content = str(text or "").strip()
        if not content:
            return Err(SdkError("text 不能为空"))
        write_mode = str(mode or "append").strip().lower()
        if write_mode not in ("append", "replace"):
            return Err(SdkError("mode 只能是 append（追加）或 replace（整篇重写）"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        mood_key = diary_mod.normalize_mood(mood, self.main_plugin.cfg.cat_diary_default_mood)
        vis = diary_mod.normalize_visibility(visibility, self.main_plugin.cfg.cat_diary_default_visibility)
        tag_list = _as_tag_list(tags)
        if write_mode == "replace":
            result = self.main_plugin.store.replace_cat_diary(
                day, content, mood=mood_key, tags=tag_list, visibility=vis
            )
        else:
            result = self.main_plugin.store.append_cat_diary(
                day, content, mood=mood_key, tags=tag_list, visibility=vis
            )
        self.main_plugin._panel_day = day
        return Ok({"day": day, "mode": write_mode, "mood": mood_key, "visibility": vis, **result})

    @plugin_entry(
        id="cat_read_diary",
        name=tr("entries.catReadDiary.name", default="读取猫娘的日记"),
        description=tr(
            "entries.catReadDiary.description",
            default="读取猫娘自己写的那篇日记（与自动生成的日记分开存放）；"
            "标了「仅自己可见」的私密日记也照读得到。日期留空表示今天。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")}
            },
        },
        llm_result_fields=["day", "exists", "chars"],
    )
    async def entry_cat_read_diary(self, date: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        text = self.main_plugin.store.read_cat_diary(day)
        entries = self.main_plugin.store.cat_diary_entries(day)
        return Ok(
            {
                "day": day,
                "exists": text is not None,
                "chars": len(text or ""),
                "count": len(entries),
                "entries": entries,
                "markdown": text or "",
                "path": str(self.main_plugin.store.cat_diary_path(day)),
            }
        )

    @plugin_entry(
        id="cat_list_days",
        name=tr("entries.catListDays.name", default="列出猫娘日记的日期"),
        description=tr(
            "entries.catListDays.description",
            default="列出猫娘已经写过日记的日期。",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["days"],
    )
    async def entry_cat_list_days(self, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        return Ok({"days": list(reversed(self.main_plugin.store.list_cat_diary_days()))})

    @plugin_entry(
        id="list_cat_diaries",
        name=tr("entries.listCatDiaries.name", default="猫娘日记总览"),
        description=tr(
            "entries.listCatDiaries.description",
            default="一次返回猫娘自己写的日记（按日期倒序），供浏览页「猫娘日记」标签页集中查看；"
            "是否带上标了「仅自己可见」的私密日记由配置 cat_diary_private_readable 决定。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": tr("fields.limitDays", default="最多返回多少天"),
                    "default": 200,
                }
            },
        },
        llm_result_fields=["count"],
    )
    async def entry_list_cat_diaries(self, limit: int = 200, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 200
        cap = max(1, min(cap, 500))
        payload = await asyncio.to_thread(self.main_plugin._cat_diary_overview, cap)
        return Ok(payload)

    @plugin_entry(
        id="cat_search_diary",
        name=tr("entries.catSearchDiary.name", default="检索猫娘日记"),
        description=tr(
            "entries.catSearchDiary.description",
            default="在猫娘自己写的日记里按关键词检索（同时匹配正文、标签与心情），"
            "返回命中的条目与上下文摘要；是否包含「仅自己可见」的私密条目由配置决定。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": tr("fields.catSearchKeyword", default="要查找的关键词"),
                },
                "limit": {
                    "type": "integer",
                    "description": tr("fields.limitDays", default="最多返回多少条"),
                    "default": 50,
                },
            },
            "required": ["keyword"],
        },
        llm_result_fields=["keyword", "count", "results"],
    )
    async def entry_cat_search_diary(self, keyword: str = "", limit: int = 50, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        query = str(keyword or "").strip()
        if not query:
            return Err(SdkError("keyword 不能为空"))
        hits = await asyncio.to_thread(self.main_plugin.store.search_cat_diary, query, limit=limit)
        readable_private = bool(self.main_plugin.cfg.cat_diary_private_readable)
        hidden = 0
        if not readable_private:
            kept = self.main_plugin.store.visible_entries(hits, include_private=False)
            hidden = len(hits) - len(kept)
            hits = kept
        return Ok(
            {
                "keyword": query,
                "count": len(hits),
                "hidden_private": hidden,
                "results": hits,
            }
        )

    @plugin_entry(
        id="cat_diary_stats",
        name=tr("entries.catDiaryStats.name", default="猫娘日记心情统计"),
        description=tr(
            "entries.catDiaryStats.description",
            default="统计最近一段时间猫娘日记的心情分布、常用标签与每日写作情况。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": tr("fields.statsDays", default="统计最近多少天（默认 30）"),
                    "default": 30,
                }
            },
        },
        llm_result_fields=["entries", "diary_days", "moods"],
    )
    async def entry_cat_diary_stats(self, days: int = 30, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        return Ok(await asyncio.to_thread(self.main_plugin.store.cat_diary_stats, days=days))

    @plugin_entry(
        id="cat_diary_throwback",
        name=tr("entries.catDiaryThrowback.name", default="猫娘日记往年今日"),
        description=tr(
            "entries.catDiaryThrowback.description",
            default="找出去年或更早同月同日的猫娘日记，用来一起回忆「往年今日」。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "years": {
                    "type": "integer",
                    "description": tr("fields.throwbackYears", default="最多回看多少年（0 = 不限）"),
                    "default": 0,
                },
            },
        },
        llm_result_fields=["date", "count", "items"],
    )
    async def entry_cat_diary_throwback(self, date: str = "", years: int = 0, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        items = await asyncio.to_thread(self.main_plugin.store.cat_diary_throwback, day=day, years=years)
        if not self.main_plugin.cfg.cat_diary_private_readable:
            for item in items:
                item["entries"] = self.main_plugin.store.visible_entries(
                    item.get("entries") or [], include_private=False
                )
            items = [item for item in items if item["entries"]]
        return Ok({"date": day, "count": len(items), "items": items})

    @plugin_entry(
        id="cat_update_diary_entry",
        name=tr("entries.catUpdateDiaryEntry.name", default="修改猫娘日记的一条"),
        description=tr(
            "entries.catUpdateDiaryEntry.description",
            default="按条目 id 修改猫娘日记中的某一段（正文/心情/标签/可见性，只改传进来的字段）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "entry_id": {
                    "type": "string",
                    "description": tr("fields.catEntryId", default="条目的 id"),
                },
                "text": {
                    "type": "string",
                    "description": tr("fields.catEntryText", default="新的正文（留空表示不修改）"),
                },
                "mood": {
                    "type": "string",
                    "description": tr("fields.catDiaryMood", default="新的心情（留空表示不修改）"),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": tr("fields.catDiaryTags", default="新的标签（留空表示不修改）"),
                },
                "visibility": {
                    "type": "string",
                    "enum": ["public", "private"],
                    "description": tr("fields.catDiaryVisibility", default="新的可见性（留空表示不修改）"),
                },
            },
            "required": ["entry_id"],
        },
        llm_result_fields=["updated", "day"],
    )
    async def entry_cat_update_diary_entry(
        self,
        date: str = "",
        entry_id: str = "",
        text: str = "",
        mood: str = "",
        tags: Any = None,
        visibility: str = "",
        **_,
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        result = self.main_plugin.store.update_cat_diary_entry(
            day,
            entry_id,
            text=str(text) if str(text or "").strip() else None,
            mood=mood or None,
            tags=_as_tag_list(tags) if tags is not None else None,
            visibility=visibility or None,
        )
        if not result.get("updated"):
            reason = result.get("reason") or "unknown"
            return Err(SdkError(f"没有修改成功：{reason}"))
        return Ok({"day": day, **result})

    @plugin_entry(
        id="cat_delete_diary_entry",
        name=tr("entries.catDeleteDiaryEntry.name", default="删除猫娘日记的一条"),
        description=tr(
            "entries.catDeleteDiaryEntry.description",
            default="按条目 id 删除猫娘日记中的某一段；默认软删除（保留历史），permanent=true 时彻底移除。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "entry_id": {
                    "type": "string",
                    "description": tr("fields.catEntryId", default="条目的 id"),
                },
                "permanent": {
                    "type": "boolean",
                    "description": tr("fields.catEntryPermanent", default="是否彻底删除（默认 false = 软删除）"),
                    "default": False,
                },
            },
            "required": ["entry_id"],
        },
        llm_result_fields=["deleted", "day"],
    )
    async def entry_cat_delete_diary_entry(
        self, date: str = "", entry_id: str = "", permanent: bool = False, **_
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        result = self.main_plugin.store.delete_cat_diary_entry(day, entry_id, permanent=bool(permanent))
        if not result.get("deleted"):
            reason = result.get("reason") or "unknown"
            return Err(SdkError(f"没有删除成功：{reason}"))
        return Ok({"day": day, **result})
