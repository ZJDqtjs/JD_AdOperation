# -*- coding: utf-8 -*-
"""排查单个计划的花费在报表中心各维度里的分布（v2：全量拉取 + 本地过滤）。

v1 用 filters 里的 campaignId 过滤，接口不认（返回 0 行）；v2 改成拉全量再本地筛。
同时对每个维度做多家族探测（orientation / base / atoms promolist），逐条容错。

用法（镜像内）：
    python /tmp/probe_plan_spend_dims.py <account> <campaignId> <day>
"""
from __future__ import annotations

import json
import random
import sys
import time

sys.path.insert(0, "/app")

from jd_roi import browser, config  # noqa: E402
from jd_roi.browser import launch_profile  # noqa: E402

_JS = """
async ({url, payload}) => {
  try {
    const r = await fetch(url, {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
        'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN','siteid':'0','loginmode':'0'},
      body: JSON.stringify(payload)});
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: -1, text: String(e)};
  }
}
"""

ORIENT = ["keyword", "crowd", "creative", "group", "unit", "plan", "campaign",
          "searchword", "flowPacket", "goodsOrient", "goodsOrientEffect",
          "posPackage", "delivery", "areaEffect", "orderEffect", "sku",
          "storePromo", "area", "orient", "audience", "dmp", "negativeWord",
          "commodity", "time", "hour"]
BASE = ["campaign", "group", "ad", "creative", "sku", "posPackage", "area",
        "crowd", "keyword", "storePromo", "delivery", "account"]
ATOMS = ["campaign", "unit", "creative", "keyword", "area", "audience", "crowd", "sku"]

URLS = ([(f"https://jzt-api.jd.com/reweb/msa/orientation/{d}/list", f"orient/{d}") for d in ORIENT]
        + [(f"https://jzt-api.jd.com/reweb/msa/base/{d}/list", f"base/{d}") for d in BASE]
        + [(f"https://atoms-api.jd.com/dspad/msa/promolist/overview/{d}", f"atoms/{d}") for d in ATOMS])


def sleep_a_bit() -> None:
    time.sleep(random.uniform(0.5, 1.0))


def post(page, url: str, payload: dict) -> dict:
    for _ in range(2):
        try:
            res = page.evaluate(_JS, {"url": url, "payload": payload})
        except Exception as exc:  # noqa: BLE001
            time.sleep(1.0)
            continue
        try:
            return json.loads(res["text"])
        except Exception:  # noqa: BLE001
            return {"code": -1, "msg": f"HTTP {res['status']} {res['text'][:100]}"}
    return {"code": -1, "msg": "evaluate failed"}


def rows_of(d: dict) -> list:
    body = d.get("data")
    if isinstance(body, dict):
        for k in ("datas", "data", "list", "rows"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    if isinstance(body, list):
        return body
    return []


def num(v) -> float:
    try:
        return float(v or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def show_keys(r: dict) -> dict:
    keep = ("campaignName", "campaignId", "groupName", "groupId", "keywordName", "searchTerm",
            "dmpName", "crowdName", "creativeName", "adName", "skuId", "skuName",
            "posPackageName", "areaName", "deliveryName", "name", "hour", "targetingType",
            "cost", "clicks", "impressions", "promotionMode", "campaignFlag")
    return {k: r.get(k) for k in keep if k in r}


def main() -> int:
    account = sys.argv[1] if len(sys.argv) > 1 else "b"
    cid = int(sys.argv[2] if len(sys.argv) > 2 else "10239745421")
    day = sys.argv[3] if len(sys.argv) > 3 else "2026-10-27"
    aid = config.get_account(account).get("account_id") or 0
    print(f"account={account} aid={aid} campaignId={cid} day={day}", flush=True)

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        page.goto(config.REPORT_KEYWORD, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        print("session_ok =", browser.session_ok(page), flush=True)

        base = {
            "isDaily": False, "startDay": day, "endDay": day,
            "clickOrOrderCaliber": 0, "clickOrOrderDay": 15,
            "giftFlag": 0, "orderStatusCategory": 1,
            "pinIds": [aid] if aid else [],
            "columns": [], "obys": "cost|desc",
            "page": 1, "pageSize": 500, "needGdEffectOrder": False,
        }

        for url, tag in URLS:
            d0 = post(page, url, dict(base, filters=[]))
            sleep_a_bit()
            if d0.get("code") != 1:
                print(f"{tag:<22} ✗ code={d0.get('code')} {str(d0.get('msg'))[:50]}", flush=True)
                continue
            rows = rows_of(d0)
            cost_all = sum(num(r.get("cost")) for r in rows)
            hit = [r for r in rows if int(num(r.get("campaignId"))) == cid]
            cost_plan = sum(num(r.get("cost")) for r in hit)
            clk = sum(num(r.get("clicks")) for r in hit)
            print(f"{tag:<22} rows={len(rows):<4} 账户cost={cost_all:9.2f} | 本计划 rows={len(hit):<3} "
                  f"cost={cost_plan:8.2f} clicks={clk:.0f}", flush=True)
            if hit:
                for r in sorted(hit, key=lambda x: -num(x.get("cost")))[:10]:
                    print("      ", json.dumps(show_keys(r), ensure_ascii=False)[:300], flush=True)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
