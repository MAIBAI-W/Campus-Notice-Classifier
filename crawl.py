# -*- coding: utf-8 -*-
"""抓取与解析层：纯 requests + BeautifulSoup，不依赖 Playwright / 无浏览器。

职责：
  1. 按 sources.SOURCES 拉取各栏目列表页，解析出通知条目；
  2. 对高权重条目进详情页取正文，用于精准分类与截止日期识别；
  3. 结果落 SQLite，保留"已读/新出现"状态，支持增量刷新。
"""
from __future__ import annotations

import http.client
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

import sources as SRC
from classify import (CAT_BY_ID, NOTICE_TYPES, build_model, classify, detect_doc,
                      predict, score_for)

# 证书降级只发生在 INSECURE_ALLOWED 里的学校域名，抑制告警免得刷屏
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TEXT_RE = re.compile(r"\s+")
STATIC_ITEM = re.compile(r"/info/\d+/\d+\.(?:htm|html)")
DYN_ITEM = re.compile(r"(content|info)\.jsp\?[^'\"]*wbnewsid=\d+")
FULL_DATE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
MD_DATE = re.compile(r"(?<![\d/.年])(\d{1,2})\s*[-/.]\s*(\d{1,2})(?![\d/.-])")
DATE_IN_TEXT = FULL_DATE

CONTENT_SELECTORS = ["div.v_news_content", "div.article-content", "div.v_content",
                     "div.content", "div#content", "article", "div.news-view"]

_host_lock = threading.Lock()
_host_last: dict[str, float] = {}
_host_fails: dict[str, int] = {}
_warned_insecure: set[str] = set()

TRANSIENT = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
             http.client.RemoteDisconnected, ConnectionResetError)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _throttle(url: str) -> None:
    """同站点串行限速，不同站点并发。"""
    host = _host(url)
    with _host_lock:
        wait = SRC.SAME_HOST_GAP - (time.time() - _host_last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.time()


def host_dead(url: str) -> bool:
    """熔断：某站连续失败够多次，本轮就不再对它发请求。"""
    with _host_lock:
        return _host_fails.get(_host(url), 0) >= SRC.HOST_FAIL_BUDGET


def _note_fail(url: str) -> None:
    with _host_lock:
        _host_fails[_host(url)] = _host_fails.get(_host(url), 0) + 1


def _note_ok(url: str) -> None:
    with _host_lock:
        _host_fails[_host(url)] = 0


def _allowed_insecure(url: str) -> bool:
    # 判断逻辑统一放在 sources.insecure_allowed：只认"等于学校域名或它的子域"，
    # 早先用子串匹配，zufedfc.edu.cn.attacker.com 这种也会被放行。
    return SRC.insecure_allowed(url)


def _decode(r: requests.Response) -> str:
    """编码判定链：响应头 charset → HTML meta → 统计推断 → utf-8 → gbk。

    学校子站里既有 UTF-8 也有 GBK，`apparent_encoding` 在缺 charset_normalizer 的
    机器上会返回 None，所以必须留显式兜底，否则中文全变乱码。
    """
    enc = ""
    ctype = r.headers.get("Content-Type", "")
    m = re.search(r"charset\s*=\s*([\w-]+)", ctype, re.I)
    if m:
        enc = m.group(1)
    if not enc:
        head = r.content[:4096].decode("latin-1", "ignore")
        m = re.search(r'<meta[^>]+charset\s*=\s*["\']?([\w-]+)', head, re.I)
        if m:
            enc = m.group(1)
    if not enc:
        enc = (getattr(r, "apparent_encoding", None) or "").strip()
    for cand in (enc, "utf-8", "gb18030"):
        if not cand:
            continue
        try:
            return r.content.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return r.content.decode("utf-8", "replace")


def _raw_get(sess: requests.Session, url: str) -> requests.Response:
    """带重试与证书降级的单次 GET。"""
    last = None
    for attempt in range(SRC.MAX_RETRY):
        _throttle(url)
        try:
            return sess.get(url, timeout=SRC.REQUEST_TIMEOUT, allow_redirects=True, verify=True)
        except requests.exceptions.SSLError as exc:
            last = exc
            if _allowed_insecure(url):
                # 学校域名证书链不全很常见；只读公开页，降级但告知一次
                if url not in _warned_insecure:
                    _warned_insecure.add(url)
                r = sess.get(url, timeout=SRC.REQUEST_TIMEOUT, allow_redirects=True, verify=False)
                # 降级只对白名单里的学校域生效：重定向跳出白名单就不认这份内容
                if not _allowed_insecure(r.url or url):
                    raise requests.exceptions.SSLError(f"redirected out of allowlist: {r.url}")
                return r
            raise
        except TRANSIENT as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))   # 线性退避，弱站重连往往一次就好
    raise last if last else requests.RequestException(url)


