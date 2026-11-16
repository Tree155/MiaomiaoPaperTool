#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

把 使用说明.md 同步成 使用说明.txt（纯文本版）。

两种触发方式：
  1. 打开 文献妙妙小工具.exe 时自动检查（desktop.py 调用 sync()）
  2. 双击 源码\\同步文档.bat 手动跑

只在 md 比 txt 新时才重写，否则跳过。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MD_NAME = "使用说明.md"
TXT_NAME = "使用说明.txt"

HEADER = """文献妙妙小工具 · 使用说明（纯文本版）
==============================================================

同目录还有 使用说明.md（排版更好，用 Typora / VS Code / Obsidian 打开）。
本文件与之一致，去掉了格式，任何记事本都能读。

"""


def _dwidth(s: str) -> int:
    """显示宽度：中日韩字符按 2 格算。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _dwidth(s))


def _fix_tables(text: str) -> str:
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = []
            for ln in block:
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    continue
                rows.append(cells)
            if not rows:
                continue
            ncol = max(len(r) for r in rows)
            rows = [r + [""] * (ncol - len(r)) for r in rows]
            widths = [max(_dwidth(r[c]) for r in rows) for c in range(ncol)]
            for idx, r in enumerate(rows):
                out.append("  " + "  ".join(_pad(c, widths[j]) for j, c in enumerate(r)).rstrip())
                if idx == 0:
                    out.append("  " + "  ".join("-" * widths[j] for j in range(ncol)))
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def convert(md_text: str) -> str:
    """markdown → 纯文本"""
    md = re.sub(r"^\s*```[a-zA-Z]*\s*$", "", md_text, flags=re.M)
    md = _fix_tables(md)
    # 整行加粗当作小标题
    md = re.sub(r"^\*\*(.+?)\*\*\s*$", lambda m: "  · " + m.group(1), md, flags=re.M)
    md = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    # 一级/二级标题 →【】，更深的 → ▸
    md = re.sub(r"^(#{1,6})\s*(.+?)\s*$",
                lambda m: "\n" + ("【" + m.group(2) + "】" if len(m.group(1)) <= 2
                                  else "  ▸ " + m.group(2)),
                md, flags=re.M)
    md = re.sub(r"`([^`]+)`", r"\1", md)
    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", md)
    md = re.sub(r"^\s*-{3,}\s*$", "-" * 66, md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return HEADER + md.lstrip()


def sync(base: Path | None = None, force: bool = False) -> str:
    """
    同步一次。返回状态字符串：
      synced / up-to-date / no-md / error:xxx
    """
    try:
        if base is None:
            base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
                   else Path(__file__).resolve().parent
        md_path = base / MD_NAME
        txt_path = base / TXT_NAME

        if not md_path.is_file():
            return "no-md"
        if not force and txt_path.is_file() and \
                txt_path.stat().st_mtime >= md_path.stat().st_mtime:
            return "up-to-date"

        body = convert(md_path.read_text(encoding="utf-8"))
        txt_path.write_text(body, encoding="utf-8-sig")   # BOM，记事本不乱码
        return "synced"
    except Exception as e:
        return f"error:{type(e).__name__}"


if __name__ == "__main__":
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    r = sync(base, force="--force" in sys.argv)
    print(f"[{r}] {base / TXT_NAME}")
