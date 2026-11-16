# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2 —— PubMed 补充（pubmed）

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

为什么要有这个模块：
  OpenAlex 的文献类型（type）是机器猜的，综述标注极不可靠 —— 同一主题实测
  OpenAlex 只标出 3 篇 review，PubMed 标出 9 篇。PubMed 的 PublicationType
  是期刊自己上报 + NLM 人工核对过的，准得多，还自带 MeSH 主题词。

做什么：
  · 拿 DOI 换 PMID → 取 PublicationTypeList 和 MeshHeadingList
  · Review / Systematic Review / Meta-Analysis 会**覆盖** OpenAlex 的 article
    （这是本模块存在的唯一理由）
  · 其余类型只在 OpenAlex 没给或给成 article 时才补，绝不反向覆盖
  · MeSH 词存下来，界面上给一小行，用来快速判断"这篇是不是我要的方向"

接口（E-utilities，NCBI 官方，免费）：
  1. esearch?db=pubmed&term=10.xxx[DOI] OR 10.yyy[DOI]  → 一批 PMID
  2. efetch?db=pubmed&id=<用逗号连的 PMID>&retmode=xml   → 详情 + DOI 反查
  两个请求就能处理几十篇，不用一篇一个请求。

取舍：
  · 只按 DOI 匹配。没有 DOI 的条目跳过 —— 用标题去猜 PubMed 记录容易张冠李戴，
    宁可不补，也不要补错。
  · 全程失败静默。PubMed 挂了不该影响检索和下载。
  · 结果落 .cache_pubmed.json，半年过期。
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
BATCH = 60                     # 一次 esearch 丢多少个 DOI（再多 URL 会顶到 8KB）
MAX_ENRICH = 200               # 只给前 N 条补 —— 500 条全查要 40 秒，而人只看前一两百条
TIMEOUT = 25

# PubMed 的 PublicationType 字符串 → 本工具的 work_type
# 顺序有用：同一个条目常常既是 "Journal Article" 又是 "Review"，取到 Review 就该停
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
# 这些类型优先级最高，能覆盖 OpenAlex 的判断
STRONG = ("review", "preprint", "dissertation", "withdrawn", "book-chapter")

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 缓存
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
# 请求
# --------------------------------------------------------------------------- #
def _get(path: str, params: dict, api_key: str = "", tries: int = 2):
    if api_key:
        params = {**params, "api_key": api_key}
    for i in range(tries):
        try:
            r = requests.get(f"{EU}/{path}", params=params, timeout=TIMEOUT, headers=UA)
            if r.status_code == 429:            # 被限速，等一下再来
                time.sleep(1.2 * (i + 1))
                continue
            if r.status_code != 200:
                return None
            return r
        except Exception:
            time.sleep(0.6 * (i + 1))
    return None


def _dois_to_pmids(dois: list[str], api_key: str = "") -> list[str]:
    """一次 esearch 把一批 DOI 换成 PMID。返回去重后的 PMID 列表。"""
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
    拿 PMID 列表换详情。返回 {doi_lower: {type, mesh, pmid}}。
    DOI 从 PubmedData/ArticleIdList 里反查，这样不用猜哪个 PMID 对应哪个 DOI。
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

        # 文献类型：Review > 其他 > Journal Article
        types = [((el.text or "").strip().lower())
                 for el in art.findall(".//PublicationTypeList/PublicationType")]
        work_type = ""
        for t in types:
            mapped = PT_MAP.get(t)
            if mapped in STRONG:              # 强类型直接定案
                work_type = mapped
                break
        if not work_type:
            for t in types:
                mapped = PT_MAP.get(t)
                if mapped and mapped != "article":
                    work_type = mapped
                    break
        if not work_type and types:
            work_type = "article"             # 只有 Journal Article 也算明确

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
# 对外
# --------------------------------------------------------------------------- #
def enrich(recs: list[dict], cache: Cache, api_key: str = "",
           max_items: int = MAX_ENRICH) -> dict:
    """
    就地给 recs 补 pubmed_type / mesh / pmid，返回统计。
    已经有缓存的直接用（不花钱），没查过的才发请求；
    **只查前 max_items 条** —— 检索 500 条时全查要 40 秒，而后面的条目用户基本不看。
    超出的部分在 stats["skipped_over_limit"] 里如实报数。
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
            continue            # 查不到就跳过，**不缓存失败**，下次还会重试
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
    """把 PubMed 的结果合进记录。类型只在「更强」时才覆盖。"""
    if not info:
        return
    pt = (info.get("type") or "").strip()
    cur = (rec.get("work_type") or "").strip()
    if pt == "review":
        # Review 覆盖一切 —— 这是本模块存在的理由。
        # 但**只有原来不是 review 时才算"改过"**：OpenAlex 本来就标对的，
        # 不能也打上 type_from，否则前端会报「N 条其实是综述（OpenAlex 标错了）」，
        # 而用户明明是在「综述」类型下检索的，每条本来就挂着「综述」标。
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
