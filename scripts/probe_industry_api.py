# -*- coding: utf-8 -*-
"""验证行业大盘接口可以直接用 fetch 复现（不依赖页面点击）。

开发容器内运行：
    docker exec -w /app -e PYTHONPATH=/app jd-roi-dev python /tmp/probe_industry_api.py main
"""
from __future__ import annotations

import json
import sys
import time

from jd_roi.browser import launch_profile, session_ok

BASE = "https://jzt-api.jd.com/dataCenter/industry/v2/"
HOME = "https://jzt.jd.com/report/index.html"
JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def main() -> int:
    account = sys.argv[1] if len(sys.argv) > 1 else "main"
    start, end, cid3, cname = "2026-10-01", "2026-10-07", 13575, "根茎类"
    common = {
        "selfBrandName": "", "businessType": [-4, 256], "cid3": [cid3],
        "clickOrOrderDay": 15, "clickOrOrderCaliber": 0, "isGift": 0,
        "orderStatusCategory": 1, "impressionOrClickCaliber": 0,
        "startDate": start, "endDate": end, "analyzeType": 2,
        "campaignTypes": [],
        "reviewCache": {"dateRange": f"{start}至{end}", "businessType": ", 站外广告",
                        "cid3": cname, "calibers": "15天/点击/不含赠品/成交订单"},
    }
    cases = [
        ("industry/getCategory", {}, "GET_CATEGORY"),
        ("industry/whole", {}, "WHOLE"),
        ("industry/whole", {"granularity": 9}, "HOURLY"),
        ("industry/detail", {"dimension": "product"}, "DETAIL"),
        ("self/detail", {"dimension": "product"}, "SELF"),
        ("industry/user/customer", {}, "CUSTOMER"),
        ("self/user/customer", {}, "SELF_CUSTOMER"),
        ("industry/user/area", {}, "AREA"),
        ("industry/brands", {}, "BRANDS"),
        ("keyword/compete", {"pageNum": 1, "pageSize": 50, "obys": ""}, "COMPETE"),
        ("keyword/billboard", {"pageNum": 1, "pageSize": 50, "obys": ""}, "BILLBOARD"),
        ("industry/searchWord/hotWords", {"searchWordType": 0}, "HOT0"),
        ("industry/searchWord/soaringWords", {"searchWordType": 0}, "SOAR0"),
        ("industry/searchWord/newWords", {"searchWordType": 0}, "NEW0"),
        ("industry/searchWord/hotWords", {"searchWordType": 1}, "HOT1"),
    ]
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        print("session_ok", session_ok(page))
        for path, extra, tag in cases:
            payload = dict(common)
            payload.update(extra)
            if path.endswith("getCategory"):
                payload = {}
            res = page.evaluate(JS, {"url": BASE + path + "?requestFrom=0&businessFrom=1",
                                     "payload": payload})
            try:
                d = json.loads(res["text"])
                data = d.get("data")
                if isinstance(data, dict):
                    rows = data.get("rows") or []
                    shape = f"rows={len(rows)} total={data.get('total')}"
                elif isinstance(data, list):
                    shape = f"list={len(data)}"
                else:
                    shape = f"data={str(data)[:40]}"
                print(f"{tag:<15} http={res['status']} code={d.get('code')} {shape} "
                      f"msg={str(d.get('msg') or '')[:60]}")
            except Exception as exc:  # noqa: BLE001
                print(f"{tag:<15} http={res['status']} 非JSON {exc} {res['text'][:120]}")
            time.sleep(1.1)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
