# AGENTS.md — 给 AI 接手者看的接口与改动说明

这份文档面向**要改代码的人或 AI**。面向使用者的说明在 `README.md`。

规则：改代码前先读完 §4（打分模型）和 §7（陷阱清单）。这两节里的每一条都是踩过之后写下的，
不是理论偏好。改完必须跑 §6 的三条命令。

工作目录：`D:\vibe coding\projects\school-radar`（Windows，纯 Python 标准库 + 3 个第三方包）。

---

## 1. 模块与真实接口

### 全部命令行入口

| 命令 | 作用 |
|---|---|
| `python app.py` | 全量抓取（含详情正文）+ 终端速览 + 起网页 |
| `python app.py --quick` | 只读列表页标题，不进详情页（约 8 秒）。正文里的受众线索会退化 |
| `python app.py --serve-only` | 不抓取，用库里已有数据起网页 |
| `python app.py --reclassify` | **改了分类/权重规则后用它**：只重算权重与摘要并跑模型建议，不联网，秒级；跑完直接退出 |
| `python app.py --review` | 列出规则没把握的条目与模型建议，编号供 `--correct` 用 |
| `python app.py --correct 7 life` | 采纳建议或人工改判（走 `set_label`，进训练集） |
| `python app.py --setup` | 重新填个人档案 |
| `python app.py --crawl-only --days 14` | 只抓取并打印终端速览，不起网页 |
| `python app.py --no-open --port 8899` | 不自动开浏览器 / 指定端口 |
| `python app.py --doctor` | 环境自检（换机器第一步） |
| `python cases.py [-v]` | 52 条分类回归 + `PAGE` 内联 JS 语法检查 |

### 为什么它在别的机器上不会崩（改动时保持这些性质）

| 问题 | 处理 |
|---|---|
| 学校子站证书链不全 | 只对 `sources.insecure_allowed()` 命中的域名在 SSLError 后降级重试；其他域名严格校验，重定向跳出白名单也不认 |
| 页面既有 UTF-8 也有 GBK | `_decode()` 五级兜底：响应头 charset → HTML meta → 统计推断 → utf-8 → gb18030，不依赖 `apparent_encoding` |
| 弱站频繁重置连接 | `_raw_get()` 线性退避重试 `MAX_RETRY=3` |
| 某站整站抽风 | 按 host 计数熔断，连续失败 `HOST_FAIL_BUDGET=4` 次后本轮不再请求（`dead`） |
| 单站解析抛异常 | `crawl_all` 逐站 try/except，异常只进 `report`（`crash:*`），不中断整轮 |
| 端口被占 | `free_port()` 从 8765 往后找 12 个 |
| 两个实例同时写库 | `PRAGMA busy_timeout=5000` |
| 老 conhost 不支持 UTF-8 字形 | `stdout.reconfigure(errors="replace")` + `_can()` 检测后 `★ █ ⏰` 降级成 `* # !` |
| 依赖缺失 | 启动前检查并打印 `pip install ...`，不抛 ImportError 栈 |
| 系统代理干扰 | `sess.trust_env=False`，不吃 Clash 代理 |
| 别人要改站点清单 | `config.local.json`，不动代码 |

```
sources.py   外部世界配置：站点清单、抓取限额、认证墙特征、域名白名单
   ↓
crawl.py     HTTP 抓取 + 列表/详情解析 + SQLite 读写 + 幂等迁移 + 自学习编排
   ↓
classify.py  概念词族分类、全局 score、文档类型、受众、摘要、n-gram 建议模型
   ↓
profile.py   个人档案 + 个人相关度 p_score（读取时算，不落库）
   ↓
app.py       CLI 入口、终端速览、http.server 网页工作台与 JSON API
doctor.py    环境自检（python app.py --doctor）
cases.py     分类回归用例 + 内联 JS 语法检查（改规则/改界面后必跑）
```

### classify.py