def _looks_blocked(html: str) -> bool:
    return len(re.sub(r"\s+", "", html)) < 260 or "<a" not in html.lower()


def fetch(url: str, sess: requests.Session) -> tuple[str, str, str]:
    """返回 (最终 URL, HTML, 错误)。空文档自动试 https 与 /index.htm。

    第三个元素非空时表示失败原因：auth（撞认证墙）/ unreachable / dead（已熔断）/ error:*。
    """
    if host_dead(url):
        return url, "", "dead"

    tries = [url]
    if url.startswith("http://"):
        tries.append("https://" + url[7:])
    p = urlparse(url)
    if p.path in ("", "/"):
        tries.append(f"{p.scheme}://{p.netloc}/index.htm")

    err = ""
    for cand in tries:
        try:
            r = _raw_get(sess, cand)
        except requests.RequestException as exc:
            _note_fail(cand)
            err = f"error:{type(exc).__name__}"
            continue
        except Exception as exc:                      # 兜底：不让单个站把整轮带崩
            _note_fail(cand)
            err = f"error:{type(exc).__name__}"
            continue

        final = r.url
        if any(m in final for m in SRC.AUTH_MARKERS):
            _note_ok(cand)
            return final, "", "auth"
        if r.status_code >= 400:
            err = f"http{r.status_code}"
            continue
        html = _decode(r)
        if _looks_blocked(html):
            err = "unreachable"
            continue
        _note_ok(cand)
        return final, html, ""
    return url, "", err or "unreachable"


def _row_text(a) -> str:
    node = a
    for _ in range(4):
        node = node.parent
        if node is None:
            break
        if node.name in ("li", "tr", "div", "dd", "p"):
            return TEXT_RE.sub(" ", node.get_text(" ", strip=True))[:400]
    return TEXT_RE.sub(" ", a.get_text(" ", strip=True))


def _date_candidates(a) -> list[str]:
    """列表页里日期可能出现在：链接 title、同级的 .time/.date 元素、或整行文本。"""
    cands = [a.get("title") or "", a.get_text(" ", strip=True)]
    node = a
    for _ in range(4):
        node = node.parent
        if node is None:
            break
        if node.name in ("li", "tr", "dd", "p"):
            for el in node.find_all(True):
                cls = " ".join(el.get("class") or [])
                if re.search(r"time|date|rq|riqi", cls, re.I):
                    cands.append(el.get_text(" ", strip=True))
            cands.append(node.get_text(" ", strip=True))
            break
    return [TEXT_RE.sub(" ", c)[:400] for c in cands if c]


def _pick_date(a) -> tuple[str, bool]:
    """返回 (日期, 是否精确到年)。

    精确到年的日期来自页面本身；只有"11-29"这种月日时**绝不贴今年的年份**——
    那会把 2024-11-29 那条《外语教学电台调频接收机维修检修》变成 2026-11-29，
    一个未来日期还会被界面分进"今天"。改成取"最近一个已经过去的该月日"。
    """
    today = date.today()
    for txt in _date_candidates(a):
        m = FULL_DATE.search(txt)
        if m:
            try:
                return datetime.strftime(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))), "%Y-%m-%d"), True
            except ValueError:
                continue
        m = MD_DATE.search(txt)
        if m:
            try:
                d = date(today.year, int(m.group(1)), int(m.group(2)))
                if d > today:
                    d = date(today.year - 1, int(m.group(1)), int(m.group(2)))
                return d.strftime("%Y-%m-%d"), False
            except ValueError:       # 2 月 29 日这类不存在的日期
                continue
    return "", False


BULLET = "·•▪‣◦●○◆■－—–~～|｜、,。;；:： \t"
LEAD_DATE = re.compile(r"^\s*[\[（(【]\s*20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\s*[\]）)】]\s*")
# 列表页角标："新 招聘公告"、"热|宣讲会"。要求角标后有空格或竖线，不误伤"新建体育馆…"
LEAD_BADGE = re.compile(r"^\s*(?:新|热|荐|顶|精|转)\s*(?=[|｜·:：]|\s)")


