# -*- coding: utf-8 -*-
"""商智(jdsz.jd.com)数据抓取：商品明细 + 流量概况。

商智网关(szgateway.jd.com)要求动态签名头 user-mnp/user-mup/uuid。
做法：先打开商智页面，捕获其自身请求的头与维度(getDims)，再复用这些头发起指定日期的请求。

用法:
    python -m jd_roi.scrape_sz product 2026-09-22 2026-09-26 cur
    python -m jd_roi.scrape_sz flow    2026-09-22 2026-09-26 cur
"""
from __future__ import annotations

import json
import sys

from . import config, store
from .browser import launch_profile

SZ = "https://szgateway.jd.com/api"
URL_PRODUCT = f"{SZ}/lowcode/productDetail/table/productTable.ajax"
URL_CORE = f"{SZ}/lowcode/flowSummary/getCoreSummary.ajax"
URL_FLOWSRC = f"{SZ}/lowcode/flowSummary/productFlow/getFlowSrcTop.ajax"
URL_ADVERT = f"{SZ}/lowcode/flow/payFlow/advertSummary/getSummaryData.ajax"

INDICATORS = [
    "jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot",
    "jdr_sch_trade_deal_ord_sku_qtty_sz_trade_deal_snapshot",
    "jdr_sch_trade_deal_ord_ord_qtty_sz_trade_deal_snapshot",
    "fo_jdr_sch_industry_deal_rate",
    "jdr_sch_traffic_brow_sku__page_qtty_traffic_plat_item_di_sz_bsg",
    "jdr_sch_traffic_brow_sku__page_cnt_traffic_plat_item_di_sz_bsg",
]

_JS_POST = """
async ({url, payload, headers, referer}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers: Object.assign({'Content-Type':'application/json',
      'accept':'application/json, text/plain, */*',
      'referer': referer || 'https://jdsz.jd.com/'}, headers || {}),
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _leaves(nodes, out):
    for n in nodes or []:
        if n.get("children"):
            _leaves(n["children"], out)
        elif n.get("value"):
            out.append(n["value"])


class SzSession:
    """持有页面、动态头与维度。"""

    def __init__(self, page: "object", kind: str = "product"):
        self.page = page
        self.kind = kind
        self.headers: dict = {}
        self.brands: list = []
        self.cates: list = []
        self.page_url = ("https://jdsz.jd.com/szweb/view/flow/flow-summary.html"
                         if kind == "flow" else
                         "https://jdsz.jd.com/szweb/view/market/advert-summary.html"
                         if kind == "ad" else
                         "https://jdsz.jd.com/szweb/view/product/productDetail.html")
        self.trigger = ("getCoreSummary.ajax" if kind == "flow" else
                        "advertSummary" if kind == "ad" else "productTable.ajax")

    def capture(self, wait_ms: int = 14000) -> None:
        def on_request(req):
            try:
                if self.trigger in req.url and not self.headers:
                    h = req.headers
                    for k in ("user-mnp", "user-mup", "uuid", "x-requested-with"):
                        if k in h:
                            self.headers[k] = h[k]
            except Exception:
                pass

        def on_response(resp):
            try:
                if "getDims.ajax" in resp.url and not self.brands:
                    d = json.loads(resp.text())
                    for dim in (d.get("body") or {}).get("data") or []:
                        if dim.get("dimCode") == "bsBrand":
                            self.brands = [v["value"] for v in dim.get("dimVals") or [] if v.get("value")]
                        if dim.get("dimCode") == "bsCate":
                            _leaves(dim.get("dimVals"), self.cates)
            except Exception:
                pass

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        self.page.goto(self.page_url, wait_until="domcontentloaded", timeout=90000)
        self.page.wait_for_timeout(wait_ms)

    def post(self, url: str, payload: dict) -> dict:
        res = self.page.evaluate(_JS_POST, {
            "url": url, "payload": payload, "headers": self.headers, "referer": self.page_url})
        return json.loads(res["text"])


def _base(start, end, cstart, cend, brands, cates, extra=None):
    p = {
        "realtime": False, "interval": "DAY", "dateType": "day",
        "startDate": start, "endDate": end,
        "compareStartDate": cstart, "compareEndDate": cend, "compareType": "hb",
        "bsBrand": brands, "bsCate3": cates, "channel": "all",
    }
    if extra:
        p.update(extra)
    return p


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    kind = args[0] if len(args) > 0 else "product"
    start = args[1] if len(args) > 1 else config.DATE_START
    end = args[2] if len(args) > 2 else config.DATE_END
    tag = args[3] if len(args) > 3 else "cur"
    cstart, cend = config.DATE_CMP_START, config.DATE_CMP_END

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        sz = SzSession(page, kind)
        sz.capture()
        print("headers:", {k: str(v)[:24] for k, v in sz.headers.items()}, "brands=", sz.brands, "cates=", sz.cates)
        if not sz.brands:
            print("[WARN] 未捕获到维度，接口可能失败")

        if kind == "ad":
            d = sz.post(URL_ADVERT, _base(start, end, cstart, cend, sz.brands, sz.cates))
            result = {"raw": d, "rows": ((d.get("body") or {}).get("data") or {}),
                      "dateStart": start, "dateEnd": end}
            name = f"sz_ad_{tag}"
            store.save(name, result, account)
            print(f"[OK] ad {start}~{end} -> {store._path(name, account)}")
            print(json.dumps(result["rows"], ensure_ascii=False)[:1500])
            return 0
        if kind == "flow":
            core = sz.post(URL_CORE, _base(start, end, cstart, cend, sz.brands, sz.cates))
            src = sz.post(URL_FLOWSRC, _base(start, end, cstart, cend, sz.brands, sz.cates, {
                "referIndicator": "jdr_sch_traffic_brow_sku_cnt_jd_unified_attribution_sz",
                "sort": "top", "attentionType": "",
                "groups": ["jdr_sch_traffic_cha_last_field_src_rmad_sz_2"],
                "attributes": ["jdr_sch_traffic_cha_last_field_src_rmad_sz_2"],
            }))
            result = {"core": (core.get("body") or {}).get("data") or [],
                      "source": (src.get("body") or {}).get("data") or [],
                      "rawCode": (core.get("header") or {}).get("code"),
                      "dateStart": start, "dateEnd": end}
            name = f"sz_flow_{tag}"
        else:
            d = sz.post(URL_PRODUCT, _base(start, end, cstart, cend, sz.brands, sz.cates, {
                "proType": "sku", "indicators": INDICATORS, "onlyAttention": False}))
            code = (d.get("header") or {}).get("code")
            if code != 0:
                raise RuntimeError(f"商品明细异常 code={code}: {json.dumps(d, ensure_ascii=False)[:300]}")
            result = {"rows": (d.get("body") or {}).get("data") or [], "dateStart": start, "dateEnd": end}
            name = f"sz_product_{tag}"

        store.save(name, result, account)
        n = len(result.get("rows") or result.get("core") or [])
        print(f"[OK] {kind} {start}~{end}: {n} 行 -> {store._path(name, account)}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
