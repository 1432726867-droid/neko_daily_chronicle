#!/usr/bin/env python3
"""用 N.E.K.O 宿主自己的打包接口生成 .neko-plugin（官方便捷、哈希可校验）。

为什么要走宿主：

* ``metadata.toml`` 里的 ``payload_hash`` 是宿主自己的算法，外部无法可靠复现
  （实测反推约 1 万多种组合全部对不上），而宿主 ``verify`` 会校验它；
* ``POST /plugin-cli/build`` **只认宿主自己的插件根**（``builtin`` / ``user``），
  传别的路径一律 404，``plugins_root`` / ``target_dir`` 之类的字段会被忽略。

所以本脚本的流程是：找到正在运行的插件服务器 → 把源码同步到宿主的 user 插件根
→ 让宿主打包 → 让宿主校验哈希 → 把产物复制回 ``<plugin_dir>/dist/``。

    python package_plugin.py                       # 对当前目录（或本脚本所在插件）打包
    python package_plugin.py --plugin-dir D:\\dsh\\my_plugin
    python package_plugin.py --host-root D:\\N.E.K.O   # 显式指定宿主用户数据根
    python package_plugin.py --keep-source          # 保留宿主插件根里的源码副本（默认保留）
    python package_plugin.py --clean-source         # 打包后删掉宿主的源码副本
    python package_plugin.py --install              # 顺带用官方安装流程装进宿主
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tomllib
import urllib.error
import urllib.request

IGNORE_DIRS = {".tmp-tests", "__pycache__", ".pytest_cache", ".git", "dist", ".venv", "node_modules"}
IGNORE_SUFFIXES = {".pyc", ".pyc-check"}
PORT_RANGE = range(48911, 48946)


# ── 基础工具 ────────────────────────────────────────────────────────


def http(base: str, path: str, payload: dict | None = None, timeout: float = 600.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", errors="replace")[:600]}
    except Exception as exc:  # noqa: BLE001
        return -1, {"detail": f"{type(exc).__name__}: {exc}"}


def find_server(port: int | None) -> tuple[str | None, str]:
    ports = [port] if port else list(PORT_RANGE)
    for candidate in ports:
        base = f"http://127.0.0.1:{candidate}"
        try:
            with urllib.request.urlopen(base + "/openapi.json", timeout=0.6) as response:
                spec = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        title = str(spec.get("info", {}).get("title", ""))
        if "Plugin Server" in title:
            return base, title
    return None, ""


def default_plugin_dir() -> pathlib.Path:
    """脚本放在 <plugin>/tools/ 时用插件根，放在 skill 里时用当前目录。"""

    here = pathlib.Path(__file__).resolve()
    for candidate in (pathlib.Path.cwd(), here.parents[1] if len(here.parents) > 1 else pathlib.Path.cwd()):
        if (candidate / "plugin.toml").is_file():
            return candidate
    return pathlib.Path.cwd()


def load_manifest(plugin_dir: pathlib.Path) -> dict:
    with open(plugin_dir / "plugin.toml", "rb") as handle:
        manifest = tomllib.load(handle)
    plugin = manifest.get("plugin")
    if not isinstance(plugin, dict) or not plugin.get("id"):
        raise SystemExit(f"[FAIL] {plugin_dir / 'plugin.toml'} 里缺少 [plugin].id")
    return plugin


# ── 宿主插件根 ──────────────────────────────────────────────────────


def user_root_candidates(host_root: pathlib.Path | None) -> list[pathlib.Path]:
    """宿主 user 插件根的候选；迁移后的 ``.neko-plugin-installations`` 优先。"""

    candidates: list[pathlib.Path] = []
    roots: list[pathlib.Path] = []
    if host_root:
        roots.append(pathlib.Path(host_root))
    for env in ("NEKO_DATA_DIR", "NEKO_USER_DATA_DIR"):
        value = os.environ.get(env)
        if value:
            roots.append(pathlib.Path(value))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(pathlib.Path(local) / "N.E.K.O")
    # 常见的手工安装位置
    roots.append(pathlib.Path(r"D:\N.E.K.O"))

    for root in roots:
        candidates.append(root / ".neko-plugin-installations" / "plugins")
        candidates.append(root / "plugins")
    return candidates


def pick_user_root(host_root: pathlib.Path | None) -> pathlib.Path | None:
    """挑出真正在用的 user 根：里面应该有带 plugin.toml 的已安装插件。"""

    best: tuple[int, pathlib.Path] | None = None
    for candidate in user_root_candidates(host_root):
        if not candidate.is_dir():
            continue
        count = sum(
            1 for item in candidate.iterdir() if item.is_dir() and (item / "plugin.toml").is_file()
        )
        if count and (best is None or count > best[0]):
            best = (count, candidate)
    return best[1] if best else None


def copy_source(plugin_dir: pathlib.Path, target: pathlib.Path, expected_id: str) -> int:
    """把源码同步到宿主插件根；目标已是别的插件时拒绝覆盖。"""

    existing = target / "plugin.toml"
    if existing.is_file():
        try:
            with open(existing, "rb") as handle:
                other_id = str(tomllib.load(handle).get("plugin", {}).get("id") or "")
        except Exception:  # noqa: BLE001
            other_id = ""
        if other_id and other_id != expected_id:
            raise SystemExit(f"[FAIL] 拒绝覆盖：{target} 里已经是另一个插件（id={other_id}）")

    def ignore(_directory, names):
        return [
            name
            for name in names
            if name in IGNORE_DIRS or pathlib.Path(name).suffix in IGNORE_SUFFIXES
        ]

    if target.exists():
        for item in target.iterdir():
            if item.name in IGNORE_DIRS:
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
    shutil.copytree(plugin_dir, target, ignore=ignore, dirs_exist_ok=True)
    return sum(1 for path in target.rglob("*") if path.is_file())


# ── 主流程 ──────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="用宿主自己的接口打包 N.E.K.O 插件")
    parser.add_argument("--plugin-dir", default=None, help="插件源码目录（默认自动判断）")
    parser.add_argument("--host-root", default=None, help="宿主的用户数据根，例如 D:\\N.E.K.O")
    parser.add_argument("--port", type=int, default=None, help="插件服务器端口（默认自动扫描）")
    parser.add_argument("--out", default=None, help="输出路径，必须落在宿主的 .neko-plugin-packages 内")
    parser.add_argument("--keep-source", action="store_true", help="保留宿主插件根里的源码副本（默认保留）")
    parser.add_argument("--clean-source", action="store_true", help="打包后删除宿主的源码副本")
    parser.add_argument("--install", action="store_true", help="打包后用官方流程安装到宿主")
    args = parser.parse_args()

    plugin_dir = pathlib.Path(args.plugin_dir).resolve() if args.plugin_dir else default_plugin_dir()
    if not (plugin_dir / "plugin.toml").is_file():
        print(f"[FAIL] {plugin_dir} 里没有 plugin.toml；用 --plugin-dir 指定插件目录")
        return 2

    plugin = load_manifest(plugin_dir)
    plugin_id = str(plugin["id"])
    if plugin_id != plugin_dir.name:
        print(f"[WARN] 插件 ID（{plugin_id}）与目录名（{plugin_dir.name}）不一致；打包与加载都要求一致")

    base, title = find_server(args.port)
    if base is None:
        print("[FAIL] 没有找到正在运行的 N.E.K.O User Plugin Server；请先启动 N.E.K.O")
        return 2
    print(f"[OK] 插件服务器：{base}（{title}）")

    host_root = pathlib.Path(args.host_root) if args.host_root else None
    root = pick_user_root(host_root)
    if root is None:
        print("[FAIL] 找不到宿主的 user 插件根；用 --host-root 指定用户数据根（例如 D:\\N.E.K.O）")
        return 2
    print(f"[OK] user 插件根：{root}")

    target = root / plugin_id
    files = copy_source(plugin_dir, target, plugin_id)
    print(f"[OK] 已同步源码到 {target}（{files} 个文件）")

    http(base, "/plugins/refresh", {})
    status, listing = http(base, "/plugin-cli/plugins")
    known = listing.get("plugins", []) if isinstance(listing, dict) else []
    if plugin_id in known:
        print(f"[OK] 宿主已识别 {plugin_id}（共 {len(known)} 个插件）")
    else:
        print(f"[WARN] 宿主还没有列出 {plugin_id}；继续尝试打包")

    request: dict = {"mode": "single", "plugin": plugin_id}
    if args.out:
        request["out"] = args.out
    status, payload = http(base, "/plugin-cli/build", request)
    if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        print(f"[FAIL] 打包失败：{status} {json.dumps(payload, ensure_ascii=False)[:600]}")
        return 1
    built = payload["built"][0]
    package_path = pathlib.Path(built["package_path"])
    print(f"[OK] 打包完成：{package_path}（{built['package_size_bytes']} 字节）")
    print(f"     payload_hash = {built['payload_hash']}")

    status, verify = http(base, "/plugin-cli/verify", {"package": str(package_path)})
    verified = bool(verify.get("payload_hash_verified")) if isinstance(verify, dict) else False
    print(f"[{'OK' if verified else 'FAIL'}] 哈希校验：payload_hash_verified={verified}")
    if isinstance(verify, dict):
        print(
            f"     版本 {verify.get('version')} ｜ 插件 {verify.get('plugin_count')} ｜ "
            f"profile {verify.get('profile_names')}"
        )

    dist = plugin_dir / "dist"
    dist.mkdir(exist_ok=True)
    local_copy = dist / f"{plugin_id}.neko-plugin"
    shutil.copy2(package_path, local_copy)
    print(f"[OK] 已复制到 {local_copy}")

    if args.install:
        plan_request = {"package": str(package_path)}
        _, plan = http(base, "/plugin-cli/install-plan", plan_request)
        request_install = {"package": str(package_path), "confirm": True}
        if isinstance(plan, dict) and plan.get("confirmation_token"):
            request_install["confirmation_token"] = plan["confirmation_token"]
        status, result = http(base, "/plugin-cli/install", request_install)
        print(f"[{'OK' if status == 200 else 'FAIL'}] 安装：{status} {json.dumps(result, ensure_ascii=False)[:400]}")

    if args.clean_source:
        shutil.rmtree(target, ignore_errors=True)
        print(f"[OK] 已删除宿主的源码副本：{target}")
    else:
        print(f"     宿主插件根保留源码副本：{target}（宿主可直接加载/热重载它）")

    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
