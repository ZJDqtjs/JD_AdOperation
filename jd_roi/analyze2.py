# -*- coding: utf-8 -*-
"""跨账号 · 按 SKU 交叉统计 + 调整操作复盘。

与旧 analyze.py 的区别：
1. **两个账号都参与广告口径**（旧版默认只算账号B，却把主账号商智成交算进渗透率分母，口径错位）
2. **三期对比** w0(前两期) / w1(前一期) / w2(用户所选区间)，区间长度任意
3. **每个 SKU 单独的成本/毛利/保本ROI**（用商智真实客单价 × Excel 成本结构推算）
4. **把京准通「操作日志」与效果变化对齐**，判定每次调整正确/有问题
5. 计划、SKU、搜索词都保留三窗口趋势

输出 data/analysis2.json
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import config, settings, store

# ---------------- 窗口定义 ----------------
# 报表固定展示 3 期：w2 = 用户所选区间，w1/w0 = 等长的前两期（用于环比与趋势）
WKEYS = ["w0", "w1", "w2"]
WINDOW_ROLE = {0: "前两期", 1: "前一期", 2: "所选区间"}

# 兼容旧脚本的固定窗口
WINDOWS = [
    ("w0", "2026-09-13", "2026-09-19", "调整前 9/13-9/19"),
    ("w1", "2026-09-20", "2026-09-26", "第一轮调整 9/20-9/26"),
    ("w2", "2026-09-27", "2026-10-03", "9/29大调整后 9/27-10/3"),
]


def windows_for(start: str, end: str) -> list:
    """以 [start,end] 为最后一期，向前生成两个等长窗口。

    返回 [(key, start, end, label), ...]，顺序为 w0(最早) → w2(所选区间)。
    """
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    n = (d1 - d0).days + 1
    out = []
    for idx, back in enumerate((2, 1, 0)):
        s = d0 - _dt.timedelta(days=n * back)
        e = d1 - _dt.timedelta(days=n * back)
        label = f"{WINDOW_ROLE[idx]} {s.strftime('%m/%d')}~{e.strftime('%m/%d')}"
        if back == 0:
            label = f"所选区间 {s.strftime('%m/%d')}~{e.strftime('%m/%d')}（{n}天）"
        out.append((WKEYS[idx], s.isoformat(), e.isoformat(), label))
    return out

# ---------------- 成本结构（来自 Excel「计算公式」，可被 load_cost_model 覆盖）----------------
# 下面是「成本表缺失」时的兜底示例口径（非真实经营数据）：客单价 100 / 成本 70 / 运费 5
# → 毛利率 25% → 保本 ROI 4.00。真实成本请放进 config/ 下的成本表（不入库）或 config/costs.json。
COST_MODEL = {
    "refPrice": 100.0,      # H2 参考客单价
    "product": 70.0,        # H3 参考产品成本
    "platform": 0.0,        # H4 京东扣点（绝对值）
    "package": 0.0,         # H5 礼物及包装
    "shipping": 5.0,        # H6 运费
    "returnRate": 0.05,     # I14 退货率
}
BREAKEVEN_FLAT = 4.00       # 兜底口径下的保本ROI（真实值由成本表算出）

TYPE_LABEL = {61: "智能化", 2: "快车-关键词", 153: "全站智能", 0: "其他"}
SZ_UV = "jdr_sch_traffic_brow_sku__page_cnt_traffic_plat_item_di_sz_bsg"
SZ_PV = "jdr_sch_traffic_brow_sku__page_qtty_traffic_plat_item_di_sz_bsg"
SZ_AMT = "jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot"
SZ_ORDN = "jdr_sch_trade_deal_ord_ord_qtty_sz_trade_deal_snapshot"
SZ_QTY = "jdr_sch_trade_deal_ord_sku_qtty_sz_trade_deal_snapshot"
SZ_CVR = "fo_jdr_sch_industry_deal_rate"

WASTE_MIN = 3.0     # 废词：花费≥3 且 0 单
LOW_MIN = 10.0      # 低效词：花费≥10 且 ROI<保本
GOOD_MIN_ORD = 2    # 高效词：订单≥2 且 ROI>保本


def _f(x, d=0.0):
    try:
        if x is None:
            return d
        return float(x)
    except (TypeError, ValueError):
        return d


def _i(x, d=0):
    return int(_f(x, d))


# ---------------- 成本 / 保本 ROI ----------------
def load_cost_model():
    p = dict(COST_MODEL)
    try:
        import openpyxl
        from . import settings as _settings
        wb = openpyxl.load_workbook(_settings.excel_path(), data_only=True)
        ws = wb["计算公式"]
        for k, cell in (("refPrice", "H2"), ("product", "H3"), ("platform", "H4"),
                        ("package", "H5"), ("shipping", "H6")):
            v = ws[cell].value
            if v is not None:
                p[k] = _f(v, p[k])
        v = ws["I14"].value
        if v is not None:
            p["returnRate"] = _f(v, p["returnRate"])
    except Exception:  # noqa: BLE001
        pass
    return p


def global_margin(m: dict) -> float:
    """Excel「计算公式」H8 的口径：毛利率 = (客单价 - 产品成本 - 扣点 - 包装 - 运费) / 客单价。

    注意：Excel 里退货成本（J14 = 产出笔数 × 退货率 × 运费）是**单独一行**，
    不计入 H8，所以这里也不减 —— 否则会与成本表 H8 的口径打架（报表数字对不上模板）。
    """
    price = _f(m.get("refPrice")) or 1.0
    profit = (price - _f(m.get("product")) - _f(m.get("platform"))
              - _f(m.get("package")) - _f(m.get("shipping")))
    return round(profit / price, 4) if price else 0.0


def margin_ladder(m: dict):
    """毛利率敏感性：不同毛利率下对应的保本 ROI。"""
    out = []
    for mg in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        out.append({"margin": mg, "breakeven": round(1 / mg, 2)})
    return out


def sku_margin(aov: float, m: dict) -> dict:
    """用 Excel 的全店毛利率作为统一成本口径（不做无依据的等比缩放）。

    低客单价商品若强行按「成本=客单价×成本率 + 固定运费」推算，会算出 20~60 的荒谬保本线，
    因此这里统一采用 Excel 给出的毛利率，并把「毛利率敏感性」交给用户自行判断。
    """
    price = aov if aov > 0 else _f(m.get("refPrice"))
    margin = global_margin(m)
    be = round(1 / margin, 2) if margin > 0 else 0.0
    return {
        "aov": round(price, 2),
        "productCost": None,
        "shipping": round(_f(m.get("shipping")), 2),
        "platform": round(_f(m.get("platform")), 2),
        "package": round(_f(m.get("package")), 2),
        "returnCost": round(_f(m.get("returnRate")) * _f(m.get("shipping")), 2),
        "grossPerOrder": round(price * margin, 2),
        "margin": round(margin, 4),
        "breakeven": be,
        "frontPrice": round(price, 2),
        "settleRate": 0.0, "marginSettle": 0.0, "breakevenSettle": 0.0,   # 全店口径没有到手结算价
        "growLine": round(be * GROW_FACTOR, 2) if be else 0.0,
        "costSource": "store",
    }


# ---------------- 按 SKU 的真实成本 ----------------
# 供货方 ERP 接口（/api/open/sku-costs）的口径与 Excel 全店口径不同，必须按「收入 − 成本」算：
#   到手结算价 supply = 京东**结算给我们**的每件金额（已扣点，含我方利润）→ 这是收入，不是成本
#   货款 _goodsCost   = 我方买货成本 → 这才是成本
# 因此：单件贡献 = 到手结算价 − 货款 − 运费 − 包材 − 人工。
#
# 分母有两个候选，只有一个能用：
#   前台件单价 price  = 商智成交额 ÷ 件数 = **消费者实付**（入仓品 24 元这类）
#   到手结算价 supply = 真正进我们账户的钱（同一件 18 元，差额是京东扣点+入仓差价）
# 京准通 ROI 的分子是**前台成交额**（实测约 20 个 SKU 广告客单价 ÷ 商智客单价 中位数 ≈ 1.05），
# 所以保本线 = 前台件单价 ÷ 贡献；若改用到手结算价做分母，线会低 ~25%，把亏的判成赚。
# 同时保留 marginSettle / breakevenSettle（贡献 ÷ 到手结算价，与 ERP 里 _marginRate 同口径）供对照展示。
# 注意别把 platformRate 再乘一遍：supply 已经是扣点后的到手结算价（platformRate 仅供核对）。
GROW_FACTOR = 1.25        # 放量线 = 保本线 × 1.25（例：全店口径 4.00 → 5.00）


def sku_cost_of(sid: str, unit_price: float, m: dict, costs: dict, units: dict | None = None) -> dict:
    """取某个 SKU 的真实保本线；ERP 里查不到就回落全店口径（Excel / COST_MODEL）。

    unit_price 用「**前台件单价**」（商智成交额÷件数 = 消费者实付），没有件数就退回客单价：
    京准通 ROI 的分子就是这个前台成交额，分母必须与它同口径；
    到手结算价 supply 只是我们从中真正拿到的那部分（到手率一般 70%~95%），不能拿来当分母。

    units：每 1 个「京东售卖件」等于几个 ERP 计量单位（袋/盒）。默认 1；
    实测 1 才成立（按 ERP÷商智 的件数比换算会让到手结算价高于消费者实付），
    那个比值差的是 ERP 里含了其他渠道销量。真有换算需求时用接口返回的
    unitsPerSale，或写 config/costs_units.json，两者都没有就按 1 并做口径体检。
    """
    units = units if units is not None else settings.costs_unit_map()
    d = (costs or {}).get("default") or {}
    row = ((costs or {}).get("skus") or {}).get(str(sid)) or {}
    price = _f(unit_price)
    if price <= 0:
        price = _f(row.get("price")) or _f(m.get("refPrice")) or 1.0
    supply = _f(row.get("supply"))
    goods = _f(row.get("_goodsCost"), _f(row.get("goodsCost"), _f(d.get("goodsCost"))))
    if supply <= 0 or goods <= 0:                   # 没接入该 SKU → 全店口径
        out = sku_margin(_f(unit_price) or _f(m.get("refPrice")), m)
        out["priceBasis"] = "store"
        out["unitFactor"] = 1.0
        out["unitFactorSource"] = "n/a"
        return out
    k = _f(row.get("unitsPerSale"))
    kf = "erp"
    if k <= 0:
        k = _f(units.get(str(sid)), 1.0)
        kf = "manual" if str(sid) in units else "assumed"
    if k <= 0:
        k = 1.0
    shipping = _f(row.get("shipping"), _f(m.get("shipping"), _f(d.get("shipping")))) * k
    package = _f(row.get("package"), _f(m.get("package"), _f(d.get("package")))) * k
    labor = _f(row.get("labor"), _f(d.get("labor"))) * k
    supply, goods = supply * k, goods * k
    ret_rate = _f(row.get("returnRate"), _f(d.get("returnRate"), _f(m.get("returnRate"))))
    contrib = supply - goods - shipping - package - labor
    # 两个口径别混：price 是**前台件单价**（消费者实付），supply 是**到手结算价**。
    # 京准通 ROI 的分子是前台成交额（实测：广告客单价 ÷ 商智客单价 ≈ 1.0），
    # 所以保本线只能除以 price；除以 supply 会把线算低约 25%，把亏的判成赚。
    margin = round(contrib / price, 4) if price > 0 else 0.0
    be = round(1 / margin, 2) if margin > 0 else 0.0
    settle_rate = round(supply / price, 4) if price > 0 else 0.0        # 到手结算价 ÷ 前台件单价
    margin_settle = round(contrib / supply, 4) if supply > 0 else 0.0   # ERP 口径毛利率（对到手结算价）
    be_settle = round(supply / contrib, 2) if contrib > 0 else 0.0      # 到手结算口径保本线，仅展示
    return {
        "aov": round(price, 2),
        "supply": round(supply, 2),
        "supplyGross": round(_f(row.get("supplyGross")) * k, 2),
        "productCost": round(goods, 2),
        "shipping": round(shipping, 2),
        "package": round(package, 2),
        "labor": round(labor, 2),
        "platform": round((_f(row.get("supplyGross")) - supply) * k, 4),   # 已含在到手结算价里，仅展示
        "platformRate": _f(row.get("platformRate"), _f(d.get("platformRate"))),
        "returnCost": round(ret_rate * (shipping + package), 2),      # 单独一行，不进毛利率
        "grossPerOrder": round(contrib, 2),
        "margin": margin,
        "breakeven": be,
        "frontPrice": round(price, 2),                 # = aov，前台件单价（消费者实付）
        "settleRate": settle_rate,                     # 到手结算价 ÷ 前台件单价
        "marginSettle": margin_settle,                 # 贡献 ÷ 到手结算价（与 ERP 毛利率同口径）
        "breakevenSettle": be_settle,                  # 到手结算口径保本线（展示用）
        "growLine": round(be * GROW_FACTOR, 2) if be else 0.0,
        "costSource": "erp",
        "erpName": row.get("_name") or "",
        "erpSource": row.get("_source") or "",
        "erpQty": _f(row.get("_qty")),
        "unitFactor": round(k, 2),
        "unitFactorSource": kf,
        "priceBasis": "unit" if _f(unit_price) > 0 else "fallback",
    }


def blended_breakeven(rows: list) -> tuple:
    """一篮子 SKU 的加权保本线：按成交额加权毛利率 → 1/加权毛利率。

    计划级、词级、以及整页文案都需要一条「这堆东西的保本线」，
    用加权而不是算术平均，才与「花费转移到高毛利商品」这类结论一致。
    """
    amt = sum(_f(r.get("amt")) for r in rows)
    profit = sum(_f(r.get("amt")) * _f(r.get("margin")) for r in rows)
    if amt <= 0 or profit <= 0:
        return round(1 / global_margin(COST_MODEL), 2) if global_margin(COST_MODEL) > 0 else BREAKEVEN_FLAT, 0.0
    mg = profit / amt
    return round(1 / mg, 2), round(mg, 4)



def _metrics(imp, clk, cost, ord_, amt, cart):
    imp, clk, cost, ord_, amt, cart = _i(imp), _i(clk), _f(cost), _i(ord_), _f(amt), _i(cart)
    return {
        "imp": imp, "clk": clk, "cost": round(cost, 2), "ord": ord_, "amt": round(amt, 2),
        "cart": cart,
        "roi": round(amt / cost, 2) if cost else 0.0,
        "ctr": round(clk / imp * 100, 2) if imp else 0.0,
        "cpc": round(cost / clk, 2) if clk else 0.0,
        "cvr": round(ord_ / clk * 100, 2) if clk else 0.0,
        "cpa": round(cost / ord_, 2) if ord_ else 0.0,
        "cartRate": round(cart / clk * 100, 2) if clk else 0.0,
        "aov": round(amt / ord_, 2) if ord_ else 0.0,
    }


def _sum_metrics(dicts):
    a = {"imp": 0, "clk": 0, "cost": 0.0, "ord": 0, "amt": 0.0, "cart": 0}
    for d in dicts:
        a["imp"] += _i(d.get("imp") or d.get("impressions"))
        a["clk"] += _i(d.get("clk") or d.get("clicks"))
        a["cost"] += _f(d.get("cost"))
        a["ord"] += _i(d.get("ord") or d.get("totalOrderCnt"))
        a["amt"] += _f(d.get("amt") or d.get("totalOrderSum"))
        a["cart"] += _i(d.get("cart") or d.get("totalCartCnt"))
    return _metrics(a["imp"], a["clk"], a["cost"], a["ord"], a["amt"], a["cart"])


def _empty():
    return _metrics(0, 0, 0, 0, 0, 0)


# ---------------- 载入 ----------------
def _load(name, account):
    return store.load(name, account)


def _sz_rows(df):
    out = []
    for r in (df or {}).get("rows", []):
        if r.get("$summary"):
            out.append({"__summary__": True, "skuId": "合计",
                        "amt": _f(r.get(SZ_AMT)), "orders": _f(r.get(SZ_ORDN)),
                        "visitors": _i(r.get(SZ_UV)), "views": _i(r.get(SZ_PV))})
            continue
        out.append({
            "skuId": str(r.get("sku_id")), "name": r.get("name"),
            "img": r.get("img_src"), "url": r.get("pro_url"),
            "amt": _f(r.get(SZ_AMT)), "orders": _f(r.get(SZ_ORDN)), "qty": _f(r.get(SZ_QTY)),
            "visitors": _i(r.get(SZ_UV)), "views": _i(r.get(SZ_PV)),
            "cvr": round(_f(r.get(SZ_CVR)) * 100, 2),
        })
    return out


def _flow_block(df):
    if not df:
        return None
    core = df.get("core") or []
    c = core[0] if core else {}
    return {
        "visitors": _i(c.get(SZ_UV)), "views": _i(c.get(SZ_PV)),
        "amt": round(_f(c.get(SZ_AMT)), 2), "orders": _i(c.get(SZ_ORDN)),
        "cvr": round(_f(c.get(SZ_CVR)) * 100, 2),
        "aov": round(_f(c.get(SZ_AMT)) / _f(c.get(SZ_ORDN)), 2) if _f(c.get(SZ_ORDN)) else 0.0,
    }


def _load_accounts(windows=None, accounts=None):
    """从「按天库」聚合出每个窗口的数据。"""
    from . import dailystore as dstore
    windows = windows or WINDOWS
    accounts = accounts if accounts is not None else config.ACCOUNTS
    accts = []
    for a in accounts:
        k, label = a["key"], a.get("label") or a["key"]
        d = {"wins": {}}
        empty = True
        for wk, s, e, _lab in windows:
            win = {
                "jzt": dstore.load_window(k, "jzt_campaign", s, e),
                "camp": dstore.load_window(k, "jst_campaign", s, e),
                "sku": dstore.load_window(k, "jst_sku", s, e),
                "sw": dstore.load_window(k, "jst_searchword", s, e),
                "kw": dstore.load_window(k, "kw", s, e),
                "sz": _sz_rows(dstore.load_window(k, "sz_product", s, e)),
                "flow": dstore.load_window(k, "sz_flow", s, e),
                "range": [s, e],
            }
            d["wins"][wk] = win
            if win["jzt"] or win["camp"] or win["sku"]:
                empty = False
        accts.append({"key": k, "label": label, "accountId": a.get("account_id"),
                      "ds": d, "hasData": not empty})
    return accts


# ---------------- 账号级总览 ----------------
def account_totals(acct):
    out = {}
    for wk in WKEYS:
        w = acct["ds"]["wins"][wk]
        jzt = w["jzt"] or {}
        ext = jzt.get("ext") or {}
        # 概览缺失时退化为智能投放口径，避免整份报表变成 0（区间数据不全时很常见）
        if jzt.get("rows"):
            m = _sum_metrics(jzt["rows"])
            m["source"] = "概览"
        elif (w["camp"] or {}).get("rows"):
            m = _sum_metrics(w["camp"]["rows"])
            m["source"] = "智能投放(概览缺失)"
        else:
            m = _empty()
            m["source"] = "无数据"
        m["extCost"] = round(_f(ext.get("cost")), 2)
        m["extAmt"] = round(_f(ext.get("totalOrderSum")), 2)
        m["extRoi"] = round(_f(ext.get("totalOrderROI")), 2)
        # 智能投放口径
        m["jst"] = _sum_metrics((w["camp"] or {}).get("rows", [])) if w["camp"] else _empty()
        # 商智店铺口径（合计行）
        sz = w["sz"]
        summ = next((r for r in sz if r.get("__summary__")), None)
        if summ is None and sz:
            # 商智没给「合计」行时（按天聚合也会丢掉汇总行）用各 SKU 加总兜底
            summ = {"amt": sum(_f(r.get("amt")) for r in sz),
                    "orders": sum(_i(r.get("orders")) for r in sz),
                    "visitors": sum(_i(r.get("visitors")) for r in sz),
                    "views": sum(_i(r.get("views")) for r in sz)}
        m["sz"] = ({"amt": round(_f(summ["amt"]), 2), "orders": _i(summ["orders"]),
                    "visitors": _i(summ["visitors"]), "views": _i(summ["views"])}
                   if summ else None)
        m["flow"] = _flow_block(w["flow"])
        if m["sz"] and m["sz"].get("amt"):
            m["adShare"] = round(m["amt"] / m["sz"]["amt"] * 100, 1)
        else:
            m["adShare"] = None
        m["plans"] = len(jzt.get("rows") or [])
        out[wk] = m
    return out


def type_split(accts):
    """按计划类型（智能化/关键词/全站智能）拆三窗口的 花费 与 ROI。"""
    out = {}
    for a in accts:
        per = {}
        for wk in WKEYS:
            by = {}
            for r in (a["ds"]["wins"][wk]["jzt"] or {}).get("rows", []):
                t = TYPE_LABEL.get(_i(r.get("campaignType")), "其他")
                e = by.setdefault(t, {"cost": 0.0, "amt": 0.0, "ord": 0, "n": 0})
                e["cost"] += _f(r.get("cost"))
                e["amt"] += _f(r.get("totalOrderSum"))
                e["ord"] += _i(r.get("totalOrderCnt"))
                e["n"] += 1
            for t, e in by.items():
                e["cost"] = round(e["cost"], 2)
                e["amt"] = round(e["amt"], 2)
                e["roi"] = round(e["amt"] / e["cost"], 2) if e["cost"] else 0.0
            per[wk] = by
        out[a["key"]] = per
    return out


# ---------------- 按 SKU 交叉（跨账号合并广告 + 分店铺商智）----------------
def sku_rows(accts, model, costs=None):
    costs = costs or {}
    units = settings.costs_unit_map()
    aggAd = {wk: defaultdict(lambda: {"cost": 0.0, "amt": 0.0, "ord": 0, "imp": 0,
                                      "clk": 0, "cart": 0, "byAcct": {}, "plans": set()})
             for wk in WKEYS}
    szMap = {wk: {} for wk in WKEYS}

    for a in accts:
        k = a["key"]
        for wk in WKEYS:
            w = a["ds"]["wins"][wk]
            for r in (w["sku"] or {}).get("rows", []):
                sid = str(r.get("skuId") or "")
                if not sid:
                    continue
                t = aggAd[wk][sid]
                c, am, o = _f(r.get("cost")), _f(r.get("totalOrderSum")), _i(r.get("totalOrderCnt"))
                t["cost"] += c
                t["amt"] += am
                t["ord"] += o
                t["imp"] += _i(r.get("impressions"))
                t["clk"] += _i(r.get("clicks"))
                t["cart"] += _i(r.get("totalCartCnt"))
                ac = t["byAcct"].setdefault(k, {"cost": 0.0, "amt": 0.0, "ord": 0, "plans": set()})
                ac["cost"] += c
                ac["amt"] += am
                ac["ord"] += o
                if r.get("campaignName"):
                    ac["plans"].add(r["campaignName"])
                    t["plans"].add(r["campaignName"])
                    t["fromJst"] = True
            # 全站智能推广（campaignType=153）不在智能投放报表里，但它自带 spuId=商智SKU，
            # 用「一计划=一商品」的方式补进来，避免漏掉这部分花费
            for r in (w["jzt"] or {}).get("rows", []):
                if _i(r.get("campaignType")) != 153:
                    continue
                sid = str(r.get("spuId") or r.get("childProNo") or "").strip()
                if not sid or sid == "0":
                    continue
                t = aggAd[wk][sid]
                c, am, o = _f(r.get("cost")), _f(r.get("totalOrderSum")), _i(r.get("totalOrderCnt"))
                t["cost"] += c
                t["amt"] += am
                t["ord"] += o
                t["imp"] += _i(r.get("impressions"))
                t["clk"] += _i(r.get("clicks"))
                t["cart"] += _i(r.get("totalCartCnt"))
                t["fromPlan"] = True
                ac = t["byAcct"].setdefault(k, {"cost": 0.0, "amt": 0.0, "ord": 0, "plans": set()})
                ac["cost"] += c
                ac["amt"] += am
                ac["ord"] += o
                if r.get("campaignName"):
                    ac["plans"].add(r["campaignName"])
                    t["plans"].add(r["campaignName"])
            for s in w["sz"]:
                if s.get("__summary__"):
                    continue
                sid = s["skuId"]
                e = szMap[wk].setdefault(sid, {"name": s["name"], "img": s.get("img"),
                                               "url": s.get("url"), "amt": 0.0, "orders": 0,
                                               "qty": 0.0,
                                               "visitors": 0, "views": 0, "shops": {}})
                e["amt"] += s["amt"]
                e["orders"] += s["orders"]
                e["qty"] += _f(s.get("qty"))
                e["visitors"] += s["visitors"]
                e["views"] += s["views"]
                e["shops"][k] = e["shops"].get(k, 0.0) + s["amt"]
                if not e["name"] and s.get("name"):
                    e["name"] = s["name"]

    # 主表只保留「有广告花费」的 SKU —— 商智里上百个 SKU 大多没投广告，全列出来没有决策价值
    ids = set()
    for wk in WKEYS:
        for sid, v in aggAd[wk].items():
            if _f(v["cost"]) > 0:
                ids.add(sid)
    labelOf = {a["key"]: a["label"] for a in accts}

    rows = []
    for sid in ids:
        per = {}
        for wk in WKEYS:
            ad = aggAd[wk].get(sid)
            m = _metrics(ad["imp"], ad["clk"], ad["cost"], ad["ord"], ad["amt"], ad["cart"]) if ad else _empty()
            sz = szMap[wk].get(sid)
            if sz:
                m["szAmt"] = round(sz["amt"], 2)
                m["szVisitors"] = sz["visitors"]
                m["szViews"] = sz["views"]
                m["szOrders"] = sz["orders"]
                m["szQty"] = round(_f(sz.get("qty")), 1)
                m["szCvr"] = round(sz["orders"] / sz["visitors"] * 100, 2) if sz["visitors"] else 0.0
            else:
                m["szAmt"] = 0.0
                m["szVisitors"] = m["szViews"] = m["szOrders"] = m["szQty"] = 0
                m["szCvr"] = 0.0
            share = round(m["amt"] / m["szAmt"] * 100, 1) if m["szAmt"] else None
            # 广告成交 > 店铺成交 说明两个口径期间不一致（归因跨期/商智无该SKU），此时渗透率不可比
            m["adShare"] = share if (share is not None and share <= 110) else None
            m["adShareRaw"] = share
            m["naturalAmt"] = round(max(m["szAmt"] - m["amt"], 0.0), 2)
            per[wk] = m

        cur = per["w1"]
        szInfo = szMap["w1"].get(sid) or szMap["w2"].get(sid) or szMap["w0"].get(sid) or {}
        plans_any = (aggAd["w1"].get(sid, {}).get("plans") or aggAd["w2"].get(sid, {}).get("plans")
                     or aggAd["w0"].get(sid, {}).get("plans") or [])
        campaign = sorted(plans_any)[0] if plans_any else ""
        name = szInfo.get("name") or (f"{campaign} · {sid[-6:]}" if campaign else f"SKU {sid}")
        aovRef = 0.0
        for wk in WKEYS:
            if per[wk]["szAmt"] and per[wk]["szOrders"]:
                aovRef = per[wk]["szAmt"] / per[wk]["szOrders"]
                break
        if not aovRef:
            for wk in ("w2", "w1", "w0"):
                if per[wk]["amt"] and per[wk]["ord"]:
                    aovRef = per[wk]["amt"] / per[wk]["ord"]
                    break
        # 商智件数（qty）才是 ERP 成本项的分母；一件 multi-pack 的 2~3 倍很常见
        unit_price = 0.0
        for e in (szMap["w1"].get(sid), szMap["w2"].get(sid), szMap["w0"].get(sid)):
            if e and _f(e.get("qty")) > 0:
                unit_price = round(_f(e["amt"]) / _f(e["qty"]), 2)
                break
        cost = sku_cost_of(sid, unit_price or aovRef, model, costs, units)
        # 两条自动体检：
        #   ① 到手结算价 ≥ 前台件单价：不可能成立（到手结算价是消费者实付里的一部分），多半是促销价/期间口径不一致
        #   ② ERP 件数 ÷ 商智件数 偏离 1：仅作参考。实测换算系数取 1 才成立
        #      （若按倍数换算，到手结算价会超过消费者实付），说明 ERP 的 _qty 含了其他渠道，不动成本
        if cost.get("costSource") == "erp":
            erp_q = _f(cost.get("erpQty"))
            # 接口是按「所选区间」要的均值（= w2），所以件数也只能跟 w2 比，跨三期比会得到 0.3 这种假异常
            sz_q = _f((szMap["w2"].get(sid) or {}).get("qty"))
            ratio = round(erp_q / sz_q, 2) if (erp_q > 0 and sz_q > 0) else 0.0
            if ratio:
                cost["qtyRatio"] = ratio
            warn = []
            if _f(cost.get("supply")) >= _f(cost.get("aov")) > 0:
                cost["supplyOverPrice"] = True
                warn.append(f"到手结算价 ¥{_f(cost.get('supply'))} ≥ 前台件单价 ¥{_f(cost.get('aov'))}，"
                            f"成本或售价口径待确认（是否为促销价/跨期）")
            # 入仓品的 _qty 是「全部记录」汇总（接口不吃 date_from/date_to），
            # 跟本区间的商智件数没有可比性，只对出库口径做这个体检
            if ratio >= 1.3 and _f(cost.get("unitFactor")) <= 1 and cost.get("erpSource") == "sale":
                warn.append(f"ERP 出库件数为该区间商智京东件数的 {ratio} 倍（已按 1:1 计，疑含其他渠道销量）")
            cost["unitWarn"] = "；".join(warn)
        est = {}
        for wk in WKEYS:
            m = per[wk]
            # 商智口径不可比时，至少用广告成交额兜底，避免把净利算成纯负数
            gmv = max(_f(m["szAmt"]), _f(m["amt"]))
            est[wk] = {
                "grossProfit": round(gmv * cost["margin"], 2),
                "adCost": round(m["cost"], 2),
                "gmv": round(gmv, 2),
                "netProfit": round(gmv * cost["margin"] - m["cost"], 2),
            }
        byAcct = {}
        for ak, v in (aggAd["w1"].get(sid, {}).get("byAcct") or {}).items():
            byAcct[ak] = {"label": labelOf.get(ak, ak), "cost": round(v["cost"], 2),
                          "amt": round(v["amt"], 2), "ord": v["ord"],
                          "roi": round(v["amt"] / v["cost"], 2) if v["cost"] else 0.0,
                          "plans": sorted(v["plans"])}
        rows.append({
            "skuId": sid, "name": name, "img": szInfo.get("img"), "url": szInfo.get("url"),
            "aov": round(aovRef, 2), "margin": cost["margin"], "breakeven": cost["breakeven"],
            "grossPerOrder": cost["grossPerOrder"],
            "unitPrice": unit_price or round(aovRef, 2), "growLine": cost.get("growLine") or 0.0,
            "costSource": cost.get("costSource") or "store", "costParams": cost,
            "wins": per, "est": est, "byAccount": byAcct,
            "shops": {labelOf.get(k2, k2): round(v, 2) for k2, v in (szInfo.get("shops") or {}).items()},
            "adAccounts": sorted((aggAd["w1"].get(sid, {}).get("byAcct") or {}).keys()),
            "fromPlan": bool((aggAd["w1"].get(sid) or aggAd["w2"].get(sid) or aggAd["w0"].get(sid) or {}).get("fromPlan")),
            "fromJst": bool(((aggAd["w1"].get(sid) or {}).get("fromJst"))),
            "plans": sorted(aggAd["w1"].get(sid, {}).get("plans", [])) if aggAd["w1"].get(sid) else
                     sorted(aggAd["w2"].get(sid, {}).get("plans", [])) if aggAd["w2"].get(sid) else
                     sorted(aggAd["w0"].get(sid, {}).get("plans", [])),
        })
    for r in rows:
        r["verdict"] = _verdict(r)
    rows.sort(key=lambda r: -max(r["wins"][wk]["cost"] for wk in WKEYS))

    # 高成交但完全没投广告的商品（机会清单）
    no_ad = []
    for sid, v in (szMap["w2"] or szMap["w1"]).items():
        if sid in ids:
            continue
        e = {"skuId": sid, "name": v["name"], "img": v.get("img"), "url": v.get("url"),
             "szAmt": round(v["amt"], 2), "szOrders": v["orders"], "szVisitors": v["visitors"],
             "cvr": round(v["orders"] / v["visitors"] * 100, 2) if v["visitors"] else 0.0}
        if e["szAmt"] > 0:
            no_ad.append(e)
    no_ad.sort(key=lambda x: -x["szAmt"])
    return rows, no_ad[:20]


def _cost_coverage(skus: list, costs: dict) -> dict:
    """报表要回答「真实成本覆盖了多少花费」，否则读者会把回落全店口径的 SKU 也当成真实结论。"""
    def _own(s):
        w = s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"]
        return _f(w["cost"])
    tot = sum(_own(s) for s in skus)
    erp = [s for s in skus if s.get("costSource") == "erp"]
    erp_cost = sum(_own(s) for s in erp)
    zero = [s for s in erp if _f(s.get("breakeven")) <= 0]
    warn = [{"skuId": s["skuId"], "name": s["name"], "warn": (s.get("costParams") or {}).get("unitWarn")}
            for s in erp if (s.get("costParams") or {}).get("unitWarn")]
    missing = [{"skuId": s["skuId"], "name": s["name"], "cost": _own(s)}
               for s in skus if s.get("costSource") != "erp" and _own(s) > 0]
    missing.sort(key=lambda x: -x["cost"])
    # 「前台成交额 → 到手结算价」的比例：京准通 ROI 的分子是前者，我们赚的是后者差额，
    # 报表必须把这层差价摊开，否则读者会把 24 元的前台件单价当成我们的收入。
    gmv = settle = contrib = 0.0
    for s in erp:
        w = s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"]
        g = _f(w.get("szAmt")) or _f(w["amt"])
        cp = s.get("costParams") or {}
        if g > 0 and _f(cp.get("settleRate")) > 0:
            gmv += g
            settle += g * _f(cp.get("settleRate"))
            contrib += g * _f(cp.get("margin"))
    return {
        "skuTotal": len(skus), "skuErp": len(erp), "skuStore": len(skus) - len(erp),
        "costErp": round(erp_cost, 2), "costTotal": round(tot, 2),
        "costShare": round(erp_cost / tot * 100, 1) if tot else 0.0,
        "gmvErp": round(gmv, 2), "settleAmt": round(settle, 2),
        "settleRate": round(settle / gmv, 4) if gmv else 0.0,
        "breakevenSettle": round(settle / contrib, 2) if contrib > 0 else 0.0,
        "negativeMargin": [{"skuId": s["skuId"], "name": s["name"],
                            "cost": _own(s), "grossPerOrder": s.get("grossPerOrder")}
                           for s in zero],
        "unitWarn": warn, "missing": missing[:40],
        "missingCost": round(sum(x["cost"] for x in missing), 2),
        "skusInErp": len((costs or {}).get("skus") or {}),
    }


def _verdict(r):
    w0, w1, w2 = r["wins"]["w0"], r["wins"]["w1"], r["wins"]["w2"]
    # 每个 SKU 用自己那条保本线；没接入真实成本的，sku_cost_of 已回落成全店保本线。
    # 注意 be==0 是有意义的（到手结算价低于成本 → 保本线不存在），不能当成「没值」
    be = _f(r.get("breakeven"))
    if (r.get("costSource") == "erp") and be <= 0:
        cp = r.get("costParams") or {}
        return {"tier": "严重亏损", "score": 4,
                "reason": (f"ERP 到手结算价 ¥{_f(cp.get('supply'))} 低于单件成本"
                           f"（货款 {_f(cp.get('productCost'))}+运费 {_f(cp.get('shipping'))}"
                           f"+包材 {_f(cp.get('package'))}+人工 {_f(cp.get('labor'))}），"
                           f"卖一件亏一件，ROI {r['wins']['w2']['roi'] if r['wins']['w2']['cost'] > 0 else r['wins']['w1']['roi']} 无意义")}
    be = be or BREAKEVEN_FLAT
    grow = _f(r.get("growLine")) or round(be * GROW_FACTOR, 2)
    latest = w2 if w2["cost"] > 0 else w1
    roi = latest["roi"]
    adCost = latest["cost"]
    net = r["est"]["w2" if w2["cost"] > 0 else "w1"]["netProfit"]
    if adCost <= 0:
        return {"tier": "未投放", "score": 9, "reason": "本周无广告花费"}
    if roi >= grow:
        if net <= 0:
            return {"tier": "优化", "score": 1,
                    "reason": f"ROI {roi} 已达放量线 {grow}，但按本 SKU 真实毛利扣广告费后仍为负，先压 CPC 再放量"}
        return {"tier": "放大", "score": 0,
                "reason": f"ROI {roi} 高于本 SKU 放量线 {grow}（保本 {be}），且扣广告费后为正"}
    if roi >= be:
        return {"tier": "优化", "score": 1,
                "reason": f"ROI {roi} 在本 SKU 保本 {be} 之上、未到放量线 {grow}，仍有抠词/提效空间"}
    if net > 0:
        return {"tier": "止损", "score": 2,
                "reason": f"ROI {roi} < 本 SKU 保本 {be}，广告在亏，但自然成交兜住了整体利润"}
    return {"tier": "严重亏损", "score": 3,
            "reason": f"ROI {roi} < 本 SKU 保本 {be}，且扣掉广告费后整体估算为负"}


# ---------------- 计划级 ----------------
def plan_rows(accts, model, skus=None):
    """计划级分层。计划可能同时投多个 SKU，所以保本线用「该计划内各 SKU 按成交额加权的毛利率」，
    而不是全店一条线；SKU 里查不到的部分才落回全店毛利率。返回 (out, beOfPlan)。"""
    marginOf = {s["skuId"]: _f(s.get("margin")) for s in (skus or [])}
    srcOf = {s["skuId"]: (s.get("costSource") or "store") for s in (skus or [])}
    skuAmt = defaultdict(dict)        # (account, campaignId) -> {skuId: 成交额}
    planBe = {}
    out = []
    for a in accts:
        k, label = a["key"], a["label"]
        ids = {}
        for wk in WKEYS:
            w = a["ds"]["wins"][wk]
            for r in (w["sku"] or {}).get("rows", []):
                cid, sid = str(r.get("campaignId")), str(r.get("skuId") or "")
                if sid:
                    e = skuAmt[(k, cid)].setdefault(sid, 0.0)
                    skuAmt[(k, cid)][sid] = e + _f(r.get("totalOrderSum"))
            for r in (w["jzt"] or {}).get("rows", []):
                if _i(r.get("campaignType")) != 153:
                    continue
                cid = str(r.get("campaignId"))
                sid = str(r.get("spuId") or r.get("childProNo") or "").strip()
                if sid and sid != "0":
                    e = skuAmt[(k, cid)].setdefault(sid, 0.0)
                    skuAmt[(k, cid)][sid] = e + _f(r.get("totalOrderSum"))
            for r in (w["jzt"] or {}).get("rows", []):
                cid = str(r.get("campaignId"))
                e = ids.setdefault(cid, {"campaignId": cid, "name": r.get("campaignName"),
                                         "type": _i(r.get("campaignType")), "wins": {}})
                e["name"] = e["name"] or r.get("campaignName")
                e["wins"][wk] = _metrics(r.get("impressions"), r.get("clicks"), r.get("cost"),
                                         r.get("totalOrderCnt"), r.get("totalOrderSum"),
                                         r.get("totalCartCnt"))
            for r in (w["camp"] or {}).get("rows", []):
                cid = str(r.get("campaignId"))
                e = ids.setdefault(cid, {"campaignId": cid, "name": r.get("campaignName"),
                                         "type": None, "wins": {}})
                e["jstWins"] = e.get("jstWins") or {}
                e["jstWins"][wk] = _metrics(r.get("impressions"), r.get("clicks"), r.get("cost"),
                                            r.get("totalOrderCnt"), r.get("totalOrderSum"),
                                            r.get("totalCartCnt"))
            for r in (w["kw"] or {}).get("rows", []):
                cid = str(r.get("campaignId"))
                e = ids.setdefault(cid, {"campaignId": cid, "name": r.get("campaignName"),
                                         "type": None, "wins": {}})
        for cid, e in ids.items():
            for wk in WKEYS:
                e["wins"].setdefault(wk, _empty())
                if e.get("jstWins"):
                    e["jstWins"].setdefault(wk, _empty())
            latest = e["wins"]["w2"] if e["wins"]["w2"]["cost"] > 0 else e["wins"]["w1"]
            # 该计划的保本线 = 计划内各 SKU 按成交额加权的毛利率的倒数
            pairs = skuAmt.get((k, cid)) or {}
            sm = global_margin(model)
            amt_tot = sum(pairs.values())
            prof = sum(a * _f(marginOf.get(s), sm) for s, a in pairs.items())
            pmargin = round(prof / amt_tot, 4) if amt_tot > 0 else round(sm, 4)
            be = round(1 / pmargin, 2) if pmargin > 0 else 0.0
            planBe[(k, cid)] = be
            grow = round(be * GROW_FACTOR, 2) if be else 0.0
            roi = latest["roi"]
            if latest["cost"] <= 0:
                tier = "未投放"
            elif be <= 0:
                tier = "严重亏损"          # 计划内的货卖一件亏一件，广告只是放大亏损
            elif roi >= grow:
                tier = "放大"
            elif roi >= be:
                tier = "优化"
            else:
                tier = "止损"
            e.update({"account": k, "accountLabel": label, "typeLabel": TYPE_LABEL.get(e["type"], "其他"),
                      "tier": tier, "breakeven": be, "growLine": grow, "planMargin": pmargin,
                      "costMapped": bool(pairs) and any(srcOf.get(s) == "erp" for s in pairs),
                      "deltaRoi": round(e["wins"]["w1"]["roi"] - e["wins"]["w0"]["roi"], 2),
                      "deltaCost": round(e["wins"]["w1"]["cost"] - e["wins"]["w0"]["cost"], 2)})
            out.append(e)
    out.sort(key=lambda p: (-p["wins"]["w1"]["cost"] - p["wins"]["w2"]["cost"],))
    return out, planBe


# ---------------- 搜索词 / 关键词 ----------------
def _terms(df, key):
    out = defaultdict(lambda: {"imp": 0, "clk": 0, "cost": 0.0, "ord": 0, "amt": 0.0, "cart": 0})
    for r in (df or {}).get("rows", []):
        w = r.get(key)
        if w is None:
            continue
        # 搜索词接口不返回 campaignId，只有 campaignName，因此统一用计划名做键
        cid = str(r.get("campaignName") or r.get("campaignId") or "未命名计划")
        t = out[(cid, w)]
        t["imp"] += _i(r.get("impressions"))
        t["clk"] += _i(r.get("clicks"))
        t["cost"] += _f(r.get("cost"))
        t["ord"] += _i(r.get("totalOrderCnt"))
        t["amt"] += _f(r.get("totalOrderSum"))
        t["cart"] += _i(r.get("totalCartCnt"))
    return out


def word_rows(accts, model, beByName=None):
    """词级分层。词报表只有计划名，所以保本线按「该计划内 SKU 加权保本线」取；
    计划里没匹配到任何真实成本 SKU 时退回全店线。"""
    beByName = beByName or {}
    per_account = {}
    for a in accts:
        k = a["key"]
        res = {}
        for wk in WKEYS:
            w = a["ds"]["wins"][wk]
            sw = _terms(w["sw"], "searchTerm")
            kw = _terms(w["kw"], "keywordName")
            byPlan = defaultdict(lambda: {"sw": [], "kw": [], "totCost": 0.0})
            for (cid, word), t in sw.items():
                be = _f(beByName.get((k, cid))) or BREAKEVEN_FLAT
                byPlan[cid]["sw"].append(_word_dict(word, t, be))
                byPlan[cid]["totCost"] += t["cost"]
            for (cid, word), t in kw.items():
                be = _f(beByName.get((k, cid))) or BREAKEVEN_FLAT
                byPlan[cid]["kw"].append(_word_dict(word, t, be))
            for cid, v in byPlan.items():
                for key in ("sw", "kw"):
                    v[key].sort(key=lambda x: -x["cost"])
            res[wk] = byPlan
        per_account[k] = res
    return per_account


def _word_dict(word, t, be):
    roi = t["amt"] / t["cost"] if t["cost"] else 0.0
    kind = "other"
    if t["ord"] >= GOOD_MIN_ORD and t["cost"] > 0 and roi > be:
        kind = "good"
    if t["cost"] >= WASTE_MIN and t["ord"] == 0:
        kind = "waste"
    if t["cost"] >= LOW_MIN and t["ord"] > 0 and roi < be:
        kind = "low"
    return {"word": word, "cost": round(t["cost"], 2), "amt": round(t["amt"], 2),
            "ord": t["ord"], "clk": t["clk"], "imp": t["imp"],
            "roi": round(roi, 2), "kind": kind, "be": round(be, 2)}


# ---------------- 操作日志 ----------------
_SYS = ("系统操作", "自动提升预算")
# 这些操作偏"文案/配置"，对投产比影响很小，不进入复盘时间线（仍计入操作总数）
LOW_VALUE_OPS = ("修改计划名称", "推广计划名称修改", "推广单元名称修改", "开启稳赚计划",
                 "关闭稳赚计划", "添加创意", "创建单元", "添加商品", "编辑黑名单SKU",
                 "更新补贴券状态", "修改创意")


def _decode(s):
    return (str(s or "").replace("&ldquo;", '"').replace("&rdquo;", '"').replace("&quot;", '"')
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&nbsp;", " ")
            .replace("<<<LOG_SEPARATION>>>", " ｜ ").replace("&lsquo;", "'").replace("&rsquo;", "'"))


def _clean(s):
    return re.sub(r"\s+", " ", _decode(s)).strip()


def op_rows(accts, start=None, end=None):
    """操作日志：优先从按天库读区间内的记录（读不到再退回老的 data/oplog.json）。"""
    from . import dailystore as dstore
    out = []
    for a in accts:
        k, label = a["key"], a["label"]
        rows, seen = [], set()
        if start and end:
            for day in dstore.day_span(start, end):
                payload = dstore.load_day(k, day, "oplog")
                for r in (payload or {}).get("rows", []):
                    key = "|".join(str(r.get(x)) for x in
                                   ("optTime", "actionObjectId", "operationContent", "operationDetails"))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(r)
        if not rows:
            d = store.load("oplog", k)
            rows = (d or {}).get("rows", [])
        for r in rows:
            t = str(r.get("optTime") or "")
            content = r.get("operationContent") or ""
            operator = r.get("operator") or ""
            manual = operator not in _SYS
            out.append({
                "account": k, "accountLabel": label, "time": t, "day": t[:10],
                "level": r.get("_level"), "content": content,
                "target": r.get("actionObject"), "targetId": str(r.get("actionObjectId") or ""),
                "operator": operator, "manual": manual,
                "detail": _clean(r.get("operationDetails"))[:600],
                "from": r.get("requestFrom"),
                "kind": _op_kind(content, r.get("operationDetails") or ""),
            })
    out.sort(key=lambda x: x["time"])
    return out


def _op_kind(content, detail):
    c = content or ""
    d = detail or ""
    if "自动提升预算" in c:
        return "autoBudget"
    if "删除计划" in c:
        return "delete"
    if "创建计划" in c:
        return "create"
    if "暂停" in d or "状态修改" in c:
        return "status"
    if "否定词" in c or "否定" in d:
        return "negative"
    if "出价" in c or "投产比" in d or "成本控制" in d or "溢价" in d:
        return "bid"
    if "预算" in c:
        return "budget"
    if "创意" in c or "商品" in c:
        return "creative"
    if "关键词" in c:
        return "keyword"
    return "other"


# ---------------- 调整复盘（日粒度前后对比）----------------
def _daily_series(account: str, cid, start: str, end: str) -> dict:
    """从按天库取某个计划在 [start,end] 的逐日表现。"""
    from . import dailystore as dstore
    out = {}
    for day in dstore.day_span(start, end):
        payload = dstore.load_day(account, day, "jst_campaign")
        if not payload:
            continue
        cost = amt = 0.0
        ordv = clk = imp = 0
        found = False
        for r in payload.get("rows", []):
            if str(r.get("campaignId")) != str(cid):
                continue
            found = True
            cost += _f(r.get("cost"))
            amt += _f(r.get("totalOrderSum"))
            ordv += _i(r.get("totalOrderCnt"))
            clk += _i(r.get("clicks"))
            imp += _i(r.get("impressions"))
        if found:
            out[day] = {"cost": cost, "amt": amt, "ord": ordv, "clk": clk, "imp": imp}
    return out


def _slice(series, start, end):
    days = [d for d in series if start <= d <= end]
    v = {"cost": 0.0, "amt": 0.0, "ord": 0, "clk": 0, "imp": 0}
    for d in days:
        for k2 in v:
            v[k2] += series[d][k2]
    n = max(len(days), 1)
    return {"days": len(days), "cost": round(v["cost"], 2), "amt": round(v["amt"], 2),
            "ord": v["ord"], "clk": v["clk"], "imp": v["imp"],
            "roi": round(v["amt"] / v["cost"], 2) if v["cost"] else 0.0,
            "dailyCost": round(v["cost"] / n, 2), "dailyAmt": round(v["amt"] / n, 2),
            "dailyOrd": round(v["ord"] / n, 2)}


def _wk_slice(p, wk):
    m = p["wins"][wk]
    return {"days": 7, "cost": round(_f(m["cost"]), 2), "amt": round(_f(m["amt"]), 2),
            "ord": _i(m["ord"]), "clk": _i(m["clk"]), "imp": _i(m["imp"]),
            "roi": round(_f(m["roi"]), 2),
            "dailyCost": round(_f(m["cost"]) / 7, 2), "dailyAmt": round(_f(m["amt"]) / 7, 2),
            "dailyOrd": round(_i(m["ord"]) / 7, 2)}


def adjustments(accts, ops, plans, windows=None, span_from=None, span_to=None):
    """把人工调整与「调整前7天 / 调整后7天」效果对齐，给出判定。"""
    windows = windows or WINDOWS
    planIdx = {}
    for p in plans:
        planIdx[(p["account"], p["campaignId"])] = p

    out = []
    for op in ops:
        if not op["manual"]:
            continue
        if op["level"] != "campaign":
            continue
        if span_from and op["day"] < span_from:
            continue
        if span_to and op["day"] > span_to:
            continue
        if any(k in (op["content"] or "") for k in LOW_VALUE_OPS):
            continue
        acct = next((a for a in accts if a["key"] == op["account"]), None)
        if not acct:
            continue
        # 通过 campaignId 匹配；否则用名称匹配
        cid, pname = op["targetId"], op["target"]
        p = planIdx.get((op["account"], cid))
        if not p:
            p = next((x for x in plans if x["account"] == op["account"] and
                      (x["name"] == pname or (pname and pname in (x["name"] or ""))
                       or (x["name"] and x["name"] in (pname or "")))), None)
        day = op["day"]
        if day:
            dd = _dt.date.fromisoformat(day)
            s_lo = (dd - _dt.timedelta(days=8)).isoformat()
            s_hi = (dd + _dt.timedelta(days=8)).isoformat()
        else:
            s_lo = s_hi = ""
        series = _daily_series(op["account"], cid, s_lo, s_hi) if day else {}
        if not series and p is not None and day:
            series = _daily_series(op["account"], p["campaignId"], s_lo, s_hi)
        win = None
        if series and day:
            d0 = _dt.date.fromisoformat(day)
            before = _slice(series, (d0 - _dt.timedelta(days=7)).isoformat(), (d0 - _dt.timedelta(days=1)).isoformat())
            after = _slice(series, (d0 + _dt.timedelta(days=1)).isoformat(), (d0 + _dt.timedelta(days=7)).isoformat())
            win = "日粒度"
            if before["days"] < 3 or after["days"] < 3:
                before = after = None
        else:
            before = after = None
        # 没有日粒度数据（例如「全站智能推广」不在智能投放日报里）→ 退化为窗口前后对比
        if before is None and p is not None and day:
            idx = None
            for i, (wkey, ws, we, _l) in enumerate(windows):
                if ws <= day <= we:
                    idx = i
                    break
            if idx is None:
                idx = len(windows) - 1 if day > windows[-1][2] else 0
            before_i = max(idx - 1, 0)
            after_i = idx if idx == len(windows) - 1 else idx + 1
            before = _wk_slice(p, windows[before_i][0])
            after = _wk_slice(p, windows[after_i][0])
            win = f"窗口 {windows[before_i][0]}→{windows[after_i][0]}"
        verdict, note = _judge(op, before, after, p)
        out.append({**op, "plan": p["name"] if p else op["target"],
                    "campaignId": p["campaignId"] if p else cid,
                    "before": before, "after": after, "basis": win,
                    "verdict": verdict, "note": note,
                    "type": (p or {}).get("typeLabel")})
    out.sort(key=lambda x: x["time"])
    return out


def _judge(op, before, after, plan):
    if not before or not after or before["days"] < 3 or after["days"] < 3:
        return "无法评估", "该计划缺少足够的日粒度数据（新计划或被删除）"
    be = _f((plan or {}).get("breakeven")) or BREAKEVEN_FLAT
    dr = after["roi"] - before["roi"]
    dc = after["dailyCost"] - before["dailyCost"]
    da = after["dailyAmt"] - before["dailyAmt"]
    kind = op["kind"]
    if before["cost"] <= 0 and after["cost"] <= 0:
        return "无花费", "该计划在调整前后都没有广告花费，无法评估效果"
    # 调整前没有基线花费（新建计划 / 长期暂停）时，不能把「0 → X」当成提效
    if before["cost"] <= 1 and kind != "delete":
        m0 = re.search(r'由[“"\']?([^”"\']+)[”"\']?修改为[“"\']?([^”"\']+)[“"\']?', op["detail"] or "")
        revived = bool(m0 and "暂停" in m0.group(1) and ("有效" in m0.group(2) or "启动" in m0.group(2)))
        if revived:
            if after["roi"] >= be:
                return "正确", f"重启后 ROI {after['roi']} 已达保本 {be}（重启前处于暂停、无花费）"
            return "存疑", f"重启后 ROI {after['roi']} 仍低于保本 {be}，日均花费 ¥{after['dailyCost']:.0f}"
        return "无基线", "调整前 7 天该计划没有花费（新建或长期未投），无法与调整后对比"
    if kind == "delete":
        return "已止损", (f"删除前日均花费 ¥{before['dailyCost']:.0f}、ROI {before['roi']}；"
                          f"删除后不再产生费用")
    if kind == "status":
        m = re.search(r'由[“"\']?([^”"\']+)[”"\']?修改为[“"\']?([^”"\']+)[”"\']?', op["detail"] or "")
        to = m.group(2).strip() if m else ""
        frm = m.group(1).strip() if m else ""
        if "暂停" in to or "删除" in to:
            return "已止损", f"暂停前日均花费 ¥{before['dailyCost']:.0f}、ROI {before['roi']}"
        if "暂停" in frm and ("有效" in to or "启动" in to):
            if after["roi"] >= be or (after["roi"] > before["roi"] and after["dailyAmt"] > before["dailyAmt"]):
                return "正确", (f"重启后日均成交 ¥{before['dailyAmt']:.0f} → ¥{after['dailyAmt']:.0f}，"
                                f"ROI {before['roi']} → {after['roi']}")
            return "存疑", f"重启后 ROI {before['roi']} → {after['roi']}，尚未达到保本 {be}"
        return "待观察", f"状态变更后 ROI {before['roi']} → {after['roi']}"
    if kind == "negative":
        if before["roi"] < be and after["roi"] >= be:
            return "正确", f"否定后 ROI {before['roi']} → {after['roi']}，由亏转平"
        if dr > 0:
            return "有效但不彻底", f"否定后 ROI 提升 {dr:+.2f}（{before['roi']} → {after['roi']}）"
        return "存疑", f"否定后 ROI 未改善（{before['roi']} → {after['roi']}）"
    if kind == "bid":
        low = "投产比" in op["detail"] and "下调" in op["detail"] or "由" in op["detail"]
        if dr >= 0.3 and da >= 0:
            return "正确", f"调价后 ROI {before['roi']} → {after['roi']}（{dr:+.2f}），日均成交 ¥{before['dailyAmt']:.0f} → ¥{after['dailyAmt']:.0f}"
        if dr <= -0.3:
            return "有问题", f"调价后 ROI 掉 {dr:+.2f}（{before['roi']} → {after['roi']}），日均花费 ¥{before['dailyCost']:.0f} → ¥{after['dailyCost']:.0f}"
        return "效果不明显", f"调价后 ROI {before['roi']} → {after['roi']}，变化不大"
    if kind == "budget":
        if after["roi"] >= be and da > 0:
            return "正确", f"加预算后日均成交 +¥{da:.0f}，ROI 仍 {after['roi']}（≥保本 {be}）"
        if after["roi"] < be:
            return "有问题", f"加预算后 ROI 掉到 {after['roi']}（<保本 {be}），日均花费 +¥{dc:.0f}"
        return "效果不明显", f"预算调整后 ROI {before['roi']} → {after['roi']}"
    if kind == "creative":
        if dr >= 0.3:
            return "正确", f"改创意后 ROI {before['roi']} → {after['roi']}"
        if dr <= -0.3:
            return "有问题", f"改创意后 ROI {before['roi']} → {after['roi']}"
        return "效果不明显", f"改创意后 ROI {before['roi']} → {after['roi']}"
    if kind == "create":
        return "观察中", "新建计划，待积累数据"
    return "待观察", f"调整后 ROI {before['roi']} → {after['roi']}"


def _coverage_block(active, wins):
    """每个窗口实际有几天数据 —— 用于提示「区间里有空洞」。"""
    from . import dailystore as dstore
    out = {}
    for a in active:
        per = {}
        for wk, s, e, _lab in wins:
            want = len(dstore.day_span(s, e))
            jzt = dstore.load_window(a["key"], "jzt_campaign", s, e)
            got = len(jzt.get("days", [])) if jzt else 0
            per[wk] = {"want": want, "got": got, "complete": got >= want}
        out[a["key"]] = per
    return out


# ---------------- 决策建议 ----------------
def _acct_label(accts) -> str:
    """按实际参与分析的账号生成措辞，避免只选一个账号时还写「两账号」。"""
    labels = [a.get("label") or a.get("key") for a in accts]
    if len(labels) == 2:
        return "两账号"
    if len(labels) == 1:
        return labels[0]
    return "、".join(labels)


def _op_span(ops) -> str:
    days = sorted({o["day"] for o in ops if o["day"]})
    return f"{days[0]} ~ {days[-1]}" if days else "窗口内"


def build_advice(accts, totals, skus, plans, ops, words, adj):
    combined = {}
    for wk in WKEYS:
        ms = [totals[a["key"]][wk] for a in accts if a["hasData"]]
        combined[wk] = _metrics(sum(m["imp"] for m in ms), sum(m["clk"] for m in ms),
                                sum(m["cost"] for m in ms), sum(m["ord"] for m in ms),
                                sum(m["amt"] for m in ms), sum(m["cart"] for m in ms))
        jst = [m["jst"] for m in ms]
        combined[wk]["jst"] = _metrics(sum(m["imp"] for m in jst), sum(m["clk"] for m in jst),
                                       sum(m["cost"] for m in jst), sum(m["ord"] for m in jst),
                                       sum(m["amt"] for m in jst), sum(m["cart"] for m in jst))
        szAmt = sum((m["sz"] or {}).get("amt", 0.0) for m in ms)
        combined[wk]["szAmt"] = round(szAmt, 2)
        combined[wk]["adShare"] = round(combined[wk]["amt"] / szAmt * 100, 1) if szAmt else None

    c0, c1, c2 = combined["w0"], combined["w1"], combined["w2"]
    manual = [o for o in ops if o["manual"]]
    right = [o for o in adj if o["verdict"] == "正确"]
    wrong = [o for o in adj if o["verdict"] == "有问题"]
    stop = [s for s in skus if s["verdict"]["tier"] in ("止损", "严重亏损") and
            (s["wins"]["w2"]["cost"] > 0 or s["wins"]["w1"]["cost"] > 0)]
    grow = [s for s in skus if s["verdict"]["tier"] == "放大"]

    def _own(s):
        """该 SKU 的「本期花费」：与 stop/grow 的取数口径保持一致。"""
        w = s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"]
        return w["cost"]
    erpS = [s for s in skus if s.get("costSource") == "erp"]
    _all = sum(_own(s) for s in skus)
    erpShare = round(sum(_own(s) for s in erpS) / _all * 100, 1) if _all else 0.0
    negMargin = [s for s in erpS if _f(s.get("breakeven")) <= 0]
    negNames = "、".join((s["name"] or s["skuId"])[:16] for s in negMargin[:6])
    if len(negMargin) > 6:
        negNames += " 等"

    stopCost = sum((s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["cost"] for s in stop)
    stopAmt = sum((s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["amt"] for s in stop)
    growCost = sum((s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["cost"] for s in grow)
    growAmt = sum((s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["amt"] for s in grow)
    last = c2 if c2["cost"] > 0 else c1
    targetRoi = round(growAmt / growCost, 2) if growCost else 4.0
    estGain = round(stopCost * (targetRoi - (stopAmt / stopCost if stopCost else 0)), 2)
    roiAfter = round((last["amt"] + estGain) / last["cost"], 2) if last["cost"] else 0.0

    autoBudget = sum(1 for o in ops if o["kind"] == "autoBudget")
    _ev = {"正确", "有问题", "效果不明显", "存疑", "有效但不彻底", "已止损"}
    evaluable = [o for o in adj if o["verdict"] in _ev]
    unevaluable = len(adj) - len(evaluable)
    summary = [
        (f"{_acct_label(accts)}合计：花费 ¥{c0['cost']:,.0f} → ¥{c1['cost']:,.0f} → ¥{c2['cost']:,.0f}；"
         f"成交 ¥{c0['amt']:,.0f} → ¥{c1['amt']:,.0f} → ¥{c2['amt']:,.0f}；"
         f"ROI {c0['roi']} → {c1['roi']} → {c2['roi']}"),
        (f"所选区间{_acct_label(accts)}商智总成交 ¥{(c2['szAmt']):,.0f}，广告贡献 "
         + (f"{c2['adShare']}%" if c2.get("adShare") is not None else "缺失（该区间无商智数据）")
         + "（广告成交/店铺总成交）"),
        (f"{_op_span(ops)} 共 {len(ops)} 条快车操作日志：人工操作 {len(manual)} 条、"
         f"系统「自动提升预算」{autoBudget} 条。真正能对比前后 7 天效果的 {len(evaluable)} 条里，"
         f"{len(right)} 条正确、{len(wrong)} 条有问题、"
         f"{len([o for o in adj if o['verdict'] == '效果不明显'])} 条效果不明显、"
         f"{len([o for o in adj if o['verdict'] == '存疑'])} 条存疑；"
         f"另有 {unevaluable} 条因新建/删除/无花费无法对比"),
        (f"所选区间仍有 {len(stop)} 个 SKU 的广告 ROI 低于「各自」的保本线"
         f"（真实成本覆盖 {erpShare:.0f}% 的花费），合计花费 ¥{stopCost:,.0f}"
         f"（占 {stopCost / last['cost'] * 100:.0f}%），这部分是主要失血点" if last["cost"] else ""),
        (f"其中 {len(negMargin)} 个 SKU 按 ERP 到手结算价算出「卖一件亏一件」"
         f"（到手结算价 < 货款+运费+包材+人工），不投广告也在亏：{negNames}" if negMargin else ""),
        (f"若把上述花费转移到 ROI≈{targetRoi} 的高效 SKU，预计多产出 ¥{estGain:,.0f}，"
         f"整体 ROI 可由 {last['roi']} 提升到约 {roiAfter}"),
    ]
    return {"summary": [s for s in summary if s], "combined": combined,
            "right": [o for o in right], "wrong": [o for o in wrong],
            "stop": [{"skuId": s["skuId"], "name": s["name"],
                      "cost": (s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["cost"],
                      "roi": (s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["roi"],
                      "breakeven": s["breakeven"], "costSource": s.get("costSource") or "store",
                      "unitPrice": s.get("unitPrice"), "grossPerOrder": s.get("grossPerOrder"),
                      "netProfit": s["est"]["w2"]["netProfit"]}
                     for s in sorted(stop, key=lambda x: -(x["wins"]["w2"] if x["wins"]["w2"]["cost"] > 0 else x["wins"]["w1"])["cost"])],
            "grow": [{"skuId": s["skuId"], "name": s["name"],
                      "cost": (s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["cost"],
                      "roi": (s["wins"]["w2"] if s["wins"]["w2"]["cost"] > 0 else s["wins"]["w1"])["roi"],
                      "breakeven": s["breakeven"], "growLine": s.get("growLine"),
                      "costSource": s.get("costSource") or "store",
                      "unitPrice": s.get("unitPrice"), "grossPerOrder": s.get("grossPerOrder")}
                     for s in sorted(grow, key=lambda x: -x["wins"]["w2"]["roi"])],
            "upside": {"stopCost": round(stopCost, 2), "targetRoi": targetRoi,
                       "estGain": estGain, "roiAfter": roiAfter, "lastCost": last["cost"]}}


def build(start=None, end=None, windows=None, accounts=None):
    if windows is None:
        windows = windows_for(start, end) if (start and end) else WINDOWS
    global BREAKEVEN_FLAT
    wins = list(windows)
    win2 = wins[-1]
    model = load_cost_model()
    # 让「全店保本 ROI」由成本表实际算出，而不是写死 4.00；
    # 这样 SKU 表里的保本线与结论里的保本线永远一致
    _gm = global_margin(model)
    if _gm > 0:
        BREAKEVEN_FLAT = round(1 / _gm, 2)
    accts = _load_accounts(wins, accounts)
    active = [a for a in accts if a["hasData"]]
    if not active:
        raise FileNotFoundError(
            f"没有找到 {win2[1]} ~ {win2[2]} 的数据，请先运行 python -m jd_roi.scrape_day")
    # 每个 SKU 的真实成本：接口优先（按所选区间要均值），断网退回缓存/costs.json/全店口径
    costs = {}
    if settings.COSTS_FOLLOW_WINDOW:
        costs = settings.load_costs(win2[1], win2[2])
    else:
        costs = settings.load_costs()
    totals = {a["key"]: account_totals(a) for a in active}
    skus, no_ad = sku_rows(active, model, costs)
    plans, planBe = plan_rows(active, model, skus)
    beByName = {(p["account"], p["name"]): _f(p.get("breakeven")) for p in plans}
    ops = op_rows(active, wins[0][1], win2[2])
    words = word_rows(active, model, beByName)
    adj = adjustments(active, ops, plans, wins, span_from=win2[1], span_to=win2[2])
    advice = build_advice(active, totals, skus, plans, ops, words, adj)
    # 整店一条「加权保本线」：按本期各 SKU 成交额加权毛利率取倒数，
    # 用它替代「全店一条线」做汇总文案，才不会把高毛利 SKU 的花费算成打平
    beBlend, marginBlend = blended_breakeven(
        [{"amt": s["wins"]["w1"]["amt"], "margin": s["margin"]} for s in skus])

    return {
        "meta": {
            "windows": [{"key": w[0], "start": w[1], "end": w[2], "label": w[3]} for w in wins],
            "range": {"start": win2[1], "end": win2[2]},
            "coverage": _coverage_block(active, wins),
            "costModel": model, "breakevenFlat": BREAKEVEN_FLAT,
            "breakevenBlend": beBlend, "marginBlend": marginBlend,
            "growFactor": GROW_FACTOR,
            "costSource": dict(settings.COSTS_SOURCE or {}),
            "costCoverage": _cost_coverage(skus, costs),
            "costUpdatedAt": costs.get("updatedAt") or "",
            "caliber": ("广告=京准通概览(点击15天/成交订单口径)；智能投放=智能投放报表；"
                        "商智=各店铺成交口径；商智客单价为真实值，产品成本按 Excel 成本率等比推算"),
            "accounts": [{"key": a["key"], "label": a["label"], "accountId": a["accountId"]} for a in active],
            "generatedAt": settings.now_str("%Y-%m-%d %H:%M"),
        },
        "accounts": [{"key": a["key"], "label": a["label"], "accountId": a["accountId"],
                      "totals": totals[a["key"]]} for a in active],
        "plans": plans,
        "skus": skus,
        "noAd": no_ad,
        "typeSplit": type_split(active),
        "ops": ops,
        "adjustments": adj,
        "words": {a["key"]: {wk: {cid: {"sw": v["sw"][:60], "kw": v["kw"][:40]}
                                   for cid, v in (words[a["key"]].get(wk) or {}).items()}
                            for wk in WKEYS} for a in active},
        "runtime": {"generatedAt": settings.now_str("%Y-%m-%d %H:%M:%S")},
        "advice": advice,
    }


def analyze(start=None, end=None, accounts=None, out_path=None):
    """对外主入口：给定区间返回分析 dict，并可落地 JSON。"""
    data = build(start=start, end=end, accounts=accounts)
    if out_path is None:
        out_path = config.DATA_DIR / "analysis2.json"
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data, out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    start = args[0] if len(args) > 0 else None
    end = args[1] if len(args) > 1 else None
    data, out = analyze(start, end)
    print(f"[OK] -> {out}  区间 {data['meta']['range']['start']} ~ {data['meta']['range']['end']}")
    adv = data["advice"]
    print(f"账号={[a['label'] for a in data['meta']['accounts']]} "
          f"SKU={len(data['skus'])} 计划={len(data['plans'])} 操作={len(data['ops'])} "
          f"可评估调整={len([o for o in data['adjustments']])}")
    for s in adv["summary"]:
        print("  ·", s)


if __name__ == "__main__":
    main()
