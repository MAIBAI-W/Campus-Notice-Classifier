# -*- coding: utf-8 -*-
"""校园信息工作台（school-radar）入口：抓取 → 个人相关度排序 → 终端速览 + 本地网页工作台。

    python app.py                  抓取（含详情正文）并起工作台，自动开浏览器
    python app.py --quick          只读列表标题，最快
    python app.py --serve-only     不抓取，直接看上次结果
    python app.py --reclassify     改完规则重算权重，不联网
    python app.py --setup          重新填写个人档案
    python app.py --crawl-only     只抓取并打印终端速览

架构与扩展说明见同目录 README.md。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import crawl
import profile as PF
import sources as SRC
from classify import CATEGORIES, CAT_BY_ID, growth_view

# Windows 终端默认 GBK，中文会乱码；errors=replace 保证即使老版 conhost 不支持
# UTF-8，也只是丢字形而不是抛 UnicodeEncodeError 把程序带崩。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _attach_log_if_headless() -> None:
    """无控制台运行（打包成 --noconsole 的 exe）时，把输出转到日志文件。

    没有窗口就没有任何报错可见，同学遇到"双击没反应"只能干瞪眼。写到数据目录里，
    让他把这一个文件发回来就能定位。
    """
    if not getattr(sys, "frozen", False):
        return
    if getattr(sys.stdout, "encoding", None):        # 有真控制台，什么都不做
        return
    try:
        log = data_dir() / "school-radar.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log, "a", encoding="utf-8", buffering=1)
        handle.write(f"\n===== 启动 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        sys.stdout = sys.stderr = handle
    except OSError:
        pass


def _can(s: str) -> bool:
    """当前终端能否安全输出这些字形（★ █ ⏰ ·）。不能就退回 ASCII。

    无控制台打包（--noconsole）时 sys.stdout 是 PyInstaller 的 NullWriter，
    连 encoding 属性都没有，这里不能假设自己在终端里。
    """
    try:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        s.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError, TypeError, ValueError):
        return False


GLYPH_STAR, GLYPH_BAR, GLYPH_DUE = ("★", "█", "⏰") if _can("★█⏰") else ("*", "#", "!")

def app_root() -> Path:
    """程序所在目录。

    打包成 exe 后 `__file__` 指向 PyInstaller 的临时解包目录（每次运行都换位置），
    数据必须落在 exe 旁边，否则同学看到的永远是"每次都要重新抓"。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """可写数据目录：优先 exe 旁边的 data/，不可写（比如装进 Program Files）就退回
    %LOCALAPPDATA%\\school-radar。开发态一直用项目里的 data/，方便直接看库、删库重跑。"""
    root = app_root()
    if not getattr(sys, "frozen", False):
        return root / "data"
    cand = root / "data"
    try:
        cand.mkdir(parents=True, exist_ok=True)
        probe = cand / ".write-test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return cand
    except OSError:
        local = os.environ.get("LOCALAPPDATA") or str(root)
        return Path(local) / "school-radar"


ROOT = app_root()
DATA = data_dir()
_attach_log_if_headless()          # 要在 data_dir() 之后：日志写在数据目录里
DB = DATA / "radar.db"
PROFILE = DATA / "profile.json"
PORT = 8765

_state = {"crawling": False, "started": "", "report": {}, "total": 0, "port": 0}


def check_deps() -> list[str]:
    """启动前检查第三方依赖，缺了就给可直接粘贴的安装命令，而不是抛 ImportError。"""
    missing = []
    for mod, pipname in (("requests", "requests"), ("bs4", "beautifulsoup4"), ("lxml", "lxml")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pipname)
    return missing


def existing_instance(start: int = PORT, tries: int = 12) -> str:
    """本机是否已经有一个工作台在跑；有就返回它的地址。

    无控制台的 exe 双击后短时间内看不出反应，同学就会连着点好几次。每个实例都会另占
    一个端口、各自起一轮抓取，既慢又开始互相写同一个库。所以先把"已经在跑"探测出来，
    直接把浏览器叫过去，不再起第二个。
    """
    from urllib.request import urlopen

    for p in range(start, start + tries):
        try:
            with urlopen(f"http://127.0.0.1:{p}/api/alive", timeout=0.35) as r:
                if json.loads(r.read().decode("utf-8", "replace")).get("ok"):
                    return f"http://127.0.0.1:{p}"
        except Exception:
            continue
    return ""


def free_port(start: int, tries: int = 12) -> int:
    """端口被占用就往后找一个能绑的，避免"第二次启动直接崩"。"""
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def conn_db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    # busy_timeout：另一个实例正在写库时等 5 秒而不是立刻报 database is locked
    conn = crawl.open_db(str(DB))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return conn


