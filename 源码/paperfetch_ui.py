#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

本地界面（供桌面客户端或浏览器调试使用）。

    python paperfetch_ui.py            # 默认 http://127.0.0.1:8765
    python paperfetch_ui.py --port 9000 --open

只监听回环地址，不对外暴露。
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
    """只读资源目录（ui.html）。打包后位于临时解压目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return HERE


CFG: dict = {}
SESS: pf.PoliteSession | None = None
CACHE: "en.Cache | None" = None
PMC: "pm.Cache | None" = None


def cache() -> "en.Cache":
    """标题译文 + 期刊指标的磁盘缓存（懒加载）。"""
    global CACHE
    if CACHE is None:
        CACHE = en.Cache(pf.app_dir())
    return CACHE


def pm_cache() -> "pm.Cache":
    """PubMed 的 DOI→类型/MeSH 缓存（懒加载）。"""
    global PMC
    if PMC is None:
        PMC = pm.Cache(pf.app_dir())
    return PMC

STATE = {
    "results": [],
    "prev": None,         # 引文扩散前的那一批关键词结果，供「返回」用
    "job": None,          # {running,total,done,ok,skip,miss,fail,current}
    "log": [],
    "lock": threading.Lock(),
}


def logline(msg: str) -> None:
    with STATE["lock"]:
        STATE["log"].append(msg)
        if len(STATE["log"]) > 400:
            del STATE["log"][:-400]


# Zotero 探测要读 4000+ 条目，2~3 秒，缓存一下别每次点都等
ZOT: dict = {"at": 0.0, "data": None}
# 整理下载会真的搬文件，同一时刻只允许一个：
# 两个标签页同时点会互相 stat 到「刚被对方搬走」的文件，报一堆假失败
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


# ---- 直写 Zotero：只用三件事（建分类 / 建条目 / 撤销），不碰已有条目 ----
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
    """给前端的账户列表（key 只露后 4 位）。"""
    accs = pf.openalex_accounts(CFG)
    idx = pf.active_account_index(CFG)
    return [{
        "index": i,
        "label": a.get("label") or "未命名",
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
    # PubMed 补文献类型 + MeSH：OpenAlex 的综述标注不可靠（实测同一主题只标出 3 篇，
    # PubMed 标出 9 篇）。只按 DOI 匹配，没有 DOI 的条目跳过；全程失败静默。
    pm_stat: dict = {}
    if CFG.get("pubmed_enrich", True):
        try:
            pm_stat = pm.enrich(recs, pm_cache(), CFG.get("pubmed_api_key") or "")
        except Exception as e:
            pm_stat = {"error": type(e).__name__}

    with STATE["lock"]:
        STATE["results"] = recs
        STATE["prev"] = None          # 新检索就丢弃旧的「返回」目标
    # 存一份给「库内搜索」建索引（元数据 + 摘要，属于书目数据，不碰正文）
    try:
        # ⚠ 变量别叫 slim —— 那是模块级序列化函数的名字，同名会把它遮蔽掉
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
    内部记录 → 界面用的结构。
    检索、引文扩散、全部列表、扩展阅读四条路径都走这里，
    保证字段名和类型判定口径完全一致（以前散在四处，改一处漏三处）。
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
            # 类型是 PubMed 给的就标一下来源，免得用户以为 OpenAlex 标对了
            "type_from": r.get("type_from", ""),
            "maybe_review": pf.is_maybe_review(r),
            "mesh": r.get("mesh") or [],
            "pmid": r.get("pmid", ""),
            # 引文扩散里「被几篇种子同时连上」（普通检索没有这个字段）
            "seed_hits": r.get("seed_hits") or 0,
            "found_via": r.get("found_via", ""),
            "abstract": (r.get("abstract") or "")[:1600],
        })
    return out


def _write_missing_files(manifest: list[dict], miss_recs: list[dict], out_dir: Path) -> None:
    """三份「没拿到全文」的清单。任何一份写不进去都不该拖垮整个任务。"""
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
        logline(f"  （_missing_for_library.csv 写不进去：{type(e).__name__} —— 可能正被 Excel 打开）")
    try:
        pf.write_ris(miss_recs, out_dir / "_missing_for_zotero.ris")
        pf.write_missing_html(miss_recs, out_dir / "待下载清单.html",
                              f"共 {len(miss_recs)} 篇没拿到全文")
        logline("")
        logline(f"没拿到 {len(miss_recs)} 篇。三条路，按省事程度：")
        logline(f"  ① 校园网/VPN 内点着下 → {out_dir / '待下载清单.html'}")
        logline(f"  ② Zotero 批量找 PDF   → {out_dir / '_missing_for_zotero.ris'}")
        logline(f"  ③ 图书馆文献传递       → {out_dir / '_missing_for_library.csv'}")
    except Exception as e:
        logline(f"（清单生成失败：{type(e).__name__}）")


def run_fetch(indices: list[int], out_dir: Path) -> None:
    """
    下载线程。三条铁律，都是真踩过的：
      1. **开场先给 STATE["results"] 拍快照** —— 下载途中改检索式，这一趟不跟着串味；
      2. **整段包 try/finally** —— 任何异常都必须把 job.running 置回 False，
         否则前端永远"下载中"，之后所有下载都被 409 挡住，只能重启程序；
      3. **单篇出错只算这一篇失败** —— 磁盘满、文件被杀软锁住，不该带崩整批。
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
            if r is None:                     # 结果集换了、索引越界：算一次失败，进度别卡住
                with STATE["lock"]:
                    STATE["job"]["done"] += 1
                    STATE["job"]["fail"] += 1
                logline(f"[{n}/{total}] 第 {idx} 条已不在结果里，跳过")
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
                    logline("    △ 只有首页，拿不到 XML（可开「保存首页图」或在校园网内重跑）")
                elif status == "skip_dup":
                    logline("    = 已存在，跳过")
                elif status == "no_source":
                    logline(f"    ✗ 未获取 —— {pf.why_missing(r)}")
                    miss_recs.append(r)
                else:
                    logline(f"    ✗ 失败 {via}")
            except Exception as e:
                status, via, key = "fail", type(e).__name__, "fail"
                logline(f"    ✗ 出错：{type(e).__name__}: {e}")
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
        logline(f"任务异常终止：{type(e).__name__}: {e}")
    finally:
        # 台账和清单各自兜底：某个文件被 Excel 打开，不该把整个任务判死
        try:
            pf.write_manifest(manifest, out_dir / "_manifest.csv")
        except Exception as e:
            logline(f"台账写不进去（{type(e).__name__}）—— _manifest.csv 可能正被 Excel 打开")
        _write_missing_files(manifest, miss_recs, out_dir)
        with STATE["lock"]:
            STATE["job"]["running"] = False
            STATE["job"]["current"] = ""
        logline("完成。")


