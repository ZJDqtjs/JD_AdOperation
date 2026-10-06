# -*- coding: utf-8 -*-
"""端到端管线测试：不登录、不联网，验证「服务化」全链路。

覆盖：
  1) 冷启动：空数据目录下 webapp 能起、接口可用、报表给友好提示而不是 500
  2) 夜间抓取编排：scrape_day.scrape 逐天写库、第二次运行自动跳过已完成的天
  3) 区间聚合：analyze2 对 1/2/3 天区间给出正确加总
  4) 报表渲染：report2 生成自包含 HTML
  5) Web 端到端：真起一个 uvicorn，用 HTTP 请求 /api/* 与 /report
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="jdroi_e2e_"))

# settings 在导入时求值，必须先设环境变量
os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_EXCEL_PATH"] = str(TMP / "config" / "missing.xlsx")
os.environ["JD_ENABLE_SCHEDULER"] = "0"
os.environ["JD_RUN_ON_START"] = "0"
os.environ["JD_ACCOUNTS"] = json.dumps([
    {"key": "main", "label": "主账号", "account_id": 111},
    {"key": "b", "label": "账号B", "account_id": 222}])
sys.path.insert(0, str(ROOT))

from jd_roi import analyze2, dailystore as ds, indicators, report2, scrape_day, settings  # noqa: E402

DAYS = ["2026-10-01", "2026-10-02", "2026-10-03"]
SKU = {"main": "100000000001", "b": "100234746567"}
FAILS = []


def check(name, got, want, tol=0.01):
    try:
        ok = abs(float(got) - float(want)) <= tol
    except (TypeError, ValueError):
        ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + str(name) + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)
    return ok


def check_true(name, cond):
    print("  [" + ("PASS" if cond else "FAIL") + "] " + str(name))
    if not cond:
        FAILS.append(name)
    return bool(cond)


# ---------------------------------------------------------------- 假浏览器 / 假接口
class FakePage:
    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def inner_text(self, *a, **k):
        return ""

    def close(self):
        return None


class FakeCtx:
    def new_page(self):
        return FakePage()

    def close(self):
        return None


class FakePW:
    def stop(self):
        return None


def idx(day):
    return DAYS.index(day) + 1


def fake_jzt_day(page, day):
    i = idx(day)
    return {"rows": [{"campaignId": 9001, "campaignName": "计划A", "campaignType": 61,
                      "spuId": None, "impressions": 1000 * i, "clicks": 100 * i,
                      "cost": 100.0 * i, "totalOrderCnt": 10 * i,
                      "totalOrderSum": 1000.0 * i, "totalCartCnt": 20 * i}],
            "dateStart": day, "dateEnd": day}


def fake_jst_multiday(page, kind, start, end):
    out = {}
    for day in DAYS:
        if not (start <= day <= end):
            continue
        i = idx(day)
        row = {"campaignId": 9001, "campaignName": "计划A", "date": day.replace("-", ""),
               "impressions": 1000 * i, "clicks": 100 * i, "cost": 100.0 * i,
               "totalOrderCnt": 10 * i, "totalOrderSum": 1000.0 * i, "totalCartCnt": 20 * i}
        if kind == "jst_sku":
            row["skuId"] = int(SKU["main"])
        out[day] = [row]
    return out


def fake_jst_day(page, kind, day, max_rows=None):
    i = idx(day)
    return {"rows": [{"campaignId": 9001, "campaignName": "计划A", "searchTerm": "测试词",
                      "impressions": 500 * i, "clicks": 50 * i, "cost": 50.0 * i,
                      "totalOrderCnt": 5 * i, "totalOrderSum": 400.0 * i,
                      "totalCartCnt": 10 * i}],
            "dateStart": day, "dateEnd": day}


def fake_kw_day(page, day, account_id):
    i = idx(day)
    return {"rows": [{"campaignId": 9001, "campaignName": "计划A", "groupId": 7,
                      "keywordName": "测试词", "targetingType": 2,
                      "impressions": 300 * i, "clicks": 30 * i, "cost": 30.0 * i,
                      "totalOrderCnt": 3 * i, "totalOrderSum": 300.0 * i,
                      "totalCartCnt": 6 * i}],
            "dateStart": day, "dateEnd": day}


def fake_oplog_range(page, acct_key, start, end):
    rows = []
    if start <= "2026-10-02" <= end:
        rows.append({"optTime": "2026-10-02 10:00:00", "actionObject": "计划A",
                     "actionObjectId": "9001", "operationContent": "出价设置",
                     "operationDetails": "目标投产比 由 5.0 修改为 3.0",
                     "operator": "tester", "_level": "campaign"})
    return {"rows": rows, "begin": start, "end": end}


class FakeSz:
    def __init__(self, context, kind):
        self.kind = kind

    def fetch(self, day):
        i = idx(day)
        if self.kind == "flow":
            return {"core": [{indicators.SZ_UV: 100 * i, indicators.SZ_PV: 200 * i,
                              indicators.SZ_AMT: 5000.0 * i, indicators.SZ_ORDN: 40 * i}],
                    "source": [{"name": "搜索", indicators.SRC_UV: 60 * i,
                                indicators.SRC_AMT: 3000.0 * i}],
                    "rawCode": 0, "dateStart": day, "dateEnd": day}
        return {"rows": [{"sku_id": SKU["main"], "name": "测试商品", "img_src": "",
                          "pro_url": "", indicators.SZ_AMT: 5000.0 * i,
                          indicators.SZ_ORDN: 40 * i, indicators.SZ_QTY: 40 * i,
                          indicators.SZ_UV: 100 * i, indicators.SZ_PV: 200 * i,
                          indicators.SZ_CVR: 0.4},
                         {"sku_id": "合计", "$summary": True,
                          indicators.SZ_AMT: 5000.0 * i, indicators.SZ_ORDN: 40 * i,
                          indicators.SZ_QTY: 40 * i, indicators.SZ_UV: 100 * i,
                          indicators.SZ_PV: 200 * i}],
                "code": 0, "dateStart": day, "dateEnd": day}

    def close(self):
        return None


def stub_network():
    scrape_day.launch_profile = lambda headless=None, account=None: (FakePW(), FakeCtx())
    scrape_day.has_login = lambda ctx: True
    scrape_day.session_ok = lambda page, retries=2: True
    scrape_day.fetch_jzt_day = fake_jzt_day
    scrape_day.fetch_jst_multiday = fake_jst_multiday
    scrape_day.fetch_jst_day = fake_jst_day
    scrape_day.fetch_kw_day = fake_kw_day
    scrape_day.fetch_oplog_range = fake_oplog_range
    scrape_day.SzFetcher = FakeSz


# ---------------------------------------------------------------- Web 端到端
PORT = 8899


def start_server():
    import uvicorn
    from jd_roi import webapp
    cfg = uvicorn.Config(webapp.app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        time.sleep(0.5)
        if getattr(server, "started", False):
            return server
    raise RuntimeError("uvicorn 启动超时")


def http(path, method="GET"):
    req = urllib.request.Request("http://127.0.0.1:" + str(PORT) + path, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main() -> int:
    settings.ensure_dirs()
    stub_network()
    server = start_server()
    print("uvicorn 已启动 on", PORT)

    # ---------- 1) 冷启动 ----------
    print("")
    print("[1] 冷启动（空数据目录）")
    code, body = http("/api/coverage")
    check("GET /api/coverage 状态", code, 200)
    check("冷启动天数", json.loads(body)["coverage"]["main"]["days"]["count"], 0)
    code, body = http("/api/status")
    check("GET /api/status 状态", code, 200)
    st = json.loads(body)
    check("调度时间", str(st["schedule"]["hour"]) + ":" + str(st["schedule"]["minute"]), "0:5")
    code, body = http("/report?start=2026-10-01&end=2026-10-03")
    check("冷启动 /report 不报 500", code, 200)
    check_true("冷启动 /report 给出友好提示", "该区间没有数据" in body)

    # ---------- 2) 抓取编排 ----------
    print("")
    print("[2] 夜间抓取编排（打桩，不联网）")
    s, e = scrape_day.default_range(days=3)
    check("默认区间跨 3 天", (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days + 1, 3)
    check("默认区间止于昨天", e, (ds.today() - dt.timedelta(days=1)).isoformat())

    res = scrape_day.scrape(settings.ACCOUNTS, DAYS[0], DAYS[-1], log=lambda m: None)
    check("两个账号都跑了", len(res["accounts"]), 2)
    for acct in ("main", "b"):
        for kind in ds.KINDS:
            check(acct + "/" + kind + " 入库天数", len(ds.days_for(acct, kind)), 3)

    calls = {"n": 0}
    orig = scrape_day.fetch_jzt_day

    def counting(page, day):
        calls["n"] += 1
        return orig(page, day)

    scrape_day.fetch_jzt_day = counting
    scrape_day.scrape(settings.ACCOUNTS, DAYS[0], DAYS[-1], log=lambda m: None)
    scrape_day.fetch_jzt_day = orig
    check("第二次运行跳过已完成的天", calls["n"], 0)

    # ---------- 3) 区间聚合 ----------
    print("")
    print("[3] 区间聚合")
    M = [settings.get_account("main")]
    check("单日花费", analyze2.build(start="2026-10-01", end="2026-10-01", accounts=M)["advice"]["combined"]["w2"]["cost"], 100.0)
    check("单日成交", analyze2.build(start="2026-10-01", end="2026-10-01", accounts=M)["advice"]["combined"]["w2"]["amt"], 1000.0)
    check("单日商智成交", analyze2.build(start="2026-10-01", end="2026-10-01", accounts=M)["advice"]["combined"]["w2"]["szAmt"], 5000.0)
    d3 = analyze2.build(start="2026-10-01", end="2026-10-03", accounts=M)
    check("三日花费", d3["advice"]["combined"]["w2"]["cost"], 600.0)
    check("三日成交", d3["advice"]["combined"]["w2"]["amt"], 6000.0)
    check("三日商智成交", d3["advice"]["combined"]["w2"]["szAmt"], 30000.0)
    check("三日广告渗透率", d3["advice"]["combined"]["w2"]["adShare"], 20.0)
    check("操作日志只落在 10/02", len(d3["adjustments"]), 1)
    check("两日花费", analyze2.build(start="2026-10-02", end="2026-10-03", accounts=M)["advice"]["combined"]["w2"]["cost"], 500.0)
    cov3 = analyze2.build(start="2026-10-01", end="2026-10-04", accounts=M)["meta"]["coverage"]["main"]["w2"]
    check("覆盖度 got", cov3["got"], 3)
    check("覆盖度 want", cov3["want"], 4)
    check("覆盖度标记不完整", cov3["complete"], False)

    # ---------- 4) 报表渲染 ----------
    print("")
    print("[4] 报表渲染")
    html = report2.render(d3, ui='<form class="rangebar"></form>', title="TEST")
    check_true("HTML 含区间选择器", 'class="rangebar"' in html)
    check_true("HTML 含商品ROI交叉表", "商品 ROI 交叉表" in html)
    check_true("HTML 内联 ECharts", "echarts" in html)
    check_true("HTML 标注所选区间", "2026-10-01" in html and "2026-10-03" in html)

    # ---------- 5) Web 端到端 ----------
    print("")
    print("[5] Web HTTP 端到端")
    code, body = http("/api/coverage")
    check("抓取后 main 天数", json.loads(body)["coverage"]["main"]["days"]["count"], 3)
    code, body = http("/api/analysis?start=2026-10-01&end=2026-10-03&accounts=main")
    check("GET /api/analysis 状态", code, 200)
    check("API 区间花费", json.loads(body)["data"]["advice"]["combined"]["w2"]["cost"], 600.0)
    code, body = http("/report?start=2026-10-01&end=2026-10-03&accounts=main")
    check("GET /report 状态", code, 200)
    check_true("/report 标题含区间", "2026-10-01 ~ 2026-10-03" in body)
    check_true("/report 含计划名", "计划A" in body)
    code, body = http("/")
    check("GET / 状态", code, 200)
    check_true("控制台含数据覆盖", "数据覆盖" in body)
    code, body = http("/healthz")
    check("GET /healthz", body.strip(), "ok")

    server.should_exit = True
    time.sleep(1)
    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
