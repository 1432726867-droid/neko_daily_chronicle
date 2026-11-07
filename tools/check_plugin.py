"""插件自检：把插件管理器「校验」按钮关心的那几件事在本地先查一遍。

    python tools/check_plugin.py

检查项：
* ``plugin.toml`` 能否解析，必填字段是否齐全；
* 插件 ID 与源码目录名是否一致（打包与开发模式都要求一致）；
* ``entry`` 是否形如 ``plugin.plugins.<id>:<ClassName>``，且类真的定义在模块里；
* 声明的 i18n 语言文件、UI panel / guide 文件是否存在且后缀合法；
* ``config.example.toml`` 与 ``config.schema.json`` 是否是合法文件；
* 所有 ``.py`` 是否能编译（语法错误会让插件直接加载失败）。
"""

from __future__ import annotations

import json
import pathlib
import py_compile
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIRED_PLUGIN_FIELDS = ("id", "name", "version", "entry")
VALID_SURFACE_SUFFIXES = {".tsx", ".jsx", ".md", ".mdx", ".html", ".htm"}


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        print(f"[OK]   {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"[FAIL] {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"[WARN] {message}")


def load_manifest(report: Report) -> dict:
    path = ROOT / "plugin.toml"
    if not path.is_file():
        report.fail("缺少 plugin.toml")
        return {}
    try:
        with open(path, "rb") as handle:
            manifest = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        report.fail(f"plugin.toml 解析失败：{exc}")
        return {}

    plugin = manifest.get("plugin")
    if not isinstance(plugin, dict):
        report.fail("plugin.toml 里缺少 [plugin] 段")
        return {}

    missing = [field for field in REQUIRED_PLUGIN_FIELDS if not plugin.get(field)]
    if missing:
        report.fail(f"[plugin] 缺少必填字段：{', '.join(missing)}")
    else:
        report.ok(f"[plugin] 必填字段齐全（id={plugin['id']} version={plugin['version']}）")

    plugin_id = str(plugin.get("id") or "")
    if plugin_id and plugin_id != ROOT.name:
        report.fail(f"插件 ID（{plugin_id}）与源码目录名（{ROOT.name}）不一致，打包与开发模式都会失败")
    elif plugin_id:
        report.ok(f"目录名与插件 ID 一致：{plugin_id}")

    if plugin.get("type") not in (None, "plugin", "adapter"):
        report.fail(f"[plugin].type 非法：{plugin.get('type')!r}")
    return manifest


def check_entry(plugin: dict, report: Report) -> None:
    entry = str(plugin.get("entry") or "")
    if not entry:
        return
    if entry.count(":") != 1:
        report.fail(f"entry 必须是 module.path:ClassName 形式，当前为 {entry!r}")
        return
    module_path, class_name = entry.split(":")
    plugin_id = str(plugin.get("id") or "")
    expected_module = f"plugin.plugins.{plugin_id}"
    if module_path != expected_module:
        report.warn(f"entry 模块路径为 {module_path!r}，与惯例 {expected_module!r} 不同（外部开发目录请确认宿主如何映射）")
    else:
        report.ok(f"entry 模块路径符合惯例：{module_path}")

    # 模块文件：plugin/plugins/<id>/__init__.py 或 <id>.py
    candidates = [ROOT / "__init__.py", ROOT / f"{plugin_id}.py"]
    source = None
    for candidate in candidates:
        if candidate.is_file():
            source = candidate.read_text(encoding="utf-8")
            break
    if source is None:
        report.fail(f"找不到 entry 对应的模块文件（尝试过 {', '.join(item.name for item in candidates)}）")
        return
    if f"class {class_name}" not in source:
        report.fail(f"模块里没有定义类 {class_name}")
    else:
        report.ok(f"入口类存在：{class_name}")


def check_resources(manifest: dict, report: Report) -> None:
    plugin = manifest.get("plugin", {})
    i18n = plugin.get("i18n")
    if isinstance(i18n, dict):
        locales_dir = ROOT / str(i18n.get("locales_dir") or "i18n")
        default_locale = str(i18n.get("default_locale") or "")
        if not locales_dir.is_dir():
            report.fail(f"i18n 目录不存在：{locales_dir.name}")
        else:
            files = sorted(item.stem for item in locales_dir.glob("*.json"))
            if default_locale and default_locale not in files:
                report.fail(f"缺少默认语言文件：{default_locale}.json（现有 {', '.join(files) or '无'}）")
            else:
                report.ok(f"i18n 语言文件：{', '.join(files)}")
            for item in locales_dir.glob("*.json"):
                try:
                    json.loads(item.read_text(encoding="utf-8"))
                except ValueError as exc:
                    report.fail(f"{item.name} 不是合法 JSON：{exc}")

    ui = plugin.get("ui")
    if isinstance(ui, dict) and ui.get("enabled"):
        surfaces: list[tuple[str, dict]] = []
        panels = ui.get("panel")
        if isinstance(panels, list):
            surfaces.extend(("panel", item) for item in panels if isinstance(item, dict))
        elif isinstance(panels, dict):
            surfaces.append(("panel", panels))
        for key in ("guide", "docs"):
            items = ui.get(key)
            if isinstance(items, list):
                surfaces.extend((key, item) for item in items if isinstance(item, dict))
        if not surfaces:
            report.warn("[plugin.ui] 已启用但没有声明任何 panel / guide")
        for kind, surface in surfaces:
            entry = str(surface.get("entry") or "")
            if not entry:
                report.fail(f"{kind} 缺少 entry")
                continue
            path = ROOT / entry
            if not path.is_file():
                report.fail(f"{kind} 文件不存在：{entry}")
            elif path.suffix.lower() not in VALID_SURFACE_SUFFIXES:
                report.fail(f"{kind} 后缀不合法：{entry}（应为 tsx/jsx/md/html）")
            else:
                report.ok(f"{kind} 资源就位：{entry}")
            if kind == "panel" and not surface.get("context"):
                report.warn(f"panel {entry} 没有声明 context，面板拿不到 Python 状态")


def check_config_files(report: Report) -> None:
    example = ROOT / "config.example.toml"
    if example.is_file():
        try:
            with open(example, "rb") as handle:
                tomllib.load(handle)
            report.ok("config.example.toml 可解析")
        except tomllib.TOMLDecodeError as exc:
            report.fail(f"config.example.toml 解析失败：{exc}")
    else:
        report.warn("没有 config.example.toml（首次运行的配置模板）")

    schema = ROOT / "config.schema.json"
    if schema.is_file():
        try:
            data = json.loads(schema.read_text(encoding="utf-8"))
        except ValueError as exc:
            report.fail(f"config.schema.json 不是合法 JSON：{exc}")
        else:
            if data.get("type") != "object":
                report.fail("config.schema.json 的根节点必须是 type: object")
            else:
                report.ok("config.schema.json 可解析且根为 object")
    else:
        report.warn("没有 config.schema.json（配置面板会退回通用编辑器）")


def check_python(report: Report) -> None:
    files = []
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if "__pycache__" in relative.parts:
            continue
        # 只跳过插件自己的 .tmp-tests，别把"路径里恰好有这个名字"的祖先也算进来
        if relative.parts and relative.parts[0] == ".tmp-tests":
            continue
        files.append(path)
    broken: list[str] = []
    for path in files:
        try:
            py_compile.compile(str(path), cfile=str(path) + ".pyc-check", doraise=True)
        except py_compile.PyCompileError as exc:
            broken.append(f"{path.relative_to(ROOT)}: {exc.msg.strip().splitlines()[-1]}")
        finally:
            leftover = pathlib.Path(str(path) + ".pyc-check")
            if leftover.exists():
                leftover.unlink(missing_ok=True)
    if broken:
        for item in broken:
            report.fail(f"语法错误 {item}")
    else:
        report.ok(f"{len(files)} 个 Python 文件全部可编译")


def main() -> int:
    report = Report()
    manifest = load_manifest(report)
    plugin = manifest.get("plugin", {}) if isinstance(manifest.get("plugin"), dict) else {}
    check_entry(plugin, report)
    check_resources(manifest, report)
    check_config_files(report)
    check_python(report)
    print(f"\n结果：{report.failures} 个错误，{report.warnings} 个提醒")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
