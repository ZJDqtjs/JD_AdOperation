# -*- coding: utf-8 -*-
"""按「天」抓取智能投放的计划/商品数据，用于把操作日志与当日效果对齐。

智能投放报表接口支持 isDaily=true，返回按 date 分行的数据。
用法:
    python -m jd_roi.scrape_daily 2026-09-01 2026-10-05 --account=main
"""
from __future__ import annotations

import json
import sys

from . import config, store
from .browser import launch_profile
from .scrape_jst_report import API as JST_API, COLS_ACCOUNT

_JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/jst/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _post(page, url, payload):
    res = page.evaluate(_JS, {"url": url, "payload": payload})
    if res["status"] != 200:
        raise RuntimeError(f"HTTP {res['status']}: {res['text'][:200]}")
    return json.loads(res["text"])


def fetch_daily(page, kind, start, end, page_size=200, daily=True):
    rows, ext, no = [], {}, 1
    while True:
        payload = {"isDaily": daily, "startDay": start, "endDay": end,
                   "obys": "impressions|desc", "filters": [],
                   "clickOrOrderDay": 15, "clickOrOrderCaliber": 0,
                   "orderStatusCategory": None, "page": no, "pageSize": page_size,
                   "giftFlag": 0, "promotionMode": None, "columns": COLS_ACCOUNT,
                   "requestFrom": 0}
        d = _post(page, JST_API[kind], payload)
        if str(d.get("code")) != "1":
            raise RuntimeError(f"{kind}: {json.dumps(d, ensure_ascii=False)[:300]}")
        body = d.get("data") or {}
        if not ext:
            ext = {k: v for k, v in body.items() if k != "datas"}
        b = body.get("datas") or []
        rows += b
        if len(b) < page_size or no > 80:
            break
        no += 1
    return {"ext": ext, "rows": rows, "daily": daily,
            "dateStart": start, "dateEnd": end,
            "total": (body.get("paginator") or {}).get("items", len(rows))}


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if args else "2026-09-01"
    end = args[1] if len(args) > 1 else "2026-10-05"
    kinds = [a for a in args[2:]] or ["campaign", "sku"]

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto("https://jzt.jd.com/jst/#/report/account", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(14000)
        for kind in kinds:
            for daily in (True, False):
                try:
                    r = fetch_daily(page, kind, start, end, daily=daily)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! {kind} daily={daily} 失败: {exc}", flush=True)
                    continue
                tag = "daily" if daily else "sum"
                store.save(f"jst_{kind}_{tag}", r, account)
                keys = sorted((r["rows"][0] or {}).keys()) if r["rows"] else []
                print(f"[OK] {kind} daily={daily} rows={len(r['rows'])} total={r['total']}", flush=True)
                if daily:
                    dates = sorted({str(x.get("date")) for x in r["rows"] if x.get("date")})
                    print(f"      dates={dates[:4]}...{dates[-3:] if len(dates) > 3 else ''}", flush=True)
                print(f"      rowKeys={keys[:14]}", flush=True)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
