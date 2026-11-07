"""拾光 · 对话目录悬浮窗（Tk 原生实现）。

只在配置开启且用户输入 ``#拾光`` 时才会导入并启动；窗口关闭后 Tk 线程结束，
不保留定时器或后台轮询。所有 Tk 操作都在同一个线程里，外部只通过队列投递命令。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from . import catalog as catalog_mod

BG = "#1d1218"
PANEL = "#2b1a22"
TITLE_BG = "#3a202b"
TEXT = "#fce9ef"
MUTED = "#c8a3b3"
ACCENT = "#ff8db4"
ACCENT_DARK = "#d4638a"
LINE = "#6d4457"
USER = "#b9dcff"
CAT = "#ffd7e5"
KEY_COLOR = "#ff00ff"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_BALL = ("Microsoft YaHei UI", 14, "bold")

#: 打包后的宿主常只带 tcl86t.dll / tk86t.dll，没带 Tcl 脚本库，
#: 这时 ``tk.Tk()`` 会因找不到 init.tcl 直接失败。插件自带一份 8.6 的库，
#: 只在默认探测失败时兜底，不覆盖宿主自带的 Tk。
VENDOR_TCL_DIR = Path(__file__).resolve().parent.parent / "vendor" / "tcl"

#: 等 Tk 完成初始化的上限。宿主是冻结打包，首次加载 Tcl 会比系统 Python 慢，
#: 4 秒会误判成超时；这里放宽到 15 秒，真正卡死时也不会无限等。
READY_TIMEOUT = 15.0


def _vendor_tk_dirs() -> tuple[Path, Path] | None:
    tcl_dir = VENDOR_TCL_DIR / "tcl8.6"
    tk_dir = VENDOR_TCL_DIR / "tk8.6"
    if (tcl_dir / "init.tcl").is_file() and (tk_dir / "tk.tcl").is_file():
        return tcl_dir, tk_dir
    return None


def _create_root() -> tk.Tk:
    """创建 Tk 根窗口。

    宿主打包时常常只带 tcl86t.dll / tk86t.dll，没带 Tcl 脚本库（实测 exe 里没有
    tcl8.6 数据），所以有自带库时先用它指路，避免先失败再重试、把初始化时间翻倍。
    """

    dirs = _vendor_tk_dirs()
    if dirs is not None:
        tcl_dir, tk_dir = dirs
        # 宿主没设、或设了个没有 init.tcl 的坏路径，就换成自带的那份。
        current = os.environ.get("TCL_LIBRARY", "")
        if not current or not (Path(current) / "init.tcl").is_file():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            os.environ["TK_LIBRARY"] = str(tk_dir)
    return tk.Tk()


class CatalogWindow:
    """单例悬浮目录窗。外部线程只调用 ``open`` / ``focus`` / ``close``。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._open_lock = threading.Lock()
        self._ready = threading.Event()
        self._alive = False
        self._closing = False
        self._last_error = ""
        self._initial_query = ""
        self._state_path = self._resolve_state_path()
        self._state = self._load_state()

        # Tk 线程内使用的字段
        self._root: tk.Tk | None = None
        self._expanded: tk.Toplevel | None = None
        self._ball: tk.Toplevel | None = None
        self._ball_canvas: tk.Canvas | None = None
        self._ball_transparent = True
        self._search_var: tk.StringVar | None = None
        self._search_entry: tk.Entry | None = None
        self._date_var: tk.StringVar | None = None
        self._speaker_var: tk.StringVar | None = None
        self._template_var: tk.StringVar | None = None
        self._date_entry: tk.Entry | None = None
        self._speaker_box: ttk.Combobox | None = None
        self._template_box: ttk.Combobox | None = None
        self._pin_label: tk.Label | None = None
        self._more_button: tk.Button | None = None
        self._history_button: tk.Button | None = None
        self._show_dates_var: tk.BooleanVar | None = None
        self._hscroll: ttk.Scrollbar | None = None
        self._ball_badge_id: int | None = None
        self._ball_badge_bg_id: int | None = None
        self._badge_count = 0
        self._ball_menu: tk.Menu | None = None
        self._pinned = bool(getattr(self.plugin.cfg, "catalog_always_on_top", True))
        self._collapsed = False
        self._idle_after: str | None = None
        self._search_running = False
        self._pending_search: tuple | None = None
        self._search_limit = max(10, min(100, int(getattr(self.plugin.cfg, "catalog_search_limit", 40) or 40)))
        self._show_dates = self._load_show_dates()
        self._last_query = ""
        self._last_day = ""
        self._last_speaker = "全部"
        self._last_template = "引用"
        self._current_query = ""
        self._history_window: tk.Toplevel | None = None
        self._history_list: tk.Listbox | None = None
        self._history_items: list[dict] = []
        self._tree: ttk.Treeview | None = None
        self._preview: tk.Text | None = None
        self._status: tk.Label | None = None
        self._back: tk.Label | None = None
        self._send_button: tk.Button | None = None
        self._respond_var: tk.BooleanVar | None = None
        self._rows_by_iid: dict[str, dict] = {}
        self._mode = "days"
        self._current_rows: list[dict] = []
        self._query_gen = 0
        self._search_after: str | None = None
        self._drag_offset = (0, 0)
        self._ball_moved = False
        self._ball_start = (0, 0)
        self._resize_origin = (0, 0, 0, 0)

    # ── 外部线程 API ───────────────────────────────────────────────

    @property
    def last_error(self) -> str:
        return self._last_error

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and self._alive)

    def open(self, initial_query: str = "") -> bool:
        with self._open_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                self._queue.put(("show", initial_query))
                # 等它完成初始化；如果它已经就绪，这里会立刻返回。
                self._ready.wait(timeout=READY_TIMEOUT)
                return bool(self._alive and not self._last_error)
            self._ready.clear()
            self._last_error = ""
            self._initial_query = str(initial_query or "")
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="neko-catalog-window"
            )
            self._thread.start()
        # 等 Tk 完成初始化，这样调用方可以明确知道是否成功。
        self._ready.wait(timeout=READY_TIMEOUT)
        if not self._ready.is_set():
            self._last_error = self._last_error or "Tk 初始化超时"
            return False
        return bool(self._alive and not self._last_error)

    def focus(self, query: str = "") -> bool:
        if not self.is_alive():
            return self.open(query)
        self._queue.put(("show", str(query or "")))
        return True

    def close(self, timeout: float = 1.5) -> None:
        thread = self._thread
        if not thread:
            return
        self._queue.put(("close",))
        thread.join(timeout=max(0.1, float(timeout)))
        if not thread.is_alive():
            self._thread = None
            self._alive = False

    # ── 状态存取 ───────────────────────────────────────────────────

    def _resolve_state_path(self) -> Path | None:
        try:
            root = Path(self.plugin.store.root)
            return root / "catalog_ui.json"
        except Exception:  # noqa: BLE001
            return None

    def _load_state(self) -> dict:
        if self._state_path is None:
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _load_show_dates(self) -> bool:
        """上次是否勾选「显示日期」；默认不勾选，列表只看内容。"""

        saved = self._state.get("show_dates")
        return saved if isinstance(saved, bool) else False

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state["saved_at"] = round(time.time(), 3)
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 位置保存失败不影响窗口
            self._log("保存拾光窗口位置失败：{}", exc)

    # ── Tk 线程入口 ─────────────────────────────────────────────────

    def _run(self) -> None:
        started = time.time()
        try:
            root = _create_root()
        except Exception as exc:  # noqa: BLE001 - 没有 Tk 运行时
            self._last_error = f"tk_unavailable: {exc}"
            self._log("拾光窗口无法启动：{}", exc)
            self._ready.set()
            return
        created = time.time()
        self._root = root
        root.withdraw()
        self._info("拾光 Tk 根窗口已创建：{:.2f}s", created - started)
        try:
            self._build()
            self._alive = True
            self._ready.set()
            self._info(
                "拾光窗口就绪：Tk 初始化 {:.2f}s，界面构建 {:.2f}s",
                created - started,
                time.time() - created,
            )
            root.after(100, self._poll)
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 - UI 构建失败
            self._last_error = f"tk_build_failed: {exc}"
            self._log("拾光窗口构建失败：{}", exc)
            self._ready.set()
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._alive = False
            self._ready.set()
            self._clear_tk_refs()

    def _clear_tk_refs(self) -> None:
        """在 Tk 线程里清空所有 Tk 对象引用。

        Tkinter 的 Variable/Toplevel 不能在其它线程被垃圾回收，否则会在解释器退出时
        触发 ``Tcl_AsyncDelete``。窗口销毁后必须在本线程把这些引用全部放掉。
        """

        self._root = None
        self._expanded = None
        self._ball = None
        self._ball_canvas = None
        self._search_var = None
        self._search_entry = None
        self._date_var = None
        self._speaker_var = None
        self._template_var = None
        self._date_entry = None
        self._speaker_box = None
        self._template_box = None
        self._pin_label = None
        self._more_button = None
        self._history_button = None
        self._show_dates_var = None
        self._hscroll = None
        self._ball_badge_id = None
        self._ball_badge_bg_id = None
        self._ball_menu = None
        self._tree = None
        self._preview = None
        self._status = None
        self._back = None
        self._send_button = None
        self._respond_var = None
        self._history_window = None
        self._history_list = None
        self._idle_after = None
        self._rows_by_iid = {}
        self._current_rows = []

    def _log(self, message: str, *args: Any) -> None:
        try:
            self.plugin.logger.warning(message, *args)
        except Exception:  # noqa: BLE001
            pass

    def _info(self, message: str, *args: Any) -> None:
        try:
            self.plugin.logger.info(message, *args)
        except Exception:  # noqa: BLE001
            pass

    # ── UI 构建 ─────────────────────────────────────────────────────

    def _clamp_position(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        root = self._root
        if root is None:
            return max(0, x), max(0, y)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = max(0, min(int(x), max(0, sw - width)))
        y = max(0, min(int(y), max(0, sh - height)))
        return x, y

    def _expanded_geometry(self) -> tuple[int, int, int, int]:
        cfg = self.plugin.cfg
        width = max(340, int(getattr(cfg, "catalog_width", 420) or 420))
        height = max(420, int(getattr(cfg, "catalog_height", 560) or 560))
        saved = self._state.get("expanded") if isinstance(self._state.get("expanded"), dict) else {}
        width = max(340, int(saved.get("width") or width))
        height = max(420, int(saved.get("height") or height))
        screen_w = self._root.winfo_screenwidth() if self._root else 1920
        screen_h = self._root.winfo_screenheight() if self._root else 1080
        x = int(saved.get("x") if saved.get("x") is not None else screen_w - width - 48)
        y = int(saved.get("y") if saved.get("y") is not None else 80)
        x, y = self._clamp_position(x, y, width, height)
        return x, y, width, height

    def _ball_geometry(self) -> tuple[int, int, int]:
        cfg = self.plugin.cfg
        size = max(48, min(96, int(getattr(cfg, "catalog_ball_size", 64) or 64)))
        saved = self._state.get("ball") if isinstance(self._state.get("ball"), dict) else {}
        if saved.get("size"):
            size = max(48, min(96, int(saved.get("size") or size)))
        screen_w = self._root.winfo_screenwidth() if self._root else 1920
        screen_h = self._root.winfo_screenheight() if self._root else 1080
        x = int(saved.get("x") if saved.get("x") is not None else screen_w - size - 24)
        y = int(saved.get("y") if saved.get("y") is not None else screen_h - size - 80)
        x, y = self._clamp_position(x, y, size, size)
        return x, y, size

    def _build(self) -> None:
        self._pinned = bool(
            self._state.get("pinned", getattr(self.plugin.cfg, "catalog_always_on_top", True))
        )
        topmost = self._pinned
        self._expanded = tk.Toplevel(self._root)
        self._expanded.withdraw()
        self._expanded.overrideredirect(True)
        self._expanded.configure(bg=BG)
        try:
            self._expanded.attributes("-topmost", topmost)
        except tk.TclError:
            pass

        self._ball = tk.Toplevel(self._root)
        self._ball.withdraw()
        self._ball.overrideredirect(True)
        self._ball.configure(bg=KEY_COLOR)
        try:
            self._ball.attributes("-topmost", topmost)
        except tk.TclError:
            pass
        self._ball_transparent = True
        try:
            self._ball.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            # 非 Windows 或 Tk 不支持时，球会落在一个深色方形背景里。
            self._ball_transparent = False
            self._ball.configure(bg=BG)
        self._build_ball()

        self._build_expanded()
        self._expanded.bind_all("<Escape>", lambda _event: self._collapse())
        self._expanded.bind_all("<Any-KeyPress>", lambda _event: self._touch())
        self._expanded.bind_all("<Button-1>", lambda _event: self._touch())
        self._show_expanded()
        if self._initial_query:
            self._set_query(self._initial_query)
        elif (
            (self._search_var and self._search_var.get().strip())
            or (self._date_var and self._date_var.get().strip())
        ):
            self._start_search_now()
        else:
            self._start_days()

    def _build_expanded(self) -> None:
        window = self._expanded
        if window is None:
            return
        x, y, width, height = self._expanded_geometry()
        window.geometry(f"{width}x{height}+{x}+{y}")

        main = tk.Frame(window, bg=BG)
        main.pack(fill="both", expand=True)

        # 底部操作栏先占住底边再排其它控件，否则列表/预览撑满后「送入对话」会被
        # 挤出窗口（pack 按顺序分配空间，后排的控件先被挤到 0 高）。
        bottom = tk.Frame(main, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=12, pady=(2, 10))
        self._status = tk.Label(
            bottom, text="准备就绪", bg=BG, fg=MUTED, font=FONT_SMALL, anchor="w"
        )
        self._status.pack(side="left", fill="x", expand=True)
        self._respond_var = tk.BooleanVar(
            value=bool(getattr(self.plugin.cfg, "catalog_send_respond", False))
        )
        check = tk.Checkbutton(
            bottom,
            text="让她回应",
            variable=self._respond_var,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=TEXT,
            selectcolor=PANEL,
            font=FONT_SMALL,
        )
        check.pack(side="left", padx=(0, 8))
        self._send_button = tk.Button(
            bottom,
            text="送入对话 ▶",
            command=self._send,
            bg=ACCENT,
            fg="#ffffff",
            activebackground=ACCENT_DARK,
            activeforeground="#ffffff",
            relief="flat",
            font=FONT_UI,
            padx=12,
            pady=4,
            cursor="hand2",
        )
        self._send_button.pack(side="right", padx=(0, 16))

        titlebar = tk.Frame(main, bg=TITLE_BG, height=36)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        title = tk.Label(
            titlebar,
            text="🗂 拾光 · 对话目录",
            bg=TITLE_BG,
            fg=TEXT,
            font=FONT_TITLE,
            anchor="w",
            padx=10,
        )
        title.pack(side="left", fill="y")
        close_btn = tk.Label(
            titlebar, text="✕", bg=TITLE_BG, fg=MUTED, font=FONT_UI, padx=10, cursor="hand2"
        )
        close_btn.pack(side="right", fill="y")
        close_btn.bind("<Button-1>", lambda _event: self._close())
        mini_btn = tk.Label(
            titlebar, text="—", bg=TITLE_BG, fg=MUTED, font=FONT_UI, padx=10, cursor="hand2"
        )
        mini_btn.pack(side="right", fill="y")
        mini_btn.bind("<Button-1>", lambda _event: self._collapse())
        self._pin_label = tk.Label(
            titlebar,
            text="📌" if self._pinned else "📍",
            bg=TITLE_BG,
            fg=MUTED,
            font=FONT_SMALL,
            padx=6,
            cursor="hand2",
        )
        self._pin_label.pack(side="right", fill="y")
        self._pin_label.bind("<Button-1>", lambda _event: self._toggle_pin())
        for widget in (titlebar, title):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        restore = bool(getattr(self.plugin.cfg, "catalog_restore_last_search", True))
        saved_query = str(self._state.get("last_query") or "") if restore else ""
        saved_day = str(self._state.get("last_day") or "") if restore else ""
        saved_speaker = str(self._state.get("last_speaker") or "全部") if restore else "全部"
        if saved_day and not self._valid_day(saved_day):
            saved_day = ""
        self._search_var = tk.StringVar(value=self._initial_query or saved_query)
        self._date_var = tk.StringVar(value=saved_day)
        self._speaker_var = tk.StringVar(value=saved_speaker)
        self._template_var = tk.StringVar(value=str(self._state.get("last_template") or "引用"))

        search_box = tk.Frame(main, bg=BG)
        search_box.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(search_box, text="🔍", bg=BG, fg=ACCENT, font=FONT_UI).pack(side="left")
        entry = tk.Entry(
            search_box,
            textvariable=self._search_var,
            bg=PANEL,
            fg=TEXT,
            insertbackground=ACCENT,
            relief="flat",
            font=FONT_UI,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
        )
        entry.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=5)
        entry.bind("<KeyRelease>", self._on_query_change)
        entry.bind("<Return>", lambda _event: self._start_search_now())
        entry.bind("<KP_Enter>", lambda _event: self._start_search_now())
        self._search_entry = entry

        hint = tk.Label(
            main,
            text="输入关键词搜索；留空看日期目录。↑↓ 选择，Enter 打开，Ctrl+F 搜索。",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
        )
        hint.pack(fill="x", padx=14, pady=(0, 4))

        filters = tk.Frame(main, bg=BG)
        filters.pack(fill="x", padx=12, pady=(0, 5))
        tk.Label(filters, text="日期", bg=BG, fg=MUTED, font=FONT_SMALL).pack(side="left")
        self._date_entry = tk.Entry(
            filters,
            textvariable=self._date_var,
            bg=PANEL,
            fg=TEXT,
            insertbackground=ACCENT,
            relief="flat",
            font=FONT_SMALL,
            width=10,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
        )
        self._date_entry.pack(side="left", padx=(4, 10), ipady=3)
        self._date_entry.bind("<KeyRelease>", self._on_query_change)
        self._date_entry.bind("<Return>", lambda _event: self._start_search_now())
        tk.Label(filters, text="说话人", bg=BG, fg=MUTED, font=FONT_SMALL).pack(side="left")
        master_name = str(getattr(self.plugin.cfg, "master_name", "主人") or "主人")
        catgirl_name = str(getattr(self.plugin.cfg, "catgirl_name", "猫娘") or "猫娘")
        if self._speaker_var.get() not in ("全部", master_name, catgirl_name):
            self._speaker_var.set("全部")
        self._speaker_box = ttk.Combobox(
            filters,
            textvariable=self._speaker_var,
            values=("全部", master_name, catgirl_name),
            state="readonly",
            width=5,
            font=FONT_SMALL,
        )
        self._speaker_box.pack(side="left", padx=(4, 10))
        self._speaker_box.bind("<<ComboboxSelected>>", lambda _event: self._on_query_change(None))
        tk.Label(filters, text="模板", bg=BG, fg=MUTED, font=FONT_SMALL).pack(side="left")
        self._template_box = ttk.Combobox(
            filters,
            textvariable=self._template_var,
            values=("引用", "话题"),
            state="readonly",
            width=4,
            font=FONT_SMALL,
        )
        self._template_box.pack(side="left", padx=(4, 0))
        self._template_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())

        nav = tk.Frame(main, bg=BG)
        nav.pack(fill="x", padx=12, pady=(0, 4))
        self._back = tk.Label(
            nav,
            text="← 返回日期目录",
            bg=BG,
            fg=ACCENT,
            font=FONT_SMALL,
            cursor="hand2",
        )
        self._back.pack(side="left")
        self._back.bind("<Button-1>", lambda _event: self._back_to_days())
        self._back.pack_forget()

        def small_button(parent, text, command):
            return tk.Button(
                parent,
                text=text,
                command=command,
                bg=BG,
                fg=ACCENT,
                activebackground=BG,
                activeforeground=TEXT,
                relief="flat",
                font=FONT_SMALL,
                padx=6,
                pady=0,
                cursor="hand2",
            )

        self._history_button = small_button(nav, "最近", self._show_history)
        self._history_button.pack(side="right")
        self._more_button = small_button(nav, "更多", lambda: self._more_results())
        self._more_button.pack(side="right", padx=(0, 6))
        self._more_button.pack_forget()
        self._show_dates_var = tk.BooleanVar(value=self._show_dates)
        dates_box = tk.Checkbutton(
            nav,
            text="显示日期",
            variable=self._show_dates_var,
            command=self._toggle_show_dates,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=TEXT,
            selectcolor=PANEL,
            font=FONT_SMALL,
            padx=4,
            pady=0,
            cursor="hand2",
        )
        dates_box.pack(side="right", padx=(0, 6))

        tree_box = tk.Frame(main, bg=BG)
        tree_box.pack(fill="both", expand=True, padx=12)
        style = ttk.Style(window)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Catalog.Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=27,
            borderwidth=0,
            font=FONT_SMALL,
        )
        style.map(
            "Catalog.Treeview",
            background=[("selected", ACCENT_DARK)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Catalog.Treeview.Heading",
            background=TITLE_BG,
            foreground=ACCENT,
            relief="flat",
            font=FONT_SMALL,
        )
        self._tree = ttk.Treeview(
            tree_box,
            columns=("date", "time", "who", "text"),
            show="headings",
            selectmode="extended",
            style="Catalog.Treeview",
            height=10,
        )
        self._tree.heading("date", text="日期", anchor="w")
        self._tree.heading("time", text="时间", anchor="w")
        self._tree.heading("who", text="说话人", anchor="w")
        self._tree.heading("text", text="内容", anchor="w")
        self._tree.column("date", width=86, minwidth=70, stretch=False)
        self._tree.column("time", width=48, minwidth=44, stretch=False)
        self._tree.column("who", width=58, minwidth=50, stretch=False)
        self._tree.column("text", width=230, minwidth=120, stretch=True)
        self._tree.tag_configure("user", foreground=USER)
        self._tree.tag_configure("assistant", foreground=CAT)
        self._tree.tag_configure("day", foreground=ACCENT)
        self._tree.tag_configure("hit", background="#3a202b")
        self._tree.tag_configure("muted", foreground=MUTED)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Button-1>", self._on_tree_click)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Return>", self._on_double_click)
        self._hscroll = ttk.Scrollbar(tree_box, orient="horizontal", command=self._tree.xview)
        self._hscroll.pack(side="bottom", fill="x")
        scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self._tree.yview)
        scroll.pack(side="right", fill="y")
        self._tree.configure(yscrollcommand=scroll.set, xscrollcommand=self._hscroll.set)
        self._tree.pack(side="left", fill="both", expand=True)
        self._apply_date_columns()

        preview_box = tk.Frame(main, bg=BG)
        preview_box.pack(fill="x", padx=12, pady=(8, 4))
        self._preview = tk.Text(
            preview_box,
            height=5,
            bg=PANEL,
            fg=TEXT,
            relief="flat",
            wrap="word",
            font=FONT_SMALL,
            padx=8,
            pady=6,
            highlightthickness=1,
            highlightbackground=LINE,
            state="disabled",
        )
        self._preview.pack(fill="x")

        window.bind("<Control-f>", lambda _event: self._search_entry.focus_set() if self._search_entry else None)
        window.bind("<Control-F>", lambda _event: self._search_entry.focus_set() if self._search_entry else None)

        # 右下角留一个拖拽手柄：无边框窗口没法拖边框，只能自己画一个角。
        grip = tk.Label(
            main, text="◢", bg=BG, fg=LINE, font=FONT_SMALL, cursor="bottom_right_corner"
        )
        grip.place(relx=1.0, rely=1.0, anchor="se", width=16, height=16)
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)
        grip.bind("<ButtonRelease-1>", self._resize_end)

    def _build_ball(self) -> None:
        if self._ball is None:
            return
        x, y, size = self._ball_geometry()
        self._ball.geometry(f"{size}x{size}+{x}+{y}")
        canvas_bg = KEY_COLOR if self._ball_transparent else BG
        self._ball_canvas = tk.Canvas(
            self._ball,
            width=size,
            height=size,
            bg=canvas_bg,
            highlightthickness=0,
        )
        self._ball_canvas.pack(fill="both", expand=True)
        pad = 2
        self._ball_canvas.create_oval(
            pad, pad, size - pad, size - pad,
            fill=ACCENT, outline=CAT, width=2,
        )
        self._ball_canvas.create_oval(
            size * 0.2, size * 0.2, size * 0.8, size * 0.8,
            fill=ACCENT_DARK, outline="",
        )
        self._ball_canvas.create_text(
            size / 2, size / 2, text="拾", fill="#ffffff", font=FONT_BALL
        )
        self._ball_badge_bg_id = self._ball_canvas.create_oval(
            size - 24, 2, size - 2, 24,
            fill=ACCENT_DARK, outline=CAT, width=1, state="hidden",
        )
        self._ball_badge_id = self._ball_canvas.create_text(
            size - 13, 13, text="", fill="#ffffff",
            font=("Microsoft YaHei UI", 8, "bold"), state="hidden",
        )
        self._ball_canvas.bind("<ButtonPress-1>", self._ball_press)
        self._ball_canvas.bind("<B1-Motion>", self._ball_move)
        self._ball_canvas.bind("<ButtonRelease-1>", self._ball_release)
        self._ball_canvas.bind("<Button-3>", self._ball_menu)
        menu = tk.Menu(self._ball, tearoff=0)
        menu.add_command(label="打开拾光", command=self._show_expanded)
        menu.add_command(label="关闭", command=self._close)
        self._ball_menu = menu

    # ── 显示 / 收起 ─────────────────────────────────────────────────

    def _show_expanded(self, query: str = "") -> None:
        if self._expanded is None or self._root is None:
            return
        self._save_expanded_geometry()
        if self._ball is not None:
            self._save_ball_position()
            self._ball.withdraw()
        x, y, width, height = self._expanded_geometry()
        self._expanded.geometry(f"{width}x{height}+{x}+{y}")
        self._expanded.deiconify()
        self._expanded.lift()
        self._collapsed = False
        self._apply_topmost()
        if query:
            self._set_query(query)
        if self._search_entry is not None:
            self._search_entry.focus_set()
        self._touch()

    def _collapse(self) -> None:
        if self._ball is None or self._expanded is None:
            return
        self._save_expanded_geometry()
        x, y, size = self._ball_geometry()
        self._ball.geometry(f"{size}x{size}+{x}+{y}")
        self._expanded.withdraw()
        self._ball.deiconify()
        self._ball.lift()
        self._collapsed = True
        self._apply_topmost()
        self._save_state()
        self._touch()

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel_idle()
        self._save_expanded_geometry()
        self._save_ball_position()
        self._save_state()
        try:
            if self._history_window is not None:
                try:
                    if self._history_window.winfo_exists():
                        self._history_window.destroy()
                except tk.TclError:
                    pass
            if self._expanded is not None:
                self._expanded.destroy()
            if self._ball is not None:
                self._ball.destroy()
            if self._root is not None:
                self._root.destroy()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._clear_tk_refs()
            self._alive = False

    def _save_expanded_geometry(self) -> None:
        if self._expanded is None or not self._expanded.winfo_exists():
            return
        x = int(self._expanded.winfo_x())
        y = int(self._expanded.winfo_y())
        w = int(self._expanded.winfo_width())
        h = int(self._expanded.winfo_height())
        if w > 1 and h > 1:
            self._state["expanded"] = {"x": x, "y": y, "width": w, "height": h}
        self._state["pinned"] = bool(self._pinned)
        if self._search_var is not None:
            self._state["last_query"] = self._search_var.get()
        if self._date_var is not None:
            self._state["last_day"] = self._date_var.get()
        if self._speaker_var is not None:
            self._state["last_speaker"] = self._speaker_var.get()
        if self._template_var is not None:
            self._state["last_template"] = self._template_var.get()

    def _save_ball_position(self) -> None:
        if self._ball is None or not self._ball.winfo_exists():
            return
        size = int(self._ball.winfo_width())
        if size < 24:
            return
        self._state["ball"] = {
            "x": int(self._ball.winfo_x()),
            "y": int(self._ball.winfo_y()),
            "size": size,
        }

    # ── 拖动 ───────────────────────────────────────────────────────

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root, event.y_root)

    def _drag_move(self, event: tk.Event) -> None:
        if self._expanded is None:
            return
        dx = event.x_root - self._drag_offset[0]
        dy = event.y_root - self._drag_offset[1]
        self._expanded.geometry(f"+{self._expanded.winfo_x() + dx}+{self._expanded.winfo_y() + dy}")
        self._drag_offset = (event.x_root, event.y_root)

    def _drag_end(self, _event: tk.Event) -> None:
        self._save_expanded_geometry()

    # ── 右下角拖拽调大小 ─────────────────────────────────────────

    def _resize_start(self, event: tk.Event) -> None:
        if self._expanded is None:
            return
        self._resize_origin = (
            int(event.x_root),
            int(event.y_root),
            int(self._expanded.winfo_width()),
            int(self._expanded.winfo_height()),
        )

    def _resize_move(self, event: tk.Event) -> None:
        window = self._expanded
        if window is None:
            return
        start_x, start_y, start_w, start_h = self._resize_origin
        if start_w <= 0 or start_h <= 0:
            return
        new_w = max(340, start_w + (int(event.x_root) - start_x))
        new_h = max(420, start_h + (int(event.y_root) - start_y))
        left = int(window.winfo_x())
        top = int(window.winfo_y())
        new_w = min(new_w, max(340, int(window.winfo_screenwidth()) - left))
        new_h = min(new_h, max(420, int(window.winfo_screenheight()) - top))
        window.geometry(f"{new_w}x{new_h}+{left}+{top}")

    def _resize_end(self, _event: tk.Event) -> None:
        self._resize_origin = (0, 0, 0, 0)
        self._save_expanded_geometry()

    def _ball_press(self, event: tk.Event) -> None:
        self._ball_moved = False
        self._ball_start = (event.x_root, event.y_root)
        self._drag_offset = (event.x_root, event.y_root)
        self._touch()

    def _ball_move(self, event: tk.Event) -> None:
        if self._ball is None:
            return
        if abs(event.x_root - self._ball_start[0]) > 3 or abs(event.y_root - self._ball_start[1]) > 3:
            self._ball_moved = True
        dx = event.x_root - self._drag_offset[0]
        dy = event.y_root - self._drag_offset[1]
        self._ball.geometry(f"+{self._ball.winfo_x() + dx}+{self._ball.winfo_y() + dy}")
        self._drag_offset = (event.x_root, event.y_root)

    def _ball_release(self, _event: tk.Event) -> None:
        if not self._ball_moved:
            self._show_expanded()
            return
        self._save_ball_position()
        self._save_state()

    def _ball_menu(self, event: tk.Event) -> None:
        if self._ball_menu is None:
            return
        try:
            self._ball_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._ball_menu.grab_release()
            except Exception:  # noqa: BLE001
                pass

    # ── 搜索 ───────────────────────────────────────────────────────

    def _set_query(self, query: str) -> None:
        if self._search_var is None:
            return
        self._search_var.set(query or "")
        self._start_search_now()

    def _on_query_change(self, _event: tk.Event | None = None) -> None:
        if self._expanded is None:
            return
        self._touch()
        if self._search_after is not None:
            try:
                self._expanded.after_cancel(self._search_after)
            except tk.TclError:
                pass
        self._search_after = self._expanded.after(250, self._start_search_now)

    def _speaker_value(self) -> str:
        label = self._speaker_var.get() if self._speaker_var else "全部"
        master_name = str(getattr(self.plugin.cfg, "master_name", "主人") or "主人")
        catgirl_name = str(getattr(self.plugin.cfg, "catgirl_name", "猫娘") or "猫娘")
        return {master_name: "user", catgirl_name: "assistant"}.get(label, "")

    def _valid_day(self, value: str) -> bool:
        if not value:
            return True
        try:
            time.strptime(value, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _start_search_now(self, keep_limit: bool = False) -> None:
        self._search_after = None
        query = self._search_var.get().strip() if self._search_var else ""
        day = self._date_var.get().strip() if self._date_var else ""
        speaker = self._speaker_value()
        self._last_query = query
        self._last_day = day
        self._last_speaker = self._speaker_var.get() if self._speaker_var else "全部"
        self._last_template = self._template_var.get() if self._template_var else "引用"
        if not self._valid_day(day):
            self._set_status("日期格式应为 YYYY-MM-DD")
            return
        if not keep_limit:
            self._search_limit = max(
                10, min(100, int(getattr(self.plugin.cfg, "catalog_search_limit", 40) or 40))
            )
        if not query:
            if day:
                self._load_day(day)
            else:
                self._start_days()
            return
        gen = self._next_gen()
        self._pending_search = (gen, query, day, speaker)
        if self._search_running:
            self._set_status("搜索中…（等待上一次搜索结束）")
            return
        self._launch_search()

    def _launch_search(self) -> None:
        if self._search_running or not self._pending_search:
            return
        gen, query, day, speaker = self._pending_search
        self._pending_search = None
        self._search_running = True
        self._set_status("搜索中…")
        limit = self._search_limit

        def work() -> None:
            error = ""
            rows: list[dict] = []
            try:
                rows = self.plugin.catalog_search(
                    query, limit, day=day, speaker=speaker
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            self._queue.put(("search", gen, rows, query, day, error))

        threading.Thread(target=work, daemon=True, name="neko-catalog-search").start()

    def _more_results(self) -> None:
        if self._mode != "search" or self._search_limit >= 100:
            return
        self._search_limit = min(100, self._search_limit * 2)
        self._start_search_now(keep_limit=True)

    def _start_days(self) -> None:
        gen = self._next_gen()
        self._set_status("读取日期…")

        def work() -> None:
            error = ""
            rows: list[dict] = []
            try:
                rows = self.plugin.catalog_days(40)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            self._queue.put(("days", gen, rows, error))

        threading.Thread(target=work, daemon=True, name="neko-catalog-days").start()

    def _next_gen(self) -> int:
        self._query_gen += 1
        return self._query_gen

    def _load_day(self, day: str) -> None:
        self._last_day = day
        if self._date_var is not None:
            self._date_var.set(day)
        gen = self._next_gen()
        self._set_status(f"打开 {day}…")

        def work() -> None:
            error = ""
            rows: list[dict] = []
            try:
                rows = self.plugin.catalog_day(day)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            self._queue.put(("day", gen, day, rows, error))

        threading.Thread(target=work, daemon=True, name="neko-catalog-day").start()

    def _back_to_days(self) -> None:
        if self._search_var is not None:
            self._search_var.set("")
        if self._date_var is not None:
            self._date_var.set("")
        if self._speaker_var is not None:
            self._speaker_var.set("全部")
        self._start_days()

    # ── 队列 / 渲染 ─────────────────────────────────────────────────

    def _poll(self) -> None:
        if self._closing or self._root is None:
            return
        try:
            while True:
                event = self._queue.get_nowait()
                kind = event[0]
                if kind == "show":
                    self._show_expanded(str(event[1] or ""))
                elif kind == "close":
                    self._close()
                    return
                elif kind == "search":
                    _, gen, rows, query, day, error = event
                    self._search_running = False
                    if gen == self._query_gen:
                        if error:
                            self._set_status(f"搜索失败：{error}")
                        else:
                            self._render_rows(rows, "search", query=query)
                    # 搜完一次才启动下一次，避免快速输入时并发扫库。
                    if self._pending_search:
                        self._launch_search()
                elif kind == "days":
                    _, gen, rows, error = event
                    if gen == self._query_gen:
                        if error:
                            self._set_status(f"读取日期失败：{error}")
                        else:
                            self._render_rows(rows, "days")
                elif kind == "day":
                    _, gen, day, rows, error = event
                    if gen == self._query_gen:
                        if error:
                            self._set_status(f"打开失败：{error}")
                        else:
                            self._render_rows(rows, "lines", query=day)
                elif kind == "history":
                    _, items, error = event
                    if error:
                        self._set_status(f"读取最近送入失败：{error}")
                    else:
                        self._show_history_window(items)
                elif kind == "send":
                    _, ok, message = event
                    self._set_status(message)
                    if self._send_button is not None:
                        self._send_button.configure(state="normal")
        except queue.Empty:
            pass
        if not self._closing and self._root is not None:
            delay = 300 if self._collapsed else 100
            self._root.after(delay, self._poll)

    def _tree_values(self, row: dict, mode: str) -> tuple[str, str, str, str]:
        if mode == "days":
            text = " ".join(str(row.get("first_text") or row.get("snippet") or "").split())
            if len(text) > 40:
                text = text[:40] + "…"
            return (
                str(row.get("date") or ""),
                f"{int(row.get('turns') or 0)} 句",
                f"{row.get('first_time') or '--:--'}–{row.get('last_time') or '--:--'}",
                text,
            )
        raw = str(row.get("text") or row.get("snippet") or "")
        text = catalog_mod.mark_query(raw, self._current_query, limit=48)
        return (
            str(row.get("date") or ""),
            str(row.get("time") or ""),
            str(row.get("speaker") or ""),
            text,
        )

    def _render_rows(self, rows: list[dict], mode: str, *, query: str = "") -> None:
        tree = self._tree
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        if mode == "lines":
            speaker = self._speaker_value()
            if speaker:
                rows = [row for row in rows if str(row.get("role") or "") == speaker]
        self._rows_by_iid = {}
        self._mode = mode
        self._current_rows = []
        self._current_query = query if mode == "search" else ""
        self._set_preview("")
        self._set_send_enabled(False)
        if mode == "days":
            self._set_status(f"最近 {len(rows)} 天")
            if self._back is not None:
                self._back.pack_forget()
            if self._more_button is not None:
                self._more_button.pack_forget()
        elif mode == "lines":
            self._set_status(f"{query} · {len(rows)} 句")
            if self._back is not None:
                self._back.pack(side="left")
            if self._more_button is not None:
                self._more_button.pack_forget()
        else:
            self._set_status(f"搜索 “{query}” · {len(rows)} 条")
            if self._back is not None:
                self._back.pack(side="left")
            if self._more_button is not None:
                if self._search_limit < 100 and len(rows) >= self._search_limit:
                    self._more_button.pack(side="right", padx=(0, 6))
                else:
                    self._more_button.pack_forget()
        self._set_ball_badge(len(rows) if mode == "search" else 0)
        for index, row in enumerate(rows):
            iid = f"r{index}"
            self._rows_by_iid[iid] = row
            tags: tuple[str, ...] = ()
            if mode == "days":
                tags = ("day",)
            else:
                if mode == "search":
                    tags = ("hit",)
                if row.get("role") == "assistant":
                    tags += ("assistant",)
                elif row.get("role") == "user":
                    tags += ("user",)
            tree.insert("", "end", iid=iid, values=self._tree_values(row, mode), tags=tags)
        self._apply_date_columns()
        if rows:
            tree.see("r0")

    def _selected_rows(self) -> list[dict]:
        tree = self._tree
        if tree is None:
            return []
        return [
            self._rows_by_iid[iid]
            for iid in tree.selection()
            if iid in self._rows_by_iid
        ]

    def _template_mode(self) -> str:
        return "topic" if (self._template_var and self._template_var.get() == "话题") else "quote"

    def _template_wrap(self) -> tuple[str | None, str | None]:
        """按当前模板取 (开头, 结尾) 文案；配置留空时用内置默认，写空串就整段省略。"""

        cfg = self.plugin.cfg
        if self._template_mode() == "topic":
            lead = getattr(cfg, "catalog_topic_lead", None)
            footer = getattr(cfg, "catalog_topic_footer", None)
            return (lead if isinstance(lead, str) else None), (
                footer if isinstance(footer, str) else None
            )
        footer = getattr(cfg, "catalog_quote_footer", None)
        return None, (footer if isinstance(footer, str) else None)

    def _apply_date_columns(self) -> None:
        """按「显示日期」勾选状态决定列；日期目录本身必须带日期。"""

        tree = self._tree
        if tree is None:
            return
        if self._mode == "days" or self._show_dates:
            tree.configure(displaycolumns=("date", "time", "who", "text"))
        else:
            tree.configure(displaycolumns=("who", "text"))

    def _toggle_show_dates(self) -> None:
        self._show_dates = bool(self._show_dates_var.get()) if self._show_dates_var else False
        self._state["show_dates"] = self._show_dates
        self._save_state()
        self._apply_date_columns()

    def _on_tree_click(self, event: tk.Event) -> str | None:
        """单击切换这一行的高亮，再点取消；不按修饰键也能选中多条。"""

        tree = self._tree
        if tree is None:
            return None
        if int(getattr(event, "state", 0)) & 0x0005:
            # Ctrl / Shift 留给 Tk 默认的连续选择。
            return None
        tree.focus_set()
        iid = tree.identify_row(event.y)
        if not iid:
            return "break"  # 点空白处不清空已选中的行
        if iid in tree.selection():
            tree.selection_remove(iid)
        else:
            tree.selection_add(iid)
        self._on_select(None)
        return "break"

    def _on_select(self, _event: tk.Event | None = None) -> None:
        rows = self._selected_rows()
        self._current_rows = rows
        self._touch()
        if not rows:
            self._set_preview("")
            self._set_send_enabled(False)
            return
        if self._mode == "days":
            if len(rows) == 1:
                row = rows[0]
                day = str(row.get("date") or "")
                text = (
                    f"{day} 共 {int(row.get('turns') or 0)} 句，"
                    f"从 {row.get('first_time') or '--:--'} 到 {row.get('last_time') or '--:--'}。\n"
                    "双击打开这一天；也可以选中后直接送入对话。"
                )
                self._set_preview(text)
                self._set_send_enabled(True)
            else:
                self._set_preview("一次只能选一天送入。")
                self._set_send_enabled(False)
            return
        lead, footer = self._template_wrap()
        quote = catalog_mod.format_quote(
            rows,
            master_name=getattr(self.plugin.cfg, "master_name", "主人"),
            catgirl_name=getattr(self.plugin.cfg, "catgirl_name", "猫娘"),
            template=self._template_mode(),
            lead=lead,
            footer=footer,
        )
        self._set_preview(quote or "选中的行没有可发送的内容。")
        self._set_send_enabled(bool(quote))

    def _refresh_preview(self) -> None:
        self._on_select(None)

    def _on_double_click(self, _event: tk.Event | None = None) -> None:
        tree = self._tree
        if self._mode != "days" or tree is None:
            return
        day = ""
        if _event is not None:
            iid = tree.identify_row(_event.y)
            row = self._rows_by_iid.get(iid) if iid else None
            if row:
                day = str(row.get("date") or "")
        if not day:
            rows = self._selected_rows()
            if len(rows) == 1:
                day = str(rows[0].get("date") or "")
        if day:
            self._load_day(day)

    def _set_preview(self, text: str) -> None:
        if self._preview is None:
            return
        self._preview.configure(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", text or "")
        self._preview.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        if self._status is not None:
            self._status.configure(text=text)

    def _set_send_enabled(self, enabled: bool) -> None:
        if self._send_button is not None:
            self._send_button.configure(state="normal" if enabled else "disabled")

    # ── 发送 ───────────────────────────────────────────────────────

    def _send(self) -> None:
        rows = self._current_rows
        if not rows:
            self._set_status("先选中要送入的对话")
            return
        if self._mode == "days":
            if len(rows) != 1:
                self._set_status("一次只能选一天送入")
                return
            text = catalog_mod.format_day_prompt(
                str(rows[0].get("date") or ""), turns=int(rows[0].get("turns") or 0)
            )
        else:
            lead, footer = self._template_wrap()
            text = catalog_mod.format_quote(
                rows,
                master_name=getattr(self.plugin.cfg, "master_name", "主人"),
                catgirl_name=getattr(self.plugin.cfg, "catgirl_name", "猫娘"),
                template=self._template_mode(),
                lead=lead,
                footer=footer,
            )
        if not text:
            self._set_status("选中的内容为空")
            return
        self._dispatch_send(text)

    def _dispatch_send(self, text: str, respond: bool | None = None) -> None:
        self._touch()
        if respond is None:
            respond = bool(self._respond_var.get()) if self._respond_var is not None else False
        self._set_status("正在送入对话…")
        if self._send_button is not None:
            self._send_button.configure(state="disabled")

        def work() -> None:
            try:
                result = self.plugin.catalog_send_text(text, respond=respond)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("send", False, f"送入失败：{exc}"))
                return
            ok = bool(result.get("ok"))
            message = str(result.get("message") or ("已送入对话" if ok else "送入失败"))
            self._queue.put(("send", ok, message))

        threading.Thread(target=work, daemon=True, name="neko-catalog-send").start()

    # ── P1/P2 辅助：角标、置顶、空闲、历史 ─────────────────────────

    def _set_ball_badge(self, count: int) -> None:
        self._badge_count = max(0, int(count))
        if self._ball_canvas is None or self._ball_badge_id is None:
            return
        text = str(self._badge_count if self._badge_count < 100 else "99+")
        self._ball_canvas.itemconfigure(
            self._ball_badge_id,
            text=text,
            state="normal" if self._badge_count > 0 else "hidden",
        )
        if getattr(self, "_ball_badge_bg_id", None):
            self._ball_canvas.itemconfigure(
                self._ball_badge_bg_id,
                state="normal" if self._badge_count > 0 else "hidden",
            )

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        if self._pin_label is not None:
            self._pin_label.configure(text="📌" if self._pinned else "📍")
        self._apply_topmost()
        self._save_state()

    def _apply_topmost(self) -> None:
        for window in (self._expanded, self._ball):
            if window is None:
                continue
            try:
                window.attributes("-topmost", self._pinned)
            except tk.TclError:
                pass

    def _touch(self) -> None:
        if self._root is None:
            return
        if self._idle_after is not None:
            try:
                self._root.after_cancel(self._idle_after)
            except tk.TclError:
                pass
            self._idle_after = None
        minutes = int(getattr(self.plugin.cfg, "catalog_idle_close_minutes", 10) or 0)
        if minutes <= 0:
            return
        self._idle_after = self._root.after(minutes * 60 * 1000, self._idle_tick)

    def _idle_tick(self) -> None:
        self._idle_after = None
        if self._collapsed:
            self._close()
            return
        self._collapse()

    def _cancel_idle(self) -> None:
        if self._root is not None and self._idle_after is not None:
            try:
                self._root.after_cancel(self._idle_after)
            except tk.TclError:
                pass
        self._idle_after = None

    def _show_history(self) -> None:
        self._set_status("读取最近送入…")

        def work() -> None:
            try:
                items = self.plugin.catalog_get_history(
                    max(5, min(100, int(getattr(self.plugin.cfg, "catalog_history_limit", 20) or 20)))
                )
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("history", [], str(exc)))
                return
            self._queue.put(("history", items, ""))

        threading.Thread(target=work, daemon=True, name="neko-catalog-history").start()

    def _show_history_window(self, items: list[dict]) -> None:
        if not items:
            self._set_status("还没有送入历史")
            return
        if self._history_window is not None:
            try:
                if self._history_window.winfo_exists():
                    self._history_window.destroy()
            except tk.TclError:
                pass
        window = tk.Toplevel(self._expanded)
        window.title("最近送入")
        window.configure(bg=BG)
        window.geometry("420x360")
        window.attributes("-topmost", self._pinned)
        window.transient(self._expanded)
        frame = tk.Frame(window, bg=BG)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        listbox = tk.Listbox(
            frame,
            bg=PANEL,
            fg=TEXT,
            selectbackground=ACCENT_DARK,
            selectforeground="#fff",
            relief="flat",
            font=FONT_SMALL,
            activestyle="none",
        )
        listbox.pack(fill="both", expand=True)
        for item in reversed(items):
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(item.get("at") or 0)))
            text = str(item.get("text") or "")
            lines = [
                " ".join(line.split())
                for line in text.split("\n")
                if line.strip() and not line.strip().startswith("【拾光")
            ]
            head = (lines[0][:60] if lines else "（空）")
            listbox.insert("end", f"{stamp} · {head}")
        self._history_list = listbox
        self._history_items = list(reversed(items))
        buttons = tk.Frame(window, bg=BG)
        buttons.pack(fill="x", padx=10, pady=(0, 10))

        def send_selected() -> None:
            selection = listbox.curselection()
            if not selection:
                return
            item = self._history_items[int(selection[0])]
            text = str(item.get("text") or "")
            if text:
                window.destroy()
                self._dispatch_send(text, bool(item.get("respond")))

        def close_history() -> None:
            window.destroy()

        tk.Button(
            buttons, text="再次送入", command=send_selected,
            bg=ACCENT, fg="#fff", relief="flat", font=FONT_UI, padx=10, pady=4,
        ).pack(side="right")
        tk.Button(
            buttons, text="关闭", command=close_history,
            bg=PANEL, fg=TEXT, relief="flat", font=FONT_UI, padx=10, pady=4,
        ).pack(side="right", padx=(0, 6))
        listbox.bind("<Double-1>", lambda _event: send_selected())
        self._history_window = window


__all__ = ["CatalogWindow"]
