#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Miaomiao Paper Tool v1.4.2

Author: Ni, Yishu & Da Fei Yu (WorkBuddy · DeepSeek)
Copyright © 2026 Ni, Yishu
For learning and exchange only; commercial use prohibited.

Desktop client

Run the local service on a background thread, then load the UI in the native WebView2 window.
Double-click the exe and it works; no command line and no browser needed.

    python desktop.py
"""

from __future__ import annotations

# DPI awareness must be declared before any window/UI library initializes,
# or at 125%/150% scaling the system scales the WebView content and clips the right side.
import ctypes as _ct

try:
    _ct.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        _ct.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import os
import socket
import sys as _sys
import threading
import time as _time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path as _Path

import webview

import docs_sync
import paperfetch as pf
import paperfetch_ui as ui


def dpi_scale() -> float:
    """Current system scaling (125% -> 1.25)."""
    try:
        return _ct.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def window_size() -> tuple[int, int, tuple[int, int]]:
    """
    pywebview interprets width/height in logical pixels, and the system scales them again by DPI.
    To get a 1480x920 physical window, divide by the scale factor first; otherwise it overflows the screen at high DPI.
    """
    k = dpi_scale()
    try:
        scr_w = _ct.windll.user32.GetSystemMetrics(0)
        scr_h = _ct.windll.user32.GetSystemMetrics(1)
    except Exception:
        scr_w, scr_h = 1920, 1080
    w = min(int(1480 / k), int(scr_w / k * 0.94))
    # The search bar became two rows so the content is taller; the window grows too, still leaving room for taskbar and title ba
    h = min(int(960 / k), int((scr_h - 90) / k))
    mn = (int(900 / k), int(620 / k))
    return w, h, mn


class Api:
    """Local capabilities exposed to the web page (available in desktop window mode only)."""

    def pick_folder(self) -> str:
        """Open the system folder picker and return the absolute path; return an empty string on cancel."""
        try:
            r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if not r:
                return ""
            return r[0] if isinstance(r, (list, tuple)) else str(r)
        except Exception:
            return ""

    def open_window(self, url: str, title: str = "") -> bool:
        """Open another native window (for the full list behind "Open All")."""
        try:
            k = dpi_scale()
            webview.create_window(
                title or "paperfetch",
                url,
                width=max(700, int(1360 / k)),
                height=max(500, int(900 / k)),
                min_size=(max(600, int(760 / k)), max(420, int(520 / k))),
                text_select=True,
            )
            return True
        except Exception as e:
            _trace(f"open_window failed: {type(e).__name__}: {e}")
            return False

    def open_url(self, url: str) -> bool:
        """Open a link in the system default browser (view the original, download the PDF)."""
        try:
            if not url.lower().startswith(("http://", "https://")):
                return False
            os.startfile(url)
            return True
        except Exception:
            return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _trace(msg: str) -> None:
    """Windowed mode has no console; on failure, write the trace to startup.log next to the exe."""
    try:
        base = _Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False) else _Path(__file__).parent
        with (base / "startup.log").open("a", encoding="utf-8") as f:
            f.write(f"{_time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _excepthook(t, e, tb):
    try:
        base = _Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False) else _Path(__file__).parent
        (base / "error.log").write_text("".join(traceback.format_exception(t, e, tb)), encoding="utf-8")
    except Exception:
        pass


_sys.excepthook = _excepthook


def main() -> None:
    _trace("main start")
    # Regenerate the .txt when the usage manual .md changes, so the two stay in sync
    try:
        _trace("docs sync: " + docs_sync.sync())
    except Exception as e:
        _trace(f"docs sync failed: {type(e).__name__}")
    ui.CFG = pf.load_config()
    _trace("config ok")
    try:
        ui.zo.set_config(ui.CFG)      # Zotero uses this config instead of reading the file itself
    except Exception as e:
        _trace(f"zotero cfg inject failed: {type(e).__name__}")
    if ui.CFG.get("_cfg_broken"):
        _trace(f"config broken, backed up: {ui.CFG['_cfg_broken']}")
    ui.SESS = pf.PoliteSession(ui.CFG)

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _trace(f"server on {port}")

    w, h, mn = window_size()
    _trace(f"window size {w}x{h}")
    webview.create_window(
        f"{pf.APP_NAME} v{pf.APP_VERSION}",
        f"http://127.0.0.1:{port}/",
        width=w,
        height=h,
        min_size=mn,
        js_api=Api(),
        text_select=True,
        confirm_close=False,
    )
    _trace("before webview.start")
    webview.start()
    _trace("webview.start returned")
    srv.shutdown()


if __name__ == "__main__":
    main()
