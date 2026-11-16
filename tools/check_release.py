#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_release.py -- 发布前三方核对。

    python tools/check_release.py                    # 只查仓库内部（版本号 / AST / 残留 / 文档引用）
    python tools/check_release.py --packs ../_packs   # 再加发布包核对
    python tools/check_release.py --packs ../_packs \
        --build-cn build/MiaomiaoTool.exe --build-en build-en/MiaomiaoTool.exe \
        --home D:/MiaomiaoTool                        # 再加构建产物与本地运行目录

退出码 0 = 全 PASS，1 = 有失败项。

查什么
------
1. **版本号一致**：从 `源码/ui.html` 取基准版本，核对另一个版本、README、四份 PDF；
   再从 exe 里提取内嵌的 ui.html 取版本 —— 这是发现「exe 是旧源码打的」最省事的办法
   （比反编译 pyc 简单，PyInstaller 的 ui.html 是原样存的）。
2. **中英代码骨架一致**：AST 节点类型序列，忽略所有字面量，所以 f-string 片段数量
   不同（译文合并/拆分的正常结果）不算差异。`paperfetch_ui.py` 有意删减翻译接口，
   单独白名单。
3. **英文版无中文残留**：除「数据词」白名单外不该有汉字。
4. **README 引用的图片都存在**。
5. **发布包**：exe / PDF 的 md5 与仓库对应文件一致，config 指向的文档在包内，
   包内 exe 的内嵌前端与仓库源码一致。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_CN = REPO / "源码"
SRC_EN = SRC_CN / "en"

# 英文版有意比中文版少的代码（移除了翻译接口），不参与骨架比对
AST_ALLOW_DIFF = {"paperfetch_ui.py"}
# 英文版里允许保留的汉字：这些是「数据词」，要与外部数据比对
CJK_ALLOW = {"区", "否", "无", "次数", "限制"}

fails: list[str] = []
skips: list[str] = []


def p(name: str, ok: bool, detail: str = "") -> bool:
    print("  %-48s %s %s" % (name, "PASS" if ok else "★FAIL", detail))
    if not ok:
        fails.append(name)
    return ok


def skip(name: str, why: str):
    print("  %-48s SKIP %s" % (name, why))
    skips.append(name)


def md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def exe_ui_html(exe: Path) -> bytes | None:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError:
        return None
    try:
        r = CArchiveReader(str(exe))
        for n in r.toc:
            if n.lower() == "ui.html":
                return r.extract(n)
    except Exception:
        return None
    return None


def version_in_html(b: bytes) -> str:
    t = b.decode("utf-8", "replace")
    m = re.search(r">v(\d+\.\d+\.\d+)<", t) or re.search(r"v(\d+\.\d+\.\d+)", t)
    return m.group(1) if m else "?"


def code_sig(p: Path) -> list[str]:
    tree = ast.parse(io.open(p, encoding="utf-8", newline="").read())
    return [type(n).__name__ for n in ast.walk(tree) if not isinstance(n, ast.Constant)]


