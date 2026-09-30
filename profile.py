# -*- coding: utf-8 -*-
"""用户档案：首次使用的命令行向导，以及"个人相关度"的算法。

设计要点：个人相关度是**读取时计算**的，不落库。这样改了档案或调了权重规则，
不需要重新抓取，刷新页面立即生效。落库的 score 只表示信息的客观重要度。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sources as SRC
from classify import CATEGORIES

PROFILE_VERSION = 1

COLLEGE_CHOICES = [c for c in SRC.SOURCES if c["kind"] == "学院"]
DEPT_CHOICES = [c for c in SRC.SOURCES if c["kind"] == "部门"]


@dataclass
class Profile:
    version: int = PROFILE_VERSION
    nickname: str = ""
    college: str = ""          # SOURCES 里的 key，如 "wy"
    college_name: str = ""
    grade: str = ""            # 入学年份，如 "2024"
    grad_year: str = ""        # 毕业年份，由 grade + 学制推出
    study_years: int = 4       # 学制：4 年制本科 / 2 年制专升本
    identity: str = "本科生"    # 本科生 | 专升本 | 教师/辅导员 | 校友
    major: str = ""
    # 关注方向不再询问用户，默认全选；只有显式标记过 narrowed 的档案才施加偏好加成
    prefs: list = field(default_factory=lambda: [c["id"] for c in CATEGORIES if c["id"] != "other"])
    prefs_narrowed: bool = False
    funded: bool = False       # 是否资助对象（影响资助类权重）
    onboarded: bool = False

    def is_graduating_soon(self, today_year: int) -> bool:
        """毕业班：当年就是他的毕业年，或已进入毕业学年。"""
        return bool(self.grad_year) and abs(int(self.grad_year) - today_year) <= 1


def load(path: Path) -> Profile:
    if not path.exists():
        return Profile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in raw.items() if k in Profile.__dataclass_fields__}
        p = Profile(**known)
        # 老档案里学院 key 与名字可能不一致（清空 key 但留着名字），按 key 重新推导
        p.college_name = next((c["name"] for c in SRC.SOURCES if c["key"] == p.college), "")
        return p
    except (json.JSONDecodeError, TypeError, ValueError):
        return Profile()


def save(path: Path, p: Profile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(p), ensure_ascii=False, indent=2), encoding="utf-8")


def as_dict(p: Profile) -> dict:
    return asdict(p)


def derive_grad_year(p: Profile) -> Profile:
    if p.grade and p.grade.isdigit():
        p.grad_year = str(int(p.grade) + int(p.study_years or 4))
    else:
        p.grad_year = ""
    return p


# ---------------- 个人相关度 ----------------

def personalize(item: dict, p: Profile, today_year: int) -> tuple[float, list[str]]:
    """在全局 score 之上乘出个人相关度，并给出可解释的理由。"""
    mul, why = 1.0, []
    cat = item.get("category", "")
    src = item.get("source", "")
    aud = item.get("audience", "") or ""

    # 1. 学院归属：自己学院的优先，别院的专属信息降权
    if p.college and src:
        if src == p.college:
            mul *= 1.35
            why.append("你所在学院")
        elif src in {c["key"] for c in COLLEGE_CHOICES}:
            mul *= 0.72
            why.append("其他学院")

    # 2. 成长路径偏好：默认不启用，只有档案显式标记 narrowed 才生效
    if p.prefs_narrowed and cat in p.prefs:
        rank = p.prefs.index(cat)
        bonus = (1.50, 1.32, 1.18, 1.10, 1.05)[min(rank, 4)]
        mul *= bonus
        why.append("你的关注路径")

    # 3. 受众匹配：年级/届别/身份对不上就明显降权
    if p.grade and f"{p.grade}级" in aud:
        mul *= 1.45
        why.append(f"面向{p.grade}级")
    if p.grad_year and f"{p.grad_year}届" in aud:
        mul *= 1.45
        why.append(f"面向{p.grad_year}届毕业班")
    if "毕业生" in aud and p.is_graduating_soon(today_year):
        mul *= 1.30
        why.append("你在毕业班")
    if "新生" in aud and p.grade != str(today_year):
        mul *= 0.60
    if "教师" in aud or "教职工" in aud or "辅导员" in aud:
        mul *= 1.10 if p.identity.startswith(("教师", "辅导员")) else 0.55
        if not p.identity.startswith(("教师", "辅导员")):
            why.append("面向教职工")
    if "专升本" in aud and p.identity != "专升本":
        mul *= 0.60
    if "家庭经济困难" in aud or "资助" in aud:
        mul *= 1.25 if p.funded else 0.92
    if "全体" in aud or "各二级学院" in aud:
        mul *= 1.08

    # 4. 专业相关：正文里点名了他的专业
    if p.major and len(p.major) >= 2 and p.major in (item.get("title", "") + aud):
        mul *= 1.20
        why.append("提到你的专业")

    return round(float(item.get("score", 0)) * mul, 2), why


def ask_profile(base: Profile, out=print, inp=input) -> Profile:
    """向导：学院用编号选择（不允许手输），其余可跳过。

    非交互环境（exe 的 stdin 被重定向、被别的脚本调用）里 `input()` 会抛 EOFError
    把整个程序弄死——打包测试时真炸过一次。所以统一走 `ask()`：拿不到输入就用默认值
    继续，宁可档案粗一点，也不能让第一次打不开。
    """
    p = Profile(**{k: v for k, v in asdict(base).items()})
    warned = []

    def ask(prompt: str, default: str = "") -> str:
        try:
            val = inp(prompt)
        except Exception:
            # 不只是 EOFError：无控制台打包时 stdin 可能根本不存在，input() 会抛
            # RuntimeError/TypeError。任何一种读不到输入的失败，都退化成默认值。
            if not warned:
                warned.append(1)
                out("（当前不是可输入的终端，档案全部用默认值，稍后可在网页「设置」里改）")
            return default
        return default if val is None else val

    out("\n首次使用，先花 30 秒建立你的档案，用于个性化排序。直接回车用默认值。\n")

    p.nickname = (ask("昵称（可留空）：") or "").strip()

    out("你的学院（输入编号，不可手打）：")
    for i, c in enumerate(COLLEGE_CHOICES, 1):
        out(f"  {i:>2}. {c['name']}")
    out(f"  {len(COLLEGE_CHOICES) + 1:>2}. 暂不指定 / 不属于以上学院")
    while True:
        raw = (ask("编号：") or "").strip()
        if not raw:
            idx = len(COLLEGE_CHOICES) + 1
        elif raw.isdigit():
            idx = int(raw)
        else:
            out("  请输入数字编号。")
            continue
        if 1 <= idx <= len(COLLEGE_CHOICES) + 1:
            break
        out(f"  编号范围 1-{len(COLLEGE_CHOICES) + 1}。")
    if idx <= len(COLLEGE_CHOICES):
        p.college, p.college_name = COLLEGE_CHOICES[idx - 1]["key"], COLLEGE_CHOICES[idx - 1]["name"]
    else:
        p.college, p.college_name = "", ""

    out("你的年级（入学年份，输入编号）：")
    import datetime
    this_year = datetime.date.today().year
    grades = [str(y) for y in range(this_year - 4, this_year + 2)]
    for i, g in enumerate(grades, 1):
        out(f"  {i}. {g} 级")
    while True:
        raw = (ask("编号：") or "").strip()
        idx = grades.index(p.grade) + 1 if p.grade in grades else 5
        if raw.isdigit():
            idx = int(raw)
        elif not raw:
            pass
        else:
            out("  请输入数字编号。")
            continue
        if 1 <= idx <= len(grades):
            p.grade = grades[idx - 1]
            break
        out(f"  编号范围 1-{len(grades)}。")

    out("身份：1.本科生 2.专升本 3.教师/辅导员 4.校友")
    raw = (ask("编号［1］：", "1") or "1").strip()
    p.identity = {1: "本科生", 2: "专升本", 3: "教师/辅导员", 4: "校友"}.get(
        int(raw) if raw.isdigit() else 1, "本科生")
    p.study_years = 2 if p.identity == "专升本" else 4
    p = derive_grad_year(p)

    p.major = (ask("你的专业（可留空）：") or "").strip()

    p.funded = (ask("是否学校认定的资助对象？(y/N)：") or "").strip().lower() == "y"
    p.onboarded = True
    out(f"\n已记录：{p.college_name or '未指定学院'} · {p.grade}级 · {p.identity} · "
        f"毕业年 {p.grad_year} · 全部成长路径均纳入个性化排序")
    return p
