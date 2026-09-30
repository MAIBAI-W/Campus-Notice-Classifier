# -*- coding: utf-8 -*-
"""站点与栏目配置。

来源：对东方学院官网及院系/部门子站的实际探测结果。
不可达站点（外网返回空文档或拒绝连接）只登记不抓取。
"""

# 抓取模式：static = /info/栏目/编号.htm ；dynamic = content.jsp?wbtreeid=&wbnewsid= ；auto = 两者都试
SOURCES = [
    # ---------- 二级学院 ----------
    {"key": "jrjm", "name": "金融与经贸学院", "kind": "学院", "base": "http://jrjm.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/index/jstz.htm", "/index/xstg.htm"], "mode": "static"},
    {"key": "cs", "name": "财税学院", "kind": "学院", "base": "http://cs.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/index/jstz.htm", "/index/xstz.htm"], "mode": "static"},
    {"key": "gs", "name": "工商管理学院", "kind": "学院", "base": "http://gs.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/index/tzgg.htm"], "mode": "static"},
    {"key": "kj", "name": "会计学院", "kind": "学院", "base": "http://kj.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/jxjs1/tzgg.htm", "/xsgz/tzgg.htm"], "mode": "static"},
    {"key": "info", "name": "信息与人工智能学院", "kind": "学院", "base": "http://info.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/index/jstz.htm", "/index/xstz.htm"], "mode": "static"},
    {"key": "fs", "name": "法律与社会工作/马克思主义学院", "kind": "学院", "base": "https://fs.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/index/jstz.htm", "/index/xstz.htm"], "mode": "static"},
    {"key": "rw", "name": "文化传播与设计学院", "kind": "学院", "base": "https://rw.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/xwdt/jstz.htm", "/xwdt/xstz.htm", "/xwdt/ggyg.htm"], "mode": "static"},
    {"key": "wy", "name": "外国语学院", "kind": "学院", "base": "http://wy.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/index/jstz.htm", "/index/xstz.htm"], "mode": "static"},
    {"key": "cyxy", "name": "创业学院", "kind": "学院", "base": "http://cyxy.zufedfc.edu.cn", "weight": 1.15,
     "lists": ["/tzgg/xytz.htm"], "mode": "auto"},
    {"key": "jxjy", "name": "继续教育学院", "kind": "学院", "base": "https://jxjy.zufedfc.edu.cn", "weight": 0.9,
     "lists": ["/index/tzgg.htm"], "mode": "static"},
    {"key": "tyzx", "name": "体育部", "kind": "学院", "base": "http://tyzx.zufedfc.edu.cn", "weight": 0.85,
     "lists": ["/tzcs/tzgg.htm", "/qtjs/tzgg.htm"], "mode": "static"},

    # ---------- 职能部门 ----------
    {"key": "jw", "name": "教务管理部", "kind": "部门", "base": "http://jw.zufedfc.edu.cn", "weight": 1.1,
     # list.jsp 系列需 CAS 统一身份认证，只从首页公开区取各栏目最新条目
     "lists": ["/"], "mode": "dynamic"},
    {"key": "xg", "name": "学生工作部", "kind": "部门", "base": "http://xg.zufedfc.edu.cn", "weight": 1.1,
     "lists": ["/"], "mode": "static"},
    {"key": "rlzyb", "name": "人力资源部", "kind": "部门", "base": "http://rlzyb.zufedfc.edu.cn", "weight": 1.1,
     "lists": ["/"], "mode": "static"},
    {"key": "dyb", "name": "党院办/工会", "kind": "部门", "base": "https://dyb.zufedfc.edu.cn", "weight": 0.95,
     "lists": ["/index.htm"], "mode": "auto"},
    {"key": "gqt", "name": "团委", "kind": "部门", "base": "https://gqt.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/"], "mode": "static"},
    {"key": "jfy", "name": "社会合作部/经社院", "kind": "部门", "base": "https://jfy.zufedfc.edu.cn", "weight": 0.95,
     "lists": ["/"], "mode": "static"},
    {"key": "swb", "name": "后勤基建部", "kind": "部门", "base": "https://swb.zufedfc.edu.cn", "weight": 1.0,
     "lists": ["/"], "mode": "static"},
    {"key": "abb", "name": "安全保卫部", "kind": "部门", "base": "https://abb.zufedfc.edu.cn", "weight": 0.9,
     "lists": ["/"], "mode": "static"},
    {"key": "lib", "name": "图书馆", "kind": "部门", "base": "https://lib.zufedfc.edu.cn", "weight": 0.85,
     "lists": ["/"], "mode": "static"},
    {"key": "it", "name": "信息技术部", "kind": "部门", "base": "https://it.zufedfc.edu.cn", "weight": 0.9,
     "lists": ["/"], "mode": "static"},
    # 官网导航"本科生就业"指向的第三方就业信息网，招聘/双选会/考公资讯主要来源。
    # 它不是博达 CMS：/jobfair、/teachin、/campus 这些栏目页要跑 JS 才有内容（requests
    # 拿到的是空壳），但**首页是服务端渲染的**，招聘会/宣讲会/招聘公告/就业通知连同
    # [日期] 都直接写在首页 HTML 里，所以只抓首页、自带 item 正则、不自动发现栏目。
    {"key": "jy", "name": "就业指导中心", "kind": "部门", "base": "http://zufedfc.jysd.com", "weight": 1.35,
     "lists": ["/"], "mode": "auto",
     "item": r"/(?:jobfair|teachin|campus|news)/(?:view|index)/",
     "cols": {"jobfair": "招聘会", "teachin": "宣讲会", "campus": "招聘公告",
              "news": "就业通知公告"}},
]

