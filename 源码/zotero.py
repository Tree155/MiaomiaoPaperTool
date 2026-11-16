#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2 —— Zotero 联动

**只读、不写 Zotero 的数据库。** 两条读路径：
  1. Zotero 开着 → 走它自带的本地 API（http://127.0.0.1:23119/api/users/0/…）
     需要在 Zotero 里打开：设置 → 高级 → 勾「允许此电脑上的其他应用与 Zotero 通信」
  2. Zotero 没开 → 只读打开 zotero.sqlite（`mode=ro&immutable=1`，绝不写、不加锁）

**写**只有一种方式：生成 Zotero RDF 文件，由你在 Zotero 里「文件 → 导入」。
带 `<z:Collection>` 的东西，导入时会自动建成分类；附件用绝对路径挂上去。
不直接写 sqlite —— 那样 Zotero 下次打开会判定数据不一致，是毁库的做法。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape as _x

HOST = "http://127.0.0.1:23119"
API_BASE = HOST + "/api/users/0"
MAX_ITEMS = 5000
# 会带 DOI 的条目类型。附件 / 笔记 / 批注不用查（也占一半以上的条目数）
API_TYPES = ("journalArticle", "conferencePaper", "book", "bookSection",
             "report", "thesis", "preprint", "manuscript")


# --------------------------------------------------------------------------- #
# 定位数据目录
# --------------------------------------------------------------------------- #
def prefs_data_dir() -> Path | None:
    """从 Zotero 的 prefs.js 里读自定义数据目录（用户经常把库放在 D/F 盘）。"""
    base = Path(os.environ.get("APPDATA", "")) / "Zotero" / "Zotero" / "Profiles"
    if not base.is_dir():
        return None
    for p in base.glob("*/prefs.js"):
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r'extensions\.zotero\.dataDir",\s*"((?:[^"\\]|\\.)*)"', txt)
        if not m:
            continue
        raw = m.group(1).replace("\\\\", "\\")
        d = Path(raw)
        if (d / "zotero.sqlite").is_file():
            return d
    return None


_DD: dict = {"v": "unset"}

# 宿主（paperfetch_ui）启动时把自己的 CFG 注入进来。
# ⚠ 不能在这里直接调 pf.load_config()：不带参数读的是 app_dir()/config.json，
# 而界面可能用的是 --config 指定的另一个文件 —— 那样会读错配置，
# 更糟的是 app_dir 没 config 时 load_config 会**凭空生成一份默认配置**。
_CFG: dict = {}


def set_config(cfg: dict) -> None:
    """把当前生效的配置交给本模块（换配置后调一次，并清掉缓存的目录）。"""
    global _CFG
    _CFG = dict(cfg or {})
    _DD["v"] = "unset"


def _zotero_sqlite(p: Path) -> Path | None:
    """
    这个目录（或它的 Zotero 子目录）里的 zotero.sqlite，两个都有时**挑新的那个**。
    用户常把新旧两份数据目录放在同一层（旧的 18 MB、新的 31 MB），
    光"找得到"不够，得看哪个还在被写。
    """
    if not p:
        return None
    cands = []
    for c in (p / "zotero.sqlite", p / "Zotero" / "zotero.sqlite"):
        try:
            if c.is_file():
                cands.append(c)
        except OSError:
            pass
    if not cands:
        return None
    return max(cands, key=lambda x: x.stat().st_mtime)


def set_data_dir(path: str | Path) -> Path | None:
    """用户在界面上手动指定的数据目录（写进 config.json 的 zotero_data_dir）。"""
    hit = _zotero_sqlite(Path(path))
    if hit:
        _DD["v"] = hit.parent
        return hit.parent
    return None


