# -*- coding: utf-8 -*-
"""区间聚合的确定性测试：不需要登录、不碰网络。

验证「任意区间分析 = 按天入库后本地聚合」这一核心承诺：
  1) 造 5 天已知数据写入按天库
  2) 用不同区间调用 analyze2.build，断言总额等于对应天的加总
  3) 断言会话/概览口径、商智口径、操作日志都按区间正确切片
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(__file__).resolve().parent.parent
_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_test_", dir=str(_TMPROOT)))

# 必须在 import jd_roi.* 之前设好环境变量（settings 在导入时求值）
os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_EXCEL_PATH"] = str(TMP / "config" / "none.xlsx")
os.environ["JD_ACCOUNTS"] = '[{"key":"main","label":"主账号","account_id":1}]'
sys.path.insert(0, str(ROOT))

from jd_roi import analyze2, dailystore as ds, indicators, settings  # noqa: E402

DAYS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
COST = [100.0, 200.0, 300.0, 400.0, 500.0]     # 每天广告花费
AMT = [1000.0, 1000.0, 1000.0, 1000.0, 1000.0]  # 每天广告成交
SZ = [2000.0, 4000.0, 6000.0, 8000.0, 10000.0]  # 每天商智成交
SKU = "100000000001"


def seed():
    for i, day in enumerate(DAYS):
        ds.save_day("main", day, "jzt_campaign", {"rows": [{
            "campaignId": 9001, "campaignName": "测试计划", "campaignType": 61,
            "impressions": 1000, "clicks": 100, "cost": COST[i],
            "totalOrderCnt": 10, "totalOrderSum": AMT[i], "totalCartCnt": 20}]})
        ds.save_day("main", day, "jst_campaign", {"rows": [{
            "campaignId": 9001, "campaignName": "测试计划", "date": day.replace("-", ""),
            "impressions": 1000, "clicks": 100, "cost": COST[i],
            "totalOrderCnt": 10, "totalOrderSum": AMT[i], "totalCartCnt": 20}]})
        ds.save_day("main", day, "jst_sku", {"rows": [{
            "campaignId": 9001, "campaignName": "测试计划", "skuId": SKU,
            "impressions": 1000, "clicks": 100, "cost": COST[i],
            "totalOrderCnt": 10, "totalOrderSum": AMT[i], "totalCartCnt": 20}]})
        ds.save_day("main", day, "sz_product", {"rows": [{
            "sku_id": SKU, "name": "测试商品",
            indicators.SZ_AMT: SZ[i], indicators.SZ_ORDN: 20,
            indicators.SZ_QTY: 20, indicators.SZ_UV: 100,
            indicators.SZ_PV: 200,
            indicators.SZ_CVR: 0.1}]})
        if i == 2:
            ds.save_day("main", day, "oplog", {"rows": [{
                "optTime": day + " 10:00:00", "actionObject": "测试计划",
                "actionObjectId": "9001", "operationContent": "出价设置",
                "operationDetails": "【目标投产比】由 5.0 修改为 3.0",
                "operator": "tester", "_level": "campaign"}]})
    ds.invalidate_cache("main")


def check(name, got, want, tol=0.01):
    try:
        ok = abs(float(got) - float(want)) <= tol
    except (TypeError, ValueError):
        ok = str(got) == str(want)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got} want={want}")
    return ok


def main() -> int:
    settings.ensure_dirs()
    seed()
    print("按天库覆盖：", {k: len(v) for k, v in
                         [("jzt", ds.days_for("main", "jzt_campaign")),
                          ("sz", ds.days_for("main", "sz_product")),
                          ("oplog", ds.days_for("main", "oplog"))]})
    allok = True

    # --- 1) 3 天区间（10/03~10/05） ---
    d = analyze2.build(start="2026-10-03", end="2026-10-05")
    w = d["meta"]["windows"]
    print("\n区间 10/03~10/05：" + " | ".join(x['label'] for x in w))
    allok &= check("所选区间=最后一天", w[-1]["start"], "2026-10-03")
    allok &= check("所选区间=最后一天", w[-1]["end"], "2026-10-05")
    allok &= check("前一期长度=3", 
                   (__import__("datetime").date.fromisoformat(w[1]["end"])
                    - __import__("datetime").date.fromisoformat(w[1]["start"])).days + 1, 3)
    tot = d["advice"]["combined"]["w2"]
    allok &= check("区间花费=300+400+500", tot["cost"], sum(COST[2:]))
    allok &= check("区间成交=3000", tot["amt"], sum(AMT[2:]))
    allok &= check("区间ROI=3000/1200", tot["roi"], round(sum(AMT[2:]) / sum(COST[2:]), 2))
    allok &= check("商智成交=6000+8000+10000", tot["szAmt"], sum(SZ[2:]))
    allok &= check("广告渗透率", tot["adShare"], round(sum(AMT[2:]) / sum(SZ[2:]) * 100, 1))
    allok &= check("SKU 花费", d["skus"][0]["wins"]["w2"]["cost"], sum(COST[2:]))
    allok &= check("SKU 商智成交", d["skus"][0]["wins"]["w2"]["szAmt"], sum(SZ[2:]))

    # --- 2) 单天区间 ---
    d1 = analyze2.build(start="2026-10-01", end="2026-10-01")
    t1 = d1["advice"]["combined"]["w2"]
    print("\n区间 10/01~10/01：")
    allok &= check("单日花费", t1["cost"], COST[0])
    allok &= check("单日成交", t1["amt"], AMT[0])
    allok &= check("单日商智", t1["szAmt"], SZ[0])
    allok &= check("单日广告渗透率", t1["adShare"], round(AMT[0] / SZ[0] * 100, 1))

    # --- 3) 操作日志只在 10/03，区间外不应出现 ---
    d3 = analyze2.build(start="2026-10-03", end="2026-10-05")
    allok &= check("区间内操作数", len(d3["adj"]["stop"]) if False else
                   len([o for o in d3["adjustments"]]), 1)
    d4 = analyze2.build(start="2026-10-04", end="2026-10-05")
    allok &= check("区间外操作数=0", len(d4["adjustments"]), 0)

    # --- 4) 7 天区间（含缺失的前置天）不应崩 ---
    d5 = analyze2.build(start="2026-09-28", end="2026-10-05")
    allok &= check("含缺数据区间仍可取到总额", d5["advice"]["combined"]["w2"]["cost"], sum(COST))
    cov = d5["meta"]["coverage"]["main"]["w2"]
    allok &= check("覆盖度 got = 实际天数", cov["got"], 5)
    allok &= check("覆盖度 want = 区间天数", cov["want"], 8)
    allok &= check("覆盖度标记不完整", cov["complete"], False)

    print("\n" + ("ALL PASS" if allok else "HAS FAILURES"))
    return 0 if allok else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
