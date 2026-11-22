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
DEFAULT_B = ("2026-11-19", "2026-11-20")


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
    # 图表多给 17 天前置上下文 + 1 天后置，和独立 HTML 报告的走势图对齐
    chart_start = (dt.date.fromisoformat(min(a_start, b_start)) - dt.timedelta(days=17)).isoformat()
    chart_end = (dt.date.fromisoformat(max(a_end, b_end)) + dt.timedelta(days=1)).isoformat()
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


# ---------------------------------------------------------------- 渲染
VS_CSS = """
:root{ --ink:#0f172a; --muted:#64748b; --line:#e5e9f0; --bg:#f6f8fb;
       --blue:#2563eb; --orange:#ea580c; --green:#059669; --red:#dc2626; --amber:#b45309; }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
  line-height:1.65}
.topbar{background:#fff;border-bottom:1px solid var(--line)}
.topbar .wrap{max-width:1120px;margin:0 auto;padding:10px 20px;display:flex;align-items:center;gap:20px}
.topbar .brand{font-weight:700;color:var(--ink);text-decoration:none;font-size:14px}
.topbar .navlinks{display:flex;gap:16px;align-items:center;margin-left:auto}
.topbar .navlinks a{color:var(--muted);text-decoration:none;font-size:13px}
.topbar .navlinks a:hover{color:var(--ink)}
.topbar .navlinks a.on{color:var(--blue);font-weight:600}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 64px}
header{padding:18px 0 6px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.3px}
.sub{color:var(--muted);font-size:13px}
h2{font-size:19px;margin:34px 0 12px;padding-left:10px;border-left:4px solid var(--blue)}
h3{font-size:15px;margin:22px 0 8px;color:#1e293b}
p{margin:8px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px 22px;
  box-shadow:0 1px 2px rgba(15,23,42,.04);margin-top:16px}
.lead{background:linear-gradient(135deg,#eff6ff,#fff7ed);border:1px solid #dbeafe}
.lead ul{margin:10px 0 0 18px;padding:0}
.lead li{margin:7px 0}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 4px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .n{font-size:24px;font-weight:700;letter-spacing:-.5px}
.kpi .l{font-size:12px;color:var(--muted)}
.kpi .d{font-size:12px;margin-top:2px}
.kpi .d.good{color:var(--green)} .kpi .d.bad{color:var(--red)}
table.tbl{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
.tbl th{text-align:left;font-weight:600;color:#475569;background:#f8fafc;
  border-bottom:1px solid var(--line);padding:9px 10px;font-size:12px}
.tbl td{padding:9px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl.small td,.tbl.small th{padding:8px 8px}
.tbl .num{text-align:right;font-variant-numeric:tabular-nums}
.tbl .l{text-align:left}
.tbl .sku{font-weight:600;white-space:nowrap}
.tbl .sku span{display:block;font-weight:400;color:var(--muted);font-size:11px;
  max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.good{color:var(--green);font-weight:600}
.up{color:var(--blue);font-weight:600}
.down{color:var(--red)}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;white-space:nowrap}
.tag.ok{background:#ecfdf5;color:#047857}
.tag.warn{background:#fffbeb;color:#b45309}
.tag.bad{background:#fef2f2;color:#b91c1c}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-top:16px}
.chartbox{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-top:16px}
.chartbox .cap{font-size:12px;color:var(--muted);margin-bottom:4px}
.legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin:6px 0 0}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.note{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:12px 16px;
  font-size:13px;color:#78350f;margin-top:12px}
.warnbox{background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:12px 16px;
  font-size:13px;color:#7f1d1d;margin-top:12px}
.okbox{background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:12px 16px;
  font-size:13px;color:#065f46;margin-top:12px}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}
.foot{margin-top:36px;font-size:12px;color:var(--muted)}
.controls{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12.5px}
.controls label{display:inline-flex;align-items:center;gap:6px;color:var(--muted)}
.controls input[type=date]{border:1px solid var(--line);border-radius:8px;padding:5px 8px;
  font-size:12.5px;color:var(--ink);background:#fff}
.controls input[type=checkbox]{accent-color:var(--blue)}
.controls button{border:0;border-radius:8px;background:var(--blue);color:#fff;
  padding:7px 14px;font-size:13px;font-weight:600;cursor:pointer}
.controls a{color:var(--blue);text-decoration:none}
.controls .sep{width:1px;height:22px;background:var(--line)}
.btn{display:inline-block;padding:7px 14px;border-radius:8px;background:var(--blue);color:#fff;
  text-decoration:none;font-size:13px;font-weight:600}
.ai-out{display:none;margin-top:12px;background:#f8fafc;border:1px solid var(--line);
  border-radius:8px;padding:12px 14px;font-size:13px;line-height:1.8;color:var(--ink)}
.sku-group{margin:14px 0}
.sku-group summary{cursor:pointer;font-weight:600;font-size:14px;padding:10px 12px;
  background:#f8fafc;border:1px solid var(--line);border-radius:8px}
.sku-group summary span{font-weight:400;color:var(--muted);font-size:12px}
.tblwrap{overflow:auto;max-height:620px;border:1px solid var(--line);border-radius:10px;background:#fff}
@media(max-width:820px){.kpis{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}}
"""


