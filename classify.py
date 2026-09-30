# -*- coding: utf-8 -*-
"""成长路径分类：概念词族 + 标题为主的打分 + 文档类型 + 受众识别 + 摘要。

纯规则、纯标准库，离线可跑，不调用任何模型 API。

打分模型 v3（概念词族）
----------------------
v1 把"标题 + 正文"拼一起匹配散词 → 会议通知正文一句"地点：某会议室"就判成场地类。
v2 拆成标题/正文两档权重，但仍是一张散词表：补了"众创空间"，来了"三号楼热水系统
检修"还是掉进其他信息，永远补不完。

v3 在 v2 的匹配循环之上加**概念词族**：一个概念 = 一组同义/近义表述，命中任意一条
即命中该概念，且同一概念的多个表述只按一次权重计（外加少量增益、封顶），不会叠加。
"维护检修"这一个概念就覆盖维修/检修/报修/更换/水管/停水/空调/热水/电梯/宿舍/施工/
改造…三十多种说法，不需要为每条通知单独加词。

三条硬规则：
  1. 概念带 scope：`title` 只认标题（歧义概念如"教室与会议室借用"），`both` 表示
     正文也算，但按 BODY_DISCOUNT 折算；
  2. 某类目若没有任何标题命中，总分再乘 NO_TITLE_PENALTY，正文证据几乎不可能赢；
  3. 跨类冲突用 `ex` 在**标题层面**显式裁决（排除词只看标题，避免正文一个无关词否掉整类）。

界面上每条结果带 `concepts`（命中的概念名）与 `hits`（概念 -> 具体表述），能直接回答
"凭什么这么分"；误判时也知道该改哪个概念。改完规则用 `python app.py --reclassify`
重算，不要重新抓取；想看单条打分过程调 `explain()`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 每类：id / 名称 / 主题色 / 成长路径基础权重 / 排除词(只看标题)
# **关键词不写在这里**：cat["kw"] 由下面的 CONCEPTS 概念词族在 import 时重建
# （见 _register_concepts）。这里再存一份散词就会造成"改了概念却不生效"——之前
# 把裸"专升本"从 further_study 删掉、考公考研那份散词还留着，就是同一类 bug。
# ex 是跨类冲突的标题级裁决表：正文里的无关词不许否掉一个类目，标题里的可以。
CATEGORIES = [
    {
        "id": "jobstart", "name": "就业创业", "color": "#e8590c", "base": 1.5,
        "kw": [],
        "ex": ["众创空间", "入驻", "孵化器", "创业园", "大创园", "教师", "事业编", "公务员"],
    },
    {
        "id": "examtgt", "name": "考公考研", "color": "#f08c00", "base": 1.45,
        "kw": [],
        "ex": ["教师招聘", "专任教师"],
    },
    {
        "id": "teacherhire", "name": "教师招聘", "color": "#5f3dc4", "base": 1.35,
        "kw": [],
        "ex": ["学生", "综合测评", "奖学金"],
    },
    {
        "id": "makeup", "name": "补考选课", "color": "#b08968", "base": 1.3,
        "kw": [],
        "ex": ["教师招聘", "公务员", "等级考试报名"],
    },
    {
        "id": "venue", "name": "场地与采购", "color": "#0c8599", "base": 1.3,
        "kw": [],
        # 维修类诉求归后勤，不让"场地"字样把水电空调维修抢走
        "ex": ["证书发放", "学位申请", "毕业论文", "座椅", "教室改造标准化考场"],
    },
    {
        "id": "contest", "name": "竞赛讲座", "color": "#c92a2a", "base": 1.1,
        "kw": [],
        "ex": ["职业规划大赛", "挑战杯", "互联网+", "创新创业大赛"],
    },
    {
        "id": "money", "name": "评奖评优公示", "color": "#2b8a3e", "base": 1.15,
        "kw": [],
        "ex": ["教师", "职工", "教学秘书", "职称", "专业技术职务", "拟聘"],
    },
    {
        # 结果公开类：综测考核成绩公示、大赛获奖名单、推优政审公示……
        # 词汇由 CONCEPTS 的 publicity / party_honor 供。
        # ex 是"挂着公示二字、但主题属于别的类目"的裁决表：奖助类公示回评奖评优，
        # 教师职称类公示回教师招聘，招标中标类公示回场地与采购。
        "id": "gongshi", "name": "公示名单", "color": "#1c7ed6", "base": 1.2,
        "kw": [],
        "ex": ["奖学金", "助学金", "资助", "勤工助学", "贷款", "励志", "困难学生", "减免",
               "教师", "职工", "职称", "专业技术职务", "拟聘", "编外", "劳务派遣",
               "考核结果", "招标", "中标", "采购", "比选", "成交", "入驻", "众创空间"],
    },
    {
        "id": "teachrun", "name": "教学运行", "color": "#7f5539", "base": 1.0,
        "kw": [],
        "ex": ["补考", "选课", "教师招聘"],
    },
    {
        "id": "research", "name": "教研成果", "color": "#3b5bdb", "base": 0.95,
        "kw": [],
        "ex": ["教师招聘", "学生"],
    },
    {
        "id": "life", "name": "后勤服务", "color": "#868e96", "base": 0.7,
        "kw": [],
        "ex": [],
    },
    {
        "id": "other", "name": "其他信息", "color": "#495057", "base": 0.45,
        "kw": [], "ex": [],
    },
]

# ---------------------------------------------------------------- 概念词族
# 一个概念 = 一组同义/近义表述，命中任意一条即命中该概念。
# cat: 归属类目；scope: "both"=标题或正文都算，"title"=只认标题；w: 概念权重
# 注册时把 words 并进所属类目的 kw，同一概念的多个命中合并计分（见 _merge_hits），
# 避免"命中词多"被误当成"证据强"而无限叠加。
CONCEPTS = {
    # ---- 后勤服务 ----
    "maintenance": {"name": "维护检修", "cat": "life", "scope": "both", "w": 2.7,
                    "words": ["维修", "检修", "报修", "修理", "维保", "更换", "水管", "自来水",
                              "停水", "停电", "断电", "抢修", "空调", "热水", "电梯", "屋面",
                              "防水", "管道", "宿舍", "学生公寓", "停车棚", "围挡", "施工",
                              "改造", "绿化", "喷药", "保洁", "消杀", "门禁", "班车", "食堂",
                              "餐厅", "后勤", "线路", "供电", "供水"]},
    "campus_safety": {"name": "校园安全", "cat": "life", "scope": "both", "w": 2.0,
                      "words": ["消防", "反诈", "诈骗", "治安", "安全隐患", "防灾", "减灾",
                                "应急演练", "烟感", "灭火器", "交通安全", "安全教育",
                                "安全检查", "交通管理", "网络安全"]},
    "service_hours": {"name": "开放与值守", "cat": "life", "scope": "both", "w": 2.0,
                      "words": ["开放安排", "开馆", "闭馆", "借阅", "值班表", "值班",
                                "寒假", "暑假", "节假日", "信箱", "钥匙", "军训", "体检",
                                "就医", "投保", "假期"]},

    # ---- 场地与采购 ----
    "space_alloc": {"name": "空间入驻", "cat": "venue", "scope": "both", "w": 3.4,
                    "words": ["众创空间", "入驻", "孵化", "创业园", "大创园", "工位",
                              "用房", "场地", "场馆"]},
    "room_booking": {"name": "教室与会议室借用", "cat": "venue", "scope": "title", "w": 2.2,
                     "words": ["教室申请", "借用教室", "借用", "教室", "会议室", "活动场地",
                               "场地申请", "借用场馆", "申请使用", "申请平台"]},
    "rent_auction": {"name": "出租与竞价", "cat": "venue", "scope": "both", "w": 3.0,
                     "words": ["出租", "招租", "竞价", "拍卖", "标的", "摊位", "门店",
                               "门面", "经营性用房", "承租", "租期"]},
    "procurement": {"name": "采购与招标", "cat": "venue", "scope": "both", "w": 2.8,
                    "words": ["采购", "购置", "购买", "招标", "中标", "流标", "比选",
                              "成交公告", "供应商", "询价", "竞争性磋商"]},

    # ---- 补考选课 ----
    # "成绩公示/考核成绩/获奖名单"这类是**结果公开**，不是考务动作，归 money 的公示名单；
    # 这里只留成绩复核/录入/成绩单这种要在教务系统里办事的说法。
    "exam_makeup": {"name": "考试与补考", "cat": "makeup", "scope": "both", "w": 3.0,
                    "words": ["补考", "缓考", "重修", "重新学习", "免考", "期末考试",
                              "期中考试", "期末", "考务", "监考", "总监考", "巡考",
                              "考试安排", "考试工作", "工作提示", "成绩复核", "成绩录入",
                              "成绩单", "补修"]},
    "course_select": {"name": "选课与学分", "cat": "makeup", "scope": "both", "w": 3.0,
                      "words": ["选课", "退课", "退补选", "改选", "改课", "换课", "免修", "免听",
                                "通识选修", "选修课", "教学计划", "课表", "排课", "开课",
                                "修读", "学分认定", "清算学分", "学分"]},

    # ---- 教学运行 ----
    "teaching_ops": {"name": "教学运行", "cat": "teachrun", "scope": "both", "w": 2.4,
                     "words": ["课时费", "课酬", "教学检查", "教学任务", "教学档案", "归档",
                               "培养方案", "课程建设", "在线课程", "教材", "校历",
                               "教学单位", "实践教学", "产教融合", "微专业", "专业导师",
                               "学籍", "转专业", "毕业论文", "答辩", "学位", "调课",
                               "停课", "教学日历", "图像信息采集", "火车票优惠", "资质核验"]},
    "cert_exam": {"name": "证书与等级考试报名", "cat": "teachrun", "scope": "both", "w": 2.6,
                  "words": ["考试报名", "等级考试", "四六级", "大学英语等级", "计算机等级",
                            "普通话测试", "教师资格考试", "证书补办", "体质健康测试"]},

    # ---- 教研成果 ----
    "research_project": {"name": "项目课题与成果", "cat": "research", "scope": "both", "w": 2.4,
                         "words": ["课题", "立项", "结题", "验收", "成果", "应用案例",
                                   "典型案例", "科研项目", "科研", "研究课题", "研究项目",
                                   "哲学社会科学", "社科",
                                   "论文", "智库", "建设点", "重点建设", "一流", "改革研究",
                                   "教学能力", "实验室信息", "发展规划"]},
    "digital_ai": {"name": "数智化与人工智能", "cat": "research", "scope": "both", "w": 2.3,
                   "words": ["人工智能", "数智", "数字化", "智慧课程", "智慧校园", "智能体",
                             "大数据", "数字贸易", "信息化"]},

    # ---- 考公考研 ----
    "civil_service": {"name": "公务员与事业编", "cat": "examtgt", "scope": "both", "w": 3.0,
                      "words": ["公务员", "选调生", "省考", "国考", "事业编", "事业单位",
                                "招录", "公考", "考公", "三支一扶", "西部计划", "征兵",
                                "应征入伍"]},
    "further_study": {"name": "升学深造", "cat": "examtgt", "scope": "title", "w": 2.3,
                      # 裸"专升本"是人群描述而非升学事项，会把专业导师名单、体质健康测试
                      # 等条目全部误拽进考公考研，只保留带升学动作的说法
                      "words": ["考研", "研究生", "研究生招生", "专升本招生", "专升本考试",
                                "专升本录取", "普通专升本", "第二学士学位", "推免", "保研",
                                "升学率"]},

    # ---- 就业创业 ----
    "jobs": {"name": "招聘与求职", "cat": "jobstart", "scope": "both", "w": 2.4,
             "words": ["招聘", "双选会", "宣讲会", "招聘会", "用人单位", "用人需求", "岗位",
                       "求职", "简历", "签约", "三方协议", "就业帮扶", "离校未就业",
                       "订单班", "企业参访", "实习", "笔试", "面试", "就业"]},
    "entrepreneurship": {"name": "创业实践与赛事", "cat": "jobstart", "scope": "both", "w": 2.7,
                         "words": ["创业", "创业实践", "训练营", "模拟公司", "沙盘", "运营仿真",
                                   "职业规划大赛", "大创", "挑战杯", "互联网+", "创新大赛",
                                   "路演", "OPC", "商业计划", "创客", "创业讲师"]},

    # ---- 教师招聘 ----
    "teacher_recruit": {"name": "教师招聘与聘用", "cat": "teacherhire", "scope": "both", "w": 3.0,
                        "words": ["教师招聘", "专任教师", "辅导员招聘", "职员招聘",
                                  "公开招聘", "招聘简章", "拟聘", "聘用", "聘任", "试讲",
                                  "竞评", "编外", "劳务派遣", "青年助讲", "助讲培养",
                                  "引进人才"]},
    "staff_assess": {"name": "教师考核与职称", "cat": "teacherhire", "scope": "both", "w": 2.6,
                     "words": ["职称", "专业技术职务", "业绩考核", "教学业绩", "考核结果",
                               "师德考核", "先进工作者", "优秀教师", "教学名师", "考核办法"]},

    # ---- 评奖评优公示 ----
    "scholarship": {"name": "奖助学金与资助", "cat": "money", "scope": "both", "w": 3.0,
                    "words": ["奖学金", "励志", "助学金", "助学贷款", "贷款", "补助", "勤工助学",
                              "学费", "资助", "困难学生", "家庭经济", "减免"]},
    "honor_eval": {"name": "评优与综测", "cat": "money", "scope": "both", "w": 2.4,
                   "words": ["评优", "表彰", "荣誉称号", "综合测评", "综测", "德育",
                             "优秀毕业生", "评选", "评审结果", "优良学风"]},

    # ---- 公示名单 ----
    # 主题证据：明确"结果已经公开"的词，够格决定归属。
    "publicity": {"name": "公示与获奖名单", "cat": "gongshi", "scope": "title", "w": 3.2,
                  "words": ["获奖", "成绩公示", "成绩公布", "榜单",
                            "入围", "入选", "候选人"]},
    # 文体证据：只说明"这条是贴出来的公示/清单"，不说明主题，所以按硬规则二不能
    # 单独定类——"课题结题验收结果公示""转专业名单公示"该回教研成果和教学运行，
    # 只有全场都拿不出主题证据时，才由它把纯公示条目接住。
    "publicity_genre": {"name": "公示与名单(文体)", "cat": "gongshi", "scope": "title",
                        "w": 2.6, "aux": True, "words": ["公示", "名单", "一览表"]},
    # 推优入党、政审、拟发展对象：本质也是把人公示出来
    "party_honor": {"name": "推优与政审公示", "cat": "gongshi", "scope": "title", "w": 2.8,
                    "words": ["推优", "入党", "发展党员", "入党积极分子", "政审", "政治审查",
                              "拟接收", "预备党员", "党课", "团组织关系", "团务", "团员"]},

    # ---- 竞赛讲座 ----
    # 竞赛**事件**留这里；"获奖名单"是结果公开，归公示名单（gongshi）
    "contests": {"name": "学科与体育竞赛", "cat": "contest", "scope": "both", "w": 2.1,
                 "words": ["大赛", "竞赛", "联赛", "锦标赛", "运动会", "辩论", "辩论赛", "征文",
                           "展演", "评比", "体育节", "选拔赛", "技能赛", "演讲比赛",
                           "知识竞赛"]},
    "lectures": {"name": "讲座与培训", "cat": "contest", "scope": "both", "w": 2.0,
                 "words": ["讲座", "讲坛", "讲堂", "沙龙", "论坛", "报告会", "会讲",
                           "工作坊", "培训", "研讨会", "交流会", "预告"]},
}

EXTRA_WORD_BONUS = 0.30    # 同一概念每多命中一个表述的加成
EXTRA_WORD_CAP = 1.6       # 单概念得分上限 = 权重 × 此倍数
WORD2CONCEPT = {}          # 表述 -> 概念名，用于命中解释与合并计分
# 文体词：说明"这是一份公开清单"，不说明主题。命中它们不能单独决定类目
# （见 classify 里的硬规则二）。由 CONCEPTS 中标了 "aux": True 的概念族填充。
AUX_WORDS = set()

CAT_BY_ID = {c["id"]: c for c in CATEGORIES}
CONCEPT_BY_ID = CONCEPTS


def _register_concepts():
    """把概念词族展开成各类目词表——**词表只有 CONCEPTS 这一份来源**。

    以前 CATEGORIES 里还另存着一份 v2 散词，注册时只做追加/覆盖，从不删除，于是
    出现"改了概念却没生效"：把裸"专升本"从 further_study 里删掉，考公考研那条散词
    还原样留着，专业导师名单照样被拽进考公考研。现在每次 import 都从概念词族重建。
    """
    for cat in CATEGORIES:
        cat["kw"] = []
    for cid, con in CONCEPTS.items():
        cat = CAT_BY_ID.get(con["cat"])
        if cat is None:
            continue
        for word in con["words"]:
            w, scope = con["w"], con["scope"]
            old = next((k for k in cat["kw"] if k[0] == word), None)
            if old:                       # 同一词被多个概念共用：取更高权重，范围从严
                w = max(w, old[1])
                scope = "title" if (old[2] == "title" or scope == "title") else "both"
                cat["kw"] = [k for k in cat["kw"] if k[0] != word]
            cat["kw"].append((word, w, scope))
            WORD2CONCEPT[word] = con["name"]
            if con.get("aux"):
                AUX_WORDS.add(word)


_register_concepts()

BODY_DISCOUNT = 0.30    # 正文命中只能当弱证据
NO_TITLE_PENALTY = 0.45  # 完全没有标题命中的类目
COLUMN_FALLBACK = 0.5    # 只靠栏目名定类：比真实标题命中低一档

# 标题里的噪音：学院全称、公文前缀
NOISE = [
    r"^浙江财经大学东方学院", r"^浙江财经大学", r"^东方学院",
    r"^关于(打印|印发|转发|报送)?", r"^转发[：:]", r"^关于印发", r"^各二级学院[、，]?部门?[：:]",
]

# 公文类型：判断"这条到底是不是通知"，用来把新闻动态类内容压到后面
DOC_TYPES = [
    ("通知", ["的通知", "关于通知", "紧急通知", "选课通知"]),
    ("公告", ["的公告", "严正声明", "声明", "招考公告", "招聘公告"]),
    ("公示", ["公示", "拟推荐", "拟聘用", "名单"]),
    ("通报", ["通报", "情况反映"]),
    ("决定", ["的决定", "关于表彰", "立项结果", "验收结果", "评审结果"]),
    ("制度文件", ["管理办法", "实施细则", "工作方案", "制度", "规定", "章程", "议事清单"]),
    ("讲座预告", ["预告", "讲坛", "讲堂", "沙龙", "会讲", "报告会"]),
    ("新闻", ["来访", "调研", "座谈", "圆满", "顺利", "开展", "举行", "召开", "参加", "获评",
              "简报", "侧记", "综述", "纪实", "一行", "赴"]),
]

# 属于"公告类"的类型：界面"只看通知/公告"筛选用
NOTICE_TYPES = {"通知", "公告", "公示", "通报", "决定"}

AUDIENCE_RULES = [
    ("全校通用", ["各二级学院", "全校", "全体师生", "各部门", "院内", "全院"]),
    ("全体学生", ["全体学生", "在校本科生", "各位同学", "学生全体"]),
    ("毕业生", ["毕业生", "毕业班", "离校", "答辩", "毕业实习", "就业方案", "三方"]),
    ("新生", ["新生", "入学", "报到"]),
    ("教师职工", ["教职工", "全体教师", "各位老师", "教师教学", "职工"]),
    ("辅导员", ["辅导员"]),
    ("团学骨干", ["团学", "团员", "团支部", "团关系", "学生助理", "学生干部", "班委"]),
    ("资助对象", ["资助", "家庭经济困难", "低保", "助学金", "勤工助学", "贷款"]),
    ("专升本", ["专升本"]),
    ("应聘考生", ["应聘人员", "报考", "考生", "笔试成绩", "面试成绩", "资格复审"]),
    ("校友", ["校友"]),
    ("参赛学生", ["竞赛", "大赛", "选拔赛", "参赛"]),
    ("入驻团队", ["入驻", "众创空间", "团队申报"]),
]

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"), None)
_FW = {ord(c): ord(h) for c, h in zip(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｔｕｗｘｚ（）：；，．！？－＋／％",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz():;,.-+/%")}

_GRADE = re.compile(r"(20\d{2})\s*级")
_GRAD = re.compile(r"(20\d{2})\s*届")

ACTION_HINTS = [
    ("招聘", "招人"), ("考核", "看考核"), ("公示", "结果公示"), ("招标", "要投标"), ("招租", "可抢租"),
    ("报名", "要报名"), ("培训", "去培训"), ("讲座", "听讲座"), ("大赛", "打比赛"), ("竞赛", "打比赛"),
    ("停水", "会停水"), ("停电", "会停电"), ("施工", "有施工"), ("选课", "记得选课"), ("补考", "有补考"),
    ("缓考", "可申请缓考"), ("答辩", "准备答辩"), ("奖学金", "申奖学金"), ("资助", "申资助"),
    ("实习", "实习安排"), ("面试", "准备面试"), ("场地", "场地相关"), ("出租", "出租信息"),
    ("入驻", "申请入驻"), ("报名", "要报名"), ("考试报名", "赶报名"), ("值班", "查值班"),
    ("军训", "军训相关"), ("归档", "交材料"), ("采购", "采购公告"),
]


def norm_text(s: str) -> str:
    """统一全角/半角、去掉零宽字符与 HTML 实体、压缩空白。

    学校 CMS 里 "8+Ｘ" 和 "8+X"、全角数字这类混排很常见，不归一化就会漏关键词；
    不同机器上解析结果也可能因此不一致。
    """
    if not s:
        return ""
    import html as _html
    s = _html.unescape(s)
    s = s.translate(_ZERO_WIDTH).translate(_FW)
    s = re.sub(r"[\u3000 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def detect_doc(title: str, column: str = "") -> str:
    """按标题公文式后缀 > 栏目名 > 正文用词 的优先级判定文档类型。"""
    t = (title or "").strip()
    for name, words in DOC_TYPES:
        if any(w in t for w in words):
            return name
    c = column or ""
    if "通知" in c or "公告" in c or "公示" in c:
        return "通知"
    if "动态" in c or "新闻" in c or "风采" in c or "简报" in c:
        return "新闻"
    return "其他"


def detect_audience(text: str) -> str:
    """识别这条通知面向谁，用于个人相关度计算与界面展示。"""
    text = norm_text(text)
    tags = [name for name, words in AUDIENCE_RULES if any(w in text for w in words)]
    tags += [m.group(0).replace(" ", "") + "学生" for m in _GRADE.finditer(text)]
    tags += [m.group(0).replace(" ", "") for m in _GRAD.finditer(text)]
    out, seen = [], set()
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return "|".join(out[:5])


@dataclass
class Verdict:
    primary: str
    primary_name: str
    color: str
    tags: list = field(default_factory=list)
    hits: dict = field(default_factory=dict)       # 概念名 -> 命中的具体表述
    concepts: list = field(default_factory=list)   # 命中的概念名（可解释依据）
    score: float = 0.0
    digest: str = ""
    audience: str = ""
    doc_type: str = "其他"
    is_notice: bool = True
    is_recent: bool = False
    confidence: str = "low"


def _clean_title(title: str) -> str:
    t = norm_text(title)
    t = re.sub(r"\s*\d{4}-\d{1,2}-\d{1,2}$", "", t).strip()
    t = re.sub(r"\s*\d{1,2}-\d{1,2}$", "", t).strip()
    for pat in NOISE:
        t = re.sub(pat, "", t).strip()
    return t or norm_text(title)


def _digest(title: str, body: str, cat_id: str) -> str:
    """把标题压成一句可扫读的提示（纯规则，不改写语义、不编造内容）。"""
    raw = _clean_title(title)
    short = raw.strip("·•|｜、,。;；:：-— ")
    if len(short) > 26:
        short = short[:26].rstrip("，、。；:") + "…"
    bits = []
    src = (raw + " " + (body or ""))[:1200]
    action = next((label for kw, label in ACTION_HINTS if kw in raw), CAT_BY_ID.get(cat_id, {}).get("name", ""))
    if action:
        bits.append(action)
    m = re.search(r"(20\d{2})[届级]", src)
    if m:
        bits.append(m.group(0))
    m = re.search(r"第([一二三四五六七八九十\d]{1,6})期", src)
    if m:
        bits.append("第" + m.group(1) + "期")
    m = re.search(r"第([一二三四五六七八九十\d]{1,6})批", src)
    if m:
        bits.append("第" + m.group(1) + "批")
    return f"{short}｜" + " · ".join(dict.fromkeys(bits)) if bits else short


def _score_concepts(cat: dict, title: str, body: str):
    """按概念词族给单个类目打分。

    返回 `(总分, 概念->命中表述, 有无主题级标题命中, 有无任何标题命中)`。
    同一概念命中多个表述只按一次权重计（额外表述小幅加成、封顶），否则一个概念
    命中 5 个词就会变成 5 倍分，把别的类目全挤掉。
    主题级 vs 文体级：AUX_WORDS 里的词（公示/名单/公告…）只表示文体，不算主题证据。
    """
    if any(x in title for x in cat["ex"]):
        return 0.0, {}, False, False   # 排除词只看标题，避免正文一个无关词否掉整类
    by_con = {}
    for kw, w, scope in cat["kw"]:
        if kw in title:
            side = "t"
        elif scope != "title" and kw in body:
            side = "b"
        else:
            continue
        cname = WORD2CONCEPT.get(kw) or kw
        slot = by_con.get(cname)
        if slot is None:
            slot = by_con[cname] = {"w": w, "t": 0, "b": 0, "topic": 0, "words": []}
        slot["w"] = max(slot["w"], w)
        slot[side] += 1
        if side == "t" and kw not in AUX_WORDS:
            slot["topic"] += 1
        slot["words"].append(kw)

    th, bh, topic_th, words = 0.0, 0.0, 0.0, {}
    for cname, s in by_con.items():
        cap = s["w"] * EXTRA_WORD_CAP
        n = s["t"] + s["b"]
        sub = min(cap, s["w"] + EXTRA_WORD_BONUS * (n - 1))
        if s["t"]:
            th += sub                            # 标题命中：全额
            if s["topic"]:
                topic_th += sub                  # 且命中里含主题词
        else:
            bh += sub * BODY_DISCOUNT            # 只有正文命中：弱证据
        words[cname] = s["words"][:4]

    raw = th + bh
    has_title_hit = th > 0
    if not has_title_hit and raw > 0:
        raw *= NO_TITLE_PENALTY
    return round(raw, 2), words, topic_th > 0, has_title_hit


def classify(title: str, body: str = "", pub_date: str = "", source_weight: float = 1.0,
             today=None, column: str = "") -> Verdict:
    """按概念词族给每条通知定类、打权重、生成摘要；标题为主、正文为辅。"""
    from datetime import date, datetime
    today = today or date.today()
    title = norm_text(title)
    body = norm_text(body)

    strengths = {}
    for cat in CATEGORIES:
        raw, words, has_topic, has_title = _score_concepts(cat, title, body)
        if raw:
            strengths[cat["id"]] = (raw, words, has_topic, has_title)

    # 硬规则一：没有标题命中就不允许定类。正文里"顺便提了一句成绩/考试"就把一条
    # 推优入党通知拽进"补考选课"，是之前反复出现的误判根因。正文证据只在已经有
    # 标题命中的类目之间当加分项，不能单独决定归属。
    # 硬规则二：只命中"公示/名单/公告"这类**文体词**的类目，地位低于命中主题词的
    # 类目，只有全场都拿不出主题证据时才有资格定类。没有这条，"课题结题验收结果
    # 公示""转专业名单公示"会被一个"公示"字全部抢进公示名单，而它们明明属于教研
    # 成果和教学运行——文体不该盖过主题。
    topic_tier = {k: v for k, v in strengths.items() if v[2]}
    genre_tier = {k: v for k, v in strengths.items() if v[3]}
    strengths = topic_tier or genre_tier

    if not strengths:
        # 最后一级兜底：标题和正文都拿不出证据时，把**栏目名**当标题看一次。
        # 就业网的企业招聘公告标题就只有公司名（"××有限公司"），"招聘公告"这个
        # 栏目名是它唯一的主题线索。打折后仍低于任何真实标题命中，不会反过来抢。
        col = norm_text(column)
        if col and not re.fullmatch(r"栏目\d+", col):
            for cat in CATEGORIES:
                raw, words, topic, hit = _score_concepts(cat, col, "")
                if raw and topic:
                    strengths[cat["id"]] = (round(raw * COLUMN_FALLBACK, 2), words, topic, hit)

    if not strengths:
        strengths = {"other": (0.6, {}, False, False)}

    ordered = sorted(strengths.items(), key=lambda kv: kv[1][0] * CAT_BY_ID[kv[0]]["base"], reverse=True)
    pid = ordered[0][0]
    cat = CAT_BY_ID[pid]
    raw_hit, hit_words = ordered[0][1][0], ordered[0][1][1]
    has_topic_hit, has_title_hit = ordered[0][1][2], ordered[0][1][3]
    density = min(1.6, 0.55 + 0.22 * len(hit_words))

    # 新鲜度：越早发布的衰减越多
    days, has_date = 0.0, False
    if pub_date:
        try:
            d = datetime.strptime(pub_date[:10], "%Y-%m-%d").date()
            days, has_date = max(0.0, (today - d).days), True
        except ValueError:
            pass
    # 无日期的条目轻度降权：列表页常见，避免它们盖过明确的新通知
    freshness = max(0.35, 1.0 / (1.0 + days / 21.0)) if has_date else 0.8

    # 公文类型因子：新闻动态压到后面，制度文件略降，通知/公告/公示保持
    doc_type = detect_doc(title, column)
    doc_factor = {"新闻": 0.42, "制度文件": 0.85, "其他": 0.9}.get(doc_type, 1.0)
    is_notice = doc_type in NOTICE_TYPES

    score = round(cat["base"] * min(raw_hit, 8) * density * freshness
                  * source_weight * doc_factor, 2)
    tags = [CAT_BY_ID[i]["name"] for i, _ in ordered[1:3]]
    confidence = ("high" if has_topic_hit and raw_hit >= 3
                  else ("mid" if has_title_hit else "low"))

    return Verdict(
        primary=pid, primary_name=cat["name"], color=cat["color"], tags=tags,
        hits=hit_words, concepts=list(hit_words.keys()),
        score=score, audience=detect_audience(f"{title} {body}"),
        doc_type=doc_type, is_notice=is_notice, confidence=confidence,
        digest=_digest(title, body, pid),
    )


def explain(title: str, body: str = "", column: str = "") -> dict:
    """给出各类目的得分与命中概念，回答"这条凭什么进这类"。"""
    v = classify(title, body, column=column)
    rows = [{"cat": c["name"], "score": round(v.score, 2) if c["id"] == v.primary else None,
             "id": c["id"]} for c in CATEGORIES]
    return {"primary": v.primary_name, "concepts": v.concepts, "hits": v.hits,
            "doc_type": v.doc_type, "audience": v.audience,
            "confidence": v.confidence, "score": v.score,
            "runner_up": v.tags}


def score_for(title: str, body: str, cat_id: str, pub_date: str = "",
              source_weight: float = 1.0, column: str = "", today=None) -> tuple:
    """按**指定类目**重算权重与依据。模型或人工改判后必须用它，否则会沿用旧类目的分。"""
    from datetime import date, datetime
    today = today or date.today()
    title, body = norm_text(title), norm_text(body)
    cat = CAT_BY_ID.get(cat_id)
    if not cat:
        return 0.0, {}, "low"
    raw, detail, t_hit, _genre = _score_concepts(cat, title, body)
    density = min(1.6, 0.55 + 0.22 * len(detail))
    days, has_date = 0.0, False
    if pub_date:
        try:
            d = datetime.strptime(pub_date[:10], "%Y-%m-%d").date()
            days, has_date = max(0.0, (today - d).days), True
        except ValueError:
            pass
    freshness = max(0.35, 1.0 / (1.0 + days / 21.0)) if has_date else 0.8
    doc_type = detect_doc(title, column)
    doc_factor = {"新闻": 0.42, "制度文件": 0.85, "其他": 0.9}.get(doc_type, 1.0)
    # 模型/人工改判的条目本身没有该类的词表证据，给一个下限，避免分数塌成 0
    raw = max(raw, 0.8)
    score = round(cat["base"] * min(raw, 8) * density * freshness
                  * source_weight * doc_factor, 2)
    conf = "high" if t_hit and raw >= 2.6 else ("mid" if t_hit else "low")
    return score, detail, conf


def growth_view(verdicts: list) -> list:
    """按成长路径聚合成总览：类目 / 条数 / 最高权重 / 近 7 天新条。"""
    from collections import defaultdict
    agg = defaultdict(lambda: {"n": 0, "top": 0.0, "recent": 0})
    for v in verdicts:
        a = agg[v.primary]
        a["n"] += 1
        a["top"] = max(a["top"], v.score)
        a["recent"] += 1 if v.is_recent else 0
    out = []
    for cat in CATEGORIES:
        a = agg.get(cat["id"], {"n": 0, "top": 0.0, "recent": 0})
        out.append({"id": cat["id"], "name": cat["name"], "color": cat["color"], **a})
    return sorted(out, key=lambda x: (x["top"], x["n"]), reverse=True)


# ================================================================ 语料自学习层
#
# 概念词族仍然可能漏掉全新表述。这一层不再依赖词表：把"规则高置信"的条目当作训练
# 样本，用字符 n-gram + TF-IDF 求每个类目的质心，再让规则没把握的条目按"和哪一类
# 最像"来归类。人工修正优先参与训练，因此改一次会影响同类的所有未来条目。
#
# 只在标准库里做（math/re），不引入 sklearn。中文没有空格，用字符 2/3 元组避免依赖
# 分词器，这也让它在不同机器上结果一致。

NGRAMS = (2, 3)
# 公文套话：几乎每条通知都有，留着它们会让质心学到"格式"而不是"主题"
BOILERPLATE = re.compile(
    r"20\d{2}\s*[-—~至]?\s*(20\d{2})?\s*学年|第?[一二三四]?\s*学期|学期|学年"
    r"|关于|的通知|的公告|的公示|的通报|关于做好|关于开展|关于公布|关于组织|关于召开"
    r"|浙江财经大学东方学院|东方学院|我院|学院|各二级学院|部门|工作|活动|安排|通知")
MIN_DF_RATIO, MAX_DF_RATIO = 0.015, 0.60   # 太稀无法泛化，太泛没有区分度
TITLE_WEIGHT = 3.0                         # 标题特征权重高于正文
# 阈值定得保守：宁可留在"其他信息"标成待确认，也不要编出一个自信的错误类目。
# 实测 MODEL_MIN_SIM 取 0.10 时，"招生章程""议事清单"这类会被硬塞进"场地与采购"。
MODEL_MIN_SIM = 0.24                       # 从"其他信息"救回所需的最低相似度
MODEL_MIN_MARGIN = 0.07
RESCUE_MIN_SIM = 0.26                      # 改判已有类目要求更高相似度
RESCUE_MIN_MARGIN = 0.07

# 建议模式：模型不再自动改类，只在够像时给出"疑似类目"让人一键采纳
SUGGEST_MIN_SIM = 0.20
SUGGEST_MIN_MARGIN = 0.05


def featurize(title: str, body: str = "") -> dict:
    """字符 n-gram 计数；标题按 TITLE_WEIGHT 倍计入。

    先剥掉公文套话（学年/学期/关于…的通知/日期）。实测不剥的话质心会被套话主导，
    任何提到"学期"的条目都扑向样本最多的那个类目，把"期初教职工大会"判成补考选课。
    """
    feats = {}
    for text, w in ((norm_text(title), TITLE_WEIGHT), (norm_text(body)[:1200], 1.0)):
        text = BOILERPLATE.sub(" ", text)
        text = re.sub(r"[\s\d（）()《》“”\"'、，。；：:;,.!！?？\-—_/]+", " ", text)
        for seg in text.split(" "):
            if len(seg) < 2:
                continue
            for n in NGRAMS:
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    feats[g] = feats.get(g, 0.0) + w
    return feats


def _l2(vec: dict) -> dict:
    import math
    s = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / s for k, v in vec.items()}


def _tfidf(vec: dict, idf: dict) -> dict:
    import math
    out = {}
    for g, tf in vec.items():
        w = idf.get(g)
        if w:
            out[g] = (1 + math.log(tf)) * w
    return _l2(out)


def build_model(docs: list) -> dict:
    """docs = [(key, category, title, body)]，返回 {idf, vecs, centroids}。

    训练样本由调用方决定（规则高置信 + 人工修正），这里只负责向量化与求质心。
    """
    import math
    from collections import defaultdict

    raw = {k: featurize(t, b) for k, t, b in _reorder(docs)}
    n = max(len(raw), 1)
    df = {}
    for v in raw.values():
        for g in v:
            df[g] = df.get(g, 0) + 1
    idf = {g: math.log(n / (1 + c)) for g, c in df.items()
           if n * MIN_DF_RATIO <= c <= n * MAX_DF_RATIO}
    vecs = {k: _tfidf(v, idf) for k, v in raw.items()}

    groups = defaultdict(list)
    for key, cat, _t, _b in docs:
        if cat and cat in CAT_BY_ID and vecs.get(key):
            groups[cat].append(vecs[key])

    centroids = {}
    for cat, vs in groups.items():
        merged = {}
        for v in vs:
            for g, w in v.items():
                merged[g] = merged.get(g, 0.0) + w / len(vs)
        centroids[cat] = _l2(merged)
    return {"idf": idf, "vecs": vecs, "centroids": centroids,
            "train_size": {c: len(v) for c, v in groups.items()}}


def _reorder(docs):
    """docs 元素是 (key, category, title, body)，转成 (key, title, body)。"""
    return [(k, t, b) for k, _c, t, b in docs]


def cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(g, 0.0) for g, v in a.items())


def predict(key: str, model: dict) -> tuple:
    """返回 (类目, 相似度, 与第二名的差距)。"""
    cent = model["centroids"]
    vec = model["vecs"].get(key)
    if not cent or not vec:
        return "", 0.0, 0.0
    scored = sorted(((c, cosine(vec, v)) for c, v in cent.items()), key=lambda x: -x[1])
    best, bs = scored[0]
    second = scored[1][1] if len(scored) > 1 else 0.0
    return best, round(bs, 4), round(bs - second, 4)
