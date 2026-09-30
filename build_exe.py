# -*- coding: utf-8 -*-
"""打包成独立 exe：build_exe.py

    python build_exe.py            构建 + 打 zip
    python build_exe.py --onefile  额外再打一个单文件版（体积大、误报率高，仅备用）

输出路径全部用 ASCII，避免中文目录在部分 Windows / 压缩工具 / CI 上出乱码。

关于杀毒软件误报，这里刻意做了三件事（都是实测有效的方向）：

1. **默认 --onedir，不用 --onefile。** onefile 每次启动把自身解压到 %TEMP% 再执行，
   这个行为跟很多恶意样本一模一样，是最常见的启发式误报来源。
2. **--noupx。** UPX 压缩壳本身不危险，但恶意样本大量使用它，签名库对它高度敏感。
3. **写版本资源（产品名/公司/版权/版本号）。** 没有版本信息的裸 exe 更容易被判为可疑。
   另外固定输出文件名和路径，不要每次换名字——换名字会让"这个文件以前见过"的信誉失效。

真正能一劳永逸消掉误报的只有代码签名证书（EV/OV），本机没有，所以脚本会在最后提醒
先用 VirusTotal 扫一遍再分发。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "school-radar"
DIST = os.path.join(HERE, "dist")              # 纯 ASCII
BUILD = os.path.join(HERE, "build", "pyi")     # 纯 ASCII
PRODUCT = "School Radar"
VERSION = "1.0.0"


def run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=HERE).returncode


def version_file() -> str:
    """PyInstaller 的版本资源描述文件。字段越完整，扫描引擎越少猜。"""
    path = os.path.join(BUILD, "version_info.txt")
    os.makedirs(BUILD, exist_ok=True)
    tail = ",".join(f"{n},0" for n in VERSION.split(".")) + ",0"
    text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tail}), prodvers=({tail}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
        StringStruct('CompanyName', '{PRODUCT}'),
        StringStruct('FileDescription', 'Campus notice aggregator and classifier'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', 'For learning and research only. No scraping behind login.'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{PRODUCT}'),
        StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def common_args(vf: str, name: str) -> list[str]:
    # 两个变体必须用不同的 --name：共用名字时后一个构建会直接覆盖前一个的产物，
    # 于是"无控制台版"其实是控制台版换个文件名。
    return [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--noupx",                                  # 见模块 docstring 第 2 点
        "--name", name,
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        "--version-file", vf,
        "--collect-submodules", "bs4",
        "--hidden-import", "lxml.html",
        "--hidden-import", "lxml.etree",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc_data",
        "--exclude-module", "test",
        "app.py",
    ]


def build(windowed: bool, mode: str) -> str:
    """构建一个变体，返回它的产物目录（onedir）。"""
    name = APP_NAME if windowed else APP_NAME + "-cli"
    shutil.rmtree(os.path.join(DIST, name), ignore_errors=True)
    args = common_args(version_file(), name) + [f"--{mode}"] + (["--noconsole"] if windowed else ["--console"])
    if run(args) != 0:
        raise SystemExit(f"PyInstaller 构建失败（{name}）")
    return os.path.join(DIST, name)


def locate(src_dir: str, name: str) -> str:
    for root, _dirs, files in os.walk(src_dir):
        for f in files:
            if f == name + ".exe":
                return os.path.join(root, f)
    raise SystemExit(f"构建产物里找不到 {name}.exe")


def package(windowed_src: str, console_src: str) -> str:
    out = os.path.join(DIST, f"{APP_NAME}-win64")
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(windowed_src, out)
    # 命令行版（有控制台，能看报错、能跑 --doctor）并进同一个目录，命名区分
    con = locate(console_src, APP_NAME + "-cli")
    shutil.copy2(con, os.path.join(out, f"{APP_NAME}-cli.exe"))
    shutil.copy2(os.path.join(HERE, "README.md"), os.path.join(out, "README.md"))
    # GPL-3.0 的硬性要求：分发可执行文件必须同时附带许可证全文。少了 LICENSE，
    # 这个包本身就是违规产物——比任何功能缺陷都严重，所以这里不做存在性判断，缺了就报错。
    shutil.copy2(os.path.join(HERE, "LICENSE"), os.path.join(out, "LICENSE"))
    guide = os.path.join(HERE, ".github", "prompt.md")
    if os.path.exists(guide):
        shutil.copy2(guide, os.path.join(out, "prompt.md"))
    zip_base = os.path.join(DIST, f"{APP_NAME}-win64")
    zip_path = zip_base + ".zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(out):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.join(APP_NAME, os.path.relpath(full, out)))
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true", help="额外打一个单文件版（不推荐分发）")
    args = ap.parse_args()

    print("① 构建前自检：分类回归 + 前端语法")
    if run([sys.executable, "cases.py"]) != 0:
        print("cases.py 未通过，停止构建。先修规则再打包。")
        return 1

    print("\n② 构建 windowed 版（无控制台，双击即用）")
    win = build(True, "onedir")
    print("\n③ 构建 console 版（排错 / --doctor 用）")
    con = build(False, "onedir")

    print("\n④ 组装分发目录与 zip")
    zip_path = package(win, con)
    size = os.path.getsize(zip_path) / 1e6
    print(f"\n完成：{zip_path}  ({size:.1f} MB)")
    print("发出去之前：")
    print("  1) 上传 https://www.virustotal.com 扫一遍，确认没有引擎报启发式恶意；")
    print("  2) 提醒接收者：Windows SmartScreen 会拦未签名 exe，点「更多信息 → 仍要运行」；")
    print("     报毒时先看是不是在 data\\ 目录里——那是它自己写的库文件。")
    print("  3) 想要真正零提示，只有买代码签名证书（OV/EV）这条路。")

    if args.onefile:
        print("\n⑤ 额外构建单文件版（体积大、误报率高，仅自用）")
        one = build(True, "onefile")
        print("单文件版：", locate(one, APP_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
