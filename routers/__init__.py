"""按功能域拆分的入口路由。

每个 Router 只负责一组 `@plugin_entry`（以及配套的 `@ui.action`），
真正的状态与内部实现仍留在主插件类上，路由通过 ``self.main_plugin`` 访问。
"""

from __future__ import annotations

from .capsule import CapsuleRouter
from .cat_diary import CatDiaryRouter
from .chronicle import ChronicleRouter
from .config import ConfigRouter
from .images import ImagesRouter
from .screening import ScreeningRouter
from .search import SearchRouter

__all__ = [
    "CapsuleRouter",
    "CatDiaryRouter",
    "ChronicleRouter",
    "ConfigRouter",
    "ImagesRouter",
    "ScreeningRouter",
    "SearchRouter",
]
