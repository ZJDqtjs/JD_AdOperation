# -*- coding: utf-8 -*-
"""确认地域报表 queryData 的日期与分页参数。

用法: python -m jd_roi.probe_area_page [account]
"""
from __future__ import annotations

import calendar
import datetime as dt
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
    return calendar.timegm(dt.date.fromisoformat(day).timetuple()) * 1000


def show(tag, res):
    try:
        d = json.loads(res["text"])
    except Exception:  # noqa: BLE001
        print(f"{tag}: 非JSON {res['text'][:200]}")
        return
    body = d.get("data") or {}
    rep = body.get("reportData") or []
    pg = body.get("paginator") or {}
    dates = sorted({r.get("date") for r in rep})
    print(f"{tag}: code={d.get('code')} 行数={len(rep)} pageSize={pg.get('pageSize')} items={pg.get('items')} "
          f"日期={dates[:3]}...{dates[-3:] if len(dates) > 3 else ''}")


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    tpl = json.loads(TPLM.read_text(encoding="utf-8"))
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto("https://jzt.jd.com/custom-report/#/create?specificType=41",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(14000)

        def run(**over):
            p = json.loads(json.dumps(tpl))
            p.update(over)
            return page.evaluate(_JS, {"url": API, "payload": p})

        show("原始模板(30天)", run())
        show("+pageSize=200 ", run(page=1, pageSize=200))
        p2 = json.loads(json.dumps(tpl))
        p2["startTime"] = _ms("2026-10-03"); p2["endTime"] = _ms("2026-10-09") + 86399000
        p2["page"] = 1; p2["pageSize"] = 200
        show("指定10-03~10-09", page.evaluate(_JS, {"url": API, "payload": p2}))
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
