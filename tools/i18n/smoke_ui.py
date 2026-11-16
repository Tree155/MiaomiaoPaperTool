#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_ui.py -- 英文版界面自检。

    python tools/i18n/smoke_ui.py                     # 用 源码/en/ 直接跑（复制到临时目录，不污染仓库）
    python tools/i18n/smoke_ui.py --exe <exe 路径>     # 测打包好的 exe
    python tools/i18n/smoke_ui.py --json out.json     # 同时把明细写成 JSON

检什么
------
1. **文本截断**：元素 scrollWidth 比 clientWidth 宽（英文比中文长，按钮/表头/标签最容易挤爆）
2. **溢出视口**：元素右边界超出窗口（弹层、chip、长路径）
3. **中文残留**：静态文案元素里还有中文字符（用户数据区除外，比如 Zotero 里的中文分类名）
4. **JS 报错**：页面 error 事件

为什么要用 pywebview 而不是 Chrome headless
----------------------------------------
Chrome 157 起 `--headless --screenshot` 在本机静默退出（exit 0/2、无 stderr、不出图），
Edge 同样。pywebview 用的是 WebView2 —— 与打包 exe 同一个内核，`evaluate_js` 还能直接量布局，
比肉眼看截图更准，且 `hidden=True` 不弹窗。

需要 pywebview（requirements.txt 里已有）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EN_DIR = os.path.join(ROOT, "源码", "en")

CJK = re.compile(r"[\u4e00-\u9fff]")

# 静态文案元素：这些位置的文字必须是英文。用户数据区（Zotero 分类名等）不在此列。
TEXT_SELECTOR = ("button, label, th, option, summary, .tab, .ap-title, .hint, .libstat, "
                 ".exmsg, .errtip, .empty, .bd, .stat, .abstip")
LAYOUT_SELECTOR = ("button, th, td, span, label, option, select, b, code, "
                   "div.exchip, div.bd, div.ap-title, div.empty, .tab")

PROBE_JS = r"""
(function(textSel, layoutSel){
  var W = window.innerWidth;
  var cjk = [], trunc = [], over = [];
  var hasCJK = function(s){ return /[\u4e00-\u9fff]/.test(s); };
  var skipBox = function(el){
    // Zotero / 整理下载弹层里会显示用户自己的中文分类名与路径，那不算残留
    return !!el.closest('#zotPop, #orgPop, #acctPop');
  };
  document.querySelectorAll(textSel).forEach(function(el){
    if (!el.getClientRects().length) return;
    var t = (el.textContent || '').trim().replace(/\s+/g, ' ');
    if (t && hasCJK(t) && !skipBox(el)) cjk.push([el.tagName, el.id, t.slice(0, 60)]);
  });
  document.querySelectorAll(layoutSel).forEach(function(el){
    var st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || !el.getClientRects().length) return;
    var cls = (el.className || '').toString();
    if (cls.indexOf('expwrap') >= 0 || cls.indexOf('exppop') >= 0) return;   // 弹层外壳，内部是绝对定位
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    var t = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0)
      trunc.push([el.tagName, el.id, cls.slice(0, 30), t, el.scrollWidth, el.clientWidth]);
    if (r.right > W + 1) over.push([el.tagName, el.id, cls.slice(0, 30), t, Math.round(r.right)]);
  });
  return JSON.stringify({vw: W, cjk: cjk.slice(0, 30), trunc: trunc.slice(0, 30),
                         over: over.slice(0, 30), n: {cjk: cjk.length, trunc: trunc.length, over: over.length}});
})(%s, %s)
""" % (json.dumps(TEXT_SELECTOR), json.dumps(LAYOUT_SELECTOR))

# 用真实检索结果驱动界面（--fixture 才会执行）。数据直接内嵌，不依赖额外端点。
INJECT_JS = r"""
(function(rows){
  render(rows);
  rows.slice(0, 2).forEach(function(r){ sel.add(r.i); });
  document.querySelectorAll('#tblBox tbody input[data-i]').forEach(function(el){
    if (sel.has(Number(el.dataset.i))) el.checked = true;
  });
  if (typeof updBtn === 'function') updBtn();
  return rows.length;
})
"""

