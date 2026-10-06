# -*- coding: utf-8 -*-
"""真实浏览器 + 打桩京东接口：把 scrape_day 的网络代码路径全部跑一遍。

和 test_pipeline_e2e 的区别：那边把 fetch_* 整个换掉，只验证编排；
这边**保留真实的 fetch 代码**（page.evaluate + 真 fetch + 分页 + 限流退避 + isDaily 拆分），
只在 Playwright 的 route 层把京东域名拦下来返回构造好的 JSON。

覆盖：
  - 分页：搜索词 200/页，第一页满、第二页不满时正确停止并合并
  - 限流：第一次返回 -3010，退避后重试成功
  - isDaily：智能投放计划/商品一次请求拿多天，按 date 正确拆分
  - 商智：真实 SzSession.capture（页面自触发 getDims + 带签名头的请求）→ 复用头请求网关
  - 登录态：/common/logininfo 打桩为 code=1，session_ok 通过
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_http_", dir=str(_TMPROOT)))

os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_EXCEL_PATH"] = str(TMP / "config" / "missing.xlsx")
os.environ["JD_ACCOUNTS"] = json.dumps([{"key": "main", "label": "主账号", "account_id": 111}])
os.environ["JD_HEADLESS"] = "1"
os.environ["JD_RETRY_SLEEP"] = "1"
os.environ["JD_PAGE_SLEEP"] = "0"
os.environ["JD_SZ_CAPTURE_WAIT"] = "1500"
os.environ["JD_ENABLE_SCHEDULER"] = "0"
os.environ["JD_RUN_ON_START"] = "0"
sys.path.insert(0, str(ROOT))

from jd_roi import dailystore as ds, indicators, scrape_day, settings  # noqa: E402

DAYS = ["2026-10-01", "2026-10-02", "2026-10-03"]
FAILS = []
COUNTERS = {"searchword": 0, "logininfo": 0}


def check(name, got, want):
    ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)


def check_true(name, cond):
    print("  [" + ("PASS" if cond else "FAIL") + "] " + name)
    if not cond:
        FAILS.append(name)


def idx(day):
    return DAYS.index(day) + 1


# ---------------------------------------------------------------- 打桩数据
STUB_PAGE = "<html><body>stub</body></html>"
SZ_STUB = """<html><body><script>
fetch('/api/lowcode/getDims.ajax',{method:'POST',
  headers:{'Content-Type':'application/json'}, body:'{}'});
fetch('/api/lowcode/productDetail/table/productTable.ajax',{method:'POST',
  headers:{'Content-Type':'application/json','user-mnp':'MNP-TOKEN','user-mup':'MUP-TOKEN','uuid':'UUID-1'},
  body:'{}'});
fetch('/api/lowcode/flowSummary/getCoreSummary.ajax',{method:'POST',
  headers:{'Content-Type':'application/json'}, body:'{}'});
fetch('/api/lowcode/flowSummary/productFlow/getFlowSrcTop.ajax',{method:'POST',
  headers:{'Content-Type':'application/json'}, body:'{}'});
