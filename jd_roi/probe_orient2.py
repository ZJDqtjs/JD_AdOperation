# -*- coding: utf-8 -*-
"""补探：orientation/base 两族下的其余维度名。

用法:
    python -m jd_roi.probe_orient2 [account] [start] [end]
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile

_JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""

FAMILIES = {
    "orientation": ["commodity", "posPackage", "pospackage", "sku", "storePromo", "store",
                    "area", "areaEffect", "orderEffect", "time", "hour", "delivery",
                    "flowPacket", "audience", "crowd", "keyword", "negativeWord", "shoppingIntent"],
    "base": ["campaign", "group", "ad", "sku", "posPackage", "area", "crowd", "keyword",
             "storePromo", "delivery", "account"],
}
HOST = "https://jzt-api.jd.com/reweb/msa"


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if len(args) > 0 else "2026-10-03"
    end = args[1] if len(args) > 1 else "2026-10-09"
    aid = config.get_account(account).get("account_id") or 0
    payload = {
        "isDaily": False, "startDay": start, "endDay": end, "clickOrOrderCaliber": 0,
        "clickOrOrderDay": 15, "giftFlag": 0, "orderStatusCategory": 1, "filters": [],
        "pinIds": [aid] if aid else [], "columns": [], "obys": "cost|desc",
        "page": 1, "pageSize": 5, "needGdEffectOrder": False,
    }
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        page.goto("https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)
        for fam, dims in FAMILIES.items():
            print(f"\n=== {fam}/* ===")
            for dim in dims:
                url = f"{HOST}/{fam}/{dim}/list"
                try:
                    res = page.evaluate(_JS, {"url": url, "payload": payload})
                    d = json.loads(res["text"])
                except Exception as exc:  # noqa: BLE001
                    print(f"  {dim:<14} ✗ 不存在（{type(exc).__name__}）")
                    continue
                body = d.get("data")
                n = None
                cols = None
                if isinstance(body, dict):
                    rows = body.get("datas") or body.get("data") or []
                    n = len(rows) if isinstance(rows, list) else None
                    if isinstance(rows, list) and rows:
                        cols = list(rows[0].keys())
                print(f"  {dim:<14} code={d.get('code')} n={n} msg={str(d.get('msg') or '')[:40]}")
                if cols:
                    print(f"                 cols={cols[:22]}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
