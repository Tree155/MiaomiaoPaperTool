# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2 — Citation Expansion (cites)

Author: Ni, Yishu & Da Fei Yu (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For learning and exchange only; commercial use prohibited.

Why this module exists:
  Keyword search has one gap you cannot avoid — the same thing may go by another name.
  Search "lipid oxidation AND aroma" and you miss papers that only say "lipid–flavor interaction".
  Swapping keywords helps a little, but never completely.

  A citation network saves you from guessing words: start from a few core papers,
    · Forward: who cited these papers (follow-up work)
    · Backward: whom these papers cited (the earlier foundation)
  Far more reliable than keywords, and the main source of "new" papers.

API: Semantic Scholar Graph API (free, official)
  GET /graph/v1/paper/DOI:<doi>/citations?fields=...&limit=100
  GET /graph/v1/paper/DOI:<doi>/references?fields=...&limit=100
  The shared pool without a key allows 100 requests / 5 minutes and returns 429 easily — so run serially with backoff retries,
  and cap the number of seeds (6 by default); more is not better.

Sorting: **co-seed count first** (a paper cited by or citing several seeds at once sits in the shared neighborhood of those papers,
usually the most relevant ones), then by S2's own citation count.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

S2 = "https://api.semanticscholar.org/graph/v1"
FIELDS = ("title,year,venue,externalIds,abstract,citationCount,"
          "isOpenAccess,openAccessPdf,authors,publicationTypes")
TIMEOUT = 25
MAX_SEEDS = 6                 # At most this many papers as seeds at once (2 requests each)
PER_SEED = 100                # Forward (who cited it): how many records to take
PER_SEED_REF = 60             # Backward (what it cites): how many records -- a review can cite 200 papers, don't pull them all
GAP = 1.15                    # Request interval (seconds); don't rush when there is no key


class CitesError(RuntimeError):
    """Raised on rate limiting / connection failure; the message is shown to the user as-is."""


def _get(path: str, params: dict, key: str = "") -> dict | None:
    q = urllib.parse.urlencode(params)
    url = f"{S2}/{path}?{q}"
    hdr = {"User-Agent": "Mozilla/5.0 (compatible; MiaomiaoTool/1.4.2)"}
    if key:
        hdr["x-api-key"] = key
    req = urllib.request.Request(url, headers=hdr)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = ""
    for attempt in range(3):
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                  # Not in S2, just skip it
            if e.code == 429:
                last = "429"
                time.sleep(3.0 * (attempt + 1))     # 3s / 6s / 9s
                continue
            last = f"HTTP {e.code}"
            break
        except Exception as e:
            last = f"{type(e).__name__}"
            time.sleep(1.0 * (attempt + 1))
    if last == "429":
        raise CitesError("Semantic Scholar is rate limiting (free quota: 100 requests / 5 minutes)."
                         "Wait a few minutes and retry, or select fewer seeds at a time.")
    return None


def _doi_of(p: dict) -> str:
    ext = p.get("externalIds") or {}
    return ((ext.get("DOI") or "") if isinstance(ext, dict) else "").strip().lower()


def _to_rec(p: dict, via: str) -> dict:
    """Map an S2 paper object to this project's internal record structure (field names aligned with paperfetch._to_rec)."""
    au = p.get("authors") or []
    first = ((au[0].get("name") or "").strip() if au else "")
    # ! Do not rewrite "Y. Zhang" into "Zhang, Y." here --
    # downstream (download filename, BibTeX citation key) all take split()[-1] as the surname,
    # with a comma form it grabs "Y." and the filename becomes 2024_Y_xxx.pdf.
    # OpenAlex also gives "first last", so keeping it as-is is what matches.
    pts = [str(x).lower() for x in (p.get("publicationTypes") or [])]
    wt = ""
    if any("review" in x or "meta" in x for x in pts):
        wt = "review"
    elif any("journal" in x or "article" in x for x in pts):
        wt = "article"
    oa = p.get("openAccessPdf") or {}
    return {
        "oaid": "", "doi": _doi_of(p), "title": (p.get("title") or "").strip(),
        "journal": (p.get("venue") or "").strip(),
        "year": p.get("year"), "first_author": first,
        "n_authors": len(au),
        "cited_by": p.get("citationCount") or 0,
        "is_oa": bool(oa.get("url")) or bool(p.get("isOpenAccess")),
        "oa_status": "", "license": "",
        "pdf_url": (oa.get("url") or ""),
        "landing": ((f"https://doi.org/{_doi_of(p)}" if _doi_of(p) else "")
                    or (oa.get("url") or "")),
        "abstract": (p.get("abstract") or "")[:1200],
        "work_type": wt,
        "found_via": via,
        "seed_hits": 1,
    }


def expand(seeds: list[dict], cfg: dict | None = None, direction: str = "both",
           limit: int = 200, max_seeds: int = MAX_SEEDS) -> tuple[list[dict], dict]:
    """
    seeds: [{doi, title, ...}] — only entries with a DOI become seeds (DOI is the most stable key for S2)
    direction: "both" / "citations" (forward, who cited it) / "references" (backward, whom it cited)
    Return (records, stats)
    """
    cfg = cfg or {}
    key = (cfg.get("semantic_scholar_key") or "").strip()
    use = [s for s in seeds if (s.get("doi") or "").strip()][:max_seeds]
    stats = {"seeds": len(use), "skipped_no_doi": len(seeds) - len(use),
             "citations": 0, "references": 0, "req": 0, "dup": 0,
             "errors": 0, "truncated": len(seeds) > max_seeds,
             # Some seeds have an empty "what it cites" -- measured: Elsevier / Wiley papers
             # on Semantic Scholar generally have no references (publishers don't open citation data),
             # only fully-OA ones like MDPI do. So the front end tells the truth instead of looking broken.
             "ref_empty_seeds": 0}

    if not use:
        return [], {**stats, "why": "None of the selected entries has a DOI, so citation expansion is not possible"}

    pool: dict[str, dict] = {}
    seed_dois = {(s.get("doi") or "").strip().lower() for s in use}

    def take(items: list[dict], kind: str) -> None:
        for it in items:
            p = it.get("citingPaper") if kind == "citations" else it.get("citedPaper")
            if not p or not (p.get("title") or "").strip():
                continue
            rec = _to_rec(p, kind)
            d = rec["doi"]
            k = d or ("t:" + re.sub(r"\W+", "", rec["title"].lower())[:120])
            if d and d in seed_dois: # Don't let the seeds themselves slip in
                continue
            if k in pool:
                pool[k]["seed_hits"] += 1     # Hit by several seeds at once -> more relevant
                pool[k]["_via"].add(kind)
                stats["dup"] += 1
                continue
            rec["_via"] = {kind}
            pool[k] = rec
            stats[kind] += 1

    for i, s in enumerate(use):
        doi = (s.get("doi") or "").strip()
        path_doi = urllib.parse.quote(doi, safe="/:;()<>._-")
        if direction in ("both", "citations"):
            d = _get(f"paper/DOI:{path_doi}/citations",
                     {"fields": FIELDS, "limit": PER_SEED}, key)
            stats["req"] += 1
            if d:
                take(d.get("data") or [], "citations")
            elif d is None:
                stats["errors"] += 1
            time.sleep(GAP)
        if direction in ("both", "references"):
            d = _get(f"paper/DOI:{path_doi}/references",
                     {"fields": FIELDS, "limit": PER_SEED_REF}, key)
            stats["req"] += 1
            if d is not None:
                items = d.get("data") or []
                if not items:
                    stats["ref_empty_seeds"] += 1
                take(items, "references")
            else:
                stats["errors"] += 1
            time.sleep(GAP)

    recs = list(pool.values())
    for r in recs:
        v = r.pop("_via", set())
        r["found_via"] = "+".join(sorted(v)) if v else "s2"
    # Three-layer sort:
    #   1. co-seed count (linked by several seeds -- on a shared neighbor, most relevant)
    #   2. direction: forward (later work citing this paper) beats backward (background listed by a review)
    #   3. S2 citation count
    recs.sort(key=lambda r: (
        -(r.get("seed_hits") or 0),
        0 if "citations" in (r.get("found_via") or "") else 1,
        -(r.get("cited_by") or 0),
    ))
    stats["total"] = len(recs)
    return recs[:limit], stats
