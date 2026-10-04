# -*- coding: utf-8 -*-
"""关键词/搜索词报表抓取。

报表中心(SPA) 的关键词报表面板走 jzt-api 接口:
  POST https://jzt-api.jd.com/reweb/msa/orientation/keyword/list
在已登录的报表页上下文内 fetch（自动带 Cookie），按日期区间分页拉全部词。

用法:
    python -m jd_roi.scrape_keyword [start] [end] [tag]
"""
from __future__ import annotations

import json
import re
import sys

from . import config, store
from .browser import launch_profile

KEYWORD_API = "https://jzt-api.jd.com/reweb/msa/orientation/keyword/list"

COLUMNS = [
    "impressions", "clicks", "CTR", "cost", "CPM", "CPC",
    "directOrderCnt", "directOrderSum", "indirectOrderCnt", "indirectOrderSum",
    "totalOrderCnt", "totalOrderSum", "totalPresaleOrderCnt", "totalPresaleOrderSum",
    "directCartCnt", "indirectCartCnt", "totalCartCnt", "totalCartRate", "totalCartCost",
    "totalOrderCVS", "CPA", "totalOrderROI",
]

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'accept': 'application/json, text/plain, */*',
      'referer': 'https://jzt.jd.com/report/index.html/',
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


def fetch_keywords(page, start: str, end: str, page_size: int = 200,
                   campaign_id: int | None = None, targeting_type: str = "",
                   account_id: int | None = None) -> dict:
    aid = account_id or config.ACCOUNT_ID
    all_rows: list[dict] = []
    page_no = 1
    total = None
    ext: dict = {}
    while True:
        payload = {
            "isDaily": False,
            "startDay": start,
            "endDay": end,
            "clickOrOrderCaliber": 0,
            "clickOrOrderDay": 30,
            "giftFlag": 0,
            "orderStatusCategory": 1,
            "filters": [],
            "pinIds": [aid],
            "targetingType": targeting_type,
            "columns": COLUMNS,
            "obys": "impressions|desc",
            "page": page_no,
            "pageSize": page_size,
            "needGdEffectOrder": False,
        }
        if campaign_id:
            payload["filters"] = [{"field": "campaignId", "operator": "=", "value": str(campaign_id)}]
        data = api_post(page, KEYWORD_API, payload)
        if data.get("code") != 1:
            raise RuntimeError(f"接口异常: {json.dumps(data, ensure_ascii=False)[:400]}")
        d = data.get("data") or {}
        if not ext:
            ext = {k: v for k, v in d.items() if k != "datas"}
        rows = d.get("datas") or []
        all_rows.extend(rows)
        if total is None:
            total = d.get("total") or d.get("totalCount") or d.get("count")
        if len(rows) < page_size:
            break
        page_no += 1
        if page_no > 50:
            break
    return {"ext": ext, "rows": all_rows, "total": total, "pageSize": page_size}


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if len(args) > 0 else config.DATE_START
    end = args[1] if len(args) > 1 else config.DATE_END
    tag = args[2] if len(args) > 2 else "cur"

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        # 先到报表中心建立 SSO，再进入关键词报表
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        page.goto(config.REPORT_KEYWORD, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)

        aid = config.get_account(account).get("account_id") or 0
        if not aid:  # 多账号时未配置 account_id -> 从页面读取
            try:
                m = re.search(r"账户ID[:：]\s*(\d+)", page.inner_text("body"))
                aid = int(m.group(1)) if m else 0
            except Exception:  # noqa: BLE001
                aid = 0
        print("pinIds =", aid)

        result = fetch_keywords(page, start, end, account_id=aid or None)
        result["dateStart"] = start
        result["dateEnd"] = end
        name = f"kw_{tag}"
        store.save(name, result, account)
        print(f"[OK] {start}~{end} 关键词行数={len(result['rows'])} total={result['total']} extKeys={list(result['ext'].keys())}")
        print("    ext:", json.dumps(result["ext"], ensure_ascii=False)[:600])
        for r in result["rows"][:15]:
            print(f"   - {str(r.get('campaignName'))[:24]:<24} {str(r.get('keywordName'))[:14]:<14} "
                  f"tt={r.get('targetingType')} 展现={r.get('impressions')} 点击={r.get('clicks')} 花费={r.get('cost')} "
                  f"订单={r.get('totalOrderCnt')} 金额={r.get('totalOrderSum')} ROI={r.get('totalOrderROI')}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
