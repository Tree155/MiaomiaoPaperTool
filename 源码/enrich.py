# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

检索结果的两个"附加信息"：
  1. 标题中译（X-MOL 那种，英文标题下面挂一行灰字中文）
  2. 期刊指标：影响因子 IF、中科院分区、JCR 分区 —— 来自 easyScholar 开放接口

两个都带本地磁盘缓存：
  · 标题翻译按 md5 存，同一篇文献只翻一次
  · 期刊指标按刊名存，半年过期（IF 一年一更新，半年足够）

设计取舍：
  · 宁可显示「—」也不猜。拿不到就是拿不到，绝不用"看起来像"的数字填空。
  · 两个服务都是免费第三方，会被限流；失败就静默放弃，不影响检索和下载主流程。
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

# HTTP 头只能是 latin-1，这里坚决不能放中文
UA = {"User-Agent": "Mozilla/5.0 (compatible; MiaomiaoTool/1.4.2)"}

ZH_CACHE_TTL = 90 * 86400      # 标题译文 90 天
JN_CACHE_TTL = 180 * 86400     # 期刊指标 180 天

# MyMemory 匿名 5000 字符/天，带 de=邮箱可到 50000 字符/天。
# 这里留出余量，单次会话最多翻这么多字符，超了就停下（下次靠缓存继续）。
ZH_CHAR_BUDGET = 40000

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 缓存
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
    """两个 JSON 缓存，按需落盘。"""

    def __init__(self, base: Path):
        self.zh_path = Path(base) / ".cache_zh.json"
        self.jn_path = Path(base) / ".cache_journal.json"
        self.zh = _load(self.zh_path)
        self.jn = _load(self.jn_path)

    # -- 标题 ------------------------------------------------------------- #
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

    # -- 摘要（用同一份 zh 缓存，key 加前缀区分）--------------------------- #
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

    # -- 期刊 ------------------------------------------------------------- #
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
    只读磁盘缓存，**一个请求都不发**：标题→中译、刊名→指标。
    给下载台账（_manifest.csv）用 —— 界面上看过的东西早就在缓存里了，
    补进去是零成本的；缓存里没有的就留空，绝不为写台账去联网。
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
# 标题中译
# --------------------------------------------------------------------------- #
_BAD_ZH = ("MYMEMORY WARNING", "QUERY LENGTH LIMIT", "INVALID LANGUAGE PAIR",
           "PLEASE SELECT TWO", "NO QUERY SPECIFIED")


def _zh_one(title: str, email: str, timeout: int) -> tuple[str | None, str]:
    """翻一条。返回 (译文, 失败原因)。"""
    try:
        params = {"q": title, "langpair": "en|zh-CN"}
        if email:
            params["de"] = email          # 带上邮箱能把日额度从 5k 提到 50k 字符
        r = requests.get(ZH_API, params=params, timeout=timeout,
                         headers=UA)
        if r.status_code != 200:
            return None, f"http{r.status_code}"
        d = r.json()
        if d.get("quotaFinished"):
            return None, "quota"
        txt = ((d.get("responseData") or {}).get("translatedText") or "").strip()
        txt = html.unescape(txt)          # MyMemory 会返回 &quot; &#39; 这类实体
        up = txt.upper()
        if not txt:
            return None, "empty"
        if any(b in up for b in _BAD_ZH):
            return None, "quota"
        # 翻译服务偶尔原样返回英文（说明没识别出来），不当作成功
        if txt == title:
            return None, "same"
        return txt, ""
    except Exception as e:
        return None, type(e).__name__


def translate_titles(titles: list[str], email: str = "", cache: Cache | None = None,
                     workers: int = 4) -> tuple[dict, str]:
    """
    批量中译。返回 ({英文标题: 中文标题}, 说明)。
    说明非空时表示中途停了（额度用尽 / 网络不通），已经拿到的照样返回。
    """
    out: dict[str, str] = {}
    todo: list[str] = []
    for t in dict.fromkeys(x.strip() for x in titles if x and x.strip()):
        if re.search(r"[\u4e00-\u9fff]", t):
            continue          # 本来就是中文，别再原样返回一遍（界面会重复显示两行一样的）
        if cache:
            hit = cache.get_zh(t)
            if hit:
                out[t] = hit
                continue
        todo.append(t)
    if not todo:
        return out, ""

    # 字符预算：超出部分这轮不翻，下次再补
    budget, picked = 0, []
    for t in todo:
        if budget + len(t) > ZH_CHAR_BUDGET:
            break
        picked.append(t)
        budget += len(t)
    note = "" if len(picked) == len(todo) else "本轮翻译额度已用完，剩余条目下次自动补齐"

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
        note = "翻译服务今日免费额度已用尽，只翻了前面这些"
    if cache:
        cache.flush()
    return out, note


