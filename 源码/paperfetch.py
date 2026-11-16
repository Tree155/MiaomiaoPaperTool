#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

合规文献检索与全文获取

设计约束（写进代码，不是写在注释里）：
  1. 所有取全文的路径都必须是【版权方公开的 API】或【OA 渠道（Unpaywall / PMC OA / arXiv）】。
     代码里硬性拒绝抓取 ScienceDirect / linkinghub 等需要订阅鉴权的页面 URL。
  2. User-Agent 明确标识工具身份与联系邮箱，全局限流，支持 Retry-After 退避。
  3. 非 API 直链先查 robots.txt，Disallow 则跳过。
  4. 单次运行有下载上限，防止误操作让机构 IP 背锅。

用法见 README.md。
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

APP_NAME = "文献妙妙小工具"
APP_VERSION = "1.4.2"
VERSION = APP_VERSION          # HTTP User-Agent 里的技术标识

# pypdf 解析字体编码失败时会刷大量警告，压到只报错
logging.getLogger("pypdf").setLevel(logging.ERROR)
HERE = Path(__file__).resolve().parent


def app_dir() -> Path:
    """
    数据目录（config.json / pdfs）。

    便携优先：打包后就用 exe 所在目录，整个文件夹压缩分享、解压即用。
    只有 exe 所在目录不可写（比如放在 Program Files）才退回「文档\\paperfetch」。
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
    "_说明": "email 必填（学校邮箱）。elsevier_api_key / elsevier_insttoken 怎么申请见「使用说明.pdf」",
    "email": "",                  # 必填：Unpaywall / 礼貌池都要求
    "elsevier_api_key": "",       # 可选：dev.elsevier.com 免费申请
    "elsevier_insttoken": "",     # 可选：学校图书馆给的 institutional token
    "library_dir": "",             # 文献库目录；空 = 程序目录\文献库
    "extra_sources": [],           # 额外扫描的目录（Zotero 等）
    "openalex_api_key": "",       # 当前生效的 OpenAlex key（由 openalex_accounts 同步）
    "openalex_accounts": [],      # 多账户：[{label, email, api_key}]
    "openalex_active": 0,         # 当前账户下标
    "save_format": "xmlgz",       # 正文存法：pdf / xml / xmlgz
    "keep_preview_pdf": False,    # 只有首页时是否额外存首页图（白占 ~245KB/篇）
    "easyscholar_key": "",        # 可选：期刊 IF / 中科院分区（easyscholar.cc 免费注册取密钥）
    "show_zh_title": True,        # 检索结果里，英文标题下面挂一行中文翻译
    "notes_dirs": [],             # 库内搜索要索引的笔记目录（.md/.txt）；留空则用程序目录下的「笔记」
    "zotero_data_dir": "",        # Zotero 数据目录（含 zotero.sqlite）；留空则自动找
    "pubmed_enrich": True,        # 检索后用 PubMed 校正文献类型 + 取 MeSH（关掉可省 3–5 秒）
    "pubmed_api_key": "",         # 可选：NCBI key（不限速用；不填也能用）
    "semantic_scholar_key": "",   # 可选：S2 key（引文扩散不限速用；不填也能用）
    "rate_limit_rps": 1.0,        # 全局请求速率（次/秒）
    "max_downloads_per_run": 200, # 单次运行下载上限
    "timeout": 30,
}

# 这些域名的页面 URL 一律不抓（需订阅鉴权），全文只能走 Elsevier 官方 API
FORBIDDEN_DOMAINS = (
    "sciencedirect.com",
    "linkinghub.elsevier.com",
    "www.sciencedirect.com",
)


def is_forbidden(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    return any(d in host for d in FORBIDDEN_DOMAINS)


# --------------------------------------------------------------------------- #
# 出版社识别 —— 决定"下不到"的时候该告诉用户什么
#
# 意义：同样是 no_source，Elsevier 是"你没配 key"，Wiley/ACS/T&F 是"这家压根没给
# 个人开放的全文接口"。不区分就只能笼统说"无 OA 版本"，用户不知道该找谁。
# 类型：api = 有官方全文接口 / oa = 开放获取 / sub = 订阅制（无自助接口）
# --------------------------------------------------------------------------- #
PUBLISHERS = {
    "10.1016": ("Elsevier", "api"),      # Article Retrieval API，key + insttoken
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

# 这几家没有个人可申请的全文接口，说清楚，别让用户去折腾 API
SUB_HINT = {
    "Wiley": "Wiley 订阅刊 —— 全文只在 onlinelibrary，校园网/VPN 内点开可下；"
             "想全自动得要图书馆开 Wiley TDM token",
    "ACS": "ACS 订阅刊 —— 没有个人可申请的全文接口，只能校园网或文献传递",
    "T&F": "T&F 订阅刊 —— 没有个人可申请的全文接口，只能校园网或文献传递",
}


def publisher_of(doi: str) -> tuple[str, str]:
    """DOI → (出版社, 类型)。认不出时返回 ("", "")。"""
    d = (doi or "").strip().lower()
    for prefix, info in PUBLISHERS.items():
        if d.startswith(prefix):
            return info
    return "", ""


# DOI 前缀会骗人：期刊转让后，10.1111（Wiley）的文章可能已经搬到 academic.oup.com。
# 真出问题的时候（拿不到全文）去 doi.org 看一眼真正的落点，只做一次，带缓存。
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
_HOST_BUDGET = [40]      # 一轮最多查 40 次，超了就用前缀判断，别把下载拖成龟速


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
    """DOI 真正跳到哪家 → (出版社, 主机名)。查过就记着，失败返回 ("", "")。"""
    d = (doi or "").strip()
    if not d:
        return "", ""
    _host_cache_path()
    hit = _HOST_MEM.get(d)
    if hit and len(hit) == 2:
        return hit[0], hit[1]
    if _HOST_BUDGET[0] <= 0:
        return "", ""                 # 一轮跑太多篇时别再挨个查，免得拖慢
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
    """为什么没拿到 —— 尽量给一句能照着做的事。"""
    pub, kind = publisher_of(rec.get("doi", ""))
    doi = (rec.get("doi") or "").strip()
    notes = "；".join((rec.get("_notes") or [])[:2])
    if rec.get("_oa_needs_api"):
        return "OA 刊但全文只挂在 ScienceDirect —— 配 elsevier_api_key 即可下"
    if rec.get("_els_note"):
        return rec["_els_note"]
    if kind == "oa":
        extra = f"（这次脚本被挡：{notes}）" if notes else "，重试即可"
        return f"{pub} 是开放获取刊，浏览器点开就能下{extra}"
    if kind == "sub":
        real, host = landing_label(doi)
        oa = (rec.get("oa_status") or "").strip().lower()
        if oa == "bronze":
            # 出版社标"免费阅读"，但脚本照样被 WAF 挡 —— 别让人以为程序坏了
            return (f"{pub} 标为免费阅读（bronze），浏览器能下、脚本被 WAF 挡 —— "
                    f"走「待下载清单」点开下" + (f"；{notes}" if notes else ""))
        if real and real != pub:
            # 期刊搬过家：前缀还是 Wiley，实际在 OUP 上
            base = (f"这个 DOI 现在跳 {host}（{real}）—— 期刊换了平台，"
                    f"需校园网/机构权限，或走文献传递")
        else:
            base = SUB_HINT.get(pub, f"{pub} 订阅刊 —— 需校园网/机构权限，或走文献传递")
        tail = ""
        if oa and oa not in ("gold", "diamond", "hybrid", "green"):
            tail = f"（元数据：{oa}，非 OA）"
        if notes:
            tail += f"（底层报错：{notes}）"
        return base + tail
    if notes:
        return notes
    if kind == "api":
        return f"{pub} 订阅刊 —— 需 elsevier_api_key + insttoken（见使用说明）"
    if not doi:
        return "没有 DOI —— 中文刊多半在知网/万方，用篇名去搜"
    return "无 OA 版本（多半是订阅刊）"


# --------------------------------------------------------------------------- #
# 配置与日志
# --------------------------------------------------------------------------- #
# 这几个以下划线开头、但要写进文件的键（`_说明` 是给用户看的注释，丢了就没人知道每项干什么）
_KEEP_UNDERSCORE = {"_说明"}


def load_config(path: Path | None = None) -> dict:
    """
    读配置。三件容易出事的事，这里都兜住了：

    1. **config.json 坏了**（手改留了尾逗号、或上次写盘被打断）不能让程序直接起不来。
       坏文件改名成 config.broken.json 留档，然后按默认值继续跑 —— 用户最多是重新填一次密钥，
       总比双击没反应强。
    2. **--config 指定了一个还不存在的路径**时，把它记成写入目标，
       不能悄悄写到 app_dir() 去（否则界面里的设置会存进另一个文件，重启就"消失"）。
    3. **新版本新增的配置键**自动补写回文件，只补缺的，用户填的值和键顺序都不动。
    """
    cfg = dict(DEFAULT_CONFIG)
    default_path = app_dir() / "config.json"
    # 首次运行（尤其是 exe）自动生成配置文件，放在程序旁边
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
                raise ValueError("顶层不是对象")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            # 坏文件：留个备份再当默认值用，千万别让它把启动路径打断
            try:
                p.replace(p.with_suffix(".broken.json"))
            except OSError:
                pass
            cfg["_source"] = str(p)          # 写回还是写到原处（会重建一份好的）
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

    # --config 指定了路径但文件不存在：记住它，之后保存就写这里
    if path and not cfg.get("_source"):
        cfg["_source"] = str(Path(path))
    return cfg


def save_config(cfg: dict) -> Path:
    """
    把配置写回 config.json（丢掉内部键，保留 `_说明`）。

    ⚠ **必须原子写**：直接 write_text 是「先清空再写」，中途断电 / 磁盘满 / 被结束进程
    都会留下半个 JSON —— 而 config.json 里是用户全部密钥，下次启动直接读不出来。
    所以先写 .tmp 再 replace（Windows 上 replace 偶尔被占用，重试一次）。
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
            time.sleep(0.15)      # Windows 上可能正被记事本/杀软占着，等一下再试
            tmp.replace(p)
    except OSError as e:
        # 别把 [WinError 5] ...config.tmp 这种原始信息直接甩给用户，他看不懂
        raise OSError(f"配置没能保存到 {p.name}（{getattr(e, 'strerror', None) or e}）。\n"
                      f"常见原因：这个文件正被记事本 / 杀毒软件占着，或者文件夹没有写权限。"
                      f"关掉再试一次。") from e
    return p


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# robots.txt（RFC 9309）
# --------------------------------------------------------------------------- #
class RobotsTxt:
    """
    Python 内置 robotparser 有两个硬伤：不实现 Allow 指令、把 403 当成全站禁止。
    这里按 RFC 9309 自己实现：最长匹配优先、等长时 Allow 优先、403/401 视为"无法确认"。
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
        """True=允许 / False=明确禁止 / None=无法确认（robots 抓不到）"""
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
# 礼貌请求层
# --------------------------------------------------------------------------- #
class QuotaExceeded(RuntimeError):
    """对方额度用尽（不是普通限速）。重试没用，直接告诉用户怎么办。"""


class PoliteSession:
    """全局限流 + 指数退避 + 明确 UA。这是全工具唯一的出口。"""

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
        self.quota: dict = {}      # 最近一次 OpenAlex 响应的额度信息

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


    def get(self, url: str, *, params=None, headers=None, stream=False,
            max_retries: int = 4):
        if is_forbidden(url):
            raise PermissionError(
                "拒绝抓取订阅制页面 URL（需鉴权，违反出版社条款）。"
                "爱思唯尔全文请走官方 API：配置 elsevier_api_key。"
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
                # 超时 / 连不上，多试几次也没用 —— 试探一次就够了，
                # 否则 4 次 × 30 秒超时 = 干等两分钟，用户以为程序卡死
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
                # 额度耗尽时 Retry-After 会给几万秒，别真等
                if r.status_code == 429 and hinted > 300:
                    host = urlparse(url).hostname or ""
                    if "openalex" in host:
                        reset_h = hinted / 3600
                        raise QuotaExceeded(
                            f"OpenAlex 的免费共享额度用完了（按 IP 共享 $0.10/天，"
                            f"{reset_h:.1f} 小时后重置）。\n"
                            "申请一个免费 key 就能立刻恢复：额度独立 $1/天，"
                            "还能翻页超过 200 条。\n"
                            "  https://openalex.org/settings/api\n"
                            "拿到后填进 config.json 的 openalex_api_key。")
                    raise QuotaExceeded(
                        f"{host} 返回 429，额度可能已用尽（Retry-After {hinted:.0f}s）")
                wait = min(hinted, 30) if hinted else min(2 ** attempt * 2, 30)
                log(f"    · HTTP {r.status_code}，{wait:.0f}s 后重试 ({attempt + 1}/{max_retries})")
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
                    f"连不上 {host}，试了两次都不通。\n"
                    "可能原因：① 网络没连上 / 需要代理；② 对方服务临时故障；"
                    "③ 该域名在你所在的网络被阻断。\n"
                    f"先用浏览器打开 https://{host} 试试——打得开再重试，打不开就是网络问题。\n"
                    f"（底层错误：{type(last_exc).__name__}）")
            raise last_exc
        raise RuntimeError(f"重试耗尽：{url}")


# --------------------------------------------------------------------------- #
# 中文检索词 → 英文（食品科学常用术语，避免机翻把专业词翻歪）
# --------------------------------------------------------------------------- #
ZH_TERMS = {
    # 样品 / 对象
    "南美白对虾": "Litopenaeus vannamei", "凡纳滨对虾": "Litopenaeus vannamei",    "对虾": "shrimp", "虾头": "shrimp head", "虾壳": "shrimp shell",
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

# 香气 / 脂质方向的补充术语（2026-11-04 加，用于检索与库内搜索的中英互转）
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
    """MyMemory 免费翻译（无 key，限量）。失败返回空串。"""
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
    中文检索词 → 英文。先查术语表（按词长降序做最长匹配），
    残留的中文片段再走免费翻译兜底。
    返回 (英文查询串, 仍未翻译的片段列表)。
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
# 检索源
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
# OpenAlex 账户与额度
#
# OpenAlex 2026 起按用量计费：无 key 时整个出口 IP 共享 $0.10/天，
# 自己的 key 独立 $1.00/天。额度信息在响应头里（X-RateLimit-*-USD）。
# --------------------------------------------------------------------------- #
OPENALEX_SIGNUP = "https://openalex.org/settings/api"
QUOTA_TTL = 300          # 额度缓存秒数，别为了看数字反复烧钱


def _parse_quota(headers) -> dict:
    def num(k, cast=float):
        try:
            v = headers.get(k)
            return cast(v) if v not in (None, "") else 0
        except (TypeError, ValueError):
            return 0

    limit_usd = num("X-RateLimit-Limit-USD")
    return {
        "has_key": limit_usd > 0.5,      # 0.10 = 共享额度，1.00 = 自己的 key
        "limit_usd": limit_usd,
        "remaining_usd": num("X-RateLimit-Remaining-USD"),
        "limit_calls": int(num("X-RateLimit-Limit")),
        "remaining_calls": int(num("X-RateLimit-Remaining")),
        "reset": int(num("X-RateLimit-Reset")),
        "at": time.time(),
    }


def openalex_accounts(cfg: dict) -> list[dict]:
    """账户列表。老配置只有单个 openalex_api_key 时，合成一条。"""
    accs = [dict(a) for a in (cfg.get("openalex_accounts") or []) if a.get("api_key")]
    if not accs and cfg.get("openalex_api_key"):
        accs = [{
            "label": cfg.get("email") or "默认账户",
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
    """把当前账户的 key 同步到 openalex_api_key（取数逻辑只认这个字段）。"""
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
    """加入账户列表并返回下标（已存在同名/同 key 就复用）。"""
    accs = openalex_accounts(cfg)
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("API key 不能为空")
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
        "label": (label or "").strip() or (email or "新账户"),
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
    发一次极轻量的请求读额度头（5 分钟内复用缓存，避免为看数字反复消耗额度）。
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
        return {"error": "这个 key 还没激活，去邮箱点激活链接",
                "not_activated": True}
    if r.status_code == 429:
        return {"error": "额度已用完，UTC 午夜重置", "exhausted": True}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    return getattr(sess, "quota", {}) or {}


def request_openalex_key(email: str) -> dict:
    """
    用邮箱申请一把新 key（OpenAlex 官方接口）。
    返回 {"api_key": ..., "message": ...}；key 需要点邮件里的激活链接才生效。
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValueError("邮箱格式不对")
    try:
        r = requests.post("https://api.openalex.org/api-keys",
                          json={"email": email}, timeout=45,
                          headers={"Content-Type": "application/json",
                                   "User-Agent": f"paperfetch/{VERSION} (academic use)"})
    except requests.RequestException as e:
        raise RuntimeError(f"申请失败：{type(e).__name__}")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"申请失败：HTTP {r.status_code} {r.text[:150]}")
    d = r.json()
    out = {"api_key": d.get("api_key", ""), "email": d.get("email", email),
           "expires": d.get("activation_link_expires", ""),
           "budget": d.get("daily_budget_usd_after_activation")}
    # 顺便解析出激活链接（如果有），方便直接提示用户
    act = d.get("activation_link") or d.get("activation_url") or ""
    if act:
        out["activation_link"] = act
    return out