# 已探测但外网不可达：http 返回空白文档、https 拒绝连接。需要校园网内访问。
UNREACHABLE = {
    "zxb": "党委组织部/宣传部/统战部",
    "jjs": "纪委办公室/监察室/审计室",
    "cwb": "计划财务与采购管理部",
    "zc": "资产管理部/采购中心",
}

# 认证墙特征：命中即判定需登录，跳过该 URL。
AUTH_MARKERS = ("cas.paas.zufedfc.edu.cn", "/hy-login.jsp", "webvpn.zufedfc.edu.cn",
                "/users/sign_in", "/user/login")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

REQUEST_TIMEOUT = 20
SAME_HOST_GAP = 0.28      # 同站请求间隔，避免打爆弱站
MAX_LIST_PER_SOURCE = 10  # 每站显式配置的栏目页上限
MAX_PAGES = 6             # 每个栏目向后翻的页数（含第 1 页）
MAX_DETAIL_PER_SOURCE = 45  # 每站最多进多少个详情页取正文（优先通知/公告类）
MAX_RETRY = 3             # 瞬时网络错误的重试次数（学校服务器经常重置连接）
HOST_FAIL_BUDGET = 4      # 同一站点连续失败多少次就判定不可达，本轮不再打扰
INCREMENTAL_TTL = 6 * 3600  # 栏目页多久内没变化就跳过（秒）

# 只有这些域名允许在证书链不完整时降级重试：学校子站普遍缺中间证书。
# 仅用于读取公开页面；换学校时把新域名加进来，其他域名一律严格校验。
INSECURE_ALLOWED = ("zufedfc.edu.cn",)


def insecure_allowed(url: str) -> bool:
    """这个 URL 是否允许在证书校验失败时降级读取。

    只有学校自己的域名可以降级（内网系统证书链不全很常见，且只读公开页）。爬虫和
    自检共用这一条判断，避免两边标准不一致：doctor 曾对所有站点无条件 verify=False，
    那等于把"证书有问题"这件事在别处也悄悄放过了。
    """
    from urllib.parse import urlparse

    h = (urlparse(url).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in INSECURE_ALLOWED)


def _apply_local_overrides() -> None:
    """读取同目录 `config.local.json`，让不同用户不改代码就能增删站点、调参数。

    支持：
      {"disable": ["jxjy"],                 # 按 key 关掉站点
       "add": [ {与 SOURCES 同结构的字典} ],  # 追加站点
       "limits": {"MAX_PAGES": 10},          # 覆盖抓取参数
       "weights": {"jrjm": 1.2}}             # 只调某站权重
    文件不存在或格式错就静默跳过，绝不让程序起不来。
    """
    import json
    import os
    import sys
    from pathlib import Path

    here = Path(__file__).with_name("config.local.json")
    cands = [here]
    if getattr(sys, "frozen", False):
        # 打包后 sources.py 在 exe 内部的解包目录里，用户改不到；
        # 所以额外认 exe 旁边和数据目录里的同名文件。
        root = Path(sys.executable).resolve().parent
        cands += [root / "config.local.json",
                  Path(os.environ.get("LOCALAPPDATA") or root) / "school-radar" / "config.local.json"]
    p = next((c for c in cands if c.exists()), None)
    if p is None:
        return
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for k, v in (cfg.get("limits") or {}).items():
        if k in globals():
            globals()[k] = v
    alive = {c["key"] for c in SOURCES}
    for w_key, w_val in (cfg.get("weights") or {}).items():
        for c in SOURCES:
            if c["key"] == w_key and isinstance(w_val, (int, float)):
                c["weight"] = float(w_val)
    disabled = set(cfg.get("disable") or [])
    if disabled:
        SOURCES[:] = [c for c in SOURCES if c["key"] not in disabled]
    for extra in (cfg.get("add") or []):
        if not isinstance(extra, dict) or not {"key", "name", "base"} <= set(extra):
            continue
        if extra["key"] in alive:
            continue
        extra.setdefault("kind", "学院")
        extra.setdefault("weight", 1.0)
        extra.setdefault("lists", ["/"])
        extra.setdefault("mode", "auto")
        SOURCES.append(extra)


_apply_local_overrides()