def pdf_text(p: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        return "".join((x.extract_text() or "") for x in PdfReader(str(p)).pages)
    except Exception:
        return ""


# --------------------------------------------------------------------------- 1 版本号
def check_version() -> str:
    print("\n一、版本号")
    ui_cn = SRC_CN / "ui.html"
    if not ui_cn.is_file():
        p("源码/ui.html 存在", False)
        return "?"
    base = version_in_html(ui_cn.read_bytes())
    p("基准版本（源码/ui.html）", base != "?", "v" + base)

    targets = [
        ("源码/en/ui.html", SRC_EN / "ui.html"),
        ("README.md", REPO / "README.md"),
    ]
    for name, f in targets:
        if not f.is_file():
            p(name, False, "缺失")
            continue
        t = io.open(f, encoding="utf-8", errors="replace").read()
        has_new = ("v" + base) in t or ("-" + base) in t
        old = re.findall(r"v?(\d+\.\d+\.\d+)", t)
        stale = sorted({v for v in old if v != base})
        p(name, has_new and not stale, ("仍含旧版本 " + ",".join(stale)) if stale else "v" + base)

    # 四份 PDF
    for name, f in (("使用说明.pdf", REPO / "使用说明.pdf"),
                    ("docs/源码说明.pdf", REPO / "docs" / "源码说明.pdf"),
                    ("User-Guide-EN.pdf", REPO / "User-Guide-EN.pdf"),
                    ("docs/Source-NOTES-EN.pdf", REPO / "docs" / "Source-NOTES-EN.pdf")):
        if not f.is_file():
            p(name, False, "缺失")
            continue
        t = pdf_text(f)
        if not t:
            skip(name, "读不出文本（缺 pypdf？）")
            continue
        # 只认「同 major.minor 的补丁号」，并加边界避免把 127.0.0.1 这类串当成版本
        old = sorted({v for v in re.findall(r"(?<![\d.])v?(\d+\.\d+\.\d+)(?![\d.])", t)
                      if v != base and v.rsplit(".", 1)[0] == base.rsplit(".", 1)[0]})
        p(name, not old, ("仍含旧版本 " + ",".join(old)) if old else "v" + base)

    # 源码里其他地方残留旧版本号
    stale_files = []
    for f in list(SRC_CN.glob("*.py")) + [SRC_CN / "ui.html", SRC_CN / "README.txt"]:
        if not f.is_file():
            continue
        t = io.open(f, encoding="utf-8", errors="replace").read()
        bad = {v for v in re.findall(r"\b(\d+\.\d+\.\d+)\b", t) if v != base and v.startswith(base[:2])}
        if bad:
            stale_files.append("%s(%s)" % (f.name, ",".join(sorted(bad))))
    p("中文源码无旧版本号残留", not stale_files, ", ".join(stale_files[:4]))
    return base


# --------------------------------------------------------------------------- 2 中英骨架
def check_ast():
    print("\n二、中英代码骨架（AST 节点类型，忽略字面量）")
    if not SRC_EN.is_dir():
        p("源码/en/ 存在", False)
        return
    bad = []
    for f in sorted(SRC_CN.glob("*.py")):
        if f.name == "pack_release.py":          # 只有中文版有
            continue
        e = SRC_EN / f.name
        if not e.is_file():
            p(f.name, False, "英文版缺失")
            bad.append(f.name)
            continue
        try:
            a, b = code_sig(f), code_sig(e)
        except SyntaxError as ex:
            p(f.name, False, "语法错误 %s" % ex)
            bad.append(f.name)
            continue
        if a == b:
            print("    %-20s 一致" % f.name)
        elif f.name in AST_ALLOW_DIFF:
            print("    %-20s 有意删减（%d vs %d 节点）" % (f.name, len(a), len(b)))
        else:
            print("    %-20s ★ 结构不同（%d vs %d）" % (f.name, len(a), len(b)))
            bad.append(f.name)
    p("中英骨架", not bad, ", ".join(bad) if bad else "")


# --------------------------------------------------------------------------- 3 中文残留
def protected_lines(p: Path) -> set[int]:
    """PROTECT_VARS 里那些「数据表」相关的语句行 —— 它们本来就该保留中文。

    不只认 `X = {...}`，也要认 `X.update({...})` / `X.extend(...)` 这类后续追加，
    否则像 ZH_TERMS 的二段词表会被误报。
    """
    NAMES = {"ZH_TERMS", "CJK_STOP", "_F_CAS", "_F_CAS_BASE", "_F_TOP", "FIELD_ALIAS"}
    try:
        tree = ast.parse(io.open(p, encoding="utf-8", newline="").read())
    except SyntaxError:
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        # 只看语句级别，不进函数/类内部（否则整个函数体都会被放过）
        if not isinstance(node, ast.stmt) or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if used & NAMES:
            out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def check_residual():
    print("\n三、英文版中文残留")
    if not SRC_EN.is_dir():
        skip("英文版中文残留", "没有 源码/en/")
        return
    bad = []
    for f in sorted(list(SRC_EN.glob("*.py")) + [SRC_EN / "ui.html"]):
        if not f.is_file():
            continue
        t = io.open(f, encoding="utf-8", errors="replace").read()
        prot = protected_lines(f) if f.suffix == ".py" else set()
        hits = []
        for i, line in enumerate(t.splitlines(), 1):
            if i in prot:
                continue
            for m in re.finditer(r"[\u4e00-\u9fff]+", line):
                if m.group(0) not in CJK_ALLOW:
                    hits.append((i, m.group(0)[:6]))
        if hits:
            bad.append("%s(%d)" % (f.name, len(hits)))
            print("    %-20s L%s %s" % (f.name, hits[0][0], [h[1] for h in hits[:4]]))
    p("英文版无多余中文（数据表除外）", not bad, ", ".join(bad))


# --------------------------------------------------------------------------- 4 文档引用
def check_docs():
    print("\n四、README 与文档引用")
    rd = REPO / "README.md"
    if not rd.is_file():
        p("README.md 存在", False)
        return
    t = io.open(rd, encoding="utf-8", errors="replace").read()
    imgs = sorted(set(re.findall(r"\]\((docs/screenshots/[^)]+)\)", t)))
    miss = [i for i in imgs if not (REPO / i).is_file()]
    p("README 引用的截图都存在", not miss, "共 %d 张，缺 %s" % (len(imgs), miss or "无"))

    pdfs = sorted(set(re.findall(r"\]\(([^)]+\.pdf)\)", t)))
    miss2 = [i for i in pdfs if not (REPO / i).is_file()]
    p("README 引用的文档都存在", not miss2, "共 %d 份，缺 %s" % (len(pdfs), miss2 or "无"))

    d = REPO / "tools" / "i18n" / "dict.json"
    if d.is_file():
        try:
            data = json.load(io.open(d, encoding="utf-8"))
            total = sum(len(v) for v in data.values())
            p("dict.json 可解析", True, "分区 %d、条目 %d" % (len(data), total))
        except Exception as ex:
            p("dict.json 可解析", False, str(ex)[:60])


# --------------------------------------------------------------------------- 5 发布包
def check_packs(packs: Path, build_cn: Path | None, build_en: Path | None):
    print("\n五、发布包")
    base = version_in_html((SRC_CN / "ui.html").read_bytes())
    cands = sorted(packs.glob("*.zip"))
    if not cands:
        skip("发布包", "%s 下没有 zip" % packs)
        return
    # 挑版本号匹配的包（中/英各一个）
    picked = [z for z in cands if base in z.name]
    if not picked:
        p("找到与当前版本对应的包", False, "该目录下没有含 %s 的 zip" % base)
        return
    for z in picked:
        is_en = z.name.lower().startswith("miaomiao")
        tag = "英文" if is_en else "中文"
        print("  [%s] %s" % (tag, z.name))
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            exe_name = "MiaomiaoTool.exe" if is_en else "文献妙妙小工具.exe"
            if exe_name not in names:
                p("%s 包含 %s" % (tag, exe_name), False, str(names[:4]))
                continue
            payload = zf.read(exe_name)
            hh = exe_ui_html_bytes(payload)
            src_html = (SRC_EN if is_en else SRC_CN) / "ui.html"
            if hh is None:
                skip("%s exe 内嵌前端" % tag, "PyInstaller 未安装")
            else:
                p("%s exe 内嵌前端 == 仓库源码" % tag, md5_bytes(hh) == md5_file(src_html))
                p("%s exe 版本" % tag, version_in_html(hh) == base, "v" + version_in_html(hh))

            exe_md5 = md5_bytes(payload)
            bp = build_en if is_en else build_cn
            if bp and bp.is_file():
                p("%s exe == 构建产物" % tag, exe_md5 == md5_file(bp), exe_md5[:12])
            else:
                skip("%s exe == 构建产物" % tag, "未提供构建路径")

            for pname in (["User-Guide-EN.pdf", "docs/Source-NOTES-EN.pdf"] if is_en
                          else ["使用说明.pdf", "docs/源码说明.pdf"]):
                if pname not in names:
                    p("%s 包含 %s" % (tag, pname), False)
                    continue
                disk = REPO / pname.replace("/", os.sep)
                p("%s %s == 仓库文件" % (tag, os.path.basename(pname)),
                  disk.is_file() and md5_bytes(zf.read(pname)) == md5_file(disk))

            cfg = json.loads(zf.read("config.json").decode("utf-8"))
            note = cfg.get("_说明") or cfg.get("_notes") or ""
            m = re.search(r"[「\[]([^」\]]+\.(?:pdf|md))[」\]]", note) or re.search(r"([\w.-]+\.pdf)", note)
            target = m.group(1) if m else ""
            p("%s config 指向的文档在包内" % tag, bool(target) and target in names, target or "解析失败")


def exe_ui_html_bytes(payload: bytes) -> bytes | None:
    """从内存里的 exe 字节提取 ui.html（先落临时文件）。"""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".exe")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        return exe_ui_html(Path(tmp))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- 6 本地目录