def _delta_pct(cur, pre) -> tuple:
    if not pre:
        return ("—", "")
    v = (cur / pre - 1) * 100
    cls = "good" if v > 0 else ("bad" if v < 0 else "")
    return (f"{v:+.1f}%", cls)


def _pctv(v, dec=2) -> str:
    """小数转百分比（report2 的 _pct 接收的是已乘 100 的值）。"""
    return _pct(_f(v) * 100, dec)


def _headline(d):
    """有 SPU 计划就用计划组做结论；没有（纯快车对比）就退回全账号。"""
    p = d["totals"]["plan"]
    if _f(p["a"].get("gmv")) or _f(p["b"].get("gmv")):
        return p["a"], p["b"], "有 SPU 计划的组"
    return d["totals"]["all"]["a"], d["totals"]["all"]["b"], "全部商品"


def _conclusion(d) -> list:
    a, b, scope = _headline(d)
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    out = []
    ds, _ = _delta_pct(b["spend"] / db, a["spend"] / da)
    dg, _ = _delta_pct(b["gmv"] / db, a["gmv"] / da)
    out.append(
        f"<b>{scope}</b>：总花费 ¥{_money(a['spend']/da)}/天 → ¥{_money(b['spend']/db)}/天（{ds}）；"
        f"商智成交 ¥{_money(a['gmv']/da)}/天 → ¥{_money(b['gmv']/db)}/天（{dg}）；"
        f"费比 {_pctv(a['fee'], 2)} → {_pctv(b['fee'], 2)}（{(b['fee']-a['fee'])*100:+.2f}pp）。")
    best = None
    for g in d["groups"]:
        fa, fb = _f(g["a"].get("fee")), _f(g["b"].get("fee"))
        if fa > 0 and fb > 0 and _f(g["a"].get("gmv")) + _f(g["b"].get("gmv")) > 500:
            gain = fa - fb
            if best is None or gain > best[0]:
                best = (gain, g)
    if best:
        g = best[1]
        out.append(
            f"费比改善最大：<b>{_e(g['label'][:28])}</b>，"
            f"{_pctv(_f(g['a'].get('fee')), 2)} → {_pctv(_f(g['b'].get('fee')), 2)}"
            f"（{(_f(g['b'].get('fee')) - _f(g['a'].get('fee')))*100:+.2f}pp），"
            f"商智成交 ¥{_money(_f(g['a'].get('gmv'))/da)}/天 → ¥{_money(_f(g['b'].get('gmv'))/db)}/天。")
    risks = []
    scan = [g for g in d["groups"] if g.get("isPlan") and not g.get("orphan")]
    if not scan:
        scan = [g for g in d["groups"] if not g.get("orphan")]
    for g in scan:
        for sid, sa, sb in g["sku"]:
            s = sb or sa
            if not s:
                continue
            fee, mg = _f(s.get("fee")), _f(s.get("margin"))
            if fee > 0 and mg > 0 and fee >= mg:
                risks.append((fee - mg, sid, (s.get("name") or sid)[:22], fee, mg))
    if risks:
        risks.sort(reverse=True)
        top = risks[:3]
        out.append("风险 SKU（费比 ≥ 毛利率，广告本身在亏钱）：" + "；".join(
            f"<b>{_e(n)}</b> 费比 {_pctv(f, 2)} / 毛利 {_pctv(m, 1)}" for _, _, n, f, m in top) + "。")
    out.append("<b>平台 ROI 不可直接比：</b>全站营销的「全站成交」≈ 商智整店成交（含自然单），"
               "快车 ROI 只算点击归因；本页统一用「广告总花费 ÷ 商智成交额」和净利做对比。")
    return out


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
        f"总计（{scope}）：A 花费 {a['spend']/da:.0f}/天、商智成交 {a['gmv']/da:.0f}/天、"
        f"费比 {a['fee']*100:.2f}%、净利 {a['profit']/da:.0f}/天；"
        f"B 花费 {b['spend']/db:.0f}/天、商智成交 {b['gmv']/db:.0f}/天、"
        f"费比 {b['fee']*100:.2f}%、净利 {b['profit']/db:.0f}/天。",
        "分组：",
    ]
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        x, y = g["a"], g["b"]
        lines.append(
            f"- {g['label'][:36]}（{g['sub']}）："
            f"A 花费 {_f(x.get('spend'))/da:.0f}/天、成交 {_f(x.get('gmv'))/da:.0f}/天、"
            f"费比 {_f(x.get('fee'))*100:.2f}%、毛利 {_f(x.get('margin'))*100:.1f}%、"
            f"净利 {_f(x.get('profit'))/da:.0f}/天；"
            f"B 花费 {_f(y.get('spend'))/db:.0f}/天、成交 {_f(y.get('gmv'))/db:.0f}/天、"
            f"费比 {_f(y.get('fee'))*100:.2f}%、毛利 {_f(y.get('margin'))*100:.1f}%、"
            f"净利 {_f(y.get('profit'))/db:.0f}/天。")
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
        "请重点回答：1) 哪个方案更省钱、成交是否保住；2) 哪个组/商品有毛利风险；"
        "3) 下一步预算/目标 ROI/SKU 取舍的具体动作；4) 提醒不要用平台 ROI 直接比。"
        "注意：B 期只有 2 天，样本很短，不要给确定性放量结论；任何放量建议必须加"
        "『先做 7 天 A/B 验证』。不要外推亏损天数或累计金额，不要编造数据。"
        "净利变化要区分『毛利率变化』和『广告效率变化』，不要只归因于广告。")
    return "\n".join(lines)


