# -*- coding: utf-8 -*-
"""逐个打开报表维度页，抓取它们实际请求的后端接口。

用法:
    python -m jd_roi.probe_dims [account]
"""
from __future__ import annotations

import sys

from . import config
from .browser import launch_profile

ROUTES = [
    "/rtb/basic/crowd",
    "/rtb/basic/campaign",
    "/rtb/basic/group",
    "/rtb/basic/creative",
    "/rtb/basic/delivery",
    "/rtb/basic/posPackage",
    "/rtb/basic/sku",
    "/rtb/basic/storePromo",
    "/rtb/insight/crowd",
    "/rtb/order/index",
]
SKIP = ("/log", "beacon", "monitor", ".gif", ".png", ".css", ".js", "jad_", "joya", "tk.js")


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    hits: list[dict] = []

    def on_request(req) -> None:
        try:
            if req.resource_type not in ("xhr", "fetch"):
                return
            u = req.url
            if any(x in u for x in SKIP):
                return
            hits.append({"route": cur_route, "method": req.method, "url": u,
                         "post": (req.post_data or "")[:1200]})
        except Exception:  # noqa: BLE001
            pass

    cur_route = ""
    page.on("request", on_request)
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        for r in ROUTES:
            cur_route = r
            before = len(hits)
            try:
                page.goto(f"https://jzt.jd.com/report/index.html/#{r}",
                          wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(9000)
            except Exception as exc:  # noqa: BLE001
                print(f"\n### {r}  导航失败 {type(exc).__name__}")
                continue
            print(f"\n### {r}   （title={page.title()[:40]}）")
            for h in hits[before:]:
                u = h["url"]
                # 只打印可能是报表接口的
                if any(k in u for k in ("jzt-api", "atoms-api", "adssvc", "dspad", "/reweb/", "/msa/")):
                    print(f"   {h['method']:<5} {u[:150]}")
                    if h["post"]:
                        print(f"         post={h['post'][:400]}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
