# -*- coding: utf-8 -*-
"""京准通数据抓取：计划列表（含各计划指标）。

原理：京准通为 SPA，数据经 atoms-api.jd.com 的 JSON 接口加载。
在已登录的 jzt.jd.com 页面上下文内直接 fetch 该接口（自动带 Cookie），
按日期区间分页拉取全部推广计划。
"""
from __future__ import annotations

import json
import sys

from . import config, store
from .browser import launch_profile

CAMPAIGN_API = "https://atoms-api.jd.com/dspad/msa/promolist/overview/campaign"

# 与页面一致的完整列（含加购、转化等）
CUSTOM_COLUMNS = [
    "campaignName", "campaignId", "childProNo", "campaignType", "spuId",
    "status", "dayBudgetStr", "time", "timeRange",
    "totalOrderROI", "impressions", "clicks", "CTR", "cost", "CPC",
    "totalOrderCnt", "totalOrderSum", "totalCartCnt", "totalCartRate",
    "totalCartCost", "totalOrderCVS", "CPA",
]

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'accept': 'application/json, text/plain, */*',
      'referer': 'https://jzt.jd.com/',
      'language': 'zh_CN',
      'siteid': '0',
      'loginmode': '0'
    },
    body: JSON.stringify(payload)
  });
  return {status: r.status, text: await r.text()};
}
"""


def api_post(page, url: str, payload: dict) -> dict:
    res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
    if res["status"] != 200:
        raise RuntimeError(f"接口HTTP {res['status']}: {res['text'][:300]}")
    return json.loads(res["text"])


def fetch_campaigns(page, start: str, end: str, page_size: int = 50) -> dict:
    page_no = 1
    all_rows: list[dict] = []
    ext: dict = {}
    total = None
    while True:
        payload = {
            "page": page_no,
            "pageSize": page_size,
            "status": "",
            "filters": [],
            "obys": "",
            "startDay": start,
            "endDay": end,
            "clickOrOrderCaliber": 0,
            "clickOrOrderDay": 15,
            "giftFlag": 0,
            "orderStatusCategory": 1,
            "customColumns": CUSTOM_COLUMNS,
            "requestFrom": 0,
        }
        data = api_post(page, CAMPAIGN_API, payload)
        if data.get("code") != 1:
            raise RuntimeError(f"接口返回异常: {json.dumps(data, ensure_ascii=False)[:300]}")
        d = data.get("data") or {}
        if not ext:
            ext = d.get("ext") or {}
        rows = d.get("data") or []
        all_rows.extend(rows)
        if total is None:
            total = d.get("totalCount") or d.get("total") or len(rows)
        if len(rows) < page_size:
            break
        page_no += 1
        if page_no > 50:
            break
    return {"ext": ext, "rows": all_rows, "pageSize": page_size, "total": total}


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if len(args) > 0 else config.DATE_START
    end = args[1] if len(args) > 1 else config.DATE_END
    tag = args[2] if len(args) > 2 else "cur"

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)  # 等页面初始化、Cookie 就绪

        result = fetch_campaigns(page, start, end)
        result["dateStart"] = start
        result["dateEnd"] = end
        result["account"] = account or config.MAIN_ACCOUNT
        name = f"jzt_campaign_{tag}"
        store.save(name, result, account)
        print(f"[OK] {start} ~ {end}: 共 {len(result['rows'])} 个计划 -> data/{name}.json")
        e = result["ext"]
        print(f"     汇总ext: 花费={e.get('cost')} 展现={e.get('impressions')} 点击={e.get('clicks')} "
              f"订单={e.get('totalOrderCnt')} 金额={e.get('totalOrderSum')} 投产比={e.get('totalOrderROI')}")
        for r in result["rows"][:30]:
            print(f"   - {str(r.get('campaignName'))[:34]:<34} 花费={r.get('cost'):>8} ROI={r.get('totalOrderROI'):>6} "
                  f"点击={r.get('clicks'):>5} 订单={r.get('totalOrderCnt')} 金额={r.get('totalOrderSum')}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
