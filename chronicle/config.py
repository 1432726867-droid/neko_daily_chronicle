"""插件配置解析（纯函数，便于单元测试）。

配置来自 ``plugin.toml`` / 用户运行配置里的 ``[chronicle]`` 段。
任何非法值都回落到默认值并在 ``warnings`` 里说明，绝不抛异常——
一个写错的配置项不应该让记录功能整体失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .catalog import QUOTE_FOOTER as _QUOTE_FOOTER
from .catalog import TOPIC_FOOTER as _TOPIC_FOOTER
from .catalog import TOPIC_LEAD as _TOPIC_LEAD
from .diary import MOODS as _DIARY_MOODS

#: 运行配置里允许出现的两个段名，前者优先。
SECTION_NAMES = ("chronicle", "neko_daily_chronicle")

DIARY_ENGINES = ("auto", "local", "llm")
DIARY_STYLES = ("first_person", "third_person", "record")
LLM_PROVIDERS = ("auto", "host", "plugin")
DIARY_SOURCES = ("memor", "transcript", "auto")
MEMO_APPEND_MODES = ("tail", "line", "append")
SCREENING_MODES = ("line", "scene")

#: 猫娘日记的可见性取值（``private`` = 仅自己可见，但猫娘仍读得到）。
CAT_DIARY_VISIBILITIES = ("public", "private")
#: 猫娘日记允许的心情键（与 :data:`chronicle.diary.MOODS` 保持一致）。
CAT_DIARY_MOODS: tuple[str, ...] = ("neutral", *_DIARY_MOODS.keys())


def _parse_bool(value: Any) -> bool | None:
    """解析布尔值；无法识别时返回 None（由调用方记警告并回落默认值）。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "y"):
            return True
        if text in ("0", "false", "no", "off", "n"):
            return False
    return None