# --------------------------------------------------------------------------- #
# 布尔检索：AND / OR / NOT / 括号 / 字段限定
#
# OpenAlex 的 filter 语义（已实测验证）：
#   同一 key 重复（逗号） = AND      tiab:shrimp,tiab:aroma  -> 439（交集）
#   管道符 |              = OR       tiab:shrimp|aroma       -> 224,891（并集）
#   前置 !                = NOT      tiab:!fish              -> 服务端生效
#   双引号                = 精确短语 tiab:"lipid oxidation"
# --------------------------------------------------------------------------- #
FIELD_MAP = {
    "all": "default.search",
    "tiab": "title_and_abstract.search",
    "title": "title.search",
    "abstract": "abstract.search",
    "fulltext": "fulltext.search",
}
FIELD_LABEL = {
    "all": "任意字段",
    "tiab": "标题+摘要",
    "title": "标题",
    "abstract": "摘要",
    "fulltext": "全文",
}
# 带 * 或 ? 通配符时必须用不词干化的 exact 字段，否则 OpenAlex 直接 400：
#   "Wildcards (* or ?) require the exact (no-stem) field"
FIELD_EXACT_MAP = {
    "all": "default.search.exact",
    "tiab": "title_and_abstract.search.exact",
    "title": "title.search.exact",
    "abstract": "abstract.search.exact",
    "fulltext": "fulltext.search.exact",
}

