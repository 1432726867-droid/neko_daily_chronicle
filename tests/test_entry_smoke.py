"""端到端冒烟测试：用假的 SDK 与假的宿主上下文把插件入口整条链路跑一遍。

覆盖：装饰器堆叠能否 import、startup 初始化、``@message`` 采集、总线补录与去重、
章节/日记生成（本地引擎）、模型不可用时的降级、运行时入口、LLM 工具、面板上下文。

在装了 N.E.K.O SDK 的环境里会优先用真实 SDK；否则自建一个最小替身。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import os
import pathlib
import re
import shutil
import sys
import time
import types
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / ".tmp-tests"


def make_temp_dir() -> pathlib.Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True)
    return target


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


# ── 假的 SDK ────────────────────────────────────────────────────────


class _Logger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _log(self, level, message, *args):
        try:
            text = message.format(*args) if args else str(message)
        except Exception:  # noqa: BLE001 - 格式化失败也要记下来
            text = f"{message} {args}"
        self.records.append((level, text))

    def info(self, message, *args):
        self._log("info", message, *args)

    def warning(self, message, *args):
        self._log("warning", message, *args)

    def error(self, message, *args):
        self._log("error", message, *args)

    def debug(self, message, *args):
        self._log("debug", message, *args)

    def messages(self, level=None):
        return [text for name, text in self.records if level is None or name == level]


def _install_fake_sdk() -> None:
    plugin_pkg = types.ModuleType("plugin")
    plugin_pkg.__path__ = []  # type: ignore[attr-defined]
    sdk_pkg = types.ModuleType("plugin.sdk")
    sdk_pkg.__path__ = []  # type: ignore[attr-defined]
    plugin_pkg.sdk = sdk_pkg  # type: ignore[attr-defined]
    shared_pkg = types.ModuleType("plugin.sdk.shared")
    shared_pkg.__path__ = []  # type: ignore[attr-defined]
    core_pkg = types.ModuleType("plugin.sdk.shared.core")
    core_pkg.__path__ = []  # type: ignore[attr-defined]
    router_module = types.ModuleType("plugin.sdk.shared.core.router")
    module = types.ModuleType("plugin.sdk.plugin")

    class PluginRouter:
        """路由替身：只需要记住名字并暴露 main_plugin。"""

        def __init__(self, name="", **_kwargs):
            self.name = name
            self.main_plugin = None

    class NekoPluginBase:
        def __init__(self, ctx):
            self.ctx = ctx
            self.logger = ctx.logger
            self.config = ctx.config
            self.metadata = getattr(ctx, "metadata", {})
            self.plugin_id = getattr(ctx, "plugin_id", "neko_daily_chronicle")
            self.storage_dir = ctx.storage_dir
            self._data_root = ctx.data_root
            self.status_reports: list[dict] = []
            self.pushed: list[dict] = []
            self.routers: list[PluginRouter] = []

        def include_router(self, router):
            binder = getattr(router, "_bind", None)
            if callable(binder):
                binder(self)
            else:
                router.main_plugin = self
            self.routers.append(router)
            return router

        def data_path(self, *parts):
            return self._data_root.joinpath(*parts) if parts else self._data_root

        def report_status(self, payload):
            self.status_reports.append(payload)

        def push_message(self, **kwargs):
            self.pushed.append(kwargs)
            return {"submitted": True}

        def enable_file_logging(self, **_kwargs):
            return None

    class Ok:
        def __init__(self, value=None):
            self.value = value

        def __repr__(self):
            return f"Ok({self.value!r})"

    class Err:
        def __init__(self, error=None):
            self.error = error

        def __repr__(self):
            return f"Err({self.error!r})"

    class SdkError(Exception):
        pass

    def _passthrough_decorator(*_args, **_kwargs):
        def decorate(func):
            return func

        return decorate

    class _Ui:
        context = staticmethod(_passthrough_decorator)
        action = staticmethod(_passthrough_decorator)

    def _tr(key, default=None, **_kwargs):
        return default if default is not None else key

    module.NekoPluginBase = NekoPluginBase
    module.Ok = Ok
    module.Err = Err
    module.SdkError = SdkError
    module.neko_plugin = lambda cls: cls
    module.plugin_entry = _passthrough_decorator
    module.lifecycle = _passthrough_decorator
    module.timer_interval = _passthrough_decorator
    module.message = _passthrough_decorator
    module.llm_tool = _passthrough_decorator
    module.ui = _Ui()
    module.tr = _tr
    router_module.PluginRouter = PluginRouter

    sys.modules["plugin"] = plugin_pkg
    sys.modules["plugin.sdk"] = sdk_pkg
    sys.modules["plugin.sdk.shared"] = shared_pkg
    sys.modules["plugin.sdk.shared.core"] = core_pkg
    sys.modules["plugin.sdk.shared.core.router"] = router_module
    sys.modules["plugin.sdk.plugin"] = module


# ── 假的宿主上下文 ──────────────────────────────────────────────────


class _FakeConfig:
    def __init__(self, payload):
        self.payload = payload

    async def dump(self, **_kwargs):
        return dict(self.payload)

    async def update(self, values, **_kwargs):
        """模拟宿主的扁平点号写入（``chronicle.master_name`` → 嵌套 dict）。"""

        for key, value in values.items():
            parts = key.split(".")
            target = self.payload
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        return dict(self.payload)


class _FakeList:
    """模拟总线的列表返回（真实 SDK 会返回可 list() 的对象）。"""

    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _FakeStore:
    def __init__(self, records):
        self.records = records
        self.calls: list[dict] = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeList(self.records)


class _FakeBus:
    def __init__(self, memory_records=None, conversation_records=None):
        self.memory = _FakeStore(memory_records or [])
        self.conversations = _FakeStore(conversation_records or [])


class _FakeCtx:
    def __init__(self, data_root, config_payload):
        self.logger = _Logger()
        self.config = _FakeConfig(config_payload)
        self.plugin_id = "neko_daily_chronicle"
        self.metadata = {"id": "neko_daily_chronicle"}
        self.data_root = data_root
        self.storage_dir = data_root.parent
        self.bus = _FakeBus()


def _load_plugin_module():
    """统一使用替身 SDK，保证在宿主 SDK 环境与纯净环境下结果一致。"""

    # 真 SDK 已被导入过时，必须连同插件子模块一起清干净，否则会拿到真实的
    # PluginRouter/ConfigRouter，跟下面的假 NekoPluginBase 混用而报错。
    for name in [n for n in sys.modules if n == "plugin" or n.startswith("plugin.")]:
        del sys.modules[name]
    for name in [n for n in sys.modules if n == "neko_daily_chronicle"
                 or (n.startswith("neko_daily_chronicle.") and ".tests" not in n)]:
        del sys.modules[name]
    _install_fake_sdk()
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module("neko_daily_chronicle")


PLUGIN_MODULE = _load_plugin_module()


class PluginSmokeTests(unittest.TestCase):
    def setUp(self):
        self.user_root = make_temp_dir()
        self.data_root = self.user_root / "plugins" / "neko_daily_chronicle" / "data"
        self.data_root.mkdir(parents=True)
        self.now = time.time()

        # 隔离模型配置探测，测试才与这台机器上装了什么无关
        self._env = mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.user_root / "none" / "local"),
                "APPDATA": str(self.user_root / "none" / "roaming"),
                "USERPROFILE": str(self.user_root / "none" / "home"),
                "HOME": str(self.user_root / "none" / "home"),
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in ("NEKO_DATA_DIR", "NEKO_USER_DATA_DIR"):
            os.environ.pop(name, None)

        self.config_payload = {
            "chronicle": {
                "diary_engine": "auto",
                "live_refresh_seconds": 30,
                "compile_on_startup": False,
            }
        }
        self.ctx = _FakeCtx(self.data_root, self.config_payload)
        self.plugin = PLUGIN_MODULE.NekoDailyChroniclePlugin(self.ctx)

    def tearDown(self):
        shutil.rmtree(self.user_root, ignore_errors=True)

    def _boot(self):
        """跑 startup 并在同一事件循环里等后台重活（补录/结算）跑完。

        生产里这些活故意丢到后台，避免拖垮宿主 10s 启动握手；测试里必须等它们跑完
        才能断言磁盘上的结果，所以包一层。
        """

        async def _run():
            result = await self.plugin.startup()
            if self.plugin._startup_task is not None:
                await self.plugin._startup_task
            return result

        return asyncio.run(_run())

    # ── 基础 ────────────────────────────────────────────────────────

    def test_module_decorators_and_methods_present(self):
        self.assertTrue(hasattr(PLUGIN_MODULE, "NekoDailyChroniclePlugin"))
        for name in (
            "startup",
            "shutdown",
            "on_chat_message",
            "harvest_tick",
            "finalize_tick",
            "entry_status",
            "entry_build_day",
            "entry_build_today",
            "entry_build_all",
            "entry_list_days",
            "entry_read_chapter",
            "entry_read_diary",
            "entry_search",
            "entry_capture",
            "entry_select_day",
            "panel_context",
            "tool_read_diary",
            "tool_love_diary",
            "tool_list_days",
            "tool_note",
            "entry_cat_write_diary",
            "_auto_tidy_memo",
            "_handle_love_diary_trigger",
        ):
            self.assertTrue(hasattr(self.plugin, name), f"缺少方法 {name}")

    def test_startup_creates_layout(self):
        result = self._boot()
        self.assertIsNotNone(result.value)
        self.assertEqual(result.value["status"], "ready")
        self.assertTrue((self.data_root / "raw").is_dir())
        self.assertTrue((self.data_root / "chapters").is_dir())
        self.assertTrue((self.data_root / "diary").is_dir())
        self.assertFalse(result.value["llm"]["available"])

    def test_message_capture_and_dedupe(self):
        self._boot()
        first = self.plugin.on_chat_message(text="早安呀", sender="主人")
        self.assertEqual(first.value["captured"], 1)
        # 同一条消息（模拟两条来源）不应重复入库
        self.plugin.on_chat_message(text="早安呀", sender="主人")
        raw = (self.data_root / "raw" / f"{today()}.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len([line for line in raw.splitlines() if line.strip()]), 1)

    def test_message_handler_never_raises_on_junk(self):
        self._boot()
        self.plugin.on_chat_message()
        self.plugin.on_chat_message(无文本="x")
        self.plugin.on_chat_message(data={"content": "嵌套形态", "role": "user"})
        raw = (self.data_root / "raw" / f"{today()}.jsonl").read_text(encoding="utf-8")
        self.assertIn("嵌套形态", raw)

    def test_image_memory_delete_and_send(self):
        self._boot()
        day = "2099-01-09"
        folder = self.data_root / "static_ui" / "images" / day
        folder.mkdir(parents=True, exist_ok=True)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        image = folder / "test.png"
        image.write_bytes(png)
        sidecar = folder / "test.png.memory.json"

        async def run_flow():
            saved = await self.plugin.entry_save_image_memory(
                date=day, filename="test.png", text="和主人一起拍的"
            )
            items = await self.plugin.entry_list_images(date=day)
            sent = await self.plugin.entry_send_image_to_catgirl(
                date=day, filename="test.png"
            )
            return saved, items, sent

        saved, items, sent = asyncio.run(run_flow())
        self.assertEqual(saved.value["memory"], "和主人一起拍的")
        self.assertTrue(sidecar.is_file())
        self.assertEqual(items.value["items"][0]["memory"], "和主人一起拍的")
        self.assertTrue(sent.value["image_sent"])
        pushed = self.plugin.pushed[-1]
        self.assertEqual(pushed["visibility"], ["chat"])
        self.assertEqual(pushed["ai_behavior"], "respond")
        self.assertEqual(pushed["parts"][0]["type"], "image")
        self.assertEqual(pushed["parts"][1]["text"], "和主人一起拍的")

        deleted = asyncio.run(
            self.plugin.entry_delete_image(date=day, filename="test.png")
        )
        self.assertEqual(deleted.value["count"], 0)
        self.assertFalse(image.exists())
        self.assertFalse(sidecar.exists())

    # ── 采集与生成 ──────────────────────────────────────────────────

    def test_bus_harvest_and_build(self):
        # 用宿主真实的两种记录形态：
        #   bus.memory        → {"type": "user_message", "content": ...}
        #   bus.conversations → {"type": "conversation_turn", "content": ...}（不带角色）
        self.ctx.bus = _FakeBus(
            memory_records=[
                {
                    "type": "user_message",
                    "content": "今天要复习考试",
                    "timestamp": self.now,
                    "source": "main_logic.core",
                },
            ],
            conversation_records=[
                {"type": "conversation_turn", "content": "======以下为环境提示======\n不要生成思考过程", "timestamp": self.now - 5},
                {"type": "conversation_turn", "content": "今天要复习考试", "timestamp": self.now},
                {"type": "conversation_turn", "content": "我陪你复习，别紧张", "timestamp": self.now + 20},
                {"type": "conversation_turn", "content": "谢谢你", "timestamp": self.now + 40},
                {"type": "conversation_turn", "content": "喝杯奶茶休息一下", "timestamp": self.now + 60},
            ],
        )
        self._boot()
        day = today()
        # 环境提示不算对话；同一条用户消息不重复入库
        raw_text = (self.data_root / "raw" / f"{day}.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("环境提示", raw_text)
        self.assertEqual(self.plugin.store.count_turns(day), 4)

        result = asyncio.run(self.plugin.harvest_tick())
        self.assertEqual(result.value["added"], 0)

        built = asyncio.run(self.plugin._build_day(day, use_llm=False, force=True))
        self.assertEqual(built["diary"]["engine"], "")
        chapter = (self.data_root / "chapters" / f"{day}.md").read_text(encoding="utf-8")
        # 模型不可用：日记留空，不写文件（等模型可用后再生成）
        self.assertFalse(self.plugin.store.has_diary(day))
        # 角色由"提示词边界 + 一问一答交替"补出来，章节里显示配置的称呼；
        # 每句前面还带当天序号，这里把序号剥掉后比对台词本身
        plain = re.sub(r"\*\*#\d+ ", "**", chapter)
        self.assertIn("**主人**：今天要复习考试", plain)
        self.assertIn("**猫娘**：我陪你复习，别紧张", plain)
        self.assertIn("**主人**：谢谢你", plain)
        self.assertIn("**猫娘**：喝杯奶茶休息一下", plain)
        self.assertIn("**#1 主人**", chapter)
        self.assertIn("**#4 猫娘**", chapter)
        self.assertIn("第 1 章", chapter)
        self.assertTrue((self.data_root / "chapters" / "README.md").is_file())
        self.assertTrue((self.data_root / "diary" / "README.md").is_file())

    def test_llm_failure_leaves_diary_empty(self):
        self._boot()
        self.plugin.on_chat_message(text="今天很开心，喝了奶茶", sender="主人")
        # 指向一个必然连不上的端口：模型失败时日记留空，而不是降级成本地摘要
        self.plugin.llm_endpoint = PLUGIN_MODULE.llm_mod.LlmEndpoint(
            base_url="http://127.0.0.1:9/v1", model="unreachable", source="test"
        )
        built = asyncio.run(self.plugin._build_day(today(), use_llm=True, force=True))
        self.assertEqual(built["diary"]["engine"], "")
        self.assertFalse(self.plugin.store.has_diary(today()))

    def test_redaction_applies_to_chapter(self):
        self.config_payload["chronicle"]["redact"] = ["13800138000"]
        plugin = PLUGIN_MODULE.NekoDailyChroniclePlugin(self.ctx)
        asyncio.run(plugin.startup())
        plugin.on_chat_message(text="我的电话是 13800138000", sender="主人")
        asyncio.run(plugin._build_day(today(), use_llm=False, force=True))
        chapter = (self.data_root / "chapters" / f"{today()}.md").read_text(encoding="utf-8")
        self.assertNotIn("13800138000", chapter)
        self.assertIn("＊＊＊", chapter)

    # ── 入口 / 工具 / 面板 ──────────────────────────────────────────

    def test_entries_and_panel_context(self):
        self._boot()
        self.plugin.on_chat_message(text="记得明天买猫粮", sender="主人")
        asyncio.run(self.plugin.entry_capture(text="手动补一句", role="assistant", speaker="猫娘"))
        asyncio.run(self.plugin.entry_build_today(with_diary=True))

        status = asyncio.run(self.plugin.entry_status())
        self.assertGreaterEqual(status.value["days"], 1)
        self.assertIn("llm", status.value)

        days = asyncio.run(self.plugin.entry_list_days(limit=5))
        self.assertEqual(days.value["days"][0]["date"], today())

        chapter = asyncio.run(self.plugin.entry_read_chapter(date=today()))
        self.assertTrue(chapter.value["exists"])
        self.assertIn("手动补一句", chapter.value["markdown"])

        diary = asyncio.run(self.plugin.entry_read_diary(date=today()))
        # 模型不可用：日记留空（章节照常生成）
        self.assertFalse(diary.value["exists"])

        found = asyncio.run(self.plugin.entry_search(query="猫粮", limit=5))
        self.assertGreaterEqual(found.value["count"], 1)

        empty = asyncio.run(self.plugin.entry_search(query="   ", limit=5))
        self.assertIsInstance(empty, PLUGIN_MODULE.Err)

        selected = asyncio.run(self.plugin.entry_select_day(date=today()))
        self.assertEqual(selected.value["selected"], today())

        context = asyncio.run(self.plugin.panel_context())
        self.assertTrue(context["ready"])
        self.assertEqual(context["selected"], today())
        self.assertTrue(context["chapter_text"])
        self.assertEqual(context["days"][0]["date"], today())
        self.assertIn("config", context)
        self.assertTrue(context["paths"]["root"])

    def test_time_capsule_entries(self):
        self._boot()
        day = today()
        future = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 86400))

        written = asyncio.run(
            self.plugin.entry_capsule_write(text="给未来的一句话", unlock_date=day)
        )
        self.assertIsInstance(written, PLUGIN_MODULE.Ok)
        record = written.value
        self.assertFalse(record["locked"])

        listed = asyncio.run(self.plugin.entry_capsule_list())
        self.assertEqual(listed.value["count"], 1)
        # 主人自己写的不能算「猫娘写好了」
        self.assertEqual(listed.value["master_count"], 1)
        self.assertEqual(listed.value["catgirl_count"], 0)
        self.assertEqual(
            asyncio.run(self.plugin.entry_capsule_list(author="catgirl")).value["count"], 0
        )
        self.assertEqual(
            asyncio.run(self.plugin.entry_capsule_list(author="master")).value["count"], 1
        )
        self.assertIsInstance(
            asyncio.run(self.plugin.entry_capsule_list(author="someone")), PLUGIN_MODULE.Err
        )
        opened = asyncio.run(self.plugin.entry_capsule_read(id=record["id"]))
        self.assertEqual(opened.value["items"][0]["text"], "给未来的一句话")

        locked = asyncio.run(
            self.plugin.entry_capsule_write(text="还没到时间", unlock_date=future)
        )
        self.assertTrue(locked.value["locked"])
        listed = asyncio.run(self.plugin.entry_capsule_list())
        item = next(item for item in listed.value["items"] if item["id"] == locked.value["id"])
        self.assertNotIn("text", item)
        denied = asyncio.run(self.plugin.entry_capsule_read(id=locked.value["id"]))
        self.assertIsInstance(denied, PLUGIN_MODULE.Err)

        visible = asyncio.run(self.plugin.entry_capsule_list(include_locked=False))
        self.assertEqual(visible.value["count"], 1)

    def test_capsule_nudge_only_when_catgirl_missing(self):
        self._boot()
        nudged = asyncio.run(self.plugin.entry_capsule_nudge())
        self.assertIsInstance(nudged, PLUGIN_MODULE.Ok)
        self.assertEqual(nudged.value["catgirl_count"], 0)
        self.assertTrue(nudged.value["pushed"])
        sent = self.plugin.pushed[-1]
        self.assertEqual(sent["ai_behavior"], "respond")
        self.assertIn("chronicle_write_capsule", sent["parts"][0]["text"])

        # 猫娘真的写了之后，不再重复打扰
        asyncio.run(
            self.plugin.tool_write_capsule(text="本喵写给未来", unlock_date=today())
        )
        again = asyncio.run(self.plugin.entry_capsule_nudge())
        self.assertEqual(again.value["catgirl_count"], 1)
        self.assertFalse(again.value["pushed"])

    def test_capsule_due_reminder_once(self):
        self._boot()
        day = today()
        written = asyncio.run(
            self.plugin.entry_capsule_write(text="到期提醒测试", unlock_date=day)
        )
        capsule_id = written.value["id"]
        noon = time.mktime(time.strptime(f"{day} 12:00", "%Y-%m-%d %H:%M"))
        morning = time.mktime(time.strptime(f"{day} 08:00", "%Y-%m-%d %H:%M"))

        before = self.plugin._notify_due_capsules(now=morning)
        self.assertEqual(before.get("skipped"), "before_hour")
        self.assertEqual(len(self.plugin.pushed), 0)

        first = self.plugin._notify_due_capsules(now=noon)
        self.assertEqual(first["notified"], 1)
        self.assertEqual(len(self.plugin.pushed), 1)
        self.assertIn(
            capsule_id, PLUGIN_MODULE.capsule_mod.notified_ids(self.plugin.store)
        )

        again = self.plugin._notify_due_capsules(now=noon)
        self.assertEqual(again["notified"], 0)
        self.assertEqual(len(self.plugin.pushed), 1)

        self.plugin.cfg.capsule_notify_enabled = False
        disabled = self.plugin._notify_due_capsules(now=noon)
        self.assertEqual(disabled.get("skipped"), "disabled")
        self.assertEqual(len(self.plugin.pushed), 1)

    def test_plugin_config_read_and_save(self):
        self._boot()
        got = asyncio.run(self.plugin.entry_get_config())
        keys = {field["key"] for field in got.value["fields"]}
        self.assertIn("master_name", keys)
        self.assertIn("search_default_days", keys)
        # 模型端点/密钥统一走浏览页「模型配置」，不再出现在插件配置里
        self.assertNotIn("llm_api_key", keys)
        self.assertNotIn("llm_provider", keys)
        self.assertNotIn("author", keys)

        saved = asyncio.run(
            self.plugin.entry_save_config(
                values={
                    "master_name": "主人大人",
                    "search_default_days": 30,
                    "cat_diary_enabled": False,
                    "llm_api_key": "",
                }
            )
        )
        self.assertTrue(saved.value["saved"])
        self.assertEqual(self.plugin.cfg.master_name, "主人大人")
        self.assertEqual(self.plugin.cfg.search_default_days, 30)
        self.assertFalse(self.plugin.cfg.cat_diary_enabled)
        # 空密钥不覆盖旧值；改动按扁平点号写回宿主配置
        self.assertNotIn("llm_api_key", saved.value["changed"])
        self.assertEqual(self.ctx.config.payload["chronicle"]["master_name"], "主人大人")
        self.assertEqual(self.ctx.config.payload["chronicle"]["search_default_days"], 30)

        # 非法值不落盘
        bad = asyncio.run(self.plugin.entry_save_config(values={"search_default_days": 0}))
        self.assertFalse(bad.value["saved"])
        self.assertEqual(self.plugin.cfg.search_default_days, 30)
        # 不认识的字段忽略；空提交报错
        unknown = asyncio.run(self.plugin.entry_save_config(values={"不存在": 1}))
        self.assertTrue(unknown.value["saved"])
        self.assertEqual(unknown.value["changed"], [])
        empty = asyncio.run(self.plugin.entry_save_config(values={}))
        self.assertIsInstance(empty, PLUGIN_MODULE.Err)
        # 清空文本字段 = 回落默认值，不算非法
        cleared = asyncio.run(
            self.plugin.entry_save_config(values={"memory_store_character": ""})
        )
        self.assertTrue(cleared.value["saved"])

    def test_llm_tools_and_edge_cases(self):
        self._boot()
        self.plugin.on_chat_message(text="今天去看了海", sender="主人")
        asyncio.run(self.plugin.entry_build_today(with_diary=True))

        # 「日记」工具现在指向猫娘自己那篇
        self.assertTrue(
            asyncio.run(self.plugin.tool_read_diary(date=today())).get("is_error")
        )
        asyncio.run(self.plugin.entry_cat_write_diary(text="今天我看了海，很开心喵～"))
        diary = asyncio.run(self.plugin.tool_read_diary(date=today()))
        self.assertEqual(diary["kind"], "cat_diary")
        self.assertIn("很开心", diary["content"])

        listing = asyncio.run(self.plugin.tool_list_days(limit=3))
        self.assertTrue(listing["days"])
        note = asyncio.run(self.plugin.tool_note(text="记一笔", speaker="猫娘"))
        self.assertTrue(note["saved"])
        missing = asyncio.run(self.plugin.tool_read_diary(date="1999-01-01"))
        self.assertTrue(missing.get("is_error"))
        empty_note = asyncio.run(self.plugin.tool_note(text="  "))
        self.assertTrue(empty_note.get("is_error"))

        # 非法日期回落到"今天"，不应报错
        bad = asyncio.run(self.plugin.entry_build_day(date="不是日期"))
        self.assertEqual(bad.value["day"], today())
        # 空 capture 报错
        bad_capture = asyncio.run(self.plugin.entry_capture(text="   "))
        self.assertIsInstance(bad_capture, PLUGIN_MODULE.Err)

    def test_love_diary_trigger_and_daily_diary_tool(self):
        self._boot()
        day = today()
        # 每日日记缺失时，「爱妻日记」工具返回 NO_DIARY
        missing = asyncio.run(self.plugin.tool_love_diary(date=day))
        self.assertEqual(missing.get("error"), "NO_DIARY")
        # 直接落一篇每日日记，再走专用通道读取
        self.plugin.store.write_diary(day, "# 每日日记\n\n今天很开心。\n")
        found = asyncio.run(self.plugin.tool_love_diary(date=day))
        self.assertEqual(found["kind"], "love_diary")
        self.assertIn("很开心", found["content"])

    def test_love_diary_chat_trigger_returns_to_catgirl(self):
        self._boot()
        day = today()
        self.plugin.store.write_diary(day, "# 每日日记\n\n主人和我在海边散步。\n")
        result = self.plugin.on_chat_message(
            text=f"爱妻日记 {day}", sender="主人"
        )
        self.assertEqual(result.value["love_diary"]["day"], day)
        self.assertTrue(result.value["love_diary"]["found"])
        self.assertEqual(len(self.plugin.pushed), 1)
        pushed = self.plugin.pushed[0]
        self.assertEqual(pushed["visibility"], [])
        self.assertEqual(pushed["ai_behavior"], "read")
        self.assertIn("海边散步", pushed["parts"][0]["text"])
        # 只给日记正文，不带"请用你自己的口吻回应"这类指令句
        self.assertTrue(pushed["parts"][0]["text"].startswith(f"【每日日记】{day}\n"))
        self.assertNotIn("不要提到", pushed["parts"][0]["text"])

        # 普通聊天里提到「日记」不触发每日日记通道
        self.plugin.on_chat_message(text="我想看看你的日记", sender="主人")
        self.assertEqual(len(self.plugin.pushed), 1)

    def test_parse_love_diary_trigger(self):
        parse = PLUGIN_MODULE.parse_love_diary_request
        self.assertEqual(parse("爱妻日记 2026-10-22"), "2026-10-22")
        self.assertEqual(parse("爱妻日记2026年10月22日"), "2026-10-22")
        self.assertEqual(parse("爱妻日记 10月7号"), "2026-10-07")
        self.assertEqual(parse("爱妻日记"), "")
        self.assertIsNone(parse("我想看看你的日记"))
        self.assertIsNone(parse("爱妻日记真好看"))

    def test_parse_turn_reference(self):
        parse = PLUGIN_MODULE.parse_turn_reference
        # 主人原话：我曾经在10月6号第13条和你说过
        self.assertEqual(parse("我曾经在10月6号第13条和你说过"), (f"{time.strftime('%Y')}-10-06", "第13条"))
        self.assertEqual(parse("我在10月6号第13条和你说过"), (f"{time.strftime('%Y')}-10-06", "第13条"))
        self.assertEqual(parse("2026-10-06 第13条，念给我听"), ("2026-10-06", "第13条"))
        # 没写日期：带"说过/念/记得"这类词才算引用，落到今天
        self.assertEqual(parse("把最后一条念一下")[1], "最后一条")
        self.assertEqual(parse("把最后一条念一下")[0], "")
        # 普通句子不算引用
        self.assertIsNone(parse("第13条规则是什么"))
        self.assertIsNone(parse("那条我提过吗"))

    def test_parse_turn_index_forms(self):
        parse = PLUGIN_MODULE._parse_turn_index
        self.assertEqual(parse("13"), 13)
        self.assertEqual(parse("第十三"), 13)
        self.assertEqual(parse("第13条"), 13)
        self.assertEqual(parse("第 13 句"), 13)
        self.assertEqual(parse("最后一条", 7), 7)
        self.assertEqual(parse("倒数第二条", 7), 6)
        self.assertIsNone(parse("最后一条", 0))

    def test_parse_memory_request(self):
        parse = PLUGIN_MODULE.parse_memory_request
        self.assertEqual(parse("我说过油菜花吗"), "油菜花")
        self.assertEqual(parse("还记得油菜花吗"), "油菜花")
        self.assertEqual(parse("我之前和你说过油菜花"), "油菜花")
        self.assertEqual(parse("你还记得我提过小宝宝吗"), "小宝宝")
        # 带序号的走精确分支，不算模糊回忆
        self.assertIsNone(parse("10月13号第3条"))
        # 没有回忆标记的普通句子不触发
        self.assertIsNone(parse("今天油菜花开得真好"))

    def test_reader_skips_invocation_lines(self):
        from chronicle.models import Turn  # type: ignore
        from chronicle.reader import _is_invocation, search_turns  # type: ignore

        self.assertTrue(_is_invocation("我说过油菜花吗"))
        self.assertTrue(_is_invocation("10月13号第3条"))
        self.assertTrue(_is_invocation("把第13条念给我听"))
        self.assertFalse(_is_invocation("那片油菜花田好大好漂亮"))
        self.assertFalse(_is_invocation("我说过，我们一起去看油菜花"))
        # 「第三句话」这种叙述不能被当成序号调用，否则正常检索会被清空
        self.assertFalse(_is_invocation("第三句话"))

        turns = [
            Turn(ts=100.0, text="我说过油菜花吗", role="user", speaker="主人"),
            Turn(ts=101.0, text="那时候我们在油菜花田里待到天黑", role="assistant", speaker="猫娘"),
        ]
        hits = search_turns([("2026-10-06", turns)], "油菜花", limit=5)
        self.assertEqual(len(hits), 1)
        self.assertIn("待到天黑", hits[0]["snippet"])

    def test_turn_reference_chat_trigger_returns_to_catgirl(self):
        self._boot()
        from chronicle.models import Turn  # type: ignore

        day = today()
        self.plugin.store.append_turns(
            [
                Turn(ts=self.now, text="第一句原话", role="user", speaker="主人"),
                Turn(ts=self.now + 1, text="第二句原话", role="assistant", speaker="猫娘"),
                Turn(ts=self.now + 2, text="第三句原话", role="user", speaker="主人"),
            ]
        )
        # 「最后一条」取的是发这句之前的那条，不是刚发的这句话本身
        result = self.plugin.on_chat_message(
            text=f"我在{day}最后一条说过什么", sender="主人"
        )
        self.assertEqual(result.value["turn_reference"]["index"], 3)
        self.assertIn("第三句原话", self.plugin.pushed[-1]["parts"][0]["text"])

        result = self.plugin.on_chat_message(
            text=f"我曾经在{day}第2条和你说过", sender="主人"
        )
        self.assertTrue(result.value["turn_reference"]["found"])
        self.assertEqual(result.value["turn_reference"]["index"], 2)
        pushed = self.plugin.pushed[-1]
        self.assertEqual(pushed["visibility"], [])
        body = pushed["parts"][0]["text"]
        # 只给"谁说的 + 原文"，不再带指令文本
        self.assertEqual(body, "【引用原文】\n说话人：猫娘\n原文：第二句原话")

        # 超范围：只说没找到
        result = self.plugin.on_chat_message(
            text=f"我在{day}第99条和你说过", sender="主人"
        )
        self.assertFalse(result.value["turn_reference"]["found"])
        self.assertEqual(self.plugin.pushed[-1]["parts"][0]["text"], "【引用原文】未找到该条记录")

        # 普通句子不触发
        before = len(self.plugin.pushed)
        self.plugin.on_chat_message(text="第13条规则是什么", sender="主人")
        self.assertEqual(len(self.plugin.pushed), before)

    def test_fuzzy_memory_uses_diary_then_memo_then_skips(self):
        self._boot()
        from chronicle.models import Turn  # type: ignore

        day = today()
        self.plugin.store.append_turns(
            [Turn(ts=self.now, text="那片油菜花田好大好漂亮，我们待了很久", role="assistant", speaker="猫娘")]
        )
        # ① 模型写的日记（叙事段）优先
        self.plugin.store.write_diary(
            day,
            "# 日记\n\n> 由 语言模型 （X） 依据当天原话生成\n\n"
            "哥哥，今天我们在油菜花田里待到天黑，风把花瓣吹到我耳朵上了。\n",
        )
        result = self.plugin.on_chat_message(text="我说过油菜花吗", sender="主人")
        self.assertTrue(result.value["memory_cue"]["found"])
        body = self.plugin.pushed[-1]["parts"][0]["text"]
        self.assertTrue(body.startswith("【回忆线索】\n"))
        self.assertIn(f"时间：{day}", body)
        self.assertIn("说话人：猫娘", body)
        self.assertIn("发生了什么：", body)
        self.assertIn("花瓣吹到我耳朵上", body)

        # ② 没有日记 → 用备忘录段落
        self.plugin.store.write_diary(day, "# 日记\n\n> 本地统计摘要（未调用语言模型）\n\n| 时间跨度 | 1 小时 |\n")
        self.plugin.store.chapters_dir.mkdir(parents=True, exist_ok=True)
        from chronicle import memo as memo_mod  # type: ignore

        memo_mod.write(
            self.plugin.store,
            day,
            "先前对话的备忘录: 下午我们在油菜花田里待到天黑，她耳朵上沾了花瓣。\n",
        )
        self.plugin.on_chat_message(text="还记得油菜花吗", sender="主人")
        body = self.plugin.pushed[-1]["parts"][0]["text"]
        self.assertIn("发生了什么：下午我们在油菜花田里待到天黑", body)

        # ③ 两者都没有 → 整行省略
        memo_mod.write(self.plugin.store, day, "")
        self.plugin.on_chat_message(text="我说过油菜花吗", sender="主人")
        body = self.plugin.pushed[-1]["parts"][0]["text"]
        self.assertNotIn("发生了什么", body)

        # 问句没命中 → 告诉她没找到
        self.plugin.on_chat_message(text="我说过小宝宝吗", sender="主人")
        self.assertEqual(self.plugin.pushed[-1]["parts"][0]["text"], "【回忆线索】没有找到相关记录")

        # 陈述句（非问句）没命中 → 不塞噪音
        before = len(self.plugin.pushed)
        result = self.plugin.on_chat_message(text="我之前和你说过小宝宝", sender="主人")
        self.assertIsNone(result.value.get("memory_cue"))
        self.assertEqual(len(self.plugin.pushed), before)

    def test_cat_diary_replace_and_auto_tidy_skips_without_model(self):
        self._boot()
        day = today()
        asyncio.run(self.plugin.entry_cat_write_diary(text="第一版喵～"))
        asyncio.run(self.plugin.entry_cat_write_diary(text="第二版喵～", mode="append"))
        text = self.plugin.store.read_cat_diary(day)
        self.assertIn("第一版", text)
        self.assertIn("第二版", text)

        replaced = asyncio.run(self.plugin.entry_cat_write_diary(text="重写版喵～", mode="replace"))
        self.assertTrue(replaced.value["replaced"])
        text = self.plugin.store.read_cat_diary(day)
        self.assertNotIn("第一版", text)
        self.assertIn("重写版", text)

        # 没有模型时自动整理直接跳过，不改备忘录
        tidy = asyncio.run(self.plugin._auto_tidy_memo())
        self.assertEqual(tidy["status"], "skipped")
        self.assertEqual(self.plugin.pushed, [])

    def test_cat_diary_private_readable_option(self):
        self._boot()
        day = today()
        asyncio.run(
            self.plugin.entry_cat_write_diary(
                date="2026-01-02", text="公开的一篇喵～", visibility="public"
            )
        )
        asyncio.run(
            self.plugin.entry_cat_write_diary(
                date=day, text="只给猫娘自己看的悄悄话", visibility="private"
            )
        )

        listed = asyncio.run(self.plugin.entry_list_cat_diaries())
        self.assertEqual(listed.value["count"], 2)
        self.assertEqual(listed.value["hidden_private"], 0)
        by_day = {item["date"]: item for item in listed.value["items"]}
        self.assertFalse(by_day["2026-01-02"]["private"])
        self.assertTrue(by_day[day]["private"])

        # 关掉「私密日记是否可读」：浏览页不再返回私密那篇，但猫娘自己仍读得到
        self.plugin.cfg.cat_diary_private_readable = False
        hidden = asyncio.run(self.plugin.entry_list_cat_diaries())
        self.assertEqual(hidden.value["count"], 1)
        self.assertEqual(hidden.value["hidden_private"], 1)
        self.assertEqual(hidden.value["items"][0]["date"], "2026-01-02")
        own = asyncio.run(self.plugin.entry_cat_read_diary(date=day))
        self.assertTrue(own.value["exists"])

        # 插件面板同样隐藏私密正文（仍报告这一天有日记）
        self.plugin._panel_day = day
        ctx = asyncio.run(self.plugin.panel_context())
        self.assertTrue(ctx["cat_diary_exists"])
        self.assertTrue(ctx["cat_diary_hidden"])
        self.assertEqual(ctx["cat_diary_text"], "")

    def test_cat_diary_entry_management_entries(self):
        self._boot()
        day = today()
        write = asyncio.run(
            self.plugin.entry_cat_write_diary(
                date=day, text="今天喝了奶茶好开心喵～", mood="happy", tags=["奶茶"]
            )
        )
        entry_id = write.value["entry_id"]
        self.assertTrue(entry_id)

        # 读取时逐条返回，带 id/时间/心情/标签/可见性
        read = asyncio.run(self.plugin.entry_cat_read_diary(date=day))
        self.assertEqual(read.value["count"], 1)
        entry = read.value["entries"][0]
        self.assertEqual(entry["id"], entry_id)
        self.assertEqual(entry["mood"], "happy")
        self.assertEqual(entry["tags"], ["奶茶"])

        # 检索 / 统计 / 往年今日
        hits = asyncio.run(self.plugin.entry_cat_search_diary(keyword="奶茶"))
        self.assertEqual(hits.value["count"], 1)
        empty = asyncio.run(self.plugin.entry_cat_search_diary(keyword="  "))
        self.assertIsInstance(empty, PLUGIN_MODULE.Err)

        stats = asyncio.run(self.plugin.entry_cat_diary_stats(days=30))
        self.assertGreaterEqual(stats.value["entries"], 1)
        self.assertEqual(stats.value["moods"][0]["mood"], "happy")

        throwback = asyncio.run(self.plugin.entry_cat_diary_throwback())
        self.assertEqual(throwback.value["count"], 0)

        # 按条修改 / 删除
        updated = asyncio.run(
            self.plugin.entry_cat_update_diary_entry(
                date=day,
                entry_id=entry_id,
                text="改写成新的喵～",
                mood="calm",
                visibility="private",
            )
        )
        self.assertTrue(updated.value["updated"])
        self.assertEqual(updated.value["entry"]["text"], "改写成新的喵～")
        self.assertEqual(updated.value["entry"]["mood"], "calm")
        self.assertEqual(updated.value["entry"]["visibility"], "private")
        missing = asyncio.run(
            self.plugin.entry_cat_update_diary_entry(date=day, entry_id="不存在", text="x")
        )
        self.assertIsInstance(missing, PLUGIN_MODULE.Err)

        deleted = asyncio.run(
            self.plugin.entry_cat_delete_diary_entry(date=day, entry_id=entry_id)
        )
        self.assertTrue(deleted.value["deleted"])
        after = asyncio.run(self.plugin.entry_cat_read_diary(date=day))
        self.assertEqual(after.value["count"], 0)

    def test_numbered_retrieval_entries_and_tools(self):
        self._boot()
        for text in ("第一句话", "第二句话", "第三句话"):
            self.plugin.on_chat_message(text=text, sender="主人")
        day = today()

        rows = asyncio.run(self.plugin.entry_read_day(date=day, start=1, count=10))
        self.assertEqual(rows.value["total"], 3)
        self.assertEqual([row["index"] for row in rows.value["items"]], [1, 2, 3])
        self.assertTrue(rows.value["items"][0]["time"])

        single = asyncio.run(self.plugin.entry_get_turn(date=day, index=2))
        self.assertEqual(single.value["index"], 2)
        self.assertEqual(single.value["text"], "第二句话")
        self.assertEqual(single.value["total"], 3)

        recent = asyncio.run(self.plugin.entry_recent(count=2, date=day))
        self.assertEqual([row["index"] for row in recent.value["items"]], [2, 3])

        found = asyncio.run(self.plugin.entry_search_chat(query="第三句", limit=5, days=3))
        self.assertGreaterEqual(found.value["count"], 1)
        self.assertEqual(found.value["results"][0]["index"], 3)

        # 越界与空查询要返回 Err，而不是抛异常
        self.assertIsInstance(
            asyncio.run(self.plugin.entry_get_turn(date=day, index=99)), PLUGIN_MODULE.Err
        )
        self.assertIsInstance(asyncio.run(self.plugin.entry_search_chat(query="   ")), PLUGIN_MODULE.Err)

        # 给猫娘用的工具：同一个序号口径
        self.assertEqual(asyncio.run(self.plugin.tool_get_turn(index=1, date=day))["text"], "第一句话")
        self.assertEqual(asyncio.run(self.plugin.tool_read_day(date=day, start=1, count=2))["count"], 2)
        self.assertEqual(
            [row["index"] for row in asyncio.run(self.plugin.tool_recent(count=1, date=day))["items"]],
            [3],
        )
        self.assertTrue(asyncio.run(self.plugin.tool_search_chat(query="第二句"))["results"])
        self.assertTrue(asyncio.run(self.plugin.tool_get_turn(index=99, date=day)).get("is_error"))

        # 章节里的编号与接口口径一致
        asyncio.run(self.plugin.entry_build_today(with_diary=True))
        chapter = (self.data_root / "chapters" / f"{day}.md").read_text(encoding="utf-8")
        self.assertIn("#1 ", chapter)
        self.assertIn("#3 ", chapter)

    def test_numbering_matches_between_reader_and_chapter(self):
        """系统消息被章节滤掉，读取接口也必须滤掉，否则序号会整体错开一位。"""

        self.ctx.bus = _FakeBus(
            conversation_records=[
                {"type": "conversation_turn", "content": "======以下为环境提示======\n不要生成思考过程", "timestamp": self.now},
                {"type": "conversation_turn", "content": "第一句话", "timestamp": self.now + 10},
                {"type": "conversation_turn", "content": "第二句话", "timestamp": self.now + 20},
            ],
        )
        self._boot()
        day = today()

        rows = asyncio.run(self.plugin.entry_read_day(date=day, start=1, count=10))
        self.assertEqual(rows.value["total"], 2)
        self.assertEqual(rows.value["items"][0]["text"], "第一句话")

        built = asyncio.run(self.plugin._build_day(day, use_llm=False, force=True))
        self.assertEqual(built["turns"], rows.value["total"])
        chapter = (self.data_root / "chapters" / f"{day}.md").read_text(encoding="utf-8")
        self.assertIn("**#1 ", chapter)
        self.assertIn("第一句话", chapter.split("**#1 ")[1].splitlines()[0])
        self.assertNotIn("环境提示", chapter)

    def test_memory_and_diary_button(self):
        self._boot()
        for text in ("早安呀", "今天去看了海", "很开心的说"):
            self.plugin.on_chat_message(text=text, sender="主人")
        day = today()

        # 收藏一条 → 回忆录里能看到
        saved = asyncio.run(self.plugin.entry_toggle_memory(date=day, index=2, on=True))
        self.assertTrue(saved.value["saved"])
        self.assertEqual(saved.value["total"], 1)
        mem = asyncio.run(self.plugin.entry_memories())
        self.assertEqual(mem.value["count"], 1)
        self.assertEqual(mem.value["items"][0]["index"], 2)
        self.assertEqual(mem.value["items"][0]["text"], "今天去看了海")
        # 取消收藏
        removed = asyncio.run(self.plugin.entry_toggle_memory(date=day, index=2, on=False))
        self.assertEqual(removed.value["total"], 0)
        self.assertEqual(asyncio.run(self.plugin.entry_memories()).value["count"], 0)
        # 越界/缺参数报错
        self.assertIsInstance(
            asyncio.run(self.plugin.entry_toggle_memory(date=day, index=99)), PLUGIN_MODULE.Err
        )

        # 生成章节（with_diary=False）不写日记；立即生成日记才写第一人称日记
        asyncio.run(self.plugin.entry_build_today(with_diary=False))
        self.assertTrue(self.plugin.store.has_chapter(day))
        self.assertFalse(self.plugin.store.has_diary(day))
        asyncio.run(self.plugin.entry_generate_diary(date=day))
        # 模型不可用：立即生成日记也不写（日记留空，等模型可用后再生成）
        self.assertFalse(self.plugin.store.has_diary(day))

    def test_build_all_and_status_reporting(self):
        self._boot()
        asyncio.run(self.plugin.entry_capture(text="第一天的话", role="user", speaker="主人"))
        # 造出另一天的记录，验证 build_all 会逐天处理并上报进度
        from chronicle.models import Turn  # type: ignore

        self.plugin.store.append_turns(
            [Turn(ts=self.now - 3 * 86400, text="三天前说的话", role="user", speaker="主人")]
        )
        result = asyncio.run(self.plugin.entry_build_all(with_diary=False, limit=0))
        self.assertGreaterEqual(result.value["days"], 2)
        self.assertTrue(self.plugin.status_reports)

    def test_finalize_writes_silently_without_notification(self):
        self._boot()
        from chronicle.models import Turn  # type: ignore

        yesterday = Turn(ts=self.now - 86400, text="昨天说的话", role="user", speaker="主人")
        self.plugin.store.append_turns([yesterday])
        # 预置一篇每日日记，确认即使日记存在也不会推送提示
        self.plugin.store.write_diary(yesterday.day, "# 每日日记\n\n昨天很开心。\n")
        built = asyncio.run(self.plugin._finalize_pending())
        self.assertEqual(built, [yesterday.day])
        self.assertTrue(self.plugin.store.has_chapter(yesterday.day))
        self.assertTrue(self.plugin.store.has_diary(yesterday.day))
        self.assertFalse(self.plugin.pushed)

        asyncio.run(self.plugin._finalize_pending())
        # 重复结算也不会推送提示
        self.assertFalse(self.plugin.pushed)

    def test_config_change_and_shutdown(self):
        self._boot()
        # 框架不传新旧配置：变更后由插件自行 dump 最新值
        self.ctx.config.payload["chronicle"].update(
            {"diary_engine": "local", "master_name": "阿岚"}
        )
        changed = asyncio.run(self.plugin.on_config_change())
        self.assertEqual(changed.value["status"], "config_updated")
        self.assertEqual(self.plugin.cfg.master_name, "阿岚")
        self.assertEqual(self.plugin.cfg.diary_engine, "local")
        self.plugin.on_chat_message(text="停机前的一句话", sender="主人")
        stop = asyncio.run(self.plugin.shutdown())
        self.assertEqual(stop.value["status"], "stopped")

    def test_screening_flow_and_auto_pause(self):
        self._boot()
        texts = [
            ("第一句话", "主人"),
            ("第二句话", "猫娘"),
            ("第三句话", "主人"),
            ("第四句话", "猫娘"),
            ("第五句话", "主人"),
        ]
        for text, sender in texts:
            self.plugin.on_chat_message(text=text, sender=sender)
        day = today()

        opened = asyncio.run(self.plugin.entry_screening_open(date=day))
        self.assertIsInstance(opened, PLUGIN_MODULE.Ok)
        self.assertEqual(opened.value["total"], 5)
        self.assertEqual(opened.value["session"]["cursor"], 0)
        self.assertFalse(opened.value["enabled"] is False)

        started = asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="resume", driver="big", who="master"
            )
        )
        self.assertEqual(started.value["session"]["status"], "playing")
        self.assertEqual(started.value["session"]["driver"], "big")

        advanced = asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="advance", count=1, auto=True, driver="big"
            )
        )
        self.assertEqual(advanced.value["session"]["cursor"], 1)

        # 别的窗口不能重复推进
        other = asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="advance", count=1, auto=True, driver="mini"
            )
        )
        self.assertTrue(other.value["ignored"])
        self.assertEqual(other.value["session"]["cursor"], 1)

        # 猫娘在聊天里开口 → 自动暂停
        self.plugin.on_chat_message(text="我想说句话", sender="猫娘")
        current = asyncio.run(self.plugin.entry_screening_state(date=day))
        self.assertEqual(current.value["session"]["status"], "paused")
        self.assertEqual(
            current.value["session"]["last_pause_reason"], "catgirl_spoke"
        )

        # 手记 + 停靠点
        noted = asyncio.run(
            self.plugin.entry_screening_comment(
                date=day, text="这一段我记得喵～", index=1, who="catgirl"
            )
        )
        self.assertEqual(noted.value["session"]["status"], "paused")
        self.assertEqual(noted.value["session"]["marks"][-1]["text"], "这一段我记得喵～")

        stopped = asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="pause", to=2, stop_at=2, note="先看到这里"
            )
        )
        self.assertEqual(stopped.value["session"]["stop_at"], 2)
        self.assertEqual(stopped.value["session"]["cursor"], 2)

        resumed = asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="resume", driver="big"
            )
        )
        self.assertEqual(resumed.value["session"]["stop_at"], 0)
        self.assertEqual(resumed.value["session"]["status"], "playing")

        watched = asyncio.run(self.plugin.tool_screening_watch(date=day, count=2))
        self.assertEqual(watched["session"]["cursor"], 4)
        self.assertEqual(watched["session"]["cat_cursor"], 4)

        invited = asyncio.run(self.plugin.entry_screening_invite(date=day))
        self.assertTrue(invited.value["invited"])
        self.assertEqual(invited.value["session"]["status"], "paused")
        self.assertTrue(self.plugin.pushed)

        # 关闭功能后入口立刻静默：不再读写放映状态
        self.ctx.config.payload["chronicle"]["screening_enabled"] = False
        self.plugin._apply_config(self.ctx.config.payload)
        self.plugin._screening_active_day = ""
        disabled = asyncio.run(self.plugin.entry_screening_open(date=day))
        self.assertIsInstance(disabled, PLUGIN_MODULE.Ok)
        self.assertFalse(disabled.value["enabled"])
        self.assertIsInstance(
            asyncio.run(self.plugin.entry_screening_control(date=day, action="resume")),
            PLUGIN_MODULE.Err,
        )

    def test_catalog_is_silent_until_enabled_and_send_defaults_to_read(self):
        self._boot()
        self.assertFalse(self.plugin.cfg.catalog_enabled)
        self.assertIsNone(self.plugin._catalog_window)
        disabled = asyncio.run(self.plugin.tool_catalog_open(query=""))
        self.assertFalse(disabled["opened"])
        self.assertEqual(disabled["reason"], "catalog_disabled")

        for text, sender in (("第一句话", "主人"), ("第二句话", "猫娘"), ("第三句话", "主人")):
            self.plugin.on_chat_message(text=text, sender=sender)
        day = today()
        rows = self.plugin.catalog_day(day)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["text"], "第二句话")
        self.assertTrue(any(item["date"] == day for item in self.plugin.catalog_days(10)))
        hits = self.plugin.catalog_search("第二句", 10)
        self.assertTrue(any(item.get("text") == "第二句话" for item in hits))
        only_cat = self.plugin.catalog_search("第", 10, speaker="assistant")
        self.assertTrue(all(item.get("role") == "assistant" for item in only_cat))
        scene = self.plugin.catalog_scene(day, 2)
        self.assertTrue(scene)
        self.assertGreaterEqual(len(scene), 3)

        self.ctx.config.payload["chronicle"]["catalog_enabled"] = True
        self.plugin._apply_config(self.ctx.config.payload)
        self.assertTrue(self.plugin.cfg.catalog_enabled)
        self.assertEqual(
            self.plugin._catalog_command_query(
                [types.SimpleNamespace(role="user", text="#拾光 雨天")]
            ),
            "雨天",
        )
        self.assertIsNone(
            self.plugin._catalog_command_query(
                [types.SimpleNamespace(role="assistant", text="#拾光")]
            )
        )

        sent = self.plugin.catalog_send_text(
            "【拾光 · 回忆片段】\n2026-11-03 18:22 主人：今天好累。", respond=False
        )
        self.assertTrue(sent["ok"])
        self.assertEqual(self.plugin.pushed[-1]["visibility"], ["chat"])
        self.assertEqual(self.plugin.pushed[-1]["ai_behavior"], "read")
        self.assertIn("不会打断", sent["message"])
        history = self.plugin.catalog_get_history(5)
        self.assertTrue(history)
        self.assertIn("今天好累", history[-1]["text"])

    def test_screening_review_and_scene_cover(self):
        self._boot()
        for text, sender in (
            ("第一句话", "主人"),
            ("第二句话", "猫娘"),
            ("第三句话", "主人"),
            ("第四句话", "猫娘"),
            ("第五句话", "主人"),
        ):
            self.plugin.on_chat_message(text=text, sender=sender)
        day = today()

        # 放一张最小 PNG 进当天图集，作为可选封面
        image_dir = self.data_root / "static_ui" / "images" / day
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "cover.png").write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                "/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
            )
        )

        asyncio.run(self.plugin.entry_screening_open(date=day))
        asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="resume", driver="big"
            )
        )
        advanced = asyncio.run(
            self.plugin.entry_screening_control(
                date=day, action="advance", count=3, mode="line"
            )
        )
        self.assertEqual(advanced.value["session"]["cursor"], 3)

        selected = asyncio.run(
            self.plugin.entry_screening_set_cover(
                date=day, scene_no=1, filename="cover.png"
            )
        )
        self.assertEqual(selected.value["cover"]["filename"], "cover.png")
        self.assertTrue(selected.value["cover"]["manual"])

        cleared = asyncio.run(
            self.plugin.entry_screening_set_cover(date=day, scene_no=1, filename="")
        )
        self.assertFalse(cleared.value["cover"]["manual"])
        self.assertEqual(cleared.value["cover"]["filename"], "cover.png")

        self.assertIsInstance(
            asyncio.run(
                self.plugin.entry_screening_set_cover(
                    date=day, scene_no=1, filename="not-exist.png"
                )
            ),
            PLUGIN_MODULE.Err,
        )

        first = asyncio.run(self.plugin.entry_screening_reflection(date=day))
        self.assertIsInstance(first, PLUGIN_MODULE.Ok)
        review = first.value["review"]
        self.assertEqual(review["source"], "local")
        self.assertIn("放映厅", review["text"])
        self.assertEqual(first.value["session"]["status"], "paused")
        self.assertEqual(len(first.value["session"]["reviews"]), 1)
        self.assertTrue(
            (self.data_root / "screenings" / "reviews" / f"{day}.md").is_file()
        )

        again = asyncio.run(
            self.plugin.entry_screening_reflection(date=day, regenerate=True)
        )
        self.assertIsInstance(again, PLUGIN_MODULE.Ok)
        self.assertEqual(len(again.value["session"]["reviews"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
