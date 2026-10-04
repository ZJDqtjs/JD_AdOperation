# -*- coding: utf-8 -*-
"""一趟浏览器会话内，按多个时间窗抓齐一个账号的全部报表（可断点续抓）。

覆盖：
  京准通 概览-计划 (atoms-api)
  智能投放 计划/商品/搜索词 (jzt-api)   ← 搜索词按 cost 降序取前 N 行，避免触发 -3010 限流
  报表中心 快车-关键词 (jzt-api)
  商智 商品明细/流量概况 (szgateway)

用法:
    python -m jd_roi.scrape_all --account=b w1=2026-09-21:2026-09-27 w2=2026-09-28:2026-10-04
    python -m jd_roi.scrape_all --account=b --force ...      # 强制重抓
"""
from __future__ import annotations

import json
import sys
import time

from . import config, store
from .browser import launch_profile
from .scrape_jst_report import API as JST_API, COLS_ACCOUNT, COLS_SW
from .scrape_jzt import CUSTOM_COLUMNS, CAMPAIGN_API
from .scrape_keyword import COLUMNS as KW_COLS, KEYWORD_API
from .scrape_sz import INDICATORS, SzSession, URL_CORE, URL_FLOWSRC, URL_PRODUCT, _base

SW_MAX_ROWS = 2000        # 搜索词只取花费最高的前 N 行（覆盖 ~99.9% 花费）
PAGE_SLEEP = 0.35         # 分页间隔，规避 -3010
RETRY_SLEEP = 22          # 命中限流后的等待秒数

_JZT_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""
_JST_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/jst/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""
_KW_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _post(page, js, url, payload, retries=5):
    """带限流重试的 POST。"""
    last = None
    for attempt in range(retries):
        res = page.evaluate(js, {"url": url, "payload": payload})
        if res["status"] == 200:
            try:
                d = json.loads(res["text"])
            except Exception:  # noqa: BLE001
                d = {"code": -1, "msg": res["text"][:200]}
            code = d.get("code")
            if code in (-3010,) or (isinstance(code, int) and code < 0 and "上限" in str(d.get("msg"))):
                print(f"    [限流] 等待 {RETRY_SLEEP}s 重试 ({attempt + 1}/{retries})", flush=True)
                time.sleep(RETRY_SLEEP)
                last = d
                continue
            return d
        last = {"code": res["status"], "msg": res["text"][:200]}
        time.sleep(3)
    raise RuntimeError(f"请求失败 {url}: {json.dumps(last, ensure_ascii=False)[:250]}")


