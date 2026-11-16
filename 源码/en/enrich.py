# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2

Author: Ni, Yishu & Da Fei Yu (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For learning and exchange only; commercial use prohibited.

Two "extras" for search results:
  1. Chinese title translation (X-MOL style: a gray Chinese line under the English title)
  2. Journal metrics: impact factor (IF), CAS quartile, JCR quartile — from the easyScholar open API

Both use an on-disk cache:
  · Title translations are keyed by md5, so each paper is translated only once
  · Journal metrics are keyed by journal name and expire after half a year (IF updates yearly, so half a year is enough)

Design trade-offs:
  · Show "—" rather than guess. If a value is unavailable, it stays unavailable; never fill in a number that merely "looks right".
  · Both services are free third parties and may rate-limit; on failure give up silently without blocking the main search and download flow.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ZH_API = "https://api.mymemory.translated.net/get"
ES_API = "https://www.easyscholar.cc/open/getPublicationRank"

# HTTP headers can only be latin-1; absolutely no Chinese here
UA = {"User-Agent": "Mozilla/5.0 (compatible; MiaomiaoTool/1.4.2)"}

ZH_CACHE_TTL = 90 * 86400      # Title translation 90 days
JN_CACHE_TTL = 180 * 86400     # Journal metrics 180 days

# MyMemory: 5000 chars/day anonymous, 50000 chars/day with de=email.
# Keep headroom: at most this many chars per session, then stop (the next run resumes from cache).
ZH_CHAR_BUDGET = 40000

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


class Cache:
    """Two JSON caches, written to disk on demand."""

    def __init__(self, base: Path):
        self.zh_path = Path(base) / ".cache_zh.json"
        self.jn_path = Path(base) / ".cache_journal.json"
        self.zh = _load(self.zh_path)
        self.jn = _load(self.jn_path)

    # -- Title ------------------------------------------------------------- #
    def get_zh(self, title: str) -> str | None:
        k = _md5(title)
        item = self.zh.get(k)
        if not item:
            return None
        if time.time() - item.get("t", 0) > ZH_CACHE_TTL:
            return None
        return item.get("v") or None

    def put_zh(self, title: str, value: str) -> None:
        with _lock:
            self.zh[_md5(title)] = {"v": value, "t": int(time.time())}

    # -- Abstract (same zh cache, key prefixed to tell them apart) --------- #
    def get_abs(self, text: str) -> str | None:
        item = self.zh.get("abs:" + _md5(text))
        if not item:
            return None
        if time.time() - item.get("t", 0) > ZH_CACHE_TTL:
            return None
        return item.get("v") or None

    def put_abs(self, text: str, value: str) -> None:
        with _lock:
            self.zh["abs:" + _md5(text)] = {"v": value, "t": int(time.time())}

    # -- Journal ----------------------------------------------------------- #
    def get_jn(self, name: str) -> dict | None:
        item = self.jn.get(name.strip().lower())
        if not item:
            return None
        if time.time() - item.get("t", 0) > JN_CACHE_TTL:
            return None
        return {k: v for k, v in item.items() if k != "t"}

    def put_jn(self, name: str, info: dict) -> None:
        with _lock:
            self.jn[name.strip().lower()] = {**info, "t": int(time.time())}

    def flush(self) -> None:
        _save(self.zh_path, self.zh)
        _save(self.jn_path, self.jn)


def _md5(s: str) -> str:
    return hashlib.md5(s.strip().encode("utf-8")).hexdigest()


def cached_extras(titles: list[str], journals: list[str],
                  cache: "Cache | None") -> "tuple[dict, dict]":
    """
    Read the on-disk cache only, **without sending a single request**: title → translation, journal name → metrics.
    Used for the download ledger (_manifest.csv) — anything seen in the UI is already cached,
    so filling it in costs nothing; leave blanks when the cache misses, and never go online just to write the ledger.
    """
    zh: dict[str, str] = {}
    jn: dict[str, dict] = {}
    if cache is None:
        return zh, jn
    for t in titles:
        if not t:
            continue
        v = cache.get_zh(t)
        if v:
            zh[t] = v
    for n in journals:
        if not n:
            continue
        v = cache.get_jn(n)
        if v:
            jn[n.strip().lower()] = v
    return zh, jn


# --------------------------------------------------------------------------- #
# Title to Chinese
# --------------------------------------------------------------------------- #
_BAD_ZH = ("MYMEMORY WARNING", "QUERY LENGTH LIMIT", "INVALID LANGUAGE PAIR",
           "PLEASE SELECT TWO", "NO QUERY SPECIFIED")


def _zh_one(title: str, email: str, timeout: int) -> tuple[str | None, str]:
    """Translate one item. Return (translation, failure reason)."""
    try:
        params = {"q": title, "langpair": "en|zh-CN"}
        if email:
            params["de"] = email          # Adding the email raises the daily quota from 5k to 50k chars
        r = requests.get(ZH_API, params=params, timeout=timeout,
                         headers=UA)
        if r.status_code != 200:
            return None, f"http{r.status_code}"
        d = r.json()
        if d.get("quotaFinished"):
            return None, "quota"
        txt = ((d.get("responseData") or {}).get("translatedText") or "").strip()
        txt = html.unescape(txt)          # MyMemory returns entities like &quot; &#39;
        up = txt.upper()
        if not txt:
            return None, "empty"
        if any(b in up for b in _BAD_ZH):
            return None, "quota"
        # The service sometimes echoes the English back (not recognized); don't count that as success
        if txt == title:
            return None, "same"
        return txt, ""
    except Exception as e:
        return None, type(e).__name__


def translate_titles(titles: list[str], email: str = "", cache: Cache | None = None,
                     workers: int = 4) -> tuple[dict, str]:
    """
    Batch title translation. Return ({en_title: zh_title}, note).
    A non-empty note means the run stopped early (quota exhausted / network down); whatever was obtained is still returned.
    """
    out: dict[str, str] = {}
    todo: list[str] = []
    for t in dict.fromkeys(x.strip() for x in titles if x and x.strip()):
        if re.search(r"[\u4e00-\u9fff]", t):
            continue          # Already Chinese, don't echo it back again (the UI would show the same line twice)
        if cache:
            hit = cache.get_zh(t)
            if hit:
                out[t] = hit
                continue
        todo.append(t)
    if not todo:
        return out, ""

    # Char budget: whatever is over is not translated this round, catch up next time
    budget, picked = 0, []
    for t in todo:
        if budget + len(t) > ZH_CHAR_BUDGET:
            break
        picked.append(t)
        budget += len(t)
    note = "" if len(picked) == len(todo) else "Translation quota for this round is used up; the remaining entries are filled in automatically next time"

    stop = threading.Event()

    def work(t: str):
        if stop.is_set():
            return
        val, why = _zh_one(t, email, 15)
        if val:
            out[t] = val
            if cache:
                cache.put_zh(t, val)
        elif why == "quota":
            stop.set()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, picked))
    if stop.is_set():
        note = "The translation service's free quota is used up for today; only the first entries were translated"
    if cache:
        cache.flush()
    return out, note