| 接口 | 契约 |
|---|---|
| `CATEGORIES: list[dict]` | 12 个类目：`id / name / color / base / kw / ex`。**`kw` 必须是空列表**，它由 `_register_concepts()` 在导入时重建。`ex` 是只在**标题**上生效的跨类裁决词。 |
| `CONCEPTS: dict[str, dict]` | 概念词族，唯一词表来源。每项：`name / cat / scope / w / words`，可选 `aux: True`（文体词）。 |
| `classify(title, body="", pub_date="", source_weight=1.0, today=None, column="") -> Verdict` | 主入口。`column` 参与两处：文档类型判定、以及标题正文都无证据时的栏目名兜底。 |
| `Verdict` | `primary, primary_name, color, tags, hits(概念→表述), concepts, score, digest, audience, doc_type, is_notice, is_recent, confidence` |
| `explain(title, body="", column="") -> dict` | 排查误判用：给出定类、命中概念与表述、置信度、亚军类目。 |
| `score_for(title, body, cat_id, ...) -> (score, detail, conf)` | **按指定类目重算**。人工改判/模型改判后必须用它，否则会沿用旧类目算出的分。 |
| `_score_concepts(cat, title, body) -> (raw, words, has_topic, has_title)` | 单个类目的打分。四个返回值里第 3、4 个分别表示"命中了主题词"和"命中了任何标题词（含文体词）"。 |
| `build_model(docs) / predict(key, model) / featurize(title, body)` | 字符 2/3-gram + TF-IDF 质心层，纯标准库。只用于**建议**。 |

模块级常量：`BODY_DISCOUNT=0.30`、`NO_TITLE_PENALTY=0.45`、`COLUMN_FALLBACK=0.5`、
`EXTRA_WORD_BONUS=0.30`、`EXTRA_WORD_CAP=1.6`、`AUX_WORDS: set`、
`SUGGEST_MIN_SIM=0.20`、`SUGGEST_MIN_MARGIN=0.05`。

### crawl.py

| 接口 | 契约 |
|---|---|
| `fetch(url, sess) -> (final_url, html, err)` | `err` 非空即失败：`auth` / `unreachable` / `dead`(已熔断) / `error:*`。内含重试、编码五级兜底、http→https→`/index.htm` 试错。 |
| `parse_listing(html, page_url, mode, item_re=None, cols=None) -> list[dict]` | 条目链接形态由 `mode`（博达 static/dynamic/auto）或站点自带的 `item_re` 决定。**只接受 http/https 协议的链接**。返回 `{url,title,date,column,row}`。 |
| `parse_detail(html, ...) -> (body, pub_date, files)` | 正文只用于分类与受众，界面不展示全文。 |
| `harvest_listing(url, sess, mode, max_pages, known=None, item_re=None, cols=None)` | 栏目页 + 分页。`known` 是库里已有 ckey 集合，用于增量跳过。 |
| `crawl_source(cfg, sess, want_detail, known=None) -> (cfg, items, status)` | 单站全流程。配了 `cfg["item"]` 的站点**跳过 `discover_columns()`**。 |
| `crawl_all(want_detail=True, workers=6, known=None) -> (rows, report)` | 并发跨站、串行同站；单站异常只进 `report`（`crash:*`）。 |
| `canon(url) -> str` | 去重键：抹平 http/https、合并斜杠、只留 `urltype/wbtreeid/wbnewsid/id` 并排序。 |
| `open_db(path)` | 建表 + **幂等自愈迁移**（见 §2）。 |
| `save_items(conn, rows) -> int` | 单条 `INSERT ... ON CONFLICT(ckey) DO UPDATE`。列名与占位符必须一一对应（曾因此整轮抓取崩在写库）。 |
| `load_items(conn, days, only_unread, category, q, limit, source, audience, doc_type)` | 读取时按 `(title, source)` 折叠重复。 |
| `set_label(conn, url, category, note="") -> bool` | 人工修正，**按标题覆盖同题多 id 的副本**，并进入模型训练集。 |
| `refine_with_model(conn) -> stats` | 编排：规则重算 → 建模型 → 写 `suggest/suggest_sim`。优先级 人工 > 规则高置信 > 模型建议 > 原样。 |

### profile.py / app.py

- `personalize(item, p, today_year) -> (p_score, why)`：学院 ×1.35/×0.72、受众 ×1.45/×0.55、
  专业 ×1.20、资助块影响奖助类。`why` 是给界面直接显示的理由列表，**改乘数必须同步改 why**。
- `apply_profile(body, cur) -> Profile`：档案写入的唯一入口，逐字段校验类型与取值。
  不要绕过它直接 `setattr`——档案会落盘，脏值会让 `personalize` 对每条通知抛异常，
  整个列表页从此 500，只能删 `profile.json` 才能恢复。
- `app_root()` / `data_dir()`：数据目录。开发态是 `./data`；打包成 exe 后是 exe 旁边，
  不可写则退回 `%LOCALAPPDATA%\school-radar`。**任何新增的落盘文件都要走这两个函数。**

---

## 2. 数据契约