# 多分支合并后本地重排用的字段（字段名, 是否降序）
_SORT_FIELD = {
    "cited_desc": ("cited_by", True),
    "cited_asc": ("cited_by", False),
    "date_desc": ("year", True),
    "date_asc": ("year", False),
}

# 排序方式：key -> (OpenAlex sort 串, 界面标签, 是否多分支可统一排序)
SORTS = {
    "relevance": (None, "相关度", False),
    "cited_desc": ("cited_by_count:desc", "被引 高→低", True),
    "cited_asc": ("cited_by_count:asc", "被引 低→高", True),
    "date_desc": ("publication_date:desc", "年份 新→旧", True),
    "date_asc": ("publication_date:asc", "年份 旧→新", True),
}
# 文献类型：界面 key -> (OpenAlex type 过滤值, 界面标签)
# OpenAlex 的 type 是枚举字段，多个值用 | 表示 OR（实测 type:article|review 的 count
# 正好等于两个单独相加，可用；但 title.search:review|survey 这种**不**支持 OR）。
DOC_TYPES = {
    "article":      ("article",                  "研究论文"),
    "review":       ("review",                   "综述"),
    "article+review": ("article|review",         "研究论文 + 综述"),
    "dissertation": ("dissertation",             "学位论文"),
    "preprint":     ("preprint",                 "预印本"),
    "book-chapter": ("book-chapter",             "图书章节"),
    "all":          ("",                         "不限类型"),
}
TYPE_LABEL = {
    "article": "研究论文", "review": "综述", "dissertation": "学位论文",
    "preprint": "预印本", "book-chapter": "图书章节", "book": "图书",
    "conference-paper": "会议论文", "proceedings-article": "会议论文",
    "dataset": "数据集", "editorial": "社论", "letter": "通讯",
    "erratum": "勘误", "paratext": "辅文", "peer-review": "同行评议",
    "reference-entry": "词条", "report": "报告", "standard": "标准",
    "textbook": "教材", "libguides": "指南", "withdrawn": "已撤稿",
    # OpenAlex 的实际枚举里还有这些，不补上界面就会冒出英文
    "post": "短文", "magazine-article": "杂志文章", "suppressed": "已屏蔽",
    "component": "组件", "journal-issue": "整期",
    "other": "其他",
}

# 标题像综述的启发式：OpenAlex 把不少综述标成 article，靠这个在界面上提醒一句，
# 不参与检索过滤（宁可少提示，也不要满屏「疑似」）。
# ⚠ 刻意**不**收 advances in / progress in / recent developments 这类词 ——
# 实测 title:"advances in shrimp" 前 10 条有 6 条命中，其中不少是正常的实验论文，
# 满屏灰标等于没有标。
_REVIEW_TITLE = re.compile(
    r"\b(a\s+)?(systematic review|review|survey|overview|meta-?analysis|"
    r"state[- ]of[- ]the[- ]art)\b", re.I)