def _parse_int(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _parse_float(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _parse_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [item.strip() for item in value.replace("，", ",").split(",")]
        return [item for item in parts if item]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return None


@dataclass
class ChronicleConfig:
    """一次运行的完整配置快照。"""

    # 存档布局（相对于插件 data 目录）
    raw_dir: str = "raw"
    chapters_dir: str = "chapters"
    diary_dir: str = "diary"
    capsule_dir: str = "capsules"
    #: 时间胶囊到期提醒：默认每天指定时刻后检查一次，只把提醒递给猫娘。
    capsule_notify_enabled: bool = True
    capsule_notify_hour: int = 9

    # 章节编排
    scene_gap_minutes: int = 30
    chapter_title_format: str = "第 {index} 章 · {date}"
    include_system: bool = False
    max_turn_chars: int = 4000

    # 日记
    diary_engine: str = "auto"
    diary_style: str = "first_person"
    diary_language: str = "zh-CN"
    diary_title_format: str = "{date} · 日记"
    keyword_top_n: int = 8
    highlight_top_n: int = 5
    llm_max_chars: int = 24000
    llm_timeout_seconds: float = 60.0
    #: 单次模型请求失败后的重试次数上限（含第一次，共尝试这么多次）。
    llm_retry_attempts: int = 3
    llm_temperature: float = 0.4
    diary_source: str = "memor"
    memo_append_mode: str = "tail"

    # 检索是"想起"用的，窗口要够长、返回要够少：默认扫全部已有记录，最多回 8 条。
    # 7 天窗口会让"你之前说过油菜花"这种回忆直接查不到（命中 0 条）。
    search_default_days: int = 365
    search_default_limit: int = 8
    search_snippet_limit: int = 120
    search_include_diary: bool = True
    search_trigger_words: list[str] = field(
        default_factory=lambda: ["我说过", "我提过", "是不是说过", "之前说过"]
    )

    cat_diary_enabled: bool = True
    cat_diary_dir: str = "neko_diary"
    cat_diary_word_limit: int = 500
    #: 猫娘日记没写可见性时的默认值（public/private）。
    cat_diary_default_visibility: str = "public"
    #: 猫娘日记没写心情时的默认值（见 CAT_DIARY_MOODS）。
    cat_diary_default_mood: str = "neutral"
    #: 猫娘标了「私密」的日记是否可读（浏览页 / 插件面板）；猫娘自己始终读得到。
    cat_diary_private_readable: bool = True

    # 放映厅：低频功能，只在打开放映页时读写；关闭时不启动任何后台任务。
    screening_enabled: bool = True
    screening_dir: str = "screenings"
    screening_page_size: int = 60
    screening_default_mode: str = "line"
    #: 自动播放每一句的间隔秒数（由打开着的放映窗口自己计时）。
    screening_auto_interval: float = 2.5
    #: 猫娘在聊天里开口时自动暂停放映。
    screening_auto_pause_on_catgirl: bool = True
    #: 放映厅观后感的字数上限与标题格式。
    screening_review_word_limit: int = 300
    screening_review_title_format: str = "{date} · 放映厅观后感"

    # 拾光 · 对话目录悬浮窗：默认关闭；只有开启并输入 #拾光 才启动 Tk 窗口。
    catalog_enabled: bool = False
    catalog_command: str = "#拾光"
    catalog_send_respond: bool = False
    catalog_search_limit: int = 40
    catalog_width: int = 420
    catalog_height: int = 560
    catalog_ball_size: int = 64
    catalog_always_on_top: bool = True
    #: 搜索最多回看多少天；0 = 全部历史。默认限制，避免冷门词扫全库。
    catalog_max_scan_days: int = 365
    #: 窗口无操作多少分钟后自动缩球；再次闲置同样时间后关闭并释放 Tk 线程。
    catalog_idle_close_minutes: int = 10
    #: 送入对话历史保留条数。
    catalog_history_limit: int = 20
    #: 重新打开拾光时是否恢复上次搜索。
    catalog_restore_last_search: bool = True
    #: 送入聊天时模板的首尾文案；填空字符串表示不要这一段。
    catalog_quote_footer: str = _QUOTE_FOOTER
    catalog_topic_lead: str = _TOPIC_LEAD
    catalog_topic_footer: str = _TOPIC_FOOTER

    # 浏览页「点内容复制」的短语片段：拼成
    #   {copy_phrase_prefix} {date} {copy_phrase_mid} {index} {copy_phrase_index_unit} {copy_phrase_suffix}
    # 例如默认 → "把 2026-10-07 的第 13 条念给我听"。四段都可在配置里自定义。
    copy_phrase_prefix: str = "把"
    copy_phrase_mid: str = "的第"
    copy_phrase_index_unit: str = "条"
    copy_phrase_suffix: str = "念给我听"

    # 直接调用模型的端点（留空表示自动复用宿主配置）
    llm_provider: str = "auto"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # 采集
    capture_all_sources: bool = False
    poll_memory_limit: int = 50
    poll_conversation_limit: int = 200
    # 宿主自己的对话存储（memory/<角色>/time_indexed.db + recent.json）——
    # 这是唯一能同时拿到用户与角色双方对话的来源
    read_host_memory_store: bool = True
    memory_store_limit: int = 200
    memory_store_character: str = ""
    live_refresh_seconds: int = 300
    compile_on_startup: bool = True
    startup_backfill_days: int = 3
    finalize_enabled: bool = True

    # 每日备忘录（抓宿主 recent.json 的 system 备忘录，只追加新增段）
    memo_enabled: bool = True
    # 每日备忘录取自宿主（定时器固定 30 分钟一次）；本字段仅兼容历史配置的读取。
    memo_fetch_seconds: int = 1800
    auto_title_from_memo: bool = True

    # 称呼（写进章节/日记的展示名）
    master_name: str = "主人"
    catgirl_name: str = "猫娘"
    # 日记字数上限（本地第一人称日记按此截断）
    diary_word_limit: int = 400

    # 隐私
    redact: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)

    # ── 构造 ────────────────────────────────────────────────────────

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | None) -> "ChronicleConfig":
        cfg = cls()
        warnings: list[str] = []
        if not isinstance(config, Mapping):
            cfg.warnings = ["配置无法读取（不是映射），全部使用默认值"]
            return cfg

        section: Mapping[str, Any] | None = None
        for name in SECTION_NAMES:
            candidate = config.get(name)
            if isinstance(candidate, Mapping):
                section = candidate
                break
        if section is None:
            cfg.warnings = ["未找到 [chronicle] 配置段，全部使用默认值"]
            return cfg

        def note(key: str, value: Any, fallback: Any) -> None:
            warnings.append(f"{key}={value!r} 非法，已回落到 {fallback!r}")

        def int_field(key: str, current: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
            if key not in section:
                return current
            parsed = _parse_int(section[key], minimum=minimum, maximum=maximum)
            if parsed is None:
                note(key, section[key], current)
                return current
            return parsed

        def float_field(key: str, current: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
            if key not in section:
                return current
            parsed = _parse_float(section[key], minimum=minimum, maximum=maximum)
            if parsed is None:
                note(key, section[key], current)
                return current
            return parsed

        def bool_field(key: str, current: bool) -> bool:
            if key not in section:
                return current
            parsed = _parse_bool(section[key])
            if parsed is None:
                note(key, section[key], current)
                return current
            return parsed

        def str_field(key: str, current: str) -> str:
            if key not in section:
                return current
            parsed = _parse_str(section[key])
            if parsed is None:
                note(key, section[key], current)
                return current
            return parsed

        def blankable_field(key: str, current: str) -> str:
            """允许显式写成空串的文本项（表示"把这段模板文案关掉"）。"""

            if key not in section:
                return current
            return _parse_str(section[key]) or ""

        def enum_field(key: str, current: str, allowed: tuple[str, ...]) -> str:
            if key not in section:
                return current
            parsed = _parse_str(section[key])
            if parsed is None:
                return current
            if parsed not in allowed:
                note(key, parsed, current)
                return current
            return parsed

        def list_field(key: str, current: list[str]) -> list[str]:
            if key not in section:
                return current
            parsed = _parse_str_list(section[key])
            if parsed is None:
                note(key, section[key], current)
                return current
            return parsed

        # 字符串类
        cfg.raw_dir = str_field("raw_dir", cfg.raw_dir)
        cfg.chapters_dir = str_field("chapters_dir", cfg.chapters_dir)
        cfg.diary_dir = str_field("diary_dir", cfg.diary_dir)
        cfg.capsule_dir = str_field("capsule_dir", cfg.capsule_dir)
        cfg.screening_dir = str_field("screening_dir", cfg.screening_dir)
        cfg.screening_review_title_format = str_field(
            "screening_review_title_format", cfg.screening_review_title_format
        )
        cfg.catalog_command = str_field("catalog_command", cfg.catalog_command) or "#拾光"
        cfg.catalog_quote_footer = blankable_field(
            "catalog_quote_footer", cfg.catalog_quote_footer
        )
        cfg.catalog_topic_lead = blankable_field("catalog_topic_lead", cfg.catalog_topic_lead)
        cfg.catalog_topic_footer = blankable_field(
            "catalog_topic_footer", cfg.catalog_topic_footer
        )
        cfg.chapter_title_format = str_field("chapter_title_format", cfg.chapter_title_format)
        cfg.diary_title_format = str_field("diary_title_format", cfg.diary_title_format)
        cfg.diary_language = str_field("diary_language", cfg.diary_language)
        cfg.master_name = str_field("master_name", cfg.master_name)
        cfg.catgirl_name = str_field("catgirl_name", cfg.catgirl_name)
        cfg.memory_store_character = str_field("memory_store_character", cfg.memory_store_character)
        cfg.memo_enabled = bool_field("memo_enabled", cfg.memo_enabled)
        cfg.memo_fetch_seconds = int_field(
            "memo_fetch_seconds", cfg.memo_fetch_seconds, minimum=60, maximum=86400
        )
        cfg.auto_title_from_memo = bool_field("auto_title_from_memo", cfg.auto_title_from_memo)
        cfg.llm_base_url = str_field("llm_base_url", "")
        cfg.llm_model = str_field("llm_model", "")
        cfg.llm_api_key = str_field("llm_api_key", "")

        # 枚举类
        cfg.diary_engine = enum_field("diary_engine", cfg.diary_engine, DIARY_ENGINES)
        cfg.diary_style = enum_field("diary_style", cfg.diary_style, DIARY_STYLES)
        cfg.llm_provider = enum_field("llm_provider", cfg.llm_provider, LLM_PROVIDERS)
        cfg.diary_source = enum_field("diary_source", cfg.diary_source, DIARY_SOURCES)
        cfg.memo_append_mode = enum_field(
            "memo_append_mode", cfg.memo_append_mode, MEMO_APPEND_MODES
        )
        cfg.cat_diary_default_visibility = enum_field(
            "cat_diary_default_visibility",
            cfg.cat_diary_default_visibility,
            CAT_DIARY_VISIBILITIES,
        )
        cfg.cat_diary_default_mood = enum_field(
            "cat_diary_default_mood", cfg.cat_diary_default_mood, CAT_DIARY_MOODS
        )
        cfg.screening_default_mode = enum_field(
            "screening_default_mode", cfg.screening_default_mode, SCREENING_MODES
        )

        # 数值类
        cfg.scene_gap_minutes = int_field(
            "scene_gap_minutes", cfg.scene_gap_minutes, minimum=0, maximum=1440
        )
        cfg.max_turn_chars = int_field(
            "max_turn_chars", cfg.max_turn_chars, minimum=100, maximum=200000
        )
        cfg.keyword_top_n = int_field("keyword_top_n", cfg.keyword_top_n, minimum=0, maximum=50)
        cfg.highlight_top_n = int_field("highlight_top_n", cfg.highlight_top_n, minimum=0, maximum=50)
        cfg.llm_max_chars = int_field(
            "llm_max_chars", cfg.llm_max_chars, minimum=1000, maximum=400000
        )
        cfg.llm_temperature = float_field(
            "llm_temperature", cfg.llm_temperature, minimum=0.0, maximum=2.0
        )
        cfg.llm_timeout_seconds = float_field(
            "llm_timeout_seconds", cfg.llm_timeout_seconds, minimum=1.0, maximum=600.0
        )
        cfg.llm_retry_attempts = int_field(
            "llm_retry_attempts", cfg.llm_retry_attempts, minimum=1, maximum=5
        )
        cfg.poll_memory_limit = int_field(
            "poll_memory_limit", cfg.poll_memory_limit, minimum=1, maximum=500
        )
        cfg.poll_conversation_limit = int_field(
            "poll_conversation_limit", cfg.poll_conversation_limit, minimum=1, maximum=2000
        )
        cfg.memory_store_limit = int_field(
            "memory_store_limit", cfg.memory_store_limit, minimum=1, maximum=5000
        )
        cfg.diary_word_limit = int_field(
            "diary_word_limit", cfg.diary_word_limit, minimum=80, maximum=2000
        )
        cfg.live_refresh_seconds = int_field(
            "live_refresh_seconds", cfg.live_refresh_seconds, minimum=30, maximum=86400
        )
        cfg.startup_backfill_days = int_field(
            "startup_backfill_days", cfg.startup_backfill_days, minimum=0, maximum=90
        )
        cfg.search_default_days = int_field(
            "search_default_days", cfg.search_default_days, minimum=1, maximum=3650
        )
        cfg.search_default_limit = int_field(
            "search_default_limit", cfg.search_default_limit, minimum=1, maximum=200
        )
        cfg.search_snippet_limit = int_field(
            "search_snippet_limit", cfg.search_snippet_limit, minimum=20, maximum=2000
        )
        cfg.search_trigger_words = list_field("search_trigger_words", cfg.search_trigger_words)
        cfg.cat_diary_dir = str_field("cat_diary_dir", cfg.cat_diary_dir)
        cfg.cat_diary_word_limit = int_field(
            "cat_diary_word_limit", cfg.cat_diary_word_limit, minimum=50, maximum=5000
        )
        cfg.screening_page_size = int_field(
            "screening_page_size", cfg.screening_page_size, minimum=10, maximum=200
        )
        cfg.screening_auto_interval = float_field(
            "screening_auto_interval",
            cfg.screening_auto_interval,
            minimum=1.0,
            maximum=10.0,
        )
        cfg.screening_review_word_limit = int_field(
            "screening_review_word_limit",
            cfg.screening_review_word_limit,
            minimum=80,
            maximum=2000,
        )
        cfg.catalog_search_limit = int_field(
            "catalog_search_limit", cfg.catalog_search_limit, minimum=10, maximum=100
        )
        cfg.catalog_width = int_field(
            "catalog_width", cfg.catalog_width, minimum=340, maximum=900
        )
        cfg.catalog_height = int_field(
            "catalog_height", cfg.catalog_height, minimum=420, maximum=900
        )
        cfg.catalog_ball_size = int_field(
            "catalog_ball_size", cfg.catalog_ball_size, minimum=48, maximum=96
        )
        cfg.catalog_max_scan_days = int_field(
            "catalog_max_scan_days", cfg.catalog_max_scan_days, minimum=0, maximum=5000
        )
        cfg.catalog_idle_close_minutes = int_field(
            "catalog_idle_close_minutes",
            cfg.catalog_idle_close_minutes,
            minimum=0,
            maximum=240,
        )
        cfg.catalog_history_limit = int_field(
            "catalog_history_limit", cfg.catalog_history_limit, minimum=5, maximum=100
        )
        cfg.capsule_notify_hour = int_field(
            "capsule_notify_hour", cfg.capsule_notify_hour, minimum=0, maximum=23
        )
        cfg.copy_phrase_prefix = str_field("copy_phrase_prefix", cfg.copy_phrase_prefix)
        cfg.copy_phrase_mid = str_field("copy_phrase_mid", cfg.copy_phrase_mid)
        cfg.copy_phrase_index_unit = str_field("copy_phrase_index_unit", cfg.copy_phrase_index_unit)
        cfg.copy_phrase_suffix = str_field("copy_phrase_suffix", cfg.copy_phrase_suffix)

        # 布尔类
        cfg.include_system = bool_field("include_system", cfg.include_system)
        cfg.capture_all_sources = bool_field("capture_all_sources", cfg.capture_all_sources)
        cfg.read_host_memory_store = bool_field(
            "read_host_memory_store", cfg.read_host_memory_store
        )
        cfg.compile_on_startup = bool_field("compile_on_startup", cfg.compile_on_startup)
        cfg.finalize_enabled = bool_field("finalize_enabled", cfg.finalize_enabled)
        cfg.search_include_diary = bool_field("search_include_diary", cfg.search_include_diary)
        cfg.cat_diary_enabled = bool_field("cat_diary_enabled", cfg.cat_diary_enabled)
        cfg.capsule_notify_enabled = bool_field(
            "capsule_notify_enabled", cfg.capsule_notify_enabled
        )
        cfg.cat_diary_private_readable = bool_field(
            "cat_diary_private_readable", cfg.cat_diary_private_readable
        )
        cfg.screening_enabled = bool_field("screening_enabled", cfg.screening_enabled)
        cfg.screening_auto_pause_on_catgirl = bool_field(
            "screening_auto_pause_on_catgirl", cfg.screening_auto_pause_on_catgirl
        )
        cfg.catalog_enabled = bool_field("catalog_enabled", cfg.catalog_enabled)
        cfg.catalog_send_respond = bool_field(
            "catalog_send_respond", cfg.catalog_send_respond
        )
        cfg.catalog_always_on_top = bool_field(
            "catalog_always_on_top", cfg.catalog_always_on_top
        )
        cfg.catalog_restore_last_search = bool_field(
            "catalog_restore_last_search", cfg.catalog_restore_last_search
        )

        # 列表类
        cfg.redact = list_field("redact", cfg.redact)

        cfg.warnings = warnings
        return cfg

    # ── 派生 ────────────────────────────────────────────────────────

    def llm_configured(self) -> bool:
        """用户是否在插件配置里显式填了可直连的端点。"""

        return bool(self.llm_base_url and self.llm_model)

    def as_public_dict(self) -> dict:
        """给 UI 用的安全快照（不含 api_key，只报告是否已设置）。"""

        return {
            "raw_dir": self.raw_dir,
            "chapters_dir": self.chapters_dir,
            "diary_dir": self.diary_dir,
            "capsule_dir": self.capsule_dir,
            "capsule_notify_enabled": self.capsule_notify_enabled,
            "capsule_notify_hour": self.capsule_notify_hour,
            "scene_gap_minutes": self.scene_gap_minutes,
            "chapter_title_format": self.chapter_title_format,
            "include_system": self.include_system,
            "max_turn_chars": self.max_turn_chars,
            "diary_engine": self.diary_engine,
            "diary_style": self.diary_style,
            "diary_language": self.diary_language,
            "diary_title_format": self.diary_title_format,
            "keyword_top_n": self.keyword_top_n,
            "highlight_top_n": self.highlight_top_n,
            "llm_max_chars": self.llm_max_chars,
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "llm_retry_attempts": self.llm_retry_attempts,
            "llm_temperature": self.llm_temperature,
            "capture_all_sources": self.capture_all_sources,
            "poll_memory_limit": self.poll_memory_limit,
            "poll_conversation_limit": self.poll_conversation_limit,
            "live_refresh_seconds": self.live_refresh_seconds,
            "startup_backfill_days": self.startup_backfill_days,
            "compile_on_startup": self.compile_on_startup,
            "redact": list(self.redact),
            "read_host_memory_store": self.read_host_memory_store,
            "memory_store_limit": self.memory_store_limit,
            "memory_store_character": self.memory_store_character,
            "finalize_enabled": self.finalize_enabled,
            "master_name": self.master_name,
            "catgirl_name": self.catgirl_name,
            "diary_word_limit": self.diary_word_limit,
            "memo_enabled": self.memo_enabled,
            "memo_fetch_seconds": self.memo_fetch_seconds,
            "auto_title_from_memo": self.auto_title_from_memo,
            "memo_append_mode": self.memo_append_mode,
            "diary_source": self.diary_source,
            "search_default_days": self.search_default_days,
            "search_default_limit": self.search_default_limit,
            "search_snippet_limit": self.search_snippet_limit,
            "search_include_diary": self.search_include_diary,
            "search_trigger_words": list(self.search_trigger_words),
            "cat_diary_enabled": self.cat_diary_enabled,
            "cat_diary_dir": self.cat_diary_dir,
            "cat_diary_word_limit": self.cat_diary_word_limit,
            "cat_diary_default_visibility": self.cat_diary_default_visibility,
            "cat_diary_default_mood": self.cat_diary_default_mood,
            "cat_diary_private_readable": self.cat_diary_private_readable,
            "screening_enabled": self.screening_enabled,
            "screening_dir": self.screening_dir,
            "screening_page_size": self.screening_page_size,
            "screening_default_mode": self.screening_default_mode,
            "screening_auto_interval": self.screening_auto_interval,
            "screening_auto_pause_on_catgirl": self.screening_auto_pause_on_catgirl,
            "screening_review_word_limit": self.screening_review_word_limit,
            "screening_review_title_format": self.screening_review_title_format,
            "catalog_enabled": self.catalog_enabled,
            "catalog_command": self.catalog_command,
            "catalog_send_respond": self.catalog_send_respond,
            "catalog_search_limit": self.catalog_search_limit,
            "catalog_width": self.catalog_width,
            "catalog_height": self.catalog_height,
            "catalog_ball_size": self.catalog_ball_size,
            "catalog_always_on_top": self.catalog_always_on_top,
            "catalog_max_scan_days": self.catalog_max_scan_days,
            "catalog_idle_close_minutes": self.catalog_idle_close_minutes,
            "catalog_history_limit": self.catalog_history_limit,
            "catalog_restore_last_search": self.catalog_restore_last_search,
            "catalog_quote_footer": self.catalog_quote_footer,
            "catalog_topic_lead": self.catalog_topic_lead,
            "catalog_topic_footer": self.catalog_topic_footer,
            "copy_phrase_prefix": self.copy_phrase_prefix,
            "copy_phrase_mid": self.copy_phrase_mid,
            "copy_phrase_index_unit": self.copy_phrase_index_unit,
            "copy_phrase_suffix": self.copy_phrase_suffix,
            "llm_provider": self.llm_provider,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_api_key_set": bool(self.llm_api_key),
            "warnings": list(self.warnings),
        }
