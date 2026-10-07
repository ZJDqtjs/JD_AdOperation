# -*- coding: utf-8 -*-
"""按 SKU 真实成本算保本线（口径测试）。

关键口径（供货方 ERP 接口 /api/open/sku-costs）：
  supply  = 京东结算给我们的单件金额（**收入**，已扣点）
  _goodsCost = 我方买货成本
  单件贡献 = supply − 货款 − 运费 − 包材 − 人工
  毛利率   = 贡献 ÷ 件单价；  保本 ROI = 1 ÷ 毛利率
旧文档把 supply 当成本、又乘一遍 platformRate，会算出负毛利与 999 的保本线 —— 本测试把正确口径钉住。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="jdroi_sku_costs_", dir=str(ROOT / "tmp")))
os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ.setdefault("JD_COSTS_URL", "")   # 离线：测试不连供货方成本接口
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_EXCEL_PATH"] = str(TMP / "config" / "missing.xlsx")
os.environ["JD_ENABLE_SCHEDULER"] = "0"
sys.path.insert(0, str(ROOT))

from jd_roi import analyze2 as a  # noqa: E402

FAILS = []


def check(name, got, want, tol=0.011):
    ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) and isinstance(got, (int, float)) \
        else str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + f"] {name}: got={got} want={want}")
    if not ok:
        FAILS.append(name)


# 全店兜底口径（等价于 Excel 缺失时的内置 COST_MODEL）
M = {"refPrice": 30.0, "product": 18.0, "platform": 1.2, "package": 0.6,
     "shipping": 3.0, "labor": 0.0, "returnRate": 0.05}

COSTS = {
    "default": {"shipping": 5.0, "platformRate": 0.0, "package": 0.0, "labor": 0.0, "returnRate": 0.05},
    "skus": {
        "1001": {"supply": 20.0, "supplyGross": 21.0, "price": None, "shipping": 2.0,
                 "package": 0.5, "labor": 0.5, "platformRate": 0.0476, "returnRate": None,
                 "_goodsCost": 10.0, "_qty": 100.0, "_source": "sale", "_name": "样例A"},
        "1002": {"supply": 12.0, "supplyGross": 12.0, "shipping": 2.0,
                 "_goodsCost": 10.5, "_source": "sale", "_name": "样例B"},
        "1003": {"supply": 8.0, "supplyGross": 8.0, "shipping": 1.0,
                 "_goodsCost": 3.0, "unitsPerSale": 2.0, "_source": "warehouse", "_name": "样例C"},
    },
}


def main() -> int:
    print("[1] supply 是「收入」：贡献 = 结算价 − 货款 − 运费 − 包材 − 人工")
    c = a.sku_cost_of("1001", 25.0, M, COSTS, {})
    check("单件贡献 20-10-2-0.5-0.5", c["grossPerOrder"], 7.0)
    check("毛利率 7/25", c["margin"], 0.28)
    check("保本 ROI 25/7", c["breakeven"], 3.57)
    check("放量线 保本×1.25", c["growLine"], 4.46)
    check("件单价优先于 ERP price(null)", c["aov"], 25.0)
    check("成本来源", c["costSource"], "erp")
    check("扣点只展示、不再进成本", c["platform"], 1.0)
    # 旧公式（把 supply 当成本 + 再扣 price×platformRate）会得到什么：
    old_cost = 20.0 + 25.0 * 0.0476 + 2.0 + 0.5 + 0.5
    old_margin = (25.0 - old_cost) / 25.0
    check("旧公式毛利率被压到 3%", round(old_margin, 4), 0.0324)
    check("旧公式保本线荒谬（>20，真实 3.57）", round(1 / old_margin, 2) > 20, True)

    print("[2] 未命中接口 → 回落全店口径，报表据此标 *")
    f = a.sku_cost_of("9999", 25.0, M, COSTS, {})
    check("成本来源", f["costSource"], "store")
    check("回落毛利率", f["margin"], a.global_margin(M))
    check("回落保本线", f["breakeven"], round(1 / a.global_margin(M), 2))

    print("[3] 结算价低于成本 = 卖一件亏一件：保本线 0 是「有意义」，不能被当成空值回落")
    # 1002 只有 supply/shipping/_goodsCost，包材按「全店模型 0.6 → default 0」的顺序回落
    z = a.sku_cost_of("1002", 20.0, M, COSTS, {})
    check("贡献为负 12-10.5-2-0.6", z["grossPerOrder"], -1.1)
    check("包材回落全店模型", z["package"], 0.6)
    check("保本线为 0", z["breakeven"], 0.0)
    row = {"skuId": "1002", "costSource": "erp", "costParams": z, "breakeven": 0.0, "growLine": 0.0,
           "wins": {k: {"cost": 100.0, "roi": 5.0, "amt": 500.0} for k in a.WKEYS},
           "est": {k: {"netProfit": 100.0} for k in a.WKEYS}}
    check("分层 = 严重亏损", a._verdict(row)["tier"], "严重亏损")
    row2 = dict(row, costSource="store", breakeven=a.BREAKEVEN_FLAT, growLine=a.BREAKEVEN_FLAT * 1.25)
    check("同一条数据按全店线则不算结构性亏损", a._verdict(row2)["tier"], "放大")

    print("[4] 单位换算：接口 unitsPerSale 优先于手工表，两者都没有按 1:1")
    u = a.sku_cost_of("1003", 20.0, M, COSTS, {"1003": 3.0})
    check("结算价 ×2（接口值优先）", u["supply"], 16.0)
    check("货款 ×2", u["productCost"], 6.0)
    check("贡献 ×2 = 16-6-2-1.2（包材回落全店 0.6）", u["grossPerOrder"], 6.8)
    check("换算来源", u["unitFactorSource"], "erp")
    m = a.sku_cost_of("1001", 25.0, M, COSTS, {"1001": 2.0})
    check("手工表生效", m["unitFactor"], 2.0)
    check("手工来源", m["unitFactorSource"], "manual")
    n = a.sku_cost_of("1001", 25.0, M, COSTS, {})
    check("默认 1:1", n["unitFactor"], 1.0)
    check("默认来源", n["unitFactorSource"], "assumed")

    print("[5] 整店一条线用「成交额加权」，不是算术平均")
    be, mg = a.blended_breakeven([{"amt": 1000.0, "margin": 0.5}, {"amt": 1000.0, "margin": 0.1}])
    check("加权毛利率", mg, 0.3)
    check("加权保本线", be, 3.33)
    be2, _ = a.blended_breakeven([{"amt": 3000.0, "margin": 0.5}, {"amt": 100.0, "margin": 0.1}])
    check("高毛利占大头时接近 2.00", be2, 2.05)
    check("空篮子退回全店线", a.blended_breakeven([])[0], round(1 / a.global_margin(a.COST_MODEL), 2))

    print("[6] 覆盖率统计：报表必须说清「多少钱是按真实成本判的」")
    def mk(sid, cost, src, be, warn=None):
        cp = {"unitWarn": warn} if warn else {}
        return {"skuId": sid, "name": sid, "costSource": src, "breakeven": be,
                "grossPerOrder": be, "costParams": cp,
                "wins": {k: {"cost": 0.0, "roi": 0.0, "amt": 0.0} for k in a.WKEYS},
                "est": {k: {"netProfit": 0.0} for k in a.WKEYS}}
    s = [mk("1001", 200.0, "erp", 3.5), mk("1002", 50.0, "erp", 0.0),
         mk("9999", 100.0, "store", 3.3), mk("1004", 50.0, "erp", 2.0, warn="结算价≥件单价")]
    for x, c in zip(s, (200.0, 50.0, 100.0, 50.0)):
        x["wins"]["w2"]["cost"] = c
    cov = a._cost_coverage(s, COSTS)
    check("命中数", cov["skuErp"], 3)
    check("真实成本花费", cov["costErp"], 300.0)
    check("本期总花费", cov["costTotal"], 400.0)
    check("覆盖率", cov["costShare"], 75.0)
    check("结构性亏损 1 个", len(cov["negativeMargin"]), 1)
    check("口径待确认 1 个", len(cov["unitWarn"]), 1)
    check("未接入清单只 1 个", [x["skuId"] for x in cov["missing"]], ["9999"])
    check("ERP 内 SKU 总数", cov["skusInErp"], 3)

    print("\n" + ("ALL GREEN" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