def clean_title(t: str) -> str:
    """列表页常把项目符号、方括号日期塞在标题前面，入库前去掉。

    只剥列表符号、[2026-09-23] 这类纯日期前缀（第三方就业网的列表行就是这样排的）
    和"新 / 热|"这种角标，不动 【转载】《办法》 这类合法开头。
    """
    t = LEAD_DATE.sub("", t)
    t = LEAD_BADGE.sub("", t)
    return t.strip(BULLET)


def parse_listing(html: str, page_url: str, mode: str,
                  item_re: re.Pattern | None = None, cols: dict | None = None) -> list[dict]:
    """从列表页/首页抽条目。

    博达 CMS 的条目链接只有两种：`/info/栏目/编号.htm`(static) 和
    `content.jsp?...wbnewsid=`(dynamic)，由 mode 决定认哪种。非博达站点（第三方就业
    信息网就不是博达）在 sources 里自带 `item` 正则，并用 `cols` 把路径段翻成栏目名。
    """
    soup = BeautifulSoup(html, "lxml")
    tree_name = {}
    if item_re is None and mode in ("dynamic", "auto"):
        for a in soup.find_all("a", href=re.compile(r"wbtreeid=\d+")):
            t = TEXT_RE.sub(" ", a.get_text(" ", strip=True))
            m = re.search(r"wbtreeid=(\d+)", a["href"])
            if m and 1 < len(t) <= 12 and not t.startswith(("更多", "首页", "返回")):
                tree_name.setdefault(m.group(1), t)

    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if item_re is not None:
            if not item_re.search(href):
                continue
            is_static, is_dyn = True, False
        else:
            is_static = bool(STATIC_ITEM.search(href))
            is_dyn = bool(DYN_ITEM.search(href))
            if mode == "static" and not is_static:
                continue
            if mode == "dynamic" and not is_dyn:
                continue
            if not (is_static or is_dyn):
                continue

        url = urljoin(page_url, href.split("#")[0])
        if urlparse(url).scheme not in ("http", "https"):
            continue          # javascript:/data: 之类的 href 一旦入库，界面点开就是执行脚本
        if url in seen:
            continue
        title = clean_title(TEXT_RE.sub(" ", (a.get("title") or a.get_text(" ", strip=True) or "")))
        if len(title) < 6:
            continue
        row = _row_text(a)
        col = ""
        if item_re is not None:
            seg = urlparse(url).path.strip("/").split("/")[0]
            col = (cols or {}).get(seg, seg)
        elif is_dyn:
            m = re.search(r"wbtreeid=(\d+)", url)
            col = tree_name.get(m.group(1), f"栏目{m.group(1)}") if m else ""
        else:
            m = re.match(r"(/info/(\d+)/)", urlparse(url).path)
            col = f"栏目{m.group(2)}" if m else ""
        seen.add(url)
        d, exact = _pick_date(a)
        out.append({"url": url, "title": title, "date": d, "date_exact": exact, "column": col,
                    "row": row[:200]})
    return out


PUB_META = re.compile(r"(?:发布时间|发布日期|投稿时间|时间)[：:]\s*(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
ATTACH = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z)\b", re.I)


def parse_detail(html: str, page_url: str = "") -> tuple[str, str, str]:
    """详情页取 (正文片段, 发布日期, 附件名)。

    正文只作为分类与截止日期的输入，不展示全文；附件文件名往往就是报名表，
    单独抽出来可以在摘要里提示"含附件"。
    """
    soup = BeautifulSoup(html, "lxml")
    body = ""
    for sel in CONTENT_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 40:
            body = TEXT_RE.sub(" ", node.get_text(" ", strip=True))
            break
    if not body:
        main = soup.get_text(" ", strip=True)
        body = TEXT_RE.sub(" ", main)[:1500]

    pub = ""
    head_text = soup.get_text(" ", strip=True)[:1500]
    m = PUB_META.search(head_text) or FULL_DATE.search(head_text)
    if not m:  # 有些站点把日期放在 meta 标签里
        for meta in soup.find_all("meta"):
            name = (meta.get("name") or "").lower()
            if name in ("publishdate", "article:published_time", "pubdate", "wcjauthor"):
                m = FULL_DATE.search(meta.get("content") or "")
                if m:
                    break
    if m:
        try:
            pub = datetime.strftime(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))), "%Y-%m-%d")
        except (ValueError, IndexError):
            pub = ""

    files = []
    for a in soup.find_all("a", href=True):
        t = TEXT_RE.sub(" ", a.get_text(" ", strip=True))
        if t and len(t) > 5 and ATTACH.search(t):
            files.append(t[:60])
    return body[:2000], pub, " ".join(files[:3])


