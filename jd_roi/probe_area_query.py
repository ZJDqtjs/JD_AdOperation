# -*- coding: utf-8 -*-
"""验证地域报表 queryData 能否用自定义 payload 复现（改日期/维度/分页）。

用法:
    python -m jd_roi.probe_area_query [account] [start] [end]
"""
from __future__ import annotations

import calendar
import json
import sys
from pathlib import Path

from . import config
from .browser import launch_profile

API = "https://jzt-api.jd.com/dataCenter/customreport/v2/report/queryData?requestFrom=0&businessFrom=1"
TPLM = Path(config.DATA_DIR) / "net" / "area_payload.json"

_JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/custom-report/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _ms(day: str) -> int:
    import datetime as dt
    d = dt.date.fromisoformat(day)
    return calendar.timegm(d.timetuple()) * 1000


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if args else "2026-10-03"
    end = args[1] if len(args) > 1 else "2026-10-09"
    tpl = json.loads(TPLM.read_text(encoding="utf-8"))

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto("https://jzt.jd.com/custom-report/#/create?specificType=41",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)

        for dims in (["mappedAreaName"], ["mappedAreaName", "campaign"]):
            p = json.loads(json.dumps(tpl))
            p["sortedDimensionKeys"] = dims
            p["sortedIndexKeys"] = ["impressions", "clicks", "CTR", "cost", "CPM", "CPC",
                                    "totalOrderCnt", "totalOrderSum", "orderROI", "orderCVS",
                                    "totalCartCnt", "directOrderCnt", "directOrderSum",
                                    "indirectOrderCnt", "indirectOrderSum"]
            p["startTime"] = _ms(start)
            p["endTime"] = _ms(end + "T23:59:59") if False else _ms(end) + 86399000
            p["cycle"] = 30
            p["daily"] = 1
            p["timeout"] = False
            for extra in ("page", "pageSize"):
                p.pop(extra, None)
            res = page.evaluate(_JS, {"url": API, "payload": p})
            print(f"\n=== dims={dims} ===")
            try:
                d = json.loads(res["text"])
            except Exception:  # noqa: BLE001
                print("  非JSON:", res["text"][:400]); continue
            print("  code=", d.get("code"), "msg=", d.get("msg"))
            body = d.get("data") or {}
            rep = body.get("reportData") or []
            pg = body.get("paginator") or {}
            print(f"  行数={len(rep)} paginator={ {k: pg.get(k) for k in ('items','pageSize','pages')} }")
            if rep:
                print("  列:", list(rep[0].keys()))
                for r in rep[:4]:
                    print("   -", json.dumps(r, ensure_ascii=False)[:260])
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
