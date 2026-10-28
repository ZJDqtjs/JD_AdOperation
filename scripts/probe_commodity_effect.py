# -*- coding: utf-8 -*-
"""补齐「商品定向」那部分花费的转化数据（订单/ROI/加购），用于解释差额流量的效果。

用法（镜像内）：
    python /tmp/probe_commodity_effect.py <account> <campaignId> <day>
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "/app")

from jd_roi import config  # noqa: E402
from jd_roi.browser import launch_profile  # noqa: E402

_JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""

COLS = ["impressions", "clicks", "CTR", "cost", "CPM", "CPC",
        "directOrderCnt", "directOrderSum", "indirectOrderCnt", "indirectOrderSum",
        "totalOrderCnt", "totalOrderSum", "totalCartCnt", "totalCartRate", "totalCartCost",
        "totalOrderCVS", "CPA", "totalOrderROI"]


def main() -> int:
    account = sys.argv[1] if len(sys.argv) > 1 else "b"
    cid = int(sys.argv[2] if len(sys.argv) > 2 else "10239745421")
    day = sys.argv[3] if len(sys.argv) > 3 else "2026-10-27"
    aid = config.get_account(account).get("account_id") or 0

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(7000)
        page.goto(config.REPORT_KEYWORD, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(7000)

        payload = {
            "isDaily": False, "startDay": day, "endDay": day,
            "clickOrOrderCaliber": 0, "clickOrOrderDay": 15,
            "giftFlag": 0, "orderStatusCategory": 1,
            "pinIds": [aid] if aid else [], "filters": [],
            "columns": COLS, "obys": "cost|desc",
            "page": 1, "pageSize": 200, "needGdEffectOrder": False,
        }
        for dim in ("commodity", "keyword"):
            url = f"https://jzt-api.jd.com/reweb/msa/orientation/{dim}/list"
            res = page.evaluate(_JS, {"url": url, "payload": payload})
            d = json.loads(res["text"])
            rows = ((d.get("data") or {}).get("datas") or [])
            hit = [r for r in rows if int(r.get("campaignId") or 0) == cid]
            print(f"\n== {dim}: 本计划 {len(hit)} 行", flush=True)
            for r in sorted(hit, key=lambda x: -float(x.get("cost") or 0)):
                print("  ", json.dumps({k: r.get(k) for k in
                                        ("skuId", "skuName", "keywordName", "cost", "clicks",
                                         "impressions", "totalOrderCnt", "totalOrderSum",
                                         "totalOrderROI", "totalCartCnt", "CPA")},
                                       ensure_ascii=False), flush=True)
            time.sleep(1.2)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
