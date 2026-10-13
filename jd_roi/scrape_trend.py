# -*- coding: utf-8 -*-
"""京准通「分时趋势」抓取（列表行「趋势」弹层背后的数据）。

为什么要它：`jzt_campaign` 只有「按计划·按天」，**没有任何小时级数据**，
而同一计划在不同时段的 ROI 差异远大于计划之间的日差异 —— 这是分时折扣（dayparting）
唯一的决策依据。

接口（在已登录的 jzt.jd.com 页面上下文内 fetch，自动带 Cookie）：
    POST https://atoms-api.jd.com/reweb/common/index/trendchart          账号级
    POST https://atoms-api.jd.com/reweb/common/index/material/trendchart 计划级（必传 campaignIds）
    granularity: 1=天 2=周 3=月 4=小时；**4 只在单日内有效**，跨天会静默降级为按天

风控策略（用户明确要求「不要频繁调用」）：
    * **只在夜间抓取时跑一次**，只抓**昨天那一天**（由 scrape_day 保证）
    * 计划级只取**花费前 N 个**计划（默认 20，`JD_HOURLY_PLANS`），不是全量计划
    * 每个请求之间 sleep（`JD_HOURLY_SLEEP`，默认 0.4s）
    * 已抓过的天直接跳过（`dailystore.has_day`），不会重复请求
    * 单个计划失败**只跳过该计划**，不影响整天

用法:
    .venv\\Scripts\\python.exe -m jd_roi.scrape_trend --account=main 2026-10-11
    .venv\\Scripts\\python.exe -m jd_roi.scrape_trend --account=b --list 2026-10-11
"""
from __future__ import annotations

import json
import sys
import time

from . import config, dailystore as ds, settings
from .browser import launch_profile

ACC_TREND = "https://atoms-api.jd.com/reweb/common/index/trendchart"
MAT_TREND = "https://atoms-api.jd.com/reweb/common/index/material/trendchart"

GRAN_HOUR = 4          # 4=小时（1=天 2=周 3=月）

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _payload(day: str, granularity: int = GRAN_HOUR, campaign_ids=None) -> dict:
    p = {
        "startDay": f"{day} 00:00:00",
        "endDay": f"{day} 23:59:59",
        "businessType": -4,
        "granularity": granularity,
        "mediaResourceType": None,
        "interactiveType": None,
        "isMsa": 1,
        "type": 1,
        "copyWriting": "广告点击15天内累计的成交订单",
        "clickOrOrderDay": 15,
        "giftFlag": 0,
        "clickOrOrderCaliber": 0,
        "orderStatusCategory": 1,
        "deliverySystemType": 0,
    }
    if campaign_ids is not None:
        p["campaignIds"] = list(campaign_ids)
    return p


def _post(page, url: str, payload: dict, retries: int = 4) -> dict:
    """带 -3010（操作次数超限）退避的 POST。"""
    last = None
    for _ in range(max(retries, 1)):
        res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
        if int(res.get("status") or 0) == 200:
            try:
                d = json.loads(res.get("text") or "{}")
            except Exception:  # noqa: BLE001
                d = {"code": -1, "msg": (res.get("text") or "")[:200]}
            code = d.get("code")
            if code == -3010 or (isinstance(code, int) and code < 0 and "上限" in str(d.get("msg"))):
                last = d
                time.sleep(settings.RETRY_SLEEP)
                continue
            return d
        last = {"code": res.get("status"), "msg": (res.get("text") or "")[:200]}
        time.sleep(3)
    raise RuntimeError(f"分时接口失败 {url}: {json.dumps(last, ensure_ascii=False)[:200]}")


def _hour_of(row: dict):
    """从 date/clickDate 里抠出小时（'20261011050000~...' -> 5）。"""
    for key in ("clickDate", "date"):
        s = str(row.get(key) or "")
        digits = s.split("~")[0]
        if len(digits) >= 10 and digits[:10].isdigit():
            try:
                return int(digits[8:10])
            except ValueError:
                continue
    return None


def _norm(rows: list, level: str, campaign_id=0, campaign_name=None) -> list:
    """统一成入库行：只留原始可加指标（派生指标由消费方算，避免聚合时算错）。"""
    out = []
    for r in rows or []:
        h = _hour_of(r)
        if h is None:
            continue
        out.append({
            "level": level,
            "campaignId": campaign_id or 0,
            "campaignName": campaign_name,
            "hour": h,
            "cost": round(_f(r.get("cost")), 2),
            "totalOrderSum": round(_f(r.get("totalOrderSum")), 2),
            "totalOrderCnt": int(_f(r.get("totalOrderCnt"))),
            "clicks": int(_f(r.get("clicks"))),
            "impressions": int(_f(r.get("impressions"))),
        })
    out.sort(key=lambda x: x["hour"])
    return out


