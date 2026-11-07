"""章节、每日日记、备忘录与回忆录相关的入口。"""

from __future__ import annotations

import asyncio
import time

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr, ui
from plugin.sdk.shared.core.router import PluginRouter

from .. import _chinese_to_int
from ..chronicle import llm as llm_mod
from ..chronicle import memo as memo_mod
from ..chronicle import reader as reader_mod
from ..chronicle.models import local_day_key


class ChronicleRouter(PluginRouter):
    """章节、每日日记、备忘录与回忆录相关的入口。"""

    def __init__(self):
        super().__init__(name="chronicle")

    @ui.action(
        label=tr("actions.harvestNow.label", default="立即补录"),
        tone="primary",
        group="chronicle",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="harvest_now",
        name=tr("entries.harvestNow.name", default="立即补录对话"),
        description=tr(
            "entries.harvestNow.description",
            default="立刻从消息总线读一次，把还没落盘的对话补进当天记录。",
        ),
        llm_result_fields=["added", "polled"],
    )
    async def entry_harvest_now(self, **_):
        result = await self.main_plugin._harvest_from_bus()
        return Ok(result)

    @ui.action(
        label=tr("actions.buildDay.label", default="生成所选日期"),
        tone="primary",
        group="chronicle",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="build_day",
        name=tr("entries.buildDay.name", default="生成某一天的章节与每日日记"),
        description=tr(
            "entries.buildDay.description",
            default=(
                "按日期重新生成章节；with_diary 为真时同时写自动的每日日记（「爱妻日记」）。"
                "留空日期表示今天。这与猫娘自己写的日记是分开的两篇。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "force": {
                    "type": "boolean",
                    "description": tr("fields.force", default="覆盖已生成的章节与日记"),
                    "default": True,
                },
                "with_llm": {
                    "type": "boolean",
                    "description": tr("fields.withLlm", default="允许调用模型写日记"),
                    "default": True,
                },
                "with_diary": {
                    "type": "boolean",
                    "description": tr("fields.withDiary", default="是否同时生成每日日记"),
                    "default": False,
                },
            },
        },
        llm_result_fields=["day", "turns", "chapter", "diary"],
    )
    async def entry_build_day(
        self,
        date: str = "",
        force: bool = True,
        with_llm: bool = True,
        with_diary: bool = False,
        **_,
    ):
        day = self.main_plugin._normalize_day(date) or local_day_key()
        result = await self.main_plugin._build_day(
            day, with_diary=bool(with_diary), use_llm=bool(with_llm), force=bool(force)
        )
        self.main_plugin._panel_day = day
        return Ok(result)

    @ui.action(
        label=tr("actions.buildToday.label", default="生成今天的章节"),
        tone="success",
        group="chronicle",
        order=30,
        refresh_context=True,
    )
    @plugin_entry(
        id="build_today",
        name=tr("entries.buildToday.name", default="生成今天的章节"),
        description=tr(
            "entries.buildToday.description",
            default="把今天的对话整理成章节；with_diary 为真时同时写今天的日记。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "with_diary": {
                    "type": "boolean",
                    "description": tr("fields.withDiary", default="是否同时生成日记"),
                    "default": False,
                }
            },
        },
        llm_result_fields=["day", "turns", "chapter", "diary"],
    )
    async def entry_build_today(self, with_diary: bool = False, **_):
        day = local_day_key()
        result = await self.main_plugin._build_day(
            day, with_diary=bool(with_diary), use_llm=bool(with_diary), force=True
        )
        self.main_plugin._panel_day = day
        return Ok(result)

    @ui.action(
        label=tr("actions.buildAll.label", default="重算全部日期"),
        tone="warning",
        group="chronicle",
        order=40,
        confirm=tr("actions.buildAll.confirm", default="将重新生成所有章节与日记，确定继续吗？"),
        refresh_context=True,
    )
    @plugin_entry(
        id="build_all",
        name=tr("entries.buildAll.name", default="重算全部日期"),
        description=tr(
            "entries.buildAll.description",
            default="按原始记录重新生成所有章节；with_diary 为真时按配置重新生成日记。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "with_diary": {
                    "type": "boolean",
                    "description": tr("fields.withDiary", default="是否同时生成日记"),
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": tr("fields.limitDays", default="最多处理多少天（0 表示全部）"),
                    "default": 0,
                },
            },
        },
        timeout=600.0,
        llm_result_fields=["days", "failed"],
    )
    async def entry_build_all(self, with_diary: bool = False, limit: int = 0, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        days = self.main_plugin.store.list_raw_days()
        targeted = days[-int(limit):] if int(limit) > 0 else days
        done: list[dict] = []
        failed: list[dict] = []
        for order, day in enumerate(targeted, start=1):
            self.main_plugin.report_status(
                {
                    "status": "processing",
                    "progress": int(order / max(1, len(targeted)) * 100),
                    "message": f"生成 {day}（{order}/{len(targeted)}）",
                }
            )
            try:
                done.append(
                    await self.main_plugin._build_day(
                        day,
                        with_diary=bool(with_diary),
                        use_llm=bool(with_diary),
                        force=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单天失败不影响其余
                self.main_plugin.logger.warning("生成 {} 失败：{}", day, exc)
                failed.append({"day": day, "error": type(exc).__name__})
        return Ok({"days": len(done), "failed": failed, "results": done[-5:]})

    @ui.action(
        label=tr("actions.generateDiary.label", default="立即生成日记"),
        tone="primary",
        group="chronicle",
        order=25,
        refresh_context=True,
    )
    @plugin_entry(
        id="generate_diary",
        name=tr("entries.generateDiary.name", default="立即生成每日日记"),
        description=tr(
            "entries.generateDiary.description",
            default=(
                "立即为某一天生成自动的每日日记（「爱妻日记」，以猫娘第一人称写当天发生的事与心情）。"
                "日期留空为今天。这是独立的每日日记，不是猫娘自己写的那篇。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                }
            },
        },
        llm_result_fields=["day", "diary"],
    )
    async def entry_generate_diary(self, date: str = "", **_):
        day = self.main_plugin._normalize_day(date) or local_day_key()
        result = await self.main_plugin._build_day(day, with_diary=True, use_llm=True, force=True)
        self.main_plugin._panel_day = day
        return Ok(result)

    @ui.action(
        label=tr("actions.toggleMemory.label", default="收藏 / 取消收藏"),
        tone="default",
        group="chronicle",
        order=60,
        refresh_context=False,
    )
    @plugin_entry(
        id="toggle_memory",
        name=tr("entries.toggleMemory.name", default="收藏 / 取消收藏一条对话"),
        description=tr(
            "entries.toggleMemory.description",
            default="按日期+序号把一条对话收藏进回忆录，或取消收藏。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD）"),
                },
                "index": {
                    "type": "integer",
                    "description": tr("fields.index", default="当天第几条"),
                },
                "on": {
                    "type": "boolean",
                    "description": tr("fields.memoryOn", default="true 收藏 / false 取消；缺省按当前状态翻转"),
                },
            },
            "required": ["date", "index"],
        },
        llm_result_fields=["saved", "total", "date", "index"],
    )
    async def entry_toggle_memory(self, date: str = "", index: int = 0, on: bool | None = None, **_):
        if self.main_plugin.store is None or self.main_plugin.memories is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date)
        wanted = _chinese_to_int(index) or 0  # 兼容"13""第十三"
        if not day or wanted <= 0:
            return Err(SdkError("需要有效的日期和序号"))
        rows = reader_mod.day_rows(
            self.main_plugin._turns_for(day),
            master_name=self.main_plugin.cfg.master_name,
            catgirl_name=self.main_plugin.cfg.catgirl_name,
            text_limit=0,
        )
        row = next((item for item in rows if int(item["index"]) == wanted), None)
        if row is None:
            return Err(SdkError(f"{day} 没有第 {wanted} 条记录"))
        result = self.main_plugin.memories.toggle(row, on=on)
        return Ok({**result, "date": day, "index": wanted})

    @plugin_entry(
        id="memories",
        name=tr("entries.memories.name", default="查看回忆录"),
        description=tr(
            "entries.memories.description",
            default="列出所有收藏的对话（回忆录）。",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["count", "items"],
    )
    async def entry_memories(self, **_):
        if self.main_plugin.memories is None:
            return Err(SdkError("插件尚未初始化完成"))
        items = self.main_plugin.memories.list()
        return Ok({"count": len(items), "items": items})

    @ui.action(
        label=tr("actions.fetchMemoNow.label", default="立即抓取备忘录"),
        tone="default",
        group="memo",
        refresh_context=True,
    )
    @plugin_entry(
        id="fetch_memo_now",
        name=tr("entries.fetchMemoNow.name", default="立即抓取备忘录"),
        description=tr(
            "entries.fetchMemoNow.description",
            default="立刻读一次宿主的 system 备忘录，把新增段追加进当天备忘录。",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["day", "added", "tail"],
    )
    async def entry_fetch_memo_now(self, **_):
        return Ok(await self.main_plugin._fetch_memo_once())

    @ui.action(
        label=tr("actions.saveMemo.label", default="保存备忘录"),
        tone="primary",
        group="memo",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_memo",
        name=tr("entries.saveMemo.name", default="保存备忘录"),
        description=tr(
            "entries.saveMemo.description",
            default="整体写回某天的备忘录（可修改内容）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
                "text": {"type": "string", "description": tr("fields.memoText", default="备忘录全文")},
            },
        },
        llm_result_fields=["date", "saved"],
    )
    async def entry_save_memo(self, date: str = "", text: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        memo_mod.write(self.main_plugin.store, day, str(text or ""))
        return Ok({"date": day, "saved": True})

    @ui.action(
        label=tr("actions.saveTitle.label", default="保存章节标题"),
        tone="default",
        group="memo",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_title",
        name=tr("entries.saveTitle.name", default="保存章节标题"),
        description=tr(
            "entries.saveTitle.description",
            default="写回某天的章节标题（留空则下次自动从备忘录取随机一条）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
                "text": {"type": "string", "description": tr("fields.titleText", default="章节标题")},
            },
        },
        llm_result_fields=["date", "saved"],
    )
    async def entry_save_title(self, date: str = "", text: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        memo_mod.write_title(self.main_plugin.store, day, str(text or ""))
        return Ok({"date": day, "saved": True})

    @ui.action(
        label=tr("actions.saveDiary.label", default="保存日记"),
        tone="primary",
        group="chronicle",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_diary",
        name=tr("entries.saveDiary.name", default="保存每日日记"),
        description=tr(
            "entries.saveDiary.description",
            default=(
                "写回某天自动生成的每日日记正文（供界面编辑或校正）。"
                "⚠️ 猫娘想写自己的日记时请用 cat_write_diary，不要用本入口。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
                "text": {"type": "string", "description": tr("fields.diaryText", default="日记正文")},
            },
        },
        llm_result_fields=["date", "saved"],
    )
    async def entry_save_diary(self, date: str = "", text: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        self.main_plugin.store.write_diary(day, str(text or ""))
        return Ok({"date": day, "saved": True})

    @plugin_entry(
        id="memo_state",
        name=tr("entries.memoState.name", default="读取备忘录与标题"),
        description=tr(
            "entries.memoState.description",
            default="读取某天的备忘录、章节标题与日记正文（供界面编辑）。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
            },
        },
        llm_result_fields=["date", "memo", "title", "diary"],
    )
    async def entry_memo_state(self, date: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        return Ok(
            {
                "date": day,
                "memo": memo_mod.read(self.main_plugin.store, day),
                "title": memo_mod.read_title(self.main_plugin.store, day),
                "diary": self.main_plugin.store.read_diary(day) or "",
                "tidied_at": memo_mod.read_tidy_mark(self.main_plugin.store, day),
            }
        )

    @plugin_entry(
        id="tidy_memo",
        name=tr("entries.tidyMemo.name", default="整理备忘录"),
        description=tr(
            "entries.tidyMemo.description",
            default="用配置好的模型整理某天的备忘录：合并重复条目、去掉口水话、理顺语句，"
            "整理结果直接写回当天备忘录。需要先在「模型配置」里配置可用模型。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
                "text": {
                    "type": "string",
                    "description": tr(
                        "fields.tidyText",
                        default="要整理的备忘录文本；留空则用该日期已保存的内容",
                    ),
                },
            },
        },
        llm_result_fields=["date", "before_chars", "after_chars", "attempts"],
        timeout=120.0,
    )
    async def entry_tidy_memo(self, date: str = "", text: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or local_day_key()
        memo_text = str(text or "").strip() or memo_mod.read(self.main_plugin.store, day).strip()
        if not memo_text:
            return Err(SdkError("这一天的备忘录还是空的，先抓取或写点内容再整理吧"))
        self.main_plugin._refresh_llm()
        if self.main_plugin.llm_endpoint is None:
            return Err(
                SdkError("还没有可用的模型配置，请先在「模型配置」里填写 API 地址和密钥")
            )
        cleaned, error, attempts_used = await self.main_plugin._tidy_memo_content(day, memo_text)
        if not cleaned:
            if error == "too_different":
                return Err(
                    SdkError("模型返回的内容和原备忘录差异太大，已取消整理，备忘录未改动")
                )
            if error == "empty":
                return Err(SdkError("模型返回了空内容，备忘录未改动"))
            return Err(
                SdkError(
                    f"模型请求失败（{llm_mod.reason_text(error)}，共尝试 {attempts_used} 次），"
                    "备忘录未改动"
                )
            )
        memo_mod.write(self.main_plugin.store, day, cleaned)
        tidied_at = time.strftime("%Y-%m-%d %H:%M")
        memo_mod.write_tidy_mark(self.main_plugin.store, day, tidied_at)
        self.main_plugin.logger.info(
            "备忘录已整理：{}（{} → {} 字，第 {} 次尝试）",
            day,
            len(memo_text),
            len(cleaned),
            attempts_used,
        )
        return Ok(
            {
                "date": day,
                "before_chars": len(memo_text),
                "after_chars": len(cleaned),
                "text": cleaned,
                "tidied_at": tidied_at,
                "attempts": attempts_used,
            }
        )

    @plugin_entry(
        id="list_days",
        name=tr("entries.listDays.name", default="列出已有日期"),
        description=tr(
            "entries.listDays.description",
            default="列出已经留下记录的日期，以及每天是否已有章节和日记。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": tr("fields.limitDays", default="最多返回多少天（0 = 全部）"),
                    "default": 30,
                }
            },
        },
        llm_result_fields=["days"],
    )
    async def entry_list_days(self, limit: int = 30, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        return Ok({"days": await asyncio.to_thread(self.main_plugin._day_cards, limit=int(limit))})

    @plugin_entry(
        id="read_chapter",
        name=tr("entries.readChapter.name", default="读取某天的章节"),
        description=tr(
            "entries.readChapter.description",
            default="读取某一天的小说式章节正文。日期留空表示今天。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")}
            },
        },
        # 正文可能很长，默认不塞进模型上下文；上面的 llm_result_fields 只放元信息
        llm_result_fields=["day", "exists", "chars"],
    )
    async def entry_read_chapter(self, date: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        text = self.main_plugin.store.read_chapter(day)
        return Ok(
            {
                "day": day,
                "exists": text is not None,
                "chars": len(text or ""),
                "markdown": text or "",
                "path": str(self.main_plugin.store.chapter_path(day)),
            }
        )

    @plugin_entry(
        id="read_diary",
        name=tr("entries.readDiary.name", default="读取某天的每日日记"),
        description=tr(
            "entries.readDiary.description",
            default=(
                "读取自动生成的每日日记（也就是「爱妻日记」）。日期留空表示今天。"
                "⚠️ 对话里主人说「日记」通常指猫娘自己那篇，请用 cat_read_diary；"
                "只有主人明确发送「爱妻日记」+日期时才用本入口。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")}
            },
        },
        llm_result_fields=["day", "exists", "chars"],
    )
    async def entry_read_diary(self, date: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        text = self.main_plugin.store.read_diary(day)
        return Ok(
            {
                "day": day,
                "exists": text is not None,
                "chars": len(text or ""),
                "markdown": text or "",
                "path": str(self.main_plugin.store.diary_path(day)),
            }
        )

    @ui.action(
        label=tr("actions.selectDay.label", default="查看这一天"),
        tone="primary",
        group="chronicle",
        order=1,
        refresh_context=True,
    )
    @plugin_entry(
        id="select_day",
        name=tr("entries.selectDay.name", default="在面板中查看某一天"),
        description=tr("entries.selectDay.description", default="把面板切换到指定日期。"),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")}
            },
            "required": ["date"],
        },
        llm_result_fields=["selected"],
    )
    async def entry_select_day(self, date: str = "", **_):
        day = self.main_plugin._normalize_day(date)
        if not day:
            return Err(SdkError("日期格式应为 YYYY-MM-DD"))
        self.main_plugin._panel_day = day
        return Ok({"selected": day})
