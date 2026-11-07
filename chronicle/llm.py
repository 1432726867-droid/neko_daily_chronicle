"""语言模型直连：复用 N.E.K.O 已配置的模型，或使用插件自己填的端点。

为什么需要这一层：官方文档里插件**没有**"让宿主模型帮我写一段话"的 API，
``@llm_tool`` 只是把插件能力注册给模型调用，方向是反的。因此想写日记只有
两条路——本地统计（``diary`` 模块）或由插件自己直连一个模型。

本模块沿用 ``forever_companion`` 验证过的做法：读宿主 ``core_config.json``
的模型槽位，用标准库 ``urllib`` 直连 OpenAI 兼容的 ``/chat/completions``，
零第三方依赖。需要注意的边界：

* 宿主走 ``coreApi = "free"``（lanlan.tech 免费代理）时，服务端会校验客户端，
  第三方直连必被拒（返回 400 "not using Lanlan"），因此这里**明确拒绝**
  复用该路由，而不是让它默默失败；
* 任何失败都不抛异常，返回原因字符串，由调用方降级成本地摘要；
* 日志与 UI 快照里绝不出现 api_key，只报告是否已配置。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

#: 免费代理的域名标记：出现即视为不可由插件直连。
FREE_ROUTE_MARKERS = ("lanlan.tech",)

#: 按优先级尝试的模型槽位。宿主 core_config.json 里每个槽位是一族字段：
#: ``{stem}ModelProvider`` / ``{stem}ModelUrl`` / ``{stem}ModelId`` /
#: ``{stem}ModelApiKey``（例如 summaryModelProvider … summaryModelApiKey）。
SLOT_STEMS = (
    "summary",
    "agent",
    "conversation",
    "emotion",
    "correction",
)


def slot_field(stem: str, field: str) -> str:
    """槽位字段名：``slot_field("summary", "ModelId")`` → ``summaryModelId``。"""

    return f"{stem}Model{field}"

#: provider → 宿主保存该家 key 的字段名。
ASSIST_KEY_FIELDS = {
    "qwen": "assistApiKeyQwen",
    "qwen_intl": "assistApiKeyQwenIntl",
    "deepseek": "assistApiKeyDeepseek",
    "glm": "assistApiKeyGlm",
    "zhipu": "assistApiKeyGlm",
    "step": "assistApiKeyStep",
    "silicon": "assistApiKeySilicon",
    "siliconflow": "assistApiKeySilicon",
    "kimi": "assistApiKeyKimi",
    "moonshot": "assistApiKeyKimi",
    "doubao": "assistApiKeyDoubao",
    "minimax": "assistApiKeyMinimax",
    "mimo": "assistApiKeyMimo",
    "openai": "assistApiKeyOpenai",
    "gemini": "assistApiKeyGemini",
    "grok": "assistApiKeyGrok",
    "claude": "assistApiKeyClaude",
    "anthropic": "assistApiKeyClaude",
    "openrouter": "assistApiKeyOpenrouter",
}

#: 已知服务商的 OpenAI 兼容端点，仅在宿主没有保存 URL 时兜底。
DEFAULT_PROVIDER_URLS = {
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen_intl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "step": "https://api.stepfun.com/v1",
    "silicon": "https://api.siliconflow.cn/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "minimax": "https://api.minimax.chat/v1",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "grok": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

REASON_TEXT = {
    "explicit": "使用插件配置里填写的端点",
    "host_slot": "复用宿主配置的模型槽位",
    "free_route": "宿主当前走 lanlan.tech 免费路由，插件直连会被服务端拒绝；"
                  "请在 N.E.K.O 里配置自己的 API，或在插件配置里填写 llm_base_url/llm_model",
    "no_slot": "宿主 core_config.json 里没有可直连的模型槽位",
    "no_model": "模型槽位缺少 URL 或模型名，无法直连",
    "no_config": "找不到宿主 core_config.json",
    "plugin_only": "配置要求只用插件自己的端点，但没有填写 llm_base_url/llm_model",
    "disabled": "日记引擎被配置为仅本地摘要",
    "http_error": "请求模型失败",
    "timeout": "请求模型超时",
    "empty": "模型返回了空内容",
    "ssl_error": "连接模型时 TLS 失败",
}


@dataclass
class LlmEndpoint:
    """一次可直连的端点描述（``api_key`` 不参与任何日志输出）。"""

    base_url: str
    model: str
    api_key: str = ""
    provider: str = ""
    source: str = "host_slot"

    def describe(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "provider": self.provider,
            "source": self.source,
            "has_key": bool(self.api_key),
        }

    def __repr__(self) -> str:  # pragma: no cover - 只为避免密钥进日志
        return (
            f"LlmEndpoint(base_url={self.base_url!r}, model={self.model!r}, "
            f"provider={self.provider!r}, source={self.source!r}, has_key={bool(self.api_key)})"
        )


# ── 宿主配置定位 ────────────────────────────────────────────────────


def candidate_config_paths(user_data_root: Path | str | None = None) -> list[Path]:
    """按可能性排序的 core_config.json 候选路径。"""

    candidates: list[Path] = []
    if user_data_root:
        candidates.append(Path(user_data_root) / "config" / "core_config.json")
    for env_name in ("NEKO_DATA_DIR", "NEKO_USER_DATA_DIR"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value) / "config" / "core_config.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "N.E.K.O" / "config" / "core_config.json")
    roaming = os.environ.get("APPDATA")
    if roaming:
        candidates.append(Path(roaming) / "N.E.K.O" / "config" / "core_config.json")
    candidates.append(Path.home() / ".local" / "share" / "N.E.K.O" / "config" / "core_config.json")
    candidates.append(Path.home() / "Library" / "Application Support" / "N.E.K.O" / "config" / "core_config.json")
    return candidates


def locate_core_config(user_data_root: Path | str | None = None) -> tuple[Path | None, dict]:
    """找到并读取宿主配置，返回 ``(路径, 内容)``。"""

    for path in candidate_config_paths(user_data_root):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return path, data
    return None, {}


# ── 槽位解析 ────────────────────────────────────────────────────────


def _resolved_provider_url(core_cfg: Mapping[str, Any], book: str, provider: str) -> str:
    urls = core_cfg.get("resolvedProviderUrls")
    if isinstance(urls, Mapping):
        group = urls.get(book)
        if isinstance(group, Mapping):
            value = group.get(provider)
            if value:
                return str(value).strip()
    return ""


def _is_free_route(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in FREE_ROUTE_MARKERS)


def _provider_base_url(core_cfg: Mapping[str, Any], provider: str, book: str = "assist") -> str:
    saved = _resolved_provider_url(core_cfg, book, provider) or _resolved_provider_url(core_cfg, "core", provider)
    if saved:
        return saved
    return DEFAULT_PROVIDER_URLS.get(provider, "")


def _resolve_slot(core_cfg: Mapping[str, Any], stem: str) -> tuple[LlmEndpoint | None, str]:
    """解析一个模型槽位；返回 ``(endpoint, reason)``。"""

    provider = str(core_cfg.get(slot_field(stem, "Provider")) or "").strip()
    model = str(core_cfg.get(slot_field(stem, "Id")) or "").strip()
    slot_url = str(core_cfg.get(slot_field(stem, "Url")) or "").strip()
    slot_key = str(core_cfg.get(slot_field(stem, "ApiKey")) or "").strip()

    # 1) 槽位自带 URL + 模型名：直接可连（本地端点常常没有 key）
    if slot_url and model:
        if provider in ("follow_core", "follow_assist", "", "custom"):
            # custom / 跟随：槽位自己的 URL 已经够用
            if provider != "custom" and _is_free_route(slot_url):
                return None, "free_route"
            key = slot_key
            if not key and provider not in ("custom", ""):
                field = ASSIST_KEY_FIELDS.get(provider)
                if field:
                    key = str(core_cfg.get(field) or "").strip()
            return (
                LlmEndpoint(
                    base_url=slot_url,
                    model=model,
                    api_key=key,
                    provider=provider or "custom",
                    source="host_slot",
                ),
                "host_slot",
            )
        key = slot_key or str(core_cfg.get(ASSIST_KEY_FIELDS.get(provider, ""), "") or "").strip()
        return (
            LlmEndpoint(base_url=slot_url, model=model, api_key=key, provider=provider, source="host_slot"),
            "host_slot",
        )

    # 2) 跟随核心 / 辅助 API：借用宿主的 provider 与 key
    if provider in ("", "follow_core", "follow_assist"):
        if not model:
            return None, "no_model"
        book = "core" if provider == "follow_core" else "assist"
        route_provider = str(core_cfg.get("coreApi" if book == "core" else "assistApi") or "").strip()
        if not route_provider:
            route_provider = "free" if str(core_cfg.get("coreApi") or "").strip() == "free" else "qwen"
        if route_provider == "free":
            return None, "free_route"
        base_url = _provider_base_url(core_cfg, route_provider, book)
        if _is_free_route(base_url):
            return None, "free_route"
        if not base_url:
            return None, "no_model"
        key = ""
        if book == "core":
            key = str(core_cfg.get("coreApiKey") or "").strip()
        else:
            field = ASSIST_KEY_FIELDS.get(route_provider)
            if field:
                key = str(core_cfg.get(field) or "").strip()
        key = key or slot_key
        return (
            LlmEndpoint(
                base_url=base_url,
                model=model,
                api_key=key,
                provider=route_provider,
                source="host_slot",
            ),
            "host_slot",
        )

    # 3) 具名服务商：需要模型名 + 已知/已保存的 URL
    if not model:
        return None, "no_model"
    base_url = _provider_base_url(core_cfg, provider)
    if _is_free_route(base_url):
        return None, "free_route"
    if not base_url:
        return None, "no_model"
    field = ASSIST_KEY_FIELDS.get(provider)
    key = slot_key or (str(core_cfg.get(field) or "").strip() if field else "")
    return (
        LlmEndpoint(base_url=base_url, model=model, api_key=key, provider=provider, source="host_slot"),
        "host_slot",
    )


def resolve_endpoint(config: Any, core_cfg: Mapping[str, Any] | None) -> tuple[LlmEndpoint | None, str]:
    """决定这次能不能直连模型，返回 ``(endpoint, reason)``。"""

    provider_mode = str(getattr(config, "llm_provider", "auto") or "auto")
    engine = str(getattr(config, "diary_engine", "auto") or "auto")
    if engine == "local":
        return None, "disabled"

    explicit_url = str(getattr(config, "llm_base_url", "") or "").strip()
    explicit_model = str(getattr(config, "llm_model", "") or "").strip()
    if explicit_url and explicit_model:
        return (
            LlmEndpoint(
                base_url=explicit_url,
                model=explicit_model,
                api_key=str(getattr(config, "llm_api_key", "") or "").strip(),
                provider="plugin",
                source="explicit",
            ),
            "explicit",
        )

    if provider_mode == "plugin":
        return None, "plugin_only"
    if not isinstance(core_cfg, Mapping) or not core_cfg:
        return None, "no_config"

    last_reason = "no_slot"
    for stem in SLOT_STEMS:
        endpoint, reason = _resolve_slot(core_cfg, stem)
        if endpoint is not None:
            return endpoint, reason
        if reason == "free_route":
            last_reason = "free_route"
        elif reason == "no_model" and last_reason == "no_slot":
            last_reason = "no_model"
    return None, last_reason


def reason_text(reason: str) -> str:
    return REASON_TEXT.get(reason, reason or "")


# ── 直连 ────────────────────────────────────────────────────────────


def _completion_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def chat_completion(
    endpoint: LlmEndpoint,
    prompt: str,
    *,
    timeout: float = 60.0,
    temperature: float = 0.4,
    logger: Any = None,
) -> tuple[str | None, str]:
    """同步直连 OpenAI 兼容接口，返回 ``(文本, 原因)``；失败时文本为 None。"""

    import socket

    url = _completion_url(endpoint.base_url)
    body = json.dumps(
        {
            "model": endpoint.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"

    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001 - 读取错误体失败不影响降级
            detail = ""
        if logger is not None:
            logger.warning("chat completion HTTP {}: {}", exc.code, detail)
        return None, f"http_error:{exc.code}"
    except socket.timeout:
        return None, "timeout"
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if logger is not None:
            logger.warning("chat completion URLError: {}", reason)
        return None, "timeout" if "timed out" in reason.lower() else "http_error"
    except Exception as exc:  # noqa: BLE001 - 任何异常都降级，不打断记录
        if logger is not None:
            logger.warning("chat completion failed: {}", exc)
        return None, "http_error"

    try:
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
            if isinstance(message, Mapping):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content, ""
                if isinstance(content, list):
                    text = "".join(
                        str(part.get("text", ""))
                        for part in content
                        if isinstance(part, Mapping)
                    )
                    if text.strip():
                        return text, ""
        # 少数服务商把结果放在 text/completion
        for key in ("text", "completion", "output_text"):
            value = payload.get(key) if isinstance(payload, Mapping) else None
            if isinstance(value, str) and value.strip():
                return value, ""
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.warning("chat completion parse failed: {}", exc)
        return None, "empty"
    return None, "empty"


async def achat_completion(
    endpoint: LlmEndpoint,
    prompt: str,
    *,
    timeout: float = 60.0,
    temperature: float = 0.4,
    logger: Any = None,
) -> tuple[str | None, str]:
    """异步包装：把阻塞的 urllib 调用丢到工作线程，不卡住事件循环。"""

    import asyncio

    return await asyncio.to_thread(
        chat_completion,
        endpoint,
        prompt,
        timeout=timeout,
        temperature=temperature,
        logger=logger,
    )


#: 这些原因重试也不会成功（鉴权/参数/配置问题），直接返回避免浪费时间和额度。
NON_RETRYABLE_REASONS = frozenset(
    {
        "http_error:400",
        "http_error:401",
        "http_error:403",
        "http_error:404",
        "disabled",
        "free_route",
        "no_config",
        "no_model",
        "no_slot",
        "plugin_only",
    }
)


async def achat_completion_with_retry(
    endpoint: LlmEndpoint,
    prompt: str,
    *,
    attempts: int = 3,
    timeout: float = 60.0,
    temperature: float = 0.4,
    logger: Any = None,
    retry_delay: float = 1.0,
) -> tuple[str | None, str, int]:
    """带重试的模型请求，返回 ``(文本, 原因, 实际尝试次数)``。

    只对超时、5xx、429、空响应等瞬时失败重试；鉴权/参数类错误立即返回。
    调用方应保证 ``attempts * timeout`` 小于入口自身的超时预算。
    """

    import asyncio

    total = max(1, int(attempts or 1))
    reason = ""
    for attempt in range(1, total + 1):
        text, reason = await achat_completion(
            endpoint,
            prompt,
            timeout=timeout,
            temperature=temperature,
            logger=logger,
        )
        if text:
            return text, "", attempt
        if reason in NON_RETRYABLE_REASONS:
            return None, reason, attempt
        if attempt < total:
            wait = max(0.0, float(retry_delay) * attempt)
            if logger is not None:
                logger.warning(
                    "模型请求失败（{}），{:.0f} 秒后重试（{}/{}）",
                    reason,
                    wait,
                    attempt + 1,
                    total,
                )
            await asyncio.sleep(wait)
    return None, reason, total


def list_available_models(endpoint: LlmEndpoint) -> tuple[list[str] | None, str]:
    """请求 OpenAI 兼容端点的 ``GET /models``，返回模型 ID 列表。

    返回 ``(模型列表, 原因)``；失败时列表为 ``None``，原因是可展示的错误码。
    """

    import socket

    base = (endpoint.base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        url = base[: -len("/chat/completions")] + "/models"
    elif base.endswith("/v1"):
        url = base + "/models"
    else:
        url = base + "/models"

    headers = {"Accept": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return None, f"http_error:{exc.code}"
    except (socket.timeout, TimeoutError):
        return None, "timeout"
    except urllib.error.URLError:
        return None, "http_error"
    except Exception:  # noqa: BLE001 - 任何异常都降级
        return None, "http_error"

    items = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return None, "empty"
    names = []
    for item in items:
        if isinstance(item, Mapping):
            mid = item.get("id")
            if isinstance(mid, str) and mid.strip():
                names.append(mid.strip())
    return sorted(set(names)), ""


__all__ = [
    "ASSIST_KEY_FIELDS",
    "DEFAULT_PROVIDER_URLS",
    "LlmEndpoint",
    "NON_RETRYABLE_REASONS",
    "REASON_TEXT",
    "SLOT_STEMS",
    "achat_completion",
    "achat_completion_with_retry",
    "candidate_config_paths",
    "chat_completion",
    "list_available_models",
    "locate_core_config",
    "reason_text",
    "resolve_endpoint",
    "slot_field",
]