def _f(v, d=0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def top_plans(rows: list, n: int) -> list:
    """按当天花费取前 N 个计划（只留 id/名称，避免带一堆字段）。"""
    items = []
    for r in rows or []:
        cid = r.get("campaignId")
        if not cid:
            continue
        items.append({"campaignId": cid,
                      "campaignName": r.get("campaignName"),
                      "cost": _f(r.get("cost"))})
    items.sort(key=lambda x: -x["cost"])
    return items[:max(int(n), 0)]


def fetch_hourly_day(page, day: str, plan_rows: list | None = None,
                     max_plans: int | None = None, log=lambda m: None) -> dict:
    """抓一天的分时数据：账号级 1 次 + 每个高花费计划 1 次。

    plan_rows: 当天的 jzt_campaign 行（用于挑高花费计划）。为空则只抓账号级。
    """
    n = settings.HOURLY_PLANS if max_plans is None else max_plans
    rows: list = []

    acc = _post(page, ACC_TREND, _payload(day))
    if acc.get("code") != 1:
        raise RuntimeError(f"账号级分时失败: {json.dumps(acc, ensure_ascii=False)[:200]}")
    a_rows = _norm(acc.get("data") or [], "account")
    rows += a_rows
    log(f"分时[{day}] 账号级 {len(a_rows)} 个小时点")
    time.sleep(settings.HOURLY_SLEEP)

    plans = top_plans(plan_rows or [], n)
    ok, failed = 0, []
    for p in plans:
        try:
            d = _post(page, MAT_TREND, _payload(day, campaign_ids=[p["campaignId"]]))
            if d.get("code") != 1:
                failed.append(f"{p['campaignId']}:{str(d.get('msg'))[:40]}")
                continue
            pr = _norm(d.get("data") or [], "plan", p["campaignId"], p["campaignName"])
            if pr:
                rows += pr
                ok += 1
        except Exception as exc:  # noqa: BLE001  单个计划失败不影响整天
            failed.append(f"{p['campaignId']}:{type(exc).__name__}")
        time.sleep(settings.HOURLY_SLEEP)
    log(f"分时[{day}] 计划级 {ok}/{len(plans)} 个成功"
        + (f"，失败 {len(failed)}" if failed else ""))
    return {"rows": rows, "dateStart": day, "dateEnd": day,
            "granularity": GRAN_HOUR, "accountRows": len(a_rows),
            "fetchedAt": settings.now_iso(),
            "plans": [p["campaignId"] for p in plans][:ok] if plans else [],
            "failed": failed}


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    only_list = "--list" in args
    days = [a for a in args if not a.startswith("--")]
    day = days[0] if days else (ds.today() - __import__("datetime").timedelta(days=1)).isoformat()
    day = ds.norm_day(day)
    acc = account or settings.MAIN_ACCOUNT

    if only_list:
        w = ds.load_day(acc, day, "jzt_campaign") or {}
        for p in top_plans(w.get("rows"), settings.HOURLY_PLANS):
            print(f"  {p['campaignId']}  ¥{p['cost']:>9.2f}  {p['campaignName']}")
        return 0

    if ds.has_day(acc, day, "hourly"):
        print(f"[skip] {acc} {day} 分时已存在（要重抓加 --force）")
        if "--force" not in args:
            return 0
    pw, ctx = launch_profile(headless=True, account=acc)
    page = ctx.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        plan_rows = (ds.load_day(acc, day, "jzt_campaign") or {}).get("rows") or []
        if not plan_rows:
            print(f"[warn] {acc} {day} 没有 jzt_campaign 数据，只抓账号级分时")
        res = fetch_hourly_day(page, day, plan_rows, log=lambda m: print(m, flush=True))
        ds.save_day(acc, day, "hourly", res)
        plan_rows_n = len([r for r in res["rows"] if r["level"] == "plan"])
        print(f"[OK] {acc} {day} 分时 {len(res['rows'])} 行"
              f"（账号 {res['accountRows']} + 计划 {plan_rows_n}）")
        for h in [r for r in res["rows"] if r["level"] == "account"]:
            print(f"   {h['hour']:02d}:00  花费 ¥{h['cost']:>8.2f}  "
                  f"成交 ¥{h['totalOrderSum']:>9.2f}  "
                  f"ROI {h['totalOrderSum'] / h['cost'] if h['cost'] else 0:>5.2f}")
        return 0
    finally:
        ctx.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
