# -*- coding: utf-8 -*-
"""运行环境自检：`python app.py --doctor`

换一台 Windows 机器时先跑这个。它只读不写（除了临时探测文件），任何一项失败都
给出可直接照做的修复建议，而不是抛栈。
"""
from __future__ import annotations

import platform
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import sources as SRC

MIN_PY = (3, 9)
DEPS = [("requests", "requests"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"),
        ("urllib3", "urllib3")]


def _line(name: str, ok: str, detail: str = "") -> tuple:
    return (name, ok, detail)


def check_python() -> tuple:
    v = sys.version_info
    ok = v >= MIN_PY
    return _line("Python 版本", "OK" if ok else "失败",
                 f"{platform.python_version()}（要求 ≥ {'.'.join(map(str, MIN_PY))}）")


def check_deps() -> tuple:
    missing = []
    for mod, pipname in DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pipname)
    if missing:
        return _line("第三方依赖", "失败", "缺 " + ", ".join(missing)
                     + f"  →  pip install {' '.join(missing)}")
    return _line("第三方依赖", "OK", "requests / bs4 / lxml / urllib3")


def check_console() -> tuple:
    enc = (getattr(sys.stdout, "encoding", None) or "?").lower()
    glyph = True
    try:
        "★█⏰".encode(enc)
    except (UnicodeEncodeError, LookupError):
        glyph = False
    note = f"stdout={enc}" + ("" if glyph else "  → 字形将自动降级为 ASCII")
    return _line("控制台编码", "OK" if "utf" in enc or glyph else "提示", note)


def check_writable(d: Path) -> tuple:
    """检查真正会写数据的那个目录（打包成 exe 后它不在程序旁边，见 app.data_dir）。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
        t = d / ".write_test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return _line("数据目录可写", "OK", str(d))
    except OSError as exc:
        return _line("数据目录可写", "失败", f"{type(exc).__name__}: {exc}  → 把整个文件夹移到"
                                            "当前用户有写权限的位置（别放在只读盘或需要管理员的目录）")


def check_port(port: int) -> tuple:
    for p in range(port, port + 12):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return _line("本地端口", "OK", f"127.0.0.1:{p}")
            except OSError:
                continue
    return _line("本地端口", "失败", f"{port} 起连续 12 个都被占用  → 用 --port 指定空闲端口")


def _probe(cfg: dict) -> tuple:
    url = cfg["base"].rstrip("/") + "/"
    s = requests.Session()
    s.trust_env = False          # 与爬虫一致：不吃系统代理
    s.headers.update({"User-Agent": SRC.UA})
    try:
        r = s.get(url, timeout=12, allow_redirects=True, verify=True)
    except requests.exceptions.SSLError:
        if not SRC.insecure_allowed(url):
            return cfg, "失败", "证书校验不通过（非学校域名，不降级）"
        try:
            r = s.get(url, timeout=12, allow_redirects=True, verify=False)
        except Exception as exc:
            return cfg, "失败", type(exc).__name__
    except Exception as exc:
        return cfg, "失败", type(exc).__name__
    if any(m in r.url for m in SRC.AUTH_MARKERS):
        return cfg, "需登录", r.url[:60]
    if len(r.text.strip()) < 260:
        return cfg, "空响应", f"HTTP {r.status_code}"
    return cfg, "OK", f"HTTP {r.status_code}"


def check_sites(workers: int = 8) -> list:
    out = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cfg, state, detail in pool.map(_probe, SRC.SOURCES):
            out.append(_line(cfg["name"], state, detail))
    return out


def run(missing_deps: list, data_dir: Path = None, port: int = 8765) -> int:
    root = Path(__file__).resolve().parent
    data_dir = data_dir or root / "data"
    print("\n" + "=" * 72)
    print("  校园信息工作台 · 环境自检")
    print("=" * 72)
    rows = [check_python(), check_deps(), check_console(),
            check_writable(data_dir), check_port(port)]
    bad = 0
    for name, state, detail in rows:
        if state not in ("OK",):
            bad += 1
        print(f"  [{state:4}] {name:14} {detail}")

    print("\n  站点可达性（走公网直连，不经代理）…\n")
    site_rows = check_sites()
    dead = auth = ok = 0
    for name, state, detail in site_rows:
        if state == "OK":
            ok += 1
        elif state == "需登录":
            auth += 1
        else:
            dead += 1
        print(f"  [{state:4}] {name:22} {detail}")

    print("\n" + "-" * 72)
    print(f"  站点：{ok} 可达 · {auth} 需统一身份认证（自动跳过） · {dead} 不可达")
    print(f"  仅校园网内可达的站点见 sources.UNREACHABLE，外网抓不到属正常。")
    if missing_deps:
        print(f"\n  修复：pip install {' '.join(missing_deps)}")
    print("=" * 72 + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(run([]))
