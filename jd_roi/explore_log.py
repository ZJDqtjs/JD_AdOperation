# -*- coding: utf-8 -*-
"""探索京准通「操作日志」页面：捕获其网络请求与响应，用于逆向接口。

用法:
    python -m jd_roi.explore_log [--account=b] [url]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import config
from .browser import launch_profile

DEFAULT_URL = "https://jzt.jd.com/logging/#/business?businessType=-16"


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    url = args[0] if args else DEFAULT_URL
    wait_ms = int(args[1]) if len(args) > 1 else 20000

    out_dir = config.BASE_DIR / "data" / "explore"
    out_dir.mkdir(parents=True, exist_ok=True)

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    captured = []

    def on_response(resp):
        try:
            u = resp.url
            if not any(k in u for k in ("jd.com",)):
                return
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return
            req = resp.request
            entry = {"url": u, "method": req.method,
                     "postData": (req.post_data or "")[:4000],
                     "status": resp.status,
                     "reqHeaders": {k: v for k, v in (req.headers or {}).items()
                                    if k.lower() in ("referer", "cookie", "content-type", "origin", "user-mnp", "uuid")}}
            if "json" in ct:
                try:
                    entry["body"] = resp.text()[:20000]
                except Exception:
                    entry["body"] = "<unreadable>"
            captured.append(entry)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        # 先到首页建立 SSO
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        print("登录检测 pin cookie:", any(c.get("name") == "pin" for c in context.cookies()))
        print("当前URL:", page.url)

        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(wait_ms)
        print("日志页URL:", page.url)
        try:
            txt = page.inner_text("body")
            print("--- 页面文本(前2500字) ---")
            print(txt[:2500])
        except Exception as e:
            print("读取页面文本失败:", e)

        p = out_dir / f"log_{account or 'main'}.json"
        p.write_text(json.dumps(captured, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n[OK] 捕获 {len(captured)} 条网络记录 -> {p}")
    finally:
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
