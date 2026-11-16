#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2 — In-Library Search (localSearch)

What to index and what not to index is the core decision of this feature:

  Indexed:
    · **Title + abstract** from library XML (under Elsevier TDM terms an abstract counts as bibliographic metadata and may be retained)
    · **Notes** you wrote yourself (.md / .txt, your copyright)
    · Metadata + abstracts of the **previous search results** (bibliographic data returned by the API)
    · Full text only for XML that the manifest marks as fetched through an **open access channel**

  Not indexed:
    · **Full text** of subscription journals (Elsevier / Wiley / ACS). TDM terms forbid "create any derivative work
      based on the corpus" and "retain the corpus"; the abstract level is the safe zone.
    · Never extract full text from PDFs (pypdf output is poor anyway, and the gain does not justify the risk).

Search uses SQLite FTS5 BM25 ranking and downloads no models:
Chinese is split into single characters, so a term like "pyrazine" becomes a character-level phrase for exact matching, and paperfetch.ZH_TERMS also expands Chinese terms into English and ORs them in.
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
import time
from pathlib import Path

import paperfetch as pf

DB_NAME = ".search.db"
MAX_BODY = 120_000          # Full-text index cap per paper (chars); enough for notes, a guard against nonsense
CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5(k UNINDEXED, t, b, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS doc(
  k TEXT PRIMARY KEY, kind TEXT, title TEXT, journal TEXT, year TEXT,
  path TEXT, doi TEXT, body TEXT, src TEXT);
CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value TEXT);
"""


def db_file() -> Path:
    return pf.app_dir() / DB_NAME


# --------------------------------------------------------------------------- #
# Tokenize
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> str:
    """English is split by word; Chinese is split character by character and joined with spaces."""
    t = (text or "").lower()
    t = re.sub(r"<[^>]{1,400}>", " ", t)
    t = re.sub(rf"([{CJK}])", r" \1 ", t)
    t = re.sub(rf"[^0-9a-z{CJK}]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


EN_STOP = set("""a an the and or not of in on at to for with by from as is are was were be been
this that these those it its we our you your i me my do does did how what which who when where why
can could should would may might will than then there their they he she his her but if so such also
more most very into about between during after before under over up down out no yes vs via using use
used study studies result results showed show found using based two three one""".split())
CJK_STOP = set("的了在和与及或对于是有为以从到把被让给而但也都很更最不没这那我你他她它之其及著者")


def build_query(q: str) -> tuple[str, list[str]]:
    """
    Turn user input into an FTS5 MATCH expression.
    · First let paperfetch.zh_to_en map Chinese terms to English (190-entry domain glossary plus a translation fallback)
    · Match English words by prefix (pyrazine* hits pyrazines, aldehyde* hits aldehydes)
    · For Chinese terms missing from the glossary (such as "pyrazine"), fall back to a character-level phrase that pins adjacent characters together
    · Drop function words in both languages; otherwise a word like "and" pulls in every English abstract and wrecks the ranking
    """
    q = (q or "").strip()
    if not q:
        return "", []
    try:
        en, _left = pf.zh_to_en(q)          # Glossary substitution + translation of leftover fragments
    except Exception:
        en = q

    terms: list[str] = []

    def add(t: str) -> None:
        if t and t not in terms:
            terms.append(t)

    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", en):
        lw = w.lower()
        if len(lw) < 3 or lw in EN_STOP:
            continue
        add('"' + lw + '"*')
    for frag in re.findall(rf"[{CJK}]+", en):
        keep = "".join(ch for ch in frag if ch not in CJK_STOP)
        if not keep:
            continue
        add('"' + " ".join(keep) + '"')
    # Keep the Chinese in the source too: the glossary turns the Chinese term into pyrazine,
    # but your own Chinese notes only hold the Chinese term, so dropping it loses the match
    for frag in re.findall(rf"[{CJK}]+", q):
        keep = "".join(ch for ch in frag if ch not in CJK_STOP)
        if len(keep) >= 2:
            add('"' + " ".join(keep) + '"')
    return " OR ".join(terms), terms


# --------------------------------------------------------------------------- #
# Build index
# --------------------------------------------------------------------------- #
def _read_text(path: Path, limit: int) -> str:
    try:
        if path.name.lower().endswith(".gz"):
            with gzip.open(path, "rb") as f:
                raw = f.read(limit * 6)
        else:
            raw = path.read_bytes()[: limit * 6]
    except (OSError, EOFError):
        return ""
    return raw.decode("utf-8", "ignore")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]{1,400}>", " ", s or "")
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#x2013;", "–").replace("&#x2019;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def _jats(xml: str) -> tuple[str, str, str]:
    """Extract (title, abstract, body) from JATS. Return empty when unavailable."""
    def grab(tag: str, limit: int = MAX_BODY) -> str:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xml, re.S | re.I)
        return _strip_tags(m.group(1))[:limit] if m else ""
    title = grab("article-title", 600)
    abs_ = grab("abstract", 4000)
    body = grab("body")
    return title, abs_, body


def _manifest_via(lib: Path) -> dict[str, str]:
    """Which route each paper in _manifest.csv came through — used to decide whether the body text may be indexed."""
    out: dict[str, str] = {}
    p = lib / "_manifest.csv"
    if not p.is_file():
        return out
    try:
        import csv
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                doi = (row.get("doi") or "").strip().lower()
                if doi:
                    out[doi] = (row.get("via") or "").strip().lower()
    except Exception:
        pass
    return out


def build(cfg: dict) -> dict:
    """Rebuild the index. Return statistics."""
    t0 = time.time()
    lib = pf.library_dir(cfg)
    via_map = _manifest_via(lib)
    docs: list[dict] = []
    skipped_pdf = skipped_sub = 0

    # 1) Library: XML for title+abstract; fetch full text only for ones the manifest shows came via an OA route
    try:
        scan = pf.scan_library(cfg)
        items = scan.get("items") or []
    except Exception:
        items = []
    for it in items:
        path = Path(it.get("file") or "")
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith((".xml", ".xml.gz")):
            xml = _read_text(path, 400_000)
            if not xml:
                continue
            title, abs_, body = _jats(xml)
            doi = (it.get("doi") or "").lower()
            via = via_map.get(doi, "")
            oa = bool(via) and "elsevier" not in via
            text = " ".join(x for x in (title, abs_, body if oa else "") if x)
            if not text.strip():
                continue
            if not oa and body:
                skipped_sub += 1
            docs.append({"k": "doi:" + (doi or path.name), "kind": "paper",
                         "title": it.get("title") or title or path.name,
                         "journal": "", "year": "", "path": str(path),
                         "doi": it.get("doi") or "", "body": text,
                         "src": "XML abstract + body (OA)" if oa else "XML abstract"})
        elif name.endswith(".pdf"):
            skipped_pdf += 1          # No text extraction from PDF (compliance + extraction quality)

    # 2) Notes: .md / .txt
    for d in notes_dirs(cfg):
        try:
            files = [p for p in d.rglob("*") if p.is_file()
                     and p.suffix.lower() in (".md", ".txt", ".markdown")]
        except OSError:
            continue
        for p in files[:2000]:
            try:
                body = p.read_text(encoding="utf-8", errors="ignore")[:MAX_BODY]
            except OSError:
                continue
            if not body.strip():
                continue
            title = p.stem
            m = re.search(r"^#\s+(.+)$", body, re.M)
            if m:
                title = m.group(1).strip()[:120]
            docs.append({"k": "note:" + str(p), "kind": "note", "title": title,
                         "journal": "", "year": "", "path": str(p), "doi": "",
                         "body": body, "src": "Notes"})

    # 3) Last search results (metadata + abstract)
    lr = pf.app_dir() / ".last_results.json"
    if lr.is_file():
        try:
            recs = json.loads(lr.read_text(encoding="utf-8"))
        except Exception:
            recs = []
        for r in recs if isinstance(recs, list) else []:
            body = " ".join(x for x in (r.get("title"), r.get("abstract")) if x)
            if not body.strip():
                continue
            docs.append({"k": "rec:" + ((r.get("doi") or "").lower() or str(id(r))),
                         "kind": "record", "title": r.get("title") or "",
                         "journal": r.get("journal") or "", "year": str(r.get("year") or ""),
                         "path": "", "doi": r.get("doi") or "", "body": body,
                         "src": "search-results"})

    # Write index
    dbp = db_file()
    tmp = dbp.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.executescript(SCHEMA)
        for d in docs:
            db.execute("INSERT INTO ft(k,t,b) VALUES (?,?,?)",
                       (d["k"], tokenize(d["title"]), tokenize(d["body"])))
            db.execute("INSERT OR REPLACE INTO doc(k,kind,title,journal,year,path,doi,body,src)"
                       " VALUES (?,?,?,?,?,?,?,?,?)",
                       (d["k"], d["kind"], d["title"], d["journal"], d["year"],
                        d["path"], d["doi"], d["body"][:MAX_BODY], d["src"]))
        stats = {"docs": len(docs),
                 "papers": sum(1 for d in docs if d["kind"] == "paper"),
                 "notes": sum(1 for d in docs if d["kind"] == "note"),
                 "records": sum(1 for d in docs if d["kind"] == "record"),
                 "skipped_pdf": skipped_pdf,
                 "skipped_sub_body": skipped_sub,
                 "built_at": int(time.time()),
                 "secs": round(time.time() - t0, 1)}
        for k, v in stats.items():
            db.execute("INSERT OR REPLACE INTO meta(name,value) VALUES (?,?)", (k, str(v)))
        db.commit()
    finally:
        db.close()
    try:
        tmp.replace(dbp)
    except OSError:
        # On Windows a search may be reading the old index, so the replace fails; wait and retry once
        time.sleep(0.3)
        try:
            tmp.replace(dbp)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    return stats


def status() -> dict:
    dbp = db_file()
    if not dbp.is_file():
        return {"built": False, "docs": 0}
    try:
        db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        rows = dict(db.execute("SELECT name,value FROM meta").fetchall())
        db.close()
    except Exception:
        return {"built": False, "docs": 0}
    out = {"built": True, "db_kb": round(dbp.stat().st_size / 1024)}
    for k, v in rows.items():
        out[k] = int(v) if str(v).lstrip("-").isdigit() else v
    return out


def notes_dirs(cfg: dict) -> list[Path]:
    """Notes directories: notes_dirs from config plus "notes" under the program directory."""
    out: list[Path] = []
    for x in (cfg.get("notes_dirs") or []):
        p = Path(x)
        if p.is_dir():
            out.append(p)
    d = pf.app_dir() / "Notes"
    if d.is_dir() and d not in out:
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _snippet(body: str, terms: list[str], width: int = 150) -> str:
    """Build the snippet yourself: locate the hit in the source text and cut a few characters before and after.
    Escape HTML first, then add <b> — notes are local files, and their angle brackets must never reach the UI as live tags."""
    from html import escape as _e

    plain = re.sub(r"\s+", " ", body or "")
    if not plain:
        return ""
    low = plain.lower()
    pos, hit = -1, ""
    for t in terms:
        w = t.replace('"', "").rstrip("*").replace(" ", "")   # "pyrazine"* → pyrazine
        if not w:
            continue
        i = low.find(w)
        if i >= 0 and (pos < 0 or i < pos):
            pos, hit = i, w
    if pos < 0:
        seg = plain[:width]
        return _e(seg) + ("…" if len(plain) > width else "")
    start = max(0, pos - width // 3)
    seg = plain[start:start + width]
    if hit:
        i = seg.lower().find(hit)
        if i >= 0:
            seg = seg[:i] + "\x00" + seg[i:i + len(hit)] + "\x01" + seg[i + len(hit):]
    out = _e(seg).replace("\x00", "<b>").replace("\x01", "</b>")
    return ("…" if start else "") + out + ("…" if start + width < len(plain) else "")


def search(q: str, k: int = 30) -> dict:
    expr, terms = build_query(q)
    if not expr:
        return {"n": 0, "results": [], "expr": "", "error": "Type something to search"}
    dbp = db_file()
    if not dbp.is_file():
        return {"n": 0, "results": [], "expr": expr, "error": "The index is not built yet; click \"Rebuild index\" first"}
    db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT k, bm25(ft) AS s FROM ft WHERE ft MATCH ? ORDER BY s LIMIT ?",
            (expr, max(1, min(k, 200)))).fetchall()
        top = max((-s for _k, s in rows), default=0.0) or 1.0
        out = []
        for key, score in rows:
            d = db.execute("SELECT kind,title,journal,year,path,doi,body,src FROM doc WHERE k=?",
                           (key,)).fetchone()
            if not d:
                continue
            s = -score
            out.append({"kind": d[0], "title": d[1], "journal": d[2], "year": d[3],
                        "path": d[4], "doi": d[5],
                        "snippet": _snippet(d[6], terms), "src": d[7],
                        "score": round(s, 4), "rel": round(min(1.0, s / top), 3)})
        return {"n": len(out), "results": out, "expr": expr, "terms": terms}
    finally:
        db.close()