COLUMN_HINT = re.compile(r"通知|公告|公示|就业|招聘|创业|实习|场地|采购|招标|竞赛|评奖|资助|选"
                         r"|tzgg|jstz|xstz|jyxx|zpxx|cxcy")


def discover_columns(html: str, page_url: str, mode: str, limit: int = 12) -> list[str]:
    """从首页导航里自动发现栏目页，解决只抓到首页摘要、覆盖过窄的问题。"""
    soup = BeautifulSoup(html, "lxml")
    base = urlparse(page_url)
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].split("#")[0])
        u = urlparse(href)
        if u.netloc != base.netloc:
            continue
        if STATIC_ITEM.search(href) or DYN_ITEM.search(href):
            continue  # 这是条目本身，不是栏目
        text = TEXT_RE.sub(" ", a.get_text(" ", strip=True))
        if not text or len(text) > 14 or href in seen:
            continue
        if not COLUMN_HINT.search(text + " " + u.path):
            continue
        if not re.search(r"\.htm|\.jsp|/$", u.path):
            continue
        seen.add(href)
        found.append(href)
    return found[:limit]


NEXT_TEXT = re.compile(r"^\s*(下一页|下页|next|>>|»|›|>)\s*$", re.I)
PAGE_SUFFIX = re.compile(r"^(.*)_([2-9]|1[0-9])\.html?$")


def find_pages(html: str, page_url: str, max_pages: int) -> list[str]:
    """找出一个栏目页的后续分页链接，兼容博达的三种分页写法。

    只跟页面里**实际存在**的链接，不猜 URL：站点没有分页时自然停。
      1) 同目录 name_2.htm / name_3.htm（博达静态发布最常见）
      2) 文案为"下一页 / >> / next"的链接
      3) 带 page / offset / pageNum 参数的 list.jsp、index.jsp
    """
    soup = BeautifulSoup(html, "lxml")
    stem_dir = page_url.rsplit("/", 1)[0] if "/" in page_url[:-1] else ""
    seen = {page_url}
    pages = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].split("#")[0])
        if href in seen:
            continue
        u = urlparse(href)
        if u.netloc != urlparse(page_url).netloc:
            continue
        rank = None
        m = PAGE_SUFFIX.match(u.path.rsplit("/", 1)[-1])
        if m and href.startswith(stem_dir + "/"):
            rank = int(m.group(2))
        elif NEXT_TEXT.match(a.get_text(" ", strip=True) or ""):
            rank = 2
        else:
            q = parse_qs(u.query)
            for k in ("page", "pageNum", "offset", "pi"):
                if k in q and q[k][0].isdigit() and int(q[k][0]) > 1:
                    rank = int(q[k][0])
                    break
        if rank is None:
            continue
        seen.add(href)
        pages.append((rank, href))
    pages.sort()
    return [u for _, u in pages][: max_pages - 1]


def harvest_listing(url: str, sess: requests.Session, mode: str,
                    max_pages: int, known: set | None = None,
                    item_re: re.Pattern | None = None, cols: dict | None = None) -> tuple[list[dict], str, str]:
    """抓一个栏目页及其分页，返回 (条目, 首页HTML, 错误)。

    known 是库里已有的条目 ckey 集合。第一页全是见过的条目，说明这个栏目没更新，
    直接不翻页——重复抓取时能省掉大半请求。
    """
    final, html, err = fetch(url, sess)
    if err:
        return [], "", err
    items = parse_listing(html, final, mode, item_re, cols)
    if known and items:
        head = [canon(i["url"]) for i in items[:5]]
        if all(h in known for h in head):
            return items, html, ""
    for pg in find_pages(html, final, max_pages):
        f2, h2, e2 = fetch(pg, sess)
        if e2:
            break
        got = parse_listing(h2, f2, mode, item_re, cols)
        if not got:      # 空页说明到底了
            break
        items.extend(got)
    return items, html, ""