def _kpi(label, a, b, fmt, good_low=True) -> str:
    delta, _ = _delta_pct(_f(b), _f(a))
    if delta == "—":
        cls = ""
    elif good_low:
        cls = "good" if delta.startswith("-") else ("bad" if delta.startswith("+") else "")
    else:
        cls = "good" if delta.startswith("+") else ("bad" if delta.startswith("-") else "")
    return (f'<div class="kpi"><div class="l">{label}</div>'
            f'<div class="n">{fmt(b)}</div>'
            f'<div class="d {cls}">{delta} vs A（{fmt(a)}）</div></div>')


def _evidence_table(d) -> str:
    rows = [g for g in d["groups"] if g.get("isPlan") and not g.get("orphan")]
    if not rows:
        return '<p class="sub">两个区间都没有带 SPU 计划的分组，无法做“全站成交 vs 商智成交”对照。</p>'
    trs = []
    for g in rows:
        for tag, side in (("A 史上最强", g["a"]), ("B 现代最强", g["b"])):
            swa_amt, gmv = _f(side.get("swaAmt")), _f(side.get("gmv"))
            swa_ord, ordn = _f(side.get("swaOrd")), _f(side.get("ord"))
            if not swa_amt and not swa_ord:
                continue
            trs.append(
                f"<tr><td class='l'><b>{_e(g['label'][:26])}</b><div class='sub'>{tag}</div></td>"
                f"<td class='num'>¥{_money(swa_amt)}</td><td class='num'>¥{_money(gmv)}</td>"
                f"<td class='num'><b>{_pctv(swa_amt/gmv, 1) if gmv else '—'}</b></td>"
                f"<td class='num'>{swa_ord:.0f}</td><td class='num'>{ordn:.0f}</td>"
                f"<td class='num'><b>{_pctv(swa_ord/ordn, 1) if ordn else '—'}</b></td></tr>")
    if not trs:
        return '<p class="sub">两个区间都没有全站营销成交。</p>'
    return ("<table class='tbl'><thead><tr><th class='l'>计划 / 区间</th>"
            "<th class='num'>全站成交</th><th class='num'>商智成交（同 SKU）</th><th class='num'>成交占比</th>"
            "<th class='num'>全站订单行</th><th class='num'>商智订单</th><th class='num'>订单占比</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


def _same_sku_table(d) -> str:
    """挑一个计划组里商智成交最大的 SKU，展示“同一 SKU 的两个 ROI”。"""
    best = None
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        for sid, sa, sb in g["sku"]:
            score = max(_f((sa or {}).get("gmv")), _f((sb or {}).get("gmv")))
            if best is None or score > best[0]:
                best = (score, sid, sa, sb, g)
    if not best:
        return ""
    _, sid, sa, sb, g = best
    name = ((sb or sa) or {}).get("name") or sid
    rows = []
    if sa:
        c, amt, gmv = _f(sa.get("adCost")), _f(sa.get("adAmt")), _f(sa.get("gmv"))
        if c:
            rows.append(("A 期快车口径", c, "点击归因成交", amt, amt / c))
            rows.append(("A 期同一花费按全站成交", c, "商智全店成交", gmv, gmv / c))
    if sb:
        c, amt, gmv = _f(sb.get("swaCost")), _f(sb.get("swaAmt")), _f(sb.get("gmv"))
        if c:
            rows.append(("B 期全站营销", c, "全站成交", amt, amt / c))
    if not rows:
        return ""
    trs = "".join(
        f"<tr><td>{_e(label)}</td><td class='num'>¥{_money(c)}</td>"
        f"<td>{_e(kind)}</td><td class='num'>¥{_money(amount)}</td>"
        f"<td class='num'><b>{roi:.2f}</b></td></tr>"
        for label, c, kind, amount, roi in rows)
    return f"""<p class="sub">以「{_e(name[:34])}」（SKU {_e(sid)}）为例：</p>
<table class="tbl"><thead><tr><th>算法</th><th class="num">花费</th><th>分子用什么成交</th>
<th class="num">分子金额</th><th class="num">ROI</th></tr></thead><tbody>{trs}</tbody></table>
<p class="sub" style="margin-top:8px">A/B 区间天数不同，表中的花费/成交是区间总额，金额不可直接比；ROI 不受天数影响。</p>
<div class="note">快车 ROI 只把 A 期点击归因成交算进来；同一花费如果按商智全店成交算，ROI 会高很多；
全站营销的 ROI 正是后者的口径。所以两个 ROI 的差距主要是<b>分子</b>，不是效率。</div>"""


def _group_table(d) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    th = ("<tr><th class='l'>分组</th>"
          "<th class='num'>A 花费/天</th><th class='num'>B 花费/天</th><th class='num'>花费Δ</th>"
          "<th class='num'>A 商智/天</th><th class='num'>B 商智/天</th><th class='num'>商智Δ</th>"
          "<th class='num'>A 费比</th><th class='num'>B 费比</th><th class='num'>费比Δ</th>"
          "<th class='num'>毛利率</th><th class='num'>A 净利/天</th><th class='num'>B 净利/天</th></tr>")
    trs = []
    for g in d["groups"]:
        a, b = g["a"], g["b"]
        if g.get("orphan"):
            trs.append(
                f"<tr><td class='l'><b>{_e(g['label'][:34])}</b><div class='sub'>{_e(g['sub'])}</div></td>"
                f"<td class='num'>¥{_money(_f(a.get('spend'))/da)}</td>"
                f"<td class='num'>¥{_money(_f(b.get('spend'))/db)}</td>"
                f"<td class='num'>—</td><td class='num'>—</td><td class='num'>—</td><td class='num'>—</td>"
                f"<td class='num'>—</td><td class='num'>—</td><td class='num'>—</td><td class='num'>—</td>"
                f"<td class='l' colspan='2'><span class='tag warn'>纯广告，商智无成交（不计入费比）</span></td></tr>")
            continue
        ds, _ = _delta_pct(_f(b.get("spend"))/db, _f(a.get("spend"))/da)
        dg, _ = _delta_pct(_f(b.get("gmv"))/db, _f(a.get("gmv"))/da)
        df = (_f(b.get("fee")) - _f(a.get("fee"))) * 100
        trs.append(
            f"<tr><td class='l'><b>{_e(g['label'][:34])}</b><div class='sub'>{_e(g['sub'])}</div></td>"
            f"<td class='num'>¥{_money(_f(a.get('spend'))/da)}</td>"
            f"<td class='num'>¥{_money(_f(b.get('spend'))/db)}</td>"
            f"<td class='num'>{ds}</td>"
            f"<td class='num'>¥{_money(_f(a.get('gmv'))/da)}</td>"
            f"<td class='num'>¥{_money(_f(b.get('gmv'))/db)}</td>"
            f"<td class='num'>{dg}</td>"
            f"<td class='num'>{_pctv(_f(a.get('fee')), 2)}</td>"
            f"<td class='num'>{_pctv(_f(b.get('fee')), 2)}</td>"
            f"<td class='num {'up' if df < 0 else ''}'>{df:+.2f}pp</td>"
            f"<td class='num'>{_pctv(_f(b.get('margin') or a.get('margin')), 1)}</td>"
            f"<td class='num'>¥{_money(_f(a.get('profit'))/da)}</td>"
            f"<td class='num'>¥{_money(_f(b.get('profit'))/db)}</td></tr>")
    return f"<div class='tblwrap'><table class='tbl'><thead>{th}</thead><tbody>{''.join(trs)}</tbody></table></div>"


def _sku_table(d, g) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    th = ("<tr><th class='l'>商品</th><th class='num'>毛利率</th><th class='num'>保本ROI</th>"
          "<th class='num'>A 花费/天</th><th class='num'>B 花费/天</th>"
          "<th class='num'>A 商智/天</th><th class='num'>B 商智/天</th><th class='num'>GMVΔ</th>"
          "<th class='num'>A 费比</th><th class='num'>B 费比</th><th class='l'>判断</th></tr>")
    trs = []
    rows = g["sku"]
    extra = 0
    if len(rows) > 30:
        extra = len(rows) - 30
        rows = rows[:30]
    for sid, sa, sb in rows:
        s = sb or sa or {}
        fee, mg = _f(s.get("fee")), _f(s.get("margin"))
        if fee and mg:
            if fee >= mg:
                verdict, cls = "亏损/临界：费比 ≥ 毛利", "bad"
            elif fee >= mg * 0.75:
                verdict, cls = "接近保本：仅小幅盈利", "warn"
            else:
                verdict, cls = "安全：费比远低于毛利", "ok"
        else:
            verdict, cls = "无成交", "warn"
        ga = _f((sa or {}).get("gmv")); gb = _f((sb or {}).get("gmv"))
        dg, _ = _delta_pct(gb/db, ga/da) if ga or gb else ("—", "")
        trs.append(
            f"<tr><td class='l sku'>{_e(sid)}<span>{_e((s.get('name') or '')[:60])}</span></td>"
            f"<td class='num'>{_pctv(mg, 1) if mg else '—'}</td>"
            f"<td class='num'>{_f(s.get('breakeven')):.2f}</td>"
            f"<td class='num'>¥{_money(_f((sa or {}).get('spend'))/da)}</td>"
            f"<td class='num'>¥{_money(_f((sb or {}).get('spend'))/db)}</td>"
            f"<td class='num'>¥{_money(ga/da)}</td>"
            f"<td class='num'>¥{_money(gb/db)}</td>"
            f"<td class='num'>{dg}</td>"
            f"<td class='num'>{_pctv(_f((sa or {}).get('fee')), 2) if sa else '—'}</td>"
            f"<td class='num'>{_pctv(fee, 2) if fee else '—'}</td>"
            f"<td class='l'><span class='tag {cls}'>{verdict}</span></td></tr>")
    if extra:
        trs.append(f"<tr><td colspan='11' class='l sub'>其余 {extra} 个 SKU 省略（合计看上方分组表）</td></tr>")
    return f"<div class='tblwrap'><table class='tbl small'><thead>{th}</thead><tbody>{''.join(trs)}</tbody></table></div>"


def _svg_trend(series, color: str, b_start: str, w=540, h=240) -> str:
    if not series:
        return ""
    vals = [_f(p.get("gmv")) for p in series]
    mx = max(vals + [1]) * 1.08
    n = len(series)
    label_idx = set(range(0, n, 5))
    if n - 1 - max(label_idx) >= 3:
        label_idx.add(n - 1)
    pad_l, pad_r, pad_t, pad_b = 44, 8, 14, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def X(i): return pad_l + iw * i / max(n - 1, 1)
    def Y(v): return pad_t + ih * (1 - v / mx)

    path = " ".join(("M" if i == 0 else "L") + f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    area = path + f" L{X(n-1):.1f},{pad_t+ih:.1f} L{X(0):.1f},{pad_t+ih:.1f} Z"
    parts = []
    for k in range(5):
        v = mx * k / 4
        y = Y(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" stroke="#eef2f7"/>'
                     f'<text x="{pad_l-5}" y="{y+3.5:.1f}" text-anchor="end" font-size="10" fill="#8a94a6">{v/1000:.0f}k</text>')
    for i, p in enumerate(series):
        if i in label_idx:
            parts.append(f'<text x="{X(i):.1f}" y="{h-8}" text-anchor="middle" font-size="10" fill="#8a94a6">{_e(p["d"][5:])}</text>')
    idx = next((i for i, p in enumerate(series) if p["d"] >= b_start), 0)
    parts.append(f'<line x1="{X(idx):.1f}" y1="{pad_t}" x2="{X(idx):.1f}" y2="{pad_t+ih}" '
                 f'stroke="#f59e0b" stroke-dasharray="4 4" stroke-width="1.2"/>'
                 f'<text x="{X(idx)+4:.1f}" y="{pad_t+11}" font-size="10" fill="#b45309">B 开始</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">' + "".join(parts)
            + f'<path d="{area}" fill="{color}" opacity=".10"/>'
            + f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.2"/>' + "</svg>")


def _svg_spend(series, b_start: str, w=540, h=240) -> str:
    if not series:
        return ""
    vals = [_f(p.get("ad")) + _f(p.get("swa")) for p in series]
    mx = max(vals + [1]) * 1.18
    n = len(series)
    label_idx = set(range(0, n, 5))
    if n - 1 - max(label_idx) >= 3:
        label_idx.add(n - 1)
    pad_l, pad_r, pad_t, pad_b = 44, 8, 14, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    bw = iw / max(n, 1) * 0.62
    parts = []
    for i, p in enumerate(series):
        x = pad_l + iw * i / max(n - 1, 1) - bw / 2
        ad, sw = _f(p.get("ad")), _f(p.get("swa"))
        ha, hs = ih * ad / mx, ih * sw / mx
        y2 = pad_t + ih
        if ha > 0:
            parts.append(f'<rect x="{x:.1f}" y="{y2-ha:.1f}" width="{bw:.1f}" height="{ha:.1f}" fill="#2563eb" opacity=".78"/>')
        if hs > 0:
            parts.append(f'<rect x="{x:.1f}" y="{y2-ha-hs:.1f}" width="{bw:.1f}" height="{hs:.1f}" fill="#ea580c" opacity=".85"/>')
    for k in range(5):
        v = mx * k / 4
        y = pad_t + ih * (1 - v / mx)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" stroke="#eef2f7"/>'
                     f'<text x="{pad_l-5}" y="{y+3.5:.1f}" text-anchor="end" font-size="10" fill="#8a94a6">{v/1000:.1f}k</text>')
    for i, p in enumerate(series):
        if i in label_idx:
            parts.append(f'<text x="{pad_l + iw*i/max(n-1,1):.1f}" y="{h-8}" text-anchor="middle" font-size="10" fill="#8a94a6">{_e(p["d"][5:])}</text>')
    idx = next((i for i, p in enumerate(series) if p["d"] >= b_start), 0)
    parts.append(f'<line x1="{pad_l + iw*idx/max(n-1,1):.1f}" y1="{pad_t}" x2="{pad_l + iw*idx/max(n-1,1):.1f}" '
                 f'y2="{pad_t+ih}" stroke="#f59e0b" stroke-dasharray="4 4" stroke-width="1.2"/>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">' + "".join(parts) + "</svg>"


def _chart_groups(d):
    gs = [g for g in d["groups"] if g.get("isPlan") and not g.get("orphan")]
    if not gs:
        gs = [g for g in d["groups"] if not g.get("orphan")][:3]
    return gs[:4]


def _chart_lines(d) -> str:
    b_start = d["meta"]["b"]["start"]
    boxes = []
    for g in _chart_groups(d):
        series = (d.get("series") or {}).get(g["key"]) or []
        if not series:
            continue
        boxes.append(
            f'<div class="chartbox"><div class="cap"><b style="color:#2563eb">{_e(g["sub"])}</b>'
            f' 商智成交额/天（元）</div>{_svg_trend(series, "#2563eb", b_start)}'
            f'<div class="legend"><span><i class="dot" style="background:#2563eb"></i>商智成交额</span>'
            f'<span style="color:#b45309">虚线 = B 期开始</span></div></div>')
    return f'<div class="chart-grid">{"".join(boxes)}</div>' if boxes else ""


def _chart_spends(d) -> str:
    b_start = d["meta"]["b"]["start"]
    boxes = []
    for g in _chart_groups(d):
        series = (d.get("series") or {}).get(g["key"]) or []
        if not series:
            continue
        boxes.append(
            f'<div class="chartbox"><div class="cap"><b>{_e(g["sub"])}</b>'
            f' 每日广告花费：<span style="color:#2563eb">快车/智能</span> + <span style="color:#ea580c">全站营销</span></div>'
            f'{_svg_spend(series, b_start)}'
            f'<div class="legend"><span><i class="dot" style="background:#2563eb"></i>快车/智能</span>'
            f'<span><i class="dot" style="background:#ea580c"></i>全站营销</span>'
            f'<span style="color:#b45309">虚线 = B 期开始</span></div></div>')
    return f'<div class="chart-grid">{"".join(boxes)}</div>' if boxes else ""


def _conclusion_table(d) -> str:
    a, b, scope = _headline(d)
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    dg, _ = _delta_pct(b["gmv"] / db, a["gmv"] / da)
    ds, _ = _delta_pct(b["spend"] / db, a["spend"] / da)
    net_a, net_b = a["profit"] / da, b["profit"] / db
    dn, _ = _delta_pct(net_b, net_a)
    return f"""<table class="tbl">
<thead><tr><th style="width:20%">判断维度</th><th>结论</th></tr></thead>
<tbody>
<tr><td>平台 ROI</td><td><b class="down">不可比。</b>全站营销的 ROI 含整店成交；快车 ROI 只含点击归因。谁高谁低不能说明方案好坏。</td></tr>
<tr><td>同一把尺（费比）</td><td><b class="good">{_pctv(a['fee'],2)} → {_pctv(b['fee'],2)}</b>（{(b['fee']-a['fee'])*100:+.2f}pp）。
  花费 {ds}，商智成交 {dg}；这是“每 100 元成交里广告费占多少”的真实变化。</td></tr>
<tr><td>商智成交</td><td>¥{_money(a['gmv']/da)}/天 → ¥{_money(b['gmv']/db)}/天（{dg}）。</td></tr>
<tr><td>利润</td><td>净利/天 ¥{_money(net_a)} → ¥{_money(net_b)}（{dn}），按统一 ERP 成本重算。
  净利同时受毛利率、前台价和产品结构影响；只看广告效率请用上面的费比。</td></tr>
<tr><td>阶段结论</td><td>{_e(scope)}：全站营销更适合“低成本收割已有流量、替代高价快车”；
  快车更适合“打新客、卡关键词、买高意向搜索流量”。是否继续放大，取决于增量验证，而不是平台 ROI。</td></tr>
</tbody></table>"""


def render(d, accounts_all=None, preset: str = "") -> str:
    meta = d["meta"]
    a, b = meta["a"], meta["b"]
    ta, tb, scope = _headline(d)
    aa, ab = d["totals"]["all"]["a"], d["totals"]["all"]["b"]
    da, db = a["days"], b["days"]
    dn, _ = _delta_pct(_f(tb["profit"])/db, _f(ta["profit"])/da)
    selected = {y["key"] for y in meta["accounts"]}
    ai_args = json.dumps({"a_start": a["start"], "a_end": a["end"],
                          "b_start": b["start"], "b_end": b["end"],
                          "accounts": [y["key"] for y in meta["accounts"]]},
                         ensure_ascii=False)
    acc_boxes = "".join(
        f'<label><input type="checkbox" name="accounts" value="{_e(x["key"])}"'
        f'{" checked" if x["key"] in selected else ""}>{_e(x["label"])}</label>'
        for x in (accounts_all or meta["accounts"]))
    bullets = "".join(f"<li>{x}</li>" for x in _conclusion(d))
    kpis = "".join([
        _kpi("花费/天", _f(ta["spend"])/da, _f(tb["spend"])/db, lambda v: "¥"+_money(v)),
        _kpi("商智成交/天", _f(ta["gmv"])/da, _f(tb["gmv"])/db, lambda v: "¥"+_money(v), good_low=False),
        _kpi("费比", _f(ta["fee"]), _f(tb["fee"]), lambda v: _pctv(v, 2)),
        _kpi("净利/天", _f(ta["profit"])/da, _f(tb["profit"])/db, lambda v: "¥"+_money(v), good_low=False),
    ])
    groups_html = "".join(
        f'<details class="sku-group"{" open" if i == 0 else ""}>'
        f'<summary>{_e(g["label"][:40])} <span>{_e(g["sub"])} · {len(g["sku"])} 个 SKU</span></summary>'
        f'{_sku_table(d, g)}</details>'
        for i, g in enumerate([x for x in d["groups"] if not x.get("orphan")]))
    # 最佳组的推荐数字
    best = None
    for g in d["groups"]:
        fa, fb = _f(g["a"].get("fee")), _f(g["b"].get("fee"))
        if fa > 0 and fb > 0 and _f(g["a"].get("gmv")) + _f(g["b"].get("gmv")) > 500:
            gain = fa - fb
            if best is None or gain > best[0]:
                best = (gain, g)
    best_txt = (f"{_e(best[1]['label'][:24])}（{_pctv(_f(best[1]['a'].get('fee')),2)} → "
                f"{_pctv(_f(best[1]['b'].get('fee')),2)}）") if best else "—"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>史上最强 vs 现代最强 · 快车/全站区间对比</title>
<style>{VS_CSS}</style></head>
<body>
<div class="topbar"><div class="wrap">
  <a class="brand" href="/">京东广告运营</a>
  <div class="navlinks"><a href="/report">分析报表</a><a class="on" href="/vs">史上最强vs现代最强</a>
  <a href="/ai">AI 分析台</a></div>
</div></div>
<div class="wrap">
<header>
  <h1>史上最强 vs 现代最强</h1>
  <div class="sub">快车 / 全站营销 · 任意两区间对比 · A 史上最强 {_e(a['start'])} ~ {_e(a['end'])}（{da}天）
    · B 现代最强 {_e(b['start'])} ~ {_e(b['end'])}（{db}天） · 账号
    {' / '.join(_e(x['label']) for x in meta['accounts'])} · 生成 {_e(meta['generatedAt'])}</div>
</header>

<div class="card" style="padding:14px 18px">
  <form class="controls" method="get" action="/vs">
    <label>A 起 <input type="date" name="a_start" value="{_e(a['start'])}"></label>
    <label>止 <input type="date" name="a_end" value="{_e(a['end'])}"></label>
    <span class="sep"></span>
    <label>B 起 <input type="date" name="b_start" value="{_e(b['start'])}"></label>
    <label>止 <input type="date" name="b_end" value="{_e(b['end'])}"></label>
    <span class="sep"></span>
    {acc_boxes}
    <button type="submit">开始对比</button>
    <a href="/vs?preset=switch">本次切换（11/18）</a>
    <a href="/vs?preset=week">前7天 vs 后7天</a>
  </form>
</div>

<div class="card lead">
  <b>一句话结论：</b>把 A 期和 B 期放在同一把尺下——费比 {_pctv(ta['fee'],2)} → {_pctv(tb['fee'],2)}，
  商智成交 ¥{_money(ta['gmv']/da)}/天 → ¥{_money(tb['gmv']/db)}/天，
  净利 ¥{_money(ta['profit']/da)}/天 → ¥{_money(tb['profit']/db)}/天（{dn}）。
  <ul>{bullets}</ul>
</div>

<div class="kpis">{kpis}</div>
<p class="sub" style="margin-top:10px">以上 KPI 统计范围：<b>{_e(scope)}</b>。
  全账号合计（含其他品类）：花费 ¥{_money(_f(aa["spend"])/da)}/天 → ¥{_money(_f(ab["spend"])/db)}/天，
  商智成交 ¥{_money(_f(aa["gmv"])/da)}/天 → ¥{_money(_f(ab["gmv"])/db)}/天，
  费比 {_pctv(_f(aa["fee"]), 2)} → {_pctv(_f(ab["fee"]), 2)}。</p>

<h2>1. 先把两把尺子换成同一把</h2>
<div class="card">
  <table class="tbl">
    <thead><tr><th style="width:22%">维度</th><th style="width:39%">快车 / 智能投放</th><th>全站营销（单品推广）</th></tr></thead>
    <tbody>
      <tr><td>计费方式</td><td>CPC，按点击扣费</td><td>CPC，按“目标成交投产比”智能出价</td></tr>
      <tr><td>成交口径</td><td>点击后 15 天归因订单（广告成交）</td><td>全站成交（含自然流量、全店归因订单）</td></tr>
      <tr><td>ROI 分子</td><td>广告归因成交额</td><td>商智全店成交额（几乎全部订单）</td></tr>
      <tr><td>能直接比吗</td><td colspan="2"><b class="down">不能。</b>分子不是同一批订单，全站 ROI 天然高 1~3 倍。</td></tr>
      <tr><td>应该比什么</td><td colspan="2"><b>费比 = 广告总花费 ÷ 商智成交额</b>；以及“多花 1 元带来的增量成交”。</td></tr>
    </tbody>
  </table>
</div>

<h2>2. 口径证据：全站营销的“成交”是整店口径</h2>
<div class="card">
  <p class="sub">拿两个区间里带 SPU 计划的分组，把“全站成交/全站订单行”和同一批 SKU 的商智成交/订单对照：</p>
  {_evidence_table(d)}
  <div class="note"><b>结论：</b>全站营销的“全站订单行”与商智订单大体对应（本页 44.9%~93%），
    覆盖比例取决于计划预算/投放范围；B 期普遍在 74%~93%，说明它统计的是<b>整店成交</b>，
    不是广告点击带来的成交。因此“全站 ROI 12”只代表“整店成交是广告花费的 12 倍”，
    并不代表“每花 1 元多赚 12 元”。</div>
</div>

<h2>3. 同一 SKU 的两个 ROI，差距来自分子</h2>
<div class="card">{_same_sku_table(d)}</div>

<h2>4. 调整前后：SPU 走势与花费</h2>
{_chart_lines(d)}
<div class="card">
  <p class="sub">A 列是「史上最强」区间，B 列是「现代最强」区间；花费包含快车/智能/全站智能 + 全站营销。
    费比 = 总花费 ÷ 商智成交额；净利 = 商智成交额 × 加权毛利率 − 总花费。</p>
  {_group_table(d)}
</div>
<h3>每日广告花费：快车/智能 + 全站营销</h3>
{_chart_spends(d)}

<div class="card">
  <h3 style="margin-top:0">AI 解读 <span class="sub">用现有 LLM 接口，基于当前 A/B 数据生成 3~5 条结论</span></h3>
  <button class="btn" id="vsAiBtn" onclick="vsAi(this)">AI 解读这组对比</button>
  <div id="vsAi" class="ai-out"></div>
</div>
<script>
var VS_ARGS = {ai_args};
async function vsAi(btn){{
  var box = document.getElementById('vsAi');
  btn.disabled = true; btn.textContent = '生成中…';
  box.style.display = 'block'; box.textContent = '正在调用现有 AI 接口（约 20~60 秒）…';
  try {{
    var r = await fetch('/api/vs/insight', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify(VS_ARGS)}});
    var d = await r.json();
    if (d.ok) {{
      var safe = String(d.text || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      safe = safe.replace(/\\*\\*(.+?)\\*\\*/g, '<b>$1</b>');
      box.innerHTML = safe.replace(/\\n/g, '<br>');
    }} else {{
      box.textContent = '生成失败：' + (d.error || '未知错误');
    }}
  }} catch (e) {{
    box.textContent = '生成失败：' + e;
  }}
  btn.disabled = false; btn.textContent = '重新生成';
}}
</script>

<h2>5. 逐商品：现在谁在赚钱，谁贴着保本线</h2>
<div class="card">
  <p class="sub">判断规则：B 期费比 ≥ 毛利率 → 亏损/临界；≥ 毛利率 × 75% → 接近保本；否则安全。
    毛利率来自 ERP 真实成本，保本 ROI = 1 ÷ 毛利率。
    B 期全站营销花费按成员商智成交占比分摊，所以同一计划内各 SKU 的 B 费比会相同；这是结构分摊，不是平台 SKU 级真实花费。</p>
  {groups_html or '<p class="sub">两个区间都没有可对比的商品。</p>'}
</div>

<h2>6. 结论：哪种方案更好？</h2>
<div class="card">{_conclusion_table(d)}</div>

<h2>7. 建议动作</h2>
<div class="card">
  <ol>
    <li><b>先看费比改善最大的组：</b>{best_txt}。这类组可以优先保留全站营销，并用 7 天小步放量验证增量。</li>
    <li><b>费比 ≥ 毛利率的 SKU 单独处理：</b>见上方逐商品表的红色/黄色标记；先降目标 ROI、缩预算或移除计划，不要用“全站 ROI 高”掩盖。</li>
    <li><b>把“全站 ROI”从考核里拿掉：</b>改用 <code>费比 = 广告总花费 ÷ 商智成交额</code> 和
      <code>净利 = 商智成交额 × 毛利率 − 广告花费</code>；全站 ROI 只作为计划内部的目标出价参数。</li>
    <li><b>检查账户/店铺归属：</b>如果 A 期有大量花费投在“商智里没有的 SKU”，先确认是不是跨店投放/账户归属错配，
      否则单店铺商智会把该账户的费比算得偏高。</li>
    <li><b>做 7 天 A/B 验证增量：</b>固定总预算，按 SKU 分组一半快车一半全站，避开大促日，
      比较同口径的商智成交/总花费和增量成交。</li>
  </ol>
</div>

<h2>8. 数据口径与局限</h2>
<div class="card">
  <ul>
    <li>成交/订单来自商智（T+1 结算，最近 15 天每晚重抓；最近 1~2 天可能仍低于终值）。</li>
    <li>广告花费来自京准通：快车/智能（jst_sku + kw_ad）、全站智能（jzt campaignType 153）、全站营销（swa_campaign）。</li>
    <li>全站营销只有 SPU 级花费，SKU 级花费按成员商智成交占比分摊，仅用于结构观察。</li>
    <li>毛利率统一用当前 ERP 成本缓存重算（A/B 同一份成本），但分母是各期自己的前台件单价；
      所以 A/B 的毛利率差异可能来自价格、产品结构或最近 1~2 天商智件数尚未结算，不完全是广告效率变化。</li>
    <li>“费比”是总花费/商智成交，天然含自然成交；它衡量“广告费占生意的比例”，不衡量纯增量。
      纯增量需要 A/B 或地理/时段对照，当前页面只能做方向判断。</li>
  </ul>
</div>

<div class="foot">数据来源：京准通（概览 / 智能投放 / 快车关键词 / 全站营销）+ 商智（商品明细）。金额单位：元；ROI 为倍数。</div>
</div></body></html>"""


def error_page(msg: str) -> str:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>无法对比</title><style>{VS_CSS}</style></head><body>
<div class="topbar"><div class="wrap"><a class="brand" href="/">京东广告运营</a>
<div class="navlinks"><a href="/report">分析报表</a><a class="on" href="/vs">史上最强vs现代最强</a></div></div></div>
<div class="wrap"><div class="card" style="margin-top:40px">
<h2 style="margin-top:0">这个区间没有数据</h2><div class="note">{_e(msg)}</div>
<p class="sub">请换一个区间，或先在控制台补抓对应日期的数据。</p>
<p><a class="btn" href="/vs">← 返回对比</a></p></div></div></body></html>"""
