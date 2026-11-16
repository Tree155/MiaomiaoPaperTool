# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2 —— 整理手动下载的 PDF（organize）

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

为什么需要这个：
  订阅刊的全文下不了，只能在浏览器里点出版社页面手动下。下完一堆
  `1-s2.0-S0308814619308454-main.pdf`、`jafc.7b00636.pdf` 躺在「下载」文件夹里，
  要一篇篇改名、拖进文献库、回清单打勾 —— 这一步最烦，也最容易忘。

  这个模块把「改名 + 归位」自动化：
    1. 扫描一个目录（默认系统的「下载」文件夹）里的 .pdf
    2. 从 PDF 里读出 DOI（读不出来就读标题）
    3. 拿 DOI 去「下载台账 / 当前检索结果 / 文献库已有记录」里找对应文献
    4. 找到就按统一规则改名（2024_Zhang_Aroma_of_shrimp_head.pdf），移到文献库
    5. 认不出来的**原地不动**，只在报告里列出来

安全边界（宁可不动，也不乱动）：
  · 只处理能确认是哪一篇的文件；认不出的一律不碰
  · 目标重名时不覆盖，加 _2 后缀
  · 只移动、不删除；失败的会告诉你卡在哪
  · 支持 dry_run：先只报告打算怎么改，真动手由用户点确认
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

# PDF 正文里的 DOI。字符集要照 DOI 规范来 —— 少一个字符就会把 DOI 截断成
# 「查不到的另一个 DOI」（Wiley 老 SICI 号里的 # % 最典型）。
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:<>%#&+=~a-zA-Z0-9]+", re.I)
# DOI 末尾常见的粘连字符（排版残留）
_TRAIL = ".,;:)]}>\u3002\uff0c\uff1b"


def _clean_doi(s: str) -> str:
    d = (s or "").strip()
    while d and d[-1] in _TRAIL:
        d = d[:-1]
    return d.lower()


def pdf_text(path: Path, max_pages: int = 3) -> str:
    """读 PDF 前几页的文本。扫描件/加密文件返回空串（调用方按「认不出」处理）。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        rd = PdfReader(str(path), strict=False)
        if getattr(rd, "is_encrypted", False):
            try:
                rd.decrypt("")            # 空密码加密的（常见于出版社 PDF）
            except Exception:
                return ""
        out = []
        for pg in rd.pages[:max_pages]:
            try:
                out.append(pg.extract_text() or "")
            except Exception:
                continue
        return "\n".join(out)
    except Exception:
        return ""


def pdf_title(path: Path) -> str:
    """
    PDF 自带的标题（出版社的 PDF 大多有）。取不到返回空串。

    ⚠ 当前 `scan()` **不调用它**：为了让"认不出来一个都不碰"这条承诺站得住，
    「有 DOI 但台账里查不到」的 PDF 一律不动（等用户检索一次再整理）。
    拿元数据标题兜底命名看着聪明，但遇到勘误、合集页就会搬错，
    而搬错的代价是「下载那边按文件名判已有」→ 那篇真论文以后再也下不到。
    留着这个函数是给以后做「按标题认文件」用的。
    """
    try:
        from pypdf import PdfReader
        rd = PdfReader(str(path), strict=False)
        md = getattr(rd, "metadata", None) or {}
        t = md.get("/Title") or md.get("title") or ""
        return str(t).strip()[:200]
    except Exception:
        return ""


def doi_candidates(path: Path) -> list[str]:
    """
    从 PDF 正文里找 DOI，返回**候选列表**（可能两个）。

    窄栏排版会把 DOI 折断成两行（"10.1111/ijfs." + "15747"），但反过来，
    完整的 DOI 后面跟一个句号收尾时（"10.5281/zenodo.1234567."），
    下一行也常常是个短词 —— 无脑拼会拼出 "…1234567.All" 这种假 DOI。
    所以两种都当候选返回，调用方拿去逐个查台账，谁查得到用谁。
    """
    txt = pdf_text(path, 2)
    if not txt:
        return []
    lines = txt.splitlines()
    out: list[str] = []
    for i, ln in enumerate(lines):
        m = _DOI_RE.search(ln)
        if not m:
            continue
        cand = m.group(0).rstrip()
        d0 = _clean_doi(cand)
        if d0 and d0 not in out:
            out.append(d0)
        if i + 1 < len(lines) and cand.endswith((".", "-", "/", "_")):
            nxt = lines[i + 1].strip()
            if nxt and " " not in nxt and len(nxt) <= 20:
                joined = _clean_doi(cand + nxt)
                if joined and joined not in out:
                    out.append(joined)
        if len(out) >= 5:                # 一页里 5 个 DOI，肯定是合集/推荐页，够了
            break
    return out


def doi_from_pdf(path: Path) -> str:
    """兼容旧调用：只要第一个候选。"""
    c = doi_candidates(path)
    return c[0] if c else ""


def _norm_title(s: str) -> str:
    """标题归一化：只留字母数字，用来做模糊比对。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())