def looks_like_review(title: str) -> bool:
    """标题里出现 review / survey / overview / meta-analysis 这类词。"""
    t = title or ""
    if not t:
        return False
    if _REVIEW_TITLE.search(t):
        return True
    return "综述" in t or "研究进展" in t


def is_maybe_review(rec: dict) -> bool:
    """
    只有被标成研究论文（或压根没标类型）的才谈「疑似综述」。
    学位论文 / 预印本本身就不是 article，界面上挂的是它们自己的类型标，
    再叠一个「疑似综述」会跟日志里的计数对不上。
    """
    wt = (rec.get("work_type") or "").strip().lower()
    if wt not in ("", "article"):
        return False
    return looks_like_review(rec.get("title", ""))


# 表达式里可写的字段前缀（小写匹配）
FIELD_ALIAS = {
    "all": "all", "default": "all", "any": "all", "任意": "all", "全部": "all",
    "tiab": "tiab", "title_abstract": "tiab", "标题摘要": "tiab",
    "title": "title", "ti": "title", "题名": "title", "标题": "title",
    "abstract": "abstract", "ab": "abstract", "摘要": "abstract",
    "fulltext": "fulltext", "full": "fulltext", "全文": "fulltext",
}
MAX_GROUPS = 6          # 合并后允许的请求数上限
MAX_EXPAND = 64         # 展开时的中间保护（合并前可能有很多分支）


class QueryError(ValueError):
    """检索式写错时抛出，message 直接给用户看。

    kind 用来区分两类，前端据此给不同的补充提示：
      - "syntax"（默认）：检索式本身写错，提示去检查括号 / AND OR
      - "filtered"：检索式没问题，是这篇被年份 / OA / 文献类型筛掉了
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
            # 只有裸词才可能是运算符，带引号的 "AND" 是普通检索词
            if up in ("AND", "OR", "NOT") and w[:1] not in ("\"", "'"):
                toks.append((up, ""))
            else:
                toks.append(("T", w))
    return toks


def _term(word: str) -> tuple:
    """解析单个检索词：-排除前缀、字段限定、引号短语。"""
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
    """把否定推到叶子（德摩根），方便转 DNF。"""
    if node is None:
        return None
    if node[0] == "term":
        return ("term", node[1], node[2], not node[3], node[4])
    return ("or" if node[0] == "and" else "and", [_negate(c) for c in node[1]])


# 单个虚词（in / of / to …）在 OpenAlex 的 *.search 字段里匹配不到东西，
# 但被当成一个 filter 塞进去，整条检索就直接变 0 命中 ——
# 粘一整句标题进去时必踩（"Changes in lipid profiles ... submitted to ..." → 0 条）。
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
    """是不是一个可以安全丢掉的虚词。引号短语、通配符、带空格的都不动。"""
    s = v.strip()
    if not s:
        return False
    if s[0] in "\"'" or "*" in s or "?" in s or " " in s:
        return False
    return s.lower() in STOPWORDS


# 判断"这串是不是一句粘进来的标题"：没有布尔运算符、没有引号/通配符、没有字段前缀
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
    """把标题收拾成能塞进 filter 的短语：逗号是 filter 分隔符，必须去掉。"""
    s = " ".join((q or "").split())
    s = re.sub(r"[,;|]", " ", s)
    s = re.sub(r"[()\[\]{}]", " ", s)
    s = re.sub(r"[\"'“”‘’]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


# ---- 直接给 DOI / OpenAlex ID / PMID：这不是"检索"，是"定位" ----
# 不加这段的话，"10.1111/ijfs.15747" 会被拆成文本去搜，返回一堆同数字的无关文献。
_DOI_Q = re.compile(
    r"^\s*(?:(?:https?://)?(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+?)\s*$", re.I)
_OAID_Q = re.compile(r"^\s*(?:(?:https?://)?openalex\.org/)?([Ww]\d{6,})\s*$")
_PMID_Q = re.compile(r"^\s*pmid:?\s*(\d{6,9})\s*$", re.I)


def parse_identifier(q: str):
    """输入是不是一个标识符 → ("doi"|"oaid"|"pmid", 值)，否则 None。"""
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
    Crossref 的 type 是另一套词表（journal-article / posted-content / edited-book…）。
    认不出来的归 "other"，否则界面上会冒出 edited-book 这种英文原值；
    **但空值要原样返回空**，让「粘 DOI」时的类型校验跳过，而不是把没标类型的当「其他」拦下。
    """
    t = (raw or "").strip().lower()
    if not t:
        return ""
    return _CROSSREF_TYPE.get(t, "other")


def _crossref_rec(doi: str) -> dict | None:
    """OpenAlex 没有这个 DOI 时，用 Crossref 的元数据兜底（下载仍走 Unpaywall）。"""
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
        # Crossref 的 type 是另一套词表，映射见 _crossref_type()
        "work_type": _crossref_type(m.get("type")),
        "found_via": "crossref",
    }