SCENES = [
    ("main", None),
    ("export-pop", "renderExPop(); document.querySelector('#exPop').hidden = false;"),
    ("cite-pop", "renderCitePop(); document.querySelector('#citePop').hidden = false;"),
    ("acct-pop", "togglePop('acct');"),
    ("storage-pop", "togglePop('stg');"),
    ("metrics-pop", "togglePop('jm');"),
    ("about-pop", "togglePop('ver');"),
    ("syntax-help", "document.querySelector('#btnHelp').click();"),
    ("library-tab", "document.querySelector('[data-pane=lib]').click();"),
    ("zotero-pop", "setTimeout(function(){ renderZotPop(); document.querySelector('#zotPop').hidden = false; }, 1200);"),
    ("organize-pop", "renderOrgPop(); document.querySelector('#orgPop').hidden = false;"),
    ("find-tab", "document.querySelector('[data-pane=find]').click();"),
    ("notes-pop", "setTimeout(function(){ renderNotesPop(); document.querySelector('#notesPop').hidden = false; }, 1200);"),
    ("abstract-open", "var s = document.querySelector('#tblBox details summary'); if (s) s.click();"),
]

ERROR_HOOK = """
(function(){
  window.__errs = [];
  window.addEventListener('error', function(e){
    window.__errs.push((e.message || '') + ' @' + (e.filename || '') + ':' + (e.lineno || ''));
  });
  window.addEventListener('unhandledrejection', function(e){
    window.__errs.push('unhandled: ' + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
  });
  return 'hooked';
})()
"""