# --------------------------------------------------------------------------- #
# 摘要中译（按需触发：用户点开某条的摘要才翻，不预先烧额度）
# --------------------------------------------------------------------------- #
ABS_CHUNK = 460          # MyMemory 单次 query 有长度上限，按句子切
_SENT = re.compile(r"(?<=[.!?;])\s+|(?<=。)\s*")


def _split_abs(text: str, limit: int = ABS_CHUNK) -> list[str]:
    out, buf = [], ""
    for sent in [s for s in _SENT.split(text) if s]:
        while len(sent) > limit:                 # 单句超长，硬切
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
    """摘要 → 中文。返回 (译文, 说明)。翻不动就原样返回英文，绝不编。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return "", "这条没有摘要"
    if not re.search(r"[A-Za-z]{3}", text):
        return text, ""                          # 本来就是中文
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
            parts.append(ch)                     # 翻不动就留英文
    out = " ".join(parts).strip()
    note = ""
    if quota:
        note = "今天翻译额度用完了，只翻了前半段，明天点一次会自动补齐"
    elif failed and failed == len(parts):
        note = "翻译服务没响应，先看英文吧"
    if out and not quota and not failed and cache:
        cache.put_abs(text, out)
        cache.flush()
    return out, note


# --------------------------------------------------------------------------- #
# 期刊指标（easyScholar）
# --------------------------------------------------------------------------- #
# easyScholar 返回的字段名（officialRank.all）在不同版本里叫法略有差异，都兜一下
_F_IF = ("SCIIF", "sciif", "影响因子")
_F_IF5 = ("SCIIF(5)", "sciif5", "5年影响因子")
_F_JCR = ("SCI", "sci", "JCR分区")
_F_CAS = ("SCI升级版", "sciUp", "中科院分区升级版", "中科院分区")
_F_CAS_BASE = ("SCI基础版", "sciBase", "中科院分区基础版")
_F_TOP = ("SCI升级版Top", "sciUpTop", "Top期刊")
_F_WARN = ("SCIWARN", "sciwarn", "中科院预警")


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
    # 分区值常见形态："1区"、"1区 Top"、"1 区"
    zone = ""
    m = re.search(r"([1-4])\s*区", cas)
    if m:
        zone = m.group(1) + "区"
    if not top and re.search(r"top", cas, re.I):
        top = "Top"
    if top and top.strip() in ("否", "无", "0", "false", "False", "-"):
        top = ""
    elif top:
        top = "Top"          # 不管原始值是"是"还是"1"，统一显示成 Top
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
    批量查期刊 IF / 分区。返回 ({刊名: 指标}, 说明)。
    没配 key 就直接返回空 —— 前端显示「—」。
    gap: 两次请求间隔。easyScholar 限 2 次/秒，这里 1.6 次/秒，留余量。
    """
    if not (key or "").strip():
        return {}, "未配置 easyScholar 密钥"

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
            note = ("easyScholar 密钥无效" if why == "key"
                    else "easyScholar 今日调用次数已用完")
            break
        # notfound / 网络错误：不缓存，下次还会重试
    if cache:
        cache.flush()
    return out, note


def test_key(key: str, name: str = "Food Chemistry", timeout: int = 15) -> dict:
    """保存密钥前先试一个常见刊，把结果和原始字段名一起返回，方便排查。"""
    if not (key or "").strip():
        return {"ok": False, "msg": "密钥是空的"}
    try:
        r = requests.get(ES_API, params={"secretKey": key.strip(), "publicationName": name},
                         timeout=timeout,
                         headers=UA)
        d = r.json()
        if d.get("code") != 200:
            return {"ok": False, "msg": f"{d.get('msg') or '查询失败'}（code {d.get('code')}）"}
        data = d.get("data") or {}
        all_ = ((data.get("officialRank") or {}).get("all")) or {}
        if not all_:
            return {"ok": False, "msg": f"密钥有效，但没查到「{name}」的等级信息"}
        raw = {k: all_[k] for k in list(all_.keys())[:14]}
        return {"ok": True, "sample": name, "fields": list(all_.keys()),
                "raw": raw, "parsed": _norm_rank(all_)}
    except Exception as e:
        return {"ok": False, "msg": f"连不上 easyScholar：{type(e).__name__}"}