def crawl_source(cfg: dict, sess: requests.Session, want_detail: bool,
                 known: set | None = None) -> tuple[dict, list[dict], str]:
    """抓一个站点：列表页(含分页) → 首页 → 首页里自动发现的栏目页；再按需补正文。"""
    base = cfg["base"]
    max_pages = SRC.MAX_PAGES
    mode = cfg.get("mode", "auto")
    # 非博达站点自带条目链接形态；它的栏目页要靠 JS 才有内容，所以不自动发现栏目
    item_re = re.compile(cfg["item"]) if cfg.get("item") else None
    cols = cfg.get("cols") or {}
    lists = [urljoin(base + "/", p.lstrip("/")) for p in cfg["lists"]][: SRC.MAX_LIST_PER_SOURCE]
    items: list[dict] = []
    status = "ok"
    home_html, home_url = "", base + "/"

    for u in lists:
        got, html, err = harvest_listing(u, sess, mode, max_pages, known, item_re, cols)
        if err:
            status = err
            continue
        if not home_html and u.rstrip("/").endswith(base):
            home_html, home_url = html, u
        items.extend(got)

    if not home_html or not items or len(items) < 12:
        final, html, err = fetch(base + "/", sess)
        if not err:
            home_html, home_url, status = html, final, "ok"
            items.extend(parse_listing(html, final, mode, item_re, cols))
        elif status == "ok":
            status = err

    # 自动发现更多栏目（含各自分页），把就业/招聘/场地这类不在首页摘要里的信息补全
    if item_re is None:
        for col in discover_columns(home_html, home_url, mode) if home_html else []:
            got, _, _ = harvest_listing(col, sess, mode, max_pages, known, item_re, cols)
            items.extend(got)

    uniq, seen = [], set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        it.setdefault("column", cfg["name"])
        uniq.append(it)

    if want_detail:
        # 截止日期、报名对象、负责部门几乎都只写在正文里，所以优先给"通知/公告/公示"类补正文，
        # 而不是单纯按权重取前 N —— 后者会把只有标题的新闻也拉进来浪费请求。
        def rank(it):
            dt = detect_doc(it["title"], it.get("column", ""))
            return (0 if dt in NOTICE_TYPES else 1,
                    -classify(it["title"], "", it["date"], cfg["weight"], column=it.get("column", "")).score)
        cand = sorted(uniq, key=rank)[: SRC.MAX_DETAIL_PER_SOURCE]
        for it in cand:
            final, html, err = fetch(it["url"], sess)
            if err:
                continue
            body, pub, files = parse_detail(html, it["url"])
            it["body"] = body
            it["files"] = files
            # 详情页的完整日期是权威值，必须盖过列表页"只有月日"的推断。
            # 原来条件是 `not it["date"]`，于是列表页猜出来的 2026-11-29 会把
            # 详情页真正的 2024-11-29 挡在外面。
            if pub and (not it["date"] or not it.get("date_exact")):
                it["date"] = pub
    return cfg, uniq, status


