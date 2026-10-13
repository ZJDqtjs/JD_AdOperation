# -*- coding: utf-8 -*-
"""按计划拉分时曲线：找出「哪个计划在哪个时段在烧钱」。

计划清单直接取本地按天库（jzt_campaign），避免再解析列表接口。
产出 data/explore/trend_plan_hourly.json

用法: .venv\\Scripts\\python.exe -m jd_roi.probe_trend5 --account=main --day=2026-10-11
"""
from __future__ import annotations

import json
import sys

from . import config, dailystore
from .browser import launch_profile

OUT = config.DATA_DIR / "explore" / "trend_plan_hourly.json"
MAT = "https://atoms-api.jd.com/reweb/common/index/material/trendchart"
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
    catch(e) { return {status: r.status, text: t.slice(0, 400)}; }
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
    # 1) 本地按天库取当日计划（按花费排序）
    w = dailystore.load_window(account or "main", "jzt_campaign", day, day)
    rows = (w or {}).get("rows") or []
    plans = []
    for r in rows:
        c = f(r.get("cost"))
        if c <= 0:
            continue
        os_ = f(r.get("totalOrderSum"))
        plans.append({"campaignId": r.get("campaignId"), "name": r.get("campaignName"),
                      "type": r.get("campaignType"), "cost": round(c, 2),
                      "orderSum": round(os_, 2),
                      "roi": round(os_ / c, 2) if c else 0,
                      "cnt": int(f(r.get("totalOrderCnt"))),
                      "clicks": int(f(r.get("clicks")))})
    plans.sort(key=lambda x: -x["cost"])
    top = plans[:8]
    print(f"本地计划数(当日有效花费)={len(plans)}，取前 {len(top)}")

    out: dict = {"account": account, "day": day, "plans": plans, "hourly": {}}
    pw, ctx = launch_profile(headless=True, account=account)
    page = ctx.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(13000)
        for p in top:
            r = page.evaluate(JS, {"url": MAT, "payload": dict(
                BASE, granularity=4, campaignIds=[p["campaignId"]],
                startDay=f"{day} 00:00:00", endDay=f"{day} 23:59:59")})
            j = (r or {}).get("json") or {}
            hrs = j.get("data") or []
            out["hourly"][str(p["campaignId"])] = {
                "name": p["name"], "dayCost": p["cost"], "dayRoi": p["roi"],
                "rows": [{"h": str(h.get("clickDate"))[8:10],
                          "cost": round(f(h.get("cost")), 2),
                          "orderSum": round(f(h.get("totalOrderSum")), 2),
                          "roi": round(f(h.get("totalOrderROI")), 2),
                          "cpc": round(f(h.get("CPC")), 2),
                          "clicks": int(f(h.get("clicks")))} for h in hrs],
            }
            page.wait_for_timeout(350)
        # 单计划趋势图还能带「昨日/今日对比」？顺带验证 granularity=4 跨天会降级
        r = page.evaluate(JS, {"url": MAT, "payload": dict(
            BASE, granularity=4, campaignIds=[top[0]["campaignId"]] if top else [],
            startDay=f"{day} 00:00:00", endDay=f"{day} 23:59:59")})
        out["_verify"] = {"code": ((r or {}).get("json") or {}).get("code")}

        # 2) 每个计划的「问题时段」
        prob = []
        for cid, v in out["hourly"].items():
            tot = sum(h["cost"] for h in v["rows"])
            if tot <= 0:
                continue
            for h in v["rows"]:
                if h["cost"] >= 15 and h["roi"] < v["dayRoi"] * 0.5:
                    prob.append({"plan": v["name"], "campaignId": cid, "hour": h["h"] + ":00",
                                 "cost": h["cost"], "roi": h["roi"],
                                 "planRoi": v["dayRoi"], "cpc": h["cpc"]})
        prob.sort(key=lambda x: -x["cost"])
        out["problemSlots"] = prob[:40]
    finally:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{'计划':22s} {'日花费':>8s} {'日ROI':>6s}  最差3个时段(时:花费/ROI)")
        for cid, v in out["hourly"].items():
            hs = sorted([h for h in v["rows"] if h["cost"] > 0], key=lambda x: x["roi"])[:3]
            txt = "  ".join(f"{h['h']}:{h['cost']:.0f}/{h['roi']:.1f}" for h in hs)
            print(f"{(v['name'] or '')[:20]:22s} {v['dayCost']:8.1f} {v['dayRoi']:6.2f}  {txt}")
        print()
        print("问题时段 top10:")
        for p in (out.get("problemSlots") or [])[:10]:
            print(f"  {p['hour']}  {(p['plan'] or '')[:16]:18s} 花费{p['cost']:7.2f} "
                  f"ROI{p['roi']:5.2f}(日{p['planRoi']:.2f}) CPC{p['cpc']:.2f}")
        print("->", OUT)
        ctx.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
