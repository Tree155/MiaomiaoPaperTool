# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2 — Organize Downloads (organize)

Author: Ni, Yishu & Da Fei Yu (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For learning and exchange only; commercial use prohibited.

Why this is needed:
  Full text of subscription journals cannot be downloaded, so you click publisher pages in a browser instead. You end up with
  `1-s2.0-S0308814619308454-main.pdf` and `jafc.7b00636.pdf` sitting in the Downloads folder,
  each needing a rename, a drop into the library and a tick back in the list — the most tedious step, and the easiest to forget.

  This module automates "rename + put in place":
    1. Scan .pdf files in one directory (the system Downloads folder by default)
    2. Read the DOI from the PDF (fall back to the title)
    3. Look the DOI up in the download ledger / current search results / existing library records
    4. On a match, rename by the standard rule (2024_Zhang_Aroma_of_shrimp_head.pdf) and move it into the library
    5. Leave unrecognized files **untouched** and only list them in the report

Safety boundaries (better to leave a file alone than to move it blindly):
  · Handle only files that can be confirmed as a specific paper; never touch unrecognized ones
  · Never overwrite a name collision; add a _2 suffix
  · Move only, never delete; failures tell you where it got stuck
  · Support dry_run: report the planned changes first, and act only after the user confirms
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

# DOI in PDF text. The charset must follow the DOI spec -- miss one char and the DOI is cut into
# "another DOI that can't be found" (Wiley's old SICI numbers with # % are the classic case).
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:<>%#&+=~a-zA-Z0-9]+", re.I)
# Junk often glued to the end of a DOI (typesetting residue)
_TRAIL = ".,;:)]}>\u3002\uff0c\uff1b"


def _clean_doi(s: str) -> str:
    d = (s or "").strip()
    while d and d[-1] in _TRAIL:
        d = d[:-1]
    return d.lower()


def pdf_text(path: Path, max_pages: int = 3) -> str:
    """Read the text of the first few PDF pages. Return an empty string for scans or encrypted files (the caller treats them as "unrecognized")."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        rd = PdfReader(str(path), strict=False)
        if getattr(rd, "is_encrypted", False):
            try:
                rd.decrypt("")            # Encrypted with an empty password (common in publisher PDFs)
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
    The title embedded in the PDF (most publisher PDFs have one). Return an empty string if unavailable.

    ⚠ `scan()` currently **does not call this**: to keep the promise that unrecognized files are never touched,
    any PDF with a DOI that is missing from the ledger is left alone (until the user runs a search and organizes again).
    Falling back to the metadata title looks clever, but it goes wrong on errata and collection pages,
    and a wrong move means the download side judges "already present" by file name — so that real paper can never be downloaded again.
    This function is kept for a future "recognize files by title" feature.
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
    Find DOIs in the PDF body text and return a **candidate list** (there may be two).

    Narrow columns break a DOI across two lines ("10.1111/ijfs." + "15747"), but conversely,
    when a complete DOI ends with a period ("10.5281/zenodo.1234567."),
    the next line is often a short word as well — gluing them blindly produces fake DOIs like "…1234567.All".
    So return both as candidates and let the caller check each against the ledger, using whichever one matches.
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
        if len(out) >= 5: # 5 DOIs on one page means a collection/recommendation page, that's enough
            break
    return out


def doi_from_pdf(path: Path) -> str:
    """Legacy call: return the first candidate only."""
    c = doi_candidates(path)
    return c[0] if c else ""


def _norm_title(s: str) -> str:
    """Title normalization: keep letters and digits only, for fuzzy comparison."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())


def load_known(cfg: dict, extra: list[dict] | None = None) -> dict[str, dict]:
    """
    Index of known papers; both keys point to the same record:
      · "doi:<lowercase doi>"
      · "t:<first 60 chars of the normalized title>"
    Sources: the download ledger `_manifest.csv`, current search results (extra), and the same-named list in the library.
    """
    out: dict[str, dict] = {}

    def put(rec: dict) -> None:
        d = (rec.get("doi") or "").strip().lower()
        t = (rec.get("title") or "").strip()
        if d:
            out.setdefault("doi:" + d, rec)
        if t:
            # Don't truncate: at 60 chars only the prefix can be verified, and series papers like Part 1/Part 2 would match each othe
            out.setdefault("t:" + _norm_title(t), rec)

    for rec in (extra or []):
        if isinstance(rec, dict):
            put(rec)

    # Manifest: program dir + library, either may have one
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
    """Generate the file name by the standard rule (without extension)."""
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
    Move src to dst: **copy fully to a temporary name → verify the size → rename to the target → delete the source last**.
    Return an empty string on success, otherwise an error description.

    Why not shutil.move directly: across drives it is copy2 + unlink, and a mid-copy failure (disk full,
    USB drive unplugged) **leaves a partial PDF in the library**, while the download side only checks size > 5KB for "full text present",
    so the partial file still passes — and that paper can never be downloaded in full again. Hence the manual version, with cleanup on failure.
    """
    tmp = dst.with_name(dst.name + ".part")
    try:
        shutil.copy2(str(src), str(tmp))
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError("Size mismatch after copy")
        tmp.replace(dst)                 # rename within the same volume, atomic
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)  # Remove the half-written file, don't leave it in the library
        except OSError:
            pass
        return f"{type(e).__name__}: {e}"
    try:
        src.unlink()
    except OSError:
        # The file is held by Acrobat / the preview pane. The copy is fine but we **must say so** --
        # else the user thinks it moved, the copy stays in Downloads, and next round says "already have, skipped".
        return ("WARN: the original cannot be deleted, it may be open; please delete it manually")
    return ""


def _match_by_title(pdf_path: Path, known: dict) -> dict | None:
    """
    Recognize files by title. **Accept high confidence only**: the whole title (≥20 chars) appears verbatim in the PDF body.

    It used to be a bare substring match (`record title in body`), so a single short title such as "Erratum" / "Editorial"
    in the ledger would claim every PDF whose body contained that word — the file was renamed after another paper and moved away,
    and since the download side judges "already present" by file name, the real paper was blocked for good. Better to skip than to match wrong.
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
        if len(t) < 20: # Titles that are too short never take part in matching
            continue
        if t in flat and len(t) > best_len:
            best, best_len = v, len(t)
    return best


def scan(folder: Path, cfg: dict, extra: list[dict] | None = None,
         dry_run: bool = True, limit: int = 300) -> dict:
    """
    Scan the PDFs in folder and rename/move into the library only those that can be **confirmed** as a specific paper.
    With dry_run=True, only report (list the planned changes in plan) and touch no file.

    What counts as "confirmed": a DOI read from the PDF that exists in the download ledger / current search results;
    or the entire title appearing verbatim in the body. Everything else stays untouched — a wrong match costs far more than a miss.
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
        return {**stats, "error": f"Directory does not exist:{folder}"}

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
        return {**stats, "error": f"Cannot read this directory:{e}"}

    for src in pdfs:
        stats["scanned"] += 1
        try:
            size = src.stat().st_size
            cands = doi_candidates(src)
        except OSError:
            stats["failed"] += 1
            continue
        doi = cands[0] if cands else ""

        # Look among the candidates for one the manifest knows. **More than 1 hit and don't dare** --
        # that means this PDF holds DOIs of several library papers (collection page, publisher picks page),
        # so we can't tell which is this paper; a wrong call means "moved the wrong file + the real paper seen as present".
        hits = [(c, known.get("doi:" + c)) for c in cands]
        hits = [(c, r) for c, r in hits if r is not None]
        rec = None
        ambiguous = len(hits) > 1 or len(cands) >= 3
        if len(hits) == 1 and not ambiguous:
            doi, rec = hits[0]

        # Title fallback is only used when "no DOI can be read from the PDF".
        # Otherwise errata (Erratum to ...) and comments (Comment on ...) always quote the original title in full,
        # would be taken for the original and moved into the library, then downloads see the filename as "present" and block the real paper for good.
        if rec is None and not cands:
            rec = _match_by_title(src, known)

        if rec is None:
            # If it can't be identified, don't touch it. Split into two kinds so the user knows what to do next:
            #   unmatched -- has a DOI, just never searched / not in the manifest; one more search identifies it
            #   unknown   -- no DOI at all (scanned, not a paper)
            if cands:
                stats["unmatched"] += 1
                # List all candidates: on wrap the first is a truncated one, reporting only it sends the user after a DOI that doesn't exist
                unmatched.append(f"{src.name}({' / '.join(cands[:2])})"
                                 + ("⚠ Multiple DOIs on one page; it may be a collection or recommendation page"
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
                if n > 50: # 50 copies under one name is not normal -- skip, don't overwrite someone else
                    too_many = True
                    break
                dst = lib / f"{base}_{n}.pdf"
            if too_many:
                stats["failed"] += 1
                plan.append({"from": src.name, "to": base + ".pdf",
                             "act": "Too many files with this name in the library; skipped to avoid overwriting"})
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
            stats["skipped"] += 1        # The library already has the same one
            plan.append({"from": src.name, "to": dst.name, "act": "Already present, skipped"})
            continue

        if dry_run:
            plan.append({"from": src.name, "to": dst.name, "act": "Will be renamed and moved into the library"})
            continue

        try:
            lib.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            stats["failed"] += 1
            plan.append({"from": src.name, "to": dst.name, "act": f"Failed to create directory:{e}"})
            continue
        why = _safe_move(src, dst)
        if why.startswith("WARN:"):
            stats["moved"] += 1          # The copy did land in the library, only the original wasn't deleted
            plan.append({"from": src.name, "to": dst.name,
                         "act": "Moved into the library (" + why[5:] + ")"})
        elif why:
            stats["failed"] += 1
            plan.append({"from": src.name, "to": dst.name, "act": f"Failed:{why}"})
        else:
            stats["moved"] += 1
            plan.append({"from": src.name, "to": dst.name, "act": "Moved into the library"})

    # plan is not truncated: the whole safety design is "review the list, then confirm"; cutting half of it cuts the net
    return {**stats, "folder": str(folder), "library": str(lib),
            "plan": plan, "total": total, "limited": total > len(pdfs),
            "unknown_files": unknown[:30], "unmatched_files": unmatched[:30]}


def guess_downloads() -> Path:
    """
    Guess the Downloads folder.
    ⚠ Do not just use Path.home() / "Downloads" — on machines with OneDrive redirection or a non-ASCII account name,
    Path.home() may return an **alias** directory while the real Downloads folder sits under another name, so the joined path is empty.
    So ask the registry first, then fall back to a few common locations.
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
