# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2 —— 引文扩散（cites）

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

为什么要有这个模块：
  关键词检索有一个绕不过去的漏 —— 同一个东西，别人可能叫别的名字。
  你搜 "lipid oxidation AND aroma"，就搜不到只写 "lipid–flavor interaction" 的文章。
  换关键词能补一点，但补不全。

  引文网络不需要你想词：从几篇核心文献出发，
    · 正向：谁引用了这几篇（后续工作）
    · 反向：这几篇引用了谁（前人的基础）
  比关键词可靠得多，也是"新文献"最主要的来源。

接口：Semantic Scholar Graph API（免费，官方）
  GET /graph/v1/paper/DOI:<doi>/citations?fields=...&limit=100
  GET /graph/v1/paper/DOI:<doi>/references?fields=...&limit=100
  无 key 共享池 100 请求 / 5 分钟，很容易 429 —— 所以串行 + 退避重试，
  种子数也设上限（默认 6 篇），不是越多越好。

排序：**共现次数优先**（一篇被多个种子同时引用/引用 = 它处在这几篇的共同邻居上，
通常就是最相关的那几篇），其次按 S2 自己的引用数。
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
MAX_SEEDS = 6                 # 一次最多拿几篇当种子（每篇 2 个请求）
PER_SEED = 100                # 正向（谁引用了它）取多少条
PER_SEED_REF = 60             # 反向（它引用了谁）取多少条 —— 综述能引 200 篇，别全拉进来
GAP = 1.15                    # 请求间隔（秒）；无 key 时别贪快


class CitesError(RuntimeError):
    """限速 / 连不上时抛，message 直接给用户看。"""


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
                return None                  # 这篇 S2 没有，跳过就是
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
        raise CitesError("Semantic Scholar 限流了（免费额度 100 请求 / 5 分钟）。"
                         "等几分钟再试，或一次少选几篇种子。")
    return None


def _doi_of(p: dict) -> str:
    ext = p.get("externalIds") or {}
    return ((ext.get("DOI") or "") if isinstance(ext, dict) else "").strip().lower()


def _to_rec(p: dict, via: str) -> dict:
    """S2 的 paper 对象 → 本项目内部记录结构（字段名与 paperfetch._to_rec 对齐）。"""
    au = p.get("authors") or []
    first = ((au[0].get("name") or "").strip() if au else "")
    # ⚠ 不要在这里把 "Y. Zhang" 改写成 "Zhang, Y." ——
    # 下游（下载文件名、BibTeX 引用键）都是取 split()[-1] 当姓，
    # 改成带逗号的格式会取到 "Y."，文件名变成 2024_Y_xxx.pdf。
    # OpenAlex 那边给的也是"名 姓"，保持原样才对得上。
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
    seeds: [{doi, title, ...}] —— 只取有 DOI 的当种子（S2 用 DOI 最稳）
    direction: "both" / "citations"（正向，谁引用了它）/ "references"（反向，它引用了谁）
    返回 (记录, 统计)
    """
    cfg = cfg or {}
    key = (cfg.get("semantic_scholar_key") or "").strip()
    use = [s for s in seeds if (s.get("doi") or "").strip()][:max_seeds]
    stats = {"seeds": len(use), "skipped_no_doi": len(seeds) - len(use),
             "citations": 0, "references": 0, "req": 0, "dup": 0,
             "errors": 0, "truncated": len(seeds) > max_seeds,
             # 有几个种子的「它引用了谁」是空的 —— 实测 Elsevier / Wiley 的文献
             # Semantic Scholar 普遍拿不到参考文献（出版社不开放引用数据），
             # MDPI 这类全 OA 的才有。前端据此给一句实话，别让用户以为是坏了。
             "ref_empty_seeds": 0}

    if not use:
        return [], {**stats, "why": "勾选的条目都没有 DOI，没法做引文扩散"}

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
            if d and d in seed_dois:          # 种子自己也别混进来
                continue
            if k in pool:
                pool[k]["seed_hits"] += 1     # 被多个种子同时命中 → 更相关
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
    # 排序三层：
    #   1. 共现次数（被几篇种子同时连上 —— 处在共同邻居上，最相关）
    #   2. 方向：正向（专门引用你这篇的后续工作）比反向（综述顺带列出的背景文献）更贴题
    #   3. S2 引用数
    recs.sort(key=lambda r: (
        -(r.get("seed_hits") or 0),
        0 if "citations" in (r.get("found_via") or "") else 1,
        -(r.get("cited_by") or 0),
    ))
    stats["total"] = len(recs)
    return recs[:limit], stats