def last_run(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT finished FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return row["finished"] if row else ""


def do_crawl(detail: bool = True) -> int:
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _state.update(crawling=True, started=started)
    try:
        conn = conn_db()
        known = {r[0] for r in conn.execute("SELECT ckey FROM items")}
        rows, report = crawl.crawl_all(want_detail=detail, known=known)
        n = crawl.save_items(conn, rows)
        crawl.log_run(conn, started, n, detail, report)
        conn.close()
        _state.update(report=report, total=n)
        return n
    finally:
        _state["crawling"] = False


# ---------------- 个人相关度装载 ----------------

# /api/items 每条只发这些字段：全部都能在 PAGE 里找到 `it.<字段>` 引用。
# 要加新字段时，先确认前端真的会用，否则等于给每台机器多压几 KB。
ITEM_FIELDS = ("url", "source_name", "title", "digest", "pub_date", "category_name", "color",
               "hits", "audience", "doc_type", "is_notice", "files", "method", "score",
               "is_recent", "suggest", "suggest_name", "p_score", "why")


def build_payload(qs: dict) -> dict:
    days = max(0, min(3650, _to_int(qs.get("days", ["0"])[0], 0)))
    q = qs.get("q", [""])[0]
    category = qs.get("cat", [""])[0]
    source = qs.get("source", [""])[0]
    audience = qs.get("aud", [""])[0]
    doc_type = qs.get("type", [""])[0]
    only_unread = qs.get("unread", ["0"])[0] == "1"
    sort = qs.get("sort", ["personal"])[0]

    prof = PF.load(PROFILE)
    conn = conn_db()
    items = crawl.load_items(conn, days=days, only_unread=only_unread, category=category,
                             source=source, q=q, audience=audience, doc_type=doc_type)
    # 侧栏计数用"分面"算法：类目轨忽略类目自身、学院轨忽略学院自身，
    # 否则选中某个类目时其他类目计数会归零，轨道内容就会乱跳。
    # 这两遍只为数数，折叠只看 (title, source)，所以只查这三列——同一套 WHERE
    # 与 ORDER BY，折叠结果与全列查询逐条一致。
    FACET_COLS = "title, source, category"
    cat_rows = crawl.load_items(conn, days=days, only_unread=only_unread, source=source,
                                q=q, audience=audience, doc_type=doc_type, cols=FACET_COLS)
    src_rows = crawl.load_items(conn, days=days, only_unread=only_unread, category=category,
                                q=q, audience=audience, doc_type=doc_type, cols=FACET_COLS)
    lr = last_run(conn)
    total_all = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    conn.close()

    from collections import Counter
    cat_n = Counter(r["category"] for r in cat_rows)
    src_n = Counter(r["source"] for r in src_rows)

    year = date.today().year
    for it in items:
        # 模型给的是类目 id，界面要显示中文名
        sid = it.get("suggest") or ""
        it["suggest_name"] = CAT_BY_ID[sid]["name"] if sid in CAT_BY_ID else ""
        ps, why = PF.personalize(it, prof, year)
        it["p_score"] = ps
        it["why"] = why
    # 注意：这里**不要**裁剪字段。build_payload 同时喂给网页和终端速览
    # （print_panel 要用 category 做成长路径聚合），裁剪只能发生在写给浏览器的那一刻，
    # 见 Handler._api_get 里的 wire_items。
    if sort == "score":
        items.sort(key=lambda x: -x["score"])
    elif sort == "date":
        # 原来这里是 x["pub_date"] 升序，等于把 2015 年的通知排在最前面，
        # 而分组标题的顺序又跟着条目顺序走，于是"近 30 天"被压到了列表底部。
        items.sort(key=lambda x: x["pub_date"] or "", reverse=True)
    else:
        items.sort(key=lambda x: -x["p_score"])
    pmax = max([i["p_score"] for i in items], default=0)

    # 顺序固定按 CATEGORIES / SOURCES 的声明顺序，永不随计数重排
    ov = [{"id": c["id"], "name": c["name"], "color": c["color"], "n": cat_n.get(c["id"], 0)}
          for c in CATEGORIES]
    colleges = [{"source": c["key"], "name": c["name"], "kind": c["kind"],
                 "n": src_n.get(c["key"], 0)} for c in SRC.SOURCES]
    return {"items": items, "overview": ov, "colleges": colleges, "max_p": pmax,
            "days": days,          # 把生效窗口回传：前端不许拿下拉框的"当前值"当依据
            "max_score": max([i["score"] for i in items], default=0), "total_all": total_all,
            "profile": PF.as_dict(prof), "last_run": lr or _state["started"],
            "crawling": _state["crawling"], "report": _state["report"],
            "unreachable": SRC.UNREACHABLE, "categories": [
                {"id": c["id"], "name": c["name"], "color": c["color"]} for c in CATEGORIES]}


# ---------------- 终端速览 ----------------

def _bar(score: float, top: float) -> str:
    width = 16
    filled = 0 if top <= 0 else int(round(width * min(1.0, score / top)))
    return GLYPH_BAR * filled + "·" * (width - filled)


def print_panel(items: list[dict], prof: PF.Profile) -> None:
    if not items:
        print("库里还没有数据，先跑一次抓取（去掉 --serve-only）。")
        return
    who = " / ".join(x for x in [prof.college_name, f"{prof.grade}级" if prof.grade else "",
                                 prof.identity, prof.major] if x)
    print("\n" + "=" * 78)
    print(f"  校园信息工作台 · {datetime.now():%Y-%m-%d %H:%M} · {len(items)} 条"
          + (f" · 档案：{who}" if who else " · 未设置档案（--setup）"))
    print("=" * 78)
    print("  成长路径  " + "  ".join(f"{g['name']}:{g['n']}" for g in growth_view(
        [type("V", (), {"primary": i["category"], "score": i["p_score"],
                        "is_recent": bool(i["is_recent"])})() for i in items]) if g["n"]))

    order = sorted(CATEGORIES, key=lambda c: -max(
        [i["p_score"] for i in items if i["category"] == c["id"]] or [0]))
    for cat in order:
        rows = sorted([i for i in items if i["category"] == cat["id"]],
                      key=lambda x: -x["p_score"])
        if not rows:
            continue
        top = max(i["p_score"] for i in items)
        print("\n" + "-" * 78)
        print(f"▌{cat['name']}  {len(rows)} 条")
        for it in rows[:5]:
            flag = GLYPH_STAR if it.get("is_recent") else " "
            why = f" ({'、'.join(it['why'])})" if it.get("why") else ""
            print(f"  {flag} [{_bar(it['p_score'], top)}] {it['p_score']:6.1f}  "
                  f"{it['digest'][:44]}{why}")
            print(f"     └ {it['source_name']} · {it['pub_date'] or '日期未标注'}"
                  f" · 受众 {it['audience'].replace('|', ' ') or '未识别'}")
    print("=" * 78 + "\n")


# ---------------- 网页工作台 ----------------

PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>校园信息工作台 · 浙江财经大学东方学院</title>
<style>
:root{
  --bg:#f5f6f8; --card:#fff; --ink:#111418; --muted:#697280; --line:#e8eaee;
  --accent:#2f6fed; --warn:#d9480f; --ok:#2b8a3e; --shadow:0 1px 2px rgba(16,24,40,.04),0 6px 20px rgba(16,24,40,.05);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
.top{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.92);backdrop-filter:blur(10px);
 border-bottom:1px solid var(--line)}
.tin{max-width:1320px;margin:0 auto;padding:12px 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px;letter-spacing:.2px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(47,111,237,.15)}
.chip{display:inline-flex;align-items:center;gap:6px;background:#fff;border:1px solid var(--line);
 border-radius:999px;padding:4px 11px;font-size:12px;color:var(--muted);cursor:pointer}
.chip:hover{border-color:#c9d2e3;color:var(--ink)}
.grow{flex:1;min-width:180px}
input[type=search],select{border:1px solid var(--line);background:#fff;border-radius:9px;padding:8px 11px;
 font-size:13px;color:var(--ink);font-family:inherit;outline:none}
input[type=search]{width:100%}
input[type=search]:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,111,237,.12)}
.btn.quit{color:#c92a2a;border-color:#f1c9c9}
.btn.quit:hover{background:#fff5f5}
.btn{border:1px solid var(--line);background:#fff;border-radius:9px;padding:8px 13px;font-size:13px;
 cursor:pointer;color:var(--ink);font-family:inherit}
.btn:hover{border-color:#c9d2e3}
.btn.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.pri:hover{background:#2560d8}
.btn.on{background:#eaf1ff;border-color:#b9d0ff;color:var(--accent)}
.seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#fff}
.seg button{border:0;background:transparent;padding:8px 14px;font-size:13px;cursor:pointer;
 color:var(--muted);font-family:inherit;white-space:nowrap}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on{background:var(--accent);color:#fff;font-weight:600}
.sect{border:1px solid var(--line);border-radius:12px;padding:13px 14px;margin-bottom:13px;background:#fbfcfe}
.sect h5{margin:0 0 3px;font-size:13px;font-weight:650}
.sect p{margin:0 0 9px;font-size:12px;color:var(--muted);line-height:1.6}
.rad{display:flex;align-items:center;gap:8px;font-size:13px;color:#414855;padding:4px 0;cursor:pointer}
.rad input{accent-color:var(--accent);width:15px;height:15px;margin:0}
.wrap{max-width:1320px;margin:0 auto;padding:18px 20px 60px;display:grid;grid-template-columns:236px 1fr;gap:20px}
.rail{position:sticky;top:74px;align-self:start;display:flex;flex-direction:column;gap:14px;
 max-height:calc(100vh - 92px);overflow-y:auto;overscroll-behavior:contain;padding-right:2px}
.rail::-webkit-scrollbar{width:6px}.rail::-webkit-scrollbar-thumb{background:#dfe3ea;border-radius:3px}
.box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px;box-shadow:var(--shadow)}
.box h4{margin:4px 8px 8px;font-size:11px;color:var(--muted);letter-spacing:.8px;font-weight:600}
.li{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:9px;cursor:pointer;
 color:#414855;font-size:13px}
.li:hover{background:#f6f7fa}
.li.sel{background:#eff4ff;color:var(--accent);font-weight:600}
.li i{width:8px;height:8px;border-radius:50%;flex:none}
.li b{margin-left:auto;font-size:11px;font-weight:600;color:var(--muted)}
.li.sel b{color:var(--accent)}
.li.dim{opacity:.42}
.li.dim:hover{opacity:.85}
.day{margin:22px 0 9px;font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.6px}
.day:first-child{margin-top:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:10px;
 display:grid;grid-template-columns:62px 1fr;gap:14px;box-shadow:var(--shadow);position:relative;overflow:hidden}
.card:hover{border-color:#d4dbe8}
.card.fresh::after{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--cat)}
.ring{align-self:start;text-align:center}
.ring .v{font-size:17px;font-weight:700;letter-spacing:-.3px}
.ring .l{font-size:10px;color:var(--muted)}
.meter{height:4px;border-radius:3px;background:#eef0f4;margin-top:6px;overflow:hidden}
.meter i{display:block;height:100%;border-radius:3px}
h3{margin:0 0 4px;font-size:15px;font-weight:650;line-height:1.5;cursor:pointer}
h3:hover{color:var(--accent)}
.dg{color:#4b5563;font-size:13px;margin-bottom:9px}
.tags{display:flex;flex-wrap:wrap;gap:6px;font-size:11.5px}
.t{border:1px solid var(--line);border-radius:7px;padding:2px 8px;color:#5a6472;background:#fbfbfd}
.t.cat{border-color:transparent;color:#fff;background:var(--cat);font-weight:600}
.t.why{border-color:#ffe0b3;background:#fff7ed;color:#9a5b10}
.t.dl{border-color:#ffc9c9;background:#fff5f5;color:var(--warn);font-weight:600}
.t.old{color:#98a1b0}
.t.hit{border-style:dashed;color:#7a8496;cursor:help}
.t.hit:hover{color:var(--accent);border-color:var(--accent)}
.t.sug{border-color:#ffd8a8;background:#fff9f0;color:#a15c07;cursor:pointer;font-weight:600}
.t.sug:hover{background:#fff2df;border-color:#f59f00}
.t.meth{border-color:#d0ebff;background:#f3faff;color:#1864ab}
.t.typen{border-color:#bfd8ff;background:#f2f7ff;color:#1b4f9c;font-weight:600}
.t.typeo{border-color:var(--line);background:#fbfbfd;color:#8a93a3}
.loading{text-align:center;color:var(--muted);padding:60px 0}
.none{text-align:center;color:var(--muted);padding:70px 0;background:var(--card);border:1px dashed var(--line);border-radius:14px}
.foot{max-width:1320px;margin:0 auto;padding:0 20px 40px;color:var(--muted);font-size:12px;line-height:1.9}
.mask{position:fixed;inset:0;background:rgba(17,20,24,.38);display:none;align-items:center;justify-content:center;z-index:50;padding:20px}
.mask.on{display:flex}
.modal{background:#fff;border-radius:16px;width:min(520px,100%);max-height:88vh;overflow:auto;padding:22px;box-shadow:0 20px 60px rgba(0,0,0,.22)}
.modal h2{margin:0 0 3px;font-size:17px}
.modal p.sub{margin:0 0 16px;color:var(--muted);font-size:12.5px}
.fld{margin-bottom:13px}
.fld label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;font-weight:600}
.fld input,.fld select{width:100%;border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:13px;
 font-family:inherit;background:#fff;color:var(--ink);outline:none}
.fld input:focus,.fld select:focus{border-color:var(--accent)}
.prefs{display:flex;flex-wrap:wrap;gap:7px}
.pref{border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12.5px;cursor:pointer;user-select:none}
.pref.on{background:#eff4ff;border-color:#b9d0ff;color:var(--accent);font-weight:600}
.pref .n{font-size:10px;opacity:.65;margin-left:4px}
.mrow{display:flex;gap:9px;justify-content:flex-end;margin-top:18px}
.fld input[type=checkbox]{width:15px;height:15px;padding:0;border:none;accent-color:var(--accent)}
.chk{display:flex;align-items:center;gap:8px;font-size:13px;color:#414855;cursor:pointer}
.warnbar{background:#fff8e6;border:1px solid #ffe1a6;color:#8a5a00;border-radius:12px;padding:10px 14px;
 font-size:12.5px;margin-bottom:14px}
.scope{background:#eef4ff;border:1px solid #d5e3ff;color:#2c4a7c;border-radius:12px;padding:9px 14px;
 font-size:12.5px;margin-bottom:14px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.scope a{color:#1c7ed6;cursor:pointer;text-decoration:underline;font-weight:600}
@media(max-width:900px){.wrap{grid-template-columns:1fr}.rail{position:static;flex-direction:row;flex-wrap:wrap;
 max-height:none;overflow:visible}
 .box{flex:1;min-width:220px}}
</style></head><body>
<div class="top"><div class="tin">
  <div class="brand"><span class="dot"></span>校园信息工作台</div>
  <div class="seg" id="mode"><button data-m="me" class="on" onclick="setMode('me')">我的视角</button><button data-m="all" onclick="setMode('all')">全部数据</button></div>
  <span class="chip" id="pchip" onclick="openCfg()">设置档案</span>
  <div class="grow"><input type="search" id="q" placeholder="搜索标题、正文、关键词（按 / 聚焦）" oninput="deb()"></div>
  <select id="days" onchange="load()">
    <option value="0">全部时间</option><option value="3">近 3 天</option>
    <option value="7" selected>近 7 天</option><option value="30">近 30 天</option></select>
  <select id="sort" onchange="load()">
    <option value="personal">按个人相关度</option><option value="score">按全局权重</option>
    <option value="date">按发布日期</option></select>
  <button class="btn on" id="un" onclick="tgUn()">只看未读</button>
  <button class="btn" onclick="openCfg()">设置</button>
  <button class="btn pri" id="cz" onclick="doCrawl()">重新抓取</button>
  <button class="btn quit" id="qt" onclick="quitApp()" title="停止后台服务并退出程序">退出</button>
</div></div>

<div class="wrap">
  <div class="rail">
    <div class="box"><h4>成长路径</h4><div id="cats"></div></div>
    <div class="box"><h4>学院 / 部门</h4><div id="cols"></div></div>
  </div>
  <div><div id="warn"></div><div id="scope"></div><div id="list"><div class="loading">正在载入…</div></div></div>
</div>
<div class="foot" id="foot"></div>

<div class="mask" id="mask" onclick="if(event.target===this)closeCfg()"><div class="modal">
  <h2>个人档案</h2>
  <p class="sub">只存在本机 <code>data/profile.json</code>，用于把全校通知折算成"跟你有关的顺序"。学院从列表里选，不用手打。</p>
  <div class="fld"><label>学院</label><select id="f_college"></select></div>
  <div class="fld"><label>年级（入学年份）</label><select id="f_grade"></select></div>
  <div class="fld"><label>身份</label><select id="f_identity">
    <option>本科生</option><option>专升本</option><option>教师/辅导员</option><option>校友</option></select></div>
  <div class="fld"><label>专业</label><input id="f_major" placeholder="如：会计学（可留空）"></div>
  <div class="sect">
    <h5>资助对象认定</h5>
    <p>这一项单独成块，因为它只影响奖助学金、勤工助学、困难补助这类通知的排序：认定过会优先看到，未认定则适度靠后。</p>
    <label class="rad"><input type="radio" name="funded" value="1"> 我是学校认定的资助对象</label>
    <label class="rad"><input type="radio" name="funded" value="0"> 不是 / 尚未认定</label>
  </div>
  <div class="mrow"><button class="btn" onclick="closeCfg()">取消</button>
    <button class="btn pri" onclick="saveCfg()">保存</button></div>
</div></div>

<script>
let S={cat:'',source:'',aud:'',unreadOnly:true,timer:0,tick:0,mode:localStorage.getItem('sr-mode')||'me'};
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function deb(){clearTimeout(S.timer);S.timer=setTimeout(load,250)}
function tgUn(){S.unreadOnly=!S.unreadOnly;document.getElementById('un').classList.toggle('on',S.unreadOnly);load()}
function setDays(v){const e=document.getElementById('days');e.value=String(v);load()}
function setMode(m){
  S.mode=m; localStorage.setItem('sr-mode',m);
  document.querySelectorAll('#mode button').forEach(b=>b.classList.toggle('on',b.dataset.m===m));
  const all=m==='all';
  // 全部数据 = 不加任何筛选与个性化：清掉类目/学院/受众/关键词，放开时间与未读
  if(all){S.cat='';S.source='';S.aud='';document.getElementById('q').value='';
    document.getElementById('days').value='0';S.unreadOnly=false;}
  document.getElementById('days').disabled=all;
  document.getElementById('un').style.display=all?'none':'';
  if(!all)S.unreadOnly=true;
  document.getElementById('un').classList.toggle('on',S.unreadOnly);
  load();
}
function pick(k,v){S[k]=v;load()}
function bucket(d){
  if(!d) return '未标注日期';
  const t=new Date(), p=new Date(d+'T00:00:00'), n=(t-p)/86400000;
  if(n<0) return '即将';                 // 双选会/宣讲会的举办日在未来，要置顶不要埋
  if(n<1) return '今天';
  if(n<=3) return '近 3 天';
  if(n<=7) return '近 7 天';
  if(n<=30) return '近 30 天';
  return '更早';
}
async function load(){
  const all=S.mode==='all';
  let sort=document.getElementById('sort').value;
  if(all&&sort==='personal')sort='score';
  const p=new URLSearchParams({days:all?0:document.getElementById('days').value,
    q:document.getElementById('q').value,cat:S.cat,source:S.source,aud:S.aud,
    unread:(all?false:S.unreadOnly)?1:0,sort:sort});
  let d; try{ d=await (await fetch('/api/items?'+p)).json(); }catch(e){
    document.getElementById('list').innerHTML='<div class="none">读不到数据，服务可能已停止</div>'; return; }
  window.D=d; render(d);
}
function render(d){
  const pr=d.profile;
  document.getElementById('pchip').textContent = pr.onboarded?
    [pr.college_name,pr.grade?pr.grade+'级':'',pr.identity,pr.major].filter(Boolean).join(' · ') : '设置档案';
  const cats=[{id:'',name:'全部',color:'#98a1b0',n:d.overview.reduce((a,b)=>a+b.n,0)}]
    .concat(d.overview);
  document.getElementById('cats').innerHTML=cats.map(c=>
    `<div class="li ${S.cat===c.id?'sel':''} ${(!c.n&&S.cat!==c.id)?'dim':''}" onclick="pick('cat','${c.id}')">
      <i style="background:${c.n?c.color:'#c9ced8'}"></i><span>${esc(c.name)}</span><b>${c.n}</b></div>`).join('');
  const cols=[{source:'',name:'全部来源',n:d.colleges.reduce((a,b)=>a+b.n,0)}].concat(d.colleges);
  document.getElementById('cols').innerHTML=cols.map(c=>
    `<div class="li ${S.source===c.source?'sel':''} ${(!c.n&&S.source!==c.source)?'dim':''}" onclick="pick('source','${c.source}')">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.name)}</span><b>${c.n}</b></div>`).join('');
  // 抓取中就每 3 秒自己刷一次，抓完自动回到 5 分钟；同学不用手动点、也不该去点
  clearInterval(S.tick); S.tick=setInterval(load, d.crawling?3000:300000);
  const bad=d.report?Object.values(d.report).filter(v=>v.status&&v.status!=='ok'):[];
  document.getElementById('warn').innerHTML = d.crawling?
    '<div class="warnbar"><b>正在抓取 22 个站点</b>，第一次约 1–3 分钟。这一页会自动刷新，不用重复打开程序。</div>':
    (bad.length?'<div class="warnbar">本轮 '+bad.length+' 个来源异常：'+esc(bad.map(v=>v.name+'('+v.status+')').join('、'))+'</div>':'');
  // 默认只看近 7 天，第一次装的人会把"54 条"理解成"只抓到 54 条"（真实反馈）。
  // 窗口用后端回传的 d.days，不读下拉框当前值：异步刷新时两者会错位。
  const win=d.days|0, WINLAB={0:'全部时间',3:'近 3 天',7:'近 7 天',30:'近 30 天'};
  document.getElementById('scope').innerHTML=
    (win&&d.total_all>d.items.length)?
      '<span>当前窗口：<b>'+(WINLAB[win]||('近 '+win+' 天'))+'</b>，'+d.items.length+' 条</span>'+
      '<span>库里共 <b>'+d.total_all+'</b> 条，还有 '+(d.total_all-d.items.length)+' 条更早的没显示</span>'+
      '<a onclick="setDays(30)">放宽到近 30 天</a><a onclick="setDays(0)">看全部</a>':'';
  if(!d.items.length){document.getElementById('list').innerHTML=
    d.crawling? '<div class="none"><b>首次抓取中…</b><br><small>抓完会自动出现，请留这一页开着等一会儿。'
              + '双击 exe 时看不到进度是正常的：程序没有命令行窗口，页面就是它的进度条。</small></div>'
    : d.total_all? '<div class="none">没有符合条件的通知<br><small>试试放宽时间范围或去掉"只看未读"</small></div>'
                 : '<div class="none">这台机器还没有任何数据<br><small>点右上角<b>重新抓取</b>，第一次约 1–3 分钟，抓完自动显示。之后每次打开都是上次的结果。</small></div>';return}
  const groups={},BORDER=['即将','今天','近 3 天','近 7 天','近 30 天','更早','未标注日期'];
  d.items.forEach(it=>{const b=bucket(it.pub_date);(groups[b]=groups[b]||[]).push(it)});
  // 分组标题顺序固定"新→旧"，不跟条目排序走。之前是"按条目出现的先后"临时决定组序，
  // 于是按相关度排时组序是乱的、按日期排时"近 30 天"被压到列表底部。
  const order=BORDER.filter(b=>groups[b]).concat(Object.keys(groups).filter(b=>!BORDER.includes(b)));
  const all=S.mode==='all';
  const mx=(all?d.max_score:d.max_p)||1;
  document.getElementById('list').innerHTML=order.map(g=>
    `<div class="day">${esc(g)} · ${groups[g].length} 条</div>`+groups[g].map(it=>card(it,mx,all)).join('')).join('');
  document.getElementById('foot').innerHTML =
    (all?'当前为<b>全部数据</b>视图：不做个性化推荐、不带任何筛选。':'')+
    '共 '+d.total_all+' 条入库 · 当前显示 '+d.items.length+' 条 · 最近抓取 '+esc(d.last_run||'—')+
    '<br>外网不可达（需校园网）：'+esc(Object.values(d.unreachable).join('、'))+
    ' ；教务栏目列表页与部分内网系统需统一身份认证，已自动跳过。';
}
function card(it,mx,all){
  const val=all?it.score:it.p_score;
  const pct=Math.round(val/mx*100);
  const aud=(it.audience||'').split('|').filter(Boolean);
  const why=all?'':(it.why||[]).map(w=>`<span class="t why">${esc(w)}</span>`).join('');
  // 模型只建议不改判：疑似类目做成可点击的"采纳"，一次修正即写入训练集
  const sug=(it.suggest_name&&it.suggest_name!==it.category_name)?
    `<span class="t sug" onclick="adopt(event,'${encodeURIComponent(it.url)}','${esc(it.suggest)}')">疑似 ${esc(it.suggest_name)} · 采纳</span>`:'';
  const meth=(it.method&&it.method!=='规则')?`<span class="t meth">${esc(it.method)}</span>`:'';
  return `<div class="card ${it.is_recent?'fresh':''}" style="--cat:${esc(it.color)}">
    <div class="ring"><div class="v" style="color:${esc(it.color)}">${val.toFixed(1)}</div>
      <div class="l">${all?'权重':'相关度'}</div><div class="meter"><i style="width:${pct}%;background:${esc(it.color)}"></i></div></div>
    <div><h3 onclick="open2('${encodeURIComponent(it.url)}',this)">${esc(it.title)}</h3>
      <div class="dg">${esc(it.digest)}</div>
      <div class="tags"><span class="t cat">${esc(it.category_name)}</span>
        <span class="t ${it.is_notice?'typen':'typeo'}">${esc(it.doc_type||'其他')}</span>${sug}${meth}${why}
        <span class="t">${esc(it.source_name)}</span><span class="t ${it.pub_date?'':'old'}">${esc(it.pub_date||'日期未标注')}</span>
        ${aud.map(a=>`<span class="t">${esc(a)}</span>`).join('')}
        ${it.files?`<span class="t" title="${esc(it.files)}">附件 ${esc(it.files.split(' ').slice(0,2).join(' '))}</span>`:''}
        ${all?'':`<span class="t">全局权重 ${it.score.toFixed(1)}</span>`}
        ${(it.hits||'').split('|').filter(Boolean).slice(0,4).map(h=>{
            const i=h.indexOf(':'); const name=i>0?h.slice(0,i):h, words=i>0?h.slice(i+1):'';
            return `<span class="t hit" ${words?`title="命中表述：${esc(words)}"`:''}>${esc(name)}</span>`;
        }).join('')}
      </div></div></div>`;
}
function open2(u,el){const t=decodeURIComponent(u);
  if(!/^https?:\/\//i.test(t))return;            // 只开 http(s)，别的协议一律不碰
  fetch('/api/read?url='+u);el.closest('.card').classList.remove('fresh');
  window.open(t,'_blank')}
async function adopt(ev,u,cat){
  ev.stopPropagation();
  await fetch('/api/correct',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:decodeURIComponent(u),category:cat})});
  load();
}
async function doCrawl(){
  const b=document.getElementById('cz');b.disabled=true;b.textContent='抓取中…';
  await fetch('/api/refresh');
  const t=setInterval(async()=>{
    try{const d=await (await fetch('/api/items?days='+document.getElementById('days').value+
      '&q=&cat=&source=&aud=&unread=0&sort=personal')).json();
      if(!d.crawling){clearInterval(t);b.disabled=false;b.textContent='重新抓取';S.unreadOnly=true;
        document.getElementById('un').classList.add('on')}
      window.D=d;render(d);}catch(e){}
  },4000);
}
async function quitApp(){
  // 关标签页不算退出：服务一直在后台跑，还会锁住自己目录里的文件。
  const busy=(window.D&&window.D.crawling)?'（正在抓取，现在退出会中断本轮）':'';
  if(!confirm('确定退出校园信息工作台'+busy+'？\n\n退出后这个网页就没用了，需要重新双击程序才能再打开。'))return;
  const b=document.getElementById('qt');b.disabled=true;b.textContent='退出中…';
  try{ await fetch('/api/quit',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
  document.getElementById('list').innerHTML=
    '<div class="none"><b>已退出。</b><br><small>这个标签页可以关了。要再用的话，重新双击 school-radar.exe。</small></div>';
  b.textContent='已退出';
}
function fillCfg(){
  const d=window.D, pr=d.profile, y=new Date().getFullYear();
  document.getElementById('f_college').innerHTML='<option value="">未指定</option>'+
    d.colleges_all.map(c=>`<option value="${c.key}" ${pr.college===c.key?'selected':''}>${esc(c.name)}</option>`).join('');
  const gs=[];for(let i=0;i<6;i++)gs.push(String(y-i));
  document.getElementById('f_grade').innerHTML='<option value="">未指定</option>'+
    gs.map(g=>`<option value="${g}" ${pr.grade===g?'selected':''}>${g} 级</option>`).join('');
  document.getElementById('f_identity').value=pr.identity||'本科生';
  document.getElementById('f_major').value=pr.major||'';
  const want=pr.funded?'1':'0';
  document.querySelectorAll('input[name=funded]').forEach(r=>{r.checked=(r.value===want)});
}
function openCfg(){fillCfg();document.getElementById('mask').classList.add('on')}
function closeCfg(){document.getElementById('mask').classList.remove('on')}
async function saveCfg(){
  const body={college:document.getElementById('f_college').value,
    grade:document.getElementById('f_grade').value,identity:document.getElementById('f_identity').value,
    major:document.getElementById('f_major').value,
    funded:document.querySelector('input[name=funded]:checked').value==='1',
    onboarded:true};
  const r=await (await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})).json();
  closeCfg();
  window.D.profile=Object.assign(window.D.profile,r.profile);
  render(window.D);
}
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&document.activeElement.tagName!=='INPUT'){e.preventDefault();document.getElementById('q').focus()}
  if(e.key==='Escape')closeCfg();
});
setMode(S.mode); setInterval(()=>{if(!document.getElementById('cz').disabled)load()},300000);
</script></body></html>"""


IDENTITIES = ("本科生", "专升本", "教师/辅导员", "校友")
CAT_IDS = {c["id"] for c in CATEGORIES}
SRC_KEYS = {s["key"] for s in SRC.SOURCES}


def _to_int(v, default: int = 0) -> int:
    """把外部传进来的数字安全取整：手打的、扫描器乱撞的，都不该让服务抛 500。"""
    try:
        return int(str(v).strip() or default)
    except (TypeError, ValueError):
        return default


def apply_profile(body: dict, cur: PF.Profile) -> PF.Profile:
    """只接受类型和取值都正确的档案字段，其余静默忽略。

    档案是要落盘的：一旦写进 prefs="xxx" 这类脏值，personalize() 会对每条通知抛异常，
    整个列表页从此 500，而且只能手动删 data/profile.json 才能恢复——对刚拿到工具的同学
    来说那就是"打不开"。
    """
    college = body.get("college")
    if isinstance(college, str) and (college in SRC_KEYS or college == ""):
        cur.college = college
    grade = body.get("grade")
    # 只认 20xx 的入学年份：9999 这种四位数会推出荒谬的毕业年，把"你是不是毕业班"整个带偏
    if isinstance(grade, str) and (grade == "" or
                                   (len(grade) == 4 and grade.isdigit() and 2000 <= int(grade) <= date.today().year + 6)):
        cur.grade = grade
    if body.get("identity") in IDENTITIES:
        cur.identity = body["identity"]
    for key in ("major", "nickname"):
        val = body.get(key)
        if isinstance(val, str):
            setattr(cur, key, val.strip()[:40])
    if isinstance(body.get("prefs"), list):
        prefs = [p for p in body["prefs"] if isinstance(p, str) and p in CAT_IDS]
        if prefs:
            cur.prefs = prefs
            cur.prefs_narrowed = len(prefs) < len(CAT_IDS - {"other"})
    for key in ("funded", "onboarded", "prefs_narrowed"):
        if isinstance(body.get(key), bool):
            setattr(cur, key, body[key])
    # 学院名与 key 必须同步，否则清空学院后会残留旧名字
    cur.college_name = next((c["name"] for c in SRC.SOURCES if c["key"] == cur.college), "")
    cur.study_years = 2 if cur.identity == "专升本" else 4
    return PF.derive_grad_year(cur)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _same_origin(self) -> bool:
        """挡掉别的网页发来的请求。

        服务只听 127.0.0.1，但浏览器里任何外网页面都能对 127.0.0.1 发请求：没有这道
        检查，同学随手点开的某个站点就能改他的档案、给他发起一轮抓取。
        Origin/Referer 缺失时放行（curl、本机脚本没有跨站上下文）。
        """
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        try:
            netloc = urlparse(origin).netloc
        except ValueError:
            return False
        return bool(netloc) and netloc == (self.headers.get("Host") or "")

    def _bytes(self, raw, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", code)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            self._bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not parsed.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._same_origin():
            self._json({"error": "cross-origin"}, 403)
            return
        try:
            self._api_get(parsed.path, qs)
        except Exception as exc:                       # 单个请求出错不该留下裸堆栈和断连
            self._json({"error": type(exc).__name__}, 500)
            raise

    def _api_get(self, path, qs):
        if path == "/api/alive":
            # 给"是不是已经有一个实例在跑"探测用：重复双击 exe 时不再叠进程，
            # 而是直接把浏览器叫到已经在跑的那个地址上。
            self._json({"ok": True, "port": _state.get("port", 0),
                        "crawling": bool(_state["crawling"])})
            return
        if path == "/api/items":
            payload = build_payload(qs)
            # 裁剪只发生在写给浏览器这一刻：body 等 13 个字段前端一处都没引用，
            # "全部时间"视图的响应从 1306 KB 降到 536 KB（实测）。
            payload["items"] = [{k: it[k] for k in ITEM_FIELDS if k in it}
                                for it in payload["items"]]
            payload["colleges_all"] = [{"key": c["key"], "name": c["name"]} for c in SRC.SOURCES]
            self._json(payload)
            return
        if path == "/api/refresh":
            if not _state["crawling"]:
                threading.Thread(target=do_crawl, kwargs={"detail": True}, daemon=True).start()
            self._json({"started": True})
            return
        if path == "/api/read":
            url = qs.get("url", [""])[0][:600]
            val = _to_int(qs.get("v", ["1"])[0], 1)
            conn = conn_db()
            crawl.mark_read(conn, url, 1 if val else 0)
            conn.close()
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._same_origin():
            self._json({"error": "cross-origin"}, 403)
            return
        try:
            n = _to_int(self.headers.get("Content-Length"), 0)
            if n < 0 or n > 200_000:
                self._json({"error": "body too large"}, 413)
                return
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            self._json({"error": "bad json"}, 400)
            return
        try:
            self._api_post(parsed.path, body)
        except Exception as exc:
            self._json({"error": type(exc).__name__}, 500)
            raise

    def _api_post(self, path, body):
        if path == "/api/quit":
            # 无控制台版没有窗口可以点"×"，关掉浏览器标签页服务照旧在跑，
            # 还会锁住自己目录里的文件（用户因此删不掉旧版）。必须给一个出口。
            self._json({"ok": True, "crawling": bool(_state["crawling"])})
            srv = _state.get("srv")
            if srv is not None:
                threading.Timer(0.4, srv.shutdown).start()   # 先回响应，再让主循环退出
            return
        if path == "/api/correct":
            url = body.get("url", "")
            cat = body.get("category", "")
            if not isinstance(url, str) or not isinstance(cat, str):
                self._json({"ok": False, "error": "bad types"}, 400)
                return
            conn = conn_db()
            ok = crawl.set_label(conn, url, cat, str(body.get("note", ""))[:200])
            stats = crawl.refine_with_model(conn) if ok else {}
            conn.close()
            self._json({"ok": ok, "stats": stats}, 200 if ok else 400)
            return
        if path == "/api/profile":
            cur = apply_profile(body, PF.load(PROFILE))
            PF.save(PROFILE, cur)
            self._json({"ok": True, "profile": PF.as_dict(cur)})
            return
        self._json({"error": "not found"}, 404)


_REVIEW_CACHE = []


def review_unsure(limit: int = 40) -> int:
    """列出规则没把握的条目和模型建议，给 --correct 用编号操作。"""
    conn = conn_db()
    crawl.refine_with_model(conn)
    rows = conn.execute(
        "SELECT url,category_name,suggest,suggest_sim,confidence,title,source_name "
        "FROM items WHERE confidence<>'high' ORDER BY suggest_sim DESC, pub_date DESC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    if not rows:
        print("没有待确认条目。")
        return 0
    global _REVIEW_CACHE
    _REVIEW_CACHE = rows
    ids = {c["id"]: c["name"] for c in CATEGORIES}
    print(f"\n{'编号':<4} {'当前类目':<10} {'模型建议':<10} 相似 标题")
    print("-" * 78)
    for i, r in enumerate(rows, 1):
        sg = ids.get(r["suggest"], "") if r["suggest"] else ""
        print(f"{i:<6} {r['category_name']:<12} {sg or '—':<10} "
              f"{(r['suggest_sim'] or 0):.2f}  {r['title'][:34]}")
    print("\n采纳建议或改判：python app.py --correct 编号 类目id")
    print("类目id：" + "  ".join(f"{k}={v}" for k, v in ids.items()))
    return 0


def apply_correction(idx: str, category: str) -> int:
    """按 --review 的编号写入人工修正。"""
    if not _REVIEW_CACHE:
        conn = conn_db()
        _REVIEW_CACHE.extend(conn.execute(
            "SELECT url,category_name,suggest,suggest_sim,confidence,title,source_name "
            "FROM items WHERE confidence<>'high' ORDER BY suggest_sim DESC, pub_date DESC LIMIT 40"))
        conn.close()
    if not idx.isdigit() or not (1 <= int(idx) <= len(_REVIEW_CACHE)):
        print(f"编号超出范围，先跑 python app.py --review 看列表（1-{len(_REVIEW_CACHE)}）")
        return 1
    row = _REVIEW_CACHE[int(idx) - 1]
    conn = conn_db()
    if not crawl.set_label(conn, row["url"], category):
        conn.close()
        print("类目 id 无效，见 --review 底部的对照表。")
        return 1
    st = crawl.refine_with_model(conn)
    conn.close()
    print(f"已改判：{row['title'][:38]}\n  {row['category_name']} → "
          f"{CAT_BY_ID[category]['name']}（已记入人工修正，会参与后续训练）")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="东方学院通知速览工作台")
    ap.add_argument("--quick", action="store_true", help="不进详情页，只按标题分类（最快）")
    ap.add_argument("--crawl-only", action="store_true", help="只抓取并打印终端速览")
    ap.add_argument("--serve-only", action="store_true", help="不抓取，直接起网页看已有数据")
    ap.add_argument("--reclassify", action="store_true", help="只按当前规则重算权重与摘要，不联网")
    ap.add_argument("--setup", action="store_true", help="重新填写个人档案")
    ap.add_argument("--review", action="store_true", help="列出待确认条目与模型建议，供逐条判定")
    ap.add_argument("--correct", nargs=2, metavar=("编号", "类目id"),
                    help="采纳某条建议或人工改判，如：--correct 7 life（编号来自 --review）")
    ap.add_argument("--days", type=int, default=7, help="终端速览只看近 N 天，默认 7")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--doctor", action="store_true", help="自检运行环境后退出（换机器时先跑这个）")
    args = ap.parse_args()

    missing = check_deps()
    if args.doctor:
        import doctor
        raise SystemExit(doctor.run(missing, DATA, PORT))
    if missing:
        print("缺少依赖：" + ", ".join(missing))
        print("请先执行：  pip install " + " ".join(missing))
        raise SystemExit(1)

    if args.review:
        raise SystemExit(review_unsure())
    if args.correct:
        raise SystemExit(apply_correction(args.correct[0], args.correct[1]))

    prof = PF.load(PROFILE)
    if not PROFILE.exists():
        # 第一次使用先说清"数据在哪、会不会上传"，比事后翻 README 有用
        print("数据只存在本机：", DATA)
        print("程序不向任何外部服务发送数据，抓取范围限于学校官网公开列出的通知。")
    if args.setup or not prof.onboarded:
        prof = PF.ask_profile(prof)
        PF.save(PROFILE, prof)

    if args.reclassify:
        conn = conn_db()
        n = crawl.reclassify_all(conn)
        st = crawl.refine_with_model(conn)
        conn.close()
        print(f"已按当前规则重算 {n} 条；模型建议 {st['suggested']} 条，"
              f"待确认 {st['unsure']} 条，人工修正 {st['manual']} 条")
        return          # 这是维护命令：重算完就退出，不要顺手把工作台起起来挡住终端

    def crawl_then_report() -> None:
        t0 = time.time()
        detail = not args.quick
        print(f"开始抓取（{'列表+正文详情' if detail else '仅列表标题'}）…")
        n = do_crawl(detail=detail)
        conn = conn_db()
        st = crawl.refine_with_model(conn)
        conn.close()
        print(f"完成：解析 {n} 条，用时 {time.time() - t0:.1f}s；"
              f"模型建议 {st['suggested']} 条，待确认 {st['unsure']} 条")
        try:
            payload = build_payload({"days": [str(args.days)], "unread": ["0"], "sort": ["personal"]})
            print_panel(payload["items"], PF.load(PROFILE))
        except Exception as exc:
            print(f"终端速览没打出来：{type(exc).__name__}")

    if args.crawl_only:
        crawl_then_report()
        return

    if args.serve_only:
        payload = build_payload({"days": [str(args.days)], "unread": ["0"], "sort": ["personal"]})
        print_panel(payload["items"], prof)

    # 先起服务、先出页面，抓取放到后台线程。原来的顺序是先抓 1–3 分钟再起服务，
    # 而无控制台的 exe 双击后屏幕上什么都没有，同学以为没开起来就会反复点。
    live = existing_instance(args.port if args.port != PORT else PORT)
    if live:
        print(f"工作台已经在跑了：{live}  这次不重复启动，直接打开页面。")
        if not args.no_open:
            webbrowser.open(live)
        return

    port = args.port if args.port != PORT else free_port(PORT)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"端口 {port} 起不来：{exc}\n可加 --port 8899 指定其他端口")
        return
    _state["port"] = port
    _state["srv"] = srv            # /api/quit 要用它把 serve_forever 停掉
    addr = f"http://127.0.0.1:{port}"
    print(f"工作台已启动：{addr}  （Ctrl+C 退出）")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(addr)).start()
    if not args.serve_only:
        threading.Thread(target=crawl_then_report, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
