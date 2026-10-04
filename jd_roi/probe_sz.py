# -*- coding: utf-8 -*-
"""探测商智商品明细接口可用的日期区间。"""
from __future__ import annotations
import sys
from . import config
from .browser import launch_profile
from .scrape_sz import INDICATORS, SzSession, URL_PRODUCT, _base

RANGES = [
    ("2026-09-28", "2026-10-04"),
    ("2026-09-28", "2026-10-03"),
    ("2026-09-27", "2026-10-03"),
    ("2026-09-29", "2026-10-04"),
    ("2026-10-01", "2026-10-04"),
    ("2026-10-01", "2026-10-03"),
    ("2026-09-28", "2026-10-05"),
    ("2026-09-21", "2026-09-27"),
]


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    pw, context = launch_profile(headless=True, account=account)
    try:
        for (s, e) in RANGES:
            sz = SzSession(context.new_page(), "product")
            sz.capture(wait_ms=12000)
            cstart, cend = "2026-09-01", "2026-09-07"
            d = sz.post(URL_PRODUCT, dict(_base(s, e, cstart, cend, sz.brands, sz.cates),
                                          proType="sku", indicators=INDICATORS, onlyAttention=False))
            hdr = d.get("header") or {}
            rows = (d.get("body") or {}).get("data") or []
            print(f"  {s} ~ {e}: code={hdr.get('code')} msg={str(hdr.get('message'))[:60]} rows={len(rows)}", flush=True)
            sz.page.close()
    finally:
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
