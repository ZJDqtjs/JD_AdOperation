# -*- coding: utf-8 -*-
"""京准通「操作日志」（调整操作记录）抓取。

接口（在已登录 jzt.jd.com 页面内 fetch，自动带 Cookie）:
  POST https://jzt-api.jd.com/logplatform/common/business/info   业务类型/层级枚举
  POST https://jzt-api.jd.com/logplatform/common/list/query      操作日志分页查询
  POST https://jzt-api.jd.com/logplatform/common/list/opt        操作类型下拉选项

**重要**：list/query 的 beginTime~endTime 跨度过大时服务端会静默截断
（实测 8/15~10/05 只返回 8 月记录）。因此必须**按小窗口（默认 7 天）分段查询再合并**。

用法:
    python -m jd_roi.scrape_oplog info
    python -m jd_roi.scrape_oplog sweep 2026-08-15 2026-10-05 --account=main
    python -m jd_roi.scrape_oplog sweep 2026-08-15 2026-10-05 --account=b
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time

from . import config, store
from .browser import launch_profile

BASE = "https://jzt-api.jd.com/logplatform"
URL_INFO = f"{BASE}/common/business/info"
URL_QUERY = f"{BASE}/common/list/query"
URL_OPT = f"{BASE}/common/list/opt"
LOG_URL = "https://jzt.jd.com/logging/#/business?businessType=-16"

KC = -16                                  # 快车（含智能投放）
LEVELS = ["campaign", "adgroup", "ad", "keyword"]

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type':'application/json',
      'accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/logging/',
      'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)
  });
  return {status: r.status, text: await r.text()};
}
"""


def _post(page, url, payload):
    res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
    if res["status"] != 200:
        raise RuntimeError(f"HTTP {res['status']}: {res['text'][:300]}")
    return json.loads(res["text"])


def _query_page(page, btype, level, begin, end, page_index, page_size=100):
    payload = {"btype": btype, "pageSize": page_size, "pageIndex": page_index,
               "type": level, "beginTime": begin, "endTime": end,
               "keywordId": "", "searchType": 1, "operator": "", "optContent": ""}
    d = _post(page, URL_QUERY, payload)
    if d.get("code") != 1:
        raise RuntimeError(f"接口异常: {json.dumps(d, ensure_ascii=False)[:300]}")
    body = d.get("data") or {}
    return body.get("data") or [], body.get("count") or 0


def query_window(page, btype, level, begin, end, max_pages=40):
    rows, page_no = [], 1
    while page_no <= max_pages:
        batch, total = _query_page(page, btype, level, begin, end, page_no)
        rows.extend(batch)
        if len(batch) < 100 or len(rows) >= total:
            break
        page_no += 1
        time.sleep(0.2)
    return rows


def _days(a: str, b: str):
    d0 = dt.date.fromisoformat(a)
    d1 = dt.date.fromisoformat(b)
    cur = d0
    while cur <= d1:
        yield cur
        cur += dt.timedelta(days=1)


def key_of(r):
    return "|".join(str(r.get(k)) for k in
                    ("optTime", "actionObjectId", "operationContent", "operationDetails", "operator"))


def sweep(page, start, end, levels, window_days=7, btype=KC, verbose=True):
    seen, out = set(), []
    win_start = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    while win_start <= end_d:
        win_end = min(win_start + dt.timedelta(days=window_days - 1), end_d)
        for lvl in levels:
            try:
                rows = query_window(page, btype, lvl, win_start.isoformat(), win_end.isoformat())
            except Exception as exc:  # noqa: BLE001
                print(f"   ! {lvl} {win_start}~{win_end} 失败: {exc}")
                continue
            add = 0
            for r in rows:
                k = key_of(r)
                if k in seen:
                    continue
                seen.add(k)
                r["_level"] = lvl
                r["_btype"] = btype
                out.append(r)
                add += 1
            if verbose and (rows or add):
                print(f"   {win_start}~{win_end} {lvl}: {len(rows)} 条(新增 {add})")
        win_start = win_end + dt.timedelta(days=1)
    out.sort(key=lambda r: str(r.get("optTime")))
    return out


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    mode = args[0] if args else "sweep"
    begin = args[1] if len(args) > 1 else "2026-08-15"
    end = args[2] if len(args) > 2 else "2026-10-05"

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        page.goto(LOG_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(9000)

        if mode == "info":
            info = _post(page, URL_INFO, {})
            print(json.dumps(info, ensure_ascii=False, indent=1)[:4000])
            for ot in (1, 2, 3):
                print(f"opt{ot}:", json.dumps(_post(page, URL_OPT, {
                    "beginTime": begin, "endTime": end, "btype": KC, "type": "campaign",
                    "optType": ot}).get("data"), ensure_ascii=False))
            return 0

        rows = sweep(page, begin, end, LEVELS)
        result = {"account": account or config.MAIN_ACCOUNT, "begin": begin, "end": end,
                  "btype": KC, "rows": rows}
        store.save("oplog", result, account)
        print(f"[OK] {begin}~{end} 共 {len(rows)} 条操作记录 -> {store._path('oplog', account)}")
        by_lvl, by_day = {}, {}
        for r in rows:
            by_lvl[r["_level"]] = by_lvl.get(r["_level"], 0) + 1
            by_day[r["optTime"][:10]] = by_day.get(r["optTime"][:10], 0) + 1
        print("  层级:", by_lvl)
        print("  按日:", " ".join(f"{d[5:]}:{n}" for d, n in sorted(by_day.items())))
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
