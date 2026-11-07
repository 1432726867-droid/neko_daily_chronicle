"""编年史状态、模型目录与插件配置相关的入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr, ui
from plugin.sdk.shared.core.router import PluginRouter

from ..chronicle import config as config_mod
from ..chronicle import llm as llm_mod
from ..chronicle.models import local_day_key


class ConfigRouter(PluginRouter):
    """编年史状态、模型目录与插件配置相关的入口。"""

    def __init__(self):
        super().__init__(name="config")

    @plugin_entry(
        id="status",
        name=tr("entries.status.name", default="编年史状态"),
        description=tr(
            "entries.status.description",
            default="查看已记录的天数、章节与日记数量、模型可用性和数据目录。",
        ),
        llm_result_fields=["days", "chapters", "diaries", "llm", "data_root"],
    )
    async def entry_status(self, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        stats = self.main_plugin.store.stats()
        return Ok(
            {
                "days": stats["days"],
                "chapters": stats["chapters"],
                "diaries": stats["diaries"],
                "first_day": stats["first_day"],
                "last_day": stats["last_day"],
                "today_turns": self.main_plugin.store.count_turns(local_day_key()),
                "llm": self.main_plugin._llm_status(),
                "capture": self.main_plugin._capture_status(),
                "data_root": stats["root"],
                "copy_phrase": {
                    "prefix": self.main_plugin.cfg.copy_phrase_prefix,
                    "mid": self.main_plugin.cfg.copy_phrase_mid,
                    "index_unit": self.main_plugin.cfg.copy_phrase_index_unit,
                    "suffix": self.main_plugin.cfg.copy_phrase_suffix,
                },
                "screening_enabled": bool(self.main_plugin.cfg.screening_enabled),
                "config_warnings": list(self.main_plugin.cfg.warnings),
            }
        )

    @ui.action(
        label=tr("actions.checkLlm.label", default="检查模型是否可用"),
        tone="primary",
        group="chronicle",
        order=5,
        refresh_context=True,
    )
    @plugin_entry(
        id="check_llm",
        name=tr("entries.checkLlm.name", default="检查写日记的模型是否可用"),
        description=tr(
            "entries.checkLlm.description",
            default="解析当前配置的模型端点，并发送一条最小测试请求，报告模型是否真的可用。",
        ),
        llm_result_fields=["available", "reason", "detail"],
    )
    async def entry_check_llm(self, **_):
        self.main_plugin._refresh_llm()
        result = await self.main_plugin._probe_llm()
        return Ok(result)

    @plugin_entry(
        id="list_models",
        name=tr("entries.listModels.name", default="读取模型目录"),
        description=tr(
            "entries.listModels.description",
            default="读取浏览页「模型配置」里的自定义模型列表与当前选中项。",
        ),
        llm_result_fields=["active", "models"],
    )
    async def entry_list_models(self, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        catalog = self.main_plugin._load_catalog()
        active = catalog["active"]
        if not active and len(catalog["models"]) == 1:
            only = catalog["models"][0]
            if only["base_url"] and only["api_key"]:
                active = only["id"]
        # 回传时剥离 api_key，只带 has_key
        public = [
            {
                "id": m["id"],
                "name": m["name"],
                "base_url": m["base_url"],
                "has_key": bool(m["api_key"]),
            }
            for m in catalog["models"]
        ]
        return Ok(
            {
                "active": active,
                "models": public,
                "endpoint_base_url": self.main_plugin.llm_endpoint.base_url if self.main_plugin.llm_endpoint else "",
                "configured": self.main_plugin.llm_endpoint is not None,
            }
        )

    @plugin_entry(
        id="save_models",
        name=tr("entries.saveModels.name", default="保存模型目录"),
        description=tr(
            "entries.saveModels.description",
            default="保存浏览页「模型配置」里的模型列表，并把选中项设为写日记所用模型。",
        ),
        llm_result_fields=["saved", "active", "count"],
    )
    async def entry_save_models(self, models: Any = None, active: Any = None, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        old = {m["id"]: m for m in self.main_plugin._load_catalog()["models"]}
        cleaned: list[dict] = []
        if isinstance(models, list):
            for item in models:
                if isinstance(item, dict):
                    mid = str(item.get("id") or item.get("model") or "").strip()
                    if not mid:
                        continue
                    key = str(item.get("api_key") or "")
                    if not key and mid in old:
                        key = old[mid]["api_key"]  # 未填新值则保留原密钥
                    cleaned.append(
                        {
                            "id": mid,
                            "name": str(item.get("name") or mid).strip() or mid,
                            "base_url": str(item.get("base_url") or "").strip(),
                            "api_key": key,
                        }
                    )
        active_str = str(active).strip() if isinstance(active, str) and active.strip() else None
        if active_str and active_str not in {m["id"] for m in cleaned}:
            active_str = None
        self.main_plugin._save_catalog({"active": active_str, "models": cleaned})
        self.main_plugin._refresh_llm()
        return Ok({"saved": True, "active": active_str, "count": len(cleaned)})

    @plugin_entry(
        id="fetch_models",
        name=tr("entries.fetchModels.name", default="获取可用模型"),
        description=tr(
            "entries.fetchModels.description",
            default="用指定模型的 API 地址与密钥请求 /models，列出该服务商下可用的模型 ID。",
        ),
        llm_result_fields=["available", "count"],
    )
    async def entry_fetch_models(
        self, id: Any = None, base_url: Any = None, api_key: Any = None, **_
    ):
        # 优先用表单里临时填的 base_url/api_key；否则用已存的 id 那条
        url = str(base_url or "").strip()
        key = str(api_key or "")
        if not url and isinstance(id, str) and id.strip():
            entry = self.main_plugin._catalog_entry(id.strip())
            if entry:
                url = entry["base_url"]
                key = entry["api_key"]
        if not url:
            return Ok({"available": [], "count": 0, "error": "no_url", "detail": "请先填写 API 地址"})
        endpoint = llm_mod.LlmEndpoint(base_url=url, model="", api_key=key)
        names, reason = await asyncio.to_thread(llm_mod.list_available_models, endpoint)
        if names is None:
            return Ok(
                {
                    "available": [],
                    "count": 0,
                    "error": reason,
                    "detail": llm_mod.reason_text(reason) or reason,
                }
            )
        return Ok({"available": names, "count": len(names)})

    @plugin_entry(
        id="get_config",
        name=tr("entries.getConfig.name", default="读取插件配置"),
        description=tr(
            "entries.getConfig.description",
            default="读取浏览页「插件配置」里的全部可编辑项与当前值（模型 API Key 不回传明文）。",
        ),
        llm_result_fields=["fields", "values", "has_llm_api_key"],
    )
    async def entry_get_config(self, **_):
        fields = self.main_plugin._config_schema_fields()
        values: dict[str, Any] = {}
        for field in fields:
            key = field["key"]
            if field.get("write_only"):
                # 密钥永远不回传明文，前端按“留空=保持原值”处理
                values[key] = ""
            else:
                values[key] = getattr(self.main_plugin.cfg, key, field.get("default"))
        return Ok(
            {
                "fields": fields,
                "values": values,
                "has_llm_api_key": bool(self.main_plugin.cfg.llm_api_key),
                "warnings": list(self.main_plugin.cfg.warnings),
            }
        )

    @plugin_entry(
        id="save_config",
        name=tr("entries.saveConfig.name", default="保存插件配置"),
        description=tr(
            "entries.saveConfig.description",
            default="把浏览页「插件配置」提交的改动写进 [chronicle] 配置段，保存后立即生效。",
        ),
        llm_result_fields=["saved", "changed", "warnings"],
    )
    async def entry_save_config(self, values: Any = None, **_):
        if not isinstance(values, dict) or not values:
            return Err(SdkError("没有要保存的配置项"))
        fields = {item["key"]: item for item in self.main_plugin._config_schema_fields()}
        try:
            dumped = await self.main_plugin.config.dump()
        except Exception as exc:  # noqa: BLE001 - 读不到现有配置就无法安全合并
            return Err(SdkError(f"读取现有配置失败：{exc}"))
        base = dict(dumped) if isinstance(dumped, Mapping) else {}
        current = base.get("chronicle")
        section = dict(current) if isinstance(current, Mapping) else {}
        changed: list[str] = []
        for key, value in values.items():
            field = fields.get(key)
            if field is None:
                continue  # 只允许写 schema 里声明过的字段
            if field.get("write_only"):
                text = str(value or "").strip()
                if not text:
                    continue  # 密钥留空 = 保持原值，不清空已保存的密钥
                section[key] = text
            else:
                section[key] = value
            changed.append(key)
        if not changed:
            return Ok({"saved": True, "changed": [], "warnings": []})
        merged = {**base, "chronicle": section}
        parsed = config_mod.ChronicleConfig.from_mapping(merged)
        # 只关心本次提交字段的非法值；其它历史遗留警告不回灌给用户。
        # 文本字段被清空是合法的（表示回落默认值），不算非法。
        bad: list[str] = []
        for warning in parsed.warnings:
            hit = [key for key in changed if warning.startswith(f"{key}=")]
            if not hit:
                continue
            if all(
                isinstance(values.get(key), str) and not values[key].strip()
                for key in hit
            ):
                continue
            bad.append(warning)
        if bad:
            return Ok({"saved": False, "changed": [], "warnings": bad})
        persist = {f"chronicle.{key}": section[key] for key in changed}
        try:
            await self.main_plugin.config.update(persist, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - 保存失败要把原因带回页面
            return Err(SdkError(f"保存配置失败：{exc}"))
        self.main_plugin._apply_config(merged)
        return Ok(
            {"saved": True, "changed": changed, "warnings": list(parsed.warnings)}
        )
