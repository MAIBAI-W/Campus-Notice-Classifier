# -*- coding: utf-8 -*-
"""分类回归用例：改规则前后都跑一下。

    python cases.py            # 全跑，只打印失败与汇总
    python cases.py -v         # 逐条打印

这里一半是用户点过的真实反例（争议条目），一半是"已经修对过、不许再回退"的条目。
带列名的用例走的是栏目兜底路径。期望值写的是**类目 id**，见 classify.CATEGORIES。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import classify  # noqa: E402

# (来源标记, 标题, 期望类目 id, 说明[, 栏目名])
CASES = [
    # ---- 用户点名的四条 ----
    ("R", "信智学院2025-2026学年学生综测考核成绩公示", "gongshi", "成绩公示不能进补考选课"),
    ("R", "信智学院2025-2026学年第二学期学生德育量化考核成绩公示", "gongshi", "德育考核成绩公示"),
    ("R", "关于公布我院第十届智慧税务大赛获奖名单的通知", "gongshi", "获奖名单进公示"),
    ("R", "关于公布我院第三届“外研社·国才杯”“理解当代中国”短视频大赛获奖名单的通知",
     "gongshi", "国才杯获奖名单"),
    ("R", "关于公布我院第五届计算机设计大赛获奖名单的通知", "gongshi", "计算机设计大赛获奖"),
    ("S", "关于举办我院第九届英语经典朗诵大赛的通知", "contest", "办赛通知留在竞赛讲座"),
    ("S", "关于举办2026年大学生创新创业大赛的通知", "jobstart", "双创办赛留在就业创业"),
    ("S", "金融与经贸学院关于2025级本科专业导师候选结果的通知", "teachrun", "专业导师候选结果"),
    ("R", "关于公布会计学院2024级、2025级（含25会计专升本）专业导师名单的通知",
     "teachrun", "裸专升本是人群不是升学"),
    ("R", "金融与经贸学院关于2025级本科以及2026级专升本专业导师互选结果的通知",
     "teachrun", "互选结果"),
    ("S", "关于2026年普通专升本招生工作的通知", "examtgt", "真升学要留在考公考研"),
    ("S", "关于举办2026年秋季校园招聘双选会的通知", "jobstart", "双选会"),
    ("S", "关于组织2026年浙江省机关单位招录公务员的通知", "examtgt", "公务员招录"),

    # ---- 之前修对过的，不许回退 ----
    ("S", "关于开展我院众创空间第九批入驻工作的通知", "venue", "众创空间入驻"),
    ("R", "关于公布我院众创空间第七批入驻学生团队名单的通知", "venue", "入驻名单归场地"),
    ("R", "关于核对2026-2027学年第一学期缓补考学生名单的通知", "makeup", "缓补考名单"),
    ("S", "关于2025-2026学年第一学期通识选修课选课的通知", "makeup", "通识选修选课"),
    ("S", "东方学院退课程、退补选申请表", "makeup", "退补选申请表"),
    ("S", "关于期末工作的通知提示", "makeup", "期末工作提示"),
    ("S", "关于2025-2026学年学分认定和转换工作的通知", "makeup", "学分认定"),
    ("S", "关于2026年上半年全国大学英语四六级考试报名的通知", "teachrun", "裸考试不许抢走考试报名"),
    ("R", "关于浙江财经大学东方学院文化传播与设计学院2026年“观東方奖学金”评选公示",
     "money", "奖学金公示留在评奖评优"),
    ("R", "关于2025-2026学年第二学期退役士兵国家助学金名单的公示", "money", "助学金公示"),
    ("R", "关于 2026 年 “勤工助学之星” 候选人推荐名单的公示", "money", "勤工助学公示"),
    ("R", "关于成立文设学院学生资助对象认定工作组的公示", "money", "资助对象认定"),
    ("R", "浙江财经大学东方学院2026年优秀“学生资助宣传大使”名单公示", "money", "资助宣传大使"),
    ("R", "关于2024级、2025级修读直播电商创业微专业面试结果的公示", "jobstart", "创业微专业面试"),
    ("R", "关于公布我院第三十八期训练营（“8+X”模拟公司创业实训）报名成功名单的通知",
     "jobstart", "创业训练营"),
    ("R", "文化传播与设计分院大学生创新创业大赛获奖一览表", "gongshi", "获奖结果按公示处理"),
    ("R", "关于拟录用专任教师名单的公示", "teacherhire", "教师拟录用公示"),
    ("R", "党院办、纪监审办公室关于2024年度教职工考核结果的公示", "teacherhire", "教职工考核公示"),
    ("R", "关于创业学院、继续教育学院2025-2026学年教学业绩考核结果的公示",
     "teacherhire", "创业学院是机构名，不许抢走教学业绩考核"),
    ("R", "2026年9月拟接收王某某等173名同志为入党申请人的公示", "gongshi", "入党申请人公示"),
    ("S", "关于2025年人工智能应用案例征集的通知", "research", "人工智能应用案例"),
    ("S", "关于第42个教师节祝全体教师节日快乐的通知", "other", "教师节祝福"),
    ("S", "关于召开2025-2026学年第一学期期初全体教师会议的通知", "other", "期初教师会议"),
    ("S", "关于3号学生公寓热水系统检修停水的通知", "life", "热水检修停水"),
    ("S", "关于2025年国家学生体质健康测试工作的通知", "teachrun", "体质健康测试"),
    ("S", "关于文设学院实训室设备采购招标公告", "venue", "采购招标"),
    ("S", "关于举办数字经济讲坛第12讲的通知", "contest", "讲坛"),
    ("S", "关于2025-2026学年第一学期微专业建设试点申报的通知", "teachrun", "微专业申报"),

    # ---- 文体词不许盖过主题词 ----
    ("R", "关于2025-2026学年第二学期转专业名单的公示", "teachrun", "转专业公示回教学运行"),
    ("R", "院级第三批微专业建设试点立项结果公示", "teachrun", "微专业条目统一回教学运行"),
    ("R", "省级一流本科专业建设点验收结果公示", "research", "验收结果回教研成果"),
    ("R", "关于2026年度省级大中小学课程教材研究项目立项及国家级项目推荐结果的公示",
     "research", "教材立项公示回教研成果"),
    ("R", "教务管理部关于2025-2026学年先进工作者评选结果的公示", "teacherhire", "先进工作者回教师招聘"),
    ("R", "浙江财经大学东方学院2026-2027学年学生会主席团、团委秘书处拟任人选名单公示",
     "gongshi", "全场无主题证据时由文体层接住"),
    ("R", "关于公布2025-2026学年第二学期课堂教学满意度调查获奖学生名单的通知",
     "gongshi", "获奖名单"),

    # ---- 栏目名兜底：标题只有企业名 ----
    ("R", "宁波中升国际货运代理有限公司", "jobstart", "招聘公告栏目", "招聘公告"),
    ("R", "杭州花果山数艺教育科技有限公司", "jobstart", "招聘公告栏目", "招聘公告"),
    ("R", "某某文化传播有限公司", "other", "栏目名没有主题线索就不该硬归类", "学生风采"),
    ("S", "关于学生公寓热水器检修的安排", "life", "标题已有证据时栏目名不参与", "通知公告"),
]


def check_page_js() -> int:
    """校验界面那段内联 JS 能不能解析。

    PAGE 是个几百行的 JS 字符串，语法错了服务照样起、页面照样返回 200，但整段脚本
    一行都不执行——表现就是"页面打开了一片空白"，而且控制台之外没有任何报错。
    有 node 就用 node --check 真解析；没有就跳过并说明。
    """
    import re
    import shutil
    import subprocess
    import tempfile

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
               encoding="utf-8").read()
    m = re.search(r'^PAGE = r?"""(.*?)"""', src, re.S | re.M)
    if not m:
        print("PAGE 常量结构变了，检不了（如果是有意改名，请同步改这里）")
        return 0
    js = "\n".join(re.findall(r"<script>(.*?)</script>", m.group(1), re.S))
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        print("JS 语法检查：跳过（没装 node，无法解析内联脚本）")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if r.returncode:
            print("JS 语法错误：\n" + (r.stderr or "")[:600])
            return 1
        print(f"JS 语法检查：通过（{len(js)} 字符）")
        return 0
    finally:
        os.unlink(path)


def run(verbose=False):
    fails = []
    for row in CASES:
        kind, title, want, note = row[:4]
        column = row[4] if len(row) > 4 else ""
        v = classify.classify(title, "", column=column)
        if verbose or v.primary != want:
            hits = "; ".join("%s:%s" % (c, "/".join(w)) for c, w in v.hits.items()) or "-"
            flag = "PASS" if v.primary == want else "FAIL"
            print(f"{flag} [{kind}] {v.primary}({v.score:.2f}) 期望{want} 命中={hits}")
            print(f"     {title[:60]}")
            if v.primary != want:
                print(f"     ↑ {note}")
        if v.primary != want:
            fails.append((title, want, v.primary, note))
    print(f"\n{len(CASES)} 例：{len(CASES) - len(fails)} 通过 / {len(fails)} 失败")
    return 1 if fails else 0


def check_dates() -> int:
    """日期推断的回归用例。

    真实事故：列表页只写"11-29"，代码贴上今年年份，把 2024-11-29 那条
    《外语教学电台调频接收机维修检修》变成 2026-11-29，还被界面分进"今天"。
    """
    from datetime import date

    import crawl

    today = date.today()
    fails = []

    # 1) 只有月日 → 不允许产生未来日期，且必须标记为"不精确"
    #    夹具形状照真实页面来：日期在 li 行内的兄弟节点里，不在锚文本里
    html = ('<html><body><ul><li><a href="/info/1003/1873.htm">关于外语教学电台调频'
            '接收机维修检修的通知</a><span class="time">11-29</span></li></ul></body></html>')
    items = crawl.parse_listing(html, "https://it.zufedfc.edu.cn/index/tzgg.htm", "static")
    if not items:
        fails.append("parse_listing 没抽到条目，用例失效了")
    else:
        it = items[0]
        if not it["date"]:
            fails.append("月日日期没被抽出来，用例没生效")
        elif it["date"] > today.strftime("%Y-%m-%d"):
            fails.append(f"月日推断产生了未来日期：{it['date']}")
        elif it.get("date_exact"):
            fails.append("月日推断被标成了精确日期，详情页日期就无法覆盖它")

    # 2) 完整日期 → 原样信任，即使是未来（双选会举办日）
    #    就业网首页的真实形状就是把 [日期] 写在锚文本里
    html2 = ('<html><body><ul><li><a href="/jobfair/view/id/58263">'
             '[2026-10-20] “入海”计划|浙江财经大学东方学院2027届毕业生秋季综合双选会</a>'
             '</li></ul></body></html>')
    items2 = crawl.parse_listing(html2, "https://zufedfc.jysd.com/", "auto",
                                 item_re=__import__("re").compile(r"/jobfair/view/"),
                                 cols={"jobfair": "招聘会"})
    if not items2:
        fails.append("完整日期用例没抽到条目")
    elif not items2[0]["date"].startswith("2026-10-20"):
        fails.append(f"完整日期被改写了：{items2[0]['date']}")
    elif not items2[0].get("date_exact"):
        fails.append("完整日期被当成推断，将来会被无谓地回退")

    # 3) clamp_past 按整年回退，且不动过去的日期
    for src, want in (("2099-05-06", "2098-05-06"), ("2020-01-01", "2020-01-01"), ("", ""), ("坏值", "坏值")):
        got = crawl.clamp_past(src)
        if src == "2099-05-06":
            if got >= src:
                fails.append(f"clamp_past 没把未来日期往前挪：{src} → {got}")
        elif got != want:
            fails.append(f"clamp_past({src!r}) = {got!r}，期望 {want!r}")

    for m in fails:
        print("FAIL", m)
    print(f"日期用例：{'全部通过' if not fails else str(len(fails)) + ' 条失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    bad = run("-v" in sys.argv)
    # 三段都要跑，别因为分类失败就跳过后面的检查
    raise SystemExit(bad + check_page_js() + check_dates())