def crawl_all(want_detail: bool = True, workers: int = 6,
              known: set | None = None) -> tuple[list[dict], dict]:
    """并发抓全部站点，返回 (条目列表, 每站状态)。

    单个站点抛异常只记进 report，不让整轮抓取失败——换台机器、换个网络时
    总有几个站会抽风。
    """
    sess = requests.Session()
    sess.trust_env = False   # 不继承系统代理：Clash 开着时学校站点不该绕道境外节点
    sess.headers.update({"User-Agent": SRC.UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    report, rows = {}, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(crawl_source, cfg, sess, want_detail, known): cfg for cfg in SRC.SOURCES}
        for fut in as_completed(futs):
            cfg = futs[fut]
            try:
                _cfg, items, status = fut.result()
            except Exception as exc:
                report[cfg["key"]] = {"name": cfg["name"], "status": f"crash:{type(exc).__name__}",
                                      "count": 0}
                continue
            report[cfg["key"]] = {"name": cfg["name"], "status": status, "count": len(items)}
            for it in items:
                it["source"] = cfg["key"]
                it["source_name"] = cfg["name"]
                it["source_weight"] = cfg["weight"]
                rows.append(it)
    return rows, report


# ---------------- 存储 ----------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  url TEXT PRIMARY KEY,
  ckey TEXT,
  source TEXT, source_name TEXT, column TEXT,
  title TEXT, digest TEXT, body TEXT, files TEXT,
  pub_date TEXT, deadline TEXT,
  category TEXT, category_name TEXT, color TEXT, tags TEXT, hits TEXT,
  audience TEXT, doc_type TEXT, is_notice INTEGER DEFAULT 1,
  confidence TEXT, method TEXT,
  suggest TEXT, suggest_sim REAL DEFAULT 0,
  score REAL, is_new INTEGER DEFAULT 1, is_recent INTEGER DEFAULT 0,
  read INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS labels(
  url TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  title TEXT DEFAULT '',
  note TEXT DEFAULT '',
  created TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT,
  total INTEGER, detail INTEGER, report TEXT
);
"""


def canon(url: str) -> str:
    """去协议化的唯一键：同一篇文章常以 http/https 两个地址各出现一次。"""
    u = urlparse(url)
    host = (u.netloc or "").lower().replace("www.", "")
    path = re.sub(r"/+", "/", u.path or "/")
    params = {}
    if u.query:
        for kv in u.query.split("&"):
            k, _, v = kv.partition("=")
            if k in ("urltype", "wbtreeid", "wbnewsid", "id"):
                params[k] = v
    q = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{host}{path}" + (f"?{q}" if q else "")


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    with conn:  # 迁移必须提交，否则进程退出时回滚，重复行又回来了
        have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        if "audience" not in have:  # 老库补列，不必删库重抓
            conn.execute("ALTER TABLE items ADD COLUMN audience TEXT DEFAULT ''")
        if "doc_type" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN doc_type TEXT DEFAULT ''")
        if "is_notice" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN is_notice INTEGER DEFAULT 1")
        if "files" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN files TEXT DEFAULT ''")
        if "confidence" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN confidence TEXT DEFAULT 'low'")
        if "method" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN method TEXT DEFAULT '规则'")
        if "suggest" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN suggest TEXT DEFAULT ''")
        if "suggest_sim" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN suggest_sim REAL DEFAULT 0")
        if "ckey" not in have:
            conn.execute("ALTER TABLE items ADD COLUMN ckey TEXT")
        # ALTER 在 SQLite 里会隐式提交，回填却可能被回滚，所以只要还有空 ckey 就重跑一遍
        need = conn.execute("SELECT 1 FROM items WHERE ckey IS NULL OR ckey='' LIMIT 1").fetchone()
        if need:
            rows = conn.execute("SELECT url, body FROM items").fetchall()
            lengths = {r["url"]: len(r["body"] or "") for r in rows}
            keep: dict[str, str] = {}
            for r in rows:
                ck = canon(r["url"])
                if ck not in keep or lengths[r["url"]] > lengths[keep[ck]]:
                    keep[ck] = r["url"]
            alive = set(keep.values())
            conn.executemany("DELETE FROM items WHERE url=?", [(r["url"],) for r in rows
                                                               if r["url"] not in alive])
            for ck, u in keep.items():
                conn.execute("UPDATE items SET ckey=? WHERE url=?", (ck, u))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_ckey ON items(ckey)")
    return conn


def clamp_past(d: str) -> str:
    """把未来日期按整年往前挪。

    发布日期不可能在未来。列表页只有"月日"时最容易推出一个未来日子，而它会直接被
    界面分进"今天"。这里做最后一道兜底：新抓取和 --reclassify 都走 save_items，
    所以库里从此不会再有未来日期。
    """
    if not d:
        return d
    try:
        y, m, dd = (int(x) for x in d.split("-"))
    except ValueError:
        return d
    today = date.today()
    while True:
        try:
            cand = date(y, m, dd)
        except ValueError:                      # 2 月 29 日
            y -= 1
            continue
        if cand <= today:
            return cand.strftime("%Y-%m-%d")
        y -= 1


def save_items(conn: sqlite3.Connection, rows: list[dict]) -> int:
    today = date.today()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    with conn:
        for r in rows:
            # 只压"月日推断"出来的日期。就业网首页直接给的完整日期可能是**未来**，
            # 那是双选会/宣讲会的举办日，正是要提前看到的，不能当成错误往回挪。
            if not r.get("date_exact"):
                r["date"] = clamp_past(r.get("date", ""))
            v = classify(r["title"], r.get("body", ""), r["date"], r.get("source_weight", 1.0),
                         column=r.get("column", ""))
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
                v.is_recent = (today - d).days <= 7
            except (ValueError, TypeError):
                v.is_recent = False
            ck = canon(r["url"])
            is_new = 0 if conn.execute("SELECT 1 FROM items WHERE ckey=?", (ck,)).fetchone() else 1
            conn.execute(
                """INSERT INTO items(url, ckey, source, source_name, column, title, digest, body, files,
                     pub_date, deadline, category, category_name, color, tags, hits, audience,
                     doc_type, is_notice, confidence, method,
                     score, is_new, is_recent, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ckey) DO UPDATE SET
                     digest=excluded.digest, body=excluded.body, files=excluded.files,
                     score=excluded.score,
                     deadline=excluded.deadline, category=excluded.category,
                     category_name=excluded.category_name, color=excluded.color,
                     tags=excluded.tags, hits=excluded.hits, audience=excluded.audience,
                     doc_type=excluded.doc_type, is_notice=excluded.is_notice,
                     confidence=excluded.confidence,
                     is_recent=excluded.is_recent,
                     pub_date=CASE WHEN excluded.pub_date<>'' THEN excluded.pub_date ELSE items.pub_date END,
                     last_seen=excluded.last_seen""",
                (r["url"], ck, r.get("source"), r.get("source_name"), r.get("column"), r["title"], v.digest,
                 r.get("body", ""), r.get("files", ""), r.get("date", ""), "",
                 v.primary, v.primary_name, v.color,
                 "|".join(v.tags), format_hits(v.hits), v.audience,
                 v.doc_type, 1 if v.is_notice else 0, v.confidence, '规则',
                 v.score, is_new, 1 if v.is_recent else 0, now, now),
            )
            written += 1
    return written


def _item_select(conn, skip=("body",)) -> str:
    """列出要查的列。列名从表结构推导，不手抄——手抄会在加字段时静默漏掉新列。

    默认跳过 body：界面从不展示正文（它只是分类与受众识别的输入），而全库正文有
    443 KB，`SELECT *` 让每次 /api/items 都白拖一遍，三次分面查询就是三倍。
    """
    return ", ".join(r[1] for r in conn.execute("PRAGMA table_info(items)") if r[1] not in skip)


def load_items(conn: sqlite3.Connection, days: int = 0, only_unread: bool = False,
               category: str = "", q: str = "", limit: int = 3000,
               source: str = "", audience: str = "", doc_type: str = "",
               cols: str = "") -> list[dict]:
    """按条件取条目。`cols` 留给侧栏分面计数用：它只要 (title, source, category)，
    列越少越快；不传就是"除 body 外的全部列"。
    """
    sql, args = f"SELECT {cols or _item_select(conn)} FROM items WHERE 1=1", []
    if days:
        # 只按发布日期过滤；无日期的条目在限定时段时排除，否则第一次抓取会全部命中
        sql += " AND pub_date >= ?"
        since = (date.today() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
        args.append(since)
    if only_unread:
        sql += " AND read=0"
    if category:
        sql += " AND category=?"
        args.append(category)
    if source:
        sql += " AND source=?"
        args.append(source)
    if audience:
        sql += " AND audience LIKE ?"
        args.append(f"%{audience}%")
    if doc_type == "notice":
        sql += " AND is_notice=1"
    elif doc_type:
        sql += " AND doc_type=?"
        args.append(doc_type)
    if q:
        sql += " AND (title LIKE ? OR digest LIKE ? OR body LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY score DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in conn.execute(sql, args)]
    # 同一篇文章常被同时挂进多个栏目（不同 article id），读取时折叠，保留权重最高的一条
    collapsed, seen = [], set()
    for r in rows:
        k = (r["title"], r["source"])
        if k in seen:
            continue
        seen.add(k)
        collapsed.append(r)
    return collapsed


def format_hits(hits: dict) -> str:
    """把命中依据序列化成 `概念:词1,词2|概念2:...`，供界面解释"凭什么这么分"。"""
    return "|".join(f"{c}:{','.join(ws)}" for c, ws in hits.items())


def reclassify_all(conn: sqlite3.Connection) -> int:
    """改了分类规则后只重算权重与摘要，不重新联网抓取。"""
    today = date.today()
    weights = {c["key"]: c["weight"] for c in SRC.SOURCES}
    n = 0
    with conn:
        for r in conn.execute("SELECT url,title,body,pub_date,source,column FROM items"):
            sw = weights.get(r["source"], 1.0)
            v = classify(r["title"], r["body"] or "", r["pub_date"] or "", sw, column=r["column"] or "")
            is_recent = False
            if r["pub_date"]:
                try:
                    d = datetime.strptime(r["pub_date"][:10], "%Y-%m-%d").date()
                    is_recent = (today - d).days <= 7
                except ValueError:
                    pass
            conn.execute(
                """UPDATE items SET digest=?, category=?, category_name=?, color=?,
                   tags=?, hits=?, audience=?, doc_type=?, is_notice=?, confidence=?,
                   score=?, is_recent=?, method='规则' WHERE url=?""",
                (v.digest, v.primary, v.primary_name, v.color, "|".join(v.tags),
                 format_hits(v.hits), v.audience, v.doc_type,
                 1 if v.is_notice else 0, v.confidence, v.score, 1 if is_recent else 0,
                 r["url"]),
            )
            n += 1
    return n


def mark_read(conn: sqlite3.Connection, url: str, value: int = 1) -> None:
    with conn:
        conn.execute("UPDATE items SET read=? WHERE url=?", (value, url))


def log_run(conn: sqlite3.Connection, started: str, total: int, detail: bool, report: dict) -> None:
    import json
    with conn:
        conn.execute("INSERT INTO runs(started, finished, total, detail, report) VALUES(?,?,?,?,?)",
                     (started, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total, 1 if detail else 0,
                      json.dumps(report, ensure_ascii=False)))


# ---------------- 人工修正 + 语料自学习 ----------------

def set_label(conn: sqlite3.Connection, url: str, category: str, note: str = "") -> bool:
    """记录人工修正。它优先于规则与模型，并作为训练样本影响同类其他条目。

    同一篇稿子常被学校发成多个 id（同标题不同 URL），所以按**标题**覆盖，
    改一次就把所有副本一起改掉，否则界面上看起来"没生效"。
    """
    if category not in CAT_BY_ID:
        return False
    rows = conn.execute("SELECT url, title FROM items WHERE title=(SELECT title FROM items WHERE url=?)",
                        (url,)).fetchall()
    if not rows:
        rows = [conn.execute("SELECT url, title FROM items WHERE url=?", (url,)).fetchone()]
        rows = [r for r in rows if r]
    if not rows:
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        for r in rows:
            conn.execute(
                """INSERT INTO labels(url, category, title, note, created) VALUES(?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET category=excluded.category,
                     title=excluded.title, note=excluded.note""",
                (r["url"], category, r["title"], note, now))
    return True


def clear_label(conn: sqlite3.Connection, url: str) -> None:
    with conn:
        conn.execute("DELETE FROM labels WHERE url=?", (url,))


def human_labels(conn: sqlite3.Connection) -> dict:
    """{ckey: category}"""
    out = {}
    for r in conn.execute("SELECT i.ckey AS ck, l.category AS cat FROM labels l "
                          "JOIN items i ON i.url = l.url"):
        if r["ck"]:
            out[r["ck"]] = r["cat"]
    return out


def refine_with_model(conn: sqlite3.Connection) -> dict:
    """跑一遍语料自学习：训练质心 → 救回"其他信息" → 改判低置信条目 → 套用人工修正。

    判定优先级：人工修正 > 规则高置信 > 模型 > 保持原样。规则高置信的条目不会被模型
    覆盖，保证可解释性优先。
    """
    from classify import (SUGGEST_MIN_MARGIN, SUGGEST_MIN_SIM)

    human = human_labels(conn)
    rows = [(r["ckey"], r["url"], r["category"], r["title"], r["body"] or "",
             r["confidence"], r["pub_date"], r["source"], r["column"] or "")
            for r in conn.execute(
                "SELECT ckey,url,category,title,body,confidence,pub_date,source,column FROM items")]
    weights = {c["key"]: c["weight"] for c in SRC.SOURCES}

    # 训练样本：人工修正 + 规则高置信
    docs = [(ck, human.get(ck) or (cat if conf == "high" else None), t, b)
            for ck, _u, cat, t, b, conf, _p, _s, _c in rows]
    model = build_model(docs)

    stats = {"trained": sum(model["train_size"].values()),
             "categories": len(model["centroids"]),
             "manual": 0, "unsure": 0, "suggested": 0, "kept": 0}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        for ck, url, cat, t, b, conf, pub, src, col in rows:
            sw = weights.get(src, 1.0)
            suggest, suggest_sim = "", 0.0
            if ck in human:
                new, method = human[ck], "人工"
            elif conf == "high":
                new, method = cat, "规则"
            else:
                # 模型只提建议、绝不自动改类：n-gram 质心容易学到公文套话而不是主题，
                # 自动改判曾把"期初教职工大会"判成补考选课。改判权交给人，一次修正即入训练集。
                p, sim, margin = predict(ck, model)
                new, method = cat, ("待确认" if cat == "other" else "规则")
                if p and p != cat and sim >= SUGGEST_MIN_SIM and margin >= SUGGEST_MIN_MARGIN:
                    suggest, suggest_sim = p, sim
                else:
                    suggest, suggest_sim = "", 0.0
            if method == "人工":
                stats["manual"] += 1
            elif method == "待确认":
                stats["unsure"] += 1
            else:
                stats["kept"] += 1
            if suggest:
                stats["suggested"] += 1

            score, detail, conf2 = score_for(t, b, new, pub, sw, col)
            c = CAT_BY_ID[new]
            conn.execute(
                "UPDATE items SET category=?, category_name=?, color=?, score=?, hits=?, "
                "confidence=?, method=?, suggest=?, suggest_sim=?, last_seen=? WHERE url=?",
                (new, c["name"], c["color"], score, format_hits(detail),
                 conf2 if method == "规则" else "mid", method, suggest, suggest_sim,
                 now, url))
    return stats
