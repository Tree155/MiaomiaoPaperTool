#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2 — Zotero integration

**Read-only; never writes Zotero's database.** Two read paths:
  1. Zotero is running → use its built-in local API (http://127.0.0.1:23119/api/users/0/…)
     Requires enabling in Zotero: Settings → Advanced → tick "Allow other applications on this computer to communicate with Zotero"
  2. Zotero is not running → open zotero.sqlite read-only (`mode=ro&immutable=1`, never writing, never locking)

**Writing** happens one way only: generate a Zotero RDF file and use "File → Import" in Zotero yourself.
Anything carrying `<z:Collection>` becomes a collection on import; attachments are linked by absolute path.
Never write sqlite directly — that makes Zotero decide on its next launch that the data is inconsistent, which is how you destroy a library.
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
# Item types that can carry a DOI. Attachments / notes / annotations need no lookup (and are over half the items)
API_TYPES = ("journalArticle", "conferencePaper", "book", "bookSection",
             "report", "thesis", "preprint", "manuscript")


# --------------------------------------------------------------------------- #
# Locate the data directory
# --------------------------------------------------------------------------- #
def prefs_data_dir() -> Path | None:
    """Read the custom data directory from Zotero's prefs.js (users often keep the library on drive D or F)."""
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

# The host (paperfetch_ui) injects its own CFG at startup.
# ! Don't call pf.load_config() here: with no argument it reads app_dir()/config.json,
# while the UI may be using another file given by --config -- that would read the wrong config,
# worse, when app_dir has no config load_config **generates a default one out of thin air**.
_CFG: dict = {}


def set_config(cfg: dict) -> None:
    """Hand the currently effective config to this module (call it once after switching configs; it also clears the cached directory)."""
    global _CFG
    _CFG = dict(cfg or {})
    _DD["v"] = "unset"


def _zotero_sqlite(p: Path) -> Path | None:
    """
    Of the zotero.sqlite files in this directory (or its Zotero subdirectory), **pick the newer one** when both exist.
    Users often keep an old and a new data directory side by side (the old one 18 MB, the new one 31 MB),
    so merely "finding" one is not enough — check which one is still being written.
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
    """The data directory the user picked manually in the UI (written to zotero_data_dir in config.json)"""
    hit = _zotero_sqlite(Path(path))
    if hit:
        _DD["v"] = hit.parent
        return hit.parent
    return None


def data_dir() -> Path | None:
    """
    Locate the Zotero data directory, in priority order:
      1. what the user picked in the UI (zotero_data_dir in config.json)
      2. Zotero's own prefs.js (extensions.zotero.dataDir)
      3. a few common default locations
    Each candidate may come as a "parent directory / Zotero subdirectory" pair; the one with the newer zotero.sqlite mtime wins.
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
        # The user-specified directory wins; use it when found
        if t is tries[0]:
            break
    _DD["v"] = best[0] if best else None
    return _DD["v"]


# --------------------------------------------------------------------------- #
# Read: local API (preferred)
# --------------------------------------------------------------------------- #
def _api(path: str, timeout: float = 2.0):
    """Access the Zotero local API. Bypass the system proxy — 127.0.0.1 through a proxy always fails."""
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

    # Querying only journal articles misses DOIs on conference papers / reports / dissertations -- those get imported twice.
    # Query per type, paging within each, with an overall cap, don't spend time on 4000 attachments.
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
# Read: sqlite read-only (fallback)
# --------------------------------------------------------------------------- #
def _from_sqlite(d: Path, immutable: bool = True) -> dict:
    p = d / "zotero.sqlite"
    # immutable=1 is safest while Zotero is closed; while Zotero is open the DB is locked exclusively,
    # and then we shouldn't read whether or not immutable is set (we might get a stale snapshot without the WAL merged).
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
    Probe Zotero: local API first, then read-only sqlite.
    ⚠ While Zotero is running the database is exclusively locked, so without the local API neither path can be read — in that case tell the user to flip that switch.
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
            why = f"Local API reachable but read failed:{type(e).__name__}"

    if d:
        try:
            out = _from_sqlite(d, immutable=True)
            out.update({"ok": True, "data_dir": str(d), "running": bool(alive)})
            return out
        except Exception as e:
            if alive or _zotero_running():
                return {"ok": False, "running": True, "data_dir": str(d),
                        "why": why or "Zotero is running but \"Allow other applications to communicate with Zotero\" is not enabled,"
                                      "and reading the database directly is blocked by its lock."
                                      "Go to Zotero Settings → Advanced and tick that option, or quit Zotero first."}
            return {"ok": False, "running": False, "data_dir": str(d),
                    "why": why or f"Cannot read zotero.sqlite:{type(e).__name__}"}

    return {"ok": False, "running": False, "data_dir": "",
            "why": why or "No Zotero data directory found (zotero.sqlite)"}


def _zotero_running() -> bool:
    """Whether the Zotero process is running (used only to make error messages accurate)."""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                             capture_output=True, text=True, timeout=5,
                             encoding="mbcs", errors="ignore")
        return "zotero.exe" in (out.stdout or "").lower()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Write: generate Zotero RDF
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
    Generate Zotero RDF. `group` decides how the collections are split:
      journal → by journal name    year → by year    single → a single collection (named root_name)
    """
    buckets: dict[str, list[dict]] = {}
    for r in records:
        if group == "year":
            key = str(r.get("year") or "Year unknown")
        elif group == "single":
            key = root_name or "Miaomiao Paper Tool"
        else:
            key = (r.get("journal") or "").strip() or "Journal not specified"
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
# Write: write straight into Zotero (new collection + new items)
#
# Zotero local API write protocol (measured 2026-11-04, Zotero 10.0.7):
#   1. POST /api/local/authorize  {"appName": "...", "remember": true}
#      with a Zotero-Server-ID header -> returns {"key": "..."} (32 chars)
#   2. every later write carries Zotero-API-Key: <key>, **body must be a JSON array**
#      returns {"successful": {"0": {key, version, ...}}, "failed": {...}}
#   3. DELETE also requires If-Unmodified-Since-Version: <version>
# Only three things: create collection, create items, undo (delete what this run created). Never modify existing items.
# --------------------------------------------------------------------------- #
APP_NAME = "Miaomiao Paper Tool"


class Writer:
    """Write to Zotero directly. The key is cached in .zotero.json in the program directory and re-requested once on 401."""

    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.sid: str | None = None
        self.key: str | None = None
        self._load()

    # -- Basics ------------------------------------------------------------- #
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
        except Exception as e: # Connection refused etc.
            return None, f"{type(e).__name__}: {e}".encode()

    def authorize(self) -> tuple[bool, str]:
        if not api_alive():
            return False, "Zotero is not running (the local API is unreachable)"
        if not self.server_id():
            return False, "Cannot read Zotero's serverID; enable \"Allow other applications to communicate with Zotero\" in the settings first"
        st, out = self._req("/api/local/authorize", "POST",
                            {"appName": APP_NAME, "remember": True}, api_root=True)
        if st == 200 and isinstance(out, dict) and out.get("key"):
            self.key = out["key"]
            self._save()
            return True, ""
        return False, f"Failed to request write authorization:{st} {out}"

    def ensure_key(self) -> tuple[bool, str]:
        if self.key:
            return True, ""
        return self.authorize()

    # -- Write -------------------------------------------------------------- #
    def create_collection(self, name: str, parent: str | None = None) -> tuple[str | None, str]:
        ok, why = self.ensure_key()
        if not ok:
            return None, why
        obj = {"name": name.strip() or "Miaomiao Import"}
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
        return None, f"Failed to create collection:{st} {out}"

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
        """Returns (success list [{key,version,title}], list of failure messages)."""
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
                fails.append(f"Item {i + 1}–{i + len(chunk)} failed to write:{st} {out}")
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
                fails.append(f"{t[:40]}: {err}")
        return done, fails

    # -- Undo --------------------------------------------------------------- #
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