def check_home(home: Path):
    print("\n六、本地运行目录")
    if not home.is_dir():
        p("本地目录存在", False, str(home))
        return
    cfg_p = home / "config.json"
    if not cfg_p.is_file():
        p("config.json 存在", False)
        return
    cfg = json.load(io.open(cfg_p, encoding="utf-8"))
    note = cfg.get("_说明") or cfg.get("_notes") or ""
    m = re.search(r"[「\[]([^」\]]+\.(?:pdf|md))[」\]]", note) or re.search(r"([\w.-]+\.pdf)", note)
    target = m.group(1) if m else ""
    p("config 指向的文档存在", bool(target) and (home / target).is_file(), target or "解析失败")
    keys = [k for k, v in cfg.items() if v and not k.startswith("_")]
    p("config 保留了用户配置", len(keys) >= 3, ", ".join(keys[:5]))

    exe = home / "文献妙妙小工具.exe"
    if exe.is_file():
        hh = exe_ui_html(exe)
        if hh is None:
            skip("本地 exe 内嵌前端", "PyInstaller 未安装")
        else:
            base = version_in_html((SRC_CN / "ui.html").read_bytes())
            p("本地 exe 版本", version_in_html(hh) == base, "v" + version_in_html(hh))
    else:
        skip("本地 exe", "目录里没有 文献妙妙小工具.exe")


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前三方核对")
    ap.add_argument("--packs", help="发布包目录（含 *.zip）")
    ap.add_argument("--build-cn", help="中文版构建产物 exe 路径")
    ap.add_argument("--build-en", help="英文版构建产物 exe 路径")
    ap.add_argument("--home", help="本地运行目录（含 config.json 与 exe）")
    a = ap.parse_args()

    print("仓库：%s" % REPO)
    check_version()
    check_ast()
    check_residual()
    check_docs()

    if a.packs:
        check_packs(Path(a.packs),
                    Path(a.build_cn) if a.build_cn else None,
                    Path(a.build_en) if a.build_en else None)
    else:
        print("\n五、发布包")
        skip("发布包核对", "未提供 --packs")

    if a.home:
        check_home(Path(a.home))
    else:
        print("\n六、本地运行目录")
        skip("本地目录核对", "未提供 --home")

    print("\n" + "=" * 68)
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  ★", f)
    else:
        print("全部 PASS")
    if skips:
        print("跳过 %d 项：%s" % (len(skips), ", ".join(skips)))
    print("=" * 68)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
