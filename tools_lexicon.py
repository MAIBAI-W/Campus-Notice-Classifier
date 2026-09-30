# -*- coding: utf-8 -*-
"""把 classify.py 里的词表渲染成 markdown，写进 .github/prompt.md。

词表是这份文档里唯一会随代码变化的部分。手写它一定会过期，所以做成脚本：

    python tools_lexicon.py            重新生成 prompt.md 里的词表区块
    python tools_lexicon.py --check    只检查区块是否与代码一致（不同则退出码 1）

它替换 `<!-- LEXICON:BEGIN -->` 与 `<!-- LEXICON:END -->` 之间的内容，其余不动。
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import classify  # noqa: E402

BEGIN = "<!-- LEXICON:BEGIN -->"
END = "<!-- LEXICON:END -->"
DOC = os.path.join(".github", "prompt.md")


def render() -> str:
    out = [BEGIN, "", "## 4. 类目与拦截词表（自动生成）", "",
           f"12 个类目、{len(classify.CONCEPTS)} 个概念、"
           f"{sum(len(c['words']) for c in classify.CONCEPTS.values())} 条表述。",
           "`scope=title` 表示只有标题命中才算；`aux` 表示文体词，只加分不定类。", ""]

    out.append("### 4.1 跨类拦截（排除词 `ex`，只看标题）")
    out.append("")
    out.append("标题出现该词，这个类目整类出局——这是处理「两个类目抢同一条通知」的唯一手段。")
    out.append("")
    for cat in classify.CATEGORIES:
        if cat["ex"]:
            out.append(f"- **{cat['name']}**（`{cat['id']}`, base {cat['base']}）："
                       + "、".join(f"`{w}`" for w in cat["ex"]))
    out.append("")

    out.append("### 4.2 概念词组（按类目）")
    out.append("")
    for cat in classify.CATEGORIES:
        cons = [(cid, c) for cid, c in classify.CONCEPTS.items() if c["cat"] == cat["id"]]
        if not cons:
            continue
        out.append(f"#### {cat['name']} · `{cat['id']}` (base {cat['base']})")
        out.append("")
        for cid, con in cons:
            flags = []
            if con["scope"] == "title":
                flags.append("scope=title")
            if con.get("aux"):
                flags.append("aux 文体词")
            tag = f"  *({', '.join(flags)})*" if flags else ""
            out.append(f"- **{con['name']}** `{cid}` w={con['w']}{tag}")
            out.append("  " + "、".join(f"`{w}`" for w in con["words"]))
        out.append("")

    out.append("### 4.3 文体词一览（不定类，只加分）")
    out.append("")
    out.append("这些词命中后，该类目只有在**全场都没有主题证据**时才有资格定类：")
    out.append("")
    out.append("、".join(f"`{w}`" for w in sorted(classify.AUX_WORDS)))
    out.append("")
    out.append("### 4.4 文档类型拦截因子")
    out.append("")
    out.append("| 类型 | 判定词 | 因子 |")
    out.append("|---|---|---|")
    factors = {"新闻": "0.42", "制度文件": "0.85", "其他": "0.9"}
    for name, words in classify.DOC_TYPES:
        out.append(f"| {name} | {'、'.join(words)} | {factors.get(name, '1.0')} |")
    out.append("")
    out.append("### 4.5 受众识别词表")
    out.append("")
    for name, words in classify.AUDIENCE_RULES:
        out.append(f"- **{name}**：" + "、".join(f"`{w}`" for w in words))
    out.append(END)
    return "\n".join(out)


def _replace_block(text: str, body: str) -> str:
    i, j = text.find(BEGIN), text.find(END)
    if i == -1 or j == -1 or j < i:
        raise SystemExit(f"{DOC} 里找不到 {BEGIN} … {END} 标记，先手工加上一对")
    return text[:i] + body + text[j + len(END):]


def main() -> int:
    path = DOC
    if not os.path.exists(path):
        print(f"找不到 {path}")
        return 1
    text = open(path, encoding="utf-8").read()
    body = render()
    if "--check" in sys.argv:
        current = text[text.find(BEGIN):text.find(END) + len(END)]
        if current.strip() != body.strip():
            print("词表区块已过期，请跑：python tools_lexicon.py")
            return 1
        print("词表区块与代码一致")
        return 0
    open(path, "w", encoding="utf-8").write(_replace_block(text, body))
    print(f"已把 {len(body)} 字符的词表写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