def data_dir() -> Path | None:
    """
    找 Zotero 数据目录，按优先级：
      1. 用户在界面上指定过的（config.json 的 zotero_data_dir）
      2. Zotero 自己的 prefs.js（extensions.zotero.dataDir）
      3. 几个常见默认位置
    每组都可能有"父目录 / Zotero 子目录"两份，按 zotero.sqlite 的修改时间取新的。
    """
    if _DD["v"] != "unset":
        return _DD["v"]

    tries: list[Path] = []
    custom = (_CFG.get("zotero_data_dir") or "").strip()
    if custom:
        tries.append(Path(custom))
    p = prefs_data_dir()
    if p:
        tries.append(p)
    tries += [Path.home() / "Zotero", Path("D:/Zotero"), Path("E:/Zotero"),
              Path("F:/Zotero"), Path("G:/Zotero"), Path("C:/Zotero")]

    best = None
    for t in tries:
        sql = _zotero_sqlite(t)
        if not sql:
            continue
        if best is None or sql.stat().st_mtime > best[1]:
            best = (sql.parent, sql.stat().st_mtime)
        # 用户指定的目录优先，找到就用
        if t is tries[0]:
            break
    _DD["v"] = best[0] if best else None
    return _DD["v"]


# --------------------------------------------------------------------------- #
# 读：本地 API（优先）
# --------------------------------------------------------------------------- #
def _api(path: str, timeout: float = 2.0):
    """访问 Zotero 本地 API。绕开系统代理 —— 127.0.0.1 走代理必然失败。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(API_BASE + path, headers={"Accept": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), dict(r.headers)


def api_alive(timeout: float = 1.2) -> bool:
    try:
        _api("/collections?limit=1", timeout)
        return True
    except Exception:
        return False


def _from_api() -> dict:
    cols, _ = _api("/collections?limit=200")
    ncol = {c["key"]: c["data"] for c in cols}
    names = {k: (v.get("name") or "") for k, v in ncol.items()}
    tree = [{"key": k, "name": v.get("name") or "",
             "parent": names.get(v.get("parentCollection") or "", "")}
            for k, v in ncol.items()]

    # 只查期刊论文会漏掉会议论文 / 报告 / 学位论文上的 DOI —— 那些漏了就会被重复导入。
    # 逐类型查，每个类型分页，总量有上限，别把时间花在 4000 条附件上。
    dois: set[str] = set()
    scanned = 0
    for t in API_TYPES:
        start, total = 0, None
        while start < MAX_ITEMS:
            try:
                page, hdr = _api(f"/items?limit=100&start={start}&itemType={t}")
            except Exception:
                break
            if total is None:
                total = int(hdr.get("Total-Results") or len(page) or 0)
            if not page:
                break
            scanned += len(page)
            for it in page:
                d = it.get("data") or {}
                doi = (d.get("DOI") or "").strip().lower()
                if doi:
                    dois.add(doi)
            start += len(page)
            if start >= (total or 0):
                break
    return {"source": "api", "collections": tree, "dois": dois, "n_items": scanned}


# --------------------------------------------------------------------------- #
# 读：只读 sqlite（退路）
# --------------------------------------------------------------------------- #
def _from_sqlite(d: Path, immutable: bool = True) -> dict:
    p = d / "zotero.sqlite"
    # immutable=1 在 Zotero 关着时最稳；Zotero 开着时数据库是独占锁，
    # 这时不管加不加 immutable 都不该去读（读到的也可能是没合并 WAL 的旧快照）。
    qs = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    db = sqlite3.connect(f"file:{p}{qs}", uri=True, timeout=1.5)
    try:
        c = db.cursor()
        cols = c.execute(
            "SELECT collectionID, collectionName, parentCollectionID FROM collections").fetchall()
        byid = {r[0]: r[1] for r in cols}
        tree = [{"key": str(r[0]), "name": r[1] or "",
                 "parent": byid.get(r[2], "") if r[2] else ""} for r in cols]
        dois = {r[0].strip().lower() for r in c.execute(
            """SELECT v.value FROM items i
               JOIN itemData d   ON d.itemID = i.itemID
               JOIN fields f     ON f.fieldID = d.fieldID
               JOIN itemDataValues v ON v.valueID = d.valueID
               WHERE f.fieldName = 'DOI'""") if r[0]}
        n = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return {"source": "sqlite", "collections": tree, "dois": dois, "n_items": n}
    finally:
        db.close()


def probe() -> dict:
    """
    探测 Zotero：先本地 API，再只读 sqlite。
    ⚠ Zotero 运行时数据库是独占锁，没开本地 API 就两边都读不了 —— 这时提示用户去开那个开关。
    """
    d = data_dir()
    alive = api_alive()
    why = ""
    if alive:
        try:
            out = _from_api()
            out.update({"ok": True, "data_dir": str(d or ""), "running": True})
            return out
        except Exception as e:
            why = f"本地 API 通了但读取失败：{type(e).__name__}"

    if d:
        try:
            out = _from_sqlite(d, immutable=True)
            out.update({"ok": True, "data_dir": str(d), "running": bool(alive)})
            return out
        except Exception as e:
            if alive or _zotero_running():
                return {"ok": False, "running": True, "data_dir": str(d),
                        "why": why or "Zotero 正在运行，但没开「允许其他应用与 Zotero 通信」，"
                                      "直接读数据库又被它锁住了。"
                                      "去 Zotero 设置 → 高级 → 勾上那一项，或者先关掉 Zotero。"}
            return {"ok": False, "running": False, "data_dir": str(d),
                    "why": why or f"读不了 zotero.sqlite：{type(e).__name__}"}

    return {"ok": False, "running": False, "data_dir": "",
            "why": why or "没找到 Zotero 数据目录（zotero.sqlite）"}


def _zotero_running() -> bool:
    """Zotero 进程在不在（只用来把错误提示说准）。"""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                             capture_output=True, text=True, timeout=5,
                             encoding="mbcs", errors="ignore")
        return "zotero.exe" in (out.stdout or "").lower()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 写：生成 Zotero RDF
# --------------------------------------------------------------------------- #
def _person(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if "," in name:
        surname, given = [x.strip() for x in name.split(",", 1)]
    else:
        parts = name.split()
        surname, given = (parts[-1], " ".join(parts[:-1])) if parts else (name, "")
    return (f"<rdf:li><foaf:Person><foaf:surname>{_x(surname)}</foaf:surname>"
            f"<foaf:givenName>{_x(given)}</foaf:givenName></foaf:Person></rdf:li>")


def rdf_xml(records: list[dict], group: str = "journal", root_name: str = "") -> str:
    """
    生成 Zotero RDF。`group` 决定分几类：
      journal → 按期刊名    year → 按年份    single → 只建一个分类（名字用 root_name）
    """
    buckets: dict[str, list[dict]] = {}
    for r in records:
        if group == "year":
            key = str(r.get("year") or "年份不详")
        elif group == "single":
            key = root_name or "文献妙妙小工具"
        else:
            key = (r.get("journal") or "").strip() or "期刊未注明"
        buckets.setdefault(key, []).append(r)

    head = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rdf:RDF\n'
        ' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        ' xmlns:z="http://www.zotero.org/namespaces/export#"\n'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        ' xmlns:dcterms="http://purl.org/dc/terms/"\n'
        ' xmlns:bib="http://purl.org/net/biblio#"\n'
        ' xmlns:foaf="http://xmlns.com/foaf/0.1/"\n'
        ' xmlns:prism="http://prismstandard.org/namespaces/1.2/basic/">\n')
    parts = [head]
    n = 0
    for gi, (gname, items) in enumerate(sorted(buckets.items()), 1):
        parts.append('  <z:Collection rdf:about="#collection_%d">\n' % gi)
        parts.append('    <dc:title>%s</dc:title>\n' % _x(gname))
        parts.append('    <dcterms:hasPart>\n')
        for r in items:
            n += 1
            iid = "item_%d" % n
            parts.append('      <bib:Article rdf:about="#%s">\n' % iid)
            parts.append('        <z:itemType>journalArticle</z:itemType>\n')
            if r.get("title"):
                parts.append('        <dc:title>%s</dc:title>\n' % _x(r["title"]))
            au = _person(r.get("first_author") or "")
            if au:
                parts.append('        <bib:authors><rdf:Seq>%s</rdf:Seq></bib:authors>\n' % au)
            if r.get("year"):
                parts.append('        <dc:date>%s</dc:date>\n' % _x(str(r["year"])))
            if r.get("journal"):
                parts.append('        <prism:publicationName>%s</prism:publicationName>\n'
                             % _x(r["journal"]))
            if r.get("doi"):
                parts.append('        <dc:identifier>DOI: %s</dc:identifier>\n' % _x(r["doi"]))
                parts.append('        <dc:identifier>https://doi.org/%s</dc:identifier>\n' % _x(r["doi"]))
            if r.get("path"):
                p = r["path"]
                low = p.lower()
                mime = ("application/pdf" if low.endswith(".pdf")
                        else "text/xml" if low.endswith(".xml") else "application/gzip")
                parts.append('        <z:attachments>\n'
                             '          <z:Attachment rdf:about="#%s-att">\n'
                             '            <z:itemType>attachment</z:itemType>\n'
                             '            <z:path>%s</z:path>\n'
                             '            <dc:title>%s</dc:title>\n'
                             '            <z:mimeType>%s</z:mimeType>\n'
                             '          </z:Attachment>\n'
                             '        </z:attachments>\n'
                             % (iid, _x(p), _x(Path(p).name), mime))
            parts.append('      </bib:Article>\n')
        parts.append('    </dcterms:hasPart>\n')
        parts.append('  </z:Collection>\n')
    parts.append('</rdf:RDF>\n')
    return "".join(parts)


def write_rdf(records: list[dict], path: Path, group: str = "journal",
              root_name: str = "") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rdf_xml(records, group, root_name), encoding="utf-8")
    return len(records)


# --------------------------------------------------------------------------- #
# 写：直接写进 Zotero（新建分类 + 新增条目）
#
# Zotero 本地 API 的写入协议（2026-11-04 实测，Zotero 10.0.7）：
#   1. POST /api/local/authorize  {"appName": "...", "remember": true}
#      带 Zotero-Server-ID 头 → 返回 {"key": "..."}（32 位）
#   2. 之后所有写请求带 Zotero-API-Key: <key>，**body 必须是 JSON 数组**
#      返回 {"successful": {"0": {key, version, ...}}, "failed": {...}}
#   3. DELETE 还要求 If-Unmodified-Since-Version: <version>
# 只做三件事：建分类、建条目、撤销（删掉本次建的）。绝不修改已有条目。
# --------------------------------------------------------------------------- #
APP_NAME = "文献妙妙小工具"


class Writer:
    """直写 Zotero。key 缓存在程序目录的 .zotero.json，401 时会重新申请一次。"""

    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.sid: str | None = None
        self.key: str | None = None
        self._load()

    # -- 基础 --------------------------------------------------------------- #
    @property
    def key_file(self) -> Path:
        return self.base / ".zotero.json"

    def _load(self) -> None:
        try:
            d = json.loads(self.key_file.read_text(encoding="utf-8"))
            self.key = d.get("key") or None
            self.sid = d.get("sid") or None
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.key_file.write_text(json.dumps({"key": self.key, "sid": self.sid},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def server_id(self) -> str | None:
        if self.sid:
            return self.sid
        d = data_dir()
        if not d:
            return None
        try:
            db = sqlite3.connect(f"file:{d / 'zotero.sqlite'}?mode=ro&immutable=1",
                                 uri=True, timeout=1.5)
            row = db.execute("select value from settings "
                             "where setting='localAPI' and key='serverID'").fetchone()
            db.close()
            self.sid = row[0] if row else None
            self._save()
        except Exception:
            self.sid = None
        return self.sid

    def _req(self, path: str, method: str = "GET", body=None, key: str | None = None,
             extra: dict | None = None, timeout: float = 30.0, api_root: bool = False):
        import urllib.error
        import urllib.request

        hdr = {"Content-Type": "application/json"}
        sid = self.server_id()
        if sid:
            hdr["Zotero-Server-ID"] = sid
        if key:
            hdr["Zotero-API-Key"] = key
        if extra:
            hdr.update(extra)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        url = (HOST + path) if api_root else (API_BASE + path)
        req = urllib.request.Request(url, method=method, data=data, headers=hdr)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                raw = r.read().strip()
                if not raw:
                    return r.status, None
                return r.status, (json.loads(raw) if raw[:1] in b"[{" else raw[:200])
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300]
        except Exception as e:                      # 连接被拒等
            return None, f"{type(e).__name__}: {e}".encode()

    def authorize(self) -> tuple[bool, str]:
        if not api_alive():
            return False, "Zotero 没在运行（本地 API 连不上）"
        if not self.server_id():
            return False, "读不到 Zotero 的 serverID，先在设置里打开「允许其他应用与 Zotero 通信」"
        st, out = self._req("/api/local/authorize", "POST",
                            {"appName": APP_NAME, "remember": True}, api_root=True)
        if st == 200 and isinstance(out, dict) and out.get("key"):
            self.key = out["key"]
            self._save()
            return True, ""
        return False, f"申请写入授权失败：{st} {out}"

    def ensure_key(self) -> tuple[bool, str]:
        if self.key:
            return True, ""
        return self.authorize()

    # -- 写 ------------------------------------------------------------------ #
    def create_collection(self, name: str, parent: str | None = None) -> tuple[str | None, str]:
        ok, why = self.ensure_key()
        if not ok:
            return None, why
        obj = {"name": name.strip() or "小工具导入"}
        if parent:
            obj["parentCollection"] = parent
        st, out = self._req("/collections", "POST", [obj], self.key)
        if st == 401:
            self.key = None
            ok, why = self.authorize()
            if ok:
                st, out = self._req("/collections", "POST", [obj], self.key)
        if st == 200 and isinstance(out, dict):
            succ = out.get("successful") or {}
            if succ:
                rec = list(succ.values())[0]
                return rec.get("key"), ""
        return None, f"建分类失败：{st} {out}"

    def _item_obj(self, r: dict, ckey: str) -> dict:
        obj = {"itemType": "journalArticle", "collections": [ckey]}
        if r.get("title"):
            obj["title"] = str(r["title"])
        if r.get("doi"):
            obj["DOI"] = str(r["doi"])
            obj["url"] = f"https://doi.org/{r['doi']}"
        if r.get("journal"):
            obj["publicationTitle"] = str(r["journal"])
        if r.get("year"):
            obj["date"] = str(r["year"])
        au = (r.get("first_author") or "").strip()
        if au:
            if "," in au:
                last, first = [x.strip() for x in au.split(",", 1)]
            else:
                parts = au.split()
                last, first = (parts[-1], " ".join(parts[:-1])) if parts else (au, "")
            obj["creators"] = [{"creatorType": "author", "lastName": last, "firstName": first}]
        return obj

    def create_items(self, records: list[dict], ckey: str,
                     batch: int = 40) -> tuple[list[dict], list[str]]:
        """返回 (成功列表[{key,version,title}], 失败说明列表)。"""
        done: list[dict] = []
        fails: list[str] = []
        for i in range(0, len(records), batch):
            chunk = records[i:i + batch]
            payload = [self._item_obj(r, ckey) for r in chunk]
            st, out = self._req("/items", "POST", payload, self.key)
            if st == 401:
                self.key = None
                ok, why = self.authorize()
                if not ok:
                    fails.append(why)
                    break
                st, out = self._req("/items", "POST", payload, self.key)
            if st != 200 or not isinstance(out, dict):
                fails.append(f"第 {i + 1}–{i + len(chunk)} 条写入失败：{st} {out}")
                continue
            succ = out.get("successful") or {}
            for idx_s, rec in succ.items():
                try:
                    src = chunk[int(idx_s)]
                except (ValueError, IndexError):
                    src = {}
                done.append({"key": rec.get("key"), "version": rec.get("version"),
                             "title": src.get("title") or ""})
            for idx_s, err in (out.get("failed") or {}).items():
                try:
                    t = chunk[int(idx_s)].get("title") or ""
                except (ValueError, IndexError):
                    t = ""
                fails.append(f"{t[:40]}：{err}")
        return done, fails

    # -- 撤销 ---------------------------------------------------------------- #
    def delete_items(self, items: list[dict]) -> int:
        n = 0
        for it in items:
            k, v = it.get("key"), it.get("version")
            if not k:
                continue
            extra = {"If-Unmodified-Since-Version": str(v)} if v is not None else None
            st, _ = self._req(f"/items/{k}", "DELETE", key=self.key, extra=extra)
            if st in (200, 204):
                n += 1
        return n

    def delete_collection(self, key: str, version=None) -> bool:
        extra = {"If-Unmodified-Since-Version": str(version)} if version is not None else None
        st, _ = self._req(f"/collections/{key}", "DELETE", key=self.key, extra=extra)
        return st in (200, 204)

    def collection_version(self, key: str):
        st, out = self._req(f"/collections/{key}")
        if st == 200 and isinstance(out, dict):
            return out.get("version")
        return None
