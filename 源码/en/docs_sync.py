#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2

Author: Ni, Yishu & Da Fei Yu (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For learning and exchange only; commercial use prohibited.

Sync User-Guide.md into User-Guide.txt (plain-text version).

Two ways to trigger it:
  1. Checked automatically when Miaomiao Paper Tool.exe starts (desktop.py calls sync())
  2. Run src\\sync-docs.bat manually

Rewrite only when the md is newer than the txt; otherwise skip.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MD_NAME = "User-Guide.md"
TXT_NAME = "User-Guide.txt"

HEADER = """Miaomiao Paper Tool · User Guide (plain-text version)
==============================================================

User-Guide.md in the same folder is formatted better (open it with Typora / VS Code / Obsidian).
This file matches it without the formatting, so any text editor can read it.

"""


def _dwidth(s: str) -> int:
    """Display width: CJK characters count as 2 cells."""
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
    """markdown → plain text"""
    md = re.sub(r"^\s*```[a-zA-Z]*\s*$", "", md_text, flags=re.M)
    md = _fix_tables(md)
    # A whole bold line acts as a subheading
    md = re.sub(r"^\*\*(.+?)\*\*\s*$", lambda m: "  · " + m.group(1), md, flags=re.M)
    md = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    # Level 1/2 headings -> [], deeper -> >
    md = re.sub(r"^(#{1,6})\s*(.+?)\s*$",
                lambda m: "\n" + ("[" + m.group(2) + "]" if len(m.group(1)) <= 2
                                  else "  ▸ " + m.group(2)),
                md, flags=re.M)
    md = re.sub(r"`([^`]+)`", r"\1", md)
    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", md)
    md = re.sub(r"^\s*-{3,}\s*$", "-" * 66, md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return HEADER + md.lstrip()


def sync(base: Path | None = None, force: bool = False) -> str:
    """
    Sync once. Return a status string:
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
        txt_path.write_text(body, encoding="utf-8-sig")   # BOM so Notepad doesn't garble it
        return "synced"
    except Exception as e:
        return f"error:{type(e).__name__}"


if __name__ == "__main__":
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    r = sync(base, force="--force" in sys.argv)
    print(f"[{r}] {base / TXT_NAME}")
