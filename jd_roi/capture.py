# -*- coding: utf-8 -*-
"""网络抓包：记录页面 XHR/fetch 请求与JSON响应，用于定位数据接口。

用法:
    .venv\\Scripts\\python.exe -m jd_roi.capture <name> <url> [wait_ms] [--click=sel1,sel2]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import config
from .browser import launch_profile


def capture(name: str, url: str, wait_ms: int = 20000, clicks: list[str] | None = None) -> None:
    out = config.DATA_DIR / "net"
    out.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    pw, context = launch_profile(headless=True)
    page = context.new_page()

    def on_response(resp) -> None:
        try:
            req = resp.request
            rt = req.resource_type
            if rt not in ("xhr", "fetch"):
                return
            rurl = resp.url
            if any(x in rurl for x in ("/log", "beacon", "monitor", ".gif", ".png")):
                return
            body = ""
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype or "javascript" in ctype:
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    body = "<unreadable>"
            records.append(
                {
                    "url": rurl,
                    "method": req.method,
                    "headers": dict(req.headers),
                    "postData": (req.post_data or "")[:4000],
                    "status": resp.status,
                    "ctype": ctype,
                    "body": body[:200000],
                }
            )
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(wait_ms)
        for sel in clicks or []:
            try:
                page.click(sel, timeout=8000)
                print("clicked:", sel)
                page.wait_for_timeout(6000)
            except Exception as exc:  # noqa: BLE001
                print("click fail:", sel, type(exc).__name__)
        page.wait_for_timeout(3000)
    finally:
        ts = time.strftime("%H%M%S")
        fp = out / f"{name}_{ts}.json"
        fp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n共捕获 {len(records)} 条 XHR/fetch -> {fp}")
        for i, rec in enumerate(records):
            print(f"  [{i}] {rec['method']} {rec['status']} {rec['url'][:130]}")
        context.close()
        pw.stop()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    clicks = []
    for a in sys.argv[1:]:
        if a.startswith("--click="):
            clicks = [s for s in a.split("=", 1)[1].split(",") if s]
    nm, u = args[0], args[1]
    w = int(args[2]) if len(args) > 2 else 20000
    capture(nm, u, w, clicks)