`data/radar.db`（gitignore）：

- `items`（29 列）：`url`(PK) `ckey`(唯一) `source` `source_name` `column` `title` `digest`
  `body` `pub_date` `deadline`(遗留，恒空) `category` `category_name` `color` `tags` `hits`
  `score` `is_new` `is_recent` `read` `first_seen` `last_seen` `audience` `doc_type`
  `is_notice` `files` `confidence` `method` `suggest` `suggest_sim`
- `runs`：`id` `started` `finished` `total` `detail` `report`
- `labels`：`url` `category` `title` `note` `created`（人工修正，参与训练）

迁移在 `crawl.open_db()`，**幂等自愈**，接手时不要改成"要求删库重建"：

1. 缺列就 `ALTER TABLE ADD COLUMN`；
2. 只要有任意 `ckey` 为空就重跑去重回填；
3. 整段包在 `with conn:` 里——**SQLite 的 `ALTER TABLE` 会隐式提交，但后面的 DML 不会**，
   早期版本因此出现过"重复行合不掉"，别再改回去；
4. 最后建 `ckey` 唯一索引。

两层去重要分清：`ckey` 解决"同一篇文章 http/https 各存一份"；`load_items()` 的
`(title, source)` 折叠解决"同一篇稿子挂进多个栏目、拿到不同 article id"。

---

## 3. HTTP API 契约

只监听 `127..0.0.1`（**不要改成 `0.0.0.0`**）。

| 方法 路径 | 参数 | 说明 |
|---|---|---|
| `GET /` | — | 单页应用，HTML 内联在 `app.PAGE`（是 `r"""` 原始字符串） |
| `GET /api/items` | `days,q,cat,source,aud,type,unread,sort` | items + overview + colleges + colleges_all + profile + last_run + report + unreachable + categories + total_all + max_p/max_score |
| `GET /api/refresh` | — | 起一轮抓取（`_state["crawling"]` 互斥），立即返回 |
| `GET /api/read` | `url,v` | 标记已读/未读 |
| `POST /api/correct` | `{url, category, note}` | 人工改判，走 `set_label` + 重训 |
| `POST /api/profile` | 档案字段子集 | 走 `apply_profile`，非法字段静默忽略 |

约定：

- `/api/*` 全部经过 `Handler._same_origin()`：带 `Origin`/`Referer` 且与 `Host` 不一致 → 403。
  没有这两个头（curl、本机脚本）放行。别删这道检查——只听 127.0.0.1 挡不住浏览器里
  别的网页对本机发请求。
- `/api/items` 的每条 item 只发 `app.ITEM_FIELDS` 里那 19 个字段（在 `Handler._api_get`
  里裁剪，**不在 `build_payload` 里裁剪**）。原因：`build_payload` 同时喂网页和终端速览
  `print_panel`，后者要用 `item["category"]` 做成长路径聚合；把裁剪放进共享函数会直接
  `KeyError: 'category'` 崩在启动路径上（实测踩过）。加字段进界面时，要同时改
  `ITEM_FIELDS`，否则浏览器拿不到该字段而终端却能用——这种不一致最难查。
- `load_items` 默认不查 `body` 列（`_item_select` 跳过），因为界面从不展示正文；
  分面计数走 `cols="title, source, category"` 只查三列。
- 所有查询参数用 `_to_int()` 取数，任何解析失败都不该让请求抛栈。
- `sort` ∈ `personal`(默认) / `score` / `date`。
- 前端 `window.D` 缓存最近一次 payload，改档案后 `render(window.D)` 复用数据不重取。

### 界面实现

`app.PAGE` 是一个 `r"""` 原始字符串里的完整单页：浅色主题、双栏筛选、按发布时段分组
（今天 / 近3天 / 近7天 / 近30天 / 更早 / 未标注）。样式全在 `<style>` 的 `:root` CSS 变量里，
改主题只动变量。视图模式存 `localStorage`（键 `sr-mode`），不落服务端。

侧栏计数用**分面算法**：类目轨忽略类目自身、学院轨忽略学院自身，否则选中某类目时其他计数会
归零、轨道内容乱跳。顺序固定按 `CATEGORIES` / `SOURCES` 声明顺序，**永不随计数重排**。
所有插值走 `esc()`（转义 `& < > "`），属性位也安全；`open2()` 只允许 `http(s)`。

---

## 3.5 许可证约束（不要违反）

本项目是 **GPL-3.0**（见 `LICENSE`）。两条对维护者有实际影响的规则：

