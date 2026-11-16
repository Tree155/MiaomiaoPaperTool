#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2

Author: Ni, Yishu & Da Fei Yu (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For learning and exchange only; commercial use prohibited.

Compliant literature search and full-text retrieval

Design constraints (enforced in code, not only in comments):
  1. Every full-text route must be a [public API of the rights holder] or an [OA channel (Unpaywall / PMC OA / arXiv)].
     The code hard-rejects scraping page URLs such as ScienceDirect / linkinghub that require subscription authentication.
  2. The User-Agent clearly identifies the tool and a contact email; global rate limiting with Retry-After backoff.
  3. Check robots.txt before any non-API direct link; skip when Disallow applies.
  4. Cap downloads per run so a mistake does not get the institution IP blocked.

See README.md for usage.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import logging
import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

APP_NAME = "Miaomiao Paper Tool"
APP_VERSION = "1.4.2"
VERSION = APP_VERSION          # Technical identifier in the HTTP User-Agent

# pypdf floods warnings when font decoding fails, keep only errors
logging.getLogger("pypdf").setLevel(logging.ERROR)
HERE = Path(__file__).resolve().parent


def app_dir() -> Path:
    """
    Data directory (config.json / pdfs).

    Portability first: once packaged, use the directory holding the exe, so the whole folder can be zipped, shared and used right after unzipping.
    Only when the exe directory is not writable (for example under Program Files) fall back to "Documents\\paperfetch".
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        try:
            probe = exe_dir / ".write_test"
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
            return exe_dir
        except OSError:
            base = Path.home() / "Documents" / "paperfetch"
            base.mkdir(parents=True, exist_ok=True)
            return base
    return HERE
DEFAULT_CONFIG = {
    "_notes": "email is required (institutional address). See User-Guide-EN.pdf for how to apply for elsevier_api_key / elsevier_insttoken",
    "email": "", # Required: Unpaywall / polite pool both need it
    "elsevier_api_key": "", # Optional: apply for free at dev.elsevier.com
    "elsevier_insttoken": "", # Optional: institutional token from your university library
    "library_dir": "", # Library directory; empty = program dir/library
    "extra_sources": [], # Extra directories to scan (Zotero etc.)
    "openalex_api_key": "", # Currently active OpenAlex key (synced by openalex_accounts)
    "openalex_accounts": [], # Multiple accounts: [{label, email, api_key}]
    "openalex_active": 0, # Index of the current account
    "save_format": "xmlgz", # Full text storage: pdf / xml / xmlgz
    "keep_preview_pdf": False, # Whether to also save a first-page image when only page 1 exists (wastes ~245KB/paper)
    "easyscholar_key": "", # Optional: journal IF / CAS quartile (free key at easyscholar.cc)
    "show_zh_title": False, # In search results, show a Chinese translation line under the English title
    "notes_dirs": [], # Note directories to index for in-library search (.md/.txt); empty uses "notes" under the program di
    "zotero_data_dir": "", # Zotero data directory (contains zotero.sqlite); empty = auto-detect
    "pubmed_enrich": True, # After searching, use PubMed to fix work type + fetch MeSH (off saves 3-5 s)
    "pubmed_api_key": "", # Optional: NCBI key (unlimited rate; works without it)
    "semantic_scholar_key": "", # Optional: S2 key (unlimited rate for citation expansion; works without it)
    "rate_limit_rps": 1.0, # Global request rate (requests/second)
    "max_downloads_per_run": 200, # Download cap per run
    "timeout": 30,
}

# Page URLs on these domains are never fetched (subscription auth); full text only via the official Elsevier API
FORBIDDEN_DOMAINS = (
    "sciencedirect.com",
    "linkinghub.elsevier.com",
    "www.sciencedirect.com",
)


def is_forbidden(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    return any(d in host for d in FORBIDDEN_DOMAINS)


# --------------------------------------------------------------------------- #
# Publisher detection -- decides what to tell the user when "can't download"
#
# Why: same no_source, Elsevier means "you didn't set a key", Wiley/ACS/T&F mean "this one has no
# self-service full-text API at all". Without the split we can only say "no OA version" and nobody knows who to ask.
# Kind: api = official full-text API / oa = open access / sub = subscription (no self-service API)
# --------------------------------------------------------------------------- #
PUBLISHERS = {
    "10.1016": ("Elsevier", "api"), # Article Retrieval API, key + insttoken
    "10.3168": ("Elsevier", "api"),
    "10.1006": ("Elsevier", "api"),
    "10.1111": ("Wiley", "sub"),
    "10.1002": ("Wiley", "sub"),
    "10.1046": ("Wiley", "sub"),
    "10.1096": ("Wiley", "sub"),
    "10.1021": ("ACS", "sub"),
    "10.1080": ("T&F", "sub"),
    "10.1081": ("T&F", "sub"),
    "10.1007": ("Springer", "sub"),
    "10.1023": ("Springer", "sub"),
    "10.1038": ("Springer Nature", "sub"),
    "10.1039": ("RSC", "sub"),
    "10.1093": ("Oxford", "sub"),
    "10.1017": ("Cambridge", "sub"),
    "10.1177": ("SAGE", "sub"),
    "10.1128": ("ASM", "sub"),
    "10.4315": ("IAFP", "sub"),
    "10.5740": ("AOAC", "sub"),
    "10.1152": ("APS", "sub"),
    "10.1126": ("Science", "sub"),
    "10.3390": ("MDPI", "oa"),
    "10.3389": ("Frontiers", "oa"),
    "10.1371": ("PLOS", "oa"),
    "10.1186": ("BMC", "oa"),
    "10.1155": ("Hindawi", "oa"),
}

# These have no full-text API an individual can apply for; say so, don't send the user off to wrestle APIs
SUB_HINT = {
    "Wiley": "Wiley subscription journal — full text only on onlinelibrary; open it on the campus network/VPN to download;"
             "Full automation requires the library to issue a Wiley TDM token",
    "ACS": "ACS subscription journal — no full-text API available to individuals; campus network or document delivery only",
    "T&F": "T&F subscription journal — no full-text API available to individuals; campus network or document delivery only",
}


def publisher_of(doi: str) -> tuple[str, str]:
    """DOI → (publisher, type). Return ("", "") when unrecognized."""
    d = (doi or "").strip().lower()
    for prefix, info in PUBLISHERS.items():
        if d.startswith(prefix):
            return info
    return "", ""


# DOI prefixes lie: after a journal moves, a 10.1111 (Wiley) article may live on academic.oup.com.
# When it really matters (no full text), check doi.org for the real landing site, once only, cached.
_LANDING = [
    ("academic.oup.com", "OUP"),
    ("pubs.acs.org", "ACS"),
    ("tandfonline.com", "T&F"),
    ("onlinelibrary.wiley.com", "Wiley"),
    ("sciencedirect.com", "Elsevier"),
    ("link.springer.com", "Springer"),
    ("nature.com", "Springer Nature"),
    ("pubs.rsc.org", "RSC"),
    ("cambridge.org", "Cambridge"),
    ("sagepub.com", "SAGE"),
    ("mdpi.com", "MDPI"),
    ("frontiersin.org", "Frontiers"),
    ("journals.plos.org", "PLOS"),
    ("biomedcentral.com", "BMC"),
]
_HOST_MEM: dict[str, list] = {}
_HOST_FILE: Path | None = None
_HOST_BUDGET = [40]      # At most 40 lookups per run, beyond that trust the prefix, don't turn the download into a crawl


def _host_cache_path() -> Path:
    global _HOST_FILE
    if _HOST_FILE is None:
        _HOST_FILE = app_dir() / ".cache_host.json"
        try:
            data = json.loads(_HOST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _HOST_MEM.update(data)
        except Exception:
            pass
    return _HOST_FILE


def landing_label(doi: str, timeout: float = 4.0) -> tuple[str, str]:
    """Where the DOI really resolves → (publisher, hostname). Cached once resolved; return ("", "") on failure."""
    d = (doi or "").strip()
    if not d:
        return "", ""
    _host_cache_path()
    hit = _HOST_MEM.get(d)
    if hit and len(hit) == 2:
        return hit[0], hit[1]
    if _HOST_BUDGET[0] <= 0:
        return "", ""                 # With too many papers in one run, stop looking them up one by one so it doesn't drag
    _HOST_BUDGET[0] -= 1
    try:
        r = requests.head("https://doi.org/" + d, allow_redirects=False,
                          timeout=timeout, headers={"User-Agent": f"paperfetch/{VERSION}"})
        loc = (r.headers.get("Location") or "").lower()
    except Exception:
        return "", ""
    if not loc:
        return "", ""
    host = urlparse(loc).hostname or ""
    host = host[4:] if host.startswith("www.") else host
    label = next((lab for frag, lab in _LANDING if frag in host), "")
    if label:
        _HOST_MEM[d] = [label, host]
        try:
            _host_cache_path().write_text(json.dumps(_HOST_MEM, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return label, host



def why_missing(rec: dict) -> str:
    """Why it was not obtained — give one actionable sentence where possible."""
    pub, kind = publisher_of(rec.get("doi", ""))
    doi = (rec.get("doi") or "").strip()
    notes = "; ".join((rec.get("_notes") or [])[:2])
    if rec.get("_oa_needs_api"):
        return "OA journal, but full text only on ScienceDirect — configure elsevier_api_key to download"
    if rec.get("_els_note"):
        return rec["_els_note"]
    if kind == "oa":
        extra = f" (script blocked this time: {notes})" if notes else ", just retry"
        return f"{pub} is an open access journal, just open it in a browser and download{extra}"
    if kind == "sub":
        real, host = landing_label(doi)
        oa = (rec.get("oa_status") or "").strip().lower()
        if oa == "bronze":
            # The publisher marks it "free to read" but the script is still blocked by the WAF -- don't let people think it's broken
            return (f"{pub} is marked as free to read (bronze), a browser can download it but the script is blocked by the WAF -- "
                    f"use \"the to-download list\" to open and download" + (f"; {notes}" if notes else ""))
        if real and real != pub:
            # The journal moved: prefix is still Wiley, actually on OUP
            base = (f"This DOI now redirects to {host}({real}) -- the journal moved to a new platform,"
                    f"requires campus network/institutional access, or use interlibrary loan")
        else:
            base = SUB_HINT.get(pub, f"{pub} is a subscription journal -- requires campus network/institutional access, or use interlibrary loan")
        tail = ""
        if oa and oa not in ("gold", "diamond", "hybrid", "green"):
            tail = f"(metadata:{oa}, not OA)"
        if notes:
            tail += f"(underlying error:{notes})"
        return base + tail
    if notes:
        return notes
    if kind == "api":
        return f"{pub} is a subscription journal -- requires elsevier_api_key + insttoken (see the instructions)"
    if not doi:
        return "No DOI — Chinese journals are mostly on CNKI / Wanfang; search by article title"
    return "No OA version available (most likely a subscription journal)"


# --------------------------------------------------------------------------- #
# Config and logging
# --------------------------------------------------------------------------- #
# Keys that start with an underscore but must be written to the file ("_note" is a comment for the user; drop it and nobody knows what each item does)
_KEEP_UNDERSCORE = {"_notes"}


def load_config(path: Path | None = None) -> dict:
    """
    Load the config. Three things that go wrong easily are all handled here:

    1. **A broken config.json** (trailing comma left by hand editing, or an interrupted write) must not stop the program from starting.
       Rename the bad file to config.broken.json for the record, then continue with defaults — the worst case is re-entering the key once,
       which beats a double-click that does nothing.
    2. **--config points at a path that does not exist yet**: record it as the write target,
       never write silently to app_dir() (otherwise settings made in the UI land in another file and "vanish" on restart).
    3. **Config keys added by a new version** are written back automatically, only the missing ones, leaving user values and key order untouched.
    """
    cfg = dict(DEFAULT_CONFIG)
    default_path = app_dir() / "config.json"
    # First run (especially the exe) generates the config file next to the program
    if not path and not default_path.exists():
        try:
            default_path.write_text(
                json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    cands = [Path(path)] if path else [default_path, Path.cwd() / "config.json"]
    for p in cands:
        if not (p and p.is_file()):
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Top level is not an object")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            # Bad file: keep a backup and fall back to defaults, never let it break the startup path
            try:
                p.replace(p.with_suffix(".broken.json"))
            except OSError:
                pass
            cfg["_source"] = str(p)          # Still write back to the same place (rebuilds a good one)
            cfg["_cfg_broken"] = str(p)
            break

        cfg.update(raw)
        cfg["_source"] = str(p)
        missing = [k for k in DEFAULT_CONFIG
                   if (k in _KEEP_UNDERSCORE or not k.startswith("_"))
                   and k not in raw]
        if missing:
            try:
                save_config(cfg)
            except Exception:
                pass
            cfg["_added_keys"] = missing
        break

    # --config gave a path but the file is missing: remember it, save there late
    if path and not cfg.get("_source"):
        cfg["_source"] = str(Path(path))
    return cfg


def save_config(cfg: dict) -> Path:
    """
    Write the config back to config.json (drop internal keys, keep `_notes`).

    ⚠ **The write must be atomic**: plain write_text truncates first, so a power cut / full disk / killed process
    leaves half a JSON — and config.json holds every key the user has, which the next start cannot read.
    So write .tmp first and then replace (replace is occasionally blocked on Windows; retry once).
    """
    p = Path(cfg["_source"]) if cfg.get("_source") else (app_dir() / "config.json")
    data = {k: v for k, v in cfg.items()
            if k in _KEEP_UNDERSCORE or not k.startswith("_")}
    txt = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(txt, encoding="utf-8")
        try:
            tmp.replace(p)
        except OSError:
            time.sleep(0.15)      # On Windows it may be held by Notepad/AV, wait and retry
            tmp.replace(p)
    except OSError as e:
        # Don't dump raw stuff like [WinError 5] ...config.tmp on the user, they can't read it
        raise OSError(f"Could not save the config to {p.name}({getattr(e, 'strerror', None) or e}).\n"
                      f"Common causes: the file is locked by Notepad / antivirus software, or the folder is not writable."
                      f"Close it and try again.") from e
    return p


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# robots.txt (RFC 9309)
# --------------------------------------------------------------------------- #
class RobotsTxt:
    """
    Python's stock robotparser has two hard flaws: it ignores Allow rules and treats 403 as a site-wide ban.
    This implementation follows RFC 9309: longest match wins, Allow wins on ties, 403/401 means "undetermined".
    """

    def __init__(self, owner: "PoliteSession"):
        self.owner = owner
        self.cache: dict[str, list | None] = {}

    def _fetch(self, scheme: str, netloc: str) -> list | None:
        if netloc in self.cache:
            return self.cache[netloc]
        rules = None
        try:
            r = self.owner.s.get(f"{scheme}://{netloc}/robots.txt", timeout=15)
            if r.status_code == 200:
                rules = self._parse(r.text)
            elif r.status_code >= 400 and r.status_code < 500 and r.status_code not in (401, 403):
                rules = []
        except Exception:
            rules = None
        self.cache[netloc] = rules
        return rules

    @staticmethod
    def _parse(text: str) -> list:
        groups, agents, rules, in_rules = [], [], [], False
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                if in_rules:
                    groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(v.lower())
            elif k in ("allow", "disallow"):
                in_rules = True
                if v:
                    rules.append((k == "allow", v))
        if agents or rules:
            groups.append((agents, rules))
        return groups

    @staticmethod
    def _hit(path: str, rule: str) -> bool:
        pat = re.escape(rule).replace(r"\*", ".*")
        if pat.endswith(r"\$"):
            pat = pat[:-2] + "$"
        return re.match(pat, path) is not None

    def check(self, url: str) -> bool | None:
        """True=allowed / False=explicitly disallowed / None=undetermined (robots.txt unreachable)"""
        u = urlparse(url)
        rules = self._fetch(u.scheme, u.netloc)
        if rules is None:
            return None
        ua = self.owner.s.headers["User-Agent"].lower()
        applicable, best_spec = [], 0
        for agents, rs in rules:
            spec = 0
            for a in agents:
                if a == "*":
                    spec = max(spec, 1)
                elif a and a in ua:
                    spec = max(spec, 2)
            if spec:
                if spec > best_spec:
                    applicable, best_spec = list(rs), spec
                elif spec == best_spec:
                    applicable.extend(rs)
        if not applicable:
            return True
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        allowed, best_len = True, -1
        for is_allow, rule in applicable:
            if self._hit(path, rule):
                if len(rule) > best_len or (len(rule) == best_len and is_allow):
                    best_len, allowed = len(rule), is_allow
        return allowed


# --------------------------------------------------------------------------- #
# Polite request laye
# --------------------------------------------------------------------------- #
class QuotaExceeded(RuntimeError):
    """The remote quota is exhausted (not ordinary rate limiting). Retrying is useless; tell the user what to do."""


class PoliteSession:
    """Global rate limiting + exponential backoff + explicit UA. This is the only outbound path in the whole tool."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rps = float(cfg.get("rate_limit_rps") or 1.0)
        self.min_interval = 1.0 / self.rps
        self.timeout = int(cfg.get("timeout") or 30)
        self._last = 0.0
        self.s = requests.Session()
        email = (cfg.get("email") or "").strip()
        self.s.headers.update({
            "User-Agent": (
                f"paperfetch/{VERSION} (academic literature retrieval; "
                f"contact: {email})" if email else f"paperfetch/{VERSION}"
            ),
            "Accept-Encoding": "gzip, deflate",
        })
        self.robots = RobotsTxt(self)
        self.stats = {"requests": 0, "retries": 0, "robots_blocked": 0}
        self.quota: dict = {}      # Quota info from the latest OpenAlex response

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


    def get(self, url: str, *, params=None, headers=None, stream=False,
            max_retries: int = 4):
        if is_forbidden(url):
            raise PermissionError(
                "Refuse to scrape subscription page URLs (they need authentication and violate publisher terms)."
                "For Elsevier full text use the official API: configure elsevier_api_key."
            )

        last_exc = None
        for attempt in range(max_retries):
            self._throttle()
            self.stats["requests"] += 1
            try:
                r = self.s.get(url, params=params, headers=headers,
                               stream=stream, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                self.stats["retries"] += 1
                # Timeout / can't connect: more retries won't help -- one probe is enough,
                # else 4 x 30 s timeouts = two minutes of waiting and the user thinks it hung
                if isinstance(e, (requests.Timeout, requests.ConnectionError)) and attempt >= 1:
                    break
                time.sleep(2 ** attempt)
                continue

            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After", "")
                try:
                    hinted = float(ra)
                except (TypeError, ValueError):
                    hinted = 0.0
                # When the quota runs out Retry-After gives tens of thousands of seconds, don't really wait
                if r.status_code == 429 and hinted > 300:
                    host = urlparse(url).hostname or ""
                    if "openalex" in host:
                        reset_h = hinted / 3600
                        raise QuotaExceeded(
                            f"The free shared OpenAlex quota is used up (shared per IP at $0.10/day,"
                            f"{reset_h:.1f} hours until reset).\n"
                            "Apply for a free key and it recovers immediately: a separate $1/day quota,"
                            "and paging past 200 results.\n"
                            "  https://openalex.org/settings/api\n"
                            "Once you have it, put it into openalex_api_key in config.json.")
                    raise QuotaExceeded(
                        f"{host} returned 429, quota may be exhausted (Retry-After {hinted:.0f}s)")
                wait = min(hinted, 30) if hinted else min(2 ** attempt * 2, 30)
                log(f"    · HTTP {r.status_code}, {wait:.0f}s until retry ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                self.stats["retries"] += 1
                continue
            if "openalex" in (urlparse(url).hostname or ""):
                self.quota = _parse_quota(r.headers)
            return r
        if last_exc:
            host = urlparse(url).hostname or url
            if isinstance(last_exc, (requests.Timeout, requests.ConnectionError)):
                raise RuntimeError(
                    f"Cannot connect to {host}, both attempts failed.\n"
                    "Possible causes: (1) no network connection / a proxy is needed; (2) the remote service is temporarily down;"
                    "(3) the domain is blocked on your network.\n"
                    f"First open https://{host} in a browser to test -- if it opens, retry; if not, it is a network problem.\n"
                    f"(underlying error:{type(last_exc).__name__})")
            raise last_exc
        raise RuntimeError(f"Retries exhausted:{url}")


# --------------------------------------------------------------------------- #
# Chinese search terms -> English (common food science terms, so machine translation doesn't mangle them)
# --------------------------------------------------------------------------- #
ZH_TERMS = {
    # 样品 / 对象
    "南美白对虾": "Litopenaeus vannamei", "凡纳滨对虾": "Litopenaeus vannamei", "对虾": "shrimp", "虾头": "shrimp head", "虾壳": "shrimp shell",
    "虾仁": "shrimp meat", "虾": "shrimp",
    "鸡肉": "chicken", "鸡": "chicken", "牛肉": "beef", "猪肉": "pork",
    "羊肉": "lamb", "鱼肉": "fish", "鱼": "fish", "肉": "meat",
    "牛乳": "milk", "乳": "milk", "鸡蛋": "egg", "大豆": "soybean",
    "米": "rice", "小麦": "wheat", "面粉": "flour",
    "植物油": "vegetable oil", "油": "oil", "脂肪": "fat", "脂质": "lipid",
    # 工艺
    "卤制": "braising", "卤": "braise", "腌制": "curing", "腌": "curing",
    "蒸煮": "steaming", "蒸": "steaming", "煮": "boiling", "熬煮": "boiling",
    "油炸": "deep frying", "煎": "pan-frying", "烘烤": "roasting", "烤": "roasting",
    "干燥": "drying", "风干": "air drying", "冷冻": "freezing", "解冻": "thawing",
    "发酵": "fermentation", "烟熏": "smoking", "微波": "microwave",
    "超声": "ultrasound", "超高压": "high hydrostatic pressure", "辐照": "irradiation",
    "包装": "packaging", "储藏": "storage", "贮藏": "storage",
    "加工": "processing", "热处理": "thermal treatment", "烹饪": "cooking",
    "炖": "stewing", "烟点": "smoke point",
    # 感官 / 品质
    "香气": "aroma", "风味": "flavor", "滋味": "taste", "口感": "mouthfeel",
    "色泽": "color", "颜色": "color", "质构": "texture", "嫩度": "tenderness",
    "硬度": "hardness", "弹性": "elasticity", "咀嚼性": "chewiness",
    "水分": "moisture", "含水率": "moisture content", "新鲜度": "freshness",
    "品质": "quality", "感官评价": "sensory evaluation", "感官": "sensory",
    "电子鼻": "electronic nose", "电子舌": "electronic tongue",
    "气味": "odor", "异味": "off-flavor", "腥味": "fishy odor", "鲜味": "umami",
    "持水性": "water holding capacity", "蒸煮损失": "cooking loss",
    # 成分
    "蛋白质": "protein", "多肽": "peptide", "氨基酸": "amino acid",
    "游离氨基酸": "free amino acid", "脂肪酸": "fatty acid",
    "挥发性化合物": "volatile compounds", "挥发性": "volatile",
    "多酚": "polyphenol", "黄酮": "flavonoid", "生物碱": "alkaloid",
    "萜类": "terpenoid", "木脂素": "lignan", "多糖": "polysaccharide",
    "糖": "sugar", "膳食纤维": "dietary fiber", "维生素": "vitamin",
    "矿物质": "mineral", "肌原纤维蛋白": "myofibrillar protein",
    "肌红蛋白": "myoglobin", "胶原": "collagen", "核苷酸": "nucleotide",
    "有机酸": "organic acid", "总酚": "total phenolic content",
    # 机制 / 变化
    "脂质氧化": "lipid oxidation", "蛋白氧化": "protein oxidation",
    "氧化": "oxidation", "美拉德反应": "Maillard reaction",
    "降解": "degradation", "代谢": "metabolism", "代谢组学": "metabolomics",
    "转录组": "transcriptomics", "蛋白组学": "proteomics",
    "水解": "hydrolysis", "聚合": "polymerization", "交联": "cross-linking",
    "变性": "denaturation", "抗氧化活性": "antioxidant activity",
    "抗氧化": "antioxidant", "自由基": "free radical",
    "丙二醛": "malondialdehyde", "过氧化值": "peroxide value", "酸价": "acid value",
    # 分析
    "气相色谱质谱联用": "GC-MS", "气相色谱": "gas chromatography",
    "液相色谱质谱联用": "LC-MS", "高效液相色谱": "HPLC",
    "液相色谱": "liquid chromatography", "质谱": "mass spectrometry",
    "核磁共振": "nuclear magnetic resonance", "红外光谱": "infrared spectroscopy",
    "顶空": "headspace", "固相微萃取": "solid phase microextraction",
    "同时蒸馏萃取": "simultaneous distillation extraction",
    "主成分分析": "principal component analysis", "聚类分析": "cluster analysis",
    "偏最小二乘": "partial least squares", "相关性分析": "correlation analysis",
    # 香料 / 药食同源
    "药食同源": "medicinal and edible", "香辛料": "spice", "香料": "spice",
    "桂皮": "cinnamon", "八角": "star anise", "花椒": "Sichuan pepper",
    "生姜": "ginger", "大蒜": "garlic", "洋葱": "onion", "辣椒": "chili",
    "胡椒": "pepper", "丁香": "clove", "茴香": "fennel",
    "陈皮": "tangerine peel", "甘草": "licorice", "枸杞": "wolfberry",
    "当归": "Angelica sinensis", "黄芪": "Astragalus", "人参": "ginseng",
    # 通用
    "内源": "endogenous", "外源": "exogenous", "游离": "free", "总": "total",
    "含量": "content", "变化": "changes", "规律": "pattern", "特性": "characteristics",
    "作用": "role", "调控": "regulation", "抑制": "inhibition", "促进": "promotion",
    "改善": "improvement", "稳定性": "stability", "乳化": "emulsification",
    "凝胶": "gel", "结构": "structure", "功能": "function", "活性": "activity",
    "指纹图谱": "fingerprint", "特征": "characteristic", "差异": "difference",
    "影响": "effect", "机制": "mechanism", "研究": "study",
    "分析": "analysis", "比较": "comparison", "优化": "optimization",
    "提取": "extraction", "分离": "separation", "纯化": "purification",
    "鉴定": "identification", "表征": "characterization",
}

# Extra terms for aroma / lipid work (added 2026-11-04, for CN<->EN in search and in-library search)
ZH_TERMS.update({
    "吡嗪": "pyrazine", "吡嗪类": "pyrazines", "醛": "aldehyde", "醛类": "aldehydes",
    "酮": "ketone", "酮类": "ketones", "醇": "alcohol", "醇类": "alcohols",
    "美拉德": "Maillard", "美拉德反应": "Maillard reaction",
    "磷脂": "phospholipid", "硫代巴比妥酸": "thiobarbituric acid", "丙二醛": "malondialdehyde",
    "阈值": "threshold", "气味阈值": "odor threshold", "加热": "heating", "热加工": "thermal processing",
    "真空": "vacuum", "真空低温": "sous-vide", "包埋": "encapsulation", "微胶囊": "microcapsule",
    "肌原纤维": "myofibrillar", "肌动蛋白": "actin", "香气物质": "aroma compounds",
    "挥发性物质": "volatile compounds", "挥发性成分": "volatile compounds",
    "腥味": "fishy off-flavor", "哈败": "rancidity", "哈喇味": "rancid",
    "氧化降解": "oxidative degradation", "自动氧化": "autoxidation", "脂质氧化": "lipid oxidation",
    "脂肪酸组成": "fatty acid composition", "游离脂肪酸": "free fatty acid",
    "抗氧化能力": "antioxidant capacity", "复水": "rehydration",
})


def _free_translate(text: str, timeout: int = 15) -> str:
    """MyMemory free translation (no key, rate-limited). Returns an empty string on failure."""
    try:
        r = requests.get("https://api.mymemory.translated.net/get",
                         params={"q": text, "langpair": "zh-CN|en"}, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            return (d.get("responseData") or {}).get("translatedText", "").strip()
    except Exception:
        pass
    return ""


def zh_to_en(q: str, use_api: bool = True) -> tuple[str, list[str]]:
    """
    Chinese search terms → English. The glossary is consulted first (longest match, terms sorted by length descending);
    leftover Chinese fragments then fall back to free translation.
    Returns (English query string, list of fragments still untranslated).
    """
    out = q
    for zh in sorted(ZH_TERMS, key=len, reverse=True):
        if zh in out:
            out = out.replace(zh, f" {ZH_TERMS[zh]} ")
    out = re.sub(r"\s+", " ", out).strip()

    leftover = re.findall(r"[\u4e00-\u9fff]+", out)
    if leftover:
        if use_api:
            for frag in dict.fromkeys(leftover):
                tr = _free_translate(frag)
                if tr:
                    out = out.replace(frag, tr)
            out = re.sub(r"\s+", " ", out).strip()
        leftover = re.findall(r"[\u4e00-\u9fff]+", out)
    return out, leftover


# --------------------------------------------------------------------------- #
# Search sources
# --------------------------------------------------------------------------- #
def _oa_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    slots: list[tuple[int, str]] = []
    for word, positions in inv.items():
        for p in positions:
            slots.append((p, word))
    slots.sort()
    return " ".join(w for _, w in slots)


def _first_author(authorships) -> str:
    try:
        return authorships[0]["author"]["display_name"] or ""
    except Exception:
        return ""


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


# --------------------------------------------------------------------------- #
# OpenAlex accounts and quota
#
# OpenAlex bills by usage from 2026: without a key the whole egress IP shares $0.10/day,
# your own key gets $1.00/day separately. Quota info is in the response headers (X-RateLimit-*-USD).
# --------------------------------------------------------------------------- #
OPENALEX_SIGNUP = "https://openalex.org/settings/api"
QUOTA_TTL = 300          # Quota cache seconds; don't burn money over and over just to see the numbe


def _parse_quota(headers) -> dict:
    def num(k, cast=float):
        try:
            v = headers.get(k)
            return cast(v) if v not in (None, "") else 0
        except (TypeError, ValueError):
            return 0

    limit_usd = num("X-RateLimit-Limit-USD")
    return {
        "has_key": limit_usd > 0.5, # 0.10 = shared quota, 1.00 = own key
        "limit_usd": limit_usd,
        "remaining_usd": num("X-RateLimit-Remaining-USD"),
        "limit_calls": int(num("X-RateLimit-Limit")),
        "remaining_calls": int(num("X-RateLimit-Remaining")),
        "reset": int(num("X-RateLimit-Reset")),
        "at": time.time(),
    }


def openalex_accounts(cfg: dict) -> list[dict]:
    """Account list. A legacy config with only a single openalex_api_key is synthesized into one entry."""
    accs = [dict(a) for a in (cfg.get("openalex_accounts") or []) if a.get("api_key")]
    if not accs and cfg.get("openalex_api_key"):
        accs = [{
            "label": cfg.get("email") or "Default account",
            "email": cfg.get("email") or "",
            "api_key": cfg["openalex_api_key"],
        }]
    return accs


def active_account_index(cfg: dict) -> int:
    accs = openalex_accounts(cfg)
    if not accs:
        return -1
    try:
        i = int(cfg.get("openalex_active") or 0)
    except (TypeError, ValueError):
        i = 0
    return i if 0 <= i < len(accs) else 0


def sync_active_key(cfg: dict) -> None:
    """Sync the current account key to openalex_api_key (the fetch logic only reads this field)."""
    accs = openalex_accounts(cfg)
    i = active_account_index(cfg)
    if i >= 0:
        cfg["openalex_active"] = i
        cfg["openalex_api_key"] = accs[i]["api_key"]
        if cfg.get("openalex_accounts"):
            cfg["openalex_accounts"] = accs


def active_account(cfg: dict) -> dict:
    accs = openalex_accounts(cfg)
    i = active_account_index(cfg)
    return accs[i] if i >= 0 else {}


def add_account(cfg: dict, label: str, email: str, api_key: str,
                activate: bool = True) -> int:
    """Add to the account list and return the index (an existing entry with the same name or key is reused)."""
    accs = openalex_accounts(cfg)
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("API key cannot be empty")
    for i, a in enumerate(accs):
        if a.get("api_key") == api_key:
            if label:
                a["label"] = label
            if email:
                a["email"] = email
            cfg["openalex_accounts"] = accs
            if activate:
                cfg["openalex_active"] = i
                sync_active_key(cfg)
            return i
    accs.append({
        "label": (label or "").strip() or (email or "New account"),
        "email": (email or "").strip(),
        "api_key": api_key,
    })
    cfg["openalex_accounts"] = accs
    idx = len(accs) - 1
    if activate:
        cfg["openalex_active"] = idx
        sync_active_key(cfg)
    return idx


def probe_quota(cfg: dict, sess: "PoliteSession", force: bool = False) -> dict:
    """
    Send one very lightweight request to read the quota header (the cache is reused for 5 minutes, so checking the numbers does not keep burning quota).
    """
    cached = getattr(sess, "quota", None) or {}
    if cached and not force and (time.time() - cached.get("at", 0) < QUOTA_TTL):
        return cached
    params = {"per-page": 1, "select": "id"}
    if cfg.get("email"):
        params["mailto"] = cfg["email"]
    if cfg.get("openalex_api_key"):
        params["api_key"] = cfg["openalex_api_key"]
    try:
        r = sess.get("https://api.openalex.org/works", params=params)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if r.status_code == 403 and "key_not_activated" in r.text:
        return {"error": "This key is not activated yet — click the activation link in your inbox",
                "not_activated": True}
    if r.status_code == 429:
        return {"error": "Quota exhausted, resets at UTC midnight", "exhausted": True}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    return getattr(sess, "quota", {}) or {}


def request_openalex_key(email: str) -> dict:
    """
    Request a new key with an email address (official OpenAlex endpoint).
    Returns {"api_key": ..., "message": ...}; the key only takes effect after you click the activation link in the email.
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValueError("Invalid email format")
    try:
        r = requests.post("https://api.openalex.org/api-keys",
                          json={"email": email}, timeout=45,
                          headers={"Content-Type": "application/json",
                                   "User-Agent": f"paperfetch/{VERSION} (academic use)"})
    except requests.RequestException as e:
        raise RuntimeError(f"Request failed:{type(e).__name__}")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Request failed: HTTP {r.status_code} {r.text[:150]}")
    d = r.json()
    out = {"api_key": d.get("api_key", ""), "email": d.get("email", email),
           "expires": d.get("activation_link_expires", ""),
           "budget": d.get("daily_budget_usd_after_activation")}
    # Also parse the activation link (if any) so the user can be pointed at it
    act = d.get("activation_link") or d.get("activation_url") or ""
    if act:
        out["activation_link"] = act
    return out


# --------------------------------------------------------------------------- #
# Boolean search: AND / OR / NOT / parentheses / field qualifiers
#
# OpenAlex filter semantics (measured and verified):
#   repeating a key (comma)   = AND      tiab:shrimp,tiab:aroma  -> 439 (intersection)
#   pipe |                    = OR       tiab:shrimp|aroma       -> 224,891 (union)
#   leading ! = NOT      tiab:!fish              -> applies server side
#   double quotes             = exact phrase tiab:"lipid oxidation"
# --------------------------------------------------------------------------- #
FIELD_MAP = {
    "all": "default.search",
    "tiab": "title_and_abstract.search",
    "title": "title.search",
    "abstract": "abstract.search",
    "fulltext": "fulltext.search",
}
FIELD_LABEL = {
    "all": "Any field",
    "tiab": "Title + abstract",
    "title": "Title",
    "abstract": "Abstract",
    "fulltext": "Full text",
}
# With * or ? wildcards you must use the non-stemmed exact field, otherwise OpenAlex returns 400:
#   "Wildcards (* or ?) require the exact (no-stem) field"
FIELD_EXACT_MAP = {
    "all": "default.search.exact",
    "tiab": "title_and_abstract.search.exact",
    "title": "title.search.exact",
    "abstract": "abstract.search.exact",
    "fulltext": "fulltext.search.exact",
}

# Fields for local re-ranking after merging branches (field name, descending?)
_SORT_FIELD = {
    "cited_desc": ("cited_by", True),
    "cited_asc": ("cited_by", False),
    "date_desc": ("year", True),
    "date_asc": ("year", False),
}

# Sort mode: key -> (OpenAlex sort string, UI label, whether branches can be merged in order)
SORTS = {
    "relevance": (None, "Relevance", False),
    "cited_desc": ("cited_by_count:desc", "Citations high → low", True),
    "cited_asc": ("cited_by_count:asc", "Citations low → high", True),
    "date_desc": ("publication_date:desc", "Year new → old", True),
    "date_asc": ("publication_date:asc", "Year old → new", True),
}
# Work type: UI key -> (OpenAlex type filter value, UI label)
# OpenAlex type is an enum field; several values joined by | mean OR (measured: type:article|review count
# equals the two added separately, so it works; but title.search:review|survey does **not** support OR).
DOC_TYPES = {
    "article": ("article", "Research article"),
    "review": ("review", "Review"),
    "article+review": ("article|review", "Research article + Review"),
    "dissertation": ("dissertation", "Dissertation"),
    "preprint": ("preprint", "Preprint"),
    "book-chapter": ("book-chapter", "Book chapter"),
    "all": ("", "Any type"),
}
TYPE_LABEL = {
    "article": "Research article", "review": "Review", "dissertation": "Dissertation",
    "preprint": "Preprint", "book-chapter": "Book chapter", "book": "Book",
    "conference-paper": "Conference paper", "proceedings-article": "Conference paper",
    "dataset": "Dataset", "editorial": "Editorial", "letter": "Correspondence",
    "erratum": "Erratum", "paratext": "Paratext", "peer-review": "Peer review",
    "reference-entry": "Entry", "report": "Report", "standard": "Standard",
    "textbook": "Textbook", "libguides": "Guideline", "withdrawn": "Retracted",
    # OpenAlex's real enum has these too; without them the UI shows raw English
    "post": "Short article", "magazine-article": "Journal article", "suppressed": "Blocked",
    "component": "Component", "journal-issue": "Whole issue",
    "other": "Other",
}

# Heuristic for review-like titles: OpenAlex labels many reviews as article, so flag it in the UI,
# not used for search filtering (better too few hints than a screen full of "maybe").
# ! Deliberately **not** including advances in / progress in / recent developments --
# measured: the top 10 for title:"advances in shrimp" had 6 hits, many normal experimental papers,
# a screen full of grey flags is the same as no flags.
_REVIEW_TITLE = re.compile(
    r"\b(a\s+)?(systematic review|review|survey|overview|meta-?analysis|"
    r"state[- ]of[- ]the[- ]art)\b", re.I)


def looks_like_review(title: str) -> bool:
    """The title contains words such as review / survey / overview / meta-analysis."""
    t = title or ""
    if not t:
        return False
    if _REVIEW_TITLE.search(t):
        return True
    return "Review" in t or "Research progress" in t


def is_maybe_review(rec: dict) -> bool:
    """
    Only records marked as research article (or not typed at all) are eligible for a "possible review" flag.
    Dissertations and preprints are not articles to begin with; the UI shows their own type badge,
    and stacking a "possible review" flag on top would not match the counts in the log.
    """
    wt = (rec.get("work_type") or "").strip().lower()
    if wt not in ("", "article"):
        return False
    return looks_like_review(rec.get("title", ""))


# Field prefixes allowed in expressions (matched lowercase)
FIELD_ALIAS = {
    "all": "all", "default": "all", "any": "all", "任意": "all", "全部": "all",
    "tiab": "tiab", "title_abstract": "tiab", "标题摘要": "tiab",
    "title": "title", "ti": "title", "题名": "title", "标题": "title",
    "abstract": "abstract", "ab": "abstract", "摘要": "abstract",
    "fulltext": "fulltext", "full": "fulltext", "全文": "fulltext",
}
MAX_GROUPS = 6          # Max request count allowed after merging
MAX_EXPAND = 64         # Intermediate guard while expanding (there may be many branches before merging)


class QueryError(ValueError):
    """Raised when the query string is malformed; message is shown to the user directly.

    kind distinguishes the two cases so the front end can add the matching hint:
      - "syntax" (default): the query itself is malformed, suggesting a check of brackets / AND OR
      - "filtered": the query is fine, but this record was filtered out by year / OA / work type
    """

    def __init__(self, message: str, kind: str = "syntax"):
        super().__init__(message)
        self.kind = kind


_TOK = re.compile(
    r'(?P<lp>\()|(?P<rp>\))|(?P<op>&&|\|\||\+)'
    r"|(?P<word>\"[^\"]*\"|'[^']*'|[^\s()]+)"
)


def _tokenize(q: str) -> list[tuple[str, str]]:
    toks = []
    for m in _TOK.finditer(q):
        if m.group("lp"):
            toks.append(("(", ""))
        elif m.group("rp"):
            toks.append((")", ""))
        elif m.group("op"):
            toks.append(("AND" if m.group("op") in ("&&", "+") else "OR", ""))
        else:
            w = m.group("word")
            up = w.upper()
            # Only a bare word can be an operator; a quoted "AND" is an ordinary search term
            if up in ("AND", "OR", "NOT") and w[:1] not in ("\"", "'"):
                toks.append((up, ""))
            else:
                toks.append(("T", w))
    return toks


def _term(word: str) -> tuple:
    """Parse a single search term: - exclusion prefix, field qualifier, quoted phrase."""
    w = word.strip()
    neg = False
    while len(w) > 1 and w[0] == "-":
        neg = not neg
        w = w[1:].strip()
    if len(w) >= 2 and w[0] in ("\"", "'") and w[-1] == w[0]:
        w = '"' + w[1:-1].strip() + '"'
    field = None
    m = re.match(r"^([A-Za-z_\u4e00-\u9fff]{1,20}):(.+)$", w)
    if m:
        k = FIELD_ALIAS.get(m.group(1).lower())
        if k:
            field, w = k, m.group(2).strip()
            if len(w) >= 2 and w[0] in ("\"", "'") and w[-1] == w[0]:
                w = '"' + w[1:-1].strip() + '"'
    has_wild = ("*" in w) or ("?" in w)
    return ("term", field, w, neg, has_wild)


def _negate(node):
    """Push negations down to the leaves (De Morgan) to make DNF conversion easier."""
    if node is None:
        return None
    if node[0] == "term":
        return ("term", node[1], node[2], not node[3], node[4])
    return ("or" if node[0] == "and" else "and", [_negate(c) for c in node[1]])


# A single stop word (in / of / to ...) matches nothing in OpenAlex *.search fields,
# but gets stuffed in as a filter and the whole search drops to 0 hits --
# guaranteed when a whole title is pasted in ("Changes in lipid profiles ... submitted to ..." -> 0 records).
STOPWORDS = set("""
a an the of in on at to for from by with without within into onto upon
as is are was were be been being am
that this these those it its their there here
and or not nor but if then than so such
during between among about above under over after before
via using used based per versus vs
we our you your they them he she his her
can could may might must shall should will would
do does did done have has had having
also more most less least very much many some any all both each other another same own only just
""".split())


def _is_stop(v: str) -> bool:
    """Whether this is a stopword that can safely be dropped. Quoted phrases, wildcards, and anything containing a space are left untouched."""
    s = v.strip()
    if not s:
        return False
    if s[0] in "\"'" or "*" in s or "?" in s or " " in s:
        return False
    return s.lower() in STOPWORDS


# Tell whether "this string is a pasted title": no boolean operator, no quotes/wildcards, no field prefix
_OP_UPPER = re.compile(r"\b(AND|OR|NOT)\b")
_FIELD_PREFIX = re.compile(r"(^|\s)(title|abstract|tiab|all|fulltext)\s*:", re.I)


def _looks_like_title(q: str) -> bool:
    s = " ".join((q or "").split())
    if not (20 <= len(s) <= 300):
        return False
    if _OP_UPPER.search(s) or _FIELD_PREFIX.search(s):
        return False
    if any(ch in s for ch in '"*?'):
        return False
    return len(s.split()) >= 5


def _phrase_of(q: str) -> str:
    """Clean a title into a phrase that can go into a filter: commas are filter separators and must be removed."""
    s = " ".join((q or "").split())
    s = re.sub(r"[,;|]", " ", s)
    s = re.sub(r"[()\[\]{}]", " ", s)
    s = re.sub(r"[\"'“”‘’]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


# ---- Given a DOI / OpenAlex ID / PMID: this is not "search", it's "lookup" ----
# Without this, "10.1111/ijfs.15747" gets split into text and searched, returning a pile of unrelated papers with the same digits.
_DOI_Q = re.compile(
    r"^\s*(?:(?:https?://)?(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+?)\s*$", re.I)
_OAID_Q = re.compile(r"^\s*(?:(?:https?://)?openalex\.org/)?([Ww]\d{6,})\s*$")
_PMID_Q = re.compile(r"^\s*pmid:?\s*(\d{6,9})\s*$", re.I)


def parse_identifier(q: str):
    """Whether the input is an identifier → ("doi"|"oaid"|"pmid", value), otherwise None."""
    s = (q or "").strip()
    m = _DOI_Q.match(s)
    if m:
        return "doi", m.group(1).rstrip(".,;")
    m = _OAID_Q.match(s)
    if m:
        return "oaid", m.group(1).upper()
    m = _PMID_Q.match(s)
    if m:
        return "pmid", m.group(1)
    return None


_CROSSREF_TYPE = {
    "journal-article": "article", "posted-content": "preprint",
    "proceedings-article": "conference-paper", "book-chapter": "book-chapter",
    "book": "book", "edited-book": "book", "monograph": "book",
    "dissertation": "dissertation", "report": "report",
    "reference-entry": "reference-entry", "dataset": "dataset",
    "journal-issue": "journal-issue", "component": "component",
    "standard": "standard", "peer-review": "peer-review",
}


def _crossref_type(raw) -> str:
    """
    Crossref uses a different type vocabulary (journal-article / posted-content / edited-book…).
    Anything unrecognized maps to "other"; otherwise raw English values such as edited-book would leak into the UI.
    **An empty value must be returned as empty**, so the type check when pasting a DOI is skipped, instead of blocking untyped records as "other".
    """
    t = (raw or "").strip().lower()
    if not t:
        return ""
    return _CROSSREF_TYPE.get(t, "other")


def _crossref_rec(doi: str) -> dict | None:
    """When OpenAlex has no record for this DOI, fall back to Crossref metadata (downloads still go through Unpaywall)."""
    try:
        r = requests.get("https://api.crossref.org/works/" + quote(doi, safe="/:;()<>._-"),
                         timeout=25, headers={"User-Agent": f"paperfetch/{VERSION}"})
        if r.status_code != 200:
            return None
        m = r.json().get("message") or {}
    except Exception:
        return None
    authors = m.get("author") or []
    first = ""
    if authors:
        a = authors[0]
        first = (a.get("family") or a.get("name") or "").strip()
    year = None
    parts = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    if parts and parts[0]:
        year = parts[0]
    return {
        "oaid": "", "doi": doi,
        "title": _strip_tags((m.get("title") or [""])[0]),
        "journal": _strip_tags((m.get("container-title") or [""])[0]),
        "year": year, "first_author": first, "n_authors": len(authors),
        "cited_by": m.get("is-referenced-by-count", 0),
        "is_oa": False, "oa_status": "", "license": "",
        "pdf_url": "", "landing": "https://doi.org/" + doi,
        "abstract": _strip_tags(m.get("abstract") or "")[:1200],
        # Crossref type is a different vocabulary, see _crossref_type() for the mapping
        "work_type": _crossref_type(m.get("type")),
        "found_via": "crossref",
    }


def _lookup_work(kind: str, val: str, cfg: dict, sess: PoliteSession):
    """Look up a single record by identifier. Returns (rec|None, note)."""
    if kind == "doi":
        url = "https://api.openalex.org/works/doi:" + quote(val, safe="/:;()<>._-")
    elif kind == "oaid":
        url = "https://api.openalex.org/works/" + val
    else:
        url = "https://api.openalex.org/works/pmid:" + val
    params = {}
    if cfg.get("email"):
        params["mailto"] = cfg["email"]
    if cfg.get("openalex_api_key"):
        params["api_key"] = cfg["openalex_api_key"]
    try:
        r = sess.get(url, params=params)
    except Exception as e:
        return None, type(e).__name__
    if r.status_code == 404:
        return None, "notfound"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return _to_rec(r.json()), ""
    except Exception as e:
        return None, type(e).__name__


def parse_bool(q: str, default_op: str = "AND"):
    """
    Recursive descent parser. Precedence: NOT > AND > OR; parentheses change grouping.
    When no operator is written between two adjacent operands, default_op is inserted.
    """
    seq = []
    for t in _tokenize(q):
        if seq:
            prev = seq[-1][0]
            if prev in ("T", ")") and t[0] in ("T", "(", "NOT"):
                seq.append((default_op, ""))
        seq.append(t)
    if not seq:
        raise QueryError("The search term is empty")

    pos = [0]

    def peek():
        return seq[pos[0]][0] if pos[0] < len(seq) else None

    def take():
        t = seq[pos[0]]
        pos[0] += 1
        return t

    def p_or():
        items = [p_and()]
        while peek() == "OR":
            take()
            items.append(p_and())
        return items[0] if len(items) == 1 else ("or", items)

    def p_and():
        items = [p_unary()]
        while peek() == "AND":
            take()
            items.append(p_unary())
        return items[0] if len(items) == 1 else ("and", items)

    def p_unary():
        if peek() == "NOT":
            take()
            return _negate(p_unary())
        return p_atom()

    def p_atom():
        t = peek()
        if t == "(":
            take()
            node = p_or()
            if peek() != ")":
                raise QueryError("Unbalanced brackets: a closing parenthesis ) is missing here")
            take()
            return node
        if t == "T":
            return _term(take()[1])
        raise QueryError("Unexpected symbol in query: %r" % (take()[1],))

    node = p_or()
    if pos[0] != len(seq):
        raise QueryError("Unbalanced brackets")
    return node


def to_dnf(node) -> list[list[tuple]]:
    """Convert to disjunctive normal form: [ [AND terms...], [AND terms...] ]; each sublist is one request."""
    k = node[0]
    if k == "term":
        return [[node]]
    if k == "or":
        out = []
        for c in node[1]:
            out.extend(to_dnf(c))
        if len(out) > MAX_EXPAND:
            raise QueryError("Too many OR branches; please simplify the query")
        return out
    out = [[]]
    for c in node[1]:
        sub = to_dnf(c)
        out = [a + b for a in out for b in sub]
        if len(out) > MAX_EXPAND:
            raise QueryError("The AND/OR combination is too complex; please simplify the query")
    return out


def expr_text(node, default_field: str = "all", nest: bool = False) -> str:
    """Render the parse tree back into a readable expression for display in the UI."""
    k = node[0]
    if k == "term":
        _, f, v, neg, exact = node
        label = f + (".exact" if (exact and f) else "") if f else ""
        s = ("%s:%s" % (label, v)) if f else v
        return ("NOT " + s) if neg else s
    joiner = " AND " if k == "and" else " OR "
    parts = [expr_text(c, default_field, True) for c in node[1]]
    s = joiner.join(parts)
    if k == "or" and len(node[1]) > 1 and nest:
        s = "(" + s + ")"
    return s


def _clean_val(v: str) -> str:
    v = v.strip()
    quoted = len(v) >= 2 and v[0] == '"' and v[-1] == '"'
    if quoted:
        v = v[1:-1]
    v = v.replace(",", " ").replace("|", " ")
    v = re.sub(r"\s+", " ", v).strip()
    v = v.strip(":;.\u3002\uff1a\uff1b")          # Don't let colons/periods from the title into the filte
    if not v:
        return ""
    return ('"%s"' % v) if quoted else v


def build_groups(node, default_field: str = "all") -> list[dict]:
    groups = []
    for clause in to_dnf(node):
        filters, negs = [], []
        dropped = 0
        for _, field, val, neg, exact in clause:
            f = field or default_field
            v = _clean_val(val)
            if not v:
                continue
            if not field and not exact and not neg and _is_stop(v):
                dropped += 1
                continue
            if exact:
                key = FIELD_EXACT_MAP.get(f, FIELD_EXACT_MAP["all"])
            else:
                key = FIELD_MAP.get(f, FIELD_MAP["all"])
            (negs if neg else filters).append((key, v))
        if not filters:
            if dropped:
                raise QueryError("The terms are all stopwords such as in / of / to and carry no search value; try content words instead")
            raise QueryError("Every OR branch needs at least one positive keyword (it cannot hold only exclusions)")
        groups.append({"filters": filters, "negs": negs})
    return groups


def merge_groups(groups: list[dict]) -> list[dict]:
    """
    Merge OR branches that differ in only one position into a single pipe expression, saving duplicate requests:
        [shrimp, aroma] + [shrimp, flavor]  ->  [shrimp, aroma|flavor]
        4 branches of (a OR b) AND (c OR d)   ->  1 branch a|b, c|d
    Merging happens only when the field is the same, the exclusions are the same, and no value is a quoted phrase.
    """
    def ok(v: str) -> bool:
        # Mixing quoted phrases with pipes is unreliable; don't pile too many synonyms on one value eithe
        return '"' not in v and v.count("|") < 14

    changed = True
    while changed and len(groups) > 1:
        changed = False
        n = len(groups)
        for i in range(n):
            for j in range(i + 1, n):
                gi, gj = groups[i], groups[j]
                fi, fj = gi["filters"], gj["filters"]
                if gi["negs"] != gj["negs"] or len(fi) != len(fj):
                    continue
                diff = [k for k in range(len(fi)) if fi[k] != fj[k]]
                if len(diff) != 1:
                    continue
                k = diff[0]
                if fi[k][0] != fj[k][0] or not (ok(fi[k][1]) and ok(fj[k][1])):
                    continue
                merged = {
                    "filters": (list(fi[:k])
                                + [(fi[k][0], fi[k][1] + "|" + fj[k][1])]
                                + list(fi[k + 1:])),
                    "negs": gi["negs"],
                }
                rest = [g for x, g in enumerate(groups) if x not in (i, j)]
                rest.insert(min(i, len(rest)), merged)
                groups = rest
                changed = True
                break
            if changed:
                break
    return groups


def _filter_str(filters: list[tuple]) -> str:
    return ",".join("%s:%s" % (k, v) for k, v in filters)


_SELECT = ("id,doi,title,display_name,publication_year,authorships,"
           "primary_location,best_oa_location,open_access,cited_by_count,"
           "abstract_inverted_index,type")


def _to_rec(w: dict) -> dict:
    best = w.get("best_oa_location") or {}
    prim = w.get("primary_location") or {}
    src = prim.get("source") or {}
    raw_pdf = best.get("pdf_url") or ""
    forb = is_forbidden(raw_pdf)
    return {
        "oaid": w.get("id") or "",
        "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
        "title": _strip_tags(w.get("display_name") or w.get("title") or ""),
        "journal": src.get("display_name") or "",
        "year": w.get("publication_year"),
        "first_author": _first_author(w.get("authorships")),
        "n_authors": len(w.get("authorships") or []),
        "cited_by": w.get("cited_by_count", 0),
        "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
        "oa_status": (w.get("open_access") or {}).get("oa_status", ""),
        "license": best.get("license") or "",
        "pdf_url": "" if forb else raw_pdf,
        "landing": (best.get("landing_page_url")
                    or prim.get("landing_page_url")
                    or (raw_pdf if forb else "")),
        "abstract": _oa_abstract(w.get("abstract_inverted_index"))[:1200],
        "work_type": (w.get("type") or "").strip(),
        "found_via": "openalex",
    }


def _fetch_group(filters: list[tuple], cfg: dict, sess: PoliteSession,
                 want: int, sort: str | None = None) -> tuple[list[dict], int, bool]:
    """
    Run one filter group, paging until want records are collected.
    Returns (records, total hits for the group, whether it was truncated by "no key means top 200 only").

    That last flag matters: without an openalex_api_key, OpenAlex **does not return next_cursor** past page 1,
    so the program silently stops at 200 records — the user asks for 500, gets 200, and cannot see why. With the flag,
    the UI can state plainly that more results require a key.
    """
    out, cursor, count = [], "*", 0
    capped = False
    fstr = _filter_str(filters)
    while len(out) < want:
        params = {
            "filter": fstr,
            "per-page": min(200, want - len(out)),
            "cursor": cursor,
            "select": _SELECT,
        }
        if sort:
            params["sort"] = sort
        if cfg.get("email"):
            params["mailto"] = cfg["email"]
        if cfg.get("openalex_api_key"):
            params["api_key"] = cfg["openalex_api_key"]
        r = sess.get("https://api.openalex.org/works", params=params)
        if r.status_code == 403 and "key_not_activated" in r.text:
            # ! Don't hardcode any email -- this hint is seen by whoever else runs the program
            who = (cfg.get("email") or "").strip()
            box = f"{who} 's inbox" if who else "The email address you entered when requesting the key"
            raise QuotaExceeded(
                f"The OpenAlex key has not been activated yet.\n"
                f"Go to{box}and find an email titled \"Activate your OpenAlex API key\", then click the button in it."
                "Nothing else to change once activated — just restart the program.\n"
                "(If it does not arrive, check your spam folder, or resend from https://openalex.org/settings/api)")
        if r.status_code == 401 and "cursor" in r.text.lower():
            # The same error has two causes, tell them apart -- otherwise people with a key are baffled
            if cfg.get("openalex_api_key"):
                # Rejected even with a key: the key itself is the problem, don't just degrade, say so
                raise QuotaExceeded(
                    "OpenAlex rejected the current API key (it may have expired, or today's free quota is used up).\n"
                    "Two options:\n"
                    "  1) Check the key status at https://openalex.org/settings/api, or switch to another account key;\n"
                    "  2) Temporarily clear openalex_api_key in config.json — the tool still works,"
                    "but each run can fetch at most the top 200 records (OpenAlex does not serve page 2).")
            # No key: **don't raise**, return the page already fetched and mark it capped.
            # Raising would hide even the first 200 records, worse than "a bit less but explained".
            capped = True
            break
        if r.status_code != 200:
            raise RuntimeError(f"OpenAlex {r.status_code}: {r.text[:200]}")
        data = r.json()
        count = (data.get("meta") or {}).get("count", count)
        for w in data.get("results") or []:
            out.append(_to_rec(w))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or not (data.get("results") or []):
            # More hits exist but no next page and no key -- that's the "paging needs a key" wall
            if len(out) < want and count > len(out) and not cfg.get("openalex_api_key"):
                capped = True
            break
    return out[:want], count, capped


def search_openalex(q: str, cfg: dict, sess: PoliteSession, *,
                    limit: int = 50, year_from: int | None = None,
                    year_to: int | None = None, oa_only: bool = False,
                    issn: str | None = None, default_field: str = "all",
                    default_op: str = "AND", sort: str = "relevance",
                    doc_type: str = "article",
                    info: dict | None = None) -> tuple[list[dict], int]:
    """
    Search OpenAlex. q accepts boolean syntax:

        shrimp AND aroma                    both terms required (default fields)
        shrimp AND (aroma OR flavor)        grouped with parentheses
        title:shrimp AND abstract:aroma     field qualifier
        "lipid oxidation" AND shrimp        quoted phrase
        shrimp AND NOT fish  /  shrimp -fish  exclusion

    Multiple OR branches are split into several requests and then merged with de-duplication (interleaved merge, preserving relevance).
    Pass a dict as info to get the parse details back.
    """
    base = []
    if year_from:
        base.append(("from_publication_date", f"{year_from}-01-01"))
    if year_to:
        base.append(("to_publication_date", f"{year_to}-12-31"))
    if oa_only:
        base.append(("is_oa", "true"))
    if issn:
        base.append(("primary_location.source.issn", issn))
    # Work type. It used to be hardcoded type:article -- reviews (type:review) and dissertations were unfindable
    dt_key = (doc_type or "article").strip().lower()
    dt_val, dt_label = DOC_TYPES.get(dt_key, DOC_TYPES["article"])
    if dt_key not in DOC_TYPES:
        dt_key = "article"
    if dt_val:
        base.append(("type", dt_val))

    sort_key, sort_label, sort_global = SORTS.get(sort, SORTS["relevance"])

    # Given a DOI / OpenAlex ID / PMID: look it up directly, don't split it into text (returns unrelated results)
    ident = parse_identifier(q)
    if ident:
        kind, val = ident
        rec, why = _lookup_work(kind, val, cfg, sess)
        if rec is None and kind == "doi":
            rec = _crossref_rec(val)          # Fall back to Crossref when OpenAlex has nothing
        if rec is None:
            extra = "" if why in ("notfound", "") else f"({why})"
            raise QueryError(f"Could not find {kind.upper()} = {val}{extra}, check whether it was copied correctly")
        y = rec.get("year")
        if year_from and y and y < year_from:
            raise QueryError(f"This paper is from {y} and was filtered out by \"start year {year_from}\"", "filtered")
        if year_to and y and y > year_to:
            raise QueryError(f"This paper is from {y} and was filtered out by \"end year {year_to}\"", "filtered")
        if oa_only and not rec.get("is_oa"):
            raise QueryError("This record is not OA and was filtered out by \"OA only\"", "filtered")
        rt = rec.get("work_type") or ""
        if rt and dt_key != "all" and rt not in dt_val.split("|"):
            raise QueryError(
                f"This paper is \"{TYPE_LABEL.get(rt, rt)}\" and was filtered out by the current \"document type:{dt_label}\"."
                f"Change the type to \"Any type\" and try again.", "filtered")
        if info is not None:
            info.update({
                "identifier": kind, "ident_value": val,
                "expr": f"{kind.upper()} {val}", "n_groups": 0,
                "field": default_field,
                "field_label": FIELD_LABEL.get(default_field, default_field),
                "op": default_op, "filters": [], "has_neg": False,
                "has_wild": False, "sort": sort, "sort_label": sort_label,
                "via": rec.get("found_via", ""),
            })
        return [rec], 1

    # When a whole title is pasted, first try it as one phrase: usually one shot hits.
    # If not, fall back to word-by-word AND, so the worst case is as before, never worse.
    if _looks_like_title(q):
        ph = _phrase_of(q)
        if ph:
            pkey = FIELD_MAP.get(default_field, "default.search")
            pfil = base + [(pkey, '"' + ph + '"')]
            try:
                recs, total, _capped = _fetch_group(pfil, cfg, sess, limit, sort_key)
            except QuotaExceeded:
                raise
            except Exception:
                recs, total, _capped = [], 0, False
            if recs:
                if info is not None:
                    info.update({
                        "capped_no_key": bool(_capped),
                        "phrase": True, "n_groups": 1, "groups": 1,
                        "expr": '"' + ph + '"',
                        "field": default_field,
                        "field_label": FIELD_LABEL.get(default_field, default_field),
                        "op": "AND", "filters": [_filter_str(pfil)],
                        "has_neg": False, "has_wild": False,
                        "sort": sort, "sort_label": sort_label,
                    })
                return recs, total

    node = parse_bool(q, default_op)
    groups = merge_groups(build_groups(node, default_field))
    if len(groups) > MAX_GROUPS:
        raise QueryError(
            "Even after merging, this query still expands to %d requests, over the limit of %d."
            "Try putting synonyms in the same group with OR (e.g. aroma OR flavor)"
            ", or reduce the number of parenthesized branches." % (len(groups), MAX_GROUPS))

    def full(g):
        return base + g["filters"] + [(k, "!" + v) for k, v in g["negs"]]

    if info is not None:
        info["n_groups"] = len(groups)
        info["expr"] = expr_text(node, default_field)
        info["field"] = default_field
        info["field_label"] = FIELD_LABEL.get(default_field, default_field)
        info["op"] = default_op
        info["filters"] = [_filter_str(full(g)) for g in groups]
        info["has_neg"] = any(g["negs"] for g in groups)
        info["sort"] = sort
        info["sort_label"] = sort_label
        info["doc_type"] = dt_key
        info["doc_type_label"] = dt_label
        info["has_wild"] = any("*" in v or "?" in v
                               for g in groups for _, v in g["filters"])

    if len(groups) == 1:
        recs, total, capped = _fetch_group(full(groups[0]), cfg, sess, limit, sort_key)
        if capped and info is not None:
            # Only report that it was capped, not the count -- after de-duplicating branches it doesn't match a single group
            info["capped_no_key"] = True
        return recs, total

    # Multiple branches: fetch each group, then de-duplicate
    buckets, total, capped_any = [], 0, False
    for g in groups:
        recs, cnt, capped = _fetch_group(full(g), cfg, sess, limit, sort_key)
        total += cnt
        capped_any = capped_any or capped
        buckets.append(recs)
    if capped_any and info is not None:
        info["capped_no_key"] = True

    def dedup(seq):
        seen, out = set(), []
        for r in seq:
            key = (r.get("oaid") or "").lower() or (r.get("doi") or "").lower() \
                or (r.get("title") or "").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    if sort_global:
        # Fields like citations/year are comparable across groups: de-duplicate first, then re-rank everything
        out = dedup([r for b in buckets for r in b])
        field, rev = _SORT_FIELD[sort]
        out.sort(key=lambda r: (r.get(field) or 0), reverse=rev)
        return out[:limit], total

    # Relevance: scores aren't comparable across groups, so interleave "1st of each, 2nd of each, ..."
    out = []
    depth = max((len(b) for b in buckets), default=0)
    for i in range(depth):
        for b in buckets:
            if i < len(b):
                out.append(b[i])
        out = dedup(out)
        if len(out) >= limit:
            return out[:limit], total
    return out[:limit], total

def enrich_unpaywall(rec: dict, cfg: dict, sess: PoliteSession) -> None:
    """Fetch a direct OA full-text link. Unpaywall is an OA index authorized by rights holders, not scraping."""
    doi = rec.get("doi")
    if not doi or not cfg.get("email"):
        return
    try:
        r = sess.get(f"https://api.unpaywall.org/v2/{doi}",
                     params={"email": cfg["email"]})
    except Exception:
        return
    if r.status_code != 200:
        return
    d = r.json()
    rec["is_oa"] = bool(d.get("is_oa"))
    rec["oa_status"] = d.get("oa_status") or rec.get("oa_status", "")
    locs = [l for l in ([d.get("best_oa_location")] + (d.get("oa_locations") or [])) if l]
    # Prefer repository copies: institutional repositories / Zenodo are friendly to programmatic access, publisher pages often sit behind a WAF
    locs.sort(key=lambda l: 0 if l.get("host_type") == "repository" else 1)
    seen = set()
    for loc in locs:
        u = loc.get("url_for_pdf") or ""
        if not u and (loc.get("url") or "").lower().split("?")[0].endswith(".pdf"):
            u = loc["url"]
        if not u or u in seen:
            continue
        if is_forbidden(u):
            rec["_oa_needs_api"] = True
            continue
        seen.add(u)
        rec.setdefault("pdf_candidates", []).append(u)
        if not rec.get("license"):
            rec["license"] = loc.get("license") or ""
        if len(seen) >= 3:
            break


def resolve_arxiv(rec: dict) -> None:
    """arXiv preprint: CC licensed, official direct PDF link, programmatic access explicitly allowed."""
    doi = rec.get("doi") or ""
    m = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.I)
    if m:
        rec.setdefault("pdf_candidates", []).append(
            f"https://arxiv.org/pdf/{m.group(1)}.pdf")


def resolve_europepmc(rec: dict, sess: PoliteSession) -> None:
    """Look up the PMCID via Europe PMC; PMC OA Service is the NIH-authorized full-text index."""
    doi = rec.get("doi")
    if not doi:
        return
    try:
        r = sess.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                     params={"query": f'DOI:"{doi}"', "format": "json",
                             "resultType": "core", "pageSize": 1})
        if r.status_code != 200:
            return
        hits = (r.json().get("resultList") or {}).get("result") or []
        if not hits:
            return
        pmcid = hits[0].get("pmcid") or ""
        rec["pmcid"] = pmcid
        if not pmcid:
            return
        oa = sess.get("https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
                      params={"id": pmcid})
        if oa.status_code == 200:
            for m in re.finditer(r'href="([^"]+)"', oa.text):
                u = m.group(1)
                if u.lower().endswith(".pdf") and not is_forbidden(u):
                    rec.setdefault("pdf_candidates", []).append(u)
    except Exception:
        return


def resolve_elsevier_api(rec: dict, cfg: dict, sess: PoliteSession) -> None:
    """Elsevier official Article Retrieval API — the only authorized path for programmatic full-text retrieval."""
    doi = rec.get("doi") or ""
    if not doi.startswith("10.1016") or not cfg.get("elsevier_api_key"):
        return
    h = {"X-ELS-APIKey": cfg["elsevier_api_key"], "Accept": "application/pdf"}
    if cfg.get("elsevier_insttoken"):
        h["X-ELS-Insttoken"] = cfg["elsevier_insttoken"]
    rec["_elsevier_headers"] = h
    rec["elsevier_ready"] = True


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _slug(s: str, n: int = 55) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return (s[:n].rstrip("_") or "")


def surname(first_author: str) -> str:
    """
    Extract the surname from first_author. Both formats must be handled:
      · "Y. Zhang"   → Zhang   (the OpenAlex format, also the most common)
      · "Zhang, Y."  → Zhang   (Zotero / Crossref / citation expansion may return this)
    The old code only took split()[-1], which yields "Y." for the comma format and names files 2024_Y_xxx.pdf.
    """
    a = (first_author or "").strip()
    if not a:
        return ""
    if "," in a:
        return a.split(",")[0].strip()
    parts = a.split()
    return parts[-1] if parts else ""


def target_name(rec: dict, ext: str = ".pdf") -> str:
    y = rec.get("year") or "nd"
    au = _slug(surname(rec.get("first_author")) or "anon", 18)
    t = _slug(rec.get("title") or "", 55)
    if not t:
        t = _slug((rec.get("doi") or "nodoi").replace("/", "_").replace("10.", ""), 40)
    return f"{y}_{au}_{t}{ext}"


def is_pdf(blob: bytes) -> bool:
    return blob[:5] == b"%PDF-" or b"%PDF-" in blob[:1024]


def pdf_page_count(blob: bytes) -> int:
    """PDF page count; returns 0 if parsing fails."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(blob), strict=False).pages)
    except Exception:
        return 0


def elsevier_full_text(doi: str, headers: dict, sess: "PoliteSession") -> str:
    """Fetch the full-text XML (available once the TDM clause is accepted); returns an empty string on failure."""
    try:
        h = dict(headers); h["Accept"] = "text/xml"
        r = sess.get(f"https://api.elsevier.com/content/article/doi/{doi}",
                     params={"view": "FULL"}, headers=h)
        if r.status_code == 200 and "<article" in r.text[:3000]:
            return r.text
    except Exception:
        pass
    return ""


# Storage format: how to store full text when a complete PDF isn't available
SAVE_FORMATS = {
    "pdf": {"label": "Full PDF",
              "desc": "Can be printed, annotated, and quoted in screenshots",
              "cost": "Largest footprint: 2–6 MB per article, about 5.8 GB for 1,000 articles"},
    "xml": {"label": "Full-text XML",
              "desc": "Body text, sections, figure captions, and references complete; opens directly in Notepad",
              "cost": "About 780 KB per article, about 780 MB for 1,000 articles"},
    "xmlgz": {"label": "Compressed XML",
              "desc": "Identical in content to the XML and readable by AI directly; humans must unzip it first",
              "cost": "About 80 KB per article, about 80 MB for 1,000 articles"},
}
DEFAULT_SAVE_FORMAT = "xmlgz"


def save_format(cfg: dict) -> str:
    fmt = (cfg.get("save_format") or DEFAULT_SAVE_FORMAT).lower()
    return fmt if fmt in SAVE_FORMATS else DEFAULT_SAVE_FORMAT


def download_one(rec: dict, cfg: dict, sess: PoliteSession,
                 out_dir: Path) -> tuple[str, Path | None, str]:
    """
    Returns (status, path, source). Status: ok / preview / skip_dup / no_source.

    The storage format is set by save_format in config (pdf / xml / xmlgz).
    pdf is saved only when a real multi-page PDF is obtained; the xml variants store the Elsevier full-text XML.
    keep_preview_pdf controls whether the first-page image is also saved when only the first page is available.
    """
    fmt = save_format(cfg)
    keep_prev = bool(cfg.get("keep_preview_pdf"))
    base = target_name(rec, "")

    # Any form already present counts as present, don't download twice
    for ext in (".pdf", ".fulltext.xml", ".fulltext.xml.gz"):
        p = out_dir / (base + ext)
        if p.exists() and p.stat().st_size > 5_000:
            return "skip_dup", p, "existing"

    def save_xml(xml: str, src: str) -> tuple[Path, str]:
        if fmt == "xmlgz":
            p = out_dir / (base + ".fulltext.xml.gz")
            with gzip.open(p, "wb", compresslevel=9) as f:
                f.write(xml.encode("utf-8"))
            return p, src + "-xml.gz"
        p = out_dir / (base + ".fulltext.xml")
        p.write_text(xml, encoding="utf-8")
        return p, src + "-xml"

    def save_pdf(blob: bytes, src: str) -> tuple[Path, str]:
        p = out_dir / (base + ".pdf")
        p.write_bytes(blob)
        return p, src + "-pdf"

    # 1) Elsevier official API (legal, publisher original)
    if rec.get("elsevier_ready") and rec.get("doi"):
        url = f"https://api.elsevier.com/content/article/doi/{rec['doi']}"
        try:
            r = sess.get(url, params={"httpAccept": "application/pdf"},
                         headers=rec["_elsevier_headers"])
            if r.status_code == 200 and is_pdf(r.content):
                pages = pdf_page_count(r.content)
                whole = pages > 1

                # PDF chosen and the whole file really came back -> save it
                if whole and fmt == "pdf":
                    p, via = save_pdf(r.content, "elsevier")
                    return "ok", p, via

                xml = elsevier_full_text(rec["doi"], rec["_elsevier_headers"], sess)
                if xml:
                    p, via = save_xml(xml, "elsevier")
                    if whole:
                        rec["_els_note"] = (
                            f"Saved full-text XML (the PDF has {pages} pages and, per \"{SAVE_FORMATS[fmt]['label']}”"
                            f"\" setting, only the text was kept)")
                    else:
                        rec["_els_note"] = (
                            "PDF has only the first page (no off-campus full-text access); full-text XML saved instead"
                            + ("+ front-page image" if keep_prev else ""))
                    if keep_prev and not whole:
                        (out_dir / (base + ".preview.pdf")).write_bytes(r.content)
                    return "ok", p, via

                # No XML available, fall back to PDF
                if whole:
                    p, via = save_pdf(r.content, "elsevier")
                    rec["_els_note"] = "No full-text XML for this journal; saved the PDF instead"
                    return "ok", p, via
                if keep_prev:
                    prev = out_dir / (base + ".preview.pdf")
                    prev.write_bytes(r.content)
                    rec["_els_note"] = f"First page only ({pages} pages), and XML could not be retrieved"
                    return "preview", prev, "elsevier-preview"
                rec["_els_note"] = "First page only and no XML available; not saved"
            elif r.status_code in (401, 403):
                rec["_els_note"] = (
                    "This publisher did not grant full-text access — either the current IP is outside the institutional entitlement (just rerun on the campus network / campus VPN,"
                    "on campus only an API key is needed, an insttoken is not required), or the institution does not subscribe to this journal; try the open channels instead")
        except PermissionError:
            pass
        except Exception as e:
            rec["_els_note"] = str(e)[:120]

    # 2) OA routes: no XML there, PDF only
    license_ok = (rec.get("license") or "").lower() in (
        "cc-by", "cc0", "public-domain", "other-oa")
    for u in rec.get("pdf_candidates") or []:
        if not u or u.lower().endswith(".tar.gz"):
            continue
        verdict = sess.robots.check(u)
        if verdict is False:
            sess.stats["robots_blocked"] += 1
            rec.setdefault("_notes", []).append(f"robots.txt disallows ({urlparse(u).hostname})")
            continue
        if verdict is None and not license_ok:
            rec.setdefault("_notes", []).append(f"license unclear ({urlparse(u).hostname})")
            continue
        try:
            r = sess.get(u)
        except PermissionError as e:
            rec.setdefault("_notes", []).append(f"skipped ({e})")
            continue
        except Exception as e:
            rec.setdefault("_notes", []).append(f"{type(e).__name__}")
            continue
        if r.status_code == 200 and is_pdf(r.content):
            p, via = save_pdf(r.content, urlparse(u).hostname or "oa")
            return "ok", p, via
        rec.setdefault("_notes", []).append(f"HTTP {r.status_code}")
    return "no_source", None, ""


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)


def _default_library() -> Path:
    """
    The default library.

    After packaging this is the library folder next to the exe.
    When running from source (python paperfetch_ui.py), app_dir() is the "source" directory;
    if a library folder already exists one level up (i.e. the packaged one), use that —
    so an empty library does not appear inside the source tree that is not the same library as the one outside.
    """
    base = app_dir()
    if not getattr(sys, "frozen", False):
        outer = base.parent / "library"
        if outer.is_dir():
            return outer
    return base / "library"


def library_dir(cfg: dict) -> Path:
    """
    Library directory. When library_dir in config is empty, use "<app dir>/library".

    Path only, **no directory is created** — it used to mkdir on every call, so merely opening the UI conjured an empty folder out of nowhere.
    Code that actually writes files (download, list generation, index writing) does its own mkdir.
    """
    custom = (cfg.get("library_dir") or "").strip()
    return Path(custom) if custom else _default_library()


def _doi_from_name(name: str) -> str:
    m = DOI_RE.search(name)
    return m.group(0).rstrip(".,;)]}") if m else ""


def _doi_from_pdf(path: Path) -> tuple[str, str]:
    """Read the first two pages and extract the DOI and title. Returns an empty string on failure and never raises."""
    try:
        from pypdf import PdfReader
        r = PdfReader(str(path))
        text = ""
        for pg in r.pages[:2]:
            text += (pg.extract_text() or "") + "\n"
            if len(text) > 20000:
                break
    except Exception:
        return "", ""
    m = DOI_RE.search(text)
    doi = m.group(0).rstrip(".,;)]}") if m else ""
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 12]
    title = " ".join(lines[:2])[:180] if lines else ""
    return doi, title


# Files recognized in the library: downloads default to xmlgz, so .xml.gz is the main one
LIB_SUFFIXES = (".pdf", ".xml", ".xml.gz")


def _is_lib_file(name: str) -> bool:
    return name.lower().endswith(LIB_SUFFIXES)


def _plain_name(name: str) -> str:
    """The name with .pdf / .xml / .xml.gz stripped, used as a title fallback."""
    low = name.lower()
    for suf in (".xml.gz", ".xml", ".pdf"):
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


def _doi_from_xml(path: Path, head_kb: int = 320) -> tuple[str, str]:
    """Extract the DOI and title from JATS / Elsevier full-text XML. Reads only the head of the file, never loads the whole document."""
    try:
        if path.name.lower().endswith(".gz"):
            with gzip.open(path, "rb") as f:
                blob = f.read(head_kb * 1024)
        else:
            with path.open("rb") as f:
                blob = f.read(head_kb * 1024)
    except Exception:
        return "", ""
    text = blob.decode("utf-8", "ignore")
    doi = ""
    m = re.search(r'<article-id[^>]*pub-id-type="doi"[^>]*>\s*([^<\s]+)\s*<', text, re.I)
    if m:
        doi = m.group(1).strip()
    if not doi:
        m = DOI_RE.search(text)
        doi = m.group(0).rstrip(".,;)]}") if m else ""
    title = ""
    m = re.search(r"<article-title[^>]*>(.*?)</article-title>", text, re.S | re.I)
    if m:
        title = _strip_tags(re.sub(r"\s+", " ", m.group(1)))[:180]
    return doi, title


def _library_roots(cfg: dict) -> list[Path]:
    roots = [library_dir(cfg)]
    for extra in (cfg.get("extra_sources") or []):
        if extra and Path(extra).is_dir():
            roots.append(Path(extra))
    return roots


def scan_library(cfg: dict, force: bool = False) -> dict:
    """
    Recursively scan the library and the extra sources for PDF / XML / compressed XML, extract DOIs and build the index.
    The index is cached in library/.index.json and re-parsed only when (size, mtime) says it is needed.
    """
    idx_file = library_dir(cfg) / ".index.json"
    cache: dict = {}
    if idx_file.exists() and not force:
        try:
            cache = json.loads(idx_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    items, seen, dup, parsed, reused = [], {}, 0, 0, 0
    total = 0
    for root in _library_roots(cfg):
        files = [x for x in root.rglob("*") if x.is_file() and _is_lib_file(x.name)]
        for p in sorted(files, key=lambda x: str(x).lower()):
            if p.name.startswith("."):
                continue
            total += 1
            try:
                st = p.stat()
            except OSError:
                continue
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            c = cache.get(str(p))
            if c and c.get("sig") == sig:
                md5v, doi, title = c.get("md5", ""), c.get("doi", ""), c.get("title", "")
                reused += 1
            else:
                try:
                    md5v = md5_of(p)
                except OSError:
                    continue
                doi = _doi_from_name(p.name)
                title = ""
                if not doi:
                    if p.name.lower().endswith(".pdf"):
                        doi, title = _doi_from_pdf(p)
                    else:
                        doi, title = _doi_from_xml(p)
                    parsed += 1
                else:
                    title = _plain_name(p.name)
            cache[str(p)] = {"sig": sig, "md5": md5v, "doi": doi, "title": title}
            # One paper may have both PDF and XML (different md5), de-duplicating by DOI makes more sense
            key = doi.lower() if doi else md5v
            if key in seen:
                dup += 1
                continue
            seen[key] = str(p)
            # Where the title comes from: in XML it's the real title (JATS metadata); what's pulled from a PDF is often the header,
            # so our own named PDFs use the filename (year_author_title), only Zotero-like ones use the extracted text
            plain = _plain_name(p.name)
            if p.name.lower().endswith(".pdf"):
                disp = plain if re.match(r"^\d{4}_", p.name) else (title or plain)
            else:
                disp = title or plain
            items.append({
                "file": str(p),
                "name": p.name,
                "size_kb": round(st.st_size / 1024),
                "mtime": int(st.st_mtime),
                "doi": doi,
                "title": disp,
                "folder": str(p.parent),
            })

    if idx_file.parent.exists():
        try:
            idx_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    items.sort(key=lambda x: x["name"].lower())
    return {
        "roots": [str(r) for r in _library_roots(cfg)],
        "library": str(library_dir(cfg)),
        "total_files": total,
        "unique": len(items),
        "duplicates": dup,
        "with_doi": sum(1 for i in items if i["doi"]),
        "parsed_this_run": parsed,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
FIELDS = ["doi", "title", "journal", "year", "first_author", "n_authors",
          "cited_by", "is_oa", "oa_status", "license", "pmcid", "pdf_url",
          "landing", "found_via", "abstract"]


def write_csv(recs: list[dict], path: Path, fields=None) -> None:
    fields = fields or FIELDS
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow({k: r.get(k, "") for k in fields})


MANIFEST_EXTRA = ["zh_title", "impact_if", "cas_zone", "jcr_zone"]


def write_manifest(manifest: list[dict], path: Path) -> None:
    """
    Download manifest: what was downloaded for each record, which route was taken, and whether it succeeded.

    Two rules:
    1. **Accumulate, do not overwrite** — repeated downloads into the same directory merge the new records into the existing manifest (deduplicated by DOI, or by title when there is no DOI),
       otherwise the second run would wipe out the first run's bookkeeping.
    2. **A successful record is never overwritten by a later skip_dup** — the file was already there and must not be counted as "not downloaded".
       At the same time carry over the Chinese title / IF / quartile already in the local cache, **reading the cache only, never going online just to write the manifest**.
    """
    fields = FIELDS + MANIFEST_EXTRA + ["status", "via"]

    def key_of(d: dict) -> str:
        return ((d.get("doi") or "").strip().lower()
                or (d.get("title") or "").strip().lower())

    rows: list[dict] = []
    pos: dict[str, int] = {}
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for old in csv.DictReader(f):
                    k = key_of(old)
                    if k and k not in pos:
                        pos[k] = len(rows)
                        rows.append({x: old.get(x, "") for x in fields})
        except Exception:
            rows, pos = [], {}

    for m in manifest:
        k = key_of(m)
        if k and k in pos:
            prev = rows[pos[k]]
            if prev.get("status") in ("ok", "preview", "skip_dup") and m.get("status") == "skip_dup":
                continue                       # Downloaded before, don't log it as "skipped"
            rows[pos[k]] = m
        else:
            if k:
                pos[k] = len(rows)
            rows.append(m)

    zh, jn = {}, {}
    try:
        import enrich as _en          # Lazy import: only borrowing its cache, a missing file doesn't affect downloads
        zh, jn = _en.cached_extras([m.get("title", "") for m in rows],
                                   [m.get("journal", "") for m in rows],
                                   _en.Cache(app_dir()))
    except Exception:
        pass
    for m in rows:
        if not m.get("zh_title"):
            m["zh_title"] = zh.get(m.get("title") or "", "")
        j = jn.get((m.get("journal") or "").strip().lower()) or {}
        if not m.get("impact_if"):
            m["impact_if"] = j.get("if", "")
        if not m.get("jcr_zone"):
            m["jcr_zone"] = j.get("jcr", "")
        if not m.get("cas_zone"):
            cas = j.get("cas", "")
            m["cas_zone"] = (cas + (" Top" if j.get("top") else "")) if cas else ""
    write_csv(rows, path, fields=fields)


def write_xlsx(recs: list[dict], path: Path) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "results"
    ws.append(FIELDS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4F6228")
        c.alignment = Alignment(horizontal="center")
    for r in recs:
        ws.append([r.get(k, "") for k in FIELDS])
    widths = {"doi": 34, "title": 60, "journal": 30, "abstract": 70, "landing": 30}
    for i, k in enumerate(FIELDS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(k, 12)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return True


def write_bib(recs: list[dict], path: Path) -> None:
    lines = []
    for r in recs:
        key = (_slug(surname(r.get("first_author")) or "anon", 20) or "anon") + str(r.get("year") or "")
        key += _slug((r.get("title") or "")[:1], 4) or "x"
        lines.append(f"@article{{{key},")
        lines.append(f"  title   = {{{r.get('title', '')}}},")
        if r.get("journal"):
            lines.append(f"  journal = {{{r['journal']}}},")
        if r.get("year"):
            lines.append(f"  year    = {{{r['year']}}},")
        if r.get("doi"):
            lines.append(f"  doi     = {{{r['doi']}}},")
        lines.append("}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_ris(recs: list[dict], path: Path) -> None:
    """RIS: drag it into Zotero / EndNote to create items, then use "Find Available PDF" to fetch the full texts in bulk."""
    out = []
    for r in recs:
        out.append("TY  - JOUR")
        if r.get("title"):
            out.append(f"TI  - {r['title']}")
        if r.get("journal"):
            out.append(f"JO  - {r['journal']}")
        if r.get("year"):
            out.append(f"PY  - {r['year']}")
        if r.get("first_author"):
            out.append(f"AU  - {r['first_author']}")
        if r.get("doi"):
            out.append(f"DO  - {r['doi']}")
        if r.get("landing"):
            out.append(f"UR  - {r['landing']}")
        out.append("ER  - ")
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")


_MISS_CSS = """
:root{--ink:#22231f;--ink2:#5d5f57;--ink3:#8b8d83;--line:#e3e4dc;--acc:#4c6b1f}
*{box-sizing:border-box}
body{margin:0;padding:28px 32px 60px;font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     color:var(--ink);background:#fbfbf8}
h1{font-size:19px;margin:0 0 6px}
.sub{color:var(--ink3);font-size:12.5px;margin-bottom:18px}
.tip{background:#fff;border:1px solid var(--line);border-left:3px solid var(--acc);
     border-radius:8px;padding:12px 15px;margin-bottom:18px;font-size:13px;color:var(--ink2)}
.tip b{color:var(--ink)}
.tip ol{margin:6px 0 0;padding-left:20px}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:12px}
button{border:1px solid var(--acc);background:var(--acc);color:#fff;border-radius:6px;
       padding:6px 14px;font:inherit;font-size:12.5px;cursor:pointer;white-space:nowrap}
button.ghost{background:#fff;color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;
      border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{background:#f1f3e9;text-align:left;padding:9px 10px;font-size:12px;color:var(--ink2);
   border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #f0f0ea;vertical-align:top}
tr:last-child td{border-bottom:0}
tr:hover td{background:#fcfcf7}
.n{color:var(--ink3);font-variant-numeric:tabular-nums}
.batch{background:#f7f8f2;border:1px solid #dfe3d2;border-radius:8px;padding:10px 12px;
       margin:0 0 14px;font-size:13px;line-height:1.7}
.batch ol{margin:6px 0 0;padding-left:20px}
.batch li{margin:3px 0}
.batch a{color:#3d5c8a;text-decoration:none}
.batch a:hover{text-decoration:underline}
td.ck{width:36px;text-align:center;padding-right:2px}
td.ck input{width:15px;height:15px;accent-color:var(--acc);cursor:pointer;margin:0}
tr.done td:not(.ck){opacity:.42}
tr.done td.t a{text-decoration:line-through}
tr.done td.ck{opacity:1}
#cnt{font-size:12.5px;color:var(--ink2);font-variant-numeric:tabular-nums;margin-left:auto}
button.ghost.on{background:var(--acc);color:#fff}
.pub{display:inline-block;font-size:11px;padding:1px 7px;border-radius:9px;
     background:#eef1e4;color:var(--acc);white-space:nowrap}
.pub.sub{background:#fdf0ec;color:#a8452a}
.pub.oa{background:#e7f3e8;color:#3f7a45}
.pub.api{background:#eef2f9;color:#3d5c8a}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.doi{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--ink2)}
"""

_MISS_JS = """
var KEY = 'miaomiao_missing_v1';
function loadSt(){ try{ return JSON.parse(localStorage.getItem(KEY) || '{}'); }catch(e){ return {}; } }
function saveSt(s){ try{ localStorage.setItem(KEY, JSON.stringify(s)); }catch(e){} }
function rk(tr){ return tr.getAttribute('data-k') || ''; }
function paint(){
  var st = loadSt(), done = 0, n = 0;
  [].forEach.call(document.querySelectorAll('tbody tr'), function(tr){
    n++;
    var on = !!st[rk(tr)];
    tr.classList.toggle('done', on);
    var ck = tr.querySelector('input[type=checkbox]');
    if(ck) ck.checked = on;
    if(on) done++;
  });
  var c = document.getElementById('cnt');
  if(c) c.textContent = 'Done ' + done + ' / ' + n;
  var f = document.getElementById('only');
  if(f && f.classList.contains('on')) filterRows(true);
}
function setDone(tr, on){
  var st = loadSt(), k = rk(tr);
  if(!k) return;
  if(on){ st[k] = 1; } else { delete st[k]; }
  saveSt(st); paint();
}
function filterRows(on){
  [].forEach.call(document.querySelectorAll('tbody tr'), function(tr){
    tr.style.display = (on && tr.classList.contains('done')) ? 'none' : '';
  });
}
function onlyTodo(btn){
  var on = btn.classList.toggle('on');
  btn.textContent = on ? 'Show all' : 'Only not downloaded';
  filterRows(on);
}
function clearAll(){
  if(!confirm('Clear all checkmarks? Records already downloaded will be lost too.')) return;
  saveSt({}); paint();
}
function batchNext(){
  var box = document.getElementById('batch');
  var todo = [].filter.call(document.querySelectorAll('tbody tr'), function(tr){
    return !tr.classList.contains('done');
  });
  box.innerHTML = '';
  if(!todo.length){
    box.textContent = 'All done, nothing left to download.';
    box.hidden = false; return;
  }
  var take = todo.slice(0, 5);
  var head = document.createElement('b');
  head.textContent = 'Next ' + take.length + ' items';
  var tip = document.createElement('span');
  tip.style.cssText = 'color:#6a7a4a;margin-left:4px';
  tip.textContent = '(Hold Ctrl while clicking to open several tabs at once)';
  box.appendChild(head); box.appendChild(tip);
  box.appendChild(document.createTextNode(': '));
  var ol = document.createElement('ol');
  take.forEach(function(tr){
    var a = tr.querySelector('a.tl');
    var li = document.createElement('li');
    var href = a ? (a.getAttribute('href') || '') : '';
    if(href){
      var link = document.createElement('a');
      link.href = href; link.target = '_blank'; link.rel = 'noopener';
      link.textContent = a.textContent; // use textContent so < and & in titles are not treated as markup
      li.appendChild(link);
    }else{
      var span0 = document.createElement('span');
      span0.textContent = a ? a.textContent : '(no link)';
      span0.style.color = '#999';
      li.appendChild(span0);
    }
    var cp = tr.querySelector('td.cp');
    var doi = (cp && cp.dataset) ? (cp.dataset.doi || '') : '';
    if(doi){
      var sp = document.createElement('span');
      sp.style.cssText = 'color:#999;font-size:11px;margin-left:6px';
      sp.textContent = doi;
      li.appendChild(sp);
    }
    ol.appendChild(li);
  });
  box.appendChild(ol);
  box.hidden = false;
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function copyAll(){
  var s=[].map.call(document.querySelectorAll('td.cp'),function(t){return t.dataset.doi});
  var txt=s.filter(Boolean).join('\\n');
  navigator.clipboard.writeText(txt).then(function(){
    var b=document.getElementById('cp'); b.textContent='Copied '+s.filter(Boolean).length+' items'; 
    setTimeout(function(){b.textContent='Copy all DOIs'},1800);
  });
}
function copyOne(btn){
  navigator.clipboard.writeText(btn.dataset.doi).then(function(){
    var t=btn.textContent; btn.textContent='Copied'; 
    setTimeout(function(){btn.textContent=t},1200);
  });
}
document.addEventListener('change', function(ev){
  var ck = ev.target;
  if(ck.type === 'checkbox' && ck.closest('tr')) setDone(ck.closest('tr'), ck.checked);
});
document.addEventListener('click', function(ev){
  var a = ev.target.closest ? ev.target.closest('a.tl') : null;
  if(!a) return;
  var tr = a.closest('tr');
  if(tr && !tr.classList.contains('done')) setDone(tr, true); // clicking a title counts as handled
});
paint();
"""


def write_missing_html(recs: list[dict], path: Path, fmt_label: str = "") -> None:
    """
    For the batch that nobody provided full text for, generate a list that can be clicked through directly on the campus network.
    No page fetching and no auth circumvention — it just lines the DOIs up; clicking through lands on the publisher's own page.
    """
    from html import escape as e

    counts: dict[str, int] = {}
    rows = []
    for i, r in enumerate(recs, 1):
        pub, kind = publisher_of(r.get("doi", ""))
        counts[pub or "Unknown"] = counts.get(pub or "Unknown", 0) + 1
        doi = (r.get("doi") or "").strip()
        link = f"https://doi.org/{doi}" if doi else (r.get("landing") or "")
        cls = kind if kind in ("sub", "oa", "api") else ""
        key = doi.lower() if doi else "t:" + (r.get("title") or "")[:80].lower()
        rows.append(
            f'<tr data-k="{e(key)}">'
            f'<td class="ck"><input type="checkbox" aria-label="Mark as downloaded"></td>'
            f'<td class="n">{i}</td>'
            f'<td class="t"><a class="tl" href="{e(link)}" target="_blank" rel="noopener">'
            f'{e(r.get("title") or "(untitled)")}</a>'
            f'<div class="doi">{e(doi or "—")}</div></td>'
            f'<td>{e(r.get("journal") or "—")}</td>'
            f'<td>{e(str(r.get("year") or ""))}</td>'
            f'<td><span class="pub {cls}">{e(pub or "Unknown")}</span></td>'
            f'<td class="cp" data-doi="{e(doi)}">'
            f'<button class="ghost" onclick="copyOne(this)" data-doi="{e(doi)}">Copy DOI</button></td>'
            "</tr>")

    stat = " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    html = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>The to-download list · {len(recs)} papers</title><style>{_MISS_CSS}</style></head><body>"
        f"<h1>The to-download list · {len(recs)} papers</h1>"
        f'<div class="sub">Generated by "Miaomiao Paper Tool" · All selected search results {stat}</div>'
        '<div class="tip">'
        "<b>How to use it (three routes, easiest first):</b>"
        "<ol>"
        "<li><b>On the campus network / school VPN</b>: click the title and grab the PDF from the top right of the publisher page. "
        "Wiley, ACS and T&amp;F only accept institutional IPs, so from home you need the school VPN.</li>"
        "<li><b>Zotero in bulk</b>: drag the <code>_missing_for_zotero.ris</code> from the same folder into Zotero, "
        "select all items → right-click \"Find Available PDF\" and it will try each one in turn.</li>"
        "<li><b>Library document delivery</b>: hand the <code>_missing_for_library.csv</code> to your library, "
        "and it usually arrives by email within 1–2 working days. This route is the most reliable, but slow.</li>"
        "</ol>"
        "This program does not fetch these pages: page URLs for subscription journals require institutional authentication, and scripted bulk access both violates the publisher's terms and "
        "gets the institution's egress IP blacklisted, so here you only get links, not fetching."
        "<div style=\"margin-top:8px;color:#6a7a4a\">"
        "<b>Progress is remembered</b>: clicking a title (or ticking the box yourself) marks it as \"downloaded\" and the row fades out; it stays that way after you close and reopen."
        "Checkmarks live in this computer's browser and do not sync across machines.</div>"
        "</div>"
        '<div class="bar"><button id="cp" onclick="copyAll()">Copy all DOIs</button>'
        '<button class="ghost" id="bat" onclick="batchNext()">Open the next 5</button>'
        '<button class="ghost" id="only" onclick="onlyTodo(this)">Only not downloaded</button>'
        '<button class="ghost" onclick="clearAll()">Clear checkmarks</button>'
        f'<span id="cnt">Downloaded 0 / {len(recs)}</span>'
        f'<span class="sub" style="margin:0">{fmt_label}</span></div>'
        '<div class="batch" id="batch" hidden></div>'
        "<table><thead><tr><th></th><th style=\"width:44px\">#</th><th>Title / DOI</th>"
        "<th style=\"width:210px\">Journal</th><th style=\"width:56px\">Year</th>"
        "<th style=\"width:96px\">Publisher</th><th style=\"width:112px\"></th></tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table>"
        f"<script>{_MISS_JS}</script></body></html>"
    )
    path.write_text(html, encoding="utf-8")



# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_search(args, cfg: dict) -> int:
    sess = PoliteSession(cfg)
    q = args.query
    if re.search(r"[\u4e00-\u9fff]", q):
        log("⚠ The query contains Chinese: OpenAlex / Crossref support Chinese poorly; please switch to English keywords.")
        log("  e.g. endogenous lipid + shrimp + aroma → 'endogenous lipid shrimp aroma'")
    log(f"Search:{q}")
    recs, total = search_openalex(q, cfg, sess, limit=args.limit,
                                  year_from=args.year_from, year_to=args.year_to,
                                  oa_only=args.oa_only, issn=args.issn)
    log(f"  OpenAlex matched {total:,} results, took the first {len(recs)} results")

    if args.resolve:
        log("Resolving full-text links (Unpaywall / Europe PMC / Elsevier API)…")
        for i, r in enumerate(recs, 1):
            if i % 20 == 0:
                log(f"  … {i}/{len(recs)}")
            resolve_arxiv(r)
            enrich_unpaywall(r, cfg, sess)
            if r.get("doi", "").startswith("10.1016"):
                resolve_elsevier_api(r, cfg, sess)
            if not r.get("pdf_url") and not r.get("pdf_candidates"):
                resolve_europepmc(r, sess)

    out = Path(args.out) if args.out else (HERE / f"results_{int(time.time())}.csv")
    if out.suffix.lower() == ".xlsx":
        if not write_xlsx(recs, out):
            out = out.with_suffix(".csv")
    if out.suffix.lower() != ".xlsx":
        write_csv(recs, out)
    log(f"Wrote {len(recs)} results → {out}")

    n_oa = sum(1 for r in recs if r.get("is_oa"))
    n_pdf = sum(1 for r in recs if r.get("pdf_url") or r.get("pdf_candidates"))
    log(f"OA flagged {n_oa} results | usable direct PDF links found: {n_pdf} results | requests sent: {sess.stats['requests']} times")
    return 0


def cmd_fetch(args, cfg: dict) -> int:
    src = Path(args.input)
    if not src.is_file():
        log(f"Input file not found:{src}")
        return 2
    if src.suffix.lower() == ".csv":
        with src.open(encoding="utf-8-sig") as f:
            recs = list(csv.DictReader(f))
    else:
        log("Input must be a .csv produced by search")
        return 2

    if args.limit:
        recs = recs[:args.limit]
    cap = int(cfg.get("max_downloads_per_run") or 200)
    if len(recs) > cap:
        log(f"⚠ This run has {len(recs)} papers, exceeding the per-run limit of {cap}; truncated. Increase max_downloads_per_run in config.json to allow more.")
        recs = recs[:cap]

    out_dir = Path(args.out_dir) if args.out_dir else library_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = PoliteSession(cfg)

    log(f"Target directory:{out_dir}   rate {sess.rps} req/s   total {len(recs)} results")

    # Reuse the links obtained during search
    for r in recs:
        r["pdf_candidates"] = [r["pdf_url"]] if r.get("pdf_url") else []
        if r.get("doi", "").startswith("10.1016"):
            resolve_elsevier_api(r, cfg, sess)

    stats = {"ok": 0, "preview": 0, "skip_dup": 0, "no_source": 0, "fail": 0}
    missing, manifest = [], []
    hashes: dict[str, str] = {}

    for i, r in enumerate(recs, 1):
        title = (r.get("title") or "")[:65]
        log(f"[{i}/{len(recs)}] {r.get('year','')} {title}")
        if not r.get("pdf_candidates") and not r.get("elsevier_ready"):
            resolve_arxiv(r)
            enrich_unpaywall(r, cfg, sess)
            r["pdf_candidates"] = [r["pdf_url"]] if r.get("pdf_url") else []
            if not r.get("pdf_candidates"):
                resolve_europepmc(r, sess)

        status, path, via = download_one(r, cfg, sess, out_dir)
        if status == "ok" and path:
            h = md5_of(path)
            if h in hashes:
                path.unlink(missing_ok=True)
                status, via = "skip_dup", "md5-dup"
            else:
                hashes[h] = str(path)
                log(f"    ✓ {path.name}  ({path.stat().st_size // 1024} KB, {via})")
        elif status == "preview":
            log(f"    △ First page only, and XML unavailable (enable \"Save first-page image\" or rerun on the campus network)")
        elif status == "skip_dup":
            log(f"    = already exists/duplicate, skipped ({via})")
        elif status == "no_source":
            missing.append(r)
            log(f"    ✗ not retrieved -- {why_missing(r)}")
        stats[status] = stats.get(status, 0) + 1
        manifest.append({**{k: r.get(k, "") for k in FIELDS}, "status": status, "via": via})

    write_manifest(manifest, out_dir / "_manifest.csv")
    if missing:
        with (out_dir / "_missing_for_library.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["doi", "title", "journal", "year", "first_author", "landing"])
            w.writeheader()
            for r in missing:
                w.writerow({k: r.get(k, "") for k in w.fieldnames})
        write_ris(missing, out_dir / "_missing_for_zotero.ris")
        try:
            write_missing_html(missing, out_dir / "to-download.html",
                               f"A total of {len(missing)} papers could not be retrieved in full")
        except Exception as e: # The manifest is a bonus; failing to build it doesn't affect the main flow
            log(f"(failed to generate the list:{type(e).__name__})")
    write_bib(recs, out_dir / "_refs.bib")

    log("")
    log("─" * 60)
    log(f"Done: saved {stats['ok']} · first page only {stats['preview']} · "
        f"already exists/duplicate {stats['skip_dup']} · no legitimate source {stats['no_source']} · failed {stats['fail']}")
    fmt = save_format(cfg)
    log(f"  Storage format:{SAVE_FORMATS[fmt]['label']}({SAVE_FORMATS[fmt]['cost']})")
    log(f"Total requests {sess.stats['requests']}, retries {sess.stats['retries']}, "
        f"robots.txt blocks {sess.stats['robots_blocked']}")
    if missing:
        log(f"Not retrieved: {len(missing)} papers. Three routes, easiest first:")
        log(f"  ① On the campus network/VPN, click to download → {out_dir / 'to-download.html'}")
        log(f"  ② Zotero bulk PDF fetch   → {out_dir / '_missing_for_zotero.ris'}")
        log(f"  ③ Library interlibrary loan       → {out_dir / '_missing_for_library.csv'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paperfetch",
        description="Compliant literature search and bulk PDF retrieval (official APIs + OA channels, no access control is bypassed)",
    )
    p.add_argument("--config", type=Path, help="Config file path (default ./config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="Multi-source search, export tables")
    s.add_argument("query", help="Query terms, English recommended")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--year-from", type=int)
    s.add_argument("--year-to", type=int)
    s.add_argument("--issn", help="Restrict to a journal ISSN, e.g. 0308-8146 (Food Chemistry)")
    s.add_argument("--oa-only", action="store_true", help="Open access only")
    s.add_argument("--no-resolve", dest="resolve", action="store_false",
                   help="Skip full-text link resolution, metadata only (fast)")
    s.add_argument("-o", "--out", help="Output .csv or .xlsx")
    s.set_defaults(func=cmd_search, resolve=True)

    f = sub.add_parser("fetch", help="Bulk download PDFs for the search results")
    f.add_argument("input", help=".csv produced by search")
    f.add_argument("-o", "--out-dir", default="pdfs")
    f.add_argument("--limit", type=int)
    f.set_defaults(func=cmd_fetch)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if not cfg.get("email"):
        log("⚠ No email configured: Unpaywall will reject the request and OA full texts cannot be located.")
        log(f"  Please fill in {app_dir() / 'config.json'} with \"email\" (a school email address is best).")
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
