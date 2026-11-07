"""时间胶囊入口：主人封存、列出、到期后读取。

正文在到期前不会下发；猫娘写入走 `@llm_tool`（author=catgirl）。
"""

from __future__ import annotations

import time

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr, ui
from plugin.sdk.shared.core.router import PluginRouter

from ..chronicle import capsule as capsule_mod
from ..chronicle.models import local_day_key


class CapsuleRouter(PluginRouter):
    """时间胶囊：写给未来某一天的话。"""

    def __init__(self):
        super().__init__(name="capsule")

    @ui.action(
        label=tr("actions.capsuleWrite.label", default="封存时间胶囊"),
        tone="primary",
        group="capsule",
        order=70,
        refresh_context=True,
    )
    @plugin_entry(
        id="capsule_write",
        name=tr("entries.capsuleWrite.name", default="封存一枚时间胶囊"),
        description=tr(
            "entries.capsuleWrite.description",
            default="把一段话封存到未来某一天；到期前只显示解锁日期，到期后才能读到正文。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": tr("fields.capsuleText", default="胶囊正文（最多 4000 字）"),
                },
                "unlock_date": {
                    "type": "string",
                    "description": tr(
                        "fields.capsuleUnlock",
                        default="解锁日期（YYYY-MM-DD，或今天/明天/后天）",
                    ),
                },
            },
            "required": ["text", "unlock_date"],
        },
        llm_result_fields=["id", "unlock_day", "chars", "locked"],
    )
    async def entry_capsule_write(self, text: str = "", unlock_date: str = "", **_):
        plugin = self.main_plugin
        if plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = plugin._normalize_day(unlock_date)
        if not day:
            return Err(SdkError("解锁日期格式应为 YYYY-MM-DD，或今天/明天/后天"))
        record, error = capsule_mod.write(
            plugin.store, text=text, unlock_day=day, author="master"
        )
        if record is None:
            return Err(SdkError(_write_error_text(error)))
        return Ok(record)

    @plugin_entry(
        id="capsule_list",
        name=tr("entries.capsuleList.name", default="列出时间胶囊"),
        description=tr(
            "entries.capsuleList.description",
            default="列出全部时间胶囊；未到期的只返回日期与字数，不返回正文。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": tr("fields.capsuleLimit", default="最多返回多少枚（0 = 全部）"),
                    "default": 50,
                },
                "include_locked": {
                    "type": "boolean",
                    "description": tr("fields.capsuleIncludeLocked", default="是否包含未到期的胶囊"),
                    "default": True,
                },
                "author": {
                    "type": "string",
                    "description": tr(
                        "fields.capsuleAuthor",
                        default="只看某个作者：master（主人）或 catgirl（猫娘）；留空看全部",
                    ),
                },
            },
        },
        llm_result_fields=[
            "catgirl_count",
            "master_count",
            "count",
            "due_today",
            "items",
        ],
    )
    async def entry_capsule_list(
        self, limit: int = 50, include_locked: bool = True, author: str = "", **_
    ):
        plugin = self.main_plugin
        if plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        today = local_day_key()
        items = capsule_mod.list_all(
            plugin.store,
            today=today,
            include_text=True,
            include_locked=bool(include_locked),
            limit=int(limit or 0),
        )
        who = str(author or "").strip()
        if who:
            if who not in capsule_mod.CAPSULE_AUTHORS:
                return Err(SdkError("author 只能是 master 或 catgirl"))
            items = [item for item in items if item["author"] == who]
        counts = capsule_mod.author_counts(items)
        return Ok(
            {
                "today": today,
                "count": len(items),
                "master_count": counts["master"],
                "catgirl_count": counts["catgirl"],
                "due_today": sum(
                    1 for item in items if item["unlock_day"] == today and not item["locked"]
                ),
                "items": items,
            }
        )

    @ui.action(
        label=tr("actions.capsuleNudge.label", default="提醒猫娘补写"),
        tone="primary",
        group="capsule",
        order=71,
        refresh_context=True,
    )
    @plugin_entry(
        id="capsule_nudge",
        name=tr("entries.capsuleNudge.name", default="提醒猫娘补写时间胶囊"),
        description=tr(
            "entries.capsuleNudge.description",
            default=(
                "检查猫娘是否封存过胶囊：她一枚都没有时，把补写提醒直接递给她（她会开口回应），"
                "已经写过则不打扰。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "unlock_date": {
                    "type": "string",
                    "description": tr(
                        "fields.capsuleUnlock",
                        default="解锁日期（YYYY-MM-DD，或今天/明天/后天）；留空建议一个月后",
                    ),
                },
            },
        },
        llm_result_fields=["catgirl_count", "pushed", "unlock_day"],
    )
    async def entry_capsule_nudge(self, unlock_date: str = "", **_):
        plugin = self.main_plugin
        if plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        today = local_day_key()
        items = capsule_mod.list_all(
            plugin.store, today=today, include_text=False, include_locked=True
        )
        counts = capsule_mod.author_counts(items)
        if counts["catgirl"]:
            return Ok(
                {
                    "catgirl_count": counts["catgirl"],
                    "pushed": False,
                    "message": "她已经写过时间胶囊了",
                }
            )
        day = ""
        if str(unlock_date or "").strip():
            day = plugin._normalize_day(unlock_date)
            if not day:
                return Err(SdkError("解锁日期格式应为 YYYY-MM-DD，或今天/明天/后天"))
        else:
            day = time.strftime(
                "%Y-%m-%d", time.localtime(time.time() + 30 * 86400)
            )
        message = (
            "【时间胶囊提醒】主人刚查过插件：你还没有封存过任何时间胶囊，"
            "目录里只有主人自己写的那枚（master），不是你的。"
            f"现在调用 chronicle_write_capsule 补写一枚，以你自己的口吻写给未来的主人和自己，"
            f"解锁日填 {day}。写完后把工具返回的 unlock_day 告诉主人——"
            "不要只说「写好了」却不调用工具。"
        )
        pushed = plugin._push_to_catgirl(message, respond=True)
        return Ok({"catgirl_count": 0, "pushed": pushed, "unlock_day": day})

    @plugin_entry(
        id="capsule_read",
        name=tr("entries.capsuleRead.name", default="打开时间胶囊"),
        description=tr(
            "entries.capsuleRead.description",
            default="按 id 或解锁日期读取已到期的时间胶囊；未到期会提示还要等多久。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": tr("fields.capsuleId", default="胶囊 id（从 capsule_list 取）"),
                },
                "date": {
                    "type": "string",
                    "description": tr(
                        "fields.capsuleUnlock",
                        default="解锁日期（YYYY-MM-DD，或今天/明天/后天）",
                    ),
                },
            },
        },
        llm_result_fields=["count", "items"],
    )
    async def entry_capsule_read(self, id: str = "", date: str = "", **_):
        plugin = self.main_plugin
        if plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        today = local_day_key()
        if str(id or "").strip():
            item, error = capsule_mod.read(plugin.store, id, today=today)
            if item is None:
                return Err(SdkError("没有找到这枚时间胶囊"))
            if error == "locked":
                return Err(
                    SdkError(
                        f"这枚胶囊要到 {item['unlock_day']} 才能打开"
                        f"（还有 {item['days_left']} 天）"
                    )
                )
            return Ok({"count": 1, "items": [item]})

        day = plugin._normalize_day(date)
        if not day:
            return Err(SdkError("需要提供胶囊 id 或解锁日期"))
        items = capsule_mod.list_all(
            plugin.store, today=today, include_text=True, include_locked=True
        )
        matched = [item for item in items if item["unlock_day"] == day]
        if not matched:
            return Err(SdkError(f"{day} 没有时间胶囊"))
        locked = [item for item in matched if item["locked"]]
        if locked and len(locked) == len(matched):
            return Err(
                SdkError(f"{day} 的胶囊还没到期（还有 {locked[0]['days_left']} 天）")
            )
        return Ok(
            {
                "count": len([item for item in matched if not item["locked"]]),
                "items": [item for item in matched if not item["locked"]],
            }
        )


def _write_error_text(error: str) -> str:
    return {
        "empty_text": "胶囊正文不能为空",
        "too_long": f"胶囊正文最多 {capsule_mod.CAPSULE_MAX_CHARS} 字",
        "bad_date": "解锁日期格式应为 YYYY-MM-DD",
        "unlock_in_past": "解锁日期不能早于今天",
        "bad_author": "作者只能是主人或猫娘",
    }.get(error, f"封存失败：{error}")
