# -*- coding: utf-8 -*-
"""智能投放(原智能化)数据报表抓取。

报表中心 → 智能投放 的关键数据接口（jzt-api.jd.com）:
  POST /reweb/jst/account/account/list    账户总览
  POST /reweb/jst/account/campaign/list   计划总览
  POST /reweb/jst/account/sku/list        商品总览
  POST /reweb/jst/effect/searchword/list  搜索词报表
  POST /reweb/jst/effect/order/list       订单明细
  POST /reweb/jst/effect/location/list    地域

用法:
    python -m jd_roi.scrape_jst_report searchword 2026-09-22 2026-09-26 sw_cur
    python -m jd_roi.scrape_jst_report campaign   2026-09-22 2026-09-26 camp_cur
"""
from __future__ import annotations

import json
import sys

from . import config, store
from .browser import launch_profile

BASE = "https://jzt-api.jd.com/reweb/jst"
API = {
    "account": f"{BASE}/account/account/list",
    "campaign": f"{BASE}/account/campaign/list",
    "sku": f"{BASE}/account/sku/list",
    "searchword": f"{BASE}/effect/searchword/list",
    "order": f"{BASE}/effect/order/list",
    "location": f"{BASE}/effect/location/list",
}

COLS_ACCOUNT = [
    "impressions", "clicks", "CTR", "cost", "CPM", "CPC",
    "directOrderCnt", "directOrderSum", "indirectOrderCnt", "indirectOrderSum",
    "totalOrderCnt", "totalOrderSum", "totalPresaleOrderCnt", "totalPresaleOrderSum",
    "directCartCnt", "indirectCartCnt", "totalCartCnt", "totalCartCost",
    "totalOrderCVS", "totalOrderROI", "newCustomersCnt", "visitorCnt",
    "visitPageCnt", "visitTimeAverage", "depthPassengerCnt",
    "goodsAttentionCnt", "shopAttentionCnt", "preorderCnt", "couponCnt",
]

COLS_SW = ["impressions", "clicks", "CTR", "cost", "CPC", "totalOrderCnt",
           "totalOrderSum", "totalCartCnt", "totalOrderROI"]

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'accept': 'application/json, text/plain, */*',
      'referer': 'https://jzt.jd.com/jst/',
      'language': 'zh_CN',
      'siteid': '0',
      'loginmode': '0'
    },
    body: JSON.stringify(payload)
  });
  return {status: r.status, text: await r.text()};
}
"""


def _post(page, url: str, payload: dict) -> dict:
    res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
    if res["status"] != 200:
        raise RuntimeError(f"HTTP {res['status']}: {res['text'][:300]}")
    return json.loads(res["text"])


def _payload(start, end, columns, page, size, order_status=None):
    return {
        "isDaily": False, "startDay": start, "endDay": end,
        "obys": "impressions|desc", "filters": [],
        "clickOrOrderDay": 15, "clickOrOrderCaliber": 0,
        "orderStatusCategory": order_status, "page": page, "pageSize": size,
        "giftFlag": 0, "promotionMode": None, "columns": columns, "requestFrom": 0,
    }


def fetch(page, kind: str, start: str, end: str, page_size: int = 200,
          order_status=None) -> dict:
    url = API[kind]
    columns = COLS_SW if kind == "searchword" else COLS_ACCOUNT
    all_rows: list[dict] = []
    ext: dict = {}
    page_no = 1
    total = None
    while True:
        data = _post(page, url, _payload(start, end, columns, page_no, page_size, order_status))
        if str(data.get("code")) != "1":
            raise RuntimeError(f"接口异常: {json.dumps(data, ensure_ascii=False)[:300]}")
        d = data.get("data") or {}
        if not ext:
            ext = d.get("ext") or {}
        rows = d.get("datas") or []
        all_rows.extend(rows)
        if total is None:
            pg = d.get("paginator") or {}
            total = pg.get("items") or len(rows)
        if len(rows) < page_size:
            break
        page_no += 1
        if page_no > 60:
            break
    return {"ext": ext, "rows": all_rows, "total": total}


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    kind = args[0] if len(args) > 0 else "searchword"
    start = args[1] if len(args) > 1 else config.DATE_START
    end = args[2] if len(args) > 2 else config.DATE_END
    tag = args[3] if len(args) > 3 else "cur"
    order_status = 1 if kind == "order" else None

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto("https://jzt.jd.com/jst/#/report/account", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)
        result = fetch(page, kind, start, end, order_status=order_status)
        result["dateStart"] = start
        result["dateEnd"] = end
        name = f"jst_{kind}_{tag}"
        store.save(name, result, account)
        print(f"[OK] {kind} {start}~{end}: rows={len(result['rows'])} total={result['total']} -> data/{name}.json")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
