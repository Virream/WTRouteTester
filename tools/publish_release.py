"""把打包结果发到 GitHub Releases(Virream/WTRouteTester)。

产物: `dist\\WTRouteTester_<版本>_win64.zip`(由本脚本从 `dist\\WTRouteTester\\` 现场压出来,
**不含 `data\\`** —— 那是用户自己的录制与地图缓存)。

用法:
  .venv\\Scripts\\python.exe tools\\publish_release.py                      # 用 v<版本> 当 tag
  .venv\\Scripts\\python.exe tools\\publish_release.py v1.4.0 notes.md      # 指定 tag / 备注文件
  .venv\\Scripts\\python.exe tools\\publish_release.py --src ..\\WT81111G\\dist\\WTRouteTester

⚠ **务必确认 `--src` 指向的是刚打出来的那份构建**: 本仓库 `dist\\WTRouteTester\\` 里还留着
  1.0.0 时代的老包(没装 GPU 依赖的那份), 默认会压它 —— 2026-11-10 就这么把 v1.0.0 的
  exe 发成了 v1.4.0。现在默认会校验 exe 的 FileVersion 与 `ROUTE_VERSION` 一致, 不一致直接退出。

凭据: `git credential fill`(需先对该仓库 push 过, 让 GitHub 凭据进 Windows 凭据管理器)。
备注末尾会自动追加「下载」小节(体积 + SHA256), 不用手写。

⚠ tag 已存在时**先删 release 和 tag 再重建**(幂等), 便于同一版本反复修正。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

REPO = "Virream/WTRouteTester"
DIST_DIR = "dist"
APP_DIR = os.path.join("dist", "WTRouteTester")


def read_version(base: str) -> str:
    with open(os.path.join(base, "wt81111g", "config.py"), encoding="utf-8") as f:
        m = re.search(r"ROUTE_VERSION\s*=\s*['\"]([^'\"]+)['\"]", f.read())
    return m.group(1) if m else ""


def make_zip(base: str, ver: str, src: str | None = None) -> str:
    """把 `<src>`(默认 dist\\WTRouteTester\\)压成 dist\\WTRouteTester_<ver>_win64.zip(跳过 data)。"""
    src = src or os.path.join(base, APP_DIR)
    if not os.path.isdir(src):
        raise SystemExit(f"找不到打包目录: {src}(先跑 build_route_tester.ps1)")
    # 版本校验: 打包目录里那个 exe 的 FileVersion 必须与 ROUTE_VERSION 一致
    exe = os.path.join(src, "WTRouteTester.exe")
    if os.path.isfile(exe):
        import ctypes  # noqa: F401  (仅 Windows)
        try:
            info = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{exe}').VersionInfo.FileVersion"],
                capture_output=True, text=True).stdout.strip()
        except Exception:  # noqa: BLE001
            info = ""
        if info and info != ver:
            raise SystemExit(f"!! {exe} 的 FileVersion 是 {info}, 但 ROUTE_VERSION 是 {ver}"
                             f" —— 这是旧构建, 换 --src 指向刚打出来的那一份")
    out = os.path.join(base, DIST_DIR, f"WTRouteTester_{ver}_win64.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(src):
            rel_root = os.path.relpath(root, src)
            if rel_root == ".":
                # 顶层目录里只排除 data\: 用户的录制与地图缓存绝不进包
                dirs[:] = [d for d in dirs if d.lower() != "data"]
                rel_root = ""
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.join(rel_root, name) if rel_root else name
                z.write(full, arc)
                n += 1
    print(f"== 打包 {n} 个文件 -> {out}")
    return out


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def get_token() -> str:
    p = subprocess.run(["git", "credential", "fill"],
                       input=b"protocol=https\nhost=github.com\n\n", capture_output=True)
    lines: dict[str, str] = {}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            lines[k] = v
    tok = (lines.get("password") or "").strip()
    if not tok:
        raise SystemExit("未找到 GitHub 凭据(请先对该仓库 git push 一次)")
    return tok


def api(method: str, path: str, token: str,
        body: dict | None = None, raw: bytes | None = None,
        content_type: str = "application/octet-stream") -> dict:
    host = "https://uploads.github.com" if "/assets?" in path else "https://api.github.com"
    req = urllib.request.Request(host + path, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "WTRouteTester")
    req.add_header("Accept", "application/vnd.github+json")
    data = None
    if raw is not None:
        data = raw
        req.add_header("Content-Type", content_type)
    elif body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=900) as r:
            payload = r.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise ValueError(f"HTTP {e.code} {method} {path}: {detail}") from e


def upload(token: str, rid: int, path: str, name: str) -> None:
    size = os.path.getsize(path)
    print(f"== 上传 {name} ({size / 1048576:.1f} MB)…")
    with open(path, "rb") as f:      # 60 MB 左右, 一次读进内存也够, 但走流式更稳
        data = f.read()
    api("POST", f"/repos/{REPO}/releases/{rid}/assets?name={name}&label={name}",
        token, raw=data)


def main() -> int:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw_argv = sys.argv[1:]
    src = ""
    for i, a in enumerate(raw_argv):
        if a == "--src" and i + 1 < len(raw_argv):
            src = raw_argv[i + 1]
    argv = [a for i, a in enumerate(raw_argv)
            if not a.startswith("--") and not (i and raw_argv[i - 1] == "--src")]
    no_zip = "--no-zip" in raw_argv
    ver = read_version(base)
    if not ver:
        raise SystemExit("读不到 ROUTE_VERSION(wt81111g/config.py)")
    tag = argv[0] if argv else f"v{ver}"
    notes_file = argv[1] if len(argv) > 1 else ""

    if no_zip:
        zpath = os.path.join(base, DIST_DIR, f"WTRouteTester_{ver}_win64.zip")
        if not os.path.isfile(zpath):
            raise SystemExit(f"--no-zip 但找不到 {zpath}")
    else:
        zpath = make_zip(base, ver, src or None)
    size = os.path.getsize(zpath) / 1048576
    digest = sha256_of(zpath)

    notes = ""
    if notes_file and os.path.isfile(notes_file):
        with open(notes_file, encoding="utf-8") as f:
            notes = f.read()
    notes += (f"\n### 下载\n\n"
              f"`{os.path.basename(zpath)}`({size:.1f} MB)—— 解压即用, 无需安装; "
              f"包里不含 `data\\`(你的录制与地图缓存, 首次运行自动创建, "
              f"升级时把旧的 `data\\` 整个拷进新目录即可)。\n\n"
              f"SHA256: `{digest}`\n\n"
              f"> 只读取游戏自带的 8111 本地接口, 不注入游戏进程、不读写游戏文件。\n")

    token = get_token()

    # 幂等重建: 已有同名 release / tag 先删掉
    try:
        rel = api("GET", f"/repos/{REPO}/releases/tags/{tag}", token)
        print(f"== 删除已存在的 release {tag}")
        api("DELETE", f"/repos/{REPO}/releases/{rel['id']}", token)
        try:
            api("DELETE", f"/repos/{REPO}/git/refs/tags/{tag}", token)
        except ValueError:
            pass
    except ValueError as e:
        if "HTTP 404" not in str(e):
            raise SystemExit(str(e))

    rel = api("POST", f"/repos/{REPO}/releases", token, body={
        "tag_name": tag,
        "target_commitish": "main",
        "name": f"WTRouteTester {tag}",
        "body": notes,
        "draft": False,
        "prerelease": False,
    })
    print(f"== 已创建 release {tag}: {rel.get('html_url', '')}")
    upload(token, int(rel["id"]), zpath, os.path.basename(zpath))
    print("== 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
