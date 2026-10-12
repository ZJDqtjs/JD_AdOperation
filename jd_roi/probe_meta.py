# -*- coding: utf-8 -*-
"""探测报表配置类接口：metainfo / indicator*，以及 crowd(人群) 报表的完整字段。

用法:
    python -m jd_roi.probe_meta [account] [start] [end]
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile

_JS = """
async ({url, method, payload}) => {
  const opt = {method: method||'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN',
      'siteid':'0','loginmode':'0'}};
  if (opt.method === 'POST') opt.body = JSON.stringify(payload||{});
  const r = await fetch(url, opt);
  return {status: r.status, text: await r.text()};
}
"""

TARGETS = [
    ("https://jzt-api.jd.com/reweb/common/config/metainfo", "POST", {}),
    ("https://jzt-api.jd.com/reweb/common/indicator", "POST", {}),
    ("https://jzt-api.jd.com/reweb/common/indicatorForList", "POST", {}),
    ("https://jzt-api.jd.com/reweb/common/indicator", "GET", None),
]


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if len(args) > 0 else "2026-10-03"
    end = args[1] if len(args) > 1 else "2026-10-09"
    aid = config.get_account(account).get("account_id") or 0

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        page.goto("https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)

        for url, method, payload in TARGETS:
            try:
                res = page.evaluate(_JS, {"url": url, "method": method, "payload": payload or {}})
            except Exception as exc:  # noqa: BLE001
                print(f"[{method}] {url}\n    eval失败: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            print(f"\n[{method}] {url}\n    HTTP {res['status']}")
            try:
                d = json.loads(res["text"])
            except Exception:  # noqa: BLE001
                print("    非JSON:", res["text"][:300])
                continue
            print("    code=", d.get("code"), "msg=", d.get("msg"))
            body = d.get("data")
            print("    data type:", type(body).__name__,
                  ("len=" + str(len(body))) if isinstance(body, (list, dict, str)) else "")
            print("    head:", json.dumps(body, ensure_ascii=False)[:2500])

        # 人群报表：完整列 + 样本
        payload = {
            "isDaily": False, "startDay": start, "endDay": end, "clickOrOrderCaliber": 0,
            "clickOrOrderDay": 15, "giftFlag": 0, "orderStatusCategory": 1, "filters": [],
            "pinIds": [aid] if aid else [], "columns": [], "obys": "cost|desc",
            "page": 1, "pageSize": 50, "needGdEffectOrder": False,
        }
        res = page.evaluate(_JS, {"url": "https://jzt-api.jd.com/reweb/msa/orientation/crowd/list",
                                  "method": "POST", "payload": payload})
        d = json.loads(res["text"])
        body = d.get("data") or {}
        rows = body.get("datas") or body.get("data") or []
        print(f"\n=== crowd(人群) n={len(rows)} extkeys={list(body.keys())} ===")
        if rows:
            print("    columns:", list(rows[0].keys()))
            for r in rows[:3]:
                print("    -", json.dumps(r, ensure_ascii=False)[:600])
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