- **不要引入 GPL 不兼容的依赖或代码**（典型如 MIT/BSD/Apache-2.0 可以，GPL-2.0-only、
  AGPL-only、以及各类闭源 SDK 不行）。新增第三方包之前先确认许可证。
- **不要擅自改变或放宽许可证**，也不要在分发版本（`build_exe.py` 的产物）里去掉 `LICENSE`
  与源码可得性——那正是这份许可证要防的事。

## 4. 打分模型（改之前必须理解）

```
score = cat.base × min(raw_hit, 8) × density × freshness × source_weight × doc_factor
p_score = score × Π(personalize 的乘数)      # 不落库，改档案立即生效
```

四条硬规则，按顺序生效：

1. **正文只值 0.30**，且没有任何标题命中的类目总分再 ×0.45。
2. **没有标题命中就不允许定类**（`topic_tier`/`genre_tier` 都空 → 其他信息）。
   起因：正文一句"地点：某会议室"把会议通知判成场地类。
3. **文体词不能盖过主题词。** 概念可标 `aux: True`（`公示/名单/一览表`），它们的命中计入
   分数但**不构成定类资格**；只有全场都没有主题证据时，文体级类目才参与。
   起因：把"公示"当强词后，公示名单一度吞掉 71 条，里面全是
   `课题结题验收结果公示`、`转专业名单公示`——主题明明属于教研成果和教学运行。
   同一逻辑还删掉了裸`成绩`、裸`考试`、裸`专升本`：**判断标准是这个词有没有告诉你主题，
   还是只告诉你文体/人群。**
4. **栏目名兜底**：标题正文都零证据时，把 `column` 当标题算一次，乘 `COLUMN_FALLBACK=0.5`。
   只可能救回本来要掉进其他信息的条目，不可能抢过真实标题命中。

合并计分：同一概念命中多个表述只按一次权重计（额外表述 +0.30，封顶 权重×1.6）。
跨概念才相加。

调规则的优先顺序：
1. 看 `hits` 判断是哪个概念命中的；
2. 覆盖不足 → 往该概念 `words` 加表述（同义词、近义说法，不是同一条通知的标题）；
3. 跨类抢词 → 类目 `ex`（只看标题）裁决，或把概念 `scope` 收紧成 `title`；
4. 改完跑 `python app.py --reclassify`（秒级，不联网）。

### 文档类型是第三层信号

`classify.detect_doc()` 优先级：标题公文式后缀 → 栏目名 → 兜底。

| 类型 | 判定词例 | `doc_factor` | 属公告类 |
|---|---|---|---|
| 通知 / 公告 / 公示 / 通报 / 决定 | "…的通知""声明""公示""评审结果" | 1.0 | 是 |
| 讲座预告 | 预告、讲坛、讲堂、沙龙、会讲 | 1.0 | 否 |
| 制度文件 | 管理办法、实施细则、工作方案、章程 | 0.85 | 否 |
| 新闻 | 召开、调研、座谈、圆满、来访、简报 | **0.42** | 否 |
| 其他 | 都不命中 | 0.9 | 否 |

两个作用：把首页大量新闻动态压到后面；让详情页配额优先给公告类（受众、专业线索多在正文）。

### 演进史（为什么规则长这样）

- **v1**：标题+正文拼一坨匹配 → 会议通知正文一句"地点：某会议室"判成场地类、公示名单正文
  出现"面试"判成就业考公。
- **v2**：拆标题/正文两档权重 + `scope` + 只查标题的 `ex`。同一份数据上
  "期初全体教师会议"从场地类回到其他信息。
- **v3**：散词表换成概念词族（一个概念覆盖几十种说法），否则永远在打地鼠。
- **v3.5**：加"没有标题命中就不允许定类"硬规则。代价是其他信息从 172 涨到 300，
  这是**有意接受的**——宁可少归类，不可错归类。
- **v4**：词表唯一来源 + 公示名单成类 + 文体词不定类 + 栏目名兜底。

---

## 5. 抓取扩展

### 取数顺序（`crawl_source`）

1. `lists` 里的栏目页（各自翻页）；
2. 条目不足 12 条则补抓首页；
3. `discover_columns()` 从首页导航里自动发现含"通知/公告/就业/招聘/场地/采购…"的栏目再抓
   （上限 12）。这一步把就业类条目从 10 条提到 41 条，**别删**；