</script></body></html>"""


def jst_rows(kind, start, end):
    out = []
    for day in DAYS:
        if not (start <= day <= end):
            continue
        i = idx(day)
        row = {"campaignId": 9001, "campaignName": "计划A", "date": day.replace("-", ""),
               "impressions": 1000 * i, "clicks": 100 * i, "cost": 100.0 * i,
               "totalOrderCnt": 10 * i, "totalOrderSum": 1000.0 * i, "totalCartCnt": 20 * i}
        if kind == "sku":
            row["skuId"] = 100000000001
        out.append(row)
    return out


def sz_product(day):
    i = idx(day)
    return [{"sku_id": "100000000001", "name": "测试商品", "img_src": "", "pro_url": "",
             indicators.SZ_AMT: 5000.0 * i, indicators.SZ_ORDN: 40 * i,
             indicators.SZ_QTY: 40 * i, indicators.SZ_UV: 100 * i,
             indicators.SZ_PV: 200 * i, indicators.SZ_CVR: 0.4},
            {"sku_id": "合计", "$summary": True,
             indicators.SZ_AMT: 5000.0 * i, indicators.SZ_ORDN: 40 * i,
             indicators.SZ_QTY: 40 * i, indicators.SZ_UV: 100 * i,
             indicators.SZ_PV: 200 * i}]


def body_of(req):
    try:
        return json.loads(req.post_data or "{}")
    except Exception:  # noqa: BLE001
        return {}


def api_response(url, payload):
    """按 URL 返回 (code, data)。"""
    if url.endswith("/common/logininfo"):
        COUNTERS["logininfo"] += 1
        return {"code": 1, "data": {"pin": "tester"}}
    if "promolist/overview/campaign" in url:
        day = payload.get("startDay")
        i = idx(day)
        return {"code": 1, "data": {"ext": {"cost": str(100.0 * i)},
                                    "data": [{"campaignId": 9001, "campaignName": "计划A",
                                              "campaignType": 61, "impressions": 1000 * i,
                                              "clicks": 100 * i, "cost": 100.0 * i,
                                              "totalOrderCnt": 10 * i,
                                              "totalOrderSum": 1000.0 * i,
                                              "totalCartCnt": 20 * i}],
                                    "totalCount": 1}}
    if "/reweb/jst/account/" in url:
        kind = "sku" if url.endswith("/sku/list") else "campaign"
        rows = jst_rows(kind, payload.get("startDay"), payload.get("endDay"))
        return {"code": 1, "data": {"ext": {}, "datas": rows,
                                    "paginator": {"items": len(rows)}}}
    if "/reweb/jst/effect/searchword/list" in url:
        COUNTERS["searchword"] += 1
        if COUNTERS["searchword"] == 1:
            return {"code": -3010, "msg": "您的操作次数已超过上限，请一分钟后再进行操作",
                    "success": False}
        if payload.get("page", 1) == 1:
            rows = [{"campaignId": 9001, "campaignName": "计划A", "searchTerm": "词" + str(n),
                     "impressions": 10, "clicks": 1, "cost": 1.0, "totalOrderCnt": 0,
                     "totalOrderSum": 0.0, "totalCartCnt": 0} for n in range(200)]
        else:
            rows = [{"campaignId": 9001, "campaignName": "计划A", "searchTerm": "尾词" + str(n),
                     "impressions": 10, "clicks": 1, "cost": 1.0, "totalOrderCnt": 0,
                     "totalOrderSum": 0.0, "totalCartCnt": 0} for n in range(5)]
        return {"code": 1, "data": {"ext": {}, "datas": rows, "paginator": {"items": 205}}}
    if "/reweb/msa/orientation/keyword/list" in url:
        return {"code": 1, "data": {"ext": {}, "datas": [
            {"campaignId": 9001, "campaignName": "计划A", "groupId": 7, "keywordName": "关键词",
             "targetingType": 2, "impressions": 50, "clicks": 5, "cost": 5.0,
             "totalOrderCnt": 1, "totalOrderSum": 50.0, "totalCartCnt": 2}], "total": 1}}
    if "/logplatform/common/list/query" in url:
        win_s, win_e = payload.get("beginTime"), payload.get("endTime")
        rows = []
        if win_s <= "2026-10-02" <= win_e:
            rows.append({"optTime": "2026-10-02 10:00:00", "actionObject": "计划A",
                         "actionObjectId": "9001", "operationContent": "出价设置",
                         "operationDetails": "目标投产比 由 5.0 修改为 3.0",
                         "operator": "tester"})
        return {"code": 1, "data": {"data": rows, "count": len(rows)}}
    if "getDims.ajax" in url:
        return {"header": {"code": 0}, "body": {"data": [
            {"dimCode": "bsBrand", "dimVals": [{"value": "B1"}]},
            {"dimCode": "bsCate", "dimVals": [{"value": "C1"}]}]}}
    if "productTable.ajax" in url:
        day = (payload.get("startDate") or DAYS[0])
        return {"header": {"code": 0}, "body": {"data": sz_product(day)}}
    if "getCoreSummary.ajax" in url:
        day = (payload.get("startDate") or DAYS[0])
        i = idx(day)
        return {"header": {"code": 0}, "body": {"data": [
            {indicators.SZ_UV: 100 * i, indicators.SZ_PV: 200 * i,
             indicators.SZ_AMT: 5000.0 * i, indicators.SZ_ORDN: 40 * i}]}}
    if "getFlowSrcTop.ajax" in url:
        return {"header": {"code": 0}, "body": {"data": [
            {"name": "搜索", indicators.SRC_UV: 60, indicators.SRC_AMT: 3000.0}]}}
    return {"code": -1, "msg": "unmocked " + url}


CORS = {"access-control-allow-origin": "*", "access-control-allow-credentials": "true",
        "access-control-allow-headers": "*", "access-control-allow-methods": "*"}


def handler(route):
    req = route.request
    url = req.url
    origin = req.headers.get("origin")
    headers = dict(CORS)
    if origin:
        headers["access-control-allow-origin"] = origin
    if req.method == "OPTIONS":
        route.fulfill(status=204, headers=headers, body="")
        return
    if "jzt-api.jd.com" in url or "atoms-api.jd.com" in url or "szgateway.jd.com" in url:
        payload = body_of(req)
        data = api_response(url, payload)
        route.fulfill(status=200, headers=dict(headers, **{"content-type": "application/json"}),
                      body=json.dumps(data, ensure_ascii=False))
        return
    if "jdsz.jd.com" in url:
        route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=SZ_STUB)
        return
    route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=STUB_PAGE)


def main() -> int:
    settings.ensure_dirs()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(TMP / "profile"), headless=True,
            viewport={"width": 1200, "height": 800}, locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"])
        ctx.add_cookies([{"name": "pin", "value": "tester", "domain": ".jd.com", "path": "/"}])
        ctx.route("**/*", handler)
        scrape_day.launch_profile = lambda headless=None, account=None: (pw, ctx)

        print("[1] 真实浏览器跑 scrape_day（京东接口被 route 打桩）")
        res = scrape_day.scrape([settings.get_account("main")], DAYS[0], DAYS[-1],
                                log=lambda m: None)
        errs = res["accounts"]["main"]["errors"]
        check("没有抓取错误", len(errs), 0)
        if errs:
            for e in errs[:5]:
                print("      !", e)

        print("")
        print("[2] 分页 + 限流退避")
        check_true("搜索词接口被调用（含一次 -3010 重试）", COUNTERS["searchword"] >= 2)
        check("搜索词天数", len(ds.days_for("main", "jst_searchword")), 3)
        sw = ds.load_day("main", "2026-10-01", "jst_searchword")
        check("分页合并行数 = 200 + 5", len(sw["rows"]), 205)
        check_true("登录校验接口被调用", COUNTERS["logininfo"] >= 1)

        print("")
        print("[3] isDaily 按天拆分")
        camp = ds.load_day("main", "2026-10-02", "jst_campaign")
        check("jst_campaign 10/02 行数", len(camp["rows"]), 1)
        check("jst_campaign 10/02 花费", camp["rows"][0]["cost"], 200.0)
        sku = ds.load_day("main", "2026-10-03", "jst_sku")
        check("jst_sku 10/03 skuId 保留", sku["rows"][0]["skuId"], 100000000001)

        print("")
        print("[4] 概览 / 关键词 / 操作日志")
        jzt = ds.load_day("main", "2026-10-01", "jzt_campaign")
        check("jzt_campaign 行数", len(jzt["rows"]), 1)
        check("jzt_campaign 花费", jzt["rows"][0]["cost"], 100.0)
        check("oplog 10/02 行数", len(ds.load_day("main", "2026-10-02", "oplog")["rows"]), 1)
        check("oplog 10/01 空天也落盘", len(ds.load_day("main", "2026-10-01", "oplog")["rows"]), 0)

        print("")
        print("[5] 商智：真实 capture + 复用动态签名头")
        prod = ds.load_day("main", "2026-10-02", "sz_product")
        check("商智商品行数（含合计行）", len(prod["rows"]), 2)
        check_true("商智商品带成交额", float(prod["rows"][0][indicators.SZ_AMT]) == 10000.0)
        flow = ds.load_day("main", "2026-10-03", "sz_flow")
        check("商智流量 core 行数", len(flow["core"]), 1)
        check("商智来源行数", len(flow["source"]), 1)

        print("")
        print("[6] 落库结果可直接聚合")
        from jd_roi import analyze2
        d = analyze2.build(start=DAYS[0], end=DAYS[-1], accounts=[settings.get_account("main")])
        check("三日花费", d["advice"]["combined"]["w2"]["cost"], 600.0)
        check("三日成交", d["advice"]["combined"]["w2"]["amt"], 6000.0)
        check("三日商智成交", d["advice"]["combined"]["w2"]["szAmt"], 30000.0)
        check("调整条数", len(d["adjustments"]), 1)

        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
