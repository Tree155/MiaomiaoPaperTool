#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2 —— 库内检索（localSearch）

索引什么、不索引什么，是这条功能的核心决定：

  索引：
    · 文献库 XML 里的 **标题 + 摘要**（摘要在 Elsevier 的 TDM 条款里属于书目元数据，可留存）
    · 你自己写的 **笔记**（.md / .txt，你的版权）
    · **上次检索结果**的元数据 + 摘要（API 返回的书目数据）
    · 只有 manifest 记录显示是走 **开放获取渠道**下来的 XML，才额外索引正文

  不索引：
    · 订阅刊（Elsevier / Wiley / ACS）的 **正文**。TDM 条款禁止 "create any derivative work
      based on the corpus" 与 "retain the corpus"，摘要级才是安全区。
    · PDF 一律不抽正文（pypdf 抽出来的质量也差，收益不抵风险）。

检索用 SQLite FTS5 的 BM25 排序，不下载任何模型：
中文按单字切、"吡嗪" 会变成短语 "吡 嗪" 精确匹配，另用 paperfetch.ZH_TERMS 把中文术语扩展成英文一起 OR。
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
MAX_BODY = 120_000          # 单篇正文入库上限（字符），笔记够用，防呆
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
# 分词
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> str:
    """英文按词切、中文逐字切开并用空格分隔。"""
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
    用户输入 → FTS5 MATCH 表达式。
    · 先让 paperfetch.zh_to_en 把中文术语换成英文（190 条领域词表 + 翻译兜底）
    · 英文词用前缀匹配（pyrazine* 能命中 pyrazines、aldehyde* 能命中 aldehydes）
    · 表里没有的中文（比如"吡嗪"）按字级短语匹配："吡 嗪"，正好卡相邻两字
    · 中英虚词都丢掉，否则 "and" 这种词会把每条英文摘要都拉进来，排序全乱
    """
    q = (q or "").strip()
    if not q:
        return "", []
    try:
        en, _left = pf.zh_to_en(q)          # 术语表替换 + 残留片段翻译
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
    # 原文里的中文也要留着：术语表会把「吡嗪」换成 pyrazine，
    # 但你自己写的中文笔记里只有「吡嗪」，不留就漏了
    for frag in re.findall(rf"[{CJK}]+", q):
        keep = "".join(ch for ch in frag if ch not in CJK_STOP)
        if len(keep) >= 2:
            add('"' + " ".join(keep) + '"')
    return " OR ".join(terms), terms


# --------------------------------------------------------------------------- #
# 建索引
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
    """从 JATS 里取 (标题, 摘要, 正文)。取不到就给空。"""
    def grab(tag: str, limit: int = MAX_BODY) -> str:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xml, re.S | re.I)
        return _strip_tags(m.group(1))[:limit] if m else ""
    title = grab("article-title", 600)
    abs_ = grab("abstract", 4000)
    body = grab("body")
    return title, abs_, body


def _manifest_via(lib: Path) -> dict[str, str]:
    """_manifest.csv 里每篇是走哪条路下来的 —— 用来判断能不能索引正文。"""
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
    """重建索引。返回统计。"""
    t0 = time.time()
    lib = pf.library_dir(cfg)
    via_map = _manifest_via(lib)
    docs: list[dict] = []
    skipped_pdf = skipped_sub = 0

    # 1) 文献库：XML 取标题+摘要；manifest 显示走 OA 渠道的才额外取正文
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
                         "src": "XML 摘要+正文(OA)" if oa else "XML 摘要"})
        elif name.endswith(".pdf"):
            skipped_pdf += 1          # PDF 正文不抽（合规 + 抽取质量）

    # 2) 笔记：.md / .txt
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
                         "body": body, "src": "笔记"})

    # 3) 上次检索结果（元数据 + 摘要）
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
                         "src": "检索结果"})

    # 写库
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
        # Windows 上如果正好有个搜索请求在读旧库，替换会失败；等一下再试一次
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
    """笔记目录：config 的 notes_dirs + 程序目录下的「笔记」。"""
    out: list[Path] = []
    for x in (cfg.get("notes_dirs") or []):
        p = Path(x)
        if p.is_dir():
            out.append(p)
    d = pf.app_dir() / "笔记"
    if d.is_dir() and d not in out:
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def _snippet(body: str, terms: list[str], width: int = 150) -> str:
    """自己拼片段：在原文里找命中词的位置，截前后若干字。
    先转义 HTML 再加 <b> —— 笔记是本地文件，不该让里面的尖括号跑到界面上当标签执行。"""
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
        return {"n": 0, "results": [], "expr": "", "error": "输入点什么再搜"}
    dbp = db_file()
    if not dbp.is_file():
        return {"n": 0, "results": [], "expr": expr, "error": "索引还没建，先点「重建索引」"}
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