4. 详情页按"是否公告类 → 权重"排序，只给前 `MAX_DETAIL_PER_SOURCE` 条拉正文；
5. 增量：`known` 里某栏目第一页前 5 条全见过 → 判定没更新，不翻页。

分页上线后总量 774 → 1098，教务管理部 26 → 327。

---

## 5. 抓取扩展

### 加一个博达站
`sources.SOURCES` 加一条 `key/name/kind/base/lists/mode/weight`。列表页打不开就填 `["/"]` 从首页取。

### 加一个非博达站
再加 `item`（条目链接正则）和 `cols`（路径段→栏目名），`lists` 只填**能直接出条目**的页。
就业信息网就是这一类：它的 `/jobfair`、`/teachin`、`/campus` 栏目页是 JS 空壳（requests 拿回
5 万字、0 条链接，加 cookies/请求头都无效），但**首页服务端渲染**，只抓首页就有 110 条。
这类站配了 `item` 会自动跳过栏目发现。

### 抓取参数（`sources.py`）
`MAX_PAGES=6`、`MAX_DETAIL_PER_SOURCE=45`、`MAX_RETRY=3`、`HOST_FAIL_BUDGET=4`、
`SAME_HOST_GAP=0.28`、`REQUEST_TIMEOUT=20`。
注意 `SAME_HOST_GAP` 是**单进程**限速：多人同时用同一批学校站点时请求量是叠加的，
不要为了快把它调小。

### 不改代码定制
`config.local.json`（exe 旁边或数据目录也认）：`disable / add / weights / limits`。
JSON 写错静默跳过，不让程序起不来。

---

## 6. 改完必须跑的三条命令

```bash
python cases.py                # 52 条分类回归 + 内联 JS 语法检查，应全绿
python app.py --reclassify     # 用当前规则重算全库，秒级
python app.py --doctor         # 换机器/换网络时；22 站可达性 + 环境检查
```

`cases.py` 里一半是用户点过的真实反例，一半是"已经修对过、不许回退"的条目。
**新增误判时先往 `CASES` 加一条，再改规则**，否则同一个坑会被下一个人再踩一次。

界面改动不能只跑语法检查：`PAGE` 是内联 JS，语法错会让整段脚本静默不执行，
表现是"页面打开一片空白"而服务照样 200。必须真在浏览器里看过（`python app.py --serve-only`）。

---

## 7. 陷阱清单（都真的踩过）

- **词表只有 `CONCEPTS` 一份**。往 `CATEGORIES["kw"]` 塞散词等于埋雷：注册时会被重建覆盖，
  而在重建之前它又确实生效，导致"改了概念没生效"。
- **模型只建议、不改判**。第一版让 n-gram 自动改判时，它把"期初教职工大会"判成补考选课、
  "招生章程"判成场地与采购——因为质心学到的是**公文格式**而不是主题。修法是 `BOILERPLATE`
  先剥套话 + 提高阈值，但**仍然只允许建议**。自动改判的错误会被当成事实读走。
- **`--reclassify` 不抓取**，抓取只在没有 `--serve-only/--reclassify` 时发生；`--reclassify`
  重算完直接退出，不要顺手起服务挡住终端。
- **`set_label` 按标题覆盖**：同一篇稿子常被学校发成多个 id，只按 url 改会出现"界面上没生效"。
- **档案写入必须走 `apply_profile`**（见 §1）。
- **`p_score` 不落库**是有意设计：改档案立即生效。别为了"性能"把它存进去。
- **证书降级只认 `sources.insecure_allowed()`**：判断是"等于学校域名或其子域"，
  早先用子串匹配，`zufedfc.edu.cn.attacker.com` 也会被放行。`doctor.py` 与 `crawl.py` 共用
  这一个判断，不要各写一份。
- **不抓需要登录的内容**：撞 CAS / `/user/login` / webvpn 一律跳过并在界面标注。不存任何凭据。
- **`items.deadline` 是遗留空列**，接口已不返回；要清掉得显式 `ALTER TABLE ... DROP COLUMN`。
- **中文路径/GBK 控制台**：`app.py` 启动即 `stdout.reconfigure(encoding="utf-8", errors="replace")`，
  字形不支持时 `★ █ ⏰` 降级为 `* # !`。别在模块导入期打印中文再指望控制台能接。
