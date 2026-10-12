# -*- coding: utf-8 -*-
"""拿到地域报表(自定义报表模板 41) queryData 的**完整**请求体。

用法:
    python -m jd_roi.probe_area_payload [account] [specificType]
输出:
    data/net/area_payload.json  —— 完整请求体（可直接改日期后复用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import config
from .browser import launch_profile


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    st = args[0] if args else "41"          # 41 = 地域计划组合报表
    out = Path(config.DATA_DIR) / "net" / "area_payload.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()

    def on_req(req) -> None:
        if "queryData" in req.url and req.method == "POST":
            captured.append({"url": req.url, "post": req.post_data or ""})

    page.on("request", on_req)
    try:
        page.goto(f"https://jzt.jd.com/custom-report/#/create?specificType={st}",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(22000)
        if not captured:
            print("未捕获到 queryData")
            return 1
        rec = captured[-1]
        print("URL:", rec["url"])
        try:
            payload = json.loads(rec["post"])
            print("顶层字段:", list(payload.keys()))
        except Exception as exc:  # noqa: BLE001
            print("payload 非严格 JSON:", exc)
            payload = rec["post"]
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("已写出完整请求体 ->", out)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