def _lookup_work(kind: str, val: str, cfg: dict, sess: PoliteSession):
    """按标识符直查单篇。返回 (rec|None, 说明)。"""
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
    递归下降解析。优先级：NOT > AND > OR；括号改变结合。
    相邻两个操作数之间没写运算符时，用 default_op 补上。
    """
    seq = []
    for t in _tokenize(q):
        if seq:
            prev = seq[-1][0]
            if prev in ("T", ")") and t[0] in ("T", "(", "NOT"):
                seq.append((default_op, ""))
        seq.append(t)
    if not seq:
        raise QueryError("检索词是空的")

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
                raise QueryError("括号没配对：这里少一个右括号 )")
            take()
            return node
        if t == "T":
            return _term(take()[1])
        raise QueryError("检索式里有多余的符号：%r" % (take()[1],))

    node = p_or()
    if pos[0] != len(seq):
        raise QueryError("括号没配对")
    return node


def to_dnf(node) -> list[list[tuple]]:
    """转成析取范式：[ [AND 项...], [AND 项...] ]，每个子列表 = 一次请求。"""
    k = node[0]
    if k == "term":
        return [[node]]
    if k == "or":
        out = []
        for c in node[1]:
            out.extend(to_dnf(c))
        if len(out) > MAX_EXPAND:
            raise QueryError("OR 分支太多，请简化检索式")
        return out
    out = [[]]
    for c in node[1]:
        sub = to_dnf(c)
        out = [a + b for a in out for b in sub]
        if len(out) > MAX_EXPAND:
            raise QueryError("AND/OR 组合太复杂，请简化检索式")
    return out


def expr_text(node, default_field: str = "all", nest: bool = False) -> str:
    """把解析树还原成可读表达式，给界面显示用。"""
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
    v = v.strip(":;.\u3002\uff1a\uff1b")          # 标题里的冒号/句号别带进 filter
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
                raise QueryError("检索词全是 in / of / to 这类虚词，没有实际检索意义，换成实词再试")
            raise QueryError("每个 OR 分支至少要有一个正向关键词（不能只有排除项）")
        groups.append({"filters": filters, "negs": negs})
    return groups


def merge_groups(groups: list[dict]) -> list[dict]:
    """
    把只在某一个位置上不同的 OR 分支合并成一个管道表达式，省掉重复请求：
        [shrimp, aroma] + [shrimp, flavor]  ->  [shrimp, aroma|flavor]
        (a OR b) AND (c OR d) 的 4 个分支   ->  1 个分支 a|b, c|d
    只在字段相同、排除项相同、值里没有引号短语时才合并。
    """
    def ok(v: str) -> bool:
        # 引号短语与管道混用不可靠；单个值也不宜堆太多同义词
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
    跑一个 filter 组，翻页到 want 条。
    返回 (记录, 该组命中的总数, 是否被「无 key 只能取前 200 条」截断)。

    最后那个标记很重要：没配 openalex_api_key 时，OpenAlex 第 1 页之后**不给 next_cursor**，
    程序只会静默停在 200 条 —— 用户填 500 却拿到 200，还看不出为什么。有了这个标记，
    界面就能明确说「要更多得先配 key」。
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
            # ⚠ 不能写死某个邮箱 —— 这是别人用这个程序时会看到的提示
            who = (cfg.get("email") or "").strip()
            box = f"{who} 的收件箱" if who else "你申请 key 时填的那个邮箱"
            raise QuotaExceeded(
                f"OpenAlex 的 key 还没激活。\n"
                f"去{box}里找一封标题为「Activate your OpenAlex API key」的邮件，点里面的按钮。"
                "激活后不用改任何东西，直接重开程序即可。\n"
                "（没收到就去垃圾箱看看，或回 https://openalex.org/settings/api 重发）")
        if r.status_code == 401 and "cursor" in r.text.lower():
            # 同一句报错有两种原因，得分开说 —— 不然配了 key 的人会一头雾水
            if cfg.get("openalex_api_key"):
                # 配了 key 还被拒：那是 key 本身的问题，不能一味降级，得让人知道
                raise QuotaExceeded(
                    "OpenAlex 不接受当前这个 API key（可能已失效，或今天的免费额度用完了）。\n"
                    "两种处理：\n"
                    "  ① 去 https://openalex.org/settings/api 看一下 key 的状态，或换一个账户的 key；\n"
                    "  ② 临时把 config.json 里的 openalex_api_key 清空 —— 还能继续用，"
                    "但每次最多取前 200 条（OpenAlex 不给第 2 页）。")
            # 没配 key：**不抛错**，把已经拿到的那一页返回去并标记 capped。
            # 直接抛错会让人连前 200 条都看不到，比"少给一点但说清楚"更糟。
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
            # 还有命中、但拿不到下一页、又没有 key —— 就是被「翻页要 key」挡住了
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
    检索 OpenAlex。q 支持布尔语法：

        shrimp AND aroma                    两个词都要有（默认字段）
        shrimp AND (aroma OR flavor)        括号分组
        title:shrimp AND abstract:aroma     字段限定
        "lipid oxidation" AND shrimp        引号短语
        shrimp AND NOT fish  /  shrimp -fish  排除

    多个 OR 分支会拆成多次请求再合并去重（交错合并，保留相关性）。
    info 传一个 dict 进来可回传解析细节。
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
    # 文献类型。以前写死 type:article —— 综述（type:review）和学位论文根本搜不出来
    dt_key = (doc_type or "article").strip().lower()
    dt_val, dt_label = DOC_TYPES.get(dt_key, DOC_TYPES["article"])
    if dt_key not in DOC_TYPES:
        dt_key = "article"
    if dt_val:
        base.append(("type", dt_val))

    sort_key, sort_label, sort_global = SORTS.get(sort, SORTS["relevance"])

    # 给的是 DOI / OpenAlex ID / PMID：直接定位，别拆成文本去搜（会返回一堆无关结果）
    ident = parse_identifier(q)
    if ident:
        kind, val = ident
        rec, why = _lookup_work(kind, val, cfg, sess)
        if rec is None and kind == "doi":
            rec = _crossref_rec(val)          # OpenAlex 没有就用 Crossref 兜底
        if rec is None:
            extra = "" if why in ("notfound", "") else f"（{why}）"
            raise QueryError(f"没找到 {kind.upper()} = {val}{extra}，确认一下有没有抄错")
        y = rec.get("year")
        if year_from and y and y < year_from:
            raise QueryError(f"这篇是 {y} 年的，被「起始年份 {year_from}」筛掉了", "filtered")
        if year_to and y and y > year_to:
            raise QueryError(f"这篇是 {y} 年的，被「截止年份 {year_to}」筛掉了", "filtered")
        if oa_only and not rec.get("is_oa"):
            raise QueryError("这篇不是 OA，被「只看 OA」筛掉了", "filtered")
        rt = rec.get("work_type") or ""
        if rt and dt_key != "all" and rt not in dt_val.split("|"):
            raise QueryError(
                f"这篇是「{TYPE_LABEL.get(rt, rt)}」，被当前的「文献类型：{dt_label}」筛掉了。"
                f"把类型改成「不限类型」再试。", "filtered")
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

    # 粘一整句标题进来时，先按「整句短语」试一把：通常一条就中。
    # 没命中再退回逐词 AND，所以最坏情况跟以前一样，不会更差。
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
            "这个检索式合并后仍要拆成 %d 次查询，超过上限 %d。"
            "建议改成「同义词用 OR 写在同一组」（如 aroma OR flavor）"
            "，或减少括号分支。" % (len(groups), MAX_GROUPS))

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
            # 只报"被挡住了"这个事实，不报具体条数 —— 多分支去重后条数跟单组对不上
            info["capped_no_key"] = True
        return recs, total

    # 多分支：各组分别取，再去重
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
        # 被引/年份这类字段跨组可比：先去重，再整体重排
        out = dedup([r for b in buckets for r in b])
        field, rev = _SORT_FIELD[sort]
        out.sort(key=lambda r: (r.get(field) or 0), reverse=rev)
        return out[:limit], total

    # 相关度：各组分数不可比，按「每组第 1 条、每组第 2 条…」交错合并
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
    """拿 OA 全文直链。Unpaywall 是版权方授权的 OA 索引，非爬取。"""
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
    # 仓储版优先：机构库 / Zenodo 这类站点对程序化访问友好，出版商页面常挂 WAF
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
    """arXiv 预印本：CC 授权、官方 PDF 直链、明确允许程序化访问。"""
    doi = rec.get("doi") or ""
    m = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.I)
    if m:
        rec.setdefault("pdf_candidates", []).append(
            f"https://arxiv.org/pdf/{m.group(1)}.pdf")


def resolve_europepmc(rec: dict, sess: PoliteSession) -> None:
    """Europe PMC 查 PMCID；PMC OA Service 是 NIH 官方授权的全文索引。"""
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
    """爱思唯尔官方 Article Retrieval API —— 唯一被授权程序化取全文的路径。"""
    doi = rec.get("doi") or ""
    if not doi.startswith("10.1016") or not cfg.get("elsevier_api_key"):
        return
    h = {"X-ELS-APIKey": cfg["elsevier_api_key"], "Accept": "application/pdf"}
    if cfg.get("elsevier_insttoken"):
        h["X-ELS-Insttoken"] = cfg["elsevier_insttoken"]
    rec["_elsevier_headers"] = h
    rec["elsevier_ready"] = True


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def _slug(s: str, n: int = 55) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return (s[:n].rstrip("_") or "")


def surname(first_author: str) -> str:
    """
    从 first_author 里取姓。要同时认两种写法：
      · "Y. Zhang"   → Zhang   （OpenAlex 的格式，也是主流）
      · "Zhang, Y."  → Zhang   （Zotero / Crossref / 引文扩散可能给这种）
    以前只取 split()[-1]，遇到逗号格式会取到 "Y."，文件名叫 2024_Y_xxx.pdf。
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
    """PDF 页数；解析失败返回 0。"""
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(blob), strict=False).pages)
    except Exception:
        return 0