def load_known(cfg: dict, extra: list[dict] | None = None) -> dict[str, dict]:
    """
    已知文献的索引，两个键都指向同一条记录：
      · "doi:<小写 doi>"
      · "t:<归一化标题前 60 字>"
    来源：下载台账 `_manifest.csv`、当前检索结果（extra）、文献库里的同名清单。
    """
    out: dict[str, dict] = {}

    def put(rec: dict) -> None:
        d = (rec.get("doi") or "").strip().lower()
        t = (rec.get("title") or "").strip()
        if d:
            out.setdefault("doi:" + d, rec)
        if t:
            # 不要截断：截到 60 字后只能验证前缀，Part 1/Part 2 这种尾缀不同的系列论文会互认
            out.setdefault("t:" + _norm_title(t), rec)

    for rec in (extra or []):
        if isinstance(rec, dict):
            put(rec)

    # 台账：程序目录 + 文献库，两处都可能有
    cands = []
    try:
        import paperfetch as pf
        cands.append(pf.app_dir() / "_manifest.csv")
        cands.append(pf.library_dir(cfg) / "_manifest.csv")
    except Exception:
        pass
    for p in cands:
        try:
            if not p.is_file():
                continue
            with p.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    put({"doi": row.get("doi", ""), "title": row.get("title", ""),
                         "year": row.get("year", ""), "first_author": row.get("first_author", ""),
                         "journal": row.get("journal", "")})
        except Exception:
            continue
    return out


def _pick_name(rec: dict, fallback_doi: str = "") -> str:
    """按统一规则生成文件名（不含扩展名）。"""
    try:
        import paperfetch as pf
        r = dict(rec)
        if not r.get("title") and fallback_doi:
            r["title"] = fallback_doi
        return pf.target_name(r, "")
    except Exception:
        return ""


def _safe_move(src: Path, dst: Path) -> str:
    """
    把 src 搬到 dst：**先完整复制到临时名 → 校验大小 → 改名成目标 → 最后删源**。
    返回空串表示成功，否则返回错误说明。

    为什么不直接 shutil.move：跨盘时它是 copy2 + unlink，复制到一半失败（磁盘满、
    U 盘拔了）会**在文献库留下半截 PDF**，而下载那边判「已有全文」只看文件大小 > 5KB，
    半截文件照样过 —— 那篇文献以后就再也下不到真全文了。所以自己来，失败要清干净。
    """
    tmp = dst.with_name(dst.name + ".part")
    try:
        shutil.copy2(str(src), str(tmp))
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError("复制后大小不一致")
        tmp.replace(dst)                 # 同一分区内 rename，原子
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)  # 把半截文件清掉，别留在库里
        except OSError:
            pass
        return f"{type(e).__name__}: {e}"
    try:
        src.unlink()
    except OSError:
        # 文件正被 Acrobat / 预览窗格占着。副本已经好了，但**必须如实说** ——
        # 否则用户以为搬完了，下载夹里那份会一直留着，下一轮又显示「已有，跳过」。
        return ("WARN:原件删不掉，可能正被打开，你顺手删一下")
    return ""