# --------------------------------------------------------------------------- #
# 导出表格（界面里「导出表格」按钮）
#   列由前端拼好（中文标题 / IF / 分区是前端按需拉的），这里只负责落盘。
# --------------------------------------------------------------------------- #
EXPORT_COLS = [
    ("中文标题", "zh_title", 46),
    ("标题（英文）", "title", 62),
    ("期刊", "journal", 28),
    ("类型", "dtype", 14),
    ("IF", "if", 7),
    ("中科院分区", "cas", 12),
    ("JCR分区", "jcr", 10),
    ("年份", "year", 7),
    ("被引", "cited_by", 7),
    ("获取方式", "oa", 9),
    ("DOI", "doi", 34),
    ("第一作者", "first_author", 15),
    ("摘要", "abstract", 90),
]


LIB_COLS = [
    ("文件名", "name", 56),
    ("标题", "title", 58),
    ("DOI", "doi", 34),
    ("大小(KB)", "size_kb", 10),
    ("修改时间", "mtime_txt", 13),
    ("所在目录", "folder", 58),
]


def _safe_tag(s: str) -> str:
    """检索式当文件名：去掉 Windows 不认的字符，别太长。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", (s or "").strip())
    s = re.sub(r"\s+", "_", s).strip("._- ")
    return s[:28]


def export_path(tag: str, ext: str, prefix: str = "检索结果") -> Path:
    d = app_dir() / "导出"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = _safe_tag(tag)
    stem = f"{prefix}_{name}_{stamp}" if name else f"{prefix}_{stamp}"
    p = d / f"{stem}.{ext}"
    n = 2
    while p.exists():                 # 同一秒内连导两次也不互相覆盖
        p = d / f"{stem}_{n}.{ext}"
        n += 1
    return p


_ILLEGAL_CELL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
CELL_LIMIT = 32767          # Excel 单元格硬上限


def _clean_cell(v, limit: int = CELL_LIMIT) -> str:
    """
    openpyxl 对 \x00-\x08 / \x0b / \x0c / \x0e-\x1f 直接抛 IllegalCharacterError，
    元数据里偶尔混进这种字符（尤其摘要）→ 整张表 500。这里统一洗掉，长文本按 Excel 上限截断。
    """
    if v is None:
        return ""
    s = _ILLEGAL_CELL.sub(" ", str(v))
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s


def _write_export_csv(rows: list[dict], path: Path, cols) -> int:
    pairs = [(h, k) for h, k, _ in cols]
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM：Excel 双击不乱码
        w = csv.writer(f)
        w.writerow([h for h, _ in pairs])
        for r in rows:
            w.writerow([_clean_cell(r.get(k, "")) for _, k in pairs])
    return len(rows)


def _write_export_xlsx(rows: list[dict], path: Path, cols) -> int:
    """返回写入行数；没装 openpyxl 抛 ImportError（调用方退回 CSV）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "清单"
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
                 cols=None, prefix: str = "检索结果") -> tuple[Path, str, int]:
    """写表格，返回 (路径, 实际格式, 行数)。xlsx 不可用时自动退回 csv。"""
    cols = cols or EXPORT_COLS
    fmt = (fmt or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        raise ValueError(f"不支持的格式：{fmt}")
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
    """文献库扫描只给文件名和 DOI，缺期刊/年份/作者。
    从 _manifest.csv 和上次检索结果里按 DOI 补上，Zotero 导入才不会全是空字段。"""
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
        path = raw.split("?", 1)[0]      # 去掉 query，否则 /?tab=lib 会 404
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
            # 只返回猜测的「下载」文件夹，给界面当默认值（用户可改）
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
        # 任何一处没预料到的异常都别让连接直接断掉 —— 断掉的话前端只会看到
        # "Failed to fetch"，连错误信息都没有（2026-11-04 被 int 的 year 坑过一次）
        try:
            self._do_post()
        except Exception as e:
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def _do_post(self):
        # 只接受本页面发来的请求：别人家的网页也能往 127.0.0.1 发 POST（DNS rebinding / 扫端口），
        # 而 /api/open 能让资源管理器打开任意路径，必须挡住。
        origin = (self.headers.get("Origin") or "").strip()
        port = self.server.server_port
        ok_hosts = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        if origin and not origin.startswith(ok_hosts):
            self._json({"error": "拒绝跨站请求"}, 403)
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
                    max(1, min(int(body.get("limit") or 30), 1000)),   # 上限 1000：再多前端渲染会卡
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
            # 文献库可能还没建（现在不再自动创建），点「打开」的时候再建
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
                self._json({"error": "路径不存在"}, 400)

        elif self.path == "/api/sources":
            act = (body.get("action") or "").strip()
            path = (body.get("path") or "").strip().strip('"')
            srcs = list(CFG.get("extra_sources") or [])
            if act == "add":
                if not path or not Path(path).is_dir():
                    self._json({"error": "目录不存在：%s" % path}, 400)
                    return
                if path not in srcs:
                    srcs.append(path)
            elif act == "remove":
                srcs = [x for x in srcs if x != path]
            elif act == "clear":
                srcs = []
            else:
                self._json({"error": "未知操作"}, 400)
                return
            CFG["extra_sources"] = srcs
            pf.save_config(CFG)
            self._json({"sources": srcs})

        elif self.path == "/api/storage":
            fmt = (body.get("save_format") or "").strip().lower()
            if fmt and fmt not in pf.SAVE_FORMATS:
                self._json({"error": "未知的存储格式"}, 400)
                return
            if fmt:
                CFG["save_format"] = fmt
            if "keep_preview_pdf" in body:
                CFG["keep_preview_pdf"] = bool(body.get("keep_preview_pdf"))
            pf.save_config(CFG)
            f = pf.save_format(CFG)
            logline(f"存储格式改为：{pf.SAVE_FORMATS[f]['label']}"
                    + ("（另存首页图）" if CFG.get("keep_preview_pdf") else ""))
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
                logline("已清除 easyScholar 密钥，IF / 分区列将显示「—」")
                self._json({"easyscholar": False})
                return
            key = (body.get("key") or "").strip()
            if not key:
                self._json({"error": "密钥是空的"}, 400)
                return
            t = en.test_key(key)
            if not t.get("ok"):
                self._json({"error": t.get("msg") or "验证没通过"}, 400)
                return
            CFG["easyscholar_key"] = key
            pf.save_config(CFG)
            logline("easyScholar 密钥已保存，IF / 中科院分区可以用了")
            self._json({"easyscholar": True, "test": t})

        elif self.path == "/api/zh":
            titles = [str(x) for x in (body.get("titles") or [])][:80]
            try:
                m, note = en.translate_titles(titles, CFG.get("email") or "", cache())
                self._json({"map": m, "note": note})
            except Exception as e:
                self._json({"map": {}, "note": f"{type(e).__name__}"}, 500)

        elif self.path == "/api/zh_abs":
            text = (body.get("text") or "")[:4000]
            try:
                zh, note = en.translate_abstract(text, CFG.get("email") or "", cache())
                self._json({"zh": zh, "note": note})
            except Exception as e:
                self._json({"zh": "", "note": f"{type(e).__name__}"}, 500)

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
                    self._json({"error": "账户不存在"}, 400)
                    return
                CFG["openalex_accounts"] = accs
                CFG["openalex_active"] = idx
                pf.sync_active_key(CFG)
                pf.save_config(CFG)
                SESS.quota = {}          # 换账户后额度要重查
                q = pf.probe_quota(CFG, SESS, force=True)
                q["accounts"] = _accounts_view()
                q["active_index"] = idx
                logline(f"已切换到账户：{accs[idx].get('label')}")
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
                logline(f"已添加账户：{pf.active_account(CFG).get('label')}")
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
                self._json({"error": "没有可导出的内容（先检索一下）"}, 400)
                return
            # 前端给的摘要只有 1600 字（界面预览够用），导出换成服务端存的全量
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
            logline(f"已导出 {n} 条 → {p.name}（{p.stat().st_size // 1024} KB）")
            self._json({"path": str(p), "fmt": used, "n": n,
                        "size": p.stat().st_size})

        elif self.path == "/api/library_export":
            rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                self._json({"error": "没有可导出的条目（先扫描文献库）"}, 400)
                return
            try:
                p, used, n = write_export(rows, body.get("fmt") or "xlsx",
                                          "", cols=LIB_COLS, prefix="文献库")
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
            logline(f"库内索引重建：{st['docs']} 条（摘要 {st['papers']} / 笔记 {st['notes']} / "
                    f"检索记录 {st['records']}），跳过 PDF {st['skipped_pdf']} 篇")
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
                self._json({"error": "没有可导入的条目"}, 400)
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
                            "msg": "这些条目 Zotero 里都已经有了"})
                return
            group = (body.get("group") or "journal").strip()
            if group not in ("journal", "year", "single"):
                group = "journal"
            tag = _safe_tag(body.get("tag") or "")
            p = export_path(tag, "rdf", prefix="Zotero导入")
            n = zo.write_rdf(rows, p, group, body.get("root") or "文献妙妙小工具")
            logline(f"已生成 Zotero 导入文件：{p.name}（{n} 条，跳过已有 {before - n} 条）")
            self._json({"ok": True, "path": str(p), "n": n, "skipped": before - n,
                        "group": group})

        elif self.path == "/api/zotero/write":
            rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                self._json({"error": "没有可写入的条目"}, 400)
                return
            # 已在库里的（按 DOI）照样跳过，免得写重
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
                "小工具导入 " + time.strftime("%Y-%m-%d %H:%M")
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
                            "msg": "这些条目 Zotero 里都已经有了"})
                return

            w = writer()
            ck, why = w.create_collection(name)
            if not ck:
                self._json({"error": why or "建分类失败"}, 400)
                return
            done, fails = w.create_items(rows, ck)
            _save_last_write({"at": int(time.time()), "name": name, "collection": ck,
                              "items": done})
            ZOT["at"] = 0                      # 写完让缓存失效，下次重新读
            logline(f"已写入 Zotero：分类「{name}」{len(done)} 条"
                    + (f"，失败 {len(fails)} 条" if fails else ""))
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
                self._json({"error": "没有可撤销的写入记录"}, 400)
                return
            w = writer()
            n = w.delete_items(last.get("items") or [])
            ver = w.collection_version(last["collection"])
            ok_del = w.delete_collection(last["collection"], ver)
            _last_write_file().unlink(missing_ok=True)
            ZOT["at"] = 0
            logline(f"已撤销 Zotero 写入：删掉 {n} 条、分类「{last.get('name')}」"
                    + ("已删" if ok_del else "没删掉（可能在 Zotero 里被锁）"))
            self._json({"ok": True, "items": n, "collection_deleted": ok_del,
                        "name": last.get("name")})

        elif self.path == "/api/zotero/dir":
            # 新手不知道自己的 Zotero 库放哪，就让他指一个目录（也认上一级）
            p = (body.get("path") or "").strip().strip('"')
            if not p:
                self._json({"error": "没选目录"}, 400)
                return
            hit = zo.set_data_dir(p)
            if not hit:
                self._json({"error": "这个目录里没有 zotero.sqlite，换一个试试"
                                     "（一般是 Zotero 的「数据目录」，里面有 zotero.sqlite 和 storage 文件夹）"}, 400)
                return
            CFG["zotero_data_dir"] = str(hit)
            pf.save_config(CFG)
            ZOT["at"] = 0
            logline(f"Zotero 数据目录已设为：{hit}")
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
                    self._json({"error": f"目录不存在：{p}"}, 400)
                    return
                if norm(p) not in {norm(x) for x in dirs}:
                    dirs.append(str(Path(p)))        # 存归一化后的写法，界面才好比对
            elif act == "remove":
                dirs = [x for x in dirs if norm(x) != norm(p)]
            elif act == "clear":
                dirs = []
            else:
                self._json({"error": "未知操作"}, 400)
                return
            CFG["notes_dirs"] = dirs
            pf.save_config(CFG)
            self._json({"dirs": dirs,
                        "effective": [str(x) for x in ls.notes_dirs(CFG)]})

        elif self.path == "/api/organize":
            # 把「下载」文件夹里手工下到的 PDF 认出来、改名、归位到文献库。
            # dry_run=True（默认）只出计划，不动任何文件。
            folder = (body.get("dir") or "").strip()
            dry = body.get("dry_run", True)
            if not isinstance(dry, bool):        # 兼容传 "false"/"0" 这类字符串
                dry = str(dry).strip().lower() not in ("false", "0", "no", "off")
            try:
                limit = max(1, min(int(body.get("limit") or 200), 1000))
            except (TypeError, ValueError):
                limit = 200
            d = Path(folder) if folder else og.guess_downloads()
            with STATE["lock"]:
                cur = list(STATE["results"])
            # 下载任务正在往文献库里写 PDF，这时整理会撞名、甚至撞上正在写的文件
            busy = bool(STATE.get("job") and STATE["job"].get("running"))
            if busy:
                self._json({"error": "下载任务正在跑，等它跑完再整理（两边都往文献库写文件，会撞）"}, 409)
                return
            if not ORG_LOCK.acquire(blocking=False):
                self._json({"error": "正在整理中，等这一轮跑完再点（别开两个窗口同时整理）"}, 409)
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
            # 引文扩散：拿勾选的几条当种子，走 Semantic Scholar 拉正向/反向引文
            try:
                idx = [int(i) for i in (body.get("indices") or [])]
            except (TypeError, ValueError):
                self._json({"error": "条目编号不合法"}, 400)
                return
            if not idx:
                self._json({"error": "先勾几篇当种子（有 DOI 的才管用）"}, 400)
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
                self._json({"error": "这些条目已经不在结果里了，重新检索一下"}, 400)
                return
            try:
                ext, stat = ct.expand(seeds, CFG, direction=direction, limit=limit)
            except ct.CitesError as e:
                self._json({"error": str(e), "rate_limited": True}, 429)
                return
            if not ext:
                self._json({"error": "这几篇在 Semantic Scholar 里没有可用的引文记录"
                                     "（可能太新，或 DOI 没被收录）", "stats": stat}, 400)
                return
            with STATE["lock"]:
                STATE["prev"] = list(STATE["results"])
                STATE["results"] = ext
            self._json({"n": len(ext), "stats": stat, "results": slim(ext)})

        elif self.path == "/api/back":
            # 从引文扩散结果返回关键词结果
            with STATE["lock"]:
                prev = STATE["prev"]
                if prev:
                    STATE["results"] = prev
                    STATE["prev"] = None
            if not prev:
                self._json({"error": "没有可返回的上一批结果"}, 400)
                return
            self._json({"n": len(prev), "results": slim(prev)})

        elif self.path == "/api/fetch":
            # 整理下载时不能开始下载：两边都往文献库写同名 PDF，会撞文件
            if ORG_LOCK.locked():
                self._json({"error": "正在整理下载的文件，等它跑完再开始下载（两边都往文献库写，会撞）"}, 409)
                return
            idx = [int(i) for i in (body.get("indices") or [])]
            if not idx:
                self._json({"error": "没有选中条目"}, 400)
                return
            cap = int(CFG.get("max_downloads_per_run") or 200)
            if len(idx) > cap:
                self._json({"error": f"选中的 {len(idx)} 条超过单次上限 {cap} 条。"
                                     f"分批下，或改 config.json 的 max_downloads_per_run 后重启。"}, 400)
                return
            out_dir = Path(body.get("out_dir") or pf.library_dir(CFG))
            # 检查和占位必须在同一把锁里：否则双击按钮（两个请求同时到达）都会通过检查，
            # 两条线程往同一个目录写同一批文件
            with STATE["lock"]:
                busy = bool(STATE.get("job") and STATE["job"].get("running"))
                if not busy:
                    STATE["job"] = {"running": True, "total": len(idx), "done": 0,
                                    "ok": 0, "preview": 0, "skip": 0, "miss": 0,
                                    "fail": 0, "current": "准备中"}
                    STATE["log"] = []
            if busy:
                self._json({"error": "已有任务在跑，等它跑完再点。"}, 409)
                return
            threading.Thread(target=run_fetch, args=(idx, out_dir), daemon=True).start()
            self._json({"started": len(idx), "out_dir": str(out_dir)})
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--config", type=Path, help="配置文件路径")
    a = ap.parse_args()
    global CFG, SESS
    CFG = pf.load_config(a.config)
    zo.set_config(CFG)          # Zotero 模块不再自己读 config，用这份
    SESS = pf.PoliteSession(CFG)
    if CFG.get("_cfg_broken"):
        print(f"⚠ 配置文件读不出来（已备份成 *.broken.json）：{CFG['_cfg_broken']}\n"
              f"   这次按默认值启动，密钥要重新填。")
    url = f"http://127.0.0.1:{a.port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"paperfetch UI → {url}")
    if not CFG.get("email"):
        print("⚠ config.json 的 email 还没填，OA 全文链接将无法解析。")
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
