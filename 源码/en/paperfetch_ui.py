#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2

Author: Ni, Yishu & Big Fat Fish (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For study and exchange only; commercial use is prohibited.

Local UI (for debugging with the desktop client or a browser).

    python paperfetch_ui.py            # default http://127.0.0.1:8765
    python paperfetch_ui.py --port 9000 --open

Listens on the loopback address only and is never exposed externally.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paperfetch as pf
import enrich as en
import localsearch as ls
import pubmed as pm
import cites as ct
import organize as og
import zotero as zo

HERE = Path(__file__).resolve().parent


def app_dir() -> Path:
    return pf.app_dir()


def res_dir() -> Path:
    """Read-only resource directory (ui.html). After packaging it lives in the temporary extraction directory."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return HERE


CFG: dict = {}
SESS: pf.PoliteSession | None = None
CACHE: "en.Cache | None" = None
PMC: "pm.Cache | None" = None


def cache() -> "en.Cache":
    """On-disk cache of translated titles + journal metrics (lazy loaded)."""
    global CACHE
    if CACHE is None:
        CACHE = en.Cache(pf.app_dir())
    return CACHE


def pm_cache() -> "pm.Cache":
    """PubMed DOI→type/MeSH cache (lazy loaded)."""
    global PMC
    if PMC is None:
        PMC = pm.Cache(pf.app_dir())
    return PMC

STATE = {
    "results": [],
    "prev": None, # The keyword results from before citation expansion, for "back"
    "job": None, # {running,total,done,ok,skip,miss,fail,current}
    "log": [],
    "lock": threading.Lock(),
}


def logline(msg: str) -> None:
    with STATE["lock"]:
        STATE["log"].append(msg)
        if len(STATE["log"]) > 400:
            del STATE["log"][:-400]


# Probing Zotero reads 4000+ items, 2-3 s, cache it so every click doesn't wait
ZOT: dict = {"at": 0.0, "data": None}
# organize downloads really moves files, only one at a time:
# two tabs clicking at once stat each other's "just moved" files and report a pile of false failures
ORG_LOCK = threading.Lock()


def zotero_status(force: bool = False, ttl: float = 90.0) -> dict:
    now = time.time()
    if not force and ZOT["data"] is not None and now - ZOT["at"] < ttl:
        return ZOT["data"]
    d = zo.probe()
    if d.get("ok"):
        d["dois"] = sorted(d.get("dois") or [])
    d["at"] = int(now)
    d["last_written"] = _last_write()
    ZOT["data"], ZOT["at"] = d, now
    return d


# ---- Write straight into Zotero: only three things (create collection / create item / undo), never touch existing items ----
WRITER: "zo.Writer | None" = None


def writer() -> "zo.Writer":
    global WRITER
    if WRITER is None:
        WRITER = zo.Writer(app_dir())
    return WRITER


def _last_write_file() -> Path:
    return app_dir() / ".zotero_last.json"


def _last_write() -> dict | None:
    try:
        d = json.loads(_last_write_file().read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("collection"):
            return {"name": d.get("name") or "", "n": len(d.get("items") or []),
                    "at": d.get("at") or 0, "collection": d["collection"]}
    except Exception:
        pass
    return None


def _save_last_write(d: dict) -> None:
    try:
        _last_write_file().write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    except OSError:
        pass


def _accounts_view() -> list:
    """Account list for the frontend (the key shows only its last 4 characters)."""
    accs = pf.openalex_accounts(CFG)
    idx = pf.active_account_index(CFG)
    return [{
        "index": i,
        "label": a.get("label") or "Untitled",
        "email": a.get("email") or "",
        "tail": (a.get("api_key") or "")[-4:],
        "active": (i == idx),
    } for i, a in enumerate(accs)]


def do_search(q: str, limit: int, y1, y2, oa_only: bool,
              field: str = "all", op: str = "AND",
              sort: str = "relevance", doc_type: str = "article") -> dict:
    original, used, leftover = q, q, []
    if re.search(r"[\u4e00-\u9fff]", q):
        used, leftover = pf.zh_to_en(q)
    info = {}
    try:
        recs, total = pf.search_openalex(
            used, CFG, SESS, limit=limit, year_from=y1, year_to=y2, oa_only=oa_only,
            default_field=(field or "all"), default_op=(op or "AND"),
            sort=(sort or "relevance"), doc_type=(doc_type or "article"), info=info)
    except pf.QueryError as e:
        return {"error": str(e), "query_error": True,
                "err_kind": getattr(e, "kind", "syntax"),
                "original": original, "used_query": used, "leftover": leftover}
    except pf.QuotaExceeded as e:
        return {"error": str(e), "quota": True, "original": original,
                "used_query": used, "leftover": leftover}
    # PubMed to fix work type + MeSH: OpenAlex review labels are unreliable (measured: only 3 labeled on one topic,
    # PubMed labeled 9). Match by DOI only, skip items without one; stay silent on failure.
    pm_stat: dict = {}
    if CFG.get("pubmed_enrich", True):
        try:
            pm_stat = pm.enrich(recs, pm_cache(), CFG.get("pubmed_api_key") or "")
        except Exception as e:
            pm_stat = {"error": type(e).__name__}

    with STATE["lock"]:
        STATE["results"] = recs
        STATE["prev"] = None          # A new search drops the old "back" target
    # Save a copy for the in-library search index (metadata + abstract, bibliographic data only, no full text)
    try:
        # ! Don't name the variable slim -- that's the module-level serialization function, the same name would shadow it
        brief = [{"title": r.get("title", ""), "abstract": r.get("abstract", ""),
                  "journal": r.get("journal", ""), "year": r.get("year") or "",
                  "doi": r.get("doi", "")} for r in recs]
        (app_dir() / ".last_results.json").write_text(
            json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return {"n": len(recs), "total": total, "original": original,
            "used_query": used, "leftover": leftover, "parse": info,
            "pubmed": pm_stat, "results": slim(recs)}


def slim(recs: list[dict]) -> list[dict]:
    """
    Internal record → the structure the UI uses.
    All four paths — search, citation expansion, the full list and further reading — go through here,
    so field names and type judgement stay identical (they used to be scattered across four places; fix one and miss three).
    """
    out = []
    for i, r in enumerate(recs):
        wt = r.get("work_type") or ""
        out.append({
            "i": i,
            "title": r.get("title", ""),
            "journal": r.get("journal", ""),
            "year": r.get("year"),
            "doi": r.get("doi", ""),
            "is_oa": r.get("is_oa"),
            "oa_status": r.get("oa_status", ""),
            "cited_by": r.get("cited_by", 0),
            "first_author": r.get("first_author", ""),
            "n_authors": r.get("n_authors", 0),
            "work_type": wt,
            "type_label": pf.TYPE_LABEL.get(wt, wt),
            # If the type came from PubMed, mark the source so the user doesn't think OpenAlex got it right
            "type_from": r.get("type_from", ""),
            "maybe_review": pf.is_maybe_review(r),
            "mesh": r.get("mesh") or [],
            "pmid": r.get("pmid", ""),
            # In citation expansion, "linked by several seeds" (plain search has no such field)
            "seed_hits": r.get("seed_hits") or 0,
            "found_via": r.get("found_via", ""),
            "abstract": (r.get("abstract") or "")[:1600],
        })
    return out


def _write_missing_files(manifest: list[dict], miss_recs: list[dict], out_dir: Path) -> None:
    """The three "no full text obtained" lists. Failing to write any one of them must not bring down the whole job."""
    miss = [m for m in manifest if m.get("status") == "no_source"]
    if not miss:
        return
    try:
        with (out_dir / "_missing_for_library.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["doi", "title", "journal", "year", "first_author"])
            w.writeheader()
            for m in miss:
                w.writerow({k: m.get(k, "") for k in w.fieldnames})
    except Exception as e:
        logline(f"  (could not write _missing_for_library.csv:{type(e).__name__} -- it may be open in Excel)")
    try:
        pf.write_ris(miss_recs, out_dir / "_missing_for_zotero.ris")
        pf.write_missing_html(miss_recs, out_dir / "to-download.html",
                              f"A total of {len(miss_recs)} papers could not be retrieved in full")
        logline("")
        logline(f"Not retrieved: {len(miss_recs)} papers. Three routes, easiest first:")
        logline(f"  ① On the campus network/VPN, click to download → {out_dir / 'to-download.html'}")
        logline(f"  ② Zotero bulk PDF fetch   → {out_dir / '_missing_for_zotero.ris'}")
        logline(f"  ③ Library interlibrary loan       → {out_dir / '_missing_for_library.csv'}")
    except Exception as e:
        logline(f"(failed to generate the list:{type(e).__name__})")


def run_fetch(indices: list[int], out_dir: Path) -> None:
    """
    Download thread. Three hard rules, all of them learned the hard way:
      1. **Snapshot STATE["results"] up front** — changing the query mid-download must not bleed into this run;
      2. **Wrap the whole block in try/finally** — any exception must set job.running back to False,
         otherwise the frontend stays on "downloading" forever and every later download is blocked with 409, leaving a restart as the only way out;
      3. **An error on one record means only that record failed** — a full disk or a file locked by antivirus must not take down the whole batch.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with STATE["lock"]:
        snapshot = list(STATE["results"])
        total = len(indices)
        STATE["job"] = {"running": True, "total": total, "done": 0,
                        "ok": 0, "preview": 0, "skip": 0, "miss": 0, "fail": 0, "current": ""}
    manifest: list[dict] = []
    miss_recs: list[dict] = []
    try:
        for n, idx in enumerate(indices, 1):
            r = snapshot[idx] if 0 <= idx < len(snapshot) else None
            if r is None: # Result set changed, index out of range: count as one failure, don't stall the progress
                with STATE["lock"]:
                    STATE["job"]["done"] += 1
                    STATE["job"]["fail"] += 1
                logline(f"[{n}/{total}] item {idx} is no longer in the results, skipped")
                continue
            title = (r.get("title") or "")[:70]
            with STATE["lock"]:
                STATE["job"]["current"] = title
            logline(f"[{n}/{total}] {r.get('year','')} {title}")

            status, via, key = "fail", "", "fail"
            try:
                pf.resolve_arxiv(r)
                pf.enrich_unpaywall(r, CFG, SESS)
                if (r.get("doi") or "").startswith("10.1016"):
                    pf.resolve_elsevier_api(r, CFG, SESS)
                if r.get("pdf_url") and r["pdf_url"] not in (r.get("pdf_candidates") or []):
                    r.setdefault("pdf_candidates", []).insert(0, r["pdf_url"])
                if not r.get("pdf_candidates") and not r.get("elsevier_ready"):
                    pf.resolve_europepmc(r, SESS)

                status, path, via = pf.download_one(r, CFG, SESS, out_dir)
                key = {"ok": "ok", "preview": "preview", "skip_dup": "skip"}.get(
                    status, "miss" if status == "no_source" else "fail")
                if status == "ok" and path:
                    size_kb = path.stat().st_size // 1024
                    logline(f"    ✓ {path.name} ({size_kb} KB, {via})")
                elif status == "preview":
                    logline("    △ First page only, no XML available (turn on \"save first-page image\" or rerun on the campus network)")
                elif status == "skip_dup":
                    logline("    = already exists, skipped")
                elif status == "no_source":
                    logline(f"    ✗ not retrieved -- {pf.why_missing(r)}")
                    miss_recs.append(r)
                else:
                    logline(f"    ✗ failed {via}")
            except Exception as e:
                status, via, key = "fail", type(e).__name__, "fail"
                logline(f"    ✗ error:{type(e).__name__}: {e}")
            finally:
                with STATE["lock"]:
                    STATE["job"]["done"] += 1
                    STATE["job"][key] += 1

            try:
                manifest.append({**{k: r.get(k, "") for k in pf.FIELDS},
                                 "status": status, "via": via})
            except Exception:
                pass
    except Exception as e:
        logline(f"Task aborted with an error:{type(e).__name__}: {e}")
    finally:
        # Manifest and list each fall back on their own: one file open in Excel shouldn't kill the whole job
        try:
            pf.write_manifest(manifest, out_dir / "_manifest.csv")
        except Exception as e:
            logline(f"Could not write the manifest ({type(e).__name__}) -- _manifest.csv may be open in Excel")
        _write_missing_files(manifest, miss_recs, out_dir)
        with STATE["lock"]:
            STATE["job"]["running"] = False
            STATE["job"]["current"] = ""
        logline("Done.")


# --------------------------------------------------------------------------- #
# Export sheet (the "Export sheet" button in the UI)
#   columns are built by the front end (Chinese title / IF / quartile are pulled on demand); this only writes the file.
# --------------------------------------------------------------------------- #
EXPORT_COLS = [
    ("Title", "title", 62),
    ("Journal", "journal", 28),
    ("Type", "dtype", 14),
    ("IF", "if", 7),
    ("CAS quartile", "cas", 12),
    ("JCR quartile", "jcr", 10),
    ("Year", "year", 7),
    ("Citations", "cited_by", 7),
    ("Access route", "oa", 9),
    ("DOI", "doi", 34),
    ("First author", "first_author", 15),
    ("Abstract", "abstract", 90),
]


LIB_COLS = [
    ("File name", "name", 56),
    ("Title", "title", 58),
    ("DOI", "doi", 34),
    ("Size (KB)", "size_kb", 10),
    ("Modified", "mtime_txt", 13),
    ("Directory", "folder", 58),
]


def _safe_tag(s: str) -> str:
    """Use the query as the file name: strip characters Windows rejects, and do not make it too long."""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", (s or "").strip())
    s = re.sub(r"\s+", "_", s).strip("._- ")
    return s[:28]


def export_path(tag: str, ext: str, prefix: str = "search-results") -> Path:
    d = app_dir() / "export"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = _safe_tag(tag)
    stem = f"{prefix}_{name}_{stamp}" if name else f"{prefix}_{stamp}"
    p = d / f"{stem}.{ext}"
    n = 2
    while p.exists(): # Two exports in the same second don't overwrite each othe
        p = d / f"{stem}_{n}.{ext}"
        n += 1
    return p


_ILLEGAL_CELL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
CELL_LIMIT = 32767          # Hard Excel cell limit


def _clean_cell(v, limit: int = CELL_LIMIT) -> str:
    """
    openpyxl raises IllegalCharacterError outright on \x00-\x08 / \x0b / \x0c / \x0e-\x1f,
    and metadata occasionally contains such characters (abstracts especially) → the whole sheet returns 500. They are scrubbed here, and long text is truncated to the Excel limit.
    """
    if v is None:
        return ""
    s = _ILLEGAL_CELL.sub(" ", str(v))
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s


def _write_export_csv(rows: list[dict], path: Path, cols) -> int:
    pairs = [(h, k) for h, k, _ in cols]
    with path.open("w", newline="", encoding="utf-8-sig") as f: # BOM: no garbling when Excel opens it by double-click
        w = csv.writer(f)
        w.writerow([h for h, _ in pairs])
        for r in rows:
            w.writerow([_clean_cell(r.get(k, "")) for _, k in pairs])
    return len(rows)


def _write_export_xlsx(rows: list[dict], path: Path, cols) -> int:
    """Returns the number of rows written; raises ImportError when openpyxl is missing (the caller falls back to CSV)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "list"
    ws.append([h for h, _, _ in cols])
    thin = Side(style="thin", color="DDDDDD")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF", size=10.5)
        c.fill = PatternFill("solid", fgColor="4F6228")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = Border(bottom=thin)
    ws.row_dimensions[1].height = 22

    wrap_keys = {"zh_title", "title", "abstract", "folder"}
    center_keys = {"if", "cas", "jcr", "year", "cited_by", "oa", "dtype", "size_kb", "mtime_txt"}
    for r in rows:
        ws.append([_clean_cell(r.get(k, "")) for _, k, _ in cols])
        for idx, (_, k, _) in enumerate(cols, 1):
            cell = ws.cell(ws.max_row, idx)
            cell.alignment = Alignment(vertical="top", wrap_text=(k in wrap_keys),
                                       horizontal="center" if k in center_keys else "left")
            cell.font = Font(size=10)
            if k == "doi" and cell.value:
                cell.hyperlink = f"https://doi.org/{cell.value}"
                cell.font = Font(size=10, color="1155CC", underline="single")

    for i, (_, k, wd) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return len(rows)


def write_export(rows: list[dict], fmt: str, tag: str = "",
                 cols=None, prefix: str = "search-results") -> tuple[Path, str, int]:
    """Write the table and return (path, actual format, rows). Falls back to csv automatically when xlsx is unavailable."""
    cols = cols or EXPORT_COLS
    fmt = (fmt or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        raise ValueError(f"Unsupported format:{fmt}")
    if fmt == "xlsx":
        p = export_path(tag, "xlsx", prefix)
        try:
            n = _write_export_xlsx(rows, p, cols)
            return p, "xlsx", n
        except ImportError:
            p.unlink(missing_ok=True)
            fmt = "csv"
    p = export_path(tag, "csv", prefix)
    return p, "csv", _write_export_csv(rows, p, cols)


def _library_meta() -> dict[str, dict]:
    """A library scan yields only file names and DOIs, with journal/year/author missing.
    Fill them in by DOI from _manifest.csv and the previous search results, so the Zotero import is not all empty fields."""
    out: dict[str, dict] = {}

    def put(doi: str, d: dict) -> None:
        doi = (doi or "").strip().lower()
        if not doi:
            return
        cur = out.setdefault(doi, {})
        for k, v in d.items():
            if v and not cur.get(k):
                cur[k] = v

    try:
        p = pf.library_dir(CFG) / "_manifest.csv"
        if p.is_file():
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    put(row.get("doi") or "", {"title": row.get("title") or "",
                                               "journal": row.get("journal") or "",
                                               "year": row.get("year") or "",
                                               "first_author": row.get("first_author") or ""})
    except Exception:
        pass
    try:
        lr = app_dir() / ".last_results.json"
        if lr.is_file():
            for r in json.loads(lr.read_text(encoding="utf-8")) or []:
                put(r.get("doi") or "", {"title": r.get("title") or "",
                                         "journal": r.get("journal") or "",
                                         "year": str(r.get("year") or "")})
    except Exception:
        pass
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "paperfetch-ui"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        raw = self.path
        path = raw.split("?", 1)[0]      # Drop the query, otherwise /?tab=lib would 404
        if path in ("/", "/index.html"):
            page = (res_dir() / "ui.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/api/config":
            self._json({
                "email": bool(CFG.get("email")),
                "elsevier_api_key": bool(CFG.get("elsevier_api_key")),
                "elsevier_insttoken": bool(CFG.get("elsevier_insttoken")),
                "rps": CFG.get("rate_limit_rps"),
                "out_dir": str(pf.library_dir(CFG)),
                "library": str(pf.library_dir(CFG)),
                "sources": CFG.get("extra_sources") or [],
                "accounts": _accounts_view(),
                "active_index": pf.active_account_index(CFG),
                "signup": pf.OPENALEX_SIGNUP,
                "save_format": pf.save_format(CFG),
                "keep_preview_pdf": bool(CFG.get("keep_preview_pdf")),
                "formats": pf.SAVE_FORMATS,
                "easyscholar": bool(CFG.get("easyscholar_key")),
                "easyscholar_tail": (CFG.get("easyscholar_key") or "")[-4:],
                "zh_title": CFG.get("show_zh_title", True) is not False,
            })
        elif path == "/api/library":
            force = "force=1" in raw
            try:
                self._json(pf.scan_library(CFG, force=force))
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif path == "/api/quota":
            force = "force=1" in raw
            try:
                q = pf.probe_quota(CFG, SESS, force=force)
            except Exception as e:
                q = {"error": f"{type(e).__name__}: {e}"}
            q["accounts"] = _accounts_view()
            q["active_index"] = pf.active_account_index(CFG)
            q["signup"] = pf.OPENALEX_SIGNUP
            self._json(q)

        elif path == "/api/results":
            with STATE["lock"]:
                recs = list(STATE["results"])
            self._json({"n": len(recs), "results": slim(recs)})
        elif path == "/api/organize/dir":
            # Return only the guessed "Downloads" folder as the UI default (the user can change it)
            self._json({"dir": str(og.guess_downloads())})

        elif path == "/api/kstatus":
            st = ls.status()
            st["notes_dirs"] = [str(p) for p in ls.notes_dirs(CFG)]
            st["notes_cfg"] = [str(Path(x)) for x in (CFG.get("notes_dirs") or []) if x]
            self._json(st)
        elif path == "/api/zotero":
            force = "force=1" in raw
            try:
                d = zotero_status(force=force)
            except Exception as e:
                d = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            d.pop("dois_set", None)
            self._json(d)
        elif path == "/api/progress":
            with STATE["lock"]:
                self._json({"job": STATE["job"], "log": STATE["log"][-120:]})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # Never let an unexpected exception kill the connection -- if it dies the front end only sees
        # "Failed to fetch" with no error text at all (bitten once by an int year on 2026-11-04)
        try:
            self._do_post()
        except Exception as e:
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def _do_post(self):
        # Only accept requests from this page: any other web page can POST to 127.0.0.1 too (DNS rebinding / port scan),
        # and /api/open lets Explorer open any path, so it must be blocked.
        origin = (self.headers.get("Origin") or "").strip()
        port = self.server.server_port
        ok_hosts = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        if origin and not origin.startswith(ok_hosts):
            self._json({"error": "Cross-site request rejected"}, 403)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        if self.path == "/api/search":
            try:
                out = do_search(
                    body.get("query", "").strip(),
                    max(1, min(int(body.get("limit") or 30), 1000)), # Cap 1000: more than that and the front end renders slowly
                    body.get("year_from") or None,
                    body.get("year_to") or None,
                    bool(body.get("oa_only")),
                    body.get("field") or "all",
                    body.get("op") or "AND",
                    body.get("sort") or "relevance",
                    body.get("doc_type") or "article",
                )
                self._json(out)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        elif self.path == "/api/open":
            target = (body.get("path") or "").strip()
            # The library may not exist yet (no longer auto-created), create it when "Open" is clicked
            try:
                tp = Path(target) if target else None
                if tp and not tp.exists() and tp == pf.library_dir(CFG):
                    tp.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            if target and Path(target).exists():
                try:
                    if Path(target).is_dir():
                        subprocess.Popen(["explorer", str(Path(target))])
                    else:
                        subprocess.Popen(["explorer", "/select,", str(Path(target))])
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"error": str(e)}, 500)
            else:
                self._json({"error": "Path does not exist"}, 400)

        elif self.path == "/api/sources":
            act = (body.get("action") or "").strip()
            path = (body.get("path") or "").strip().strip('"')
            srcs = list(CFG.get("extra_sources") or [])
            if act == "add":
                if not path or not Path(path).is_dir():
                    self._json({"error": "Directory does not exist: %s" % path}, 400)
                    return
                if path not in srcs:
                    srcs.append(path)
            elif act == "remove":
                srcs = [x for x in srcs if x != path]
            elif act == "clear":
                srcs = []
            else:
                self._json({"error": "Unknown operation"}, 400)
                return
            CFG["extra_sources"] = srcs
            pf.save_config(CFG)
            self._json({"sources": srcs})

        elif self.path == "/api/storage":
            fmt = (body.get("save_format") or "").strip().lower()
            if fmt and fmt not in pf.SAVE_FORMATS:
                self._json({"error": "Unknown storage format"}, 400)
                return
            if fmt:
                CFG["save_format"] = fmt
            if "keep_preview_pdf" in body:
                CFG["keep_preview_pdf"] = bool(body.get("keep_preview_pdf"))
            pf.save_config(CFG)
            f = pf.save_format(CFG)
            logline(f"Storage format changed to:{pf.SAVE_FORMATS[f]['label']}"
                    + ("(first-page image saved separately)" if CFG.get("keep_preview_pdf") else ""))
            self._json({"save_format": f,
                        "keep_preview_pdf": bool(CFG.get("keep_preview_pdf")),
                        "formats": pf.SAVE_FORMATS})

        elif self.path == "/api/easyscholar":
            act = (body.get("action") or "save").strip()
            if act == "toggle_zh":
                CFG["show_zh_title"] = bool(body.get("on"))
                pf.save_config(CFG)
                self._json({"zh_title": CFG["show_zh_title"]})
                return
            if act == "clear":
                CFG["easyscholar_key"] = ""
                pf.save_config(CFG)
                logline("easyScholar key cleared; the IF / quartile columns will show \"—\"")
                self._json({"easyscholar": False})
                return
            key = (body.get("key") or "").strip()
            if not key:
                self._json({"error": "The key is empty"}, 400)
                return
            t = en.test_key(key)
            if not t.get("ok"):
                self._json({"error": t.get("msg") or "Verification failed"}, 400)
                return
            CFG["easyscholar_key"] = key
            pf.save_config(CFG)
            logline("easyScholar key saved; IF / CAS quartile are now available")
            self._json({"easyscholar": True, "test": t})

        elif self.path == "/api/zh":
            # English build: Chinese title translation is not offered.
            self._json({"map": {}, "note": "disabled"})

        elif self.path == "/api/zh_abs":
            # English build: abstract translation is not offered.
            self._json({"zh": "", "note": "disabled"})

        elif self.path == "/api/rank":
            names = [str(x) for x in (body.get("names") or [])][:40]
            key = (CFG.get("easyscholar_key") or "").strip()
            try:
                m, note = en.journal_metrics(names, key, cache())
                self._json({"map": m, "note": note, "configured": bool(key)})
            except Exception as e:
                self._json({"map": {}, "note": f"{type(e).__name__}", "configured": bool(key)}, 500)

        elif self.path == "/api/account/switch":
            try:
                idx = int(body.get("index"))
                accs = pf.openalex_accounts(CFG)
                if not (0 <= idx < len(accs)):
                    self._json({"error": "Account does not exist"}, 400)
                    return
                CFG["openalex_accounts"] = accs
                CFG["openalex_active"] = idx
                pf.sync_active_key(CFG)
                pf.save_config(CFG)
                SESS.quota = {}          # Re-check the quota after switching accounts
                q = pf.probe_quota(CFG, SESS, force=True)
                q["accounts"] = _accounts_view()
                q["active_index"] = idx
                logline(f"Switched to account:{accs[idx].get('label')}")
                self._json(q)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        elif self.path == "/api/account/add":
            try:
                idx = pf.add_account(CFG,
                                     body.get("label") or "",
                                     body.get("email") or "",
                                     body.get("api_key") or "")
                pf.save_config(CFG)
                SESS.quota = {}
                q = pf.probe_quota(CFG, SESS, force=True)
                q["accounts"] = _accounts_view()
                q["active_index"] = idx
                logline(f"Account added:{pf.active_account(CFG).get('label')}")
                self._json(q)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        elif self.path == "/api/account/request":
            try:
                d = pf.request_openalex_key(body.get("email") or "")
                self._json(d)
            except (ValueError, RuntimeError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        elif self.path == "/api/export":
            rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                self._json({"error": "Nothing to export (run a search first)"}, 400)
                return
            # The front end only has 1600 chars of abstract (enough for the preview); export the full one stored server side
            with STATE["lock"]:
                full = {(r.get("doi") or "").lower(): (r.get("abstract") or "")
                        for r in STATE["results"] if r.get("doi")}
            for r in rows:
                key = (r.get("doi") or "").lower()
                if key and len(full.get(key, "")) > len(r.get("abstract") or ""):
                    r["abstract"] = full[key]
            try:
                p, used, n = write_export(rows, body.get("fmt") or "xlsx",
                                          body.get("tag") or "")
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            logline(f"Exported {n} results → {p.name}({p.stat().st_size // 1024} KB)")
            self._json({"path": str(p), "fmt": used, "n": n,
                        "size": p.stat().st_size})

        elif self.path == "/api/library_export":
            rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                self._json({"error": "No items to export (scan the library first)"}, 400)
                return
            try:
                p, used, n = write_export(rows, body.get("fmt") or "xlsx",
                                          "", cols=LIB_COLS, prefix="library")
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._json({"path": str(p), "fmt": used, "n": n})

        elif self.path == "/api/kbuild":
            try:
                st = ls.build(CFG)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            logline(f"Library index rebuilt:{st['docs']} entries (abstracts {st['papers']} / notes {st['notes']} / "
                    f"search records {st['records']}), skipped {st['skipped_pdf']} PDFs")
            st["notes_dirs"] = [str(p) for p in ls.notes_dirs(CFG)]
            self._json(st)

        elif self.path == "/api/ksearch":
            try:
                self._json(ls.search(body.get("q") or "", int(body.get("k") or 30)))
            except Exception as e:
                self._json({"n": 0, "results": [], "error": f"{type(e).__name__}: {e}"}, 500)

        elif self.path == "/api/zotero/rdf":
            rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                self._json({"error": "No items to import"}, 400)
                return
            skip = bool(body.get("skip_existing", True))
            meta = _library_meta()
            for r in rows:
                k = (r.get("doi") or "").strip().lower()
                m = meta.get(k) or {}
                for f in ("title", "journal", "year", "first_author"):
                    if not str(r.get(f) or "").strip() and m.get(f):
                        r[f] = m[f]
            known: set[str] = set()
            if skip:
                try:
                    known = {d.lower() for d in (zotero_status().get("dois") or [])}
                except Exception:
                    known = set()
            before = len(rows)
            if known:
                rows = [r for r in rows
                        if (r.get("doi") or "").strip().lower() not in known]
            if not rows:
                self._json({"ok": True, "n": 0, "skipped": before,
                            "msg": "These items are already in Zotero"})
                return
            group = (body.get("group") or "journal").strip()
            if group not in ("journal", "year", "single"):
                group = "journal"
            tag = _safe_tag(body.get("tag") or "")
            p = export_path(tag, "rdf", prefix="Zotero Import")
            n = zo.write_rdf(rows, p, group, body.get("root") or "Miaomiao Paper Tool")
            logline(f"Zotero import file created:{p.name}({n} results, skipped existing {before - n} results)")
            self._json({"ok": True, "path": str(p), "n": n, "skipped": before - n,
                        "group": group})

        elif self.path == "/api/zotero/write":
            rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                self._json({"error": "No items to write"}, 400)
                return
            # Ones already in the library (by DOI) are still skipped, so nothing is written twice
            known: set[str] = set()
            try:
                known = {d.lower() for d in (zotero_status().get("dois") or [])}
            except Exception:
                known = set()
            before = len(rows)
            if known:
                rows = [r for r in rows if (r.get("doi") or "").strip().lower() not in known]
            meta = _library_meta()
            for r in rows:
                k = (r.get("doi") or "").strip().lower()
                m = meta.get(k) or {}
                for f in ("title", "journal", "year", "first_author"):
                    if not str(r.get(f) or "").strip() and m.get(f):
                        r[f] = m[f]

            name = (body.get("name") or "").strip() or \
                "Miaomiao Import " + time.strftime("%Y-%m-%d %H:%M")
            if body.get("dry_run"):
                self._json({"ok": True, "dry_run": True, "name": name, "n": len(rows),
                            "skipped": before - len(rows),
                            "no_doi": sum(1 for r in rows if not (r.get("doi") or "").strip()),
                            "sample": [(r.get("title") or "")[:80] for r in rows[:5]],
                            "api": zo.api_alive(),
                            "server_id": bool(writer().server_id())})
                return
            if not rows:
                self._json({"ok": True, "n": 0, "skipped": before,
                            "msg": "These items are already in Zotero"})
                return

            w = writer()
            ck, why = w.create_collection(name)
            if not ck:
                self._json({"error": why or "Failed to create collection"}, 400)
                return
            done, fails = w.create_items(rows, ck)
            _save_last_write({"at": int(time.time()), "name": name, "collection": ck,
                              "items": done})
            ZOT["at"] = 0                      # Invalidate the cache after writing so the next read is fresh
            logline(f"Written to Zotero: collection \"{name}”{len(done)} results"
                    + (f", failed {len(fails)} results" if fails else ""))
            self._json({"ok": True, "collection": ck, "name": name,
                        "n": len(done), "skipped": before - len(rows),
                        "fail_n": len(fails), "failed": fails[:6],
                        "can_undo": True})

        elif self.path == "/api/zotero/undo":
            last = None
            try:
                last = json.loads(_last_write_file().read_text(encoding="utf-8"))
            except Exception:
                last = None
            if not last or not last.get("collection"):
                self._json({"error": "No write records to undo"}, 400)
                return
            w = writer()
            n = w.delete_items(last.get("items") or [])
            ver = w.collection_version(last["collection"])
            ok_del = w.delete_collection(last["collection"], ver)
            _last_write_file().unlink(missing_ok=True)
            ZOT["at"] = 0
            logline(f"Undid the Zotero write: deleted {n} results, collection \"{last.get('name')}”"
                    + ("Deleted" if ok_del else "Not deleted (it may be locked in Zotero)"))
            self._json({"ok": True, "items": n, "collection_deleted": ok_del,
                        "name": last.get("name")})

        elif self.path == "/api/zotero/dir":
            # New users don't know where their Zotero library is, so let them point at a folder (parent folder accepted)
            p = (body.get("path") or "").strip().strip('"')
            if not p:
                self._json({"error": "No directory selected"}, 400)
                return
            hit = zo.set_data_dir(p)
            if not hit:
                self._json({"error": "This directory has no zotero.sqlite; try another one"
                                     "(usually Zotero's \"data directory\", containing zotero.sqlite and the storage folder)"}, 400)
                return
            CFG["zotero_data_dir"] = str(hit)
            pf.save_config(CFG)
            ZOT["at"] = 0
            logline(f"Zotero data directory set to:{hit}")
            self._json(zotero_status(force=True))

        elif self.path == "/api/notes":
            act = (body.get("action") or "").strip()
            p = (body.get("path") or "").strip().strip('"')

            def norm(x: str) -> str:
                try:
                    return str(Path(x).resolve()).lower()
                except OSError:
                    return str(x).lower()

            dirs = [x for x in (CFG.get("notes_dirs") or []) if x]
            if act == "add":
                if not p or not Path(p).is_dir():
                    self._json({"error": f"Directory does not exist:{p}"}, 400)
                    return
                if norm(p) not in {norm(x) for x in dirs}:
                    dirs.append(str(Path(p)))        # Store the normalized form so the UI can compare it
            elif act == "remove":
                dirs = [x for x in dirs if norm(x) != norm(p)]
            elif act == "clear":
                dirs = []
            else:
                self._json({"error": "Unknown operation"}, 400)
                return
            CFG["notes_dirs"] = dirs
            pf.save_config(CFG)
            self._json({"dirs": dirs,
                        "effective": [str(x) for x in ls.notes_dirs(CFG)]})

        elif self.path == "/api/organize":
            # Recognize PDFs downloaded by hand into the "Downloads" folder, rename them, file them into the library.
            # dry_run=True (the default) only produces a plan, touches no files.
            folder = (body.get("dir") or "").strip()
            dry = body.get("dry_run", True)
            if not isinstance(dry, bool): # Accept strings like "false"/"0" too
                dry = str(dry).strip().lower() not in ("false", "0", "no", "off")
            try:
                limit = max(1, min(int(body.get("limit") or 200), 1000))
            except (TypeError, ValueError):
                limit = 200
            d = Path(folder) if folder else og.guess_downloads()
            with STATE["lock"]:
                cur = list(STATE["results"])
            # A download job is writing PDFs into the library; organizing now would collide on names, even on the file being written
            busy = bool(STATE.get("job") and STATE["job"].get("running"))
            if busy:
                self._json({"error": "A download job is running; wait for it to finish before organizing (both write files into the library and would collide)"}, 409)
                return
            if not ORG_LOCK.acquire(blocking=False):
                self._json({"error": "Organizing is in progress; wait for this round to finish (do not organize from two windows at once)"}, 409)
                return
            try:
                out = og.scan(d, CFG, cur, dry_run=dry, limit=limit)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            finally:
                ORG_LOCK.release()
            out["dir"] = str(d)
            self._json(out)

        elif self.path == "/api/cites":
            # Citation expansion: use the checked rows as seeds, pull forward/backward citations from Semantic Schola
            try:
                idx = [int(i) for i in (body.get("indices") or [])]
            except (TypeError, ValueError):
                self._json({"error": "Invalid item ID"}, 400)
                return
            if not idx:
                self._json({"error": "Tick a few items as seeds first (only those with a DOI will work)"}, 400)
                return
            direction = body.get("direction") or "both"
            if direction not in ("both", "citations", "references"):
                direction = "both"
            try:
                limit = max(10, min(int(body.get("limit") or 200), 500))
            except (TypeError, ValueError):
                limit = 200
            with STATE["lock"]:
                recs = list(STATE["results"])
            seeds = [recs[i] for i in idx if 0 <= i < len(recs)]
            if not seeds:
                self._json({"error": "These items are no longer in the results; run the search again"}, 400)
                return
            try:
                ext, stat = ct.expand(seeds, CFG, direction=direction, limit=limit)
            except ct.CitesError as e:
                self._json({"error": str(e), "rate_limited": True}, 429)
                return
            if not ext:
                self._json({"error": "These items have no usable citation records in Semantic Scholar"
                                     "(they may be too recent, or the DOI is not indexed)", "stats": stat}, 400)
                return
            with STATE["lock"]:
                STATE["prev"] = list(STATE["results"])
                STATE["results"] = ext
            self._json({"n": len(ext), "stats": stat, "results": slim(ext)})

        elif self.path == "/api/back":
            # Go back from citation expansion results to keyword results
            with STATE["lock"]:
                prev = STATE["prev"]
                if prev:
                    STATE["results"] = prev
                    STATE["prev"] = None
            if not prev:
                self._json({"error": "No previous batch of results to go back to"}, 400)
                return
            self._json({"n": len(prev), "results": slim(prev)})

        elif self.path == "/api/fetch":
            # Can't start a download while organizing: both write same-name PDFs into the library and would collide
            if ORG_LOCK.locked():
                self._json({"error": "Downloaded files are being organized; wait for that to finish before starting a download (both write into the library and would collide)"}, 409)
                return
            idx = [int(i) for i in (body.get("indices") or [])]
            if not idx:
                self._json({"error": "No items selected"}, 400)
                return
            cap = int(CFG.get("max_downloads_per_run") or 200)
            if len(idx) > cap:
                self._json({"error": f"The selected {len(idx)} papers, exceeding the per-run limit of {cap} papers."
                                     f"Download in batches, or change max_downloads_per_run in config.json and restart."}, 400)
                return
            out_dir = Path(body.get("out_dir") or pf.library_dir(CFG))
            # The check and the claim must be in the same lock: otherwise a double click (two requests at once) both pass the check,
            # and two threads write the same batch of files into the same folde
            with STATE["lock"]:
                busy = bool(STATE.get("job") and STATE["job"].get("running"))
                if not busy:
                    STATE["job"] = {"running": True, "total": len(idx), "done": 0,
                                    "ok": 0, "preview": 0, "skip": 0, "miss": 0,
                                    "fail": 0, "current": "Preparing"}
                    STATE["log"] = []
            if busy:
                self._json({"error": "A job is already running; wait for it to finish."}, 409)
                return
            threading.Thread(target=run_fetch, args=(idx, out_dir), daemon=True).start()
            self._json({"started": len(idx), "out_dir": str(out_dir)})
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="Open the browser automatically on startup")
    ap.add_argument("--config", type=Path, help="Config file path")
    a = ap.parse_args()
    global CFG, SESS
    CFG = pf.load_config(a.config)
    zo.set_config(CFG)          # The Zotero module no longer reads config itself, use this one
    SESS = pf.PoliteSession(CFG)
    if CFG.get("_cfg_broken"):
        print(f"⚠ Could not read the config file (backed up as *.broken.json):{CFG['_cfg_broken']}\n"
              f"   Starting with defaults this time; the key must be re-entered.")
    url = f"http://127.0.0.1:{a.port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"paperfetch UI → {url}")
    if not CFG.get("email"):
        print("⚠ email in config.json is not set; OA full-text links cannot be resolved.")
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
