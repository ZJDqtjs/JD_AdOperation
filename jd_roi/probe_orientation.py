# -*- coding: utf-8 -*-
"""探测京准通报表「非关键词」维度的接口是否可用。

在已登录的报表页上下文里，对一批候选端点逐个 POST，打印 code/msg/首行字段，
用来发现 人群/地域/时段/单元/创意 等维度的真实接口。

用法:
    python -m jd_roi.probe_orientation [account] [start] [end]
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile

# 与关键词接口同族（jzt-api …/reweb/msa/orientation/<x>/list）
# 维度名来自报表 SPA 主 JS 的 report 维度映射：
#   promotion: plan/unit/creative/flowPacket/crowd/goodsOrient
#   dataReport: orderEffect/areaEffect/goodsOrientEffect/account/plan/unit/creative/flowPacket/goodsOrient
ORIENT_DIMS = ["keyword", "crowd", "flowPacket", "goodsOrient", "goodsOrientEffect",
               "areaEffect", "orderEffect", "account", "plan", "unit", "creative",
               "searchword", "negativeWord", "area", "orient", "delivery",
               "crowdList", "crowdPackage", "dmp", "audience"]
JZT_API = "https://jzt-api.jd.com/reweb/msa/orientation/{dim}/list"

# atoms-api promolist 家族
ATOMS_DIMS = ["campaign", "unit", "creative", "keyword", "area", "audience", "crowd"]
ATOMS_API = "https://atoms-api.jd.com/dspad/msa/promolist/overview/{dim}"

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN',
      'siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _payload(start: str, end: str, aid: int, columns=None) -> dict:
    return {
        "isDaily": False, "startDay": start, "endDay": end,
        "clickOrOrderCaliber": 0, "clickOrOrderDay": 15, "giftFlag": 0,
        "orderStatusCategory": 1, "filters": [], "pinIds": [aid] if aid else [],
        "columns": columns or [], "obys": "cost|desc", "page": 1, "pageSize": 5,
        "needGdEffectOrder": False,
    }


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    start = args[0] if len(args) > 0 else config.DATE_START
    end = args[1] if len(args) > 1 else config.DATE_END
    aid = config.get_account(account).get("account_id") or 0

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        page.goto("https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)

        def probe(url: str, payload: dict) -> tuple:
            try:
                res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
            except Exception as exc:  # noqa: BLE001
                return None, f"eval失败 {type(exc).__name__}: {exc}"
            try:
                return json.loads(res["text"]), f"HTTP {res['status']}"
            except Exception:  # noqa: BLE001
                return None, f"HTTP {res['status']} 非JSON: {res['text'][:120]}"

        print(f"=== jzt-api orientation/* （aid={aid}, {start}~{end}）===")
        for dim in ORIENT_DIMS:
            data, note = probe(JZT_API.format(dim=dim), _payload(start, end, aid))
            if data is None:
                print(f"  {dim:<12} ✗ {note}")
                continue
            code = data.get("code")
            msg = data.get("msg") or data.get("message") or ""
            body = data.get("data")
            n = None
            keys = None
            if isinstance(body, dict):
                rows = body.get("datas") or body.get("data") or []
                n = len(rows) if isinstance(rows, list) else None
                if isinstance(rows, list) and rows:
                    keys = list(rows[0].keys())[:14]
            print(f"  {dim:<12} code={code} n={n} msg={str(msg)[:60]}")
            if keys:
                print(f"               cols={keys}")

        print(f"\n=== atoms-api promolist/overview/* ===")
        for dim in ATOMS_DIMS:
            data, note = probe(ATOMS_API.format(dim=dim), _payload(start, end, aid))
            if data is None:
                print(f"  {dim:<12} ✗ {note}")
                continue
            code = data.get("code")
            body = data.get("data")
            n = None
            keys = None
            if isinstance(body, dict):
                rows = body.get("data") or body.get("datas") or []
                n = len(rows) if isinstance(rows, list) else None
                if isinstance(rows, list) and rows:
                    keys = list(rows[0].keys())[:14]
            print(f"  {dim:<12} code={code} n={n} msg={str(data.get('msg') or '')[:50]}")
            if keys:
                print(f"               cols={keys}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
