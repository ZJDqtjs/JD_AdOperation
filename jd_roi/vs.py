# -*- coding: utf-8 -*-
"""史上最强 vs 现代最强：任意两区间（快车 / 全站营销）对比。

设计目标
--------
把「快车期」和「全站营销期」放在**同一把尺子**下看：
    * 统一用 商智成交额（整店全渠道）做分母，算费比 = 广告总花费 ÷ 商智成交额；
    * 同时保留平台口径的「广告归因成交 / 全站成交」，解释为什么两个 ROI 不能直接比；
    * 按「店铺 + SPU」分组，跨广告账户合并（解决雨润禾账户投梓云茶玉米这类归属错配）。

两个区间的数据各自调用 analyze2.build()，取 w2（所选区间）；SWA 仍按 SPU 汇总、
按成员商智成交占比拆分到 SKU（平台只给 SPU 级花费，SKU 级仅作结构参考）。
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from . import analyze2, dailystore, settings
from .indicators import SZ_AMT, SZ_ORDN, SZ_QTY, SZ_UV
from .report2 import _e, _money, _pct

DEFAULT_A = ("2026-11-06", "2026-11-17")
DEFAULT_B = ("2026-11-19", "2026-11-22")


def _f(v, d=0.0) -> float:
    try:
        if v is None:
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _days(start: str, end: str) -> int:
    return max((dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1, 1)


def _load_sz(accounts, start: str, end: str) -> dict:
    """商智商品明细：sku -> {amt, ord, qty, uv, account, label, name}。"""
    out: dict = {}
    for a in accounts:
        win = dailystore.load_range_window(a["key"], "sz_product", start, end)
        for r in win.get("rows") or []:
            sid = str(r.get("sku_id") or "")
            if not sid or r.get("$summary"):
                continue
            row = {"amt": _f(r.get(SZ_AMT)), "ord": _f(r.get(SZ_ORDN)),
                   "qty": _f(r.get(SZ_QTY)), "uv": _f(r.get(SZ_UV)),
                   "account": a["key"], "label": a.get("label") or a["key"],
                   "name": r.get("name") or ""}
            old = out.get(sid)
            if old is None or row["amt"] > old["amt"]:
                out[sid] = row
    return out


def _plan_members(plan: dict) -> dict:
    """把 SPU 级花费/全站成交按成员商智成交占比拆到 SKU。"""
    w = (plan.get("wins") or {}).get("w2") or {}
    cost, amt = _f(w.get("cost")), _f(w.get("amt"))
    members = plan.get("members") or []
    n = len(members)
    sz = [_f(m.get("szAmt")) for m in members]
    tot = sum(sz)
    out = {}
    for i, m in enumerate(members):
        share = (sz[i] / tot) if tot else (1 / n if n else 0)
        out[m["skuId"]] = {"cost": cost * share, "amt": amt * share,
                           "ord": _f(w.get("ord")) * share,
                           "szAmt": sz[i], "szQty": _f(m.get("szQty")),
                           "name": m.get("name") or ""}
    return out


def _money_or_dash(v) -> str:
    return "—" if not v else "¥" + _money(v)


def _daily_series(accounts, groups, start: str, end: str) -> dict:
    """按天算每个分组的商智成交额、快车/智能花费、全站营销花费。

    图表口径与独立 HTML 报告一致：
      * 商智成交额 = 分组所属店铺的商智商品明细（同 SKU 求和）；
      * 快车/智能 = jst_sku + kw_ad（按 SKU），jzt campaignType=153（按 SPU）；
      * 全站营销 = swa_campaign（按 SPU）。
    广告账户可能不是店铺本身（例如雨润禾账户投梓云茶玉米），所以花费跨所选账号求和。
    """
    days = dailystore.day_span(start, end)
    cache = {}

    def load(acct, day, kind):
        key = (acct, day, kind)
        if key not in cache:
            cache[key] = dailystore.load_day(acct, day, kind) or {}
        return cache[key]

    out = {}
    for gkey, g in groups.items():
        shop, _, spu = gkey.partition(":")
        rows = []
        for day in days:
            gmv = 0.0
            for r in load(shop, day, "sz_product").get("rows") or []:
                sid = str(r.get("sku_id") or "")
                if sid in g["skus"]:
                    gmv += _f(r.get(SZ_AMT))
            ad = 0.0
            swa = 0.0
            for a in accounts:
                k = a["key"]
                for r in load(k, day, "jst_sku").get("rows") or []:
                    if str(r.get("skuId") or "") in g["skus"]:
                        ad += _f(r.get("cost"))
                for r in load(k, day, "kw_ad").get("rows") or []:
                    if str(r.get("skuId") or "") in g["skus"]:
                        ad += _f(r.get("cost"))
                if spu and spu != "fast":
                    for r in load(k, day, "jzt_campaign").get("rows") or []:
                        if _f(r.get("campaignType")) == 153 and \
                                str(r.get("spuId") or r.get("childProNo") or "") == spu:
                            ad += _f(r.get("cost"))
                    for r in load(k, day, "swa_campaign").get("rows") or []:
                        if str(r.get("sxuId") or "") == spu:
                            swa += _f(r.get("cost"))
            rows.append({"d": day, "gmv": gmv, "ad": ad, "swa": swa})
        out[gkey] = rows
    return out


def build(a_start: str, a_end: str, b_start: str, b_end: str, accounts: list) -> dict:
    A = analyze2.build(start=a_start, end=a_end, accounts=accounts)
    B = analyze2.build(start=b_start, end=b_end, accounts=accounts)
    sz_a = _load_sz(accounts, a_start, a_end)
    sz_b = _load_sz(accounts, b_start, b_end)

    # SKU -> 店铺（以两个区间里商智成交更大的那次为准）
    shop = {}
    for m in (sz_a, sz_b):
        for sid, r in m.items():
            if sid not in shop or r["amt"] > shop[sid]["amt"]:
                shop[sid] = r

    # SKU -> 分组（店铺:SPU）；计划在任一区间出现都算同一组
    plans, sku_group = {}, {}
    for D in (A, B):
        for p in (D.get("swa") or {}).get("plans") or []:
            key = f"{p.get('account')}:{p.get('spu')}"
            plans[key] = p
            for m in p.get("members") or []:
                sku_group[m["skuId"]] = key

    universe = set()
    for D in (A, B):
        for s in D.get("skus") or []:
            universe.add(s["skuId"])
        for p in (D.get("swa") or {}).get("plans") or []:
            for m in p.get("members") or []:
                universe.add(m["skuId"])
    for sid in universe:
        if sid not in sku_group:
            sh = shop.get(sid) or {}
            key = sh.get("account")
            if not key:
                # 广告在投、但商智没有成交：按实际花钱的广告账号归组，避免出现 “?:fast”
                for D in (A, B):
                    s = next((x for x in D.get("skus") or [] if x["skuId"] == sid), None)
                    if s and s.get("byAccount"):
                        key = max(s["byAccount"].items(),
                                  key=lambda kv: _f(kv[1].get("cost")))[0]
                        break
            sku_group[sid] = f"{key or '?'}:fast"

    groups = {}
    for sid, gkey in sku_group.items():
        if sid not in universe:
            continue
        g = groups.setdefault(gkey, {"key": gkey, "skus": [], "label": "", "sub": ""})
        g["skus"].append(sid)

    for gkey, g in groups.items():
        sh, spu = gkey.split(":", 1)
        p = plans.get(gkey)
        if p:
            g["label"] = p.get("name") or f"SPU {spu}"
            g["sub"] = f"{p.get('accountLabel') or sh} · SPU {spu}"
        else:
            label = next((r["label"] for r in shop.values() if r["account"] == sh), sh)
            g["label"] = f"{label} · 快车/智能（其他）"
            g["sub"] = f"店铺 {label}"

    model = analyze2.load_cost_model()
    costs = settings.load_costs()
    units = settings.costs_unit_map()
    margin_cache = {}

    def margin_of(sid: str, gmv: float, qty: float):
        key = (sid, round(gmv / qty, 2) if qty else 0)
        if key in margin_cache:
            return margin_cache[key]
        c = analyze2.sku_cost_of(sid, (gmv / qty) if qty else 0, model, costs, units)
        val = (_f(c.get("margin")), _f(c.get("breakeven")))
        margin_cache[key] = val
        return val

    def agg(D, szmap):
        skurows = {s["skuId"]: s for s in D.get("skus") or []}
        swa_members = {}
        for p in (D.get("swa") or {}).get("plans") or []:
            swa_members.update(_plan_members(p))
        out = {}
        for gkey, g in groups.items():
            r = defaultdict(float)
            per = {}
            for sid in g["skus"]:
                z = szmap.get(sid) or {}
                s = skurows.get(sid) or {}
                gmv, qty = _f(z.get("amt")), _f(z.get("qty"))
                r["gmv"] += gmv
                r["ord"] += _f(z.get("ord"))
                r["qty"] += qty
                r["uv"] += _f(z.get("uv"))
                ad_cost = ad_amt = 0.0
                if s:
                    # 只取所选区间(w2)的广告花费；byAccount 存的是「最近一个有花费的窗口」，
                    # 在快车已停投的 B 期会误带出上一期的花费，不能直接用。
                    w2 = (s.get("wins") or {}).get("w2") or {}
                    ad_cost = _f(w2.get("cost"))
                    ad_amt = _f(w2.get("amt"))
                # 毛利率统一用同一份成本重算，避免 A/B 各自用各自窗口的 ERP 均价，
                # 把“成本变了”误读成“效率变了”。分母仍用各期自己的前台件单价。
                mg, be = margin_of(sid, gmv, qty)
                m = swa_members.get(sid) or {}
                swa_cost, swa_amt, swa_ord = _f(m.get("cost")), _f(m.get("amt")), _f(m.get("ord"))
                r["adCost"] += ad_cost
                r["adAmt"] += ad_amt
                r["swaCost"] += swa_cost
                r["swaAmt"] += swa_amt
                r["swaOrd"] += swa_ord
                r["marginAmt"] += mg * gmv
                spend = ad_cost + swa_cost
                per[sid] = {"gmv": gmv, "ord": _f(z.get("ord")), "uv": _f(z.get("uv")),
                            "adCost": ad_cost, "adAmt": ad_amt,
                            "swaCost": swa_cost, "swaAmt": swa_amt, "swaOrd": swa_ord,
                            "spend": spend, "fee": (spend / gmv) if gmv else 0.0,
                            "margin": mg, "breakeven": be,
                            "profit": mg * gmv - spend,
                            "name": (s.get("name") if s else "") or (m.get("name") or (z.get("name") or ""))}
            r["spend"] = r["adCost"] + r["swaCost"]
            r["fee"] = (r["spend"] / r["gmv"]) if r["gmv"] else 0.0
            r["margin"] = (r["marginAmt"] / r["gmv"]) if r["gmv"] else 0.0
            r["profit"] = r["marginAmt"] - r["spend"]
            r["roiStore"] = (r["gmv"] / r["spend"]) if r["spend"] else 0.0
            r["roiAd"] = (r["adAmt"] / r["adCost"]) if r["adCost"] else 0.0
            r["roiSwa"] = (r["swaAmt"] / r["swaCost"]) if r["swaCost"] else 0.0
            r["swaShare"] = (r["swaAmt"] / r["gmv"]) if r["gmv"] else 0.0
            out[gkey] = {"agg": dict(r), "sku": per}
        return out

    ga, gb = agg(A, sz_a), agg(B, sz_b)

    def total(g):
        t = defaultdict(float)
        for x in g.values():
            for k in ("gmv", "ord", "qty", "uv", "adCost", "adAmt", "swaCost",
                      "swaAmt", "spend", "marginAmt", "profit"):
                t[k] += x["agg"][k]
        t["fee"] = (t["spend"] / t["gmv"]) if t["gmv"] else 0.0
        t["margin"] = (t["marginAmt"] / t["gmv"]) if t["gmv"] else 0.0
        t["roiStore"] = (t["gmv"] / t["spend"]) if t["spend"] else 0.0
        return dict(t)

    rows = []
    for gkey, g in groups.items():
        a, b = ga.get(gkey, {}), gb.get(gkey, {})
        aagg = (a.get("agg") or {})
        bagg = (b.get("agg") or {})
        if not _f(aagg.get("gmv")) and not _f(bagg.get("gmv")) \
                and not _f(aagg.get("spend")) and not _f(bagg.get("spend")):
            continue
        skus = []
        for sid in g["skus"]:
            sa = (a.get("sku") or {}).get(sid)
            sb = (b.get("sku") or {}).get(sid)
            if not sa and not sb:
                continue
            skus.append((sid, sa, sb))
        skus.sort(key=lambda x: max(_f((x[1] or {}).get("gmv")), _f((x[2] or {}).get("gmv"))), reverse=True)
        rows.append({"key": gkey, "label": g["label"], "sub": g["sub"],
                     "a": aagg, "b": bagg, "sku": skus,
                     "isPlan": gkey in plans,
                     "orphan": not (_f(aagg.get("gmv")) or _f(bagg.get("gmv")))})
    # 有 SPU 计划的组排前面，其余按商智成交降序；完全没有商智成交的“纯广告”组放最后
    rows.sort(key=lambda r: (r["orphan"], not r["isPlan"],
                             -max(_f(r["a"].get("gmv")), _f(r["b"].get("gmv")))))

    def _sum_rows(side: str, pred) -> dict:
        t = defaultdict(float)
        for r in rows:
            if not pred(r):
                continue
            x = r[side]
            for k in ("gmv", "ord", "qty", "uv", "adCost", "adAmt", "swaCost",
                      "swaAmt", "swaOrd", "spend", "marginAmt", "profit"):
                t[k] += _f(x.get(k))
        t["fee"] = (t["spend"] / t["gmv"]) if t["gmv"] else 0.0
        t["margin"] = (t["marginAmt"] / t["gmv"]) if t["gmv"] else 0.0
        t["roiStore"] = (t["gmv"] / t["spend"]) if t["spend"] else 0.0
        return dict(t)

    valid = lambda r: not r["orphan"]  # noqa: E731
    plan_only = lambda r: r["isPlan"] and not r["orphan"]  # noqa: E731
    # 图表多给 17 天前置上下文；末端就到 B 期最后一天，避免把今天未结算的数据画进来
    chart_start = (dt.date.fromisoformat(min(a_start, b_start)) - dt.timedelta(days=17)).isoformat()
    chart_end = max(a_end, b_end)
    series = _daily_series(accounts, groups, chart_start, chart_end)
    return {
        "meta": {
            "a": {"start": a_start, "end": a_end, "days": _days(a_start, a_end)},
            "b": {"start": b_start, "end": b_end, "days": _days(b_start, b_end)},
            "accounts": [{"key": a["key"], "label": a.get("label") or a["key"]} for a in accounts],
            "generatedAt": settings.now_str("%Y-%m-%d %H:%M"),
        },
        "groups": rows,
        "series": series,
        "totals": {
            "all": {"a": _sum_rows("a", valid), "b": _sum_rows("b", valid)},
            "plan": {"a": _sum_rows("a", plan_only), "b": _sum_rows("b", plan_only)},
        },
    }


# ---------------------------------------------------------------- AI 提示词与页面渲染
from .report_ui.vs_view import (
    _headline,
    render_vs as render,
    render_vs_error as error_page,
)


def insight_prompt(d) -> str:
    """把当前 A/B 对比压缩成一段给现有 LLM 接口的提示词。"""
    a, b, scope = _headline(d)
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    lines = [
        "你是京东广告投放分析师。下面是「史上最强(A) vs 现代最强(B)」两区间的同口径对比，"
        "请用中文输出 3~5 条结论，每条不超过 80 字。",
        "关键前提：全站营销的『全站成交』≈ 商智整店成交（含自然单），快车 ROI 只算点击归因；"
        "两者平台 ROI 不能直接比，只能比『广告总花费 ÷ 商智成交额』（费比）和净利。",
        f"A 期：{d['meta']['a']['start']} ~ {d['meta']['a']['end']}（{da}天）；"
        f"B 期：{d['meta']['b']['start']} ~ {d['meta']['b']['end']}（{db}天）。",
        f"总计（{scope}）：A 花费 {a['spend']/da:.0f}/天（快车 {a['adCost']/da:.0f} + 全站 {a['swaCost']/da:.0f}）、"
        f"商智成交 {a['gmv']/da:.0f}/天、费比 {a['fee']*100:.2f}%、净利 {a['profit']/da:.0f}/天；"
        f"B 花费 {b['spend']/db:.0f}/天（快车 {b['adCost']/db:.0f} + 全站 {b['swaCost']/db:.0f}）、"
        f"商智成交 {b['gmv']/db:.0f}/天、费比 {b['fee']*100:.2f}%、净利 {b['profit']/db:.0f}/天。",
        f"全账号合计（含其他快车商品）：A 花费 {d['totals']['all']['a']['spend']/da:.0f}/天"
        f"（快车 {d['totals']['all']['a']['adCost']/da:.0f} + 全站 {d['totals']['all']['a']['swaCost']/da:.0f}）、"
        f"B 花费 {d['totals']['all']['b']['spend']/db:.0f}/天"
        f"（快车 {d['totals']['all']['b']['adCost']/db:.0f} + 全站 {d['totals']['all']['b']['swaCost']/db:.0f}）。",
        "分组：",
    ]
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        x, y = g["a"], g["b"]
        lines.append(
            f"- {g['label'][:36]}（{g['sub']}）："
            f"A 花费 {_f(x.get('spend'))/da:.0f}/天（快车 {_f(x.get('adCost'))/da:.0f} + 全站 {_f(x.get('swaCost'))/da:.0f}）、"
            f"成交 {_f(x.get('gmv'))/da:.0f}/天、费比 {_f(x.get('fee'))*100:.2f}%、"
            f"毛利 {_f(x.get('margin'))*100:.1f}%、净利 {_f(x.get('profit'))/da:.0f}/天；"
            f"B 花费 {_f(y.get('spend'))/db:.0f}/天（快车 {_f(y.get('adCost'))/db:.0f} + 全站 {_f(y.get('swaCost'))/db:.0f}）、"
            f"成交 {_f(y.get('gmv'))/db:.0f}/天、费比 {_f(y.get('fee'))*100:.2f}%、"
            f"毛利 {_f(y.get('margin'))*100:.1f}%、净利 {_f(y.get('profit'))/db:.0f}/天。")
    lines.append("其他快车/非计划组（未切全站，但影响全账号费比）：")
    for g in d["groups"]:
        if g.get("isPlan") or g.get("orphan"):
            continue
        x, y = g["a"], g["b"]
        if _f(x.get("gmv")) + _f(y.get("gmv")) < 500:
            continue
        lines.append(
            f"- {g['label'][:30]}：A 花费 {_f(x.get('spend'))/da:.0f}/天、成交 {_f(x.get('gmv'))/da:.0f}/天、"
            f"费比 {_f(x.get('fee'))*100:.2f}%；B 花费 {_f(y.get('spend'))/db:.0f}/天、"
            f"成交 {_f(y.get('gmv'))/db:.0f}/天、费比 {_f(y.get('fee'))*100:.2f}%。")
    risks = []
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        for sid, sa, sb in g["sku"]:
            s = sb or sa
            if not s:
                continue
            fee, mg = _f(s.get("fee")), _f(s.get("margin"))
            if fee > 0 and mg > 0 and fee >= mg:
                risks.append(f"{s.get('name') or sid}（费比 {fee*100:.2f}% / 毛利 {mg*100:.1f}%）")
    if risks:
        lines.append("风险 SKU（费比≥毛利）：" + "；".join(risks[:6]) + "。")
    lines.append(
        f"请重点回答：1) 哪个方案更省钱、成交是否保住；2) 哪个组/商品有毛利风险；"
        f"3) 下一步预算/目标 ROI/SKU 取舍的具体动作；4) 提醒不要用平台 ROI 直接比。"
        f"注意：B 期共 {db} 天，样本较短时不要给确定性放量结论；任何放量建议必须加"
        f"『先做 7 天 A/B 验证』。不要外推亏损天数或累计金额，不要编造数据。"
        f"净利变化要区分『毛利率变化』和『广告效率变化』，不要只归因于广告。")
    return "\n".join(lines)
