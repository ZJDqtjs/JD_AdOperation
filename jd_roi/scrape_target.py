# -*- coding: utf-8 -*-
"""定向类报表抓取：人群（DMP 人群包）与 地域（自定义报表-地域计划组合报表）。

两个接口都在 jzt-api.jd.com 上，但归属不同页面，referer 不同：
  人群  POST /reweb/msa/orientation/crowd/list           （referer: /report/index.html/）
  地域  POST /dataCenter/customreport/v2/report/queryData （referer: /custom-report/）

设计：**一次区间查询 → 按天拆分**（两个接口都支持按天返回），
避免每天一次请求；抓取侧只负责产出 {day: rows}，落盘由 scrape_day 负责。

用法：
    python -m jd_roi.scrape_target crowd --account=b 2026-10-03 2026-10-11
    python -m jd_roi.scrape_target area  --account=b 2026-10-03 2026-10-11
    python -m jd_roi.scrape_target metainfo --account=b      # 指标字典
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import sys
import time
from pathlib import Path

from . import config
from .browser import launch_profile

CROWD_API = "https://jzt-api.jd.com/reweb/msa/orientation/crowd/list"
METINFO_API = "https://jzt-api.jd.com/reweb/common/config/metainfo"
AREA_API = ("https://jzt-api.jd.com/dataCenter/customreport/v2/report/queryData"
            "?requestFrom=0&businessFrom=1")
AREA_TEMPLATE = Path(config.BASE_DIR) / "config" / "report_templates" / "area_41.json"

CROWD_PAGE = "https://jzt.jd.com/report/index.html/#/rtb/basic/crowd?hideNav=1"
AREA_PAGE = "https://jzt.jd.com/custom-report/#/create?specificType=41"

CROWD_COLUMNS = [
    "impressions", "clicks", "CTR", "cost", "CPM", "CPC",
    "directOrderCnt", "directOrderSum", "indirectOrderCnt", "indirectOrderSum",
    "totalOrderCnt", "totalOrderSum", "totalOrderROI", "totalCartCnt", "totalCartRate",
    "newCustomersCnt", "newCustomersOrderCnt", "newCustomersGMV",
    "dealUserCnt", "dealUserCost", "shopAttentionCnt", "goodsAttentionCnt",
    "visitorCnt", "depthPassengerCnt", "visitTimeAverage",
]

_JS = """
async ({url, payload, referer}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':referer,'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _post(page, url: str, payload: dict, referer: str) -> dict:
    res = page.evaluate(_JS, {"url": url, "payload": payload, "referer": referer})
    if res["status"] != 200:
        raise RuntimeError(f"接口HTTP {res['status']}: {res['text'][:300]}")
    return json.loads(res["text"])


_JS_GET = """
async ({url, referer}) => {
  const r = await fetch(url, {credentials:'include',
    headers:{'accept':'application/json, text/plain, */*','referer':referer,
      'language':'zh_CN','siteid':'0','loginmode':'0'}});
  return {status: r.status, text: await r.text()};
}
"""


def _get(page, url: str, referer: str) -> dict:
    res = page.evaluate(_JS_GET, {"url": url, "referer": referer})
    if res["status"] != 200:
        raise RuntimeError(f"接口HTTP {res['status']}: {res['text'][:300]}")
    return json.loads(res["text"])


def fetch_pin_name(page, account_id: int) -> str | None:
    """查当前登录主体下的账户名（地域报表模板里必须带上正确的 pin）。"""
    d = _get(page, "https://jzt-api.jd.com/dataCenter/index/user/pinlist"
                    "?requestFrom=0&businessFrom=1", "https://jzt.jd.com/custom-report/")
    if d.get("code") != 1:
        return None
    body = d.get("data") or {}
    for grp in ("own", "value"):
        for it in body.get(grp) or []:
            if str(it.get("userId")) == str(account_id):
                return it.get("pin") or it.get("userDisplayName")
    return None


def _norm_day(v) -> str | None:
    """'20261006' / '2026-10-06' / '20261005~20261011' -> 'YYYY-MM-DD' or None。"""
    s = str(v or "").strip()
    if "~" in s or len(s) != 8 or not s.isdigit():
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


# ---------------------------------------------------------------- 人群
def fetch_crowd_days(page, start: str, end: str, account_id: int) -> dict:
    """人群报表按天：返回 {day: [row, ...]}。"""
    by_day: dict = {}
    no = 1
    while True:
        d = _post(page, CROWD_API, {
            "isDaily": True, "startDay": start, "endDay": end,
            "clickOrOrderCaliber": 0, "clickOrOrderDay": 15, "giftFlag": 0,
            "orderStatusCategory": 1, "filters": [], "pinIds": [account_id] if account_id else [],
            "columns": CROWD_COLUMNS, "obys": "cost|desc",
            "page": no, "pageSize": 200, "needGdEffectOrder": False,
        }, "https://jzt.jd.com/report/index.html/")
        if d.get("code") != 1:
            raise RuntimeError(f"crowd: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        rows = body.get("datas") or body.get("data") or []
        for r in rows:
            day = _norm_day(r.get("date")) or _norm_day(r.get("clickDate"))
            if day:
                by_day.setdefault(day, []).append(r)
        if len(rows) < 200 or no > 40:
            break
        no += 1
    return by_day


# ---------------------------------------------------------------- 指标字典
def fetch_metainfo(page) -> dict:
    d = _post(page, METINFO_API, {}, "https://jzt.jd.com/report/index.html/")
    if d.get("code") != 1:
        raise RuntimeError(f"metainfo: {json.dumps(d, ensure_ascii=False)[:200]}")
    return d.get("data") or {}


# ---------------------------------------------------------------- 地域
def _ms(day: str) -> int:
    return calendar.timegm(dt.date.fromisoformat(day).timetuple()) * 1000


def fetch_area_days(page, start: str, end: str, *, account_id: int | None = None,
                    max_pages: int = 60, page_size: int = 200, sleep: float = 0.8) -> dict:
    """地域报表按天：返回 {day: [row, ...]}。

    模板 41 的维度已在 config/report_templates/area_41.json 里收窄为「只投放地域」，
    行数从 ~480/天 降到 ~34/天，翻页只需几页；仍按天过滤以确保只留目标区间。

    account_id：必须传！模板里的 pin 是抓包时那个账号，不同账号数据不同，
    这里按 pinlist 把 pin 换成目标账号。
    """
    tpl = json.loads(AREA_TEMPLATE.read_text(encoding="utf-8"))
    tpl["startTime"] = _ms(start)
    tpl["endTime"] = _ms(end) + 86_399_000
    if account_id:
        pin = fetch_pin_name(page, account_id) or str(account_id)
        for grp in tpl.get("customDimensionOptions") or []:
            if grp.get("key") != "pin":
                continue
            for sub in grp.get("options") or []:
                if sub.get("key") != "subUser":
                    continue
                sub["options"] = [{"checked": True, "desc": pin, "flag": True,
                                   "hidden": False, "key": str(account_id), "value": pin}]
    by_day: dict = {}
    no, seen, total = 1, 0, None
    while no <= max_pages:
        payload = dict(tpl)
        payload["page"] = no
        payload["pageSize"] = page_size
        d = None
        for attempt in range(3):   # -3010 = 操作次数超限，等一分钟再来
            d = _post(page, AREA_API, payload, "https://jzt.jd.com/custom-report/")
            if d.get("code") == 1:
                break
            if d.get("code") == -3010 and attempt < 2:
                time.sleep(62)
                continue
            raise RuntimeError(f"area: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = (d or {}).get("data") or {}
        rep = body.get("reportData") or []
        pg = body.get("paginator") or {}
        total = pg.get("items")
        if not rep:
            break
        page_min = min(str(r.get("date") or "9999")[:10] for r in rep)
        for r in rep:
            day = str(r.get("date") or "")[:10]
            if start <= day <= end:
                by_day.setdefault(day, []).append(r)
        seen += len(rep)
        if page_min < start or len(rep) < page_size:
            break
        if total and seen >= total:
            break
        no += 1
        time.sleep(sleep)
    return by_day


# ---------------------------------------------------------------- CLI
def main() -> int:
    argv = sys.argv[1:]
    what = argv[0] if argv and not argv[0].startswith("-") else "crowd"
    account, rest = config.parse_account(argv[1:])
    start = rest[0] if len(rest) > 0 else config.DATE_START
    end = rest[1] if len(rest) > 1 else config.DATE_END
    aid = config.get_account(account).get("account_id") or 0

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        if what in ("area", "custom"):
            page.goto(AREA_PAGE, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(15000)
            by_day = fetch_area_days(page, start, end, account_id=aid)
            print(f"[area] {start}~{end} 天数={len(by_day)} 行数={sum(len(v) for v in by_day.values())}")
            for day in sorted(by_day)[:3]:
                print(f"  {day}: {len(by_day[day])} 行  例={json.dumps(by_day[day][0], ensure_ascii=False)[:200]}")
        else:
            page.goto("https://jzt.jd.com/report/index.html/#/overview/page",
                      wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(12000)
            page.goto(CROWD_PAGE, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(10000)
            if what == "metainfo":
                m = fetch_metainfo(page)
                out = Path(config.DATA_DIR) / "metainfo.json"
                out.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"[metainfo] {len(m)} 个指标 -> {out}")
            else:
                by_day = fetch_crowd_days(page, start, end, aid)
                print(f"[crowd] {start}~{end} 天数={len(by_day)} 行数={sum(len(v) for v in by_day.values())}")
                for day in sorted(by_day)[:3]:
                    for r in by_day[day][:3]:
                        print(f"  {day} {r.get('dmpName')} | {str(r.get('campaignName'))[:18]} "
                              f"花费={r.get('cost')} ROI={r.get('totalOrderROI')} 新客={r.get('newCustomersCnt')}")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