def elsevier_full_text(doi: str, headers: dict, sess: "PoliteSession") -> str:
    """取全文 XML（勾了 TDM 条款就能拿），失败返回空串。"""
    try:
        h = dict(headers); h["Accept"] = "text/xml"
        r = sess.get(f"https://api.elsevier.com/content/article/doi/{doi}",
                     params={"view": "FULL"}, headers=h)
        if r.status_code == 200 and "<article" in r.text[:3000]:
            return r.text
    except Exception:
        pass
    return ""


# 存储格式：拿不到完整 PDF 时的正文存法
SAVE_FORMATS = {
    "pdf":   {"label": "完整 PDF",
              "desc": "能打印、能批注、能截图引用",
              "cost": "最占地方：一篇 2–6 MB，1000 篇约 5.8 GB"},
    "xml":   {"label": "全文 XML",
              "desc": "正文、章节、图注、参考文献齐全，记事本可直接打开",
              "cost": "一篇约 780 KB，1000 篇约 780 MB"},
    "xmlgz": {"label": "XML 压缩",
              "desc": "内容与 XML 完全一致，AI 能直接读；人要看需先解压",
              "cost": "一篇约 80 KB，1000 篇约 80 MB"},
}
DEFAULT_SAVE_FORMAT = "xmlgz"


def save_format(cfg: dict) -> str:
    fmt = (cfg.get("save_format") or DEFAULT_SAVE_FORMAT).lower()
    return fmt if fmt in SAVE_FORMATS else DEFAULT_SAVE_FORMAT


def download_one(rec: dict, cfg: dict, sess: PoliteSession,
                 out_dir: Path) -> tuple[str, Path | None, str]:
    """
    返回 (状态, 路径, 来源)。状态: ok / preview / skip_dup / no_source。

    存储格式由 config 的 save_format 决定（pdf / xml / xmlgz）。
    pdf 只在真拿到多页 PDF 时才存；xml 系列存 Elsevier 的全文 XML。
    keep_preview_pdf 控制「只有首页」时要不要额外存那张首页图。
    """
    fmt = save_format(cfg)
    keep_prev = bool(cfg.get("keep_preview_pdf"))
    base = target_name(rec, "")

    # 任一形态已存在就算有了，别重复下
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

    # 1) 爱思唯尔官方 API（合法且是出版社原文）
    if rec.get("elsevier_ready") and rec.get("doi"):
        url = f"https://api.elsevier.com/content/article/doi/{rec['doi']}"
        try:
            r = sess.get(url, params={"httpAccept": "application/pdf"},
                         headers=rec["_elsevier_headers"])
            if r.status_code == 200 and is_pdf(r.content):
                pages = pdf_page_count(r.content)
                whole = pages > 1

                # 选了 PDF 且确实拿到整本 → 直接存
                if whole and fmt == "pdf":
                    p, via = save_pdf(r.content, "elsevier")
                    return "ok", p, via

                xml = elsevier_full_text(rec["doi"], rec["_elsevier_headers"], sess)
                if xml:
                    p, via = save_xml(xml, "elsevier")
                    if whole:
                        rec["_els_note"] = (
                            f"已存全文 XML（PDF 有 {pages} 页，按「{SAVE_FORMATS[fmt]['label']}」"
                            f"设置只保留文本）")
                    else:
                        rec["_els_note"] = (
                            "PDF 只有首页（校外无全文权限），已存全文 XML"
                            + ("＋首页预览图" if keep_prev else ""))
                    if keep_prev and not whole:
                        (out_dir / (base + ".preview.pdf")).write_bytes(r.content)
                    return "ok", p, via

                # 没有 XML 可取，退回 PDF
                if whole:
                    p, via = save_pdf(r.content, "elsevier")
                    rec["_els_note"] = "该刊没有全文 XML，改存 PDF"
                    return "ok", p, via
                if keep_prev:
                    prev = out_dir / (base + ".preview.pdf")
                    prev.write_bytes(r.content)
                    rec["_els_note"] = f"只有首页（{pages} 页），且拿不到 XML"
                    return "preview", prev, "elsevier-preview"
                rec["_els_note"] = "只有首页且拿不到 XML，未保存"
            elif r.status_code in (401, 403):
                rec["_els_note"] = (
                    "这家没给全文权限 —— 要么当前 IP 不在学校授权范围内（校园网 / 校园 VPN 下重跑即可，"
                    "校内只要 API key，不必非得有 insttoken），要么学校没订这本刊；改试开放渠道")
        except PermissionError:
            pass
        except Exception as e:
            rec["_els_note"] = str(e)[:120]

    # 2) OA 渠道：这类没有 XML，只能存 PDF
    license_ok = (rec.get("license") or "").lower() in (
        "cc-by", "cc0", "public-domain", "other-oa")
    for u in rec.get("pdf_candidates") or []:
        if not u or u.lower().endswith(".tar.gz"):
            continue
        verdict = sess.robots.check(u)
        if verdict is False:
            sess.stats["robots_blocked"] += 1
            rec.setdefault("_notes", []).append(f"robots 禁止({urlparse(u).hostname})")
            continue
        if verdict is None and not license_ok:
            rec.setdefault("_notes", []).append(f"许可不明({urlparse(u).hostname})")
            continue
        try:
            r = sess.get(u)
        except PermissionError as e:
            rec.setdefault("_notes", []).append(f"跳过({e})")
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
# 文献库
# --------------------------------------------------------------------------- #
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)


def _default_library() -> Path:
    """
    默认文献库。

    打包后就是 exe 同级的 文献库\。
    从源码跑（python paperfetch_ui.py）时 app_dir() 是「源码」，
    如果上一层已经有 文献库\（也就是打包后的那个），就用它 ——
    免得源码目录里冒出一个空的、和外面不是一回事的库。
    """
    base = app_dir()
    if not getattr(sys, "frozen", False):
        outer = base.parent / "文献库"
        if outer.is_dir():
            return outer
    return base / "文献库"


def library_dir(cfg: dict) -> Path:
    """
    文献库目录。config 里 library_dir 为空时用「程序目录/文献库」。

    只算路径，**不建目录** —— 以前每次调用都 mkdir，结果一开界面就凭空多出个空文件夹。
    真正要写文件的地方（下载、生成清单、写索引）自己 mkdir。
    """
    custom = (cfg.get("library_dir") or "").strip()
    return Path(custom) if custom else _default_library()


