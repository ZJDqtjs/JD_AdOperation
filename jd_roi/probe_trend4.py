# -*- coding: utf-8 -*-
"""实证：用 trendchart 的分时数据做「时段ROI」分析，看能否指导调价。

产出 data/explore/trend_hourly.json：
  - account: 账号级 24 小时曲线（两天的对比）
  - plans:   高花费计划的 24 小时曲线
  - waste:   高花费 + 低ROI 的「问题时段」清单（可直接对应分时折扣）
用法: .venv\\Scripts\\python.exe -m jd_roi.probe_trend4 --account=main --day=2026-10-11
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile

OUT = config.DATA_DIR / "explore" / "trend_hourly.json"
MAT = "https://atoms-api.jd.com/reweb/common/index/material/trendchart"
ACC = "https://atoms-api.jd.com/reweb/common/index/trendchart"
PLANS = "https://atoms-api.jd.com/dspad/msa/promolist/overview/campaign"

BASE = {
    "businessType": -4, "mediaResourceType": None, "interactiveType": None,
    "isMsa": 1, "type": 1, "copyWriting": "广告点击15天内累计的成交订单",
    "clickOrOrderDay": 15, "giftFlag": 0, "clickOrOrderCaliber": 0,
    "orderStatusCategory": 1, "deliverySystemType": 0,
}

JS = """async ({url, payload}) => {
  try {
    const r = await fetch(url, {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
        'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
      body: JSON.stringify(payload)});
    const t = await r.text();
    try { return {status: r.status, json: JSON.parse(t)}; }
    catch(e) { return {status: r.status, text: t.slice(0, 500)}; }
  } catch (e) { return {error: String(e)}; }
}"""


def _arg(name: str, default: str | None = None) -> str | None:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def f(x, d=0.0) -> float:
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return d


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    day = _arg("day", "2026-10-11")
    pw, ctx = launch_profile(headless=True, account=account)
    page = ctx.new_page()
    out: dict = {"account": account, "day": day}
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(14000)

        def post(url: str, payload: dict) -> dict:
            r = page.evaluate(JS, {"url": url, "payload": payload})
            return (r or {}).get("json") or {}

        # 1) 当天计划列表（拿 campaignId / 花费 / ROI）
        pl = post(PLANS, {"page": 1, "pageSize": 50, "status": "", "filters": [], "obys": "",
                          "startDay": day, "endDay": day, "clickOrOrderCaliber": 0,
                          "clickOrOrderDay": 15, "giftFlag": 0, "orderStatusCategory": 1,
                          "customColumns": ["campaignName", "campaignId", "cost",
                                            "totalOrderSum", "totalOrderROI", "totalOrderCnt",
                                            "clicks", "impressions"],
                          "requestFrom": 0})
        rows = ((pl.get("data") or {}) if isinstance(pl.get("data"), dict) else {}).get("list") or []
        if not rows:
            rows = (pl.get("data") or {}).get("campaignList") or []
        plans = []
        for c in rows:
            if not isinstance(c, dict):
                continue
            cid = c.get("campaignId")
            cost = f(c.get("cost"))
            if not cid or cost <= 0:
                continue
            plans.append({"campaignId": cid, "name": c.get("campaignName"),
                          "cost": round(cost, 2), "orderSum": round(f(c.get("totalOrderSum")), 2),
                          "roi": round(f(c.get("totalOrderROI")), 3),
                          "cnt": int(f(c.get("totalOrderCnt"))),
                          "clicks": int(f(c.get("clicks")))})
        plans.sort(key=lambda x: -x["cost"])
        out["plansToday"] = plans[:20]

        # 2) 账号级分时
        acc = post(ACC, dict(BASE, granularity=4,
                             startDay=f"{day} 00:00:00", endDay=f"{day} 23:59:59"))
        out["accountHourly"] = acc.get("data") or []

        # 3) 高花费计划的分时（前 8 个）
        per: dict = {}
        for p in plans[:8]:
            d = post(MAT, dict(BASE, granularity=4, campaignIds=[p["campaignId"]],
                               startDay=f"{day} 00:00:00", endDay=f"{day} 23:59:59"))
            per[str(p["campaignId"])] = {"name": p["name"], "rows": d.get("data") or []}
            page.wait_for_timeout(400)
        out["planHourly"] = per
    finally:
        # 4) 汇总：账号级「问题时段」
        hrs = out.get("accountHourly") or []
        tot = sum(f(h.get("cost")) for h in hrs)
        waste = []
        for h in hrs:
            c = f(h.get("cost"))
            if c <= 0:
                continue
            roi = f(h.get("totalOrderROI"))
            waste.append({
                "hour": str(h.get("clickDate"))[8:10] + ":00",
                "cost": round(c, 2), "share": round(c / tot * 100, 1) if tot else 0,
                "orderSum": round(f(h.get("totalOrderSum")), 2),
                "roi": round(roi, 2), "cnt": int(f(h.get("totalOrderCnt"))),
                "cpc": round(f(h.get("CPC")), 2),
                "cpm": round(f(h.get("CPM")), 1),
            })
        out["hourTable"] = waste
        out["hourTable"].sort(key=lambda x: x["roi"])
        # 半日 vs 全天：把「花费占比高且 ROI 明显低于全天均值」的时段挑出来
        avg = (sum(f(h.get("totalOrderSum")) for h in hrs) / tot) if tot else 0
        out["dayTotals"] = {
            "cost": round(tot, 2),
            "orderSum": round(sum(f(h.get("totalOrderSum")) for h in hrs), 2),
            "roi": round((sum(f(h.get("totalOrderSum")) for h in hrs) / tot) if tot else 0, 3),
            "avgRoiPerYuan": round(avg, 3),
        }
        out["problemHours"] = [w for w in out["hourTable"]
                               if w["share"] >= 3 and w["roi"] < avg * 0.7]

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("day:", day, "plans:", len(out.get("plansToday") or []),
              "hours:", len(hrs))
        print("totals:", json.dumps(out.get("dayTotals"), ensure_ascii=False))
        print(f"{'hour':6s} {'cost':>8s} {'share%':>7s} {'orderSum':>9s} {'ROI':>6s} {'CPC':>5s}")
        for w in (out.get("hourTable") or []):
            print(f"{w['hour']:6s} {w['cost']:8.2f} {w['share']:7.1f} {w['orderSum']:9.2f} "
                  f"{w['roi']:6.2f} {w['cpc']:5.2f}")
        print("problemHours:", json.dumps(out.get("problemHours"), ensure_ascii=False))
        print("->", OUT)
        ctx.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
