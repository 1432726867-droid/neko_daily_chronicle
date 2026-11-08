"""对话编年史（neko_daily_chronicle）—— N.E.K.O 插件入口。

做三件事：

1. **完整留存**：``@message(source="chat")`` 实时接住每一条聊天消息，
   ``@timer_interval`` 定时从消息总线补录漏掉的轮次，两条来源按指纹去重，
   按日期写进只追加的 ``raw/YYYY-MM-DD.jsonl``；
2. **编成章节**：按日期把当天原话编排成小说式章节 ``chapters/YYYY-MM-DD.md``
   （分场景、标时间、标说话人，不改写原话）；
3. **另写日记**：为每一章生成 ``diary/YYYY-MM-DD.md``——复用宿主已配置的
   模型写一篇真日记；**模型不可用时日记留空**（不写文件，等模型可用后再生成）。

设计取向：**原始记录先行**。章节和日记都可以随时重算，原始 JSONL 不会被覆盖，
因此换模型、改格式、修 bug 都不会丢掉已经记下的对话。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import io
import json
import os
import random
import re
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from plugin.sdk.plugin import (
    Err,  # re-export：测试与外部代码仍从插件模块取 Err
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    message,
    neko_plugin,
    timer_interval,
    ui,
)

from .chronicle import chapter as chapter_mod
from .chronicle import capsule as capsule_mod
from .chronicle import config as config_mod
from .chronicle import diary as diary_mod
from .chronicle import harvest, host_store, llm as llm_mod
from .chronicle import memo as memo_mod
from .chronicle import catalog as catalog_mod
from .chronicle import reader as reader_mod
from .chronicle import screening as screening_mod
from .chronicle.memory import MemoryStore
from .chronicle.models import (
    ROLE_UNKNOWN,
    ROLE_USER,
    Turn,
    local_day_key,
    numbered_turns,
    visible_turns,
)
from .chronicle.store import ChronicleStore

PLUGIN_ID = "neko_daily_chronicle"

#: 定时器间隔在装饰器求值时固定，因此直接写字面量（官方 check --release 也要求
#: seconds 是字面数值）：60 秒补录 / 600 秒收尾 / 1800 秒备忘录 / 3600 秒时间胶囊。

#: 拾光开启时，用多快的轻量轮询接住 ``#拾光`` 命令；关闭时这个任务不会创建。
#: 补录是 60 秒一轮，命令等不了那么久，所以单独走一个只读几条记录的短轮询。
CATALOG_WATCH_SECONDS = 5.0

#: finalize_tick 的"每天定点结算"调度参数（本地时间）。
#: 每天 FINALIZE_HOUR 点开始第一次尝试；失败后每隔 FINALIZE_RETRY_SECONDS 重试一次，
#: 当天最多 FINALIZE_MAX_ATTEMPTS 次（含首次），3 次都失败今天不再尝试，次日再来。
FINALIZE_HOUR = 18
FINALIZE_RETRY_SECONDS = 600
FINALIZE_MAX_ATTEMPTS = 3

#: 面板里单段正文最多回传多少字符（避免一次 UI 上下文过大）。
PANEL_TEXT_LIMIT = 16000

#: 面板列表最多列多少天（0 = 全部，避免旧日期从列表里消失）。
PANEL_DAY_LIMIT = 0

#: 解析后的轮次按天缓存的条数上限（超过就丢最久未用的那天，避免长年运行内存增长）。
TURNS_CACHE_DAYS = 16

#: 从日记开头抓心情：本地摘要写"心情推测：X"，模型日记写"> 😊 X ｜ 关键词：…"。
_MOOD_RE = re.compile(r"心情推测：(\S+)|> (\S+) (\S+) ｜")

#: 「爱妻日记」是每日日记的专用触发词：主人发送「爱妻日记」+日期时，
#: 才把那一天自动生成的日记递回给猫娘；平时对话里说「日记」都指猫娘自己那篇。
LOVE_DIARY_TRIGGER = "爱妻日记"
LOVE_DIARY_MAX_CHARS = 4000

#: 主人用「我曾经在10月6号第13条和你说过」这种说法引用某条原话时，
#: 插件直接把那条原文（说话人 + 原话）递回给猫娘，她不用再去查一次。
#: 有日期就够明确；没写日期时，要带上下面这些"在回忆"的词才触发，
#: 免得「第13条规则」这种普通句子被误当成引用。
REFERENCE_HINTS = ("说过", "聊过", "提过", "讲过", "原文", "念", "记得", "记录")
#: 「第N条」里的单位词。
REFERENCE_UNITS = ("条", "句", "行")
#: 序号：第13条 / 第十三句 / 最后一条 / 倒数第二条。
_TURN_INDEX_RE = re.compile(r"(?:倒数)?第\s*([0-9零〇一二三四五六七八九十百]+)\s*[条句行]")
_TURN_LAST_RE = re.compile(r"(?:倒数第\s*([0-9零〇一二三四五六七八九十百]+)\s*[条句行])|(最后\s*(?:一)?[条句行])")

#: 模糊回忆：主人只提一个关键词（「我说过油菜花吗」「还记得油菜花吗」）。
_MEMORY_CUE_RE = re.compile(
    r"(?:我说过|我提过|我讲过|我聊过|我跟你说过|我和你说过|和你说过|跟你说过"
    r"|我之前说过|我以前说过|我曾经说过|之前说过|以前说过"
    r"|还记不记得|记不记得|还记得|记得|记着)"
)
#: 从这句话里剥掉的口语词，剩下的就是检索关键词。
_MEMORY_NOISE = (
    "我跟你说过", "我和你说过", "我跟你讲过", "我跟你提过", "我之前说过", "我以前说过",
    "我曾经说过", "之前说过", "以前说过", "我说过", "我提过", "我讲过", "我聊过",
    "还记不记得", "记不记得", "你还记得", "你记得", "记得吗", "还记得",
    "的时候", "那会儿", "那次", "那个", "是不是", "有没有", "和你说过", "跟你说过",
    "曾经", "之前", "以前", "你说过", "说过", "提过", "讲过",
    "吗", "么", "嘛", "呢", "呀", "啊", "吧", "？", "?", "！", "!", "，", "。", "、", "…",
    "「", "」", "\u201c", "\u201d",
)
#: 剥完回忆词后剩下的主语碎片，不算关键词。
_MEMORY_SUBJECTS = ("我", "你", "咱", "他", "她", "它", "们", "我们", "你们", "咱们")
#: 模糊回忆里"发生了什么"的素材：优先模型写的日记，其次备忘录。
#: 日记头部这两句话决定了它是"模型叙事"还是"本地统计摘要"。
DIARY_LLM_MARK = "由 语言模型"
DIARY_LOCAL_MARK = "未调用语言模型"
#: 「发生了什么」最多多少字，按句号截断，不硬切。
HAPPENED_MAX_CHARS = 120
#: 备忘录段落的前缀。
MEMO_LINE_PREFIX = re.compile(r"^(?:先前对话的备忘录|备忘录|[-\u2013•·])\s*[:：]?\s*")
#: 备忘录段落开头的时间戳（「2026年10月18日19:30起，」「18:53，」），读起来没用，清掉。
MEMO_LEAD_TIME = re.compile(
    r"^(?:(?:\d{4}年)?\d{1,2}月\d{1,2}日(?:周[一二三四五六日])?"
    r"|(?:\d{4}年)?\d{1,2}[:：]\d{2}(?:[-–~～]\d{1,2}[:：]\d{2})?"
    r"|周[一二三四五六日])\s*"
    r"(?:起)?\s*[，,、:：]?\s*"
)
#: 备忘录是旁白体：出现这些字样越多，越不适合直接当"她说的"。
_NARRATION_MARKS = ("YUI", "称", "其", "它", "两人", "他")


#: 章节图片：存在插件可写静态目录的 images/<日期>/ 下，
#: 由宿主静态服务按 /plugin/<id>/ui/images/... 直接返回，浏览页不用走 base64。
IMAGE_DIR_NAME = "images"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
#: 单张图片上限（解码后字节数）。再大就走 /runs 传参不划算。
IMAGE_MAX_BYTES = 25 * 1024 * 1024
#: 浏览页「相册合集」一次最多返回多少张（只回元信息，不回图片本体）。
IMAGE_LIST_LIMIT = 1000
#: 图片记忆最多允许的字数。
IMAGE_MEMORY_MAX_CHARS = 2000
#: 推送给猫娘时，单张图直接走 base64 的上限；超过就改发缩略图，避免消息过大。
PUSH_IMAGE_MAX_BYTES = 6 * 1024 * 1024
#: 相册预览用的缩略图：放在 ``images/<日期>/thumbs/``，长边不超过 480px。
THUMB_DIR_NAME = "thumbs"
THUMB_MAX_SIDE = 480
THUMB_QUALITY = 82
#: Pillow 惰性导入 + 失败缓存（宿主没装也不影响原图浏览）。
_PIL_CACHE: dict[str, Any] = {"checked": False}


def _pil_modules() -> tuple[Any, Any]:
    """惰性导入 Pillow；失败后缓存结果，避免反复 import。"""

    if not _PIL_CACHE["checked"]:
        _PIL_CACHE["checked"] = True
        try:
            from PIL import Image, ImageOps

            _PIL_CACHE.update(ok=True, image=Image, ops=ImageOps)
        except Exception as exc:  # noqa: BLE001 - 没装 Pillow 也要能浏览原图
            _PIL_CACHE.update(ok=False, error=str(exc))
    if _PIL_CACHE.get("ok"):
        return _PIL_CACHE["image"], _PIL_CACHE["ops"]
    return None, None


def _text_overlap(source: str, result: str) -> float:
    """原备忘录的实义字符有多大比例仍出现在结果里（防止模型返回无关内容）。"""

    ignored = set("，。！？、；：,.!?;: \t\r\n-—…（）()【】[]“”\"'‘’")
    source_chars = {ch for ch in source if ch not in ignored}
    if not source_chars:
        return 1.0
    result_chars = {ch for ch in result if ch not in ignored}
    return len(source_chars & result_chars) / len(source_chars)


def _as_tag_list(value: Any) -> list[str]:
    """把标签输入（字符串或列表）规整成去重后的标签列表。"""

    if isinstance(value, str):
        parts = re.split(r"[,，、;；\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    out: list[str] = []
    for part in parts:
        tag = part.strip()
        if tag and tag not in out:
            out.append(tag)
    return out


#: 中文数字 → 阿拉伯数字（用于猫娘说"10月7号第13条"这类自然表达）。
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _chinese_to_int(text: Any) -> int | None:
    """把"13"或"十三""二十三""十"转成阿拉伯数字；无法识别返回 None。"""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if all(ch in _CN_DIGITS for ch in raw):
        # 纯中文数字拼接，如 二〇二六 → 2026
        value = 0
        for ch in raw:
            value = value * 10 + _CN_DIGITS[ch]
        return value
    if len(raw) == 1:
        return _CN_DIGITS.get(raw)
    try:
        return int(raw)
    except ValueError:
        return None


#: 相对日期的中文词（按本地"今天"换算；负数表示未来）。
_CN_RELATIVE_DAYS = {
    "今天": 0, "今日": 0,
    "昨天": 1, "昨日": 1,
    "前天": 2, "前日": 2,
    "明天": -1, "明日": -1,
    "后天": -2, "后日": -2,
    "大后天": -3,
}


def _normalize_day_text(text: str) -> str:
    """把用户/模型说的自然中文日期收敛成 YYYY-MM-DD。

    支持：今天/昨天/前天/明天/后天；M月D号或日；YYYY年M月D号或日；年月日里的中文数字
    （七月七号、十月七日、二〇二六年十月七日）也认。识别不了返回空串。
    """

    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw in _CN_RELATIVE_DAYS:
        delta = _CN_RELATIVE_DAYS[raw]
        return time.strftime("%Y-%m-%d", time.localtime(time.time() - delta * 86400))

    # 按 年/月/日/号/-/. 切段，每一段用 _chinese_to_int 转阿拉伯。
    parts = re.split(r"[年月日号/\-\.]", raw)
    nums = [_chinese_to_int(part) for part in parts if part.strip()]
    try:
        if len(nums) == 2 and None not in nums:  # 月-日 → 今年
            month, day = nums
            return _compose_date(time.localtime().tm_year, month, day)
        if len(nums) == 3 and None not in nums:  # 年-月-日
            return _compose_date(nums[0], nums[1], nums[2])
    except (ValueError, OverflowError, OSError):
        pass
    return ""


def _compose_date(year: int, month: int, day: int) -> str:
    """把 年/月/日 整数拼成 YYYY-MM-DD；非法返回空串。"""

    return time.strftime(
        "%Y-%m-%d",
        time.localtime(time.mktime((int(year), int(month), int(day), 0, 0, 0, 0, 0, -1))),
    )


def parse_love_diary_request(text: str) -> str | None:
    """解析「爱妻日记」+日期的触发指令。

    返回 ``YYYY-MM-DD``（没写日期返回空串，表示今天）；不是「爱妻日记」
    指令时返回 ``None``。日期后面允许跟「的」「看看」这类口语词。
    """

    raw = str(text or "").strip()
    if not raw.startswith(LOVE_DIARY_TRIGGER):
        return None
    rest = raw[len(LOVE_DIARY_TRIGGER):].strip()
    rest = rest.strip(":：,，.。!！~～ \t的看看念念给我")
    if not rest:
        return ""
    return _normalize_day_text(rest) or None


def _parse_turn_index(raw: Any, total: int = 0) -> int | None:
    """把「13」「第十三」「第13条」「最后一条」「倒数第二条」解析成当日序号。

    ``total`` 是当天总条数，只有"最后一条/倒数第N条"需要它；拿不到时这两种返回 None。
    """

    text = str(raw or "").strip()
    if not text:
        return None
    if "倒数" in text:
        if total <= 0:
            return None
        match = _TURN_INDEX_RE.search(text)
        behind = _chinese_to_int(match.group(1)) if match else 1
        return max(1, total - (behind or 1) + 1)
    if "最后" in text:
        return total or None
    match = _TURN_INDEX_RE.search(text)
    if match is not None:
        return _chinese_to_int(match.group(1))
    return _chinese_to_int(text)


def _reference_day(text: str, end: int) -> str:
    """在「第N条」之前的那半句里找最近说到的日期，返回 YYYY-MM-DD。"""

    head = text[:end]
    candidates: list[tuple[int, str]] = []
    for pattern in (
        re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日号]?"),
        re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[号日]"),
        re.compile(r"(?:今天|今日|昨天|昨日|前天|前日)"),
    ):
        for match in pattern.finditer(head):
            candidates.append((match.end(), match.group(0)))
    if not candidates:
        return ""
    return _normalize_day_text(max(candidates, key=lambda item: item[0])[1])


def parse_turn_reference(text: str) -> tuple[str, str] | None:
    """解析「我曾经在10月6号第13条和你说过」这类引用。

    返回 ``(日期, 序号说法)``：日期没写时为空串（表示今天），序号说法交给
    ``_parse_turn_index`` 再解析（"最后一条"要知道当天总条数才能算）。
    不是引用时返回 ``None``——没日期、又没"说过/念/记得"这类词，就不算引用。
    """

    raw = str(text or "").strip()
    if not raw:
        return None
    index_match = _TURN_INDEX_RE.search(raw)
    last_match = _TURN_LAST_RE.search(raw)
    if index_match is not None and (last_match is None or index_match.start() <= last_match.start()):
        spec, begin = index_match.group(0), index_match.start()
    elif last_match is not None:
        spec, begin = last_match.group(0), last_match.start()
    else:
        return None
    day = _reference_day(raw, begin)
    if not day and not any(word in raw for word in REFERENCE_HINTS):
        return None
    return day, spec


def parse_memory_request(text: str) -> str | None:
    """「我说过油菜花吗」→ 关键词「油菜花」；不是模糊回忆时返回 ``None``。"""

    raw = str(text or "").strip()
    if len(raw) < 2 or len(raw) > 120:
        return None
    if parse_turn_reference(raw) is not None:  # 带序号的走精确分支
        return None
    if _MEMORY_CUE_RE.search(raw) is None:
        return None
    keyword = raw
    for word in sorted(_MEMORY_NOISE, key=len, reverse=True):
        keyword = keyword.replace(word, " ")
    # 剥完只剩"我 / 你"这类主语，不是关键词
    keyword = " ".join(
        part for part in " ".join(keyword.split()).split() if part not in _MEMORY_SUBJECTS
    ).strip()
    if len(keyword) < 2 or len(keyword) > 40:
        return None
    return keyword


def asks_recall(text: str) -> bool:
    """这句话是不是在"问"（而不是随口陈述）——只对问句回"没找到"。"""

    return bool(re.search(r"[吗么嘛?？]|记不记得|还记得", str(text or "")))


def diary_paragraphs(text: str) -> list[str]:
    """从每日日记里取**模型写的叙事段**；本地统计摘要返回空。"""

    if not text or DIARY_LOCAL_MARK in text or DIARY_LLM_MARK not in text:
        return []
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        seg = " ".join(block.split())
        if not seg or seg[0] in "#>|-" or seg.startswith("!"):
            continue
        if len(seg) < 20:  # 表格残留、短行都不算叙事段
            continue
        out.append(seg)
    return out


def memo_paragraphs(text: str) -> list[str]:
    """把备忘录切段：去前缀、去前置时间戳、丢掉完全重复的段。"""

    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        seg = MEMO_LINE_PREFIX.sub("", line.strip()).strip()
        for _ in range(3):  # 「2026年10月24日周六23:17-23:46，」这种叠了两层
            trimmed = MEMO_LEAD_TIME.sub("", seg).strip()
            if trimmed == seg:
                break
            seg = trimmed
        if len(seg) < 15 or seg.startswith("用户编辑后"):
            continue
        if seg in seen:
            continue
        seen.add(seg)
        out.append(seg)
    return out


def narration_score(text: str) -> int:
    """旁白味有多重：第三人称字样越少，越像她自己说的话。"""

    return sum(str(text or "").count(mark) for mark in _NARRATION_MARKS)


def clip_sentence(text: str, limit: int = HAPPENED_MAX_CHARS) -> str:
    """按句号截断到 ``limit`` 以内，不硬切半句。"""

    body = " ".join(str(text or "").split()).strip()
    if len(body) <= limit:
        return body
    head = body[:limit]
    cut = max(head.rfind("。"), head.rfind("；"), head.rfind("！"), head.rfind("？"))
    if cut >= limit // 3:
        return head[: cut + 1]
    return head.rstrip("，、,;；") + "……"


# 路由模块要用上面这些常量和文本解析函数，因此放在类定义前、常量都就绪之后导入，
# 避免包初始化时的循环导入。
from .routers import (  # noqa: E402
    CapsuleRouter,
    CatDiaryRouter,
    ChronicleRouter,
    ConfigRouter,
    ImagesRouter,
    ScreeningRouter,
    SearchRouter,
)


@neko_plugin
class NekoDailyChroniclePlugin(NekoPluginBase):
    """把每天的对话留成章节，并为每章写一篇日记。"""

    #: 按功能域拆分的入口路由；主进程也能据此静态扫描入口元信息。
    __routers__ = [
        ConfigRouter,
        ChronicleRouter,
        CatDiaryRouter,
        ImagesRouter,
        ScreeningRouter,
        SearchRouter,
        CapsuleRouter,
    ]

    # ── 构造 ────────────────────────────────────────────────────────

    def __init__(self, ctx):
        super().__init__(ctx)
        # 文件日志能大幅方便排障；不同 SDK 版本可能没有这个方法，因此不强制
        try:
            file_logger = self.enable_file_logging(log_level="INFO")
            if file_logger is not None:
                self.logger = file_logger
        except Exception:  # noqa: BLE001 - 没有文件日志也要能跑
            pass

        self._lock = threading.RLock()
        self.cfg = config_mod.ChronicleConfig()
        self.store: ChronicleStore | None = None
        self.memories: MemoryStore | None = None
        # 按天缓存解析过的轮次（键是当天 raw 文件的 mtime），
        # 让"模糊回忆"每次不必把 100 多天重新读一遍。
        self._turns_cache: OrderedDict[str, tuple[float, list[Turn]]] = OrderedDict()
        #: config.schema.json 的解析缓存（键是文件 mtime），避免每次读写配置都重读。
        self._schema_fields_cache: tuple[int, list[dict[str, Any]]] | None = None
        self.data_root: Path | None = None
        self.user_data_root: Path | None = None
        self.llm_endpoint: llm_mod.LlmEndpoint | None = None
        self.llm_reason: str = "no_config"
        self.llm_config_path: str = ""
        self.llm_core_config: dict[str, Any] = {}
        # 最近一次真实探测结果：None=还没探过；True/False=可用/不可用。
        # 与"是否配置了端点"分开——配置了端点≠模型真能调用（401 等）。
        self.llm_probed: bool | None = None
        self.llm_probe_reason: str = ""
        self.llm_probe_detail: str = ""
        self._ready = False
        self._buffer: list[Turn] = []
        self._last_live_refresh = 0.0
        self._panel_day = ""
        self._last_warn = 0.0
        self._startup_task: asyncio.Task | None = None
        # 可写静态目录（static/ 的副本）与其中的章节图片目录
        self._static_ui_dir: Path | None = None
        self._images_dir: Path | None = None
        # 图片数量按目录 mtime 缓存：面板/浏览页列全部日期时不必反复扫目录
        self._image_count_cache: OrderedDict[str, tuple[int, int]] = OrderedDict()
        # 时间胶囊到期提醒：串行化，避免启动检查与每小时定时器重复推送
        self._capsule_notify_lock = threading.Lock()
        # 放映厅当前打开的那一天。为空时聊天处理器立刻返回，不读任何放映文件。
        self._screening_active_day = ""
        # 拾光：默认不加载 Tk；只有执行 #拾光 且配置开启时才懒加载。
        self._catalog_window = None
        self._catalog_lock = threading.Lock()
        self._catalog_data_lock = threading.RLock()
        # 拾光开窗失败的聊天提示做节流，避免主人连点命令时刷屏。
        self._catalog_notice_at = 0.0
        # 补录路径每轮都会重读同一条命令，记住上次处理过的那条。
        self._catalog_last_command = ""
        # 补录路径的时间水位：只把比它新的轮次当作"刚发生"。
        self._harvest_seen_ts = time.time()
        # 宿主对话存储的上一轮文件签名：没变就不重复打开 SQLite 解析。
        self._host_store_signature: tuple[str, tuple[int, ...]] | None = None
        # 当天章节上次写盘时的轮次条数：没新增就不重复排版、重复落盘。
        self._chapter_revisions: dict[str, int] = {}
        # 拾光开启期间的命令轮询任务；关闭时为 None。
        self._catalog_watch_task: asyncio.Task | None = None

        # 入口按功能域注册到路由；同时把入口方法挂回插件实例，
        # 让 `@llm_tool` 与既有调用方仍能通过 `self.entry_xxx(...)` 访问。
        self._routers: list[Any] = []
        for router_cls in self.__routers__:
            router = router_cls()
            self.include_router(router)
            self._routers.append(router)
            for name in vars(type(router)):
                if name.startswith("entry_"):
                    setattr(self, name, getattr(router, name))

    # ── 生命周期 ────────────────────────────────────────────────────

    @lifecycle(id="startup")
    async def startup(self, **_):
        self.logger.info("[chronicle-boot] startup() 进入，开始读配置")
        raw_config: dict[str, Any] = {}
        try:
            dumped = await self.config.dump()
            if isinstance(dumped, dict):
                raw_config = dumped
        except Exception as exc:  # noqa: BLE001 - 配置读取失败就用默认值
            self.logger.warning("读取插件配置失败，使用默认值：{}", exc)

        self.cfg = config_mod.ChronicleConfig.from_mapping(raw_config)
        self.logger.info("[chronicle-boot] 配置已解析，开始建数据目录")
        for warning in self.cfg.warnings:
            self.logger.warning("配置项被忽略：{}", warning)

        self.data_root = self._resolve_data_root()
        self.user_data_root = self._resolve_user_data_root(self.data_root)
        self.store = ChronicleStore(self.data_root, self.cfg)
        self.store.ensure_dirs()
        self.memories = MemoryStore(self.store)
        # 浏览页要显示上传的章节图片：把 static/ 复制到可写目录并注册，
        # 图片存到该目录的 images/<日期>/ 下（宿主静态服务可直接访问）。
        self._register_writable_static_ui()
        self.logger.info("[chronicle-boot] 数据目录/store 就绪，开始 _refresh_llm")
        self._refresh_llm()
        self.logger.info("[chronicle-boot] _refresh_llm 返回，准备后台任务并 Ok")

        # 启动前收到的消息先补进磁盘
        if self._buffer:
            buffered = list(self._buffer)
            self._buffer.clear()
            added = self.store.append_turns(buffered)
            self.logger.info("启动前缓存的消息已写入：{}", added)

        self._ready = True
        self._sync_catalog_watch()
        self._panel_day = local_day_key()

        # 重活（结算历史天 / 补录总线 / 刷新当天章节，可能真调 LLM，单次最长 60s）
        # 放到后台任务里跑——它们不能阻塞宿主的启动握手（只有 10s），
        # 否则整插件会被判定启动超时、打不开。
        if self._startup_task is None or self._startup_task.done():
            self._startup_task = asyncio.create_task(self._startup_heavy())

        stats = self.store.stats()
        return Ok(
            {
                "status": "ready",
                "data_root": str(self.data_root),
                "days": stats.get("days", 0),
                "finalized": [],
                "llm": self._llm_status(),
            }
        )

    async def _startup_heavy(self) -> None:
        """后台启动初始化：结算历史天、补录总线、刷新当天章节。

        与 ``startup()`` 解耦：这里会真调 LLM（写日记），单次最长 ``llm_timeout_seconds``，
        放进后台才不会拖垮宿主的启动握手。任何异常都吞掉并记日志，不影响插件已就绪。
        """

        try:
            # 必须让宿主先走完 startup 之后的开机收尾（起定时器、登记就绪），
            # 再开始这边的重活：实测只要同一时刻就开始调模型，宿主收尾会被拖
            # 36 秒（模型请求占住了宿主也要用的线程池），直接触发启动超时。
            await asyncio.sleep(5)
            finalized: list[str] = []
            tidy = await self._auto_tidy_memo()
            if self.cfg.compile_on_startup:
                finalized = await self._finalize_pending(backfill_days=self.cfg.startup_backfill_days)
            harvested = await self._harvest_from_bus()
            # 开机时补一次到期胶囊提醒（错过 9 点、插件当时没运行的情况）
            await asyncio.to_thread(self._notify_due_capsules)
            await self._refresh_today_chapter(force=True)
            stats = self.store.stats()
            store_info = harvested.get("host_store") or {}
            self.logger.info(
                "对话编年史就绪：数据目录 {}，已有 {} 天，本次结算 {} 天，补录 {} 条，昨日备忘录整理 {}；对话存储 {}（{}）",
                self.data_root, stats.get("days"), len(finalized), harvested.get("added", 0),
                tidy.get("status") if isinstance(tidy, dict) else "-",
                store_info.get("used") or "-", store_info.get("source") or "-",
            )
        except Exception as exc:  # noqa: BLE001 - 后台失败不影响插件已就绪
            self.logger.warning("启动后台初始化失败：{}", exc)

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        """停机时把缓冲区落盘，并保存状态。"""

        with self._lock:
            try:
                if self.store is not None and self._buffer:
                    self.store.append_turns(list(self._buffer))
                    self._buffer.clear()
                if self.store is not None:
                    state = self.store.load_state()
                    state["last_shutdown"] = time.time()
                    self.store.save_state(state)
            except Exception as exc:  # noqa: BLE001 - 停机流程不抛异常
                self.logger.warning("停机落盘失败：{}", exc)
            self._ready = False
        task = getattr(self, "_catalog_watch_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._catalog_watch_task = None
        self._close_catalog_window()
        self.logger.info("对话编年史已停止")
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        """配置面板改动后立刻生效，不重启插件。

        框架并不会传入新旧配置，这里自行 dump 最新配置；
        读不到时保持现有快照，绝不用空映射回落成默认值。
        """

        try:
            dumped = await self.config.dump()
        except Exception as exc:  # noqa: BLE001 - 读不到配置就保持现有快照
            self.logger.warning("配置变更后读取配置失败：{}", exc)
            return Ok({"status": "config_unchanged"})
        if not isinstance(dumped, Mapping):
            return Ok({"status": "config_unchanged"})
        return self._apply_config(dumped)

    def _apply_config(self, mapping: Mapping[str, Any]) -> Ok:
        """按新的配置映射重建配置快照 / store / 模型端点（热生效）。"""

        was_catalog_on = bool(getattr(self.cfg, "catalog_enabled", False))
        self.cfg = config_mod.ChronicleConfig.from_mapping(mapping)
        for warning in self.cfg.warnings:
            self.logger.warning("配置项被忽略：{}", warning)
        if self.store is not None:
            # 目录名可能被改过，重建 store 并确保目录存在
            self.store = ChronicleStore(self.data_root, self.cfg)
            self.store.ensure_dirs()
            self.memories = MemoryStore(self.store)
        self._turns_cache.clear()  # 称呼/口径变了，缓存的轮次作废
        self._chapter_revisions.clear()  # 排版口径可能变了，允许重写当天章节
        if not self.cfg.screening_enabled:
            # 关闭放映厅后不再参与聊天自动暂停，也不留下活动日期。
            self._screening_active_day = ""
        if not self.cfg.catalog_enabled:
            # 拾光关闭时连窗口一起收起；之后不会加载 Tk。
            self._close_catalog_window()
        elif not was_catalog_on:
            # 在配置页把拾光打开并保存：不用再等命令，立刻把窗口弹出来。
            self._open_catalog_soon()
        # 开关一变就启停命令轮询：关掉后不留任何后台任务。
        self._sync_catalog_watch()
        self._refresh_llm()
        return Ok({"status": "config_updated", "llm": self._llm_status()})

    # ── 数据采集 ────────────────────────────────────────────────────

    @message(id="capture_chat", source="chat")
    def on_chat_message(self, **_kwargs):
        """实时接住聊天消息。

        ``@message`` 用同步函数：这里只做一次很小的文件追加，而宿主可能在不同
        线程分派消息。任何异常都不能冒泡出去影响宿主的对话流程。
        """

        try:
            turns = harvest.turns_from_message_kwargs(
                _kwargs,
                source="message.chat",
                default_speaker="",
            )
            if not turns:
                return Ok({"captured": 0})
            # 拾光送入聊天的引用/日期引子不再写回原始记录，避免同一段回忆重复入库。
            # 拾光关闭时连这次字符串检查都跳过。
            if self.cfg.catalog_enabled:
                turns = [
                    turn for turn in turns
                    if not catalog_mod.is_catalog_injection(turn.text)
                ]
                if not turns:
                    return Ok({"captured": 0})
            # 两条通道都要先解析：这时这一轮消息还没写进记录，否则「最后一条」
            # 会指到主人刚发的这句话本身。精确分支优先，其次模糊回忆。
            reference = self._handle_exact_reference(turns)
            memory = None if reference is not None else self._handle_fuzzy_memory(turns)
            added = self._ingest(turns)
            screening = self._maybe_pause_screening(turns)
            catalog = self._maybe_open_catalog(turns)
            love = self._handle_love_diary_trigger(turns)
            result: dict[str, Any] = {
                "captured": len(turns),
                "added": sum(added.values()),
            }
            if screening:
                result["screening_paused"] = screening
            if catalog:
                result["catalog"] = catalog
            if love is not None:
                result["love_diary"] = love
            if reference is not None:
                result["turn_reference"] = reference
            if memory is not None:
                result["memory_cue"] = memory
            return Ok(result)
        except Exception as exc:  # noqa: BLE001 - 采集失败不能影响对话
            self._warn_throttled("聊天消息采集失败：{}", exc)
            return Ok({"captured": 0, "error": "capture_failed"})

    def _handle_love_diary_trigger(self, turns: list[Turn]) -> dict | None:
        """主人发「爱妻日记+日期」时，把当天的每日日记递回给猫娘。

        每日日记不再自动往聊天里推送；只有这条独立触发通道会把它交给猫娘
        （``visibility=[]``：只进猫娘的上下文，不在聊天窗口里显示）。
        """

        if self.store is None:
            return None
        for turn in turns:
            if turn.role not in (ROLE_USER, ROLE_UNKNOWN):
                continue
            parsed = parse_love_diary_request(turn.text)
            if parsed is None:
                continue
            day = parsed or local_day_key()
            diary = self.store.read_diary(day)
            if diary:
                body = f"【每日日记】{day}\n{diary[:LOVE_DIARY_MAX_CHARS]}"
            else:
                body = f"【每日日记】{day} 还没有生成"
            self._push_to_catgirl(body)
            return {"day": day, "found": bool(diary)}
        return None

    def _handle_exact_reference(self, turns: list[Turn]) -> dict | None:
        """主人点名「10月6号第13条」时，只递"说话人 + 原文"，不带任何指令。

        ``visibility=[]``：只进猫娘的上下文，不在聊天窗口里显示。
        """

        if self.store is None:
            return None
        for turn in turns:
            if turn.role not in (ROLE_USER, ROLE_UNKNOWN):
                continue
            parsed = parse_turn_reference(turn.text)
            if parsed is None:
                continue
            day, spec = parsed
            day = day or local_day_key()
            rows = self._turns_for(day)
            wanted = _parse_turn_index(spec, len(rows)) or 0
            found = reader_mod.find_turn(rows, wanted) if wanted else None
            if found is None:
                self._push_to_catgirl("【引用原文】未找到该条记录")
                return {"day": day, "index": wanted, "found": False, "total": len(rows)}
            position, row = found
            speaker = row.display_speaker(
                master_name=self.cfg.master_name, catgirl_name=self.cfg.catgirl_name
            )
            text = reader_mod.clamp_text(row.text, reader_mod.SINGLE_TEXT_LIMIT)
            self._push_to_catgirl(
                "\n".join(("【引用原文】", f"说话人：{speaker}", f"原文：{text}"))
            )
            return {
                "day": day,
                "index": position,
                "found": True,
                "speaker": speaker,
                "role": row.role,
            }
        return None

    def _happened_note(self, day: str) -> str:
        """「发生了什么」：优先当天模型写的日记，其次当天备忘录，都没有返回空串。

        素材都不是插件现编的——日记是那天生成的、备忘录是宿主同步下来的。
        """

        if self.store is None:
            return ""
        try:
            narrative = diary_paragraphs(self.store.read_diary(day) or "")
        except Exception as exc:  # noqa: BLE001 - 素材取不到就当没有
            self._warn_throttled("读日记失败：{}", exc)
            narrative = []
        if narrative:
            return clip_sentence(random.choice(narrative))
        try:
            segments = memo_paragraphs(memo_mod.read(self.store, day))
        except Exception as exc:  # noqa: BLE001
            self._warn_throttled("读备忘录失败：{}", exc)
            segments = []
        if segments:
            # 备忘录是旁白体：从"最不像旁白"的几段里随机挑一段，别把 YUI/他/称 的腔调带进去
            pool = sorted(segments, key=narration_score)[:5]
            return clip_sentence(random.choice(pool))
        return ""

    def _handle_fuzzy_memory(self, turns: list[Turn]) -> dict | None:
        """主人只提一个关键词时，递一条【回忆线索】：时间、说话人、内容、发生了什么。"""

        if self.store is None:
            return None
        for turn in turns:
            if turn.role not in (ROLE_USER, ROLE_UNKNOWN):
                continue
            keyword = parse_memory_request(turn.text)
            if keyword is None:
                continue
            pairs = [(day, self._turns_for(day)) for day in self._recent_days(self.cfg.search_default_days)]
            hits = reader_mod.search_turns(
                pairs,
                keyword,
                limit=3,
                master_name=self.cfg.master_name,
                catgirl_name=self.cfg.catgirl_name,
                snippet_limit=self.cfg.search_snippet_limit,
                skip_meta=True,
            )
            if not hits:
                # 随口陈述（不是问句）没找到就闭嘴，别往她上下文里塞噪音
                if asks_recall(turn.text):
                    self._push_to_catgirl("【回忆线索】没有找到相关记录")
                    return {"keyword": keyword, "found": False, "notified": True}
                return None
            # "想起"是她自己说的话：优先取她那一条，别把主人当时的一句追问当回忆
            hit = next((item for item in hits if item.get("role") == "assistant"), hits[0])
            lines = [
                "【回忆线索】",
                f"时间：{hit['date']}",
                f"说话人：{hit['speaker']}",
                f"内容：{hit['snippet']}",
            ]
            happened = self._happened_note(hit["date"])
            if happened:
                lines.append(f"发生了什么：{happened}")
            self._push_to_catgirl("\n".join(lines))
            return {
                "keyword": keyword,
                "found": True,
                "day": hit["date"],
                "index": hit["index"],
                "with_happened": bool(happened),
            }
        return None

    def _maybe_pause_screening(self, turns: list[Turn]) -> dict | None:
        """猫娘一开口就暂停放映。

        这里刻意把最贵的判断放在最前面：功能关闭、没有打开的放映、或这一轮没有
        猫娘发言时直接返回，不读任何放映文件。只有她真的说话、且放映正在进行，
        才读写一个小 JSON。
        """

        if not self.cfg.screening_enabled or not self.cfg.screening_auto_pause_on_catgirl:
            return None
        day = str(self._screening_active_day or "")
        if not day or self.store is None:
            return None
        if not any(turn.role == "assistant" for turn in turns):
            return None
        try:
            raw = self.store.load_screening(day)
            if not raw or str(raw.get("status") or "") != screening_mod.STATUS_PLAYING:
                return None
            total = max(
                int(raw.get("total") or 0),
                int(raw.get("cursor") or 0),
                int(raw.get("cat_cursor") or 0),
            )
            session = screening_mod.normalize_session(raw, day, total)
            session = screening_mod.pause(
                session,
                total,
                who="catgirl",
                reason="catgirl_spoke",
                note="",
                add_mark=True,
            )
            self.store.save_screening(day, session)
            return {
                "day": day,
                "cursor": int(session.get("cursor") or 0),
                "reason": "catgirl_spoke",
            }
        except Exception as exc:  # noqa: BLE001 - 自动暂停失败不能影响聊天
            self._warn_throttled("放映厅自动暂停失败：{}", exc)
            return None

    def _push_to_catgirl(self, text: str, *, respond: bool = False) -> bool:
        """把一段内容只递给猫娘（不显示在聊天里）；返回是否成功提交。

        ``respond=True`` 时用 ``ai_behavior="respond"``：她会真的开口回应，
        用来「催」她做事（如补写胶囊）；默认 ``read`` 只让她读到、不打扰对话。
        """

        try:
            result = self.push_message(
                source=PLUGIN_ID,
                visibility=[],
                ai_behavior="respond" if respond else "read",
                parts=[{"type": "text", "text": text}],
                priority=4,
            )
            if isinstance(result, dict) and not result.get("submitted", False):
                self.logger.info("递送内容给猫娘失败：{}", result.get("reason"))
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - 递送失败不能影响对话
            self._warn_throttled("递送内容给猫娘失败：{}", exc)
            return False

    @timer_interval(id="harvest_tick", seconds=60, auto_start=True)
    async def harvest_tick(self, **_):
        """每分钟从总线补录一次，并定期刷新当天的章节文件。

        后台静默执行：补录结果只写进插件日志，不再通过宿主推"系统通知"让猫娘汇报。
        """

        result = await self._harvest_from_bus()
        try:
            await self._refresh_today_chapter()
        except Exception as exc:  # noqa: BLE001
            self._warn_throttled("刷新当天章节失败：{}", exc)
        # 每分钟一拍，只写 debug，避免长期运行时日志文件不断膨胀。
        self.logger.debug(
            "harvest_tick 静默完成：polled={}, added={}, days={}, notes={}",
            result.get("polled"), result.get("added"), result.get("days"), result.get("notes"),
        )
        return Ok(result)

    @timer_interval(id="finalize_tick", seconds=600, auto_start=True)
    async def finalize_tick(self, **_):
        """每天 FINALIZE_HOUR 点（默认 18:00）开始结算前一天的章节 + 每日日记。

        调度规则：
        - 18:00 第一次尝试；
        - 失败则每 10 分钟（FINALIZE_RETRY_SECONDS）重试一次；
        - 当天最多 FINALIZE_MAX_ATTEMPTS 次（含首次），全部失败则当天不再尝试；
        - 跨天自动重置计数；若 18:00 程序没开，开机后第一拍会立即补一次。

        全程静默：不向聊天推系统通知，结果只写插件日志。
        备忘录整理（_auto_tidy_memo）每次心跳都跑，它自身幂等。
        """

        # 备忘录整理幂等且轻量，每次心跳都执行（不论是否到 18:00）
        tidy = await self._auto_tidy_memo()

        if not self.cfg.finalize_enabled or self.store is None:
            return Ok({})

        now = time.time()
        today = local_day_key(now)
        state = self.store.load_state()
        sched = state.get("finalize_schedule") or {}

        # 跨天重置调度状态
        if sched.get("date") != today:
            sched = {
                "date": today,
                "attempts": 0,
                "last_attempt_ts": 0.0,
                "succeeded": False,
                "gave_up": False,
            }

        already_saved = False

        def _save() -> None:
            nonlocal already_saved
            state["finalize_schedule"] = sched
            self.store.save_state(state)
            already_saved = True

        # 今天已成功 → 直接返回
        if sched.get("succeeded"):
            if state.get("finalize_schedule") != sched:
                _save()
            return Ok({})

        # 今天已放弃（3 次都失败）→ 直接返回
        if sched.get("gave_up"):
            return Ok({})

        # 还没到 18:00 → 等到点
        if time.localtime(now).tm_hour < FINALIZE_HOUR:
            if state.get("finalize_schedule") != sched:
                _save()
            return Ok({})

        attempts = int(sched.get("attempts", 0))

        # 超过最大次数 → 标记当日放弃
        if attempts >= FINALIZE_MAX_ATTEMPTS:
            sched["gave_up"] = True
            _save()
            self.logger.warning(
                "finalize_tick：今天已尝试 {} 次均未成功，当日不再重试，等明天 {}:00",
                FINALIZE_MAX_ATTEMPTS, FINALIZE_HOUR,
            )
            return Ok({})

        # 距上次尝试不足重试间隔 → 等下一拍
        if attempts > 0:
            elapsed = now - float(sched.get("last_attempt_ts") or 0)
            if elapsed < FINALIZE_RETRY_SECONDS:
                return Ok({})

        # 真正执行一次结算
        sched["attempts"] = attempts + 1
        sched["last_attempt_ts"] = now
        _save()
        attempt_no = sched["attempts"]

        try:
            finalized = await self._finalize_pending()
            sched["succeeded"] = True
            _save()
            self.logger.info(
                "finalize_tick 第 {}/{} 次尝试成功（{}:{}）：finalized={}, memo_tidy={}",
                attempt_no, FINALIZE_MAX_ATTEMPTS,
                FINALIZE_HOUR, (attempt_no - 1) * (FINALIZE_RETRY_SECONDS // 60),
                finalized, tidy,
            )
        except Exception as exc:  # noqa: BLE001 - 失败只记日志，等下一拍重试
            self.logger.warning(
                "finalize_tick 第 {}/{} 次尝试失败：{}",
                attempt_no, FINALIZE_MAX_ATTEMPTS, exc,
            )
        return Ok({})

    @timer_interval(id="memo_tick", seconds=1800, auto_start=True)
    async def memo_tick(self, **_):
        """每 30 分钟抓一次宿主 recent.json 的 system 备忘录，只追加新增段。

        后台静默执行：抓取结果只写进插件日志。
        """

        if not self.cfg.memo_enabled:
            self.logger.debug("memo_tick：memo_enabled=false，跳过")
            return Ok({})
        memo_result = await self._fetch_memo_once()
        self.logger.debug("memo_tick 静默完成：{}", memo_result)
        return Ok({})

    async def _fetch_memo_once(self) -> dict:
        """读宿主 system 备忘录，把新增段追加进当天备忘录。"""

        if self.store is None or self.user_data_root is None:
            return {"skipped": True, "reason": "not_ready"}
        text = await asyncio.to_thread(
            host_store.read_system_memo,
            self.user_data_root,
            self.cfg.memory_store_character,
        )
        if not text:
            return {"skipped": True, "reason": "no_system_memo"}
        day = local_day_key()
        result = memo_mod.append_new(
            self.store, day, text, mode=self.cfg.memo_append_mode
        )
        return {"day": day, **result}

    @timer_interval(id="capsule_tick", seconds=3600, auto_start=True)
    async def capsule_tick(self, **_):
        """每小时检查一次到期胶囊；每枚只提醒一次，且默认在设定时刻之后。"""

        result = await asyncio.to_thread(self._notify_due_capsules)
        return Ok(result)

    def _notify_due_capsules(self, now: float | None = None) -> dict:
        """把今天已到期、还没提醒过的胶囊递给猫娘（不在聊天里显示）。

        每枚胶囊只提醒一次；提醒成功后把 id 记进 state.json，失败则下次重试。
        """

        if self.store is None:
            return {"notified": 0, "skipped": "not_ready"}
        if not self.cfg.capsule_notify_enabled:
            return {"notified": 0, "skipped": "disabled"}

        moment = float(now) if now is not None else time.time()
        if time.localtime(moment).tm_hour < int(self.cfg.capsule_notify_hour):
            return {"notified": 0, "skipped": "before_hour"}

        today = local_day_key(moment)
        with self._capsule_notify_lock:
            items = capsule_mod.list_all(
                self.store,
                today=today,
                include_text=True,
                include_locked=False,
                limit=200,
            )
            if not items:
                return {"notified": 0, "due": 0}

            with self.store._lock:
                notified = capsule_mod.notified_ids(self.store)
            fresh = [item for item in items if item["id"] not in notified]
            if not fresh:
                return {"notified": 0, "due": len(items)}

            batch = fresh[:5]
            lines = []
            for item in batch:
                excerpt = " ".join(str(item.get("text") or "").split())
                if len(excerpt) > 120:
                    excerpt = excerpt[:120] + "…"
                lines.append(
                    f"- {item['author_name']} 在 {item['created_day']} 封存，"
                    f"解锁日 {item['unlock_day']}：{excerpt}（id={item['id']}）"
                )
            message = (
                f"⏳ 时间胶囊到期了，共 {len(batch)} 枚：\n"
                + "\n".join(lines)
                + "\n想读完整内容就调用 chronicle_read_capsule（id 或 date）。"
            )
            if not self._push_to_catgirl(message):
                return {"notified": 0, "due": len(items)}

            merged = list(notified)
            for item in batch:
                if item["id"] not in merged:
                    merged.append(item["id"])
            capsule_mod.save_notified_ids(self.store, merged)
            self.logger.info(
                "时间胶囊到期提醒：本次 {} 枚，剩余 {} 枚待下次",
                len(batch), max(0, len(fresh) - len(batch)),
            )
            return {
                "notified": len(batch),
                "due": len(items),
                "remaining": max(0, len(fresh) - len(batch)),
            }

    def _config_schema_fields(self) -> list[dict[str, Any]]:
        """从 ``config.schema.json`` 抽出 ``[chronicle]`` 段的字段元信息。

        浏览页的「插件配置」弹窗直接照这份 schema 渲染表单，
        与宿主配置页共用同一份定义，避免两边字段漂移。
        """

        path = Path(__file__).resolve().parent / "config.schema.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = -1
        cached = self._schema_fields_cache
        if cached is not None and cached[0] == stamp:
            return [dict(item) for item in cached[1]]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # noqa: BLE001 - schema 读不到就退回空列表
            self.logger.warning("读取配置 schema 失败：{}", exc)
            return []
        properties = raw.get("properties") if isinstance(raw, dict) else None
        section = properties.get("chronicle") if isinstance(properties, dict) else None
        section_props = section.get("properties") if isinstance(section, dict) else None
        if not isinstance(section_props, dict):
            return []
        fields: list[dict[str, Any]] = []
        for key, spec in section_props.items():
            if not isinstance(spec, dict):
                continue
            item: dict[str, Any] = {
                "key": key,
                "type": spec.get("type") or "string",
                "title": spec.get("title") or key,
                "description": spec.get("description") or "",
            }
            for extra in ("enum", "minimum", "maximum", "default"):
                if extra in spec:
                    item[extra] = spec[extra]
            if spec.get("writeOnly"):
                item["write_only"] = True
            if item["type"] == "array":
                items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
                item["item_type"] = items.get("type") or "string"
            fields.append(item)
        self._schema_fields_cache = (stamp, fields)
        return [dict(item) for item in fields]


    async def _tidy_memo_content(
        self, day: str, memo_text: str
    ) -> tuple[str | None, str, int]:
        """调用模型整理备忘录正文。

        返回 ``(整理后的文本或 None, 错误码, 尝试次数)``；错误码为空表示成功。
        """

        prompt = (
            f"请整理下面这份 {day} 的备忘录。\n"
            "要求：\n"
            "1. 合并内容重复或语义相同的条目，只保留信息最完整的一条；\n"
            "2. 去掉口头禅、语气词和啰嗦重复的表达，让每句话通顺自然；\n"
            "3. 保留所有有效信息和事实，不要编造，也不要添加原文没有的内容；\n"
            "4. 用简洁的中文陈述句，按主题或时间顺序排列，每条单独一行并以「- 」开头；\n"
            "5. 只输出整理后的备忘录正文，不要输出解释、标题或总结。\n\n"
            f"原始备忘录：\n{memo_text}"
        )
        # 单次 35s，默认最多 3 次（加退避约 108s），仍小于入口的 120s 超时预算
        cleaned, reason, attempts_used = await llm_mod.achat_completion_with_retry(
            self.llm_endpoint,
            prompt,
            attempts=int(self.cfg.llm_retry_attempts or 3),
            timeout=min(float(self.cfg.llm_timeout_seconds), 35.0),
            temperature=0.3,
            logger=self.logger,
            retry_delay=1.0,
        )
        if not cleaned:
            return None, reason or "model_failed", attempts_used
        cleaned = cleaned.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        if not cleaned:
            return None, "empty", attempts_used
        if _text_overlap(memo_text, cleaned) < 0.5:
            self.logger.warning("整理结果与原备忘录差异过大，已放弃写回：{}", day)
            return None, "too_different", attempts_used
        return cleaned, "", attempts_used

    async def _auto_tidy_memo(self) -> dict:
        """过 0 点后自动整理「昨天」的备忘录；不向聊天发送任何提示。

        只处理昨天这一天，成功后写「已整理」标记，重复 tick 直接跳过。
        没有模型配置或整理失败时保持原样，不打断每日日记的生成。
        """

        if self.store is None or not self.cfg.memo_enabled:
            return {"status": "skipped", "reason": "memo_disabled"}
        day = local_day_key(time.time() - 86400)
        try:
            state = self.store.load_state()
            auto_days = list(state.get("auto_tidy_days") or [])
            if day in auto_days:
                return {"status": "skipped", "date": day, "reason": "already_auto_tidied"}
            memo_text = memo_mod.read(self.store, day).strip()
            if not memo_text:
                return {"status": "skipped", "date": day, "reason": "empty"}
            if memo_mod.read_tidy_mark(self.store, day):
                return {"status": "skipped", "date": day, "reason": "already_tidied"}
            self._refresh_llm()
            if self.llm_endpoint is None:
                return {"status": "skipped", "date": day, "reason": "no_llm"}
            cleaned, error, attempts_used = await self._tidy_memo_content(day, memo_text)
            if not cleaned:
                self.logger.warning("自动整理昨日备忘录失败：{}（{}）", day, error)
                return {
                    "status": "failed",
                    "date": day,
                    "reason": error,
                    "attempts": attempts_used,
                }
            memo_mod.write(self.store, day, cleaned)
            tidied_at = time.strftime("%Y-%m-%d %H:%M")
            memo_mod.write_tidy_mark(self.store, day, tidied_at)
            state["auto_tidy_days"] = (auto_days + [day])[-30:]
            self.store.save_state(state)
            self.logger.info(
                "昨日备忘录已自动整理：{}（{} → {} 字）", day, len(memo_text), len(cleaned)
            )
            return {
                "status": "tidied",
                "date": day,
                "before_chars": len(memo_text),
                "after_chars": len(cleaned),
            }
        except Exception as exc:  # noqa: BLE001 - 自动整理失败不影响日记结算
            self.logger.warning("自动整理昨日备忘录异常：{}", exc)
            return {"status": "failed", "date": day, "reason": "exception"}


    @staticmethod
    def _compress_to_jpeg_data_url(path: Path, max_side: int = 1920, quality: int = 85) -> str:
        """用 Pillow 把图片转成 JPEG data URL（最长边 max_side）；失败返回空串。"""

        try:
            from PIL import Image  # type: ignore
        except Exception:
            return ""
        try:
            with Image.open(path) as img:
                # EXIF 方向
                try:
                    from PIL import ImageOps  # type: ignore
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                # 缩放到最长边 max_side
                w, h = img.size
                scale = min(1.0, max_side / max(w, h))
                if scale < 1.0:
                    img = img.resize((int(w * scale), int(h * scale)))
                # RGBA → RGB 白底
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                raw = buf.getvalue()
        except Exception:
            return ""
        if not raw:
            return ""
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"


    def _cat_diary_overview(self, cap: int) -> dict:
        """猫娘日记总览的实际读取（在线程里跑，避免阻塞事件循环）。"""

        items: list[dict] = []
        hidden_private = 0
        visible_days = 0
        readable_private = bool(self.cfg.cat_diary_private_readable)
        if self.cfg.cat_diary_enabled:
            for day in reversed(self.store.list_cat_diary_days()):
                entries = self.store.cat_diary_entries(day)
                if not entries:
                    continue
                visible = self.store.visible_entries(
                    entries, include_private=readable_private
                )
                if not visible:
                    hidden_private += 1
                    continue
                visible_days += 1
                if len(items) >= cap:
                    # 已经够返回条数：后面只统计总数，不再渲染 Markdown / 拼条目
                    continue
                items.append(
                    {
                        "date": day,
                        "count": len(visible),
                        "chars": sum(len(entry.get("text") or "") for entry in visible),
                        "private": all(
                            self.store.is_private_entry(entry) for entry in visible
                        ),
                        "entries": visible,
                        "markdown": self.store.cat_diary_markdown(
                            day, include_private=readable_private
                        ),
                    }
                )
        return {
            "count": visible_days,
            "truncated": visible_days > len(items),
            "hidden_private": hidden_private,
            "items": items,
        }


    def _search_chat_payload(
        self, text: str, size: int, day_span: int, per_day_cap: int, skip_meta: bool
    ) -> dict:
        """对话检索的实际读取（在线程里跑，避免阻塞事件循环）。"""

        # 用生成器：猫娘只召回几条时（per_day_cap>0）能提前停止，不必解析全部历史；
        # 浏览页（per_day_cap==0）需要扫完整个窗口，balance_days 会让它在旧日期之间轮转。
        pairs = ((day, self._turns_for(day)) for day in self._recent_days(day_span))
        results = reader_mod.search_turns(
            pairs,
            text,
            limit=size,
            master_name=self.cfg.master_name,
            catgirl_name=self.cfg.catgirl_name,
            snippet_limit=self.cfg.search_snippet_limit,
            max_per_day=per_day_cap,
            skip_meta=skip_meta,
            balance_days=per_day_cap <= 0,
        )
        return {
            "query": text,
            "count": len(results),
            "days_searched": day_span,
            "impressions": reader_mod.day_impressions(results),
            "results": results,
        }

    # ── 拾光 · 对话目录悬浮窗 ────────────────────────────────────────

    def _catalog_open(self, initial_query: str = "") -> tuple[bool, str]:
        """按需启动 Tk 悬浮窗；未开启配置时不会导入 tkinter。"""

        if not self.cfg.catalog_enabled:
            return False, "catalog_disabled"
        started = time.time()
        try:
            from .chronicle.catalog_window import CatalogWindow
        except Exception as exc:  # noqa: BLE001 - 宿主没有 Tk 运行时
            self._warn_throttled("拾光无法加载 Tk：{}", exc)
            return False, f"tk_import_failed: {exc}"
        elapsed = time.time() - started
        if elapsed > 1.0:
            # 冻结宿主里首次加载 tkinter 可能很慢，超时排查时先看这一行。
            self.logger.info("拾光模块加载耗时 {:.2f}s", elapsed)
        with self._catalog_lock:
            window = self._catalog_window
            if window is None or not window.is_alive():
                window = CatalogWindow(self)
                self._catalog_window = window
                if not window.open(initial_query):
                    reason = window.last_error or "tk_unavailable"
                    self.logger.warning("拾光窗口启动失败：{}", reason)
                    self._catalog_window = None
                    return False, reason
            else:
                window.focus(initial_query)
        return True, ""

    def _close_catalog_window(self) -> None:
        with self._catalog_lock:
            window = self._catalog_window
            self._catalog_window = None
        if window is not None:
            try:
                window.close(timeout=1.5)
            except Exception as exc:  # noqa: BLE001 - 关闭失败不影响插件
                self._warn_throttled("关闭拾光窗口失败：{}", exc)

    def _catalog_command_hit(self, turns: list[Turn]) -> tuple[str, str] | None:
        """找本批轮次里的 ``#拾光``，返回 ``(去重键, 搜索词)``。"""

        command = self.cfg.catalog_command or "#拾光"
        for turn in turns:
            if turn.role != ROLE_USER:
                continue
            rest = catalog_mod.parse_command(turn.text, command)
            if rest is not None:
                return f"{getattr(turn, 'ts', '')}|{rest}", rest
        return None

    def _catalog_command_query(self, turns: list[Turn]) -> str | None:
        """从本轮消息里找 ``#拾光``；不匹配返回 None（含空搜索词的命令）。"""

        hit = self._catalog_command_hit(turns)
        return hit[1] if hit else None

    def _catalog_notice(self, text: str) -> None:
        """给主人一句不打断的提示：只在 ``#拾光`` 没能打开窗口时用。"""

        now = time.time()
        if now - self._catalog_notice_at < 30:
            return
        self._catalog_notice_at = now
        try:
            self.push_message(
                source=PLUGIN_ID,
                visibility=["chat"],
                ai_behavior="read",
                parts=[{"type": "text", "text": text}],
                priority=4,
                metadata={"context_type": "conversation_catalog_notice"},
            )
        except Exception as exc:  # noqa: BLE001 - 提示失败不影响对话
            self._warn_throttled("拾光提示发送失败：{}", exc)

    def _open_catalog_async(self, query: str) -> None:
        """后台打开窗口；失败时把原因递回聊天，避免主人以为没反应。"""

        opened, reason = self._catalog_open(query)
        if opened:
            return
        detail = str(reason or "unknown")[:160]
        self._catalog_notice(f"拾光窗口没能打开（{detail}）。")

    def _open_catalog_soon(self) -> None:
        """配置页刚把拾光打开时，后台拉起窗口，不阻塞保存请求。

        没有事件循环（单元测试、或同步调用）时不开窗，保持可测且不弹窗。
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        def work() -> None:
            opened, reason = self._catalog_open("")
            if not opened:
                self._warn_throttled("拾光窗口自动启动失败：{}", reason)

        threading.Thread(
            target=work, daemon=True, name="neko-catalog-autostart"
        ).start()

    async def _catalog_watch_loop(self) -> None:
        """配置开启时用轻量轮询接住 ``#拾光``。

        部分宿主不会把用户输入派发到 ``@message(source="chat")``，补录又要等 60 秒；
        这里只在拾光开启时读几条总线条目，命中命令就开窗。关掉配置后任务被取消。
        """

        while True:
            try:
                await asyncio.sleep(CATALOG_WATCH_SECONDS)
                if not self.cfg.catalog_enabled:
                    continue
                bus = getattr(self.ctx, "bus", None)
                memory = getattr(bus, "memory", None) if bus is not None else None
                getter = getattr(memory, "get", None)
                if not callable(getter):
                    continue
                records, error = await self._read_bus(
                    getter,
                    [
                        {"bucket_id": "default", "limit": 8, "timeout": 0.5},
                        {"bucket_id": "default", "limit": 8},
                        {},
                    ],
                )
                if error or not records:
                    continue
                turns = harvest.turns_from_records(records, source="bus.memory")
                turns = harvest.resolve_roles(
                    turns,
                    master_name=self.cfg.master_name,
                    catgirl_name=self.cfg.catgirl_name,
                )
                self._maybe_open_catalog(turns)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - 监听失败不能拖垮插件
                self._warn_throttled("拾光命令监听失败：{}", exc)
                await asyncio.sleep(10)

    def _sync_catalog_watch(self) -> None:
        """按配置开关启停命令轮询；关闭时不保留任何后台任务。"""

        enabled = bool(self.cfg.catalog_enabled)
        task = getattr(self, "_catalog_watch_task", None)
        running = task is not None and not task.done()
        if enabled and not running:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # 没有运行中的事件循环（同步调用或测试）
                self._catalog_watch_task = None
                return
            self._catalog_watch_task = loop.create_task(self._catalog_watch_loop())
        elif not enabled and running:
            task.cancel()
            self._catalog_watch_task = None

    def _maybe_open_catalog(self, turns: list[Turn]) -> dict | None:
        hit = self._catalog_command_hit(turns)
        if hit is None:
            return None
        key, query = hit
        # 补录每轮都会把同一条命令再读一遍，用去重键避免反复开窗/刷提示。
        if key == self._catalog_last_command:
            return None
        self._catalog_last_command = key
        if not self.cfg.catalog_enabled:
            self._catalog_notice(
                "拾光还没开启：在插件设置里打开「拾光 · 对话目录」，再输入 #拾光。"
            )
            return None
        # 不在聊天消息回调里等 Tk 初始化；开窗慢也不拖住宿主对话。
        threading.Thread(
            target=self._open_catalog_async,
            args=(query,),
            daemon=True,
            name="neko-catalog-open",
        ).start()
        return {"requested": True, "query": query}

    def catalog_days(self, limit: int = 40) -> list[dict]:
        """拾光日期目录：最近若干天（在 Tk 的搜索线程里调用）。"""

        if self.store is None:
            return []
        limit = max(1, min(int(limit or 40), 120))
        with self._catalog_data_lock:
            available = self.store.list_raw_days()
            days = list(reversed(available[-limit:]))
            rows: list[dict] = []
            for day in days:
                turns = self._turns_for(day)
                if not turns:
                    continue
                rows.append(
                    {
                        "date": day,
                        "turns": len(turns),
                        "first_time": turns[0].clock,
                        "last_time": turns[-1].clock,
                        "first_text": " ".join(turns[0].text.split())[:80],
                    }
                )
            return rows

    def catalog_search(
        self,
        query: str,
        limit: int = 40,
        *,
        day: str = "",
        speaker: str = "",
    ) -> list[dict]:
        """跨日期搜索对话原文，返回带完整正文的命中。

        ``day`` 只搜一天；``speaker`` 为 ``user`` / ``assistant`` 时按说话人过滤。
        为了低压力，默认只回看 ``catalog_max_scan_days`` 天。
        """

        if self.store is None:
            return []
        needle = str(query or "").strip()
        if not needle:
            return []
        limit = max(1, min(int(limit or 40), 100))
        day_filter = str(day or "").strip()
        speaker_filter = str(speaker or "").strip().lower()
        if speaker_filter not in ("user", "assistant"):
            speaker_filter = ""
        with self._catalog_data_lock:
            available = self.store.list_raw_days()
            if day_filter:
                if day_filter not in available:
                    return []
                days = [day_filter]
            else:
                max_days = int(self.cfg.catalog_max_scan_days or 0)
                days = list(reversed(available[-max_days:])) if max_days > 0 else list(reversed(available))
            fetch_limit = min(200, limit * 4) if speaker_filter else limit
            # 用生成器让 search_turns 凑够候选就停止，避免每次搜索都解析全部历史。
            pairs = ((item, self._turns_for(item)) for item in days)
            hits = reader_mod.search_turns(
                pairs,
                needle,
                limit=fetch_limit,
                master_name=self.cfg.master_name,
                catgirl_name=self.cfg.catgirl_name,
                snippet_limit=self.cfg.search_snippet_limit,
                max_per_day=0,
                skip_meta=False,
            )
            enriched: list[dict] = []
            for hit in hits:
                hit_day = str(hit.get("date") or "")
                try:
                    index = int(hit.get("index") or 0) - 1
                except (TypeError, ValueError):
                    index = -1
                turns = self._turns_for(hit_day) if hit_day else []
                if not (0 <= index < len(turns)):
                    continue
                turn = turns[index]
                if speaker_filter and turn.role != speaker_filter:
                    continue
                hit["text"] = turn.text
                hit["time"] = turn.clock
                hit["role"] = turn.role
                hit["speaker"] = turn.display_speaker(
                    master_name=self.cfg.master_name,
                    catgirl_name=self.cfg.catgirl_name,
                )
                enriched.append(hit)
                if len(enriched) >= limit:
                    break
            return enriched

    def catalog_scene(self, day: str, index: int, *, pad: int = 3) -> list[dict]:
        """选中某条所在的场景（时间间隔切分），供“选中整段”使用。"""

        if self.store is None:
            return []
        with self._catalog_data_lock:
            turns = self._turns_for(day)
            if not turns:
                return []
            total = len(turns)
            try:
                target = max(1, min(int(index or 1), total))
            except (TypeError, ValueError):
                target = 1
            gap = float(self.cfg.scene_gap_minutes or 0) * 60.0
            scenes = screening_mod.build_scenes(turns, gap)
            start = max(1, target - pad)
            end = min(total, target + pad)
            for scene in scenes:
                if int(scene.get("start_index") or 0) <= target <= int(scene.get("end_index") or 0):
                    start = max(1, int(scene.get("start_index") or 1))
                    end = min(total, int(scene.get("end_index") or total))
                    break
            if end - start > 300:
                end = start + 300
            rows: list[dict] = []
            for number, turn in numbered_turns(turns):
                if start <= number <= end:
                    rows.append(
                        {
                            "date": day,
                            "index": number,
                            "time": turn.clock,
                            "role": turn.role,
                            "speaker": turn.display_speaker(
                                master_name=self.cfg.master_name,
                                catgirl_name=self.cfg.catgirl_name,
                            ),
                            "text": turn.text,
                        }
                    )
            return rows

    def catalog_day(self, day: str) -> list[dict]:
        """某一天的全部对话行，供拾光日期目录下钻。"""

        if self.store is None:
            return []
        with self._catalog_data_lock:
            turns = self._turns_for(day)
            rows = [
                {
                    "date": day,
                    "index": index,
                    "time": turn.clock,
                    "role": turn.role,
                    "speaker": turn.display_speaker(
                        master_name=self.cfg.master_name,
                        catgirl_name=self.cfg.catgirl_name,
                    ),
                    "text": turn.text,
                }
                for index, turn in numbered_turns(turns)
            ]
            return rows[:800]

    def catalog_send_text(self, text: str, *, respond: bool = False) -> dict:
        """把拾光选中的内容送进宿主聊天（默认不打断猫娘）。"""

        if not self.cfg.catalog_enabled:
            return {"ok": False, "message": "拾光未开启"}
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "message": "没有可送入的内容"}
        try:
            receipt = self.push_message(
                source=PLUGIN_ID,
                visibility=["chat"],
                ai_behavior="respond" if respond else "read",
                parts=[{"type": "text", "text": body}],
                priority=4,
                metadata={"context_type": "conversation_catalog_quote"},
            )
            if isinstance(receipt, dict) and not receipt.get("submitted", False):
                reason = str(receipt.get("reason") or "delivery_rejected")
                return {"ok": False, "message": f"送入失败：{reason}"}
            try:
                self._catalog_append_history(body, respond)
            except Exception as exc:  # noqa: BLE001 - 历史失败不影响送入
                self._warn_throttled("拾光保存送入历史失败：{}", exc)
            return {
                "ok": True,
                "message": "已送入对话，等她回应" if respond else "已送入对话（不会打断她）",
            }
        except Exception as exc:  # noqa: BLE001 - 送入失败只反馈到窗口
            self._warn_throttled("拾光送入对话失败：{}", exc)
            return {"ok": False, "message": f"送入失败：{exc}"}

    def _catalog_history_path(self) -> Any:
        return self.store.root / "catalog_history.json" if self.store is not None else None

    def _catalog_append_history(self, text: str, respond: bool) -> None:
        path = self._catalog_history_path()
        if path is None:
            return
        with self._catalog_data_lock:
            try:
                items = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    items = []
            except Exception:  # noqa: BLE001
                items = []
            items.append(
                {
                    "at": round(time.time(), 3),
                    "text": str(text or "")[:3000],
                    "respond": bool(respond),
                }
            )
            cap = max(5, int(self.cfg.catalog_history_limit or 20))
            items = items[-cap:]
            try:
                path.write_text(
                    json.dumps(items, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                pass

    def catalog_get_history(self, limit: int = 20) -> list[dict]:
        path = self._catalog_history_path()
        if path is None:
            return []
        with self._catalog_data_lock:
            try:
                items = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                items = []
            if not isinstance(items, list):
                return []
            return [item for item in items if isinstance(item, dict)][-max(1, int(limit or 20)):]


    # ── LLM 工具（让猫娘自己也能翻日记） ────────────────────────────

    @llm_tool(
        name="chronicle_read_diary",
        description=(
            "读取猫娘自己的日记——对话里主人提到「日记」「你的日记」「写日记」时，"
            "全部指这一篇，读它就用本工具。这是她自己能完整读写的日记，"
            "与自动生成的每日日记分开存放。"
            "只有主人明确发送「爱妻日记」+日期时，才改用 chronicle_love_diary 去读每日日记。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期，格式 YYYY-MM-DD 或自然中文（10月7号/昨天）；留空表示今天。",
                }
            },
        },
    )
    async def tool_read_diary(self, *, date: str = "") -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        day = self._normalize_day(date) or local_day_key()
        text = self.store.read_cat_diary(day)
        if not text:
            return {
                "output": {"date": day, "reason": "这一天还没有猫娘的日记"},
                "is_error": True,
                "error": "NO_DIARY",
            }
        return {"date": day, "kind": "cat_diary", "content": text[:4000]}

    @llm_tool(
        name="chronicle_search_diary",
        description=(
            "在自己写过的日记里按关键词检索（正文、标签、心情都会匹配）。"
            "主人问「我以前是不是写过……」「你日记里有没有提过……」时用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "要查找的关键词。"},
                "limit": {"type": "integer", "description": "最多返回几条，默认 10。"},
            },
            "required": ["keyword"],
        },
    )
    async def tool_search_diary(self, *, keyword: str = "", limit: int = 10) -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        query = str(keyword or "").strip()
        if not query:
            return {"output": {"reason": "keyword 不能为空"}, "is_error": True, "error": "EMPTY_KEYWORD"}
        hits = await asyncio.to_thread(self.store.search_cat_diary, query, limit=limit)
        if not self.cfg.cat_diary_private_readable:
            hits = self.store.visible_entries(hits, include_private=False)
        return {"keyword": query, "count": len(hits), "results": hits}

    @llm_tool(
        name="chronicle_diary_stats",
        description="统计自己最近一段时间日记的心情分布、常用标签与写作天数，用来回顾最近的心情。",
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "统计最近多少天，默认 30。"}
            },
        },
    )
    async def tool_diary_stats(self, *, days: int = 30) -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        return self.store.cat_diary_stats(days=days)

    @llm_tool(
        name="chronicle_diary_throwback",
        description="往年今日：找出自己去年或更早同月同日写的日记，用来和主人一起回忆。",
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期 YYYY-MM-DD，留空为今天。"},
                "years": {"type": "integer", "description": "最多回看多少年，0 表示不限。"},
            },
        },
    )
    async def tool_diary_throwback(self, *, date: str = "", years: int = 0) -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        day = self._normalize_day(date) or local_day_key()
        items = await asyncio.to_thread(self.store.cat_diary_throwback, day=day, years=years)
        if not self.cfg.cat_diary_private_readable:
            for item in items:
                item["entries"] = self.store.visible_entries(
                    item.get("entries") or [], include_private=False
                )
            items = [item for item in items if item["entries"]]
        return {"date": day, "count": len(items), "items": items}

    @llm_tool(
        name="chronicle_love_diary",
        description=(
            "「爱妻日记」专用触发通道：只有当主人明确发送「爱妻日记」并跟日期"
            "（例如「爱妻日记 2026-10-22」「爱妻日记昨天」「爱妻日记 10月7号」）时才调用。"
            "返回那一天自动生成的每日日记正文。平时主人说「日记」不要用这个工具，"
            "那指的是猫娘自己的日记（用 chronicle_read_diary）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "主人指定的日期，支持 YYYY-MM-DD 或自然中文；留空表示今天。",
                }
            },
        },
    )
    async def tool_love_diary(self, *, date: str = "") -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        day = self._normalize_day(date) or local_day_key()
        text = self.store.read_diary(day)
        if not text:
            return {
                "output": {"date": day, "reason": "这一天的每日日记还没有生成"},
                "is_error": True,
                "error": "NO_DIARY",
            }
        return {
            "date": day,
            "kind": "love_diary",
            "content": text[:LOVE_DIARY_MAX_CHARS],
        }

    @llm_tool(
        name="chronicle_list_days",
        description="列出已经留下对话记录的日期，用于挑选要回忆哪一天。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回多少天，默认 10。"}
            },
        },
    )
    async def tool_list_days(self, *, limit: int = 10) -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        return {"days": await asyncio.to_thread(self._day_cards, limit=int(limit or 10))}

    @llm_tool(
        name="chronicle_recent",
        description=(
            "查看最近的对话记录（带当天序号与时间）。主人问「刚才」「最近聊了什么」「我们刚才说到哪」"
            "时直接调用本工具——这是对话记录的权威来源，不要先查你自己的记忆或宿主数据库，"
            "查不到是正常的，直接查这里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "要几条，默认 10。"},
                "date": {"type": "string", "description": "日期：YYYY-MM-DD 或自然中文（今天/昨天/10月7号），留空为今天。"},
            },
        },
    )
    async def tool_recent(self, *, count: int = 10, date: str = "") -> dict:
        value, error = self._value_of(await self.entry_recent(count=count, date=date))
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "READ_FAILED"}
        return value

    @llm_tool(
        name="chronicle_get_turn",
        description=(
            "按序号取某一条对话的原文——这是对话记录的权威来源。"
            "只要主人提到日期+序号（如「把 2026-10-14 的第 114 条念给我听」「10月7号第13条」「第 12 条」）"
            "或想让你念某句原话，就**直接调用本工具去查**，不要先去翻你自己的记忆或宿主数据库："
            "你自己的记忆/宿主数据库里没有完整的按序号对话，查不到是正常的，那不代表没有——查这里才有。"
            "index 支持阿拉伯或中文数字（114 / 第十三）；date 支持 YYYY-MM-DD 或自然中文"
            "（今天/昨天/10月7号/七月七日），留空为今天。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "index": {"type": "string", "description": "当天第几条（阿拉伯或中文数字，如 114 / 第十三）。"},
                "date": {"type": "string", "description": "日期：YYYY-MM-DD 或自然中文（今天/昨天/10月7号/七月七日），留空为今天。"},
            },
            "required": ["index"],
        },
    )
    async def tool_get_turn(self, *, index, date: str = "") -> dict:
        value, error = self._value_of(await self.entry_get_turn(index=index, date=date))
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "NO_SUCH_TURN"}
        return value

    @llm_tool(
        name="chronicle_read_day",
        description=(
            "读取某一天的对话记录（带序号，可分页）。需要回看某天整天内容、或主人说「把今天的对话读一遍」时"
            "直接调用本工具——这是对话记录的权威来源，不要先查你自己的记忆或宿主数据库。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期：YYYY-MM-DD 或自然中文（今天/昨天/10月7号），留空为今天。"},
                "start": {"type": "integer", "description": "从第几条开始，默认 1。"},
                "count": {"type": "integer", "description": "要几条，默认 50，最多 200。"},
            },
        },
    )
    async def tool_read_day(self, *, date: str = "", start: int = 1, count: int = 50) -> dict:
        value, error = self._value_of(
            await self.entry_read_day(date=date, start=start, count=count)
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "READ_FAILED"}
        return value

    @llm_tool(
        name="chronicle_search_chat",
        description=(
            "在对话原文里搜索关键词，返回命中的日期、序号与片段——这是查「我说过/提过…」的权威来源。"
            "当主人问「我说过…吗」「我是不是提过…」「之前有没有聊过…」这类话时，直接调用本工具去搜，"
            "不要先凭你自己的记忆回答；你的记忆/宿主数据库里可能没有完整原文，查这里才有。"
            "days / limit 缺省使用插件配置里的检索默认值（可配置 search_default_days 等）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的文字。"},
                "limit": {"type": "integer", "description": "最多返回几条，缺省用配置。"},
                "days": {"type": "integer", "description": "往回查几天，缺省用配置。"},
            },
            "required": ["query"],
        },
    )
    async def tool_search_chat(self, *, query: str, limit: int = 0, days: int = 0) -> dict:
        value, error = self._value_of(
            await self.entry_search_chat(
                query=query, limit=limit, days=days, per_day=2, skip_meta=True
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SEARCH_FAILED"}
        return value

    @llm_tool(
        name="chronicle_note",
        description="把一件值得记住的事补记进当天的对话记录里，之后会出现在当天的章节中。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记下的内容。"},
                "speaker": {"type": "string", "description": "说话人显示名，可省略。"},
            },
            "required": ["text"],
        },
    )
    async def tool_note(self, *, text: str = "", speaker: str = "") -> dict:
        content = str(text or "").strip()
        if not content:
            return {"output": {"reason": "text 不能为空"}, "is_error": True, "error": "EMPTY_TEXT"}
        turn = Turn(
            ts=time.time(),
            text=content,
            role="assistant",
            speaker=str(speaker or ""),
            source="llm_tool",
        )
        self._ingest([turn])
        return {"day": turn.day, "saved": True}

    @llm_tool(
        name="chronicle_cat_write_diary",
        description=(
            "写猫娘自己的日记——对话里主人说「日记」「记日记」「写进日记」时，"
            "全部指这一篇。以猫娘第一人称、用你软萌的口吻（自然地用「喵～」「的说」「哦～」"
            "这类语气词，写给主人看）写；与自动生成的每日日记分开存放。"
            "默认 mode=append 追加到当天那篇的末尾；mode=replace 则整篇重写当天的日记。"
            "可以顺便标上心情、标签；想记点只属于自己的悄悄话，就把 visibility 设为 private"
            "（记为「仅自己可见」——放心，你自己依旧读得到）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "你想记下的日记内容，以第一人称、猫娘口吻写（可带「喵～」等语气词）。"},
                "date": {"type": "string", "description": "日期 YYYY-MM-DD，留空为今天。"},
                "mood": {"type": "string", "description": "这篇的心情：excited/happy/calm/tired/anxious/sad/angry/neutral（或对应中文），留空用默认心情。"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "标签（可选，最多 6 个）。"},
                "visibility": {"type": "string", "enum": ["public", "private"], "description": "可见性：public=公开，private=仅自己可见（私密内容你自己仍读得到），留空用默认可见性。"},
                "mode": {"type": "string", "enum": ["append", "replace"], "description": "append=追加到当天末尾（默认）；replace=整篇重写当天的日记。"},
            },
            "required": ["text"],
        },
    )
    async def tool_cat_write_diary(
        self,
        *,
        text: str = "",
        date: str = "",
        mood: str = "",
        tags: Any = None,
        visibility: str = "",
        mode: str = "append",
    ) -> dict:
        value, error = self._value_of(
            await self.entry_cat_write_diary(
                date=date, text=text, mood=mood, tags=tags, visibility=visibility, mode=mode
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "WRITE_FAILED"}
        return value

    @llm_tool(
        name="chronicle_write_capsule",
        description=(
            "写一封时间胶囊——以猫娘自己的口吻，写给未来某一天的自己或主人。"
            "主人说「给未来的我们写一封信」「封存一句话到明年」时调用。"
            "unlock_date 必须是今天或未来（支持 YYYY-MM-DD 或今天/明天/后天）。"
            "封存后到期前谁也读不到正文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "胶囊正文，以猫娘第一人称写。"},
                "unlock_date": {
                    "type": "string",
                    "description": "解锁日期，格式 YYYY-MM-DD 或今天/明天/后天。",
                },
            },
            "required": ["text", "unlock_date"],
        },
    )
    async def tool_write_capsule(self, *, text: str = "", unlock_date: str = "") -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        day = self._normalize_day(unlock_date)
        if not day:
            return {
                "output": {"reason": "解锁日期格式应为 YYYY-MM-DD，或今天/明天/后天"},
                "is_error": True,
                "error": "BAD_DATE",
            }
        record, error = capsule_mod.write(
            self.store, text=text, unlock_day=day, author="catgirl"
        )
        if record is None:
            return {
                "output": {"reason": error},
                "is_error": True,
                "error": error.upper(),
            }
        return record

    @llm_tool(
        name="chronicle_list_capsules",
        description=(
            "列出时间胶囊。每枚都带 author / author_name：master 是主人自己封存的，"
            "catgirl 才是猫娘封存的。未到期的只返回作者、解锁日期和字数，正文不会返回。"
            "主人问「你写了吗」「你的胶囊在哪」时，只能看 catgirl_count 或 author=catgirl 的条目，"
            "不要把 master 的胶囊当成自己写的；主人问「我们封存过什么」时才看全部。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回多少枚，默认 20。"},
                "include_locked": {"type": "boolean", "description": "是否包含未到期的胶囊，默认 true。"},
                "author": {
                    "type": "string",
                    "description": "只看某个作者：master（主人）或 catgirl（猫娘）；留空看全部。",
                },
            },
        },
    )
    async def tool_list_capsules(
        self, *, limit: int = 20, include_locked: bool = True, author: str = ""
    ) -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        today = local_day_key()
        items = capsule_mod.list_all(
            self.store,
            today=today,
            include_text=True,
            include_locked=bool(include_locked),
            limit=int(limit or 0),
        )
        who = str(author or "").strip()
        if who:
            if who not in capsule_mod.CAPSULE_AUTHORS:
                return {
                    "output": {"reason": "author 只能是 master 或 catgirl"},
                    "is_error": True,
                    "error": "BAD_AUTHOR",
                }
            items = [item for item in items if item["author"] == who]
        counts = capsule_mod.author_counts(items)
        return {
            "today": today,
            "count": len(items),
            "master_count": counts["master"],
            "catgirl_count": counts["catgirl"],
            "due_today": sum(
                1 for item in items if item["unlock_day"] == today and not item["locked"]
            ),
            "items": items,
        }

    @llm_tool(
        name="chronicle_read_capsule",
        description=(
            "打开一枚时间胶囊。只有到期后才能读到正文；未到期会返回还要等多少天。"
            "可以按 id 读一枚，也可以按 date 读那天到期的全部胶囊。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "胶囊 id（从 chronicle_list_capsules 取）。"},
                "date": {
                    "type": "string",
                    "description": "解锁日期，格式 YYYY-MM-DD 或今天/明天/后天。",
                },
            },
        },
    )
    async def tool_read_capsule(self, *, id: str = "", date: str = "") -> dict:
        if self.store is None:
            return {"output": {"reason": "插件尚未初始化完成"}, "is_error": True, "error": "NOT_READY"}
        today = local_day_key()
        if str(id or "").strip():
            item, error = capsule_mod.read(self.store, id, today=today)
            if item is None:
                return {
                    "output": {"reason": "没有找到这枚时间胶囊"},
                    "is_error": True,
                    "error": "NOT_FOUND",
                }
            if error == "locked":
                return {
                    "output": {
                        "reason": f"这枚胶囊要到 {item['unlock_day']} 才能打开（还有 {item['days_left']} 天）",
                        "unlock_day": item["unlock_day"],
                        "days_left": item["days_left"],
                    },
                    "is_error": True,
                    "error": "LOCKED",
                }
            return item

        day = self._normalize_day(date)
        if not day:
            return {
                "output": {"reason": "需要提供胶囊 id 或解锁日期"},
                "is_error": True,
                "error": "BAD_DATE",
            }
        items = capsule_mod.list_all(
            self.store, today=today, include_text=True, include_locked=True
        )
        matched = [item for item in items if item["unlock_day"] == day]
        if not matched:
            return {
                "output": {"reason": f"{day} 没有时间胶囊"},
                "is_error": True,
                "error": "NOT_FOUND",
            }
        unlocked = [item for item in matched if not item["locked"]]
        if not unlocked:
            return {
                "output": {
                    "reason": f"{day} 的胶囊还没到期（还有 {matched[0]['days_left']} 天）",
                    "unlock_day": day,
                    "days_left": matched[0]["days_left"],
                },
                "is_error": True,
                "error": "LOCKED",
            }
        return {"date": day, "count": len(unlocked), "items": unlocked}

    @llm_tool(
        name="chronicle_catalog_open",
        description=(
            "打开「拾光 · 对话目录」悬浮窗，让主人搜索并选择以前的对话送入聊天。"
            "主人说「打开拾光」「打开对话目录」「帮我找找以前的对话」时调用。"
            "拾光默认关闭；如果配置未开启会返回 opened=false，不要反复调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "可选：打开后立刻搜索的关键词。",
                }
            },
        },
    )
    async def tool_catalog_open(self, *, query: str = "") -> dict:
        # Tk 初始化不能占住插件事件循环；交给线程等待窗口 ready。
        opened, reason = await asyncio.to_thread(
            self._catalog_open, str(query or "").strip()
        )
        payload = {"opened": opened, "query": str(query or "").strip(), "reason": reason}
        if not opened:
            payload["hint"] = (
                "拾光开关没打开：请主人到插件设置里开启「拾光 · 对话目录」，再输入 #拾光。"
                if reason == "catalog_disabled"
                else "拾光窗口没能启动：把 reason 原样告诉主人，不要反复重试。"
            )
        return payload

    # ── 放映厅工具（和猫娘一起回看某一天） ──────────────────────────

    @llm_tool(
        name="chronicle_screening_open",
        description=(
            "打开「放映厅」，和主人一起回顾某一天的对话。主人说「我们看看某天」「一起回看」"
            "「放映厅」时调用。返回这一天看到第几条、猫娘看到第几条、停靠点和最近画面。"
            "打开后不会自动播放，等主人或你决定继续。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文（今天/昨天/10月7号），留空为今天。",
                }
            },
        },
    )
    async def tool_screening_open(self, *, date: str = "") -> dict:
        value, error = self._value_of(await self.entry_screening_open(date=date))
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_OPEN_FAILED"}
        return self._compact_screening_payload(value)

    @llm_tool(
        name="chronicle_screening_state",
        description=(
            "查看放映厅当前状态：放映/暂停/结束、看到第几条、猫娘看到第几条、"
            "这一场计划停在哪一条，以及已经留下的放映手记。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文，留空为今天。",
                }
            },
        },
    )
    async def tool_screening_state(self, *, date: str = "") -> dict:
        value, error = self._value_of(await self.entry_screening_state(date=date))
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_STATE_FAILED"}
        return self._compact_screening_payload(value)

    @llm_tool(
        name="chronicle_screening_watch",
        description=(
            "在放映厅里继续往下看几条（默认 5 条）。返回新看到的原话，并把手记里的"
            "「看到第几条」推进到那里。主人说「继续」「往下看」「再看一点」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文，留空为今天。",
                },
                "count": {
                    "type": "integer",
                    "description": "往下看几条，默认 5，最多 50。",
                },
            },
        },
    )
    async def tool_screening_watch(self, *, date: str = "", count: int = 5) -> dict:
        value, error = self._value_of(
            await self.entry_screening_control(
                date=date,
                action="advance",
                count=max(1, min(int(count or 5), 50)),
                mode="line",
                auto=False,
                driver="",
                who="catgirl",
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_WATCH_FAILED"}
        return self._compact_screening_payload(value)

    @llm_tool(
        name="chronicle_screening_stop",
        description=(
            "把放映停在第 index 条（这一天先看到这里），并记下这一场的停靠点；"
            "下次继续放映就从下一条开始。主人说「先看到这里」「今天就到这」「记住停在哪」时调用。"
            "如果只是想随口说一句，不需要调用本工具——你一开口，放映就会自动暂停。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文，留空为今天。",
                },
                "index": {
                    "type": "integer",
                    "description": "停在第几条（从 1 开始）；0 = 停在当前位置。",
                },
                "note": {
                    "type": "string",
                    "description": "想留在放映手记里的一句话，可省略。",
                },
            },
        },
    )
    async def tool_screening_stop(
        self, *, date: str = "", index: int = 0, note: str = ""
    ) -> dict:
        target = max(0, int(index or 0))
        value, error = self._value_of(
            await self.entry_screening_control(
                date=date,
                action="pause",
                to=target,
                stop_at=target,
                note=str(note or ""),
                who="catgirl",
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_STOP_FAILED"}
        return self._compact_screening_payload(value)

    @llm_tool(
        name="chronicle_screening_seek",
        description=(
            "跳转到某一天的第 index 条（会先暂停）。主人说「回到第 88 条」「跳到那一段」"
            "「重看这里」时调用。跳过去之后用 chronicle_screening_resume 继续。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文，留空为今天。",
                },
                "index": {
                    "type": "integer",
                    "description": "要跳到第几条（从 1 开始）。",
                },
            },
            "required": ["index"],
        },
    )
    async def tool_screening_seek(self, *, date: str = "", index: int = 0) -> dict:
        target = max(1, int(index or 1))
        value, error = self._value_of(
            await self.entry_screening_control(
                date=date,
                action="seek",
                to=target,
                who="catgirl",
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_SEEK_FAILED"}
        return self._compact_screening_payload(value)

    @llm_tool(
        name="chronicle_screening_resume",
        description=(
            "从上次停下的地方继续放映。主人说「继续放映」「接着看」「从刚才那里继续」时调用。"
            "调用后打开着的放映窗口会重新开始自动推进。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文，留空为今天。",
                }
            },
        },
    )
    async def tool_screening_resume(self, *, date: str = "") -> dict:
        value, error = self._value_of(
            await self.entry_screening_control(
                date=date,
                action="resume",
                driver="",
                who="catgirl",
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_RESUME_FAILED"}
        return self._compact_screening_payload(value)

    @llm_tool(
        name="chronicle_screening_comment",
        description=(
            "在放映厅里留一句手记，写在第 index 条旁边；猫娘写手记时会自动把放映停下来。"
            "主人说「把这句话记下来」「这里写一句」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD 或自然中文，留空为今天。",
                },
                "text": {
                    "type": "string",
                    "description": "要留下的那句话。",
                },
                "index": {
                    "type": "integer",
                    "description": "写在第几条旁边；0 = 当前位置。",
                },
            },
            "required": ["text"],
        },
    )
    async def tool_screening_comment(
        self, *, date: str = "", text: str = "", index: int = 0
    ) -> dict:
        value, error = self._value_of(
            await self.entry_screening_comment(
                date=date, text=text, index=max(0, int(index or 0)), who="catgirl"
            )
        )
        if value is None:
            return {"output": {"reason": error}, "is_error": True, "error": "SCREENING_COMMENT_FAILED"}
        return self._compact_screening_payload(value)

    # ── Hosted UI ───────────────────────────────────────────────────

    @ui.context(id="dashboard", title="对话编年史")
    async def panel_context(self) -> dict:
        if self.store is None:
            return {
                "ready": False,
                "days": [],
                "selected": "",
                "chapter_text": "",
                "diary_text": "",
                "stats": {},
                "llm": self._llm_status(),
            }
        today = local_day_key()
        # 面板要扫描全部日期的章节/日记文件，全是同步文件 I/O：
        # 放进线程执行，避免阻塞宿主事件循环（面板每次刷新都会走这里）。
        return await asyncio.to_thread(self._panel_payload, today)

    def _panel_payload(self, today: str) -> dict:
        days = self._day_cards(
            limit=PANEL_DAY_LIMIT, include_images=False, include_mood=False
        )
        selected = self._panel_day or (days[0]["date"] if days else today)
        if selected not in {item["date"] for item in days}:
            selected = days[0]["date"] if days else today
        chapter_text = self.store.read_chapter(selected) or ""
        diary_text = self.store.read_diary(selected) or ""
        cat_diary_raw = self.store.read_cat_diary(selected) or ""
        cat_diary_entries = self.store.cat_diary_entries(selected)
        readable_private = bool(self.cfg.cat_diary_private_readable)
        cat_diary_hidden = (
            bool(cat_diary_entries)
            and not readable_private
            and self.store.cat_diary_all_private(selected)
        )
        cat_diary_text = self.store.cat_diary_markdown(
            selected, include_private=readable_private
        )
        stats = self.store.stats()
        capsules = capsule_mod.list_all(
            self.store, today=today, include_text=True, limit=50
        )
        return {
            "ready": True,
            "today": today,
            "days": days,
            "days_count": stats["days"],
            "chapters_count": stats["chapters"],
            "diaries_count": stats["diaries"],
            "today_turns": self.store.count_turns(today),
            "selected": selected,
            "selected_exists": bool(chapter_text or diary_text),
            "chapter_text": chapter_text[:PANEL_TEXT_LIMIT],
            "diary_text": diary_text[:PANEL_TEXT_LIMIT],
            "chapter_truncated": len(chapter_text) > PANEL_TEXT_LIMIT,
            "diary_truncated": len(diary_text) > PANEL_TEXT_LIMIT,
            # 猫娘日记（任务3）
            "cat_diary_text": cat_diary_text[:PANEL_TEXT_LIMIT],
            "cat_diary_exists": bool(cat_diary_raw),
            "cat_diary_hidden": cat_diary_hidden,
            "cat_diary_truncated": len(cat_diary_text) > PANEL_TEXT_LIMIT,
            "cat_diary_days": self.store.list_cat_diary_days(),
            "cat_diary_entry_count": len(cat_diary_entries),
            "memo_text": memo_mod.read(self.store, selected),
            "memo_title": memo_mod.read_title(self.store, selected),
            "llm": self._llm_status(),
            "paths": {
                "root": stats["root"],
                "chapters": str(self.store.chapters_dir),
                "diary": str(self.store.diary_dir),
                "raw": str(self.store.raw_dir),
                "cat_diary": str(self.store.cat_diary_dir),
            },
            # 静态浏览页（plugin.toml 里 [[plugin.ui.panel]] entry = "static/index.html"）
            "ui_path": f"/plugin/{PLUGIN_ID}/ui/",
            "numbering_hint": "每句前的 #编号 是当天第几条记录，可以直接让猫娘按编号调取原文。",
            "memories_count": self.memories.count() if self.memories else 0,
            "memories": [
                {**item, "text": (item.get("text") or "")[:160]}
                for item in (self.memories.list() if self.memories else [])[:60]
            ],
            # 时间胶囊：未到期的条目不会带正文
            "capsules": capsules,
            "capsules_due": sum(
                1
                for item in capsules
                if item["unlock_day"] == today and not item["locked"]
            ),
            "config": self.cfg.as_public_dict(),
        }


    # ── 内部实现 ────────────────────────────────────────────────────

    @staticmethod
    def _value_of(result: Any) -> tuple[Any | None, str]:
        """把 ``Ok`` / ``Err`` 拆成 ``(值, 错误说明)``。"""

        if isinstance(result, Ok):
            return result.value, ""
        return None, str(getattr(result, "error", "") or "未知错误")

    @staticmethod
    def _compact_screening_payload(value: Any, *, item_limit: int = 8) -> Any:
        """给猫娘工具用的精简放映结果：不把整窗画面、全部场景和全部手记塞进上下文。"""

        if not isinstance(value, Mapping):
            return value
        keys = ("date", "total", "progress", "invited", "ignored", "changed")
        compact = {key: value.get(key) for key in keys if key in value}
        session = value.get("session")
        if isinstance(session, Mapping):
            item = dict(session)
            marks = item.get("marks")
            if isinstance(marks, list):
                item["marks"] = marks[-item_limit:]
            compact["session"] = item
        items = value.get("items")
        if isinstance(items, list):
            compact["items"] = items[-max(1, int(item_limit)):]
        return compact

    def _turns_for(self, day: str) -> list[Turn]:
        """某天的轮次：补齐角色 + 与章节同一口径（滤系统消息）。

        编号、给猫娘的工具与静态浏览页共用这一份，保证 ``#12`` 到处指同一条。
        """

        if self.store is None or not day:
            return []
        try:
            stamp = self.store.raw_path(day).stat().st_mtime
        except OSError:
            stamp = 0.0
        cached = self._turns_cache.get(day)
        if cached is not None and cached[0] == stamp:
            with self._lock:
                self._turns_cache.move_to_end(day)
            return cached[1]
        resolved = harvest.resolve_roles(
            self.store.load_turns(day),
            master_name=self.cfg.master_name,
            catgirl_name=self.cfg.catgirl_name,
        )
        turns = visible_turns(resolved, include_system=self.cfg.include_system)
        with self._lock:
            self._turns_cache[day] = (stamp, turns)
            self._turns_cache.move_to_end(day)
            while len(self._turns_cache) > TURNS_CACHE_DAYS:
                self._turns_cache.popitem(last=False)
        return turns

    def _recent_days(self, days: int = 7) -> list[str]:
        """最近的若干天（新 → 旧）。"""

        if self.store is None:
            return []
        available = self.store.list_raw_days()
        return list(reversed(available[-max(1, int(days or 7)):]))

    def _resolve_data_root(self) -> Path:
        """插件的可写数据目录（章节、日记、原始记录都放这里）。"""

        try:
            root = Path(self.data_path())
            if root.is_absolute():
                return root
        except Exception as exc:  # noqa: BLE001 - 回落到 storage_dir
            self.logger.warning("data_path() 不可用，改用 storage_dir：{}", exc)
        try:
            return Path(self.storage_dir) / "data"
        except Exception:  # noqa: BLE001
            return Path.cwd() / PLUGIN_ID

    @staticmethod
    def _resolve_user_data_root(data_root: Path | None) -> Path | None:
        """从数据目录反推用户数据根目录：``<root>/plugins/<id>/data``。"""

        if data_root is None:
            return None
        parents = list(data_root.parents)
        if len(parents) >= 3:
            return parents[2]
        return None

    # ── 章节图片（可写静态目录）──────────────────────────────────────

    def _static_source_dir(self) -> Path:
        """源码里的 ``static/`` 目录（只读，作为可写副本的来源）。"""

        candidates: list[Path] = []
        for attr in ("config_dir", "plugin_dir"):
            value = getattr(self, attr, None)
            if value:
                candidates.append(Path(value) / "static")
        candidates.append(Path(__file__).resolve().parent / "static")
        for candidate in candidates:
            if (candidate / "index.html").is_file():
                return candidate
        return candidates[0]

    def _register_writable_static_ui(self) -> None:
        """把 ``static/`` 复制到可写目录并注册成浏览页，图片才能被静态服务出来。

        宿主默认按 ``plugin.toml`` 的 ``entry = "static/index.html"`` 提供只读目录；
        这里再注册一个可写目录（``data/static_ui``），上传的图片放进它的
        ``images/<日期>/``，浏览页用 ``/plugin/<id>/ui/images/...`` 直接加载。
        """

        target = Path(self.data_path("static_ui"))
        try:
            source = self._static_source_dir()
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                for path in source.rglob("*"):
                    relative = path.relative_to(source)
                    if not relative.parts or relative.parts[0] == IMAGE_DIR_NAME:
                        continue
                    destination = target / relative
                    if path.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    elif path.is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
            self._static_ui_dir = target
        except Exception as exc:  # noqa: BLE001 - 复制失败也要能用图片目录
            self.logger.warning("复制静态浏览页失败：{}", exc)

        self._ensure_images_dir()
        if not (target / "index.html").is_file():
            fallback = getattr(self, "register_static_ui", None)
            if callable(fallback):
                try:
                    fallback("static")
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("回退到只读静态目录失败：{}", exc)
                else:
                    self.logger.warning("可写浏览页复制不完整，已回退到只读静态目录")
                    return
        registered = getattr(self, "_notify_static_ui_registered", None)
        if not callable(registered):
            self.logger.warning("宿主不支持可写静态目录，章节图片可能无法显示")
            return
        try:
            registered(
                {
                    "enabled": True,
                    "directory": str(target),
                    "index_file": "index.html",
                    "cache_control": "public, max-age=60",
                    "plugin_id": PLUGIN_ID,
                }
            )
            self.logger.info("浏览页静态目录已注册：{}", target)
        except Exception as exc:  # noqa: BLE001 - 注册失败不影响插件本体
            self.logger.warning("注册可写静态目录失败：{}", exc)

    def _ensure_images_dir(self) -> Path | None:
        """确保图片目录存在并返回它（未初始化时返回 ``None``）。"""

        if self._images_dir is not None and self._images_dir.is_dir():
            return self._images_dir
        try:
            root = Path(self.data_path("static_ui")) / IMAGE_DIR_NAME
            root.mkdir(parents=True, exist_ok=True)
            self._images_dir = root
            return root
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("创建图片目录失败：{}", exc)
            return None

    @staticmethod
    def _image_ext(filename: str, mime: str = "") -> str:
        """从文件名/MIME 里认出图片扩展名；不支持时返回空串。"""

        ext = Path(str(filename or "")).suffix.lower()
        if ext == ".jpe":
            ext = ".jpg"
        if ext in IMAGE_EXTS:
            return ext
        mime = str(mime or "").lower()
        mapping = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        return mapping.get(mime, "")

    @staticmethod
    def _decode_image_payload(data: str) -> tuple[bytes, str]:
        """把 data URL 或纯 base64 解成 ``(字节, MIME)``；失败返回 ``(b"", "")``。"""

        raw = str(data or "").strip()
        if not raw:
            return b"", ""
        mime = ""
        if raw.startswith("data:"):
            header, _, raw = raw.partition(",")
            match = re.match(r"data:([^;,]+)", header)
            if match:
                mime = match.group(1).strip().lower()
        try:
            binary = base64.b64decode(raw, validate=False)
        except (ValueError, binascii.Error):
            return b"", ""
        return binary, mime

    @staticmethod
    def _atomic_write_bytes(path: Path, binary: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with open(temp, "wb") as handle:
            handle.write(binary)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, path)

    @staticmethod
    def _thumb_path(image_path: Path) -> Path:
        return image_path.parent / THUMB_DIR_NAME / (image_path.stem + ".jpg")

    def _thumb_url(self, day: str, thumb: Path) -> str:
        return (
            f"/plugin/{PLUGIN_ID}/ui/{IMAGE_DIR_NAME}/{day}/"
            f"{THUMB_DIR_NAME}/{thumb.name}"
        )

    def _ensure_thumb(self, image_path: Path) -> Path | None:
        """生成（或复用）预览缩略图；Pillow 不可用或图片损坏时返回 ``None``。"""

        try:
            thumb = self._thumb_path(image_path)
            if thumb.is_file() and thumb.stat().st_mtime >= image_path.stat().st_mtime:
                return thumb
        except OSError:
            return None
        image_module, ops_module = _pil_modules()
        if image_module is None:
            return None
        try:
            with image_module.open(image_path) as source:
                frame = ops_module.exif_transpose(source)
                if frame.mode == "L":
                    frame = frame.convert("RGB")
                elif frame.mode not in ("RGB",):
                    rgba = frame.convert("RGBA")
                    backdrop = image_module.new("RGB", rgba.size, (255, 255, 255))
                    backdrop.paste(rgba, mask=rgba.split()[-1])
                    frame = backdrop
                frame.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
                thumb.parent.mkdir(parents=True, exist_ok=True)
                temp = thumb.with_name(thumb.name + ".tmp")
                frame.save(
                    temp,
                    format="JPEG",
                    quality=THUMB_QUALITY,
                    optimize=True,
                    progressive=True,
                )
            os.replace(temp, thumb)
            return thumb
        except Exception as exc:  # noqa: BLE001 - 缩略图失败就回退到原图
            self.logger.warning("生成缩略图失败：{}（{}）", image_path.name, exc)
            return None

    def _image_items(self, day: str = "", *, limit: int = 0) -> list[dict]:
        """列出某天（``day`` 为空则全部日期）的图片元信息，按时间倒序。"""

        root = self._ensure_images_dir()
        if root is None or not root.is_dir():
            return []
        if day:
            directories = [root / day]
        else:
            try:
                directories = sorted(
                    (path for path in root.iterdir() if path.is_dir()),
                    key=lambda path: path.name,
                    reverse=True,
                )
            except OSError:
                return []
        items: list[dict] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            try:
                files = list(directory.iterdir())
            except OSError:
                continue
            for path in files:
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                items.append(
                    {
                        "date": directory.name,
                        "filename": path.name,
                        "url": f"/plugin/{PLUGIN_ID}/ui/{IMAGE_DIR_NAME}/{directory.name}/{path.name}",
                        "size": stat.st_size,
                        "created_at": stat.st_mtime,
                    }
                )
        items.sort(key=lambda item: (item["date"], item["created_at"]), reverse=True)
        if limit and limit > 0:
            items = items[: int(limit)]
        # 列表只回元信息：缩略图由前端按"是否进入视口"逐张调用 get_image_thumb 懒加载，
        # 避免一次把上百张 base64 塞进 /runs/export 的响应里。
        for item in items:
            path = root / item["date"] / item["filename"]
            item["thumb_url"] = ""  # 由前端懒加载后填到 <img>
            item["url"] = ""  # 原图走 get_image_data entry 按需拉取
            item["memory"] = self._read_image_memory(path)
        return items

    @staticmethod
    def _path_to_data_url(path: Path | None) -> str:
        """把本地图片文件读成 ``data:image/...;base64,...``；失败返回空串。"""

        if path is None:
            return ""
        try:
            if not path.is_file():
                return ""
            raw = path.read_bytes()
        except OSError:
            return ""
        if not raw:
            return ""
        suffix = path.suffix.lower().lstrip(".")
        if suffix == "jpg":
            suffix = "jpeg"
        if suffix not in ("png", "jpeg", "gif", "webp", "bmp"):
            suffix = "jpeg"
        try:
            encoded = base64.b64encode(raw).decode("ascii")
        except Exception:  # noqa: BLE001
            return ""
        return f"data:image/{suffix};base64,{encoded}"

    def _image_count(self, day: str) -> int:
        """某天保存的图片数量（没有就是 0）；按目录 mtime 缓存。"""

        root = self._images_dir
        if root is None or not day:
            return 0
        directory = root / day
        if not directory.is_dir():
            return 0
        key = str(directory)
        try:
            stamp = directory.stat().st_mtime_ns
        except OSError:
            return 0
        hit = self._image_count_cache.get(key)
        if hit is not None and hit[0] == stamp:
            self._image_count_cache.move_to_end(key)
            return hit[1]
        try:
            count = sum(
                1
                for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_EXTS
            )
        except OSError:
            return 0
        self._image_count_cache[key] = (stamp, count)
        while len(self._image_count_cache) > 256:
            self._image_count_cache.popitem(last=False)
        return count

    def _image_file(self, day: str, filename: str) -> Path | None:
        """把「日期 + 文件名」安全地解析成图片路径；非法或不存在返回 ``None``。"""

        root = self._ensure_images_dir()
        raw_name = str(filename or "")
        name = Path(raw_name).name
        if (
            root is None
            or not day
            or not name
            or name != raw_name
            or name.startswith(".")
            or Path(name).suffix.lower() not in IMAGE_EXTS
        ):
            return None
        try:
            path = root / day / name
            if root.resolve() not in path.resolve().parents:
                return None
        except OSError:
            return None
        return path if path.is_file() else None

    @staticmethod
    def _image_memory_path(image_path: Path) -> Path:
        return image_path.with_name(image_path.name + ".memory.json")

    def _read_image_memory(self, image_path: Path) -> str:
        """读图片旁边的记忆文字；没有或损坏返回 ``""``。"""

        sidecar = self._image_memory_path(image_path)
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return ""
        if isinstance(data, dict):
            return str(data.get("text") or "").strip()
        return ""

    def _write_image_memory(self, image_path: Path, text: str) -> None:
        """写/清除图片记忆（空文本即删除记忆文件）。"""

        sidecar = self._image_memory_path(image_path)
        if not text:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
            return
        payload = json.dumps(
            {"text": text, "updated_at": int(time.time())}, ensure_ascii=False
        )
        temp = sidecar.with_name(sidecar.name + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, sidecar)

    def _delete_image_files(self, image_path: Path) -> None:
        """删除原图、缩略图和图片记忆。"""

        for target in (
            image_path,
            self._thumb_path(image_path),
            self._image_memory_path(image_path),
        ):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    def _image_push_part(self, image_path: Path) -> dict | None:
        """把图片读成 push_message 的 image part；太大时改发缩略图。"""

        target = image_path
        try:
            if image_path.stat().st_size > PUSH_IMAGE_MAX_BYTES:
                thumb = self._ensure_thumb(image_path)
                if thumb is not None:
                    target = thumb
            data = target.read_bytes()
        except OSError:
            return None
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(target.suffix.lower(), "image/jpeg")
        return {"type": "image", "data": data, "mime": mime}

    # ── 模型目录（浏览页「模型配置」）─────────────────────────────────

    def _catalog_path(self) -> Path | None:
        if self.data_root is None:
            return None
        return self.data_root / "models.json"

    def _load_catalog(self) -> dict:
        """读 models.json。

        结构：``{"active": 模型ID|None, "models":[{"id","name","base_url","api_key"}]}``。
        每条模型自带 API 地址与密钥（密钥只写不读，回传前端时只给 has_key）。
        """

        path = self._catalog_path()
        if path is None:
            return {"active": None, "models": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return {"active": None, "models": []}
        if not isinstance(data, dict):
            return {"active": None, "models": []}
        models = data.get("models")
        if not isinstance(models, list):
            models = []
        cleaned = []
        for item in models:
            if isinstance(item, dict) and str(item.get("id") or item.get("model") or "").strip():
                mid = str(item.get("id") or item.get("model")).strip()
                cleaned.append(
                    {
                        "id": mid,
                        "name": str(item.get("name") or mid).strip(),
                        "base_url": str(item.get("base_url") or "").strip(),
                        "api_key": str(item.get("api_key") or ""),
                    }
                )
        active = data.get("active")
        active = str(active).strip() if isinstance(active, str) and active.strip() else None
        if active and active not in {m["id"] for m in cleaned}:
            active = None
        return {"active": active, "models": cleaned}

    def _catalog_entry(self, model_id: str) -> dict | None:
        for entry in self._load_catalog()["models"]:
            if entry["id"] == model_id:
                return entry
        return None

    def _save_catalog(self, data: dict) -> None:
        path = self._catalog_path()
        if path is None:
            raise RuntimeError("数据目录尚未初始化")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def _refresh_llm(self) -> None:
        """重新解析可用的模型端点（只报告是否配置了 key，从不打印 key）。"""

        path, core_cfg = llm_mod.locate_core_config(self.user_data_root)
        self.llm_core_config = core_cfg
        self.llm_config_path = str(path) if path else ""
        catalog = self._load_catalog()
        entry = self._catalog_entry(catalog["active"]) if catalog.get("active") else None
        if entry is None and len(catalog["models"]) == 1:
            # 目录里只存了一个完整模型却没点选：按用户意图直接用它，
            # 否则会回落到 plugin.toml 里的旧端点（常见于换密钥后忘了选中）
            only = catalog["models"][0]
            if only["base_url"] and only["api_key"]:
                entry = only
        if entry and entry["base_url"] and entry["api_key"]:
            # 目录选中的模型自带地址+密钥，优先用它
            endpoint = llm_mod.LlmEndpoint(
                base_url=entry["base_url"],
                model=entry["id"],
                api_key=entry["api_key"],
                provider="catalog",
                source="catalog",
            )
            reason = ""
        else:
            endpoint, reason = llm_mod.resolve_endpoint(self.cfg, core_cfg)
            if endpoint is not None and entry:
                # 只有模型名、没有自带地址/密钥：复用当前端点，只换模型名
                endpoint.model = entry["id"]
        self.llm_endpoint = endpoint
        self.llm_reason = reason
        if endpoint is not None:
            self.logger.info(
                "日记将调用模型：{} @ {}（来源 {}）",
                endpoint.model, endpoint.base_url, endpoint.source,
            )
        else:
            self.logger.info("日记改用本地统计摘要：{}", llm_mod.reason_text(reason))

    def _llm_status(self) -> dict:
        # ``available`` 沿用旧语义 = 已配置可直连的端点（fast，不联网）。
        # 面板要判断"真能用吗"，请用 ``probed`` + ``usable``：
        #   - probed is None  → 还没探测过（点"检查模型"会探测）
        #   - probed True     → 真能用（绿色"可用"）
        #   - probed False    → 已配置但调用失败（红色"不可用"，reason 里有原因）
        return {
            "available": self.llm_endpoint is not None,
            "reason": self.llm_reason,
            "detail": llm_mod.reason_text(self.llm_reason),
            "endpoint": self.llm_endpoint.describe() if self.llm_endpoint else None,
            "config_path": self.llm_config_path,
            "config_found": bool(self.llm_core_config),
            "probed": self.llm_probed,
            "probe_reason": self.llm_probe_reason,
            "probe_detail": self.llm_probe_detail,
            "usable": self.llm_endpoint is not None and self.llm_probed is not False,
        }

    async def _probe_llm(self) -> dict:
        """检查模型是否真的可用：解析端点后发一条最小测试请求。

        结果同时记进实例状态（``llm_probed`` / ``llm_probe_reason`` /
        ``llm_probe_detail``），供面板展示"已配置但不可用"这类诚实状态。
        """

        if self.llm_endpoint is None:
            self.llm_probed = False
            self.llm_probe_reason = self.llm_reason
            self.llm_probe_detail = llm_mod.reason_text(self.llm_reason)
            return {
                "available": False,
                "reason": self.llm_reason,
                "detail": llm_mod.reason_text(self.llm_reason),
                "endpoint": None,
                "probed": False,
            }
        text, reason = await llm_mod.achat_completion(
            self.llm_endpoint,
            "只回复两个字：在。",
            timeout=min(self.cfg.llm_timeout_seconds, 30.0),
            temperature=0.0,
            logger=self.logger,
        )
        ok = bool(text)
        self.llm_probed = ok
        self.llm_probe_reason = "" if ok else reason
        self.llm_probe_detail = (
            f"模型可用，回复：{text.strip()[:60]}" if ok else llm_mod.reason_text(reason)
        )
        if ok:
            return {
                "available": True,
                "reason": "",
                "detail": f"模型可用，回复：{text.strip()[:60]}",
                "endpoint": self.llm_endpoint.describe(),
                "probed": True,
                "reply": text.strip()[:200],
            }
        return {
            "available": False,
            "reason": reason,
            "detail": llm_mod.reason_text(reason),
            "endpoint": self.llm_endpoint.describe(),
            "probed": True,
            "reply": "",
        }

    def _warn_throttled(self, message: str, *args) -> None:
        now = time.monotonic()
        if now - self._last_warn < 60:
            return
        self._last_warn = now
        self.logger.warning(message, *args)

    def _ingest(self, turns: list[Turn]) -> dict[str, int]:
        """把轮次写进磁盘；插件还没就绪时先缓存。"""

        if not turns:
            return {}
        with self._lock:
            if self.store is None or not self._ready:
                self._buffer.extend(turns)
                if len(self._buffer) > 5000:
                    self._buffer = self._buffer[-5000:]
                return {}
            return self.store.append_turns(turns)

    async def _read_bus(self, getter, attempts: list[dict]) -> tuple[list, str]:
        """按几种可能的调用方式读总线，返回 ``(记录, 错误)``。"""

        last_error = ""
        for kwargs in attempts:
            try:
                result = await asyncio.to_thread(lambda: getter(**kwargs))
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001 - 总线不可用不应中断采集
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if inspect.isawaitable(result):
                try:
                    result = await result
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
            bus_error = getattr(result, "error", None)
            if bus_error:
                last_error = str(bus_error)
                continue
            try:
                return list(result), ""
            except TypeError:
                return [], ""
        return [], last_error

    async def _harvest_from_bus(self) -> dict:
        """补录一轮：消息总线（用户消息）+ 宿主对话存储（双方完整对话）。"""

        if self.store is None:
            return {"polled": 0, "added": 0, "skipped": "not_ready"}

        bus = getattr(self.ctx, "bus", None)
        collected: list[Turn] = []
        notes: list[str] = []

        memory = getattr(bus, "memory", None) if bus is not None else None
        memory_get = getattr(memory, "get", None)
        if callable(memory_get):
            records, error = await self._read_bus(
                memory_get,
                [
                    {"bucket_id": "default", "limit": self.cfg.poll_memory_limit, "timeout": 1.0},
                    {"bucket_id": "default", "limit": self.cfg.poll_memory_limit},
                    {},
                ],
            )
            if error:
                notes.append(f"memory:{error}")
            collected.extend(harvest.turns_from_records(records, source="bus.memory"))

        conversations = getattr(bus, "conversations", None) if bus is not None else None
        conversations_get = getattr(conversations, "get", None)
        if callable(conversations_get):
            records, error = await self._read_bus(
                conversations_get,
                [
                    {"max_count": self.cfg.poll_conversation_limit},
                    {"limit": self.cfg.poll_conversation_limit},
                    {},
                ],
            )
            if error:
                notes.append(f"conversations:{error}")
            collected.extend(harvest.turns_from_records(records, source="bus.conversations"))

        # 3) 宿主自己的对话存储——实测唯一能同时拿到用户与角色双方对话的来源。
        # 先比文件签名：宿主还没写新内容时直接跳过打开 SQLite + 解析 200 条消息。
        store_info: dict = {}
        if self.cfg.read_host_memory_store and self.user_data_root is not None:
            try:
                store = host_store.select_store(
                    self.user_data_root, self.cfg.memory_store_character
                )
                if store is None:
                    self._host_store_signature = None
                    store_info = {"used": "", "source": ""}
                else:
                    signature = (store.name, host_store.store_signature(store))
                    if signature == self._host_store_signature:
                        store_info = {"used": store.name, "source": "unchanged"}
                    else:
                        store_turns, source = host_store.read_store(
                            store, limit=self.cfg.memory_store_limit
                        )
                        self._host_store_signature = signature
                        collected.extend(store_turns)
                        store_info = {
                            "used": store.name,
                            "source": source,
                            "count": len(store_turns),
                        }
            except Exception as exc:  # noqa: BLE001 - 读不到就只靠总线
                self._host_store_signature = None
                self._warn_throttled("读取宿主对话存储失败：{}", exc)
                store_info = {"error": f"{type(exc).__name__}: {exc}"}

        # 先把各来源的重复项合并、按对话规律补齐角色，再过滤系统消息
        collected = harvest.resolve_roles(
            collected,
            master_name=self.cfg.master_name,
            catgirl_name=self.cfg.catgirl_name,
        )
        if not self.cfg.capture_all_sources:
            collected = [turn for turn in collected if turn.role != "system"]

        added = self._ingest(collected)
        # 部分宿主不会把用户输入派发到 @message(source="chat")，
        # 这里用补录到的新轮次兜底：#拾光 命令与放映自动暂停都按同一套判断。
        # 只取比上次补录更新的轮次，避免每 60 秒把旧发言当成"猫娘刚开口"。
        fresh = [
            turn
            for turn in collected
            if float(getattr(turn, "ts", 0) or 0) > self._harvest_seen_ts
        ]
        if fresh:
            self._harvest_seen_ts = max(float(getattr(t, "ts", 0) or 0) for t in fresh)
            self._maybe_open_catalog(fresh)
            self._maybe_pause_screening(fresh)
        if notes:
            self._warn_throttled("总线读取部分失败：{}", "；".join(notes))
        return {
            "polled": len(collected),
            "added": sum(added.values()),
            "days": added,
            "notes": notes,
            "host_store": store_info,
        }

    def _capture_status(self) -> dict:
        """采集来源概览（不读正文，供 status 入口与排障用）。"""

        status: dict[str, Any] = {
            "bus_memory": self.cfg.poll_memory_limit,
            "bus_conversations": self.cfg.poll_conversation_limit,
            "host_store_enabled": self.cfg.read_host_memory_store,
        }
        if self.user_data_root is None:
            status["host_store"] = "user_data_root 未知"
            return status
        try:
            stores = host_store.find_character_stores(self.user_data_root)
            active = host_store.select_store(self.user_data_root, self.cfg.memory_store_character)
            status["stores"] = [store.name for store in stores]
            status["active_store"] = active.name if active else ""
            if active is not None:
                _, source = host_store.read_store(active, limit=1)
                status["active_store_source"] = source or "(空)"
        except Exception as exc:  # noqa: BLE001
            status["error"] = f"{type(exc).__name__}: {exc}"
        return status

    async def _probe_bus(self, *, sample: int = 2) -> dict:
        """诊断：摊开总线记录的字段名与识别结果（不写入任何记录）。

        宿主版本之间总线载荷的字段名并不统一，这个入口让排障不必靠猜：
        它显示"记录里有哪些字段、被识别成了谁说的话"。
        """

        bus = getattr(self.ctx, "bus", None)
        report: dict[str, Any] = {
            "available": {},
            "counts": {},
            "memory_keys": [],
            "conversations_keys": [],
            "messages_keys": [],
            "detected": [],
            "errors": [],
        }
        targets = (
            (
                "memory",
                getattr(bus, "memory", None),
                [
                    {"bucket_id": "default", "limit": self.cfg.poll_memory_limit, "timeout": 1.0},
                    {},
                ],
                "user",
            ),
            (
                "conversations",
                getattr(bus, "conversations", None),
                [{"max_count": 20}, {}],
                "unknown",
            ),
            # 只探测不采集：message plane 上混着插件/HUD 消息，直接收会污染章节
            (
                "messages",
                getattr(bus, "messages", None),
                [{"max_count": 20}, {}],
                "unknown",
            ),
        )
        limit = max(1, min(int(sample or 2), 20))
        for label, holder, attempts, default_role in targets:
            getter = getattr(holder, "get", None) if holder is not None else None
            report["available"][label] = callable(getter)
            if not callable(getter):
                continue
            records, error = await self._read_bus(getter, attempts)
            if error:
                report["errors"].append(f"{label}: {error}")
            report["counts"][label] = len(records)
            keys: set[str] = set()
            for record in records[:limit]:
                mapping = harvest.unwrap_record(record)
                if isinstance(mapping, Mapping):
                    keys.update(str(key) for key in mapping.keys())
                turn = harvest.turn_from_payload(
                    record, source=f"bus.{label}", default_role=default_role
                )
                if turn is not None:
                    preview = turn.text[:120]
                elif isinstance(mapping, Mapping):
                    preview = str(dict(mapping))[:200]
                else:
                    preview = str(record)[:200]
                report["detected"].append(
                    {
                        "source": label,
                        "role": turn.role if turn is not None else "(未识别)",
                        "speaker": turn.speaker if turn is not None else "",
                        "preview": preview,
                        "fields": harvest.describe_record(record),
                    }
                )
            report[f"{label}_keys"] = sorted(keys)

        # 宿主自己的对话存储：最能说明"猫娘的话为什么没记到"的一段
        if self.user_data_root is not None:
            try:
                store_turns, store_info = host_store.read_active_store(
                    self.user_data_root,
                    wanted=self.cfg.memory_store_character,
                    limit=limit,
                )
                report["host_store"] = dict(store_info)
                report["host_store"]["detected"] = [
                    {
                        "role": turn.role,
                        "speaker": turn.speaker,
                        "preview": turn.text[:120],
                        "source": turn.source,
                    }
                    for turn in store_turns[-limit:]
                ]
            except Exception as exc:  # noqa: BLE001
                report["host_store"] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            report["host_store"] = {"error": "user_data_root 未知"}
        return report

    def _normalize_day(self, value: str | None) -> str:
        """把用户输入的日期收敛成 YYYY-MM-DD；非法返回空串。

        除了 ``YYYY-MM-DD`` / ``YYYY/MM/DD`` / ``YYYY.MM.DD`` / ``YYYYMMDD``，
        还接受自然中文表达：今天/昨天/前天、10月7号、七月七日、2026年10月7日
        （月/日/年份里的中文数字也会被识别）。
        """

        text = str(value or "").strip()
        if not text:
            return ""
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
            try:
                return time.strftime("%Y-%m-%d", time.strptime(text, fmt))
            except ValueError:
                continue
        return _normalize_day_text(text)

    async def _refresh_today_chapter(self, *, force: bool = False) -> None:
        """按配置节流地刷新"今天"的章节，让用户随时能看到当天内容。"""

        if self.store is None or not self._ready:
            return
        now = time.time()
        interval = max(30, int(self.cfg.live_refresh_seconds or 300))
        if not force and now - self._last_live_refresh < interval:
            return
        # 读原始记录、排版章节、落盘都是同步 I/O，放到线程里做，别卡住事件循环
        await asyncio.to_thread(self._refresh_today_chapter_sync, now, force)

    def _refresh_today_chapter_sync(self, now: float, force: bool) -> None:
        day = local_day_key()
        turns = harvest.resolve_roles(
            self.store.load_turns(day),
            master_name=self.cfg.master_name,
            catgirl_name=self.cfg.catgirl_name,
        )
        # 无论这次是否真的落盘，都推进节流水位，避免每分钟重复进线程空跑。
        self._last_live_refresh = now
        if not turns and not force:
            return
        # 条数没变就说明当天没有新对话：不必重新排版、更不必重写磁盘文件。
        if not force and self._chapter_revisions.get(day) == len(turns):
            return
        index = self.store.day_index(day)
        markdown = chapter_mod.build_chapter(day, turns, self.cfg, index=index)
        self.store.write_chapter(day, markdown)
        self._chapter_revisions[day] = len(turns)

    async def _build_day(
        self,
        day: str,
        *,
        with_diary: bool = True,
        use_llm: bool = True,
        force: bool = False,
    ) -> dict:
        """生成某一天的章节（与可选日记），返回结果摘要。

        日记默认只在 ``with_diary`` 时生成——这样"生成今天的章节"不会顺带写日记，
        日记由专门的按钮或第二天的结算来写。

        同步的文件读写/排版放在线程里（``_prepare_day_build`` /
        ``_finish_day_build``），中间只把模型调用留在事件循环里等。
        """

        if self.store is None:
            raise SdkError("插件尚未初始化完成")

        prepared = await asyncio.to_thread(
            self._prepare_day_build,
            day,
            bool(with_diary),
            bool(use_llm),
            bool(force),
        )
        if prepared is None:
            return {"day": day, "skipped": "already_built"}

        llm_raw: str | None = None
        llm_model = ""
        model_reason = ""
        if prepared["needs_diary"] and prepared["use_model"]:
            llm_raw, model_reason = await llm_mod.achat_completion(
                self.llm_endpoint,
                prepared["prompt"],
                timeout=self.cfg.llm_timeout_seconds,
                temperature=self.cfg.llm_temperature,
                logger=self.logger,
            )
            if llm_raw:
                llm_model = self.llm_endpoint.model
            else:
                # 模型这次没成：把探测结果记下来，面板就会显示"不可用"而非"可用"
                self.llm_probed = False
                self.llm_probe_reason = model_reason
                self.llm_probe_detail = llm_mod.reason_text(model_reason)
                self._warn_throttled("模型没有返回内容（{}），{} 的日记留空", model_reason, day)

        chapter_markdown, diary_markdown, diary_info = await asyncio.to_thread(
            self._finish_day_build, day, prepared, llm_raw, llm_model, model_reason
        )
        return {
            "day": day,
            "turns": len(prepared["turns"]),
            "chapter": {"chars": len(chapter_markdown), "path": str(self.store.chapter_path(day))},
            "diary": {
                "engine": diary_info.get("engine"),
                "engine_label": "语言模型" if diary_info.get("engine") == "llm" else "本地摘要",
                "model": diary_info.get("model", ""),
                "mood": diary_info.get("mood_label"),
                "keywords": (diary_info.get("keywords") or [])[:6],
                "chars": len(diary_markdown),
                "path": str(self.store.diary_path(day)),
                "skipped": diary_info.get("skipped"),
                "reason": diary_info.get("reason") or "",
                "detail": diary_info.get("detail") or "",
            },
        }

    def _prepare_day_build(
        self, day: str, with_diary: bool, use_llm: bool, force: bool
    ) -> dict | None:
        """章节生成中不依赖模型的同步部分：读原话、定标题、写章节、备提示词。"""

        needs_chapter = force or not self.store.has_chapter(day)
        needs_diary = bool(with_diary) and (force or not self.store.has_diary(day))
        if not needs_chapter and not needs_diary:
            return None

        turns = harvest.resolve_roles(
            self.store.load_turns(day),
            master_name=self.cfg.master_name,
            catgirl_name=self.cfg.catgirl_name,
        )
        index = self.store.day_index(day)

        # 章节标题：用户自定义 > 自动从备忘录取随机一条 > 默认格式
        try:
            default_title = self.cfg.chapter_title_format.format(
                index=index, date=day, chapter=index
            )
        except (KeyError, IndexError, ValueError):
            default_title = f"第 {index} 章 · {day}"
        chapter_title = memo_mod.effective_title(
            self.store,
            day,
            auto_from_memo=bool(self.cfg.auto_title_from_memo),
            default=default_title,
        )

        if needs_chapter:
            chapter_markdown = chapter_mod.build_chapter(
                day, turns, self.cfg, index=index, title=chapter_title
            )
            self.store.write_chapter(day, chapter_markdown)
        else:
            chapter_markdown = self.store.read_chapter(day) or ""

        # 日记来源：备忘录为主、对话为辅（任务改动）——读当天备忘录正文作主干
        prompt = ""
        use_model = bool(needs_diary and use_llm and self.llm_endpoint is not None)
        if use_model:
            stats = diary_mod.analyze_day(day, turns, self.cfg)
            prompt = diary_mod.build_llm_prompt(
                day, turns, stats, self.cfg, memo=memo_mod.read(self.store, day)
            )

        return {
            "turns": turns,
            "chapter_markdown": chapter_markdown,
            "needs_diary": needs_diary,
            "use_model": use_model,
            "prompt": prompt,
        }

    def _finish_day_build(
        self,
        day: str,
        prepared: dict,
        llm_raw: str | None,
        llm_model: str,
        model_reason: str,
    ) -> tuple[str, str, dict]:
        """把模型回复编排成日记并落盘，重建目录。"""

        chapter_markdown = prepared["chapter_markdown"]
        turns = prepared["turns"]
        diary_markdown = ""
        diary_info: dict = {"engine": "", "model": "", "mood_label": "", "keywords": []}
        if prepared["needs_diary"]:
            if llm_raw:
                diary_markdown, diary_info = diary_mod.compose_diary(
                    day, turns, self.cfg, llm_raw=llm_raw, llm_model=llm_model
                )
                self.store.write_diary(day, diary_markdown)
            else:
                # 模型不可用：日记留空（不写文件），等模型可用后再生成；带原因便于界面提示
                diary_info = {
                    "engine": "",
                    "model": "",
                    "mood_label": "",
                    "keywords": [],
                    "skipped": "no_model",
                    "reason": model_reason or self.llm_probe_reason or "no_model",
                    "detail": llm_mod.reason_text(model_reason or self.llm_probe_reason or "no_model"),
                }

        self.store.write_indexes()

        self.logger.info(
            "{}：{} 句，章节 {} 字，日记 {}（{}）",
            day, len(turns), len(chapter_markdown),
            diary_info.get("engine") or "-", diary_info.get("mood_label") or "-",
        )
        return chapter_markdown, diary_markdown, diary_info

    async def _finalize_pending(self, *, backfill_days: int = 0) -> list[str]:
        """结算所有"已经结束但还没生成日记"的日期。

        过 0 点后的每日日记只安静落盘，不向聊天发送提示；
        需要看时由主人发送「爱妻日记」+日期，走独立触发通道递给猫娘。
        """

        if self.store is None:
            return []
        today = local_day_key()
        state = self.store.load_state()
        built: list[str] = []

        candidates = [day for day in self.store.list_raw_days() if day < today]
        if backfill_days > 0:
            recent = self.store.list_raw_days()[-int(backfill_days):]
            for day in recent:
                if day < today and day not in candidates:
                    candidates.append(day)

        for day in sorted(set(candidates)):
            if self.store.has_chapter(day) and self.store.has_diary(day):
                continue
            try:
                await self._build_day(day, use_llm=True, force=False)
                built.append(day)
            except Exception as exc:  # noqa: BLE001 - 单天失败不影响其余
                self.logger.warning("结算 {} 失败：{}", day, exc)

        if built:
            state["last_finalize"] = time.time()
            self.store.save_state(state)
        return built

    def _day_cards(
        self, *, limit: int = 30, include_images: bool = True, include_mood: bool = True
    ) -> list[dict]:
        """给面板/入口用的轻量日期列表（只数行，不解析全文）。

        ``include_images`` / ``include_mood`` 默认打开给浏览页与 LLM 用；
        面板不展示这两列，传 False 可省掉每天的目录扫描与日记全文读取。
        """

        if self.store is None:
            return []
        all_days = self.store.list_raw_days()
        # 一次算出"日期 → 章序号"，避免为每张卡片都重新扫一遍原始目录（O(n²)）。
        index_by_day = {day: order for order, day in enumerate(all_days, start=1)}
        if limit and limit > 0:
            days = all_days[-int(limit):]
        else:
            days = all_days
        cards: list[dict] = []
        # 猫娘日记的日期一次取齐（底层有目录签名缓存），免得为每天解析一遍条目 JSON
        cat_days = set(self.store.list_cat_diary_days())
        for day in reversed(days):
            has_diary = self.store.has_diary(day)
            mood = ""
            if include_mood and has_diary:
                diary = self.store.read_diary(day)
                if diary:
                    match = _MOOD_RE.search(diary)
                    if match:
                        mood = (match.group(1) or match.group(3) or "").strip()
            # 章节标题与生成时同一口径：手动 > 自动从备忘录取 > 默认格式。
            # 只读，不落盘；面板在日期旁边展示用它。
            idx = index_by_day.get(day) or self.store.day_index(day)
            try:
                default_title = self.cfg.chapter_title_format.format(
                    index=idx, date=day, chapter=idx
                )
            except (KeyError, IndexError, ValueError):
                default_title = f"第 {idx} 章 · {day}"
            chapter_title = memo_mod.effective_title(
                self.store,
                day,
                auto_from_memo=bool(self.cfg.auto_title_from_memo),
                default=default_title,
            )
            card = {
                "date": day,
                "turns": self.store.turn_count(day),
                "chapter": self.store.has_chapter(day),
                "diary": has_diary,
                # 猫娘自己写的日记（任务2/3 面板标记用）
                "cat_diary": day in cat_days,
                "mood": mood,
                "chapter_title": chapter_title,
            }
            if include_images:
                # 这一章保存的图片数量（浏览页的图片入口直接显示它）
                card["images"] = self._image_count(day)
            cards.append(card)
        return cards


__all__ = ["NekoDailyChroniclePlugin", "PLUGIN_ID"]