def _match_by_title(pdf_path: Path, known: dict) -> dict | None:
    """
    用标题认文件。**只认高置信度的**：整条标题（≥20 字符）原样出现在 PDF 正文里。

    以前是裸子串匹配（`记录标题 in 正文`），台账里只要有一条叫 "Erratum" / "Editorial"
    的短标题，任何正文里出现这个词的 PDF 都会被认成它 —— 文件被改成别人的名字搬走，
    而且下载那边按文件名判「已有」，等于把真论文永久挡住了。宁可不认，也不能认错。
    """
    txt = pdf_text(pdf_path, 2)
    if not txt:
        return None
    flat = _norm_title(txt[:4000])
    if len(flat) < 20:
        return None
    best, best_len = None, 0
    for k, v in known.items():
        if not k.startswith("t:"):
            continue
        t = k[2:]
        if len(t) < 20:                  # 太短的标题一律不参与匹配
            continue
        if t in flat and len(t) > best_len:
            best, best_len = v, len(t)
    return best


def scan(folder: Path, cfg: dict, extra: list[dict] | None = None,
         dry_run: bool = True, limit: int = 300) -> dict:
    """
    扫描 folder 里的 PDF，能**确认**是哪一篇的才改名并移进文献库。
    dry_run=True 时只报告（plan 里列出打算怎么改），一个文件都不动。

    什么算「能确认」：PDF 里读到 DOI，且这个 DOI 在下载台账 / 当前检索结果里；
    或者整个标题在正文里原样出现。其余一律不动 —— 认错的代价比不认大得多。
    """
    import paperfetch as pf

    folder = Path(folder).expanduser()
    lib = pf.library_dir(cfg)
    known = load_known(cfg, extra)

    stats = {"scanned": 0, "matched": 0, "moved": 0, "skipped": 0,
             "unknown": 0, "unmatched": 0, "failed": 0, "dry_run": bool(dry_run)}
    plan: list[dict] = []
    unknown: list[str] = []
    unmatched: list[str] = []

    if not folder.is_dir():
        return {**stats, "error": f"目录不存在：{folder}"}

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    try:
        allpdfs = [p for p in folder.glob("*.pdf") if p.is_file()]
        total = len(allpdfs)
        pdfs = sorted(allpdfs, key=_mtime, reverse=True)[:limit]
    except OSError as e:
        return {**stats, "error": f"读不了这个目录：{e}"}

    for src in pdfs:
        stats["scanned"] += 1
        try:
            size = src.stat().st_size
            cands = doi_candidates(src)
        except OSError:
            stats["failed"] += 1
            continue
        doi = cands[0] if cands else ""

        # 在候选里找有没有台账认识的。**命中超过 1 个就不敢认** ——
        # 说明这份 PDF 里出现了好几篇在库文献的 DOI（合集页、出版社推荐页），
        # 拿不准哪个才是本文的；认错的代价是「搬错 + 以后真论文被判已有」。
        hits = [(c, known.get("doi:" + c)) for c in cands]
        hits = [(c, r) for c, r in hits if r is not None]
        rec = None
        ambiguous = len(hits) > 1 or len(cands) >= 3
        if len(hits) == 1 and not ambiguous:
            doi, rec = hits[0]

        # 标题兜底只在「PDF 里读不出 DOI」时才用。
        # 否则勘误（Erratum to …）、评论（Comment on …）这类 PDF 正文里必然完整引用原文标题，
        # 会被认成原文搬进库里，然后下载那边按文件名判「已有」，把真论文永久挡住。
        if rec is None and not cands:
            rec = _match_by_title(src, known)

        if rec is None:
            # 认不出来就不动。分成两类，好让用户知道下一步该干什么：
            #   unmatched —— 有 DOI，只是没检索过 / 台账里没有，回去检索一次就能认出来
            #   unknown   —— 连 DOI 都没有（扫描件、不是论文）
            if cands:
                stats["unmatched"] += 1
                # 把候选都列出来：折行时第一个是半截的，只报它会让用户去查一个查不到的 DOI
                unmatched.append(f"{src.name}（{' / '.join(cands[:2])}）"
                                 + ("　⚠ 一页里出现多个 DOI，可能是合集/推荐页"
                                    if len(cands) >= 3 or ambiguous else ""))
            else:
                stats["unknown"] += 1
                unknown.append(src.name)
            continue

        base = _pick_name(rec, doi)
        if not base:
            stats["failed"] += 1
            continue
        dst = lib / (base + ".pdf")
        try:
            n = 1
            too_many = False
            while dst.exists() and dst.stat().st_size != size:
                n += 1
                if n > 50:               # 同名堆了 50 份，不正常 —— 跳过，别覆盖别人
                    too_many = True
                    break
                dst = lib / f"{base}_{n}.pdf"
            if too_many:
                stats["failed"] += 1
                plan.append({"from": src.name, "to": base + ".pdf",
                             "act": "库里同名文件太多，跳过（怕覆盖）"})
                continue
        except OSError:
            stats["failed"] += 1
            continue

        stats["matched"] += 1
        try:
            same = dst.exists() and dst.stat().st_size == size
        except OSError:
            same = False
        if same:
            stats["skipped"] += 1        # 库里已经有同一份了
            plan.append({"from": src.name, "to": dst.name, "act": "已有，跳过"})
            continue

        if dry_run:
            plan.append({"from": src.name, "to": dst.name, "act": "将改名并移入文献库"})
            continue

        try:
            lib.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            stats["failed"] += 1
            plan.append({"from": src.name, "to": dst.name, "act": f"建目录失败：{e}"})
            continue
        why = _safe_move(src, dst)
        if why.startswith("WARN:"):
            stats["moved"] += 1          # 副本确实进库了，只是原件没删掉
            plan.append({"from": src.name, "to": dst.name,
                         "act": "已移入文献库（" + why[5:] + "）"})
        elif why:
            stats["failed"] += 1
            plan.append({"from": src.name, "to": dst.name, "act": f"失败：{why}"})
        else:
            stats["moved"] += 1
            plan.append({"from": src.name, "to": dst.name, "act": "已移入文献库"})

    # plan 不截断：整个安全设计就是「先看清单再确认」，砍掉一半等于把安全网剪了
    return {**stats, "folder": str(folder), "library": str(lib),
            "plan": plan, "total": total, "limited": total > len(pdfs),
            "unknown_files": unknown[:30], "unmatched_files": unmatched[:30]}


def guess_downloads() -> Path:
    """
    猜「下载」文件夹。
    ⚠ 不能直接用 Path.home() / "Downloads" —— 在 OneDrive 重定向或账户名含中文的机器上，
    Path.home() 可能返回一个**别名**目录，真实「下载」在另一个名字下，拼出来的路径是空的。
    所以先问注册表，再退回若干常见位置。
    """
    import os
    cands: list[Path] = []
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        for name in ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"):
            try:
                v, _ = winreg.QueryValueEx(k, name)
                if v:
                    cands.append(Path(v))
            except OSError:
                continue
        winreg.CloseKey(k)
    except Exception:
        pass
    for k in ("USERPROFILE", "OneDrive"):
        v = os.environ.get(k)
        if v:
            cands.append(Path(v) / "Downloads")
    cands += [Path.home() / "Downloads", Path("C:/Users/Public/Downloads")]
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return Path.home() / "Downloads"