def _doi_from_name(name: str) -> str:
    m = DOI_RE.search(name)
    return m.group(0).rstrip(".,;)]}") if m else ""


def _doi_from_pdf(path: Path) -> tuple[str, str]:
    """读前两页抽 DOI 与标题。失败返回空串，不抛异常。"""
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


# 文献库里能认的文件：下载默认存 xmlgz，所以 .xml.gz 才是主力
LIB_SUFFIXES = (".pdf", ".xml", ".xml.gz")


def _is_lib_file(name: str) -> bool:
    return name.lower().endswith(LIB_SUFFIXES)


def _plain_name(name: str) -> str:
    """去掉 .pdf / .xml / .xml.gz 后的名字，用作标题兜底。"""
    low = name.lower()
    for suf in (".xml.gz", ".xml", ".pdf"):
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


def _doi_from_xml(path: Path, head_kb: int = 320) -> tuple[str, str]:
    """从 JATS / Elsevier 全文 XML 抽 DOI 与标题。只读文件头，不整篇加载。"""
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
    递归扫描文献库与额外来源里的 PDF / XML / XML 压缩，抽 DOI 建索引。
    索引缓存在 文献库/.index.json，按 (大小, mtime) 判断是否需要重解析。
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
            # 同一篇可能既有 PDF 又有 XML（md5 不同），按 DOI 判重更有意义
            key = doi.lower() if doi else md5v
            if key in seen:
                dup += 1
                continue
            seen[key] = str(p)
            # 标题怎么取：XML 里的是真标题（JATS 元数据）；PDF 里抽出来的常是页眉，
            # 所以自己命名的 PDF 用文件名（年_作者_标题），Zotero 那类才用抽取结果
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
# 输出
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
    下载台账：每篇下了什么、走哪条路、成功没有。

    两条规矩：
    1. **累积而不是覆盖**——同一目录下反复下载，后来的记录并进已有台账（按 DOI、无 DOI 用标题判重），
       否则第二趟就把第一趟的账冲掉了。
    2. **成功的记录不会被后来的 skip_dup 覆盖**——文件早就在了，不该看成"没下"。
       顺手把本地缓存里已有的中译 / IF / 分区带上，**只读缓存、绝不为了写台账去联网**。
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
                continue                       # 已经下到过，别再记成"跳过"
            rows[pos[k]] = m
        else:
            if k:
                pos[k] = len(rows)
            rows.append(m)

    zh, jn = {}, {}
    try:
        import enrich as _en          # 延迟导入：只是蹭它的缓存，缺文件也不影响下载
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
    """RIS：拖进 Zotero / EndNote 就能建条目，再用「查找可用 PDF」批量补全文。"""
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
  if(c) c.textContent = '已下 ' + done + ' / ' + n;
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
  btn.textContent = on ? '显示全部' : '只看没下的';
  filterRows(on);
}
function clearAll(){
  if(!confirm('清空全部勾选？已经下好的记录会一起没掉。')) return;
  saveSt({}); paint();
}
function batchNext(){
  var box = document.getElementById('batch');
  var todo = [].filter.call(document.querySelectorAll('tbody tr'), function(tr){
    return !tr.classList.contains('done');
  });
  box.innerHTML = '';
  if(!todo.length){
    box.textContent = '都处理完了，没有待下的了。';
    box.hidden = false; return;
  }
  var take = todo.slice(0, 5);
  var head = document.createElement('b');
  head.textContent = '接下来 ' + take.length + ' 篇';
  var tip = document.createElement('span');
  tip.style.cssText = 'color:#6a7a4a;margin-left:4px';
  tip.textContent = '（按住 Ctrl 点，可一次开多个标签页）';
  box.appendChild(head); box.appendChild(tip);
  box.appendChild(document.createTextNode('：'));
  var ol = document.createElement('ol');
  take.forEach(function(tr){
    var a = tr.querySelector('a.tl');
    var li = document.createElement('li');
    var href = a ? (a.getAttribute('href') || '') : '';
    if(href){
      var link = document.createElement('a');
      link.href = href; link.target = '_blank'; link.rel = 'noopener';
      link.textContent = a.textContent;          // 用 textContent，标题里的 < & 不会被当标签
      li.appendChild(link);
    }else{
      var span0 = document.createElement('span');
      span0.textContent = a ? a.textContent : '(无链接)';
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
    var b=document.getElementById('cp'); b.textContent='已复制 '+s.filter(Boolean).length+' 条'; 
    setTimeout(function(){b.textContent='复制全部 DOI'},1800);
  });
}
function copyOne(btn){
  navigator.clipboard.writeText(btn.dataset.doi).then(function(){
    var t=btn.textContent; btn.textContent='已复制'; 
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
  if(tr && !tr.classList.contains('done')) setDone(tr, true);   // 点过标题就当处理了
});
paint();
"""


def write_missing_html(recs: list[dict], path: Path, fmt_label: str = "") -> None:
    """
    没人给全文的那批，生成一张能直接在校园网里点着下载的清单。
    不抓页面、不绕鉴权 —— 只是把 DOI 摆好，点开就是出版社自己的页面。
    """
    from html import escape as e

    counts: dict[str, int] = {}
    rows = []
    for i, r in enumerate(recs, 1):
        pub, kind = publisher_of(r.get("doi", ""))
        counts[pub or "未知"] = counts.get(pub or "未知", 0) + 1
        doi = (r.get("doi") or "").strip()
        link = f"https://doi.org/{doi}" if doi else (r.get("landing") or "")
        cls = kind if kind in ("sub", "oa", "api") else ""
        key = doi.lower() if doi else "t:" + (r.get("title") or "")[:80].lower()
        rows.append(
            f'<tr data-k="{e(key)}">'
            f'<td class="ck"><input type="checkbox" aria-label="标记已下"></td>'
            f'<td class="n">{i}</td>'
            f'<td class="t"><a class="tl" href="{e(link)}" target="_blank" rel="noopener">'
            f'{e(r.get("title") or "(无标题)")}</a>'
            f'<div class="doi">{e(doi or "—")}</div></td>'
            f'<td>{e(r.get("journal") or "—")}</td>'
            f'<td>{e(str(r.get("year") or ""))}</td>'
            f'<td><span class="pub {cls}">{e(pub or "未知")}</span></td>'
            f'<td class="cp" data-doi="{e(doi)}">'
            f'<button class="ghost" onclick="copyOne(this)" data-doi="{e(doi)}">复制 DOI</button></td>'
            "</tr>")

    stat = "　·　".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    html = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>待下载清单 · {len(recs)} 篇</title><style>{_MISS_CSS}</style></head><body>"
        f"<h1>待下载清单 · {len(recs)} 篇</h1>"
        f'<div class="sub">由「文献妙妙小工具」生成　·　全选已检索 {stat}</div>'
        '<div class="tip">'
        "<b>怎么用（三条路，按省事程度排）：</b>"
        "<ol>"
        "<li><b>校园网 / 学校 VPN 内</b>：直接点标题，进出版社页面右上角下 PDF。"
        "Wiley、ACS、T&amp;F 只认机构 IP，家里要挂学校 VPN。</li>"
        "<li><b>Zotero 批量</b>：把同目录的 <code>_missing_for_zotero.ris</code> 拖进 Zotero，"
        "全选条目 → 右键「查找可用的 PDF」，它会挨个试一遍。</li>"
        "<li><b>图书馆文献传递</b>：把 <code>_missing_for_library.csv</code> 交给图书馆，"
        "一般 1–2 个工作日发到邮箱。这条最稳，但慢。</li>"
        "</ol>"
        "本程序不抓这些页面：订阅刊的页面 URL 需要机构鉴权，脚本批量访问既违反出版社条款、"
        "也会把学校的出口 IP 拖进黑名单，所以这里只给链接，不给抓取。"
        "<div style=\"margin-top:8px;color:#6a7a4a\">"
        "<b>进度会记住</b>：点过标题（或自己打勾）就算「已下」，消掉一行；关掉再打开还在。"
        "勾选存在这台电脑的浏览器里，换电脑不同步。</div>"
        "</div>"
        '<div class="bar"><button id="cp" onclick="copyAll()">复制全部 DOI</button>'
        '<button class="ghost" id="bat" onclick="batchNext()">打开接下来 5 篇</button>'
        '<button class="ghost" id="only" onclick="onlyTodo(this)">只看没下的</button>'
        '<button class="ghost" onclick="clearAll()">清空勾选</button>'
        f'<span id="cnt">已下 0 / {len(recs)}</span>'
        f'<span class="sub" style="margin:0">{fmt_label}</span></div>'
        '<div class="batch" id="batch" hidden></div>'
        "<table><thead><tr><th></th><th style=\"width:44px\">#</th><th>标题 / DOI</th>"
        "<th style=\"width:210px\">期刊</th><th style=\"width:56px\">年</th>"
        "<th style=\"width:96px\">出版社</th><th style=\"width:112px\"></th></tr></thead><tbody>"
        + "".join(rows) +
        "</tbody></table>"
        f"<script>{_MISS_JS}</script></body></html>"
    )
    path.write_text(html, encoding="utf-8")



# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_search(args, cfg: dict) -> int:
    sess = PoliteSession(cfg)
    q = args.query
    if re.search(r"[\u4e00-\u9fff]", q):
        log("⚠ 检索词含中文：OpenAlex / Crossref 对中文支持很差，建议改英文关键词。")
        log("  例：内源脂质+对虾+香气 → 'endogenous lipid shrimp aroma'")
    log(f"检索：{q}")
    recs, total = search_openalex(q, cfg, sess, limit=args.limit,
                                  year_from=args.year_from, year_to=args.year_to,
                                  oa_only=args.oa_only, issn=args.issn)
    log(f"  OpenAlex 共命中 {total:,} 条，已取前 {len(recs)} 条")

    if args.resolve:
        log("解析全文链接（Unpaywall / Europe PMC / Elsevier API）…")
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
    log(f"已写出 {len(recs)} 条 → {out}")

    n_oa = sum(1 for r in recs if r.get("is_oa"))
    n_pdf = sum(1 for r in recs if r.get("pdf_url") or r.get("pdf_candidates"))
    log(f"OA 标记 {n_oa} 条 | 已定位到可用 PDF 直链 {n_pdf} 条 | 请求 {sess.stats['requests']} 次")
    return 0


def cmd_fetch(args, cfg: dict) -> int:
    src = Path(args.input)
    if not src.is_file():
        log(f"找不到输入文件：{src}")
        return 2
    if src.suffix.lower() == ".csv":
        with src.open(encoding="utf-8-sig") as f:
            recs = list(csv.DictReader(f))
    else:
        log("输入须为 search 产出的 .csv")
        return 2

    if args.limit:
        recs = recs[:args.limit]
    cap = int(cfg.get("max_downloads_per_run") or 200)
    if len(recs) > cap:
        log(f"⚠ 本次 {len(recs)} 条超过单次上限 {cap}，已截断。改 config.json 的 max_downloads_per_run 可放宽。")
        recs = recs[:cap]

    out_dir = Path(args.out_dir) if args.out_dir else library_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = PoliteSession(cfg)

    log(f"目标目录：{out_dir}   速率 {sess.rps} req/s   共 {len(recs)} 条")

    # 复用检索阶段拿到的链接
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
            log(f"    △ 只有首页，且拿不到 XML（可开「保存首页图」或换校园网重跑）")
        elif status == "skip_dup":
            log(f"    = 已存在/重复，跳过 ({via})")
        elif status == "no_source":
            missing.append(r)
            log(f"    ✗ 未获取 —— {why_missing(r)}")
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
            write_missing_html(missing, out_dir / "待下载清单.html",
                               f"共 {len(missing)} 篇没拿到全文")
        except Exception as e:                       # 清单是附加品，生成失败不影响主流程
            log(f"（清单生成失败：{type(e).__name__}）")
    write_bib(recs, out_dir / "_refs.bib")

    log("")
    log("─" * 60)
    log(f"完成：已保存 {stats['ok']} · 仅首页 {stats['preview']} · "
        f"已存在/重复 {stats['skip_dup']} · 无合法来源 {stats['no_source']} · 失败 {stats['fail']}")
    fmt = save_format(cfg)
    log(f"  存储格式：{SAVE_FORMATS[fmt]['label']}（{SAVE_FORMATS[fmt]['cost']}）")
    log(f"请求总数 {sess.stats['requests']}，重试 {sess.stats['retries']}，"
        f"robots 拦截 {sess.stats['robots_blocked']}")
    if missing:
        log(f"未获取 {len(missing)} 篇。三条路，按省事程度：")
        log(f"  ① 校园网/VPN 内点着下 → {out_dir / '待下载清单.html'}")
        log(f"  ② Zotero 批量找 PDF   → {out_dir / '_missing_for_zotero.ris'}")
        log(f"  ③ 图书馆文献传递       → {out_dir / '_missing_for_library.csv'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paperfetch",
        description="合规文献检索与 PDF 批量获取（官方 API + OA 渠道，不绕过任何访问控制）",
    )
    p.add_argument("--config", type=Path, help="配置文件路径（默认 ./config.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="多源检索，导出表格")
    s.add_argument("query", help="检索词，建议英文")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--year-from", type=int)
    s.add_argument("--year-to", type=int)
    s.add_argument("--issn", help="限定期刊 ISSN，如 0308-8146（Food Chemistry）")
    s.add_argument("--oa-only", action="store_true", help="只要开放获取")
    s.add_argument("--no-resolve", dest="resolve", action="store_false",
                   help="跳过全文链接解析，只出元数据（快）")
    s.add_argument("-o", "--out", help="输出 .csv 或 .xlsx")
    s.set_defaults(func=cmd_search, resolve=True)

    f = sub.add_parser("fetch", help="按检索结果批量下载 PDF")
    f.add_argument("input", help="search 产出的 .csv")
    f.add_argument("-o", "--out-dir", default="pdfs")
    f.add_argument("--limit", type=int)
    f.set_defaults(func=cmd_fetch)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if not cfg.get("email"):
        log("⚠ 未配置邮箱：Unpaywall 会拒绝请求，OA 全文将无法定位。")
        log(f"  请在 {app_dir() / 'config.json'} 填 \"email\"（用学校邮箱最好）。")
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