# --------------------------------------------------------------------------- #
# Abstract to Chinese (on demand: translate when the user opens an abstract, don't burn quota up front)
# --------------------------------------------------------------------------- #
ABS_CHUNK = 460          # A single MyMemory query has a length limit, so split by sentence
_SENT = re.compile(r"(?<=[.!?;])\s+|(?<=。)\s*")


def _split_abs(text: str, limit: int = ABS_CHUNK) -> list[str]:
    out, buf = [], ""
    for sent in [s for s in _SENT.split(text) if s]:
        while len(sent) > limit: # One sentence too long, hard split
            if buf:
                out.append(buf); buf = ""
            out.append(sent[:limit])
            sent = sent[limit:]
        if len(buf) + len(sent) + 1 > limit:
            out.append(buf); buf = sent
        else:
            buf = (buf + " " + sent).strip() if buf else sent
    if buf:
        out.append(buf)
    return out


def translate_abstract(text: str, email: str = "", cache: Cache | None = None) -> tuple[str, str]:
    """Abstract → Chinese. Return (translation, note). If translation fails, return the English as-is; never make things up."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return "", "This entry has no abstract"
    if not re.search(r"[A-Za-z]{3}", text):
        return text, ""                          # Already Chinese
    if cache:
        hit = cache.get_abs(text)
        if hit:
            return hit, ""
    if len(text) > ZH_CHAR_BUDGET:
        text = text[:ZH_CHAR_BUDGET]

    parts, quota, failed = [], False, 0
    for ch in _split_abs(text):
        val, why = _zh_one(ch, email, 25)
        if val:
            parts.append(val)
        elif why == "quota":
            quota = True
            break
        else:
            failed += 1
            parts.append(ch)                     # If it won't translate, keep the English
    out = " ".join(parts).strip()
    note = ""
    if quota:
        note = "Translation quota for today is used up; only the first half was translated, and one click tomorrow fills in the rest automatically"
    elif failed and failed == len(parts):
        note = "The translation service is not responding; read the English for now"
    if out and not quota and not failed and cache:
        cache.put_abs(text, out)
        cache.flush()
    return out, note


# --------------------------------------------------------------------------- #
# Journal metrics (easyScholar)
# --------------------------------------------------------------------------- #
# easyScholar field names (officialRank.all) differ slightly across versions, cover them all
_F_IF = ("SCIIF", "sciif", "Impact factor")
_F_IF5 = ("SCIIF(5)", "sciif5", "5-year impact factor")
_F_JCR = ("SCI", "sci", "JCR quartile")
_F_CAS = ("SCI升级版", "sciUp", "中科院分区升级版", "中科院分区")
_F_CAS_BASE = ("SCI基础版", "sciBase", "中科院分区基础版")
_F_TOP = ("SCI升级版Top", "sciUpTop", "Top期刊")
_F_WARN = ("SCIWARN", "sciwarn", "CAS warning list")


def _pick(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return str(v).strip()
    return ""


def _norm_rank(all_: dict) -> dict:
    cas = _pick(all_, _F_CAS)
    top = _pick(all_, _F_TOP)
    warn = _pick(all_, _F_WARN)
    # Common quartile values: "Q1", "Q1 Top", "Q 1"
    zone = ""
    m = re.search(r"([1-4])\s*区", cas)
    if m:
        zone = m.group(1) + "区"
    if not top and re.search(r"top", cas, re.I):
        top = "Top"
    if top and top.strip() in ("否", "无", "0", "false", "False", "-"):
        top = ""
    elif top:
        top = "Top"          # Whether the raw value is "yes" or "1", always display it as Top
    return {
        "if": _pick(all_, _F_IF),
        "if5": _pick(all_, _F_IF5),
        "jcr": _pick(all_, _F_JCR),
        "cas": zone,
        "cas_raw": cas,
        "cas_base": _pick(all_, _F_CAS_BASE),
        "top": top,
        "warn": warn,
    }


def _es_one(name: str, key: str, timeout: int) -> tuple[dict | None, str]:
    try:
        r = requests.get(ES_API, params={"secretKey": key, "publicationName": name},
                         timeout=timeout, headers=UA)
        if r.status_code != 200:
            return None, f"http{r.status_code}"
        d = r.json()
        code = d.get("code")
        if code != 200:
            msg = str(d.get("msg") or "")
            if "Key" in msg or code in (40002, 40001):
                return None, "key"
            if "次数" in msg or "限制" in msg:
                return None, "quota"
            return None, msg or f"code{code}"
        data = d.get("data") or {}
        all_ = ((data.get("officialRank") or {}).get("all")) or {}
        if not all_:
            return None, "notfound"
        return _norm_rank(all_), ""
    except Exception as e:
        return None, type(e).__name__


def journal_metrics(names: list[str], key: str, cache: Cache | None = None,
                    timeout: int = 12, gap: float = 0.6) -> tuple[dict, str]:
    """
    Batch query journal IF / quartile. Return ({journal: metrics}, note).
    With no key configured, return empty immediately — the UI shows "—".
    gap: interval between requests. easyScholar allows 2 req/s; use 1.6 req/s here to leave headroom.
    """
    if not (key or "").strip():
        return {}, "easyScholar key not configured"

    out: dict[str, dict] = {}
    todo: list[str] = []
    for n in dict.fromkeys(x.strip() for x in names if x and x.strip()):
        if cache:
            hit = cache.get_jn(n)
            if hit is not None:
                out[n] = hit
                continue
        todo.append(n)

    note = ""
    for i, n in enumerate(todo):
        if i:
            time.sleep(gap)
        info, why = _es_one(n, key.strip(), timeout)
        if info is not None:
            out[n] = info
            if cache:
                cache.put_jn(n, info)
        elif why in ("key", "quota"):
            note = ("Invalid easyScholar key" if why == "key"
                    else "easyScholar daily call quota exhausted")
            break
        # notfound / network error: don't cache, retry next time
    if cache:
        cache.flush()
    return out, note


def test_key(key: str, name: str = "Food Chemistry", timeout: int = 15) -> dict:
    """Before saving the key, try one common journal and return the result together with the raw field names, to make troubleshooting easier."""
    if not (key or "").strip():
        return {"ok": False, "msg": "The key is empty"}
    try:
        r = requests.get(ES_API, params={"secretKey": key.strip(), "publicationName": name},
                         timeout=timeout,
                         headers=UA)
        d = r.json()
        if d.get("code") != 200:
            return {"ok": False, "msg": f"{d.get('msg') or 'Query failed'}(code {d.get('code')})"}
        data = d.get("data") or {}
        all_ = ((data.get("officialRank") or {}).get("all")) or {}
        if not all_:
            return {"ok": False, "msg": f"Key is valid, but no grade info found for \"{name}\""}
        raw = {k: all_[k] for k in list(all_.keys())[:14]}
        return {"ok": True, "sample": name, "fields": list(all_.keys()),
                "raw": raw, "parsed": _norm_rank(all_)}
    except Exception as e:
        return {"ok": False, "msg": f"Cannot reach easyScholar:{type(e).__name__}"}
