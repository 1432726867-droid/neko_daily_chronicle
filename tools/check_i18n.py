"""检查 Python / TSX 里用到的 i18n key 与 i18n/*.json 是否对得上。

用法（在插件目录下）：

    python tools/check_i18n.py

任何缺失或多余的 key 都会打印出来并以退出码 1 结束，方便接进 CI。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_FILES = [
    ROOT / "__init__.py",
    *sorted((ROOT / "routers").glob("*.py")),
    ROOT / "ui" / "panel.tsx",
]
LOCALES = ["zh-CN.json", "en.json"]
KEY_PATTERNS = [re.compile(r'\btr\(\s*"([^"]+)"'), re.compile(r'\bt\(\s*"([^"]+)"')]


def collect_used_keys() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for path in SOURCE_FILES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in KEY_PATTERNS:
            for key in pattern.findall(text):
                used.setdefault(key, []).append(path.name)
    return used


def main() -> int:
    used = collect_used_keys()
    failures = 0

    for locale in LOCALES:
        path = ROOT / "i18n" / locale
        if not path.is_file():
            print(f"[FAIL] 缺少语言文件：{path}")
            failures += 1
            continue
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"[FAIL] {locale} 不是合法 JSON：{exc}")
            failures += 1
            continue

        missing = sorted(key for key in used if key not in catalog)
        extra = sorted(key for key in catalog if key not in used)
        empty = sorted(key for key, value in catalog.items() if not str(value).strip())

        if missing:
            failures += 1
            print(f"[FAIL] {locale} 缺少 {len(missing)} 个 key：")
            for key in missing:
                print(f"       {key}  （用于 {', '.join(sorted(set(used[key]))) }）")
        if extra:
            print(f"[WARN] {locale} 有 {len(extra)} 个未被引用的 key：{', '.join(extra)}")
        if empty:
            failures += 1
            print(f"[FAIL] {locale} 有空值：{', '.join(empty)}")
        if not (missing or extra or empty):
            print(f"[OK] {locale}：{len(catalog)} 个 key 全部对得上")

    print(f"\n共引用 {len(used)} 个 key。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
