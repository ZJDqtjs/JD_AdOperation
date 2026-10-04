# -*- coding: utf-8 -*-
"""京准通广告账户分析核心（支持多账号/多店铺，按 SKU 关联）。

数据模型：
- 每个账号(key) = 一个店铺，拥有自己的京准通数据与自己的商智数据，落在 data/[<key>/]
- 广告数据：京准通「智能投放-商品」报表按 skuId 聚合（可跨账号累加）
- 商智数据：按 sku_id 聚合，**按店铺分别保留**（两店商智口径不同）
- SKU 为跨店铺主键：同款 SKU 在两店都有时，广告相加、商智分店展示

输出：结构化分析结果(dict)，供 HTML 报告 / 其他模型复用。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import defaultdict

from . import config, store

# ---- 成本默认值（可被 Excel「计算公式」sheet 覆盖）----
DEFAULT_COST = {"price": 180.0, "product": 120.0, "platform": 0.0, "package": 0.0, "shipping": 5.0}

WASTE_MIN = 3.0    # 废词：花费≥该值且0单
LOW_MIN = 10.0     # 低效词：花费≥该值且 ROI<保本
GOOD_MIN_ORD = 2   # 高效词：订单≥该值且 ROI>保本

TYPE_LABEL = {61: "智能化", 2: "快车-关键词", 153: "全站智能", 0: "其他"}
_TIER_ORDER = {"放大": 0, "优化": 1, "止损": 2, "未启动": 3}


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _load(name: str, account: str | None = None) -> dict | None:
    return store.load(name, account)


# ---- 商智指标编码 ----
SZ_UV = "jdr_sch_traffic_brow_sku__page_cnt_traffic_plat_item_di_sz_bsg"      # 访客数
SZ_PV = "jdr_sch_traffic_brow_sku__page_qtty_traffic_plat_item_di_sz_bsg"     # 浏览量
SZ_AMT = "jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot"              # 成交金额
SZ_ORDN = "jdr_sch_trade_deal_ord_ord_qtty_sz_trade_deal_snapshot"            # 成交单量
SZ_QTY = "jdr_sch_trade_deal_ord_sku_qtty_sz_trade_deal_snapshot"             # 成交件数
SZ_CVR = "fo_jdr_sch_industry_deal_rate"                                      # 成交转化率
SZ_AOV = "fo_jdr_sch_trade_deal_ord_amt_user_sz_trade_deal_snapshot"          # 客单价

_PREFIXES = ("商品_常规推广_智能化_", "商品_常规推广_", "AI_快车-关键词_",
             "商品-常规推广-", "商品_常规推广_智能化")
# 匹配时忽略的通用词（产地/修饰/单位等），避免“新鲜”之类造成误匹配
_GENERIC = ["新鲜", "京鲜生", "京东自营", "源头直发", "现挖", "净重", "包邮", "礼盒装", "礼盒",
            "整箱", "正宗", "当季", "非转基因", "大规格", "小规格", "大果", "中果", "小果", "特大",
            "云南", "山东", "陕西", "四川", "海南"]
_UNITS = re.compile(r"\d+(?:\.\d+)?\s*(?:斤|公斤|千克|kg|KG|克|g|个|装|根|袋|箱|盒|cm|厘米)")


def _core_token(name: str) -> str:
    t = str(name or "")
    for p in _PREFIXES:
        if t.startswith(p):
            t = t[len(p):]
    for suf in ("/智能", "_智能"):
        t = t.replace(suf, "")
    t = _UNITS.sub("", t)
    for g in _GENERIC:
        t = t.replace(g, "")
    return re.sub(r"[\s_+\-]+", "", t)


def _ngrams(s: str, n: int = 2) -> set:
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def _match_sku(token: str, skus: list) -> dict | None:
    """按 2-gram 重叠匹配商智 SKU，取重叠最多者，平局取成交额高者。"""
    core = _core_token(token)
    if len(core) < 2 or not skus:
        return None
    grams = _ngrams(core)
    best, best_score = None, 0
    for s in skus:
        score = len(grams & _ngrams(s["name"]))
        if score > best_score or (score == best_score and best and s["amt"] > best["amt"]):
            best, best_score = s, score
    return best if best_score >= 1 else None


def _sz_rows(df: dict | None) -> list:
    out = []
    for r in (df or {}).get("rows", []):
        if r.get("$summary"):
            continue
        out.append({
            "skuId": str(r.get("sku_id")), "name": r.get("name"), "img": r.get("img_src"),
            "url": r.get("pro_url"),
            "amt": _f(r.get(SZ_AMT)), "orders": _f(r.get(SZ_ORDN)), "qty": _f(r.get(SZ_QTY)),
            "visitors": int(_f(r.get(SZ_UV))), "views": int(_f(r.get(SZ_PV))),
            "cvr": round(_f(r.get(SZ_CVR)) * 100, 2),
            "aov": round(_f(r.get(SZ_AOV)), 2),
        })
    return out


def _load_sz(tag: str, account: str | None = None) -> list:
    return _sz_rows(_load(f"sz_product_{tag}", account))


def load_cost_params() -> dict:
    """从 Excel「计算公式」sheet 读成本参数，失败则用默认值。"""
    params = dict(DEFAULT_COST)
    try:
        import openpyxl

        wb = openpyxl.load_workbook(config.EXCEL_PATH, data_only=True)
        ws = wb["计算公式"]
        vals = {
            "price": ws["H2"].value, "product": ws["H3"].value, "platform": ws["H4"].value,
            "package": ws["H5"].value, "shipping": ws["H6"].value,
        }
        for k, v in vals.items():
            if v is not None:
                params[k] = _f(v, params[k])
    except Exception:  # noqa: BLE001
        pass
    return params


def breakeven_roi(cost: dict) -> float:
    profit = cost["price"] - cost["product"] - cost["platform"] - cost["package"] - cost["shipping"]
    margin = profit / cost["price"] if cost["price"] else 0
    return round(1 / margin, 2) if margin > 0 else 0.0


def _metrics(imp, clk, cost, ord_, amt, cart) -> dict:
    return {
        "imp": int(imp), "clk": int(clk), "cost": round(_f(cost), 2),
        "ord": int(ord_), "amt": round(_f(amt), 2), "cart": int(cart),
        "roi": round(_f(amt) / _f(cost), 2) if _f(cost) else 0.0,
        "ctr": round(int(clk) / imp * 100, 2) if imp else 0.0,
        "cpc": round(_f(cost) / clk, 2) if clk else 0.0,
        "cvr": round(int(ord_) / clk * 100, 2) if clk else 0.0,
        "cartRate": round(int(cart) / clk * 100, 2) if clk else 0.0,
    }


def _terms_for(df: dict | None, key: str) -> dict:
    """按计划聚合词维度：返回 {plan: {word: metrics}}。"""
    out: dict = defaultdict(lambda: defaultdict(lambda: {"imp": 0, "clk": 0, "cost": 0.0, "ord": 0, "amt": 0.0, "cart": 0}))
    if not df:
        return out
    for r in df.get("rows", []):
        w = r.get(key)
        if w is None:
            continue
        pk = (r.get("_acct"), r.get("campaignName")) if r.get("_acct") else r.get("campaignName")
        t = out[pk][w]
        t["imp"] += r.get("impressions") or 0
        t["clk"] += r.get("clicks") or 0
        t["cost"] += _f(r.get("cost"))
        t["ord"] += r.get("totalOrderCnt") or 0
        t["amt"] += _f(r.get("totalOrderSum"))
        t["cart"] += r.get("totalCartCnt") or 0
    return out


def _sum_terms(terms: dict) -> dict:
    a = {"imp": 0, "clk": 0, "cost": 0.0, "ord": 0, "amt": 0.0, "cart": 0}
    for d in terms.values():
        for k in a:
            a[k] += d[k]
    return a


def _word_rows(terms: dict, breakeven: float, limit: int = 8) -> tuple[list, list, list]:
    good, waste, low = [], [], []
    for w, d in terms.items():
        roi = d["amt"] / d["cost"] if d["cost"] else 0.0
        if d["ord"] >= GOOD_MIN_ORD and d["cost"] > 0 and roi > breakeven:
            good.append({"word": w, "roi": round(roi, 2), "ord": d["ord"], "cost": round(d["cost"], 2), "amt": round(d["amt"], 2)})
        if d["cost"] >= WASTE_MIN and d["ord"] == 0:
            waste.append({"word": w, "cost": round(d["cost"], 2), "clk": d["clk"], "imp": d["imp"]})
        if d["cost"] >= LOW_MIN and d["ord"] > 0 and roi < breakeven:
            low.append({"word": w, "roi": round(roi, 2), "cost": round(d["cost"], 2), "ord": d["ord"]})
    good.sort(key=lambda x: -x["roi"])
    waste.sort(key=lambda x: -x["cost"])
    low.sort(key=lambda x: -x["cost"])
    return good[:limit], waste[:limit], low[:limit]


def _tier_and_target(roi: float, breakeven: float) -> tuple[str, float]:
    if roi <= 0:
        return "未启动", 0.0
    if roi >= 4:
        return "放大", round(roi + 0.5, 1)
    if roi >= breakeven:
        return "优化", round(max(roi + 0.3, breakeven + 0.2), 1)
    return "止损", round(breakeven + 0.2, 1)


def _build_products(acct: dict, be: float, sz_cur_all: list, sz_cmp_all: list) -> list:
    """构造某账号(店铺)的计划级商品分析；商智跨店匹配（广告在B、商智在A 也能对上）。"""
    ds, key, label = acct["ds"], acct["key"], acct["label"]
    msa_c, msa_p = ds["jzt_c"], ds["jzt_p"]
    if not msa_c:
        return []
    jst_cur_map = {r.get("campaignName"): r for r in (ds["jst_c"] or {}).get("rows", [])}
    jst_cmp_map = {r.get("campaignName"): r for r in (ds["jst_p"] or {}).get("rows", [])}
    p_map = {r.get("campaignId"): r for r in (msa_p or {}).get("rows", [])}
    sw_cur = _terms_for(ds["sw_c"], "searchTerm")
    kw_terms = _terms_for(ds["kw_c"], "keywordName")
    sz_cur_list, sz_cmp_list = sz_cur_all, sz_cmp_all

    out = []
    for r in msa_c.get("rows", []):
        name = r.get("campaignName")
        ctype = int(_f(r.get("campaignType")))
        jr = jst_cur_map.get(name)
        jp = jst_cmp_map.get(name)
        src = "智能投放" if jr else "概览"

        def mk(row, jrow):
            if jrow:
                return _metrics(jrow.get("impressions"), jrow.get("clicks"), jrow.get("cost"),
                                jrow.get("totalOrderCnt"), jrow.get("totalOrderSum"), jrow.get("totalCartCnt"))
            if not row:
                return _metrics(0, 0, 0, 0, 0, 0)
            return _metrics(row.get("impressions"), row.get("clicks"), row.get("cost"),
                            row.get("totalOrderCnt"), row.get("totalOrderSum"), row.get("totalCartCnt"))

        cur = mk(r, jr)
        cmp_ = mk(p_map.get(r.get("campaignId")), jp)

        split = None
        terms = sw_cur.get(name, {})
        if terms:
            st = _sum_terms(terms)
            rec = {k: cur_row - st[k] for k, cur_row in (
                ("imp", cur["imp"]), ("clk", cur["clk"]), ("cost", cur["cost"]),
                ("ord", cur["ord"]), ("amt", cur["amt"]), ("cart", cur["cart"]))
            }
            sroi = round(st["amt"] / st["cost"], 2) if st["cost"] else 0.0
            rroi = round(rec["amt"] / rec["cost"], 2) if rec["cost"] else 0.0
            split = {
                "search": {"cost": round(st["cost"], 2), "amt": round(st["amt"], 2),
                           "ord": st["ord"], "roi": sroi, "share": round(st["cost"] / cur["cost"] * 100, 1) if cur["cost"] else 0},
                "recommend": {"cost": round(rec["cost"], 2), "amt": round(rec["amt"], 2),
                              "ord": rec["ord"], "roi": rroi, "share": round(rec["cost"] / cur["cost"] * 100, 1) if cur["cost"] else 0},
            }

        tier, target = _tier_and_target(cur["roi"], be)
        good, waste, low = _word_rows(terms, be) if terms else ([], [], [])
        kws = None
        if name in kw_terms:
            g, w_, l = _word_rows(kw_terms[name], be, limit=20)
            kws = {"good": g, "waste": w_, "low": l}

        actions = []
        if tier == "放大":
            actions.append("日预算 +20~30%，承接更多高效流量")
            actions.append("对高效词提高出价 10~20%")
        elif tier == "优化":
            actions.append("抠词：否定废词、降低低效词出价")
            actions.append(f"目标投产比提升至 {target}")
        elif tier == "止损":
            actions.append("下调预算，优先保住高效词")
            actions.append("否定全部0单废词；持续低于保本则暂停")
        if split and split["recommend"]["cost"] >= 15 and split["recommend"]["roi"] < be:
            actions.append(f"推荐流量ROI仅{split['recommend']['roi']}偏低 → 在「快车-关键词-成交-投产比出价」新建计划承接")
        if waste:
            saved = sum(x["cost"] for x in waste)
            actions.append(f"可否定 {len(waste)} 个废词，预计节省 {saved:.1f} 元")

        # 商智对照：优先按 SKU 精确匹配，其次按商品名模糊匹配
        sku = None
        if jr and jr.get("skuId"):
            sid = str(jr.get("skuId"))
            sku = next((s for s in sz_cur_list if s["skuId"] == sid), None)
        if sku is None:
            sku = _match_sku(name, sz_cur_list) if sz_cur_list else None
        sku_prev = None
        if sku and sz_cmp_list:
            sku_prev = next((s for s in sz_cmp_list if s["skuId"] == sku["skuId"]), None)
        sz_block = None
        if sku:
            share = round(cur["amt"] / sku["amt"] * 100, 1) if sku["amt"] else 0.0
            reliable = 0 < share <= 100
            sz_block = {
                "skuId": sku["skuId"], "name": sku["name"], "img": sku.get("img"), "url": sku.get("url"),
                "shop": sku.get("_shop"),
                "amt": round(sku["amt"], 2), "visitors": sku["visitors"], "views": sku["views"],
                "cvr": sku["cvr"], "aov": sku["aov"],
                "adShare": share if reliable else None,
                "cmpAmt": round(sku_prev["amt"], 2) if sku_prev else None,
                "cmpVisitors": sku_prev["visitors"] if sku_prev else None,
            }
            if reliable and share < 30 and cur["cost"] > 100:
                actions.append(f"广告成交仅占该商品总成交 {share}%，自然流量为主，可适度加投抢占搜索位")

        out.append({
            "name": name, "type": ctype, "typeLabel": TYPE_LABEL.get(ctype, "其他"),
            "source": src, "cur": cur, "cmp": cmp_,
            "acctKey": key, "account": label,
            "deltaCost": round(cur["cost"] - cmp_["cost"], 2),
            "deltaRoi": round(cur["roi"] - cmp_["roi"], 2),
            "split": split, "tier": tier, "targetRoi": target,
            "goodWords": good, "wasteWords": waste, "lowWords": low, "kw": kws,
            "sz": sz_block,
            "actions": actions,
        })
    return out


def _sku_rows(accts: list, be: float, sz_accounts: list | None = None) -> list:
    """按 SKU 合并：广告跨账号累加（仅 scope 内账号）；商智按店铺分别保留（默认全部店铺）。"""
    sz_accounts = sz_accounts if sz_accounts is not None else accts

    def agg(df):
        out: dict = {}
        for r in (df or {}).get("rows", []):
            sid = str(r.get("skuId") or "")
            if not sid:
                continue
            a = out.setdefault(sid, {"cost": 0.0, "amt": 0.0, "ord": 0, "imp": 0, "clk": 0, "cart": 0, "campaigns": set()})
            a["cost"] += _f(r.get("cost"))
            a["amt"] += _f(r.get("totalOrderSum"))
            a["ord"] += r.get("totalOrderCnt") or 0
            a["imp"] += r.get("impressions") or 0
            a["clk"] += r.get("clicks") or 0
            a["cart"] += r.get("totalCartCnt") or 0
            if r.get("campaignName"):
                a["campaigns"].add(r["campaignName"])
        return out

    ad_cur, ad_cmp, sz_cur, sz_cmp = {}, {}, {}, {}
    for a in accts:
        k, ds = a["key"], a["ds"]
        ad_cur[k] = agg(ds["sku_c"])
        ad_cmp[k] = agg(ds["sku_p"])
    for a in sz_accounts:
        k, ds = a["key"], a["ds"]
        sz_cur[k] = {s["skuId"]: s for s in ds["sz_c"]}
        sz_cmp[k] = {s["skuId"]: s for s in ds["sz_p"]}

    ids = set()
    for m in (ad_cur, ad_cmp):
        for mm in m.values():
            ids |= set(mm)

    rows = []
    for sid in ids:
        cost = amt = p_cost = p_amt = 0.0
        ord_ = imp = clk = cart = 0
        accts_used, campaigns = set(), set()
        for k, m in ad_cur.items():
            r = m.get(sid)
            if r:
                cost += r["cost"]; amt += r["amt"]; ord_ += r["ord"]
                imp += r["imp"]; clk += r["clk"]; cart += r["cart"]
                accts_used.add(k); campaigns |= r["campaigns"]
        for m in ad_cmp.values():
            r = m.get(sid)
            if r:
                p_cost += r["cost"]; p_amt += r["amt"]

        by_shop, name, img, url = {}, None, None, None
        sz_total = sz_ord = sz_vis = sz_views = 0.0
        for a in sz_accounts:
            k = a["key"]
            s = sz_cur[k].get(sid)
            if not s:
                continue
            by_shop[k] = {"label": a["label"], "amt": round(s["amt"], 2), "visitors": s["visitors"],
                          "cvr": s["cvr"], "aov": round(s["amt"] / s["orders"], 2) if s.get("orders") else 0.0}
            sz_total += s["amt"]; sz_ord += s["orders"]; sz_vis += s["visitors"]; sz_views += s["views"]
            name = name or s["name"]; img = img or s.get("img"); url = url or s.get("url")

        if not accts_used and not by_shop:
            continue
        cost, amt = round(cost, 2), round(amt, 2)
        roi = round(amt / cost, 2) if cost else 0.0
        share = round(amt / sz_total * 100, 1) if sz_total else None
        if share is not None and not (0 < share <= 100):
            share = None
        tier, target = _tier_and_target(roi, be)
        rows.append({
            "skuId": sid,
            "name": name or (sorted(campaigns)[0] if campaigns else sid),
            "img": img, "url": url,
            "cost": cost, "amt": amt, "ord": ord_, "imp": imp, "clk": clk, "cart": cart, "roi": roi,
            "cmpCost": round(p_cost, 2), "cmpAmt": round(p_amt, 2),
            "tier": tier, "targetRoi": target,
            "szAmt": round(sz_total, 2), "szVisitors": int(sz_vis), "szViews": int(sz_views),
            "szCvr": round(sz_ord / sz_vis * 100, 2) if sz_vis else 0.0,
            "szAov": round(sz_total / sz_ord, 2) if sz_ord else 0.0,
            "adShare": share,
            "shops": by_shop,
            "accts": list(accts_used),
            "campaigns": sorted(campaigns),
        })
    rows.sort(key=lambda x: (-x["cost"], -x["szAmt"]))
    return rows


def _metrics_sum(rows: list) -> dict:
    return _metrics(
        sum(r.get("impressions") or 0 for r in rows),
        sum(r.get("clicks") or 0 for r in rows),
        sum(_f(r.get("cost")) for r in rows),
        sum(r.get("totalOrderCnt") or 0 for r in rows),
        sum(_f(r.get("totalOrderSum")) for r in rows),
        sum(r.get("totalCartCnt") or 0 for r in rows))


def _build_advice(products: list, tot_c: dict, tot_p: dict, be: float, shops: list | None = None) -> dict:
    """生成决策摘要 / 止损清单 / 加投清单 / 否定词清单。"""
    live = [p for p in products if p["cur"]["cost"] > 0]
    stop = sorted([p for p in live if 0 < p["cur"]["roi"] < be], key=lambda p: -p["cur"]["cost"])
    grow = sorted([p for p in live if p["cur"]["roi"] >= 4], key=lambda p: -p["cur"]["roi"])

    stop_cost = sum(p["cur"]["cost"] for p in stop)
    stop_amt = sum(p["cur"]["amt"] for p in stop)
    stop_roi = stop_amt / stop_cost if stop_cost else 0
    grow_cost = sum(p["cur"]["cost"] for p in grow)
    grow_amt = sum(p["cur"]["amt"] for p in grow)
    grow_roi = grow_amt / grow_cost if grow_cost else 0
    target_roi = grow_roi if grow_roi > stop_roi else be + 2
    est_gain = stop_cost * (target_roi - stop_roi) if stop_cost else 0
    roi_after = (tot_c["amt"] + est_gain) / tot_c["cost"] if tot_c["cost"] else 0

    neg: dict = {}
    for p in products:
        for w in p.get("wasteWords", []):
            n = neg.setdefault(w["word"], {"word": w["word"], "cost": 0.0, "plans": 0})
            n["cost"] += w["cost"]
            n["plans"] += 1
    negatives = sorted(neg.values(), key=lambda x: -x["cost"])[:20]
    neg_total = sum(x["cost"] for x in negatives)

    roi_delta = tot_c["roi"] - tot_p["roi"]
    summary = [
        f"账号整体：花费 ¥{tot_p['cost']:,.0f} → ¥{tot_c['cost']:,.0f}（{(tot_c['cost']-tot_p['cost'])/tot_p['cost']*100:+.1f}%），"
        f"成交 ¥{tot_p['amt']:,.0f} → ¥{tot_c['amt']:,.0f}（{(tot_c['amt']-tot_p['amt'])/tot_p['amt']*100:+.1f}%），"
        f"ROI {tot_p['roi']} → {tot_c['roi']}（{roi_delta:+.2f}）"
        + ("——缩量的同时效率也在下滑，需立刻纠偏" if roi_delta < 0 else "——效率提升，方向正确"),
        f"保本 ROI={be}；调整后有 {len(stop)} 个计划低于保本，合计花费 ¥{stop_cost:,.0f}"
        f"（占 {stop_cost/tot_c['cost']*100:.0f}%），平均 ROI 仅 {stop_roi:.2f}",
        f"{len(grow)} 个高效计划 ROI≥4，平均 {grow_roi:.2f}，是被削减/未充分投放的增量池",
        f"若把低于保本的 ¥{stop_cost:,.0f} 转移到高效计划（ROI≈{target_roi:.1f}），"
        f"预计多产出 ¥{est_gain:,.0f}，整体 ROI 可由 {tot_c['roi']} 提升到约 {roi_after:.2f}",
        f"另有 {len(negatives)} 个高频 0 单废词，合计浪费 ¥{neg_total:,.0f}，建议直接否定",
    ]
    # ---- 运营指导（可直接执行）----
    guide: list = []
    share = stop_cost / tot_c["cost"] * 100 if tot_c["cost"] else 0
    gi = []
    for p in stop:
        w = [x["word"] for x in p.get("wasteWords", [])[:3]]
        lw = [x["word"] for x in p.get("lowWords", [])[:2]]
        d = f"花 ¥{p['cur']['cost']:,.0f} · ROI {p['cur']['roi']}"
        if w:
            d += f" → 否定 {('、'.join(w))}"
        if lw:
            d += f"；降价 {('、'.join(lw))}"
        if not w and not lw:
            d += " → 降预算或改「成交-投产比出价」"
        gi.append({"name": p["name"], "detail": d})
    guide.append({"icon": "stop", "title": f"第一步 · 立即止损 / 优化（合计 ¥{stop_cost:,.0f}，占 {share:.0f}%，平均 ROI {stop_roi:.2f}）", "items": gi})

    gi = []
    for p in grow:
        gi.append({"name": p["name"], "detail": f"花 ¥{p['cur']['cost']:,.0f} · ROI {p['cur']['roi']} → 建议加投 +¥{p['cur']['cost'] * 0.3:,.0f}（+30%）"})
    guide.append({"icon": "grow", "title": f"第二步 · 加大投入（{len(grow)} 个计划 ROI≥4，平均 {grow_roi:.2f}）", "items": gi})

    gi = [{"name": w["word"], "detail": f"浪费 ¥{w['cost']:.1f} · 涉及 {w['plans']} 个计划"} for w in negatives[:14]]
    guide.append({"icon": "neg", "title": f"第三步 · 清理 0 单废词（合计浪费 ¥{neg_total:,.0f}）", "items": gi})

    guide.append({"icon": "gain", "title": "第四步 · 量化收益", "items": [
        {"name": "可腾挪花费", "detail": f"¥{stop_cost:,.0f}（ROI {stop_roi:.2f} 的计划）"},
        {"name": "转入高效计划", "detail": f"目标 ROI ≈ {target_roi:.2f}"},
        {"name": "预计多产出", "detail": f"+¥{est_gain:,.0f}"},
        {"name": "整体 ROI", "detail": f"{tot_c['roi']} → 约 {roi_after:.2f}"},
    ]})

    if shops:
        ins = next((s for s in shops if s.get("inScope")), None)
        oth = next((s for s in shops if not s.get("inScope")), None)
        if ins and oth:
            guide.append({"icon": "ref", "title": "第五步 · 对齐参考店铺打法", "items": [
                {"name": oth["label"], "detail": f"广告 ¥{oth['ad']['cmp']['cost']:,.0f}→¥{oth['ad']['cur']['cost']:,.0f}"
                                                  f"（{(oth['ad']['cur']['cost']-oth['ad']['cmp']['cost'])/max(oth['ad']['cmp']['cost'],1)*100:+.0f}%），"
                                                  f"ROI {oth['ad']['cmp']['roi']}→{oth['ad']['cur']['roi']}"},
                {"name": ins["label"], "detail": f"广告 ¥{ins['ad']['cmp']['cost']:,.0f}→¥{ins['ad']['cur']['cost']:,.0f}"
                                                 f"（{(ins['ad']['cur']['cost']-ins['ad']['cmp']['cost'])/max(ins['ad']['cmp']['cost'],1)*100:+.0f}%），"
                                                 f"ROI {ins['ad']['cmp']['roi']}→{ins['ad']['cur']['roi']}"},
                {"name": "建议", "detail": "参考店铺逆势加投仍提效；本账号应停止无差别缩量，改为「砍低效、保高效」"},
            ]})

    return {
        "summary": summary,
        "guide": guide,
        "stop": [{"name": p["name"], "account": p.get("account"), "cost": p["cur"]["cost"],
                  "amt": p["cur"]["amt"], "roi": p["cur"]["roi"], "loss": round((be - p["cur"]["roi"]) * p["cur"]["cost"], 2)}
                 for p in stop],
        "grow": [{"name": p["name"], "account": p.get("account"), "cost": p["cur"]["cost"],
                  "roi": p["cur"]["roi"], "add": round(p["cur"]["cost"] * 0.3, 2)} for p in grow],
        "negatives": negatives,
        "upside": {"stopCost": round(stop_cost, 2), "stopRoi": round(stop_roi, 2),
                   "targetRoi": round(target_roi, 2), "estGain": round(est_gain, 2),
                   "roiAfter": round(roi_after, 2), "negCost": round(neg_total, 2)},
    }


def _load_accounts() -> list:
    """加载全部有数据的账号（含商智）。是否参与广告分析由 config.in_scope 决定。"""
    accts = []
    for a in config.ACCOUNTS:
        k = a["key"]
        ds = {
            "jzt_c": _load("jzt_campaign_cur", k), "jzt_p": _load("jzt_campaign_cmp", k),
            "jst_c": _load("jst_campaign_cur", k), "jst_p": _load("jst_campaign_cmp", k),
            "sw_c": _load("jst_searchword_cur", k), "sw_p": _load("jst_searchword_cmp", k),
            "kw_c": _load("kw_cur", k),
            "sku_c": _load("jst_sku_cur", k), "sku_p": _load("jst_sku_cmp", k),
            "sz_c": _load_sz("cur", k), "sz_p": _load_sz("cmp", k),
            "flow_c": _load("sz_flow_cur", k), "flow_p": _load("sz_flow_cmp", k),
        }
        if any(ds.values()):
            accts.append({"key": k, "label": a.get("label") or k, "account_id": a.get("account_id"), "ds": ds})
    return accts


def _flow_block(df):
    if not df:
        return None
    core = df.get("core") or []
    c = core[0] if core else {}
    src = [{
        "name": s.get("name"),
        "visitors": int(_f(s.get("jdr_sch_traffic_brow_sku_cnt_jd_unified_attribution_sz"))),
        "amt": round(_f(s.get("jdr_sch_traffic_intr_ord_ord_amt_jd_unified_attribution_trade_deal_snapshot_sz")), 2),
    } for s in (df.get("source") or [])]
    return {
        "visitors": int(_f(c.get(SZ_UV))), "views": int(_f(c.get(SZ_PV))),
        "amt": round(_f(c.get(SZ_AMT)), 2), "orders": int(_f(c.get(SZ_ORDN))),
        "aov": round(_f(c.get(SZ_AOV)), 2), "cvr": round(_f(c.get(SZ_CVR)) * 100, 2),
        "sources": src,
    }


def build_analysis() -> dict:
    cost = load_cost_params()
    be = breakeven_roi(cost)

    accts_all = _load_accounts()
    accts = [a for a in accts_all if config.in_scope(a["key"])]
    if not any(a["ds"]["jzt_c"] for a in accts):
        raise FileNotFoundError("未找到 scope 内账号的计划数据(jzt_campaign_cur)，请先抓取")

    # 商智：跨全部店铺（广告在B、商品成交在A 也能关联）
    sz_all_cur, sz_all_cmp = [], []
    for a in accts_all:
        for s in a["ds"]["sz_c"]:
            s = dict(s)
            s["_shop"] = a["label"]
            sz_all_cur.append(s)
        for s in a["ds"]["sz_p"]:
            s = dict(s)
            s["_shop"] = a["label"]
            sz_all_cmp.append(s)

    products = []
    for a in accts:
        products += _build_products(a, be, sz_all_cur, sz_all_cmp)
    products.sort(key=lambda p: (-p["cur"]["cost"], _TIER_ORDER.get(p["tier"], 9)))

    skus = _sku_rows(accts, be, sz_accounts=accts_all)

    def _tot(kind):
        rows = []
        for a in accts:
            rows += (a["ds"][kind] or {}).get("rows", [])
        return _metrics(
            sum(r.get("impressions") or 0 for r in rows),
            sum(r.get("clicks") or 0 for r in rows),
            sum(_f(r.get("cost")) for r in rows),
            sum(r.get("totalOrderCnt") or 0 for r in rows),
            sum(_f(r.get("totalOrderSum")) for r in rows),
            sum(r.get("totalCartCnt") or 0 for r in rows))

    tot_c, tot_p = _tot("jzt_c"), _tot("jzt_p")

    sz_flows = []
    for a in accts_all:
        fc = _flow_block(a["ds"]["flow_c"])
        if fc:
            sz_flows.append({"acct": a["key"], "label": a["label"],
                             "cur": fc, "cmp": _flow_block(a["ds"]["flow_p"])})

    ref = next((a["ds"]["jzt_c"] for a in accts if a["ds"]["jzt_c"]), {})

    # 店铺维度（广告 + 商智）
    shops = []
    for a in accts_all:
        rows_c = (a["ds"]["jzt_c"] or {}).get("rows", [])
        rows_p = (a["ds"]["jzt_p"] or {}).get("rows", [])
        shops.append({
            "key": a["key"], "label": a["label"], "inScope": config.in_scope(a["key"]),
            "ad": {"cur": _metrics_sum(rows_c), "cmp": _metrics_sum(rows_p)},
            "flow": {"cur": _flow_block(a["ds"]["flow_c"]), "cmp": _flow_block(a["ds"]["flow_p"])},
        })

    advice = _build_advice(products, tot_c, tot_p, be, shops)

    return {
        "meta": {
            "start": ref.get("dateStart"), "end": ref.get("dateEnd"),
            "cmpStart": (next((a["ds"]["jzt_p"] for a in accts if a["ds"]["jzt_p"]), {}) or {}).get("dateStart"),
            "cmpEnd": (next((a["ds"]["jzt_p"] for a in accts if a["ds"]["jzt_p"]), {}) or {}).get("dateEnd"),
            "breakeven": be, "costParams": cost,
            "caliber": "账户合计=概览口径(点击15天/成交订单)；智能化=智能投放报表；商智=各店铺成交口径",
            "accounts": [{"key": a["key"], "label": a["label"]} for a in accts],
            "szShops": [a["label"] for a in accts_all if a["ds"]["sz_c"]],
            "scope": config.SCOPE_ACCOUNTS,
            "generatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "szFlows": sz_flows,
        "shops": shops,
        "advice": advice,
        "totals": {"cur": tot_c, "cmp": tot_p,
                   "deltaRoi": round(tot_c["roi"] - tot_p["roi"], 2),
                   "deltaCost": round(tot_c["cost"] - tot_p["cost"], 2)},
        "products": products,
        "skus": skus,
    }


def main() -> None:
    data = build_analysis()
    out = config.DATA_DIR / "analysis.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 分析完成 -> {out}")
    print(f"保本ROI={data['meta']['breakeven']} 本期 花费{data['totals']['cur']['cost']} "
          f"ROI{data['totals']['cur']['roi']} 计划数{len(data['products'])} SKU数{len(data['skus'])} "
          f"账号={[a['label'] for a in data['meta']['accounts']]}")


if __name__ == "__main__":
    main()
