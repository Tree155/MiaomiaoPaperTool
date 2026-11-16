#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献妙妙小工具 v1.4.2

作者：Ni, Yishu & 大肥鱼（WorkBuddy · DeepSeek）
版权所有 © 2026 Ni, Yishu
仅供学习交流使用，禁止用于商业用途。

桌面客户端

把本地服务跑在后台线程里，再用系统原生的 WebView2 窗口加载界面。
双击 exe 即用，不需要命令行，也不需要开浏览器。

    python desktop.py
"""

from __future__ import annotations

# 必须在任何窗口/UI 库初始化之前声明 DPI 感知，
# 否则 125%/150% 缩放下 WebView 内容会被系统放大，导致右侧被裁。
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
    """当前系统缩放（125% -> 1.25）。"""
    try:
        return _ct.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def window_size() -> tuple[int, int, tuple[int, int]]:
    """
    pywebview 的 width/height 按逻辑像素解释，系统还会按 DPI 放大一次。
    想得到 1480x920 的物理窗口，就得先除以缩放比，否则高 DPI 下会超出屏幕。
    """
    k = dpi_scale()
    try:
        scr_w = _ct.windll.user32.GetSystemMetrics(0)
        scr_h = _ct.windll.user32.GetSystemMetrics(1)
    except Exception:
        scr_w, scr_h = 1920, 1080
    w = min(int(1480 / k), int(scr_w / k * 0.94))
    # 检索栏改成两行后内容变高，窗口相应加高；仍留出任务栏和标题栏的空间
    h = min(int(960 / k), int((scr_h - 90) / k))
    mn = (int(900 / k), int(620 / k))
    return w, h, mn


class Api:
    """暴露给网页的本地能力（仅桌面窗口模式可用）。"""

    def pick_folder(self) -> str:
        """弹系统文件夹选择框，返回绝对路径；取消返回空串。"""
        try:
            r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if not r:
                return ""
            return r[0] if isinstance(r, (list, tuple)) else str(r)
        except Exception:
            return ""

    def open_window(self, url: str, title: str = "") -> bool:
        """再开一个原生窗口（用于「打开全部」的全量列表）。"""
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
        """用系统默认浏览器打开链接（看原文、下载 PDF）。"""
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
    """windowed 模式没有控制台，出问题时把过程写到 exe 旁边的 startup.log。"""
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
    # 使用说明.md 改过就重新生成 .txt，免得两份不同步
    try:
        _trace("docs sync: " + docs_sync.sync())
    except Exception as e:
        _trace(f"docs sync failed: {type(e).__name__}")
    ui.CFG = pf.load_config()
    _trace("config ok")
    try:
        ui.zo.set_config(ui.CFG)      # Zotero 用这份配置，不再自己读文件
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