def fetch_jzt(page, start, end, page_size=50):
    rows, ext, total, no = [], {}, None, 1
    while True:
        d = _post(page, _JZT_FETCH, CAMPAIGN_API, {
            "page": no, "pageSize": page_size, "status": "", "filters": [], "obys": "",
            "startDay": start, "endDay": end, "clickOrOrderCaliber": 0, "clickOrOrderDay": 15,
            "giftFlag": 0, "orderStatusCategory": 1, "customColumns": CUSTOM_COLUMNS, "requestFrom": 0})
        if d.get("code") != 1:
            raise RuntimeError(f"jzt: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        if not ext:
            ext = body.get("ext") or {}
        b = body.get("data") or []
        rows += b
        total = total or body.get("totalCount") or body.get("total") or len(b)
        if len(b) < page_size or no > 40:
            break
        no += 1
        time.sleep(PAGE_SLEEP)
    return {"ext": ext, "rows": rows, "total": total}


def fetch_jst(page, kind, start, end, page_size=200):
    cols = COLS_SW if kind == "searchword" else COLS_ACCOUNT
    obys = "cost|desc" if kind == "searchword" else "impressions|desc"
    max_rows = SW_MAX_ROWS if kind == "searchword" else 10 ** 9
    rows, ext, no, paginator = [], {}, 1, {}
    while True:
        d = _post(page, _JST_FETCH, JST_API[kind], {
            "isDaily": False, "startDay": start, "endDay": end,
            "obys": obys, "filters": [],
            "clickOrOrderDay": 15, "clickOrOrderCaliber": 0,
            "orderStatusCategory": 1 if kind == "order" else None, "page": no, "pageSize": page_size,
            "giftFlag": 0, "promotionMode": None, "columns": cols, "requestFrom": 0})
        if str(d.get("code")) != "1":
            raise RuntimeError(f"jst {kind}: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        if not ext:
            ext = body.get("ext") or {}
        paginator = body.get("paginator") or paginator
        b = body.get("datas") or []
        rows += b
        if len(b) < page_size or len(rows) >= max_rows or no > 60:
            break
        no += 1
        time.sleep(PAGE_SLEEP)
    return {"ext": ext, "rows": rows, "total": paginator.get("items", len(rows)),
            "capped": len(rows) >= max_rows, "obys": obys}


def fetch_kw(page, start, end, aid, page_size=200):
    rows, ext, no, total = [], {}, 1, None
    while True:
        d = _post(page, _KW_FETCH, KEYWORD_API, {
            "isDaily": False, "startDay": start, "endDay": end, "clickOrOrderCaliber": 0,
            "clickOrOrderDay": 30, "giftFlag": 0, "orderStatusCategory": 1, "filters": [],
            "pinIds": [aid], "targetingType": "", "columns": KW_COLS, "obys": "impressions|desc",
            "page": no, "pageSize": page_size, "needGdEffectOrder": False})
        if d.get("code") != 1:
            raise RuntimeError(f"kw: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        if not ext:
            ext = {k: v for k, v in body.items() if k != "datas"}
        b = body.get("datas") or []
        rows += b
        total = total or body.get("total")
        if len(b) < page_size or no > 40:
            break
        no += 1
        time.sleep(PAGE_SLEEP)
    return {"ext": ext, "rows": rows, "total": total}


def open_sz(context, kind, wait_ms=14000):
    """新开一个商智页面并捕获当前有效的动态签名头。"""
    page = context.new_page()
    s = SzSession(page, kind)
    s.capture(wait_ms=wait_ms)
    return s


def _prev_window(start, end):
    import datetime as dt
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    n = (e - s).days + 1
    return (s - dt.timedelta(days=n)).isoformat(), (s - dt.timedelta(days=1)).isoformat()


def main() -> int:
    account, args = config.parse_account(sys.argv[1:])
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    specs = []
    for a in args:
        if "=" in a and ":" in a:
            tag, rng = a.split("=", 1)
            s, e = rng.split(":", 1)
            specs.append((tag, s, e))
    if not specs:
        specs = [("w0", "2026-09-13", "2026-09-19"),
                 ("w1", "2026-09-20", "2026-09-26"),
                 ("w2", "2026-09-27", "2026-10-03")]
    aid = config.get_account(account).get("account_id") or config.ACCOUNT_ID

    def todo(name):
        return force or not store.exists(name, account)

    pw, context = launch_profile(headless=True, account=account)
    p_jzt = context.new_page()
    p_kw = context.new_page()
    try:
        need_jzt = any(todo(f"jzt_campaign_{t}") or todo(f"jst_campaign_{t}") or
                       todo(f"jst_sku_{t}") or todo(f"jst_searchword_{t}") for t, _, _ in specs)
        need_kw = any(todo(f"kw_{t}") for t, _, _ in specs)
        need_sz = any(todo(f"sz_product_{t}") or todo(f"sz_flow_{t}") for t, _, _ in specs)

        if need_jzt:
            p_jzt.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
            p_jzt.wait_for_timeout(9000)
        if need_kw:
            p_kw.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
            p_kw.wait_for_timeout(8000)
            p_kw.goto(config.REPORT_KEYWORD, wait_until="domcontentloaded", timeout=90000)
            p_kw.wait_for_timeout(8000)

        for tag, start, end in specs:
            print(f"===== [{tag}] {start} ~ {end} =====", flush=True)
            if todo(f"jzt_campaign_{tag}"):
                d = fetch_jzt(p_jzt, start, end)
                d.update({"dateStart": start, "dateEnd": end, "account": account or config.MAIN_ACCOUNT})
                store.save(f"jzt_campaign_{tag}", d, account)
                print(f"  jzt 计划 {len(d['rows'])}", flush=True)
            else:
                print("  jzt 计划 (skip)", flush=True)

            for kind in ("campaign", "sku", "searchword"):
                name = f"jst_{kind}_{tag}"
                if not todo(name):
                    print(f"  jst {kind} (skip)", flush=True)
                    continue
                r = fetch_jst(p_jzt, kind, start, end)
                r.update({"dateStart": start, "dateEnd": end})
                store.save(name, r, account)
                print(f"  jst {kind} {len(r['rows'])} (total={r['total']} capped={r['capped']})", flush=True)

            if todo(f"kw_{tag}"):
                k = fetch_kw(p_kw, start, end, aid)
                k.update({"dateStart": start, "dateEnd": end})
                store.save(f"kw_{tag}", k, account)
                print(f"  kw {len(k['rows'])}", flush=True)
            else:
                print("  kw (skip)", flush=True)

            cstart, cend = _prev_window(start, end)
            if todo(f"sz_product_{tag}") or todo(f"sz_flow_{tag}"):
                # 商智的动态签名头存活时间很短，必须每次请求前重新捕获
                s2 = open_sz(context, "product", wait_ms=13000)
                base = _base(start, end, cstart, cend, s2.brands, s2.cates)
                try:
                    if todo(f"sz_product_{tag}"):
                        prod = s2.post(URL_PRODUCT, dict(base, proType="sku", indicators=INDICATORS,
                                                         onlyAttention=False))
                        code = (prod.get("header") or {}).get("code")
                        rows = (prod.get("body") or {}).get("data") or []
                        if code != 0 or not rows:
                            print(f"  ! sz product code={code} rows={len(rows)}，重试一次", flush=True)
                            s2 = open_sz(context, "product", wait_ms=13000)
                            base = _base(start, end, cstart, cend, s2.brands, s2.cates)
                            prod = s2.post(URL_PRODUCT, dict(base, proType="sku", indicators=INDICATORS,
                                                             onlyAttention=False))
                            code = (prod.get("header") or {}).get("code")
                            rows = (prod.get("body") or {}).get("data") or []
                        store.save(f"sz_product_{tag}", {"rows": rows, "dateStart": start,
                                                         "dateEnd": end, "code": code}, account)
                        print(f"  sz product {len(rows)} (code={code})", flush=True)
                    else:
                        print("  sz product (skip)", flush=True)
                    if todo(f"sz_flow_{tag}"):
                        s3 = open_sz(context, "flow", wait_ms=13000)
                        base3 = _base(start, end, cstart, cend, s3.brands, s3.cates)
                        core = s3.post(URL_CORE, base3)
                        src = s3.post(URL_FLOWSRC, dict(
                            base3, referIndicator="jdr_sch_traffic_brow_sku_cnt_jd_unified_attribution_sz",
                            sort="top", attentionType="",
                            groups=["jdr_sch_traffic_cha_last_field_src_rmad_sz_2"],
                            attributes=["jdr_sch_traffic_cha_last_field_src_rmad_sz_2"]))
                        rc = (core.get("header") or {}).get("code")
                        store.save(f"sz_flow_{tag}", {"core": (core.get("body") or {}).get("data") or [],
                                                      "source": (src.get("body") or {}).get("data") or [],
                                                      "rawCode": rc,
                                                      "dateStart": start, "dateEnd": end}, account)
                        print(f"  sz flow core={len((core.get('body') or {}).get('data') or [])} code={rc}", flush=True)
                    else:
                        print("  sz flow (skip)", flush=True)
                finally:
                    try:
                        s2.page.close()
                    except Exception:  # noqa: BLE001
                        pass
            else:
                print("  sz product (skip) / sz flow (skip)", flush=True)
        print("ALL-DONE", flush=True)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
