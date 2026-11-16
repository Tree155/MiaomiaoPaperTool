# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2 — PubMed supplement (pubmed)

Author: Ni, Yishu & Big Fat Fish (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For study and exchange only; commercial use is prohibited.

Why this module exists:
  OpenAlex publication types (type) are machine-guessed and its review labels are extremely unreliable — on the same topic,
  OpenAlex flagged only 3 as review while PubMed flagged 9. PubMed PublicationType
  is reported by the journal itself and manually checked by NLM, so it is far more accurate, and it comes with MeSH terms.

What it does:
  · Trade DOIs for PMIDs → fetch PublicationTypeList and MeshHeadingList
  · Review / Systematic Review / Meta-Analysis **override** the OpenAlex article type
    (this is the only reason this module exists)
  · Other types are filled in only when OpenAlex gave nothing or said article, and never override in the other direction
  · MeSH terms are stored and shown as a small line in the UI, for a quick "is this the direction I want" check

Interface (E-utilities, official NCBI, free):
  1. esearch?db=pubmed&term=10.xxx[DOI] OR 10.yyy[DOI]  → a batch of PMIDs
  2. efetch?db=pubmed&id=<comma-separated PMIDs>&retmode=xml   → details + DOI reverse lookup
  Two requests handle dozens of records, so there is no need for one request per record.

Trade-offs:
  · Match by DOI only. Items without a DOI are skipped — guessing PubMed records from titles easily mismatches,
    so better to leave a record unfilled than to fill it wrongly.
  · Fail silently throughout. A PubMed outage must not affect search and download.
  · Results land in .cache_pubmed.json and expire after six months.
"""

from __future__ import annotations

import json
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

EU = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MiaomiaoTool/1.4.2)"}

CACHE_TTL = 180 * 86400
BATCH = 60                     # How many DOIs per esearch (more and the URL hits 8KB)
MAX_ENRICH = 200               # Only enrich the first N -- querying all 500 takes 40 s and people only read the first hundred or two
TIMEOUT = 25

# PubMed PublicationType string -> this tool's work_type
# Order matters: one record is often both "Journal Article" and "Review"; once Review is hit, stop
PT_MAP = {
    "review": "review",
    "systematic review": "review",
    "meta-analysis": "review",
    "scoping review": "review",
    "preprint": "preprint",
    "dissertation": "dissertation",
    "thesis": "dissertation",
    "retracted publication": "withdrawn",
    "retraction of publication": "erratum",
    "published erratum": "erratum",
    "editorial": "editorial",
    "comment": "editorial",
    "letter": "letter",
    "news": "editorial",
    "book chapter": "book-chapter",
    "clinical trial": "article",
    "randomized controlled trial": "article",
    "journal article": "article",
}
# These types have the highest priority and can override OpenAlex's call
STRONG = ("review", "preprint", "dissertation", "withdrawn", "book-chapter")

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
class Cache:
    """DOI → {type, mesh, pmid, t}"""

    def __init__(self, base: Path):
        self.path = Path(base) / ".cache_pubmed.json"
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.d = d if isinstance(d, dict) else {}
        except Exception:
            self.d = {}
        self._dirty = False

    def get(self, doi: str) -> dict | None:
        item = self.d.get((doi or "").strip().lower())
        if not item:
            return None
        if time.time() - item.get("t", 0) > CACHE_TTL:
            return None
        return item

    def put(self, doi: str, info: dict) -> None:
        with _lock:
            self.d[(doi or "").strip().lower()] = {**info, "t": int(time.time())}
            self._dirty = True

    def save(self) -> None:
        with _lock:
            if not self._dirty:
                return
            try:
                txt = json.dumps(self.d, ensure_ascii=False)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(txt, encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
def _get(path: str, params: dict, api_key: str = "", tries: int = 2):
    if api_key:
        params = {**params, "api_key": api_key}
    for i in range(tries):
        try:
            r = requests.get(f"{EU}/{path}", params=params, timeout=TIMEOUT, headers=UA)
            if r.status_code == 429: # Rate limited, wait and come back
                time.sleep(1.2 * (i + 1))
                continue
            if r.status_code != 200:
                return None
            return r
        except Exception:
            time.sleep(0.6 * (i + 1))
    return None


def _dois_to_pmids(dois: list[str], api_key: str = "") -> list[str]:
    """Turn a batch of DOIs into PMIDs with a single esearch. Returns a de-duplicated PMID list."""
    if not dois:
        return []
    term = " OR ".join(f"{d}[DOI]" for d in dois)
    r = _get("esearch.fcgi", {"db": "pubmed", "term": term, "retmode": "json",
                              "retmax": len(dois) + 10}, api_key)
    if not r:
        return []
    try:
        return list(((r.json() or {}).get("esearchresult") or {}).get("idlist") or [])
    except Exception:
        return []


def _efetch(pmids: list[str], api_key: str = "") -> dict[str, dict]:
    """
    Trade a PMID list for details. Returns {doi_lower: {type, mesh, pmid}}.
    DOIs are looked up back from PubmedData/ArticleIdList, so there is no guessing which PMID maps to which DOI.
    """
    if not pmids:
        return {}
    r = _get("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                             "retmode": "xml", "rettype": "abstract"}, api_key)
    if not r:
        return {}
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return {}

    out: dict[str, dict] = {}
    for art in root.iter("PubmedArticle"):
        pmid_el = art.find(".//MedlineCitation/PMID")
        pmid = (pmid_el.text or "").strip() if pmid_el is not None else ""

        # Work type: Review > other > Journal Article
        types = [((el.text or "").strip().lower())
                 for el in art.findall(".//PublicationTypeList/PublicationType")]
        work_type = ""
        for t in types:
            mapped = PT_MAP.get(t)
            if mapped in STRONG: # A strong type settles it
                work_type = mapped
                break
        if not work_type:
            for t in types:
                mapped = PT_MAP.get(t)
                if mapped and mapped != "article":
                    work_type = mapped
                    break
        if not work_type and types:
            work_type = "article"             # Journal Article alone is also explicit

        mesh = []
        for el in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName"):
            s = (el.text or "").strip()
            if s and s not in mesh:
                mesh.append(s)

        doi = ""
        for el in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if (el.get("IdType") or "").lower() == "doi":
                doi = (el.text or "").strip().lower()
                break
        if not doi:
            for el in art.findall(".//ArticleIdList/ArticleId"):
                if (el.get("IdType") or "").lower() == "doi":
                    doi = (el.text or "").strip().lower()
                    break
        if not doi:
            continue
        out[doi] = {"type": work_type, "mesh": mesh[:12], "pmid": pmid}
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def enrich(recs: list[dict], cache: Cache, api_key: str = "",
           max_items: int = MAX_ENRICH) -> dict:
    """
    Fill pubmed_type / mesh / pmid into recs in place and return the counts.
    Anything already cached is used as is (no cost); only records not yet looked up trigger a request;
    **only the first max_items records are looked up** — looking up all 500 search results takes 40 seconds, and the user rarely scrolls that far.
    The overflow is reported honestly in stats["skipped_over_limit"].
    """
    stats = {"asked": 0, "hit": 0, "updated": 0, "skipped_no_doi": 0,
             "skipped_over_limit": 0, "req": 0}
    uniq: dict[str, list[dict]] = {}
    for i, r in enumerate(recs):
        doi = (r.get("doi") or "").strip().lower()
        if not doi:
            stats["skipped_no_doi"] += 1
            continue
        cached = cache.get(doi)
        if cached:
            _apply(r, cached)
            stats["hit"] += 1
            continue
        if i >= max_items:
            stats["skipped_over_limit"] += 1
            continue
        uniq.setdefault(doi, []).append(r)

    dois = list(uniq.keys())
    stats["asked"] = len(dois)
    for i in range(0, len(dois), BATCH):
        chunk = dois[i:i + BATCH]
        pmids = _dois_to_pmids(chunk, api_key)
        stats["req"] += 1
        if not pmids:
            continue            # Skip when not found, **don't cache failures**, retry next time
        detail = _efetch(pmids, api_key)
        stats["req"] += 1
        for doi in chunk:
            info = detail.get(doi)
            if info:
                cache.put(doi, info)
                for r in uniq[doi]:
                    _apply(r, info)
                stats["updated"] += 1
    cache.save()
    return stats


def _apply(rec: dict, info: dict) -> None:
    """Merge the PubMed results into the records. The type is overridden only when the new one is "stronger"."""
    if not info:
        return
    pt = (info.get("type") or "").strip()
    cur = (rec.get("work_type") or "").strip()
    if pt == "review":
        # Review overrides everything -- that's why this module exists.
        # But only count as "changed" when it wasn't a review before: ones OpenAlex already got right
        # must not get type_from either, or the front end reports "N are actually reviews (OpenAlex got it wrong)"
        # while the user searched under the "review" type, where every row already carries the review tag.
        if cur != "review":
            rec["work_type"] = "review"
            rec["type_from"] = "pubmed"
    elif pt and pt in STRONG and cur in ("", "article"):
        rec["work_type"] = pt
        rec["type_from"] = "pubmed"
    elif pt == "article" and not cur:
        rec["work_type"] = "article"
        rec["type_from"] = "pubmed"
    mesh = info.get("mesh") or []
    if mesh:
        rec["mesh"] = mesh
    if info.get("pmid"):
        rec["pmid"] = info["pmid"]