# --------------------------------------------------------------------------- 服务器
def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(workdir: str, port: int, fixture: str | None):
    # 不要 chdir：pywebview 在 import 时按 os.getcwd() 推算 base_uri，
    # cwd 指到临时目录会让它去找 <tmp>/tools/i18n 而报错。
    # paperfetch 的 app_dir() 基于 __file__，与 cwd 无关。
    sys.path.insert(0, workdir)
    import paperfetch as pf
    import paperfetch_ui as ui

    ui.CFG = pf.load_config()
    try:
        ui.zo.set_config(ui.CFG)
    except Exception:
        pass
    ui.SESS = pf.PoliteSession(ui.CFG)

    srv = ThreadingHTTPServer(("127.0.0.1", port), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# --------------------------------------------------------------------------- 主流程
def run_probe(url: str, fixture: str | None, settle: float):
    import webview

    out = {}

    def job(w):
        time.sleep(settle)
        w.evaluate_js(ERROR_HOOK)
        if fixture:
            try:
                out["inject"] = w.evaluate_js("(" + INJECT_JS.strip() + ")(" + fixture + ")")
            except Exception as e:
                out["inject"] = "ERR %s" % e
            time.sleep(2.0)

        scenes = []
        for name, js in SCENES:
            try:
                if js:
                    w.evaluate_js(js)
                    time.sleep(1.8 if "pop" in name else 0.7)
                raw = w.evaluate_js(PROBE_JS)
                r = json.loads(raw) if raw else {}
                r["name"] = name
            except Exception as e:
                r = {"name": name, "error": "%s: %s" % (type(e).__name__, e)}
            scenes.append(r)
            n = r.get("n", {})
            print("  %-14s cjk=%d trunc=%d over=%d %s" % (
                name, n.get("cjk", -1), n.get("trunc", -1), n.get("over", -1),
                r.get("error", "")))
        out["scenes"] = scenes
        try:
            out["js_errors"] = json.loads(w.evaluate_js("JSON.stringify(window.__errs || [])"))
        except Exception:
            out["js_errors"] = []
        try:
            w.destroy()
        except Exception:
            pass

    win = webview.create_window("smoke", url, width=1480, height=1040, hidden=True)
    webview.start(job, win)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="测打包好的 exe（不给就直接跑 源码/en/）")
    ap.add_argument("--fixture", help="检索结果 JSON，用来把真实数据灌进结果表")
    ap.add_argument("--json", help="把明细写到此文件")
    ap.add_argument("--settle", type=float, default=5.0, help="页面初始化等待秒数")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="miaomiao-smoke-")
    proc = None
    srv = None
    fixture_text = None
    if args.fixture:
        fixture_text = io.open(args.fixture, encoding="utf-8").read()

    try:
        if args.exe:
            exe = os.path.abspath(args.exe)
            if not os.path.isfile(exe):
                print("找不到 exe：%s" % exe)
                return 2
            tgt = os.path.join(tmp, os.path.basename(exe))
            shutil.copy2(exe, tgt)
            log = os.path.join(tmp, "startup.log")
            proc = subprocess.Popen([tgt], cwd=tmp)
            port = None
            for _ in range(300):
                time.sleep(0.5)
                if os.path.exists(log):
                    m = re.search(r"server on (\d+)",
                                  io.open(log, encoding="utf-8", errors="replace").read())
                    if m:
                        port = int(m.group(1))
                        break
            if not port:
                print("exe 没能起来，startup.log：")
                print(io.open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else "(无)")
                return 2
            mode = "exe"
        else:
            src = EN_DIR
            if not os.path.isdir(src):
                print("找不到 %s" % src)
                return 2
            for fn in os.listdir(src):
                if fn.endswith(".py") or fn == "ui.html":
                    shutil.copy2(os.path.join(src, fn), os.path.join(tmp, fn))
            port = free_port()
            srv = start_server(tmp, port, fixture_text)
            mode = "source"

        url = "http://127.0.0.1:%d/" % port
        print("模式 %s，端口 %d\n" % (mode, port))

        print("=== 接口返回语言 ===")
        api_bad = 0
        for ep in ("/api/config", "/api/library", "/api/kstatus", "/api/quota", "/api/organize/dir"):
            try:
                with urllib.request.urlopen(url + ep.lstrip("/"), timeout=60) as r:
                    txt = r.read().decode("utf-8", "replace")
                n = len(CJK.findall(txt))
                api_bad += n
                print("  %-20s CJK=%d" % (ep, n))
            except Exception as e:
                print("  %-20s ERR %s" % (ep, e))
                api_bad += 1

        print("\n=== 界面 ===")
        res = run_probe(url, fixture_text, args.settle)
        res["mode"] = mode
        res["api_cjk"] = api_bad

        scenes = res.get("scenes", [])
        fail = [s for s in scenes if s.get("n", {}).get("cjk", 0) or s.get("n", {}).get("trunc", 0)
                or s.get("n", {}).get("over", 0) or s.get("error")]
        js_err = res.get("js_errors") or []

        print("\n=== 汇总 ===")
        print("  场景 %d，问题场景 %d，接口中文 %d，JS 报错 %d"
              % (len(scenes), len(fail), api_bad, len(js_err)))
        for s in fail:
            print("  ! %s: %s" % (s["name"], s.get("error") or s.get("n")))
            for row in (s.get("cjk") or [])[:5]:
                print("      中文 %s#%s %r" % (row[0], row[1], row[2]))
            for row in (s.get("trunc") or [])[:5]:
                print("      截断 %s#%s %r %d>%d" % (row[0], row[1], row[3], row[4], row[5]))
            for row in (s.get("over") or [])[:5]:
                print("      溢出 %s#%s %r right=%d" % (row[0], row[1], row[3], row[4]))
        for e in js_err[:10]:
            print("  JS: %s" % e)

        if args.json:
            io.open(args.json, "w", encoding="utf-8").write(
                json.dumps(res, ensure_ascii=False, indent=1))
            print("  明细：%s" % args.json)

        ok = not fail and api_bad == 0 and not js_err
        print("\n%s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    except Exception:
        traceback.print_exc()
        return 2
    finally:
        if srv:
            srv.shutdown()
        if proc:
            proc.terminate()
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
