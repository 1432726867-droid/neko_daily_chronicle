"""放映厅：和猫娘一起回看某一天对话的入口。

资源策略：没有定时器、没有启动扫描。只有打开放映页或猫娘调用放映工具时，
才会读写 ``screenings/<日期>.json``；关闭功能后所有入口直接返回，不产生 I/O。
"""

from __future__ import annotations

import asyncio
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr
from plugin.sdk.shared.core.router import PluginRouter

from .. import IMAGE_EXTS
from ..chronicle import diary as diary_mod
from ..chronicle import llm as llm_mod
from ..chronicle import reader as reader_mod
from ..chronicle import screening as screening_mod
from ..chronicle import screening_review as review_mod
from ..chronicle.models import local_day_key


class ScreeningRouter(PluginRouter):
    """放映厅的状态、控制、手记与“让她说”入口。"""

    def __init__(self):
        super().__init__(name="screening")
        #: day -> (目录 mtime, 图片文件名列表)，避免自动播放心跳反复扫图片目录。
        self._cover_cache: dict[str, tuple[int, list[str]]] = {}

    # ── 内部 helper ────────────────────────────────────────────────

    def _require_ready(self) -> str | None:
        plugin = self.main_plugin
        if plugin.store is None:
            return "插件尚未初始化完成"
        if not plugin.cfg.screening_enabled:
            return "放映厅未启用，请先在插件配置里打开 screening_enabled"
        return None

    def _day(self, value: Any) -> str:
        return self.main_plugin._normalize_day(value) or local_day_key()

    async def _turns(self, day: str) -> list[Any]:
        return await asyncio.to_thread(self.main_plugin._turns_for, day)

    def _disabled_payload(self, day: str) -> dict:
        """关闭放映厅时的最小返回；界面据此把入口藏起来，不读任何状态文件。"""

        plugin = self.main_plugin
        return {
            "enabled": False,
            "date": day,
            "total": 0,
            "session": None,
            "progress": {},
            "scenes": [],
            "cover": {},
            "review": None,
            "items": [],
            "start": 0,
            "page_size": max(10, min(int(plugin.cfg.screening_page_size or 60), 200)),
            "auto_interval": float(plugin.cfg.screening_auto_interval or 2.5),
            "names": {
                "master": plugin.cfg.master_name,
                "catgirl": plugin.cfg.catgirl_name,
            },
            "changed": False,
            "ignored": False,
        }

    def _session(self, day: str, total: int) -> dict:
        raw = self.main_plugin.store.load_screening(day)
        session = screening_mod.normalize_session(raw, day, total)
        if not raw:
            session["mode"] = self.main_plugin.cfg.screening_default_mode
        return session

    def _scenes(self, turns: list[Any]) -> list[dict]:
        gap = float(self.main_plugin.cfg.scene_gap_minutes or 0) * 60.0
        return screening_mod.build_scenes(turns, gap)

    def _cover_files(self, day: str) -> list[str]:
        """当天章节图集里的图片文件名（按目录 mtime 缓存，只读文件名不读图片）。"""

        if not day:
            return []
        root = self.main_plugin._ensure_images_dir()
        if root is None:
            return []
        directory = root / day
        try:
            stamp = directory.stat().st_mtime_ns
        except OSError:
            return []
        cached = self._cover_cache.get(day)
        if cached is not None and cached[0] == stamp:
            return list(cached[1])
        try:
            files = sorted(
                path.name
                for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_EXTS
            )
        except OSError:
            files = []
        self._cover_cache[day] = (stamp, files)
        while len(self._cover_cache) > 32:
            self._cover_cache.pop(next(iter(self._cover_cache)))
        return list(files)

    def _cover_info(self, day: str, session: dict, progress: dict) -> dict:
        """当前场景的封面：优先用手动指定，否则按场景顺序轮换当天图片。"""

        scene_no = int(progress.get("scene_no") or 0) or 1
        files = self._cover_files(day)
        covers = session.get("covers")
        if not isinstance(covers, dict):
            covers = {}
        filename = str(covers.get(str(scene_no)) or "")
        if filename not in files:
            filename = ""
        manual = bool(filename)
        if not filename and files:
            filename = files[(scene_no - 1) % len(files)]
        return {
            "scene_no": scene_no,
            "filename": filename,
            "manual": manual,
            "candidates": files[:60],
        }

    def _save(self, day: str, session: dict) -> None:
        self.main_plugin.store.save_screening(day, session)
        self.main_plugin._screening_active_day = day

    def _payload(
        self,
        day: str,
        session: dict,
        turns: list[Any],
        *,
        include_rows: bool = True,
        start: int = 0,
        count: int = 0,
        changed: bool = False,
        ignored: bool = False,
    ) -> dict:
        plugin = self.main_plugin
        total = len(turns)
        session = screening_mod.normalize_session(session, day, total)
        # 界面/轮询只看最近的手记；完整历史仍留在磁盘里，避免每次 3 秒轮询
        # 把一个很大的 JSON 传回前端。
        public_session = dict(session)
        public_session["marks"] = (session.get("marks") or [])[-30:]
        public_session["reviews"] = (session.get("reviews") or [])[-5:]
        scenes = self._scenes(turns)
        progress = screening_mod.progress(public_session, total, scenes)
        cover = self._cover_info(day, session, progress)
        page_size = max(10, min(int(plugin.cfg.screening_page_size or 60), 200))
        if include_rows:
            if int(start or 0) <= 0:
                cursor = int(session.get("cursor") or 0)
                start = max(1, cursor - page_size // 2)
            rows = reader_mod.day_rows(
                turns,
                master_name=plugin.cfg.master_name,
                catgirl_name=plugin.cfg.catgirl_name,
                start=max(1, int(start or 1)),
                count=max(1, min(int(count or page_size), 400)),
                text_limit=screening_mod.SCREENING_TEXT_LIMIT,
            )
        else:
            rows = []
            start = 0
        return {
            "enabled": bool(plugin.cfg.screening_enabled),
            "date": day,
            "total": total,
            "session": public_session,
            "progress": progress,
            "scenes": scenes,
            "cover": cover,
            "review": (public_session.get("reviews") or [None])[-1],
            "items": rows,
            "start": int(start or 0),
            "page_size": page_size,
            "auto_interval": float(plugin.cfg.screening_auto_interval or 2.5),
            "names": {
                "master": plugin.cfg.master_name,
                "catgirl": plugin.cfg.catgirl_name,
            },
            "changed": bool(changed),
            "ignored": bool(ignored),
        }

    def _cue_text(self, session: dict, rows: list[dict], scene_label: str) -> str:
        day = str(session.get("day") or "")
        cursor = int(session.get("cursor") or 0)
        lines = [
            "放映厅 · 一起回顾",
            f"日期：{day}",
            f"看到：第 {cursor} 条" + (f"（{scene_label}）" if scene_label else ""),
            "",
            "刚才这一小段是：",
            reader_mod.describe_rows(rows),
            "",
            "主人按下了暂停，想听你说说这一段。你想说什么就在聊天里自然地说；",
            "想把停靠位置记下来可以调用 chronicle_screening_comment；",
            "准备好继续时调用 chronicle_screening_resume。",
        ]
        return "\n".join(line for line in lines if line is not None)

    # ── 入口 ────────────────────────────────────────────────────────

    @plugin_entry(
        id="screening_state",
        name=tr("entries.screeningState.name", default="放映厅状态"),
        description=tr(
            "entries.screeningState.description",
            default="读取某天的放映状态：看到第几条、猫娘看到第几条、停靠点与手记。",
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
        llm_result_fields=["date", "total", "session", "progress", "scenes"],
    )
    async def entry_screening_state(self, date: str = "", **_):
        plugin = self.main_plugin
        if plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self._day(date)
        if not plugin.cfg.screening_enabled:
            return Ok(self._disabled_payload(day))
        turns = await self._turns(day)
        session = await asyncio.to_thread(self._session, day, len(turns))
        return Ok(self._payload(day, session, turns, include_rows=False))

    @plugin_entry(
        id="screening_read",
        name=tr("entries.screeningRead.name", default="读取放映厅当前画面"),
        description=tr(
            "entries.screeningRead.description",
            default="按窗口读取放映厅当前页码附近的对话原文，供大窗/小窗渲染。",
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
                    "description": tr("fields.start", default="从第几条开始；0 = 自动围绕当前进度"),
                },
                "count": {
                    "type": "integer",
                    "description": tr("fields.count", default="要几条；0 = 用插件配置"),
                },
            },
        },
        llm_result_fields=["date", "total", "items", "start", "progress"],
    )
    async def entry_screening_read(
        self, date: str = "", start: int = 0, count: int = 0, **_
    ):
        error = self._require_ready()
        if error:
            return Err(SdkError(error))
        day = self._day(date)
        turns = await self._turns(day)
        session = await asyncio.to_thread(self._session, day, len(turns))
        return Ok(
            self._payload(
                day,
                session,
                turns,
                include_rows=True,
                start=int(start or 0),
                count=int(count or 0),
            )
        )

    @plugin_entry(
        id="screening_open",
        name=tr("entries.screeningOpen.name", default="打开放映厅"),
        description=tr(
            "entries.screeningOpen.description",
            default="打开某天的放映厅并返回当前画面；不会自动开始播放。",
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
        llm_result_fields=["date", "total", "session", "progress", "items"],
    )
    async def entry_screening_open(self, date: str = "", **_):
        plugin = self.main_plugin
        if plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self._day(date)
        if not plugin.cfg.screening_enabled:
            return Ok(self._disabled_payload(day))
        turns = await self._turns(day)
        session = await asyncio.to_thread(self._session, day, len(turns))
        # 只有真的有一场正在放映时才挂上“聊天自动暂停”，避免空闲时每次
        # 猫娘说话都去碰一个可能不存在的放映文件。
        raw = await asyncio.to_thread(plugin.store.load_screening, day)
        if raw and str(raw.get("status") or "") == screening_mod.STATUS_PLAYING:
            plugin._screening_active_day = day
        return Ok(self._payload(day, session, turns))

    @plugin_entry(
        id="screening_control",
        name=tr("entries.screeningControl.name", default="控制放映"),
        description=tr(
            "entries.screeningControl.description",
            default="开始/暂停/继续/推进/跳转/从头放映。auto=true 是窗口自动播放心跳。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "action": {
                    "type": "string",
                    "enum": ["resume", "pause", "advance", "seek", "previous", "restart", "finish"],
                    "description": tr("fields.screeningAction", default="要执行的动作"),
                },
                "to": {
                    "type": "integer",
                    "description": tr("fields.screeningIndex", default="跳到/停到第几条（从 1 开始）"),
                },
                "stop_at": {
                    "type": "integer",
                    "description": tr("fields.screeningStop", default="这一场计划看到第几条；0 = 不设终点"),
                },
                "note": {
                    "type": "string",
                    "description": tr("fields.screeningNote", default="暂停时留下的一句话"),
                },
                "count": {
                    "type": "integer",
                    "description": tr("fields.count", default="推进几条，默认 1"),
                },
                "mode": {
                    "type": "string",
                    "enum": ["line", "scene"],
                    "description": tr("fields.screeningMode", default="推进方式：逐句/逐段"),
                },
                "auto": {
                    "type": "boolean",
                    "description": tr("fields.screeningAuto", default="是否为窗口自动播放心跳"),
                },
                "driver": {
                    "type": "string",
                    "description": tr("fields.screeningDriver", default="窗口标识，用于避免大窗小窗重复推进"),
                },
                "who": {
                    "type": "string",
                    "enum": ["master", "catgirl"],
                    "description": tr("fields.screeningWho", default="谁在操作：master/catgirl"),
                },
            },
            "required": ["action"],
        },
        llm_result_fields=["date", "session", "progress", "items", "changed", "ignored"],
    )
    async def entry_screening_control(
        self,
        date: str = "",
        action: str = "",
        to: int = 0,
        stop_at: int = 0,
        note: str = "",
        count: int = 1,
        mode: str = "",
        auto: bool = False,
        driver: str = "",
        who: str = "master",
        **_,
    ):
        error = self._require_ready()
        if error:
            return Err(SdkError(error))
        day = self._day(date)
        turns = await self._turns(day)
        total = len(turns)
        session = await asyncio.to_thread(self._session, day, total)
        scenes = self._scenes(turns)
        changed = False
        ignored = False
        act = str(action or "").strip().lower()
        who_value = who if who in screening_mod.WHO_VALUES else "master"
        mode_value = mode if mode in screening_mod.MODES else session.get("mode") or "line"

        if act == "resume":
            session, changed = screening_mod.resume(
                session, total, driver=str(driver or "")
            )
        elif act == "restart":
            session, changed = screening_mod.resume(
                session, total, driver=str(driver or ""), restart=True
            )
        elif act == "pause":
            session, changed = session, True
            session = screening_mod.pause(
                session,
                total,
                index=int(to) if int(to or 0) > 0 else None,
                note=str(note or ""),
                stop_at=int(stop_at) if int(stop_at or 0) > 0 else None,
                who=who_value,
                reason="catgirl_stop" if who_value == "catgirl" else "manual",
            )
        elif act == "advance":
            driver_mismatch = bool(
                auto and driver and session.get("driver")
                and str(session.get("driver")) != str(driver)
            )
            session, changed = screening_mod.advance(
                session,
                total,
                scenes,
                to=int(to) if int(to or 0) > 0 else None,
                count=int(count or 1),
                mode=mode_value,
                who=who_value,
                auto=bool(auto),
                driver=str(driver or ""),
            )
            ignored = bool(
                auto and not changed
                and (driver_mismatch or str(session.get("status") or "") != "playing")
            )
        elif act == "seek":
            target = int(to or 1)
            session, changed = screening_mod.seek(
                session,
                total,
                scenes,
                target,
                who=who_value,
                driver=str(driver or ""),
            )
        elif act == "previous":
            session, changed = screening_mod.previous_scene(
                session,
                total,
                scenes,
                who=who_value,
                driver=str(driver or ""),
            )
        elif act == "finish":
            session = screening_mod.finish(session)
            changed = True
        else:
            return Err(SdkError("action 必须是 resume/pause/advance/seek/previous/restart/finish"))

        if changed:
            session = await asyncio.to_thread(
                self._save_and_return, day, session
            )
        self.main_plugin._screening_active_day = day
        return Ok(
            self._payload(
                day,
                session,
                turns,
                changed=changed,
                ignored=ignored,
            )
        )

    def _save_and_return(self, day: str, session: dict) -> dict:
        self._save(day, session)
        return session

    @plugin_entry(
        id="screening_comment",
        name=tr("entries.screeningComment.name", default="写放映手记"),
        description=tr(
            "entries.screeningComment.description",
            default="在放映的某一条旁边留一句话；猫娘写手记时会自动把放映停下来。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "text": {
                    "type": "string",
                    "description": tr("fields.screeningNote", default="想留下的那句话"),
                },
                "index": {
                    "type": "integer",
                    "description": tr("fields.screeningIndex", default="写在第几条旁边；0 = 当前位置"),
                },
                "who": {
                    "type": "string",
                    "enum": ["master", "catgirl"],
                    "description": tr("fields.screeningWho", default="谁写的手记"),
                },
            },
            "required": ["text"],
        },
        llm_result_fields=["date", "session", "progress", "items"],
    )
    async def entry_screening_comment(
        self, date: str = "", text: str = "", index: int = 0, who: str = "catgirl", **_
    ):
        error = self._require_ready()
        if error:
            return Err(SdkError(error))
        day = self._day(date)
        turns = await self._turns(day)
        total = len(turns)
        session = await asyncio.to_thread(self._session, day, total)
        target = int(index or 0)
        session, changed = screening_mod.comment(
            session,
            total,
            str(text or ""),
            index=target if target > 0 else None,
            who=who if who in screening_mod.WHO_VALUES else "catgirl",
            auto_pause=True,
        )
        if not changed:
            return Err(SdkError("手记内容不能为空"))
        session = await asyncio.to_thread(self._save_and_return, day, session)
        self.main_plugin._screening_active_day = day
        return Ok(self._payload(day, session, turns))

    @plugin_entry(
        id="screening_invite",
        name=tr("entries.screeningInvite.name", default="请猫娘说说这一段"),
        description=tr(
            "entries.screeningInvite.description",
            default="把放映暂停在她当前的位置，并把刚看完的几句递给她，让她开口说说。",
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
        llm_result_fields=["date", "invited", "session", "progress"],
    )
    async def entry_screening_invite(self, date: str = "", **_):
        error = self._require_ready()
        if error:
            return Err(SdkError(error))
        day = self._day(date)
        turns = await self._turns(day)
        total = len(turns)
        session = await asyncio.to_thread(self._session, day, total)
        session = screening_mod.pause(
            session,
            total,
            who="master",
            reason="invite",
            note="",
            add_mark=False,
        )
        session = await asyncio.to_thread(self._save_and_return, day, session)
        self.main_plugin._screening_active_day = day

        cursor = int(session.get("cursor") or 0)
        rows = reader_mod.day_rows(
            turns,
            master_name=self.main_plugin.cfg.master_name,
            catgirl_name=self.main_plugin.cfg.catgirl_name,
            start=max(1, cursor - 4),
            count=5 if cursor else 3,
            text_limit=screening_mod.SCREENING_TEXT_LIMIT,
        )
        scenes = self._scenes(turns)
        progress = screening_mod.progress(session, total, scenes)
        cue = self._cue_text(session, rows, str(progress.get("scene_label") or ""))
        invited = bool(await asyncio.to_thread(self.main_plugin._push_to_catgirl, cue, respond=True))
        payload = self._payload(day, session, turns, include_rows=True)
        payload["invited"] = invited
        return Ok(payload)

    @plugin_entry(
        id="screening_reflection",
        name=tr("entries.screeningReflection.name", default="写放映观后感"),
        description=tr(
            "entries.screeningReflection.description",
            default="根据已经看到的那一段对话和放映手记，写一篇猫娘口吻的观后感；"
            "默认复用已有观后感，regenerate=true 才重新写。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "regenerate": {
                    "type": "boolean",
                    "description": tr("fields.screeningRegenerate", default="是否重新写（默认复用已有观后感）"),
                },
            },
        },
        llm_result_fields=["date", "review", "generated", "progress"],
    )
    async def entry_screening_reflection(
        self, date: str = "", regenerate: bool = False, **_
    ):
        error = self._require_ready()
        if error:
            return Err(SdkError(error))
        plugin = self.main_plugin
        day = self._day(date)
        turns = await self._turns(day)
        total = len(turns)
        session = await asyncio.to_thread(self._session, day, total)
        watched_to = min(
            int(session.get("cat_cursor") or session.get("cursor") or 0), total
        )
        if watched_to <= 0:
            return Err(SdkError("还没有看到任何一条对话，先放映一段再写观后感吧"))

        reviews = session.get("reviews")
        if isinstance(reviews, list) and reviews and not regenerate:
            payload = self._payload(day, session, turns)
            payload["review"] = reviews[-1]
            payload["generated"] = False
            return Ok(payload)

        # 写观后感时先把放映停住：模型请求可能要几十秒，别让银幕继续跑。
        if str(session.get("status") or "") == screening_mod.STATUS_PLAYING:
            session = screening_mod.pause(
                session, total, reason="review", add_mark=False
            )
            session = await asyncio.to_thread(self._save_and_return, day, session)

        watched = list(turns[:watched_to])
        stats = await asyncio.to_thread(diary_mod.analyze_day, day, watched, plugin.cfg)
        prompt = review_mod.build_prompt(
            day, watched, session, plugin.cfg, stats, watched_to=watched_to
        )
        endpoint = plugin.llm_endpoint
        text: str | None = None
        reason = plugin.llm_reason or "no_config"
        model = ""
        if endpoint is not None:
            model = endpoint.model
            text, reason, _attempts = await llm_mod.achat_completion_with_retry(
                endpoint,
                prompt,
                attempts=int(plugin.cfg.llm_retry_attempts or 3),
                timeout=min(float(plugin.cfg.llm_timeout_seconds or 60.0), 35.0),
                temperature=float(plugin.cfg.llm_temperature or 0.4),
                logger=plugin.logger,
            )
        body = review_mod.strip_model_text(text) if text else ""
        if body:
            source = "llm"
            warning = ""
        else:
            body = review_mod.local_review(
                day, watched, session, plugin.cfg, stats, watched_to=watched_to
            )
            source = "local"
            warning = llm_mod.reason_text(reason)

        session, changed = screening_mod.add_review(
            session,
            body,
            watched_to=watched_to,
            total=total,
            source=source,
            model=model,
            warning=warning,
        )
        if not changed:
            return Err(SdkError("观后感为空"))
        session = await asyncio.to_thread(self._save_and_return, day, session)
        markdown = review_mod.review_markdown(day, session["reviews"][-1], plugin.cfg)
        await asyncio.to_thread(plugin.store.write_screening_review, day, markdown)

        payload = self._payload(day, session, turns)
        payload["review"] = session["reviews"][-1]
        payload["generated"] = True
        payload["review_source"] = source
        return Ok(payload)

    @plugin_entry(
        id="screening_set_cover",
        name=tr("entries.screeningSetCover.name", default="设置场景封面"),
        description=tr(
            "entries.screeningSetCover.description",
            default="从当天章节图集里给某个场景选一张封面；filename 留空则恢复自动轮换。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）"),
                },
                "scene_no": {
                    "type": "integer",
                    "description": tr("fields.screeningSceneNo", default="场景序号（从 1 开始；0 = 当前场景）"),
                },
                "filename": {
                    "type": "string",
                    "description": tr("fields.screeningCoverFilename", default="当天图集里的图片文件名；留空恢复自动封面"),
                },
            },
        },
        llm_result_fields=["date", "cover", "progress"],
    )
    async def entry_screening_set_cover(
        self, date: str = "", scene_no: int = 0, filename: str = "", **_
    ):
        error = self._require_ready()
        if error:
            return Err(SdkError(error))
        day = self._day(date)
        turns = await self._turns(day)
        total = len(turns)
        session = await asyncio.to_thread(self._session, day, total)
        scenes = self._scenes(turns)
        if not scenes:
            return Err(SdkError("这一天没有场景，无法设置封面"))
        progress = screening_mod.progress(session, total, scenes)
        try:
            no = int(scene_no or progress.get("scene_no") or 1)
        except (TypeError, ValueError):
            no = 1
        no = max(1, min(no, len(scenes)))

        files = self._cover_files(day)
        name = str(filename or "").strip()
        if name and name not in files:
            return Err(SdkError("这一天图集里没有这张图片"))
        covers = session.get("covers")
        if not isinstance(covers, dict):
            covers = {}
        if name:
            covers[str(no)] = name
        else:
            covers.pop(str(no), None)
        session["covers"] = covers
        session = await asyncio.to_thread(self._save_and_return, day, session)
        payload = self._payload(day, session, turns)
        return Ok(payload)