- **`start.bat` 只能写 ASCII，且必须 CRLF**。cmd 解析含多字节字符的批处理时会算错字节偏移，
  把 `REM` 注释的后半截当命令执行（实测报 `'次，只认跑得通的那个。' is not recognized`）。
  另外重定向到空设备的写法很容易被工具链按 POSIX 习惯改成 `/dev/null`——那样每个 Python 探测
  都会失败并报"系统找不到指定的路径"，同学双击就是"找不到 Python"。现在的写法是
  `set "NULLDEV=NUL"` + `1>%NULLDEV% 2>&1`，改完务必确认文件里没有 `/dev/null`。
- **`.gitattributes` 固定 `*.bat text eol=crlf`**，否则换台机器 checkout 又变回 LF。

---

## 8. 已知可优化点（想动就从这里挑）

1. **类目互斥**：一条通知只有一个 `primary`。用户提过"选评奖评优时学院栏要联动"这类需求，
   真正的解法是支持交叉归类（`tags` 已有雏形），会动 `load_items` 与界面筛选逻辑。
2. **定时抓取**：目前靠手动或界面按钮。可加 `--interval` 或注册计划任务，但要考虑
   §5 的多用户限速叠加问题。
3. **详情页配额**：`MAX_DETAIL_PER_SOURCE=45` 是固定值，可按"该站近 7 天新增数"动态分配。
4. **`PAGE` 单文件**：内联 HTML/JS 已 300+ 行，再加功能应拆到 `static/`，别继续堆进字符串。
5. **模型层可换更好特征**：现在是无分词的字符 2/3-gram。若引入分词，必须同时更新
   `BOILERPLATE` 并**保持"只建议"**这条底线。
6. **摘要 `_digest()` 是规则压缩**，不做语义总结；要上生成式摘要必须离线、不外发数据。

---

## 9. 打包与分发（build_exe.py）

```bash
python build_exe.py            # 自检 → 两个变体 → dist/school-radar-win64.zip
python build_exe.py --onefile  # 额外再打一个单文件版（只自用，不分发）
```

构建前它会先跑 `cases.py`，不绿就不打包。产物：

| 文件 | 说明 |
|---|---|
| `school-radar.exe` | `--noconsole`，双击即用，输出写进 `data/school-radar.log` |
| `school-radar-cli.exe` | 带控制台，排错和 `--doctor` 用 |

**降低杀软误报的四个决定，改动前先看懂再动：**

1. **默认 `--onedir`，不用 `--onefile`。** onefile 每次启动把自身解压到 `%TEMP%` 再执行，
   这个行为与大量恶意样本一致，是最主要的启发式误报来源。
2. **`--noupx`。** UPX 壳本身无害，但签名库对它极度敏感。
3. **写完整版本资源**（`build/pyi/version_info.txt`，产品名/公司/版权/版本号）。裸 exe 更容易被猜。
4. **两个变体必须用不同 `--name`。** 共用名字时后一个构建会直接覆盖前一个的产物——
   打出来的"无控制台版"其实是控制台版换个文件名，肉眼完全看不出来（这里踩过）。
   输出路径一律 ASCII：中文目录名在部分压缩工具和目标机上会出问题。

真正能消除提示的只有代码签名证书，本机没有，所以构建完脚本会提醒先上 VirusTotal 扫，
并告知同学 SmartScreen 的「更多信息 → 仍要运行」路径。

**无控制台带来的两个代码约束**（别在打包后才想起来）：

- `_can()` 不能假设 `sys.stdout` 有 `encoding`——PyInstaller 会塞一个 NullWriter 进来。
- 向导读输入失败要退化成默认值（`profile.ask_profile` 里的 `ask()` 捕获的是 Exception，
  不只是 EOFError），并且 `app._attach_log_if_headless()` 会把 stdout 转到日志文件，
  否则同学遇到"双击没反应"时什么线索都没有。

## 10. 词表文档同步

`.github/prompt.md` 里的完整拦截词表由 `tools_lexicon.py` 从 `classify.py` 生成：

```bash
python tools_lexicon.py          # 重新生成
python tools_lexicon.py --check  # 只检查是否过期
```

**改了概念词表就跑一次**，不要手改那份区块（标记 `<!-- LEXICON:BEGIN/END -->` 之间）。

---

## 11. 现状快照（2026-09-30）

12 类目 / 26 概念 / 368 条表述；22 个抓取来源；库内 1208 条（other 291、就业创业 253、
教学运行 161、竞赛讲座 147、补考选课 86、教研成果 59、评奖评优公示 47、场地与采购 44、
教师招聘 37、公示名单 35、后勤服务 30、考公考研 18）；671 条有正文；151 条有模型建议、
291 条待确认。全量抓取 40–90 秒。
