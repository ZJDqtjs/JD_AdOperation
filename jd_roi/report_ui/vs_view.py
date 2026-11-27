# -*- coding: utf-8 -*-
"""「史上最强 vs 现代最强」页面渲染层（复用全站 Claude 设计系统 + ECharts + 4 页签）。"""
from __future__ import annotations

import datetime as dt
import json

from .. import settings
from ..report2 import _e, _money, _pct
from .scripts import REPORT_JS
from .theme import CSS as BASE_CSS, SPIKE_SVG, UI_CSS, _echarts_src, navtop

VS_TABS = [
    ("vs-ov", "① 核心结论与趋势"),
    ("vs-grp", "② 分组与花费明细"),
    ("vs-sku", "③ 逐商品明细"),
    ("vs-cal", "④ 口径对照与证据"),
]

VS_EXTRA_CSS = """
/* /vs 专属排版：全部基于 report_ui.theme 变量 */
.vs-verdict{border-radius:var(--r-lg);padding:18px 22px;margin:0 0 18px;border:1px solid var(--hair);
  border-left:4px solid var(--primary);background:var(--surface-card)}
.vs-verdict.good{background:var(--greenbg);border-color:#cfe3d4;border-left-color:var(--green)}
.vs-verdict.warn{background:var(--amberbg);border-color:#ebddb4;border-left-color:var(--amber)}
.vs-verdict.info{background:var(--surface-card);border-color:var(--hair);border-left-color:var(--primary)}
.vs-verdict .vtitle{font-size:21px;font-weight:400;font-family:var(--serif);color:var(--ink);
  line-height:1.4;letter-spacing:-.3px}
.vs-verdict .vsub{font-size:13px;color:var(--body);margin-top:6px;line-height:1.65}
.vs-gcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;margin:0 0 18px}
.vs-gcard{background:var(--canvas);border:1px solid var(--hair);border-left:4px solid var(--hair);
  border-radius:var(--r-lg);padding:18px 20px}
.vs-gcard.good{border-left-color:var(--green)}
.vs-gcard.warn{border-left-color:var(--amber)}
.vs-gcard.bad{border-left-color:var(--red)}
.vs-gcard.info{border-left-color:var(--primary)}
.vs-ghead{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:16px;
  font-family:var(--serif);color:var(--ink)}
.vs-gverdict{font-size:13px;color:var(--body);margin:8px 0 12px}
.vs-gmetrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 12px}
.vs-gmetrics div{background:var(--soft);border:1px solid var(--hair2);border-radius:var(--r-sm);padding:9px 11px}
.vs-gmetrics span{display:block;font-size:11px;color:var(--muted)}
.vs-gmetrics b{display:block;font-size:13px;color:var(--ink);font-family:var(--mono);margin:2px 0}
.vs-gmetrics em{font-style:normal;font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
.vs-gwhy{font-size:12.2px;color:var(--muted);line-height:1.7;border-top:1px dashed var(--hair);padding-top:9px}
.vs-simple-list{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:8px}
.vs-simple-col h3{margin:0 0 10px;font-size:14.5px;color:var(--strong)}
.vs-simple-row{display:flex;align-items:center;gap:10px;padding:9px 0;
  border-bottom:1px solid var(--hair2);font-size:13px}
.vs-simple-row:last-child{border-bottom:0}
.vs-simple-row .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink);font-weight:500}
.vs-simple-row .mt{color:var(--muted);font-size:11.5px;white-space:nowrap;font-family:var(--mono)}
.vs-steps{counter-reset:s;margin:0;padding:0;list-style:none}
.vs-steps li{position:relative;padding:10px 0 10px 36px;border-bottom:1px solid var(--hair2);font-size:13.5px;line-height:1.7}
.vs-steps li:last-child{border-bottom:0}
.vs-steps li:before{counter-increment:s;content:counter(s);position:absolute;left:0;top:11px;
  width:23px;height:23px;border-radius:50%;background:var(--bluebg);color:var(--primary-a);font-size:12px;
  font-weight:700;display:flex;align-items:center;justify-content:center;font-family:var(--mono)}
.vs-ai-out{display:none;margin-top:14px;background:var(--soft);border:1px solid var(--hair);
  border-radius:var(--r-md);padding:14px 16px;font-size:13.2px;line-height:1.85;color:var(--ink)}
.vs-glossary dl{margin:10px 0 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px 20px}
.vs-glossary dt{font-weight:600;color:var(--ink);font-size:13px}
.vs-glossary dd{margin:2px 0 0;color:var(--muted);font-size:12.5px;line-height:1.65}
.vs-sku-name{display:block;font-weight:400;color:var(--muted);font-size:11.5px;
  max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:860px){.vs-simple-list,.vs-gmetrics{grid-template-columns:1fr}}
"""


def _f(v, d=0.0) -> float:
    try:
        return d if v is None else float(v)
    except (TypeError, ValueError):
        return d


def _chg(cur, pre) -> float:
    return (cur / pre - 1) if pre else 0.0


def _delta_pct(cur, pre) -> tuple[str, str]:
    if not pre:
        return ("—", "")
    v = (cur / pre - 1) * 100
    cls = "up" if v > 0 else ("down" if v < 0 else "")
    return (f"{v:+.1f}%", cls)


def _pctv(v, dec=2) -> str:
    return _pct(_f(v) * 100, dec)


def _headline(d):
    p = d["totals"]["plan"]
    if _f(p["a"].get("gmv")) or _f(p["b"].get("gmv")):
        return p["a"], p["b"], "有 SPU 计划的组"
    return d["totals"]["all"]["a"], d["totals"]["all"]["b"], "全部商品"


def _control_group(d):
    cands = [g for g in d["groups"]
             if not g.get("isPlan") and not g.get("orphan") and _f(g["a"].get("gmv")) > 0]
    cands.sort(key=lambda g: -_f(g["a"].get("gmv")))
    return cands[0] if cands else None


def _chart_groups(d):
    gs = [g for g in d["groups"] if g.get("isPlan") and not g.get("orphan")]
    if not gs:
        gs = [g for g in d["groups"] if not g.get("orphan")][:3]
    return gs[:4]


def _chart_name(g) -> str:
    if "100004361356" in g["key"]:
        return "梓云茶 · 玉米系列"
    if "100006799721" in g["key"]:
        return "雨润禾 · 蔬菜系列"
    return g["label"][:16]


def _freshness_notice(d) -> str:
    b_end_str = d["meta"]["b"]["end"]
    try:
        gap = (settings.today() - dt.date.fromisoformat(b_end_str)).days
    except ValueError:
        gap = 99
    if gap <= 3:
        return (f'<div class="warnbox" style="margin-top:0"><b>数据新鲜度提醒</b>：B 期截止至 '
                f'<code>{_e(b_end_str)}</code>（距今 {max(gap, 0)} 天），最近 1~2 天商智成交仍在 T+2/T+3 回补，'
                f'B 期成交与净利可能偏低，建议回补完成后复算。</div>')
    return ""


def _verdict_banner(d) -> str:
    a, b, scope = _headline(d)
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    sg = _chg(_f(b["spend"]) / db, _f(a["spend"]) / da)
    gg = _chg(_f(b["gmv"]) / db, _f(a["gmv"]) / da)
    pg = _chg(_f(b["profit"]) / db, _f(a["profit"]) / da)
    if pg >= 0 and gg >= -0.05:
        cls, sub = "good", "广告费控制得当，成交基本保住，净利提升。"
    elif pg < 0 and gg < 0:
        cls, sub = "warn", "广告费虽有变化，但成交与毛利回落更明显；建议结合下方各商品组拆开看。"
    else:
        cls, sub = "info", "广告费与成交各有波动，下面按商品组拆开看。"
    title = (f"本次对比（{scope}）：广告费{'省' if sg < 0 else '多'}了 {abs(sg)*100:.0f}%，"
             f"成交{'少' if gg < 0 else '多'}了 {abs(gg)*100:.0f}%，"
             f"净利{'少' if pg < 0 else '多'}了 {abs(pg)*100:.0f}%")
    tags = []
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        gp = _chg(_f(g["b"].get("profit")) / db, _f(g["a"].get("profit")) / da)
        gq = _chg(_f(g["b"].get("gmv")) / db, _f(g["a"].get("gmv")) / da)
        icon = "✅" if gp >= 0 and gq >= -0.05 else ("⚠️" if gp < 0 and gq < -0.1 else "➖")
        tags.append(f"{icon} {_chart_name(g)}")
    sub = "；".join(tags) + "。" + sub if tags else sub
    return f'<div class="vs-verdict {cls}"><div class="vtitle">{_e(title)}</div><div class="vsub">{_e(sub)}</div></div>'


def _big_kpis(d) -> str:
    a, b, _ = _headline(d)
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]

    def card(label, av, bv, fmt, lower_better):
        chg = _chg(bv, av)
        if chg == 0:
            cls = "flat"
        elif lower_better:
            cls = "up" if chg < 0 else "down"
        else:
            cls = "up" if chg > 0 else "down"
        word = "少" if chg < 0 else ("多" if chg > 0 else "持平")
        sub = f'<span class="delta {cls}">比 A {word} {fmt(abs(bv - av))}（{chg*100:+.0f}%）</span> · A 期 {fmt(av)}'
        return f'<div class="kpi hero"><div class="lbl">{label}</div><div class="val">{fmt(bv)}</div><div class="sub">{sub}</div></div>'

    df = (_f(b["fee"]) - _f(a["fee"])) * 100
    fcls = "up" if df < 0 else ("down" if df > 0 else "flat")
    fee_card = (f'<div class="kpi hero"><div class="lbl">整体费比（花费 ÷ 商智成交）</div>'
                f'<div class="val">{_pctv(b["fee"], 2)}</div>'
                f'<div class="sub"><span class="delta {fcls}">{df:+.2f}pp</span> · A 期 {_pctv(a["fee"], 2)}</div></div>')
    return ('<div class="grid g4" style="margin-bottom:16px">'
            + card("广告费 / 天（B 期）", _f(a["spend"])/da, _f(b["spend"])/db, lambda v: "¥"+_money(v), True)
            + card("商智成交 / 天（B 期）", _f(a["gmv"])/da, _f(b["gmv"])/db, lambda v: "¥"+_money(v), False)
            + fee_card
            + card("估算净利 / 天（B 期）", _f(a["profit"])/da, _f(b["profit"])/db, lambda v: "¥"+_money(v), False)
            + '</div>')


def _group_cards(d) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    control = _control_group(d)
    cg = _chg(_f(control["b"].get("gmv"))/db, _f(control["a"].get("gmv"))/da) if control else 0
    cards = []
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        a, b = g["a"], g["b"]
        sa, sb = _f(a.get("spend"))/da, _f(b.get("spend"))/db
        ga, gb = _f(a.get("gmv"))/da, _f(b.get("gmv"))/db
        pa, pb = _f(a.get("profit"))/da, _f(b.get("profit"))/db
        sg, gg, pg = _chg(sb, sa), _chg(gb, ga), _chg(pb, pa)
        ma, mb = _f(a.get("margin")), _f(b.get("margin"))
        if pg >= 0 and gg >= -0.05:
            cls, pill, txt = "good", "切对了", "广告费省了，成交基本保住，净利变好。"
        elif pg < 0 and gg < -0.1:
            cls, pill, txt = "warn", "需关注", "广告费省了，但成交和毛利掉得更多，净利变差。"
        else:
            cls, pill, txt = "info", "观察中", "有省有花，建议继续观察或做小步 A/B。"
        badge_cls = {"good": "b-ok", "warn": "b-warn", "info": "b-info"}[cls]
        why = (f"对照「其他快车」成交 {cg*100:+.1f}%，本组 {gg*100:+.1f}%；"
               f"费比 {_pctv(a.get('fee'), 2)} → {_pctv(b.get('fee'), 2)}；"
               f"毛利率 {ma*100:.1f}% → {mb*100:.1f}%。")
        cards.append(f"""<div class="vs-gcard {cls}">
<div class="vs-ghead"><b>{_e(_chart_name(g))}</b><span class="badge {badge_cls}">{pill}</span></div>
<div class="vs-gverdict">{_e(txt)}</div>
<div class="vs-gmetrics">
<div><span>广告费/天</span><b>¥{_money(sa)} → ¥{_money(sb)}</b><em class="{'up' if sg<0 else 'down'}">{sg*100:+.0f}%</em></div>
<div><span>成交/天</span><b>¥{_money(ga)} → ¥{_money(gb)}</b><em class="{'up' if gg>=0 else 'down'}">{gg*100:+.0f}%</em></div>
<div><span>净利/天</span><b>¥{_money(pa)} → ¥{_money(pb)}</b><em class="{'up' if pg>=0 else 'down'}">{pg*100:+.0f}%</em></div>
</div>
<div class="vs-gwhy">{_e(why)}</div></div>""")
    return f'<div class="vs-gcards">{"".join(cards)}</div>' if cards else ""


def _build_vs_charts(d) -> tuple[list, str, str]:
    b_start = d["meta"]["b"]["start"]
    charts, gmv_boxes, spend_boxes = [], [], []
    for i, g in enumerate(_chart_groups(d)):
        series = (d.get("series") or {}).get(g["key"]) or []
        if not series:
            continue
        name = _chart_name(g)
        days = [p["d"][5:] for p in series]
        gmvs = [round(_f(p.get("gmv")), 1) for p in series]
        ads = [round(_f(p.get("ad")), 1) for p in series]
        swas = [round(_f(p.get("swa")), 1) for p in series]
        fees = [round((_f(p.get("ad")) + _f(p.get("swa"))) / _f(p.get("gmv")) * 100, 2)
                if _f(p.get("gmv")) > 0 else 0 for p in series]
        b_idx = next((idx for idx, p in enumerate(series) if p["d"] >= b_start), 0)
        b_lbl = days[b_idx] if days else ""
        mark = {"silent": True, "symbol": "none",
                "lineStyle": {"color": "#d4a017", "type": "dashed", "width": 1.5},
                "label": {"formatter": "B 期开始", "color": "#8a6a1e", "fontSize": 11},
                "data": [{"xAxis": b_lbl}]} if b_lbl else None

        cid_g = f"vsGmv{i}"
        charts.append({"id": cid_g, "option": {
            "tooltip": {"trigger": "axis"},
            "legend": {"data": ["商智成交额", "当日费比%"], "top": 0},
            "grid": {"left": 56, "right": 52, "top": 36, "bottom": 28},
            "xAxis": {"type": "category", "data": days, "axisLabel": {"fontSize": 11}},
            "yAxis": [
                {"type": "value", "name": "成交额(元)", "axisLabel": {"fontSize": 11}},
                {"type": "value", "name": "费比%", "axisLabel": {"fontSize": 11, "formatter": "{value}%"}},
            ],
            "series": [
                {"name": "商智成交额", "type": "line", "smooth": True, "data": gmvs,
                 "itemStyle": {"color": "#cc785c"}, "lineStyle": {"width": 2.4},
                 "areaStyle": {"color": "rgba(204,120,92,0.12)"},
                 **({"markLine": mark} if mark else {})},
                {"name": "当日费比%", "type": "line", "yAxisIndex": 1, "smooth": True, "data": fees,
                 "itemStyle": {"color": "#5db8a6"}, "lineStyle": {"width": 1.8, "type": "dashed"}},
            ],
        }})
        gmv_boxes.append(
            f'<div class="card tight"><h3 style="margin-top:0">{_e(name)} '
            f'<span class="mini">商智成交额与当日费比</span></h3>'
            f'<div id="{cid_g}" class="chart" style="height:270px"></div></div>')

        cid_s = f"vsSpend{i}"
        charts.append({"id": cid_s, "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"data": ["快车/智能", "全站营销"], "top": 0},
            "grid": {"left": 56, "right": 24, "top": 36, "bottom": 28},
            "xAxis": {"type": "category", "data": days, "axisLabel": {"fontSize": 11}},
            "yAxis": {"type": "value", "name": "花费(元)", "axisLabel": {"fontSize": 11}},
            "series": [
                {"name": "快车/智能", "type": "bar", "stack": "sp", "barMaxWidth": 22, "data": ads,
                 "itemStyle": {"color": "#5db8a6"}},
                {"name": "全站营销", "type": "bar", "stack": "sp", "barMaxWidth": 22, "data": swas,
                 "itemStyle": {"color": "#cc785c"},
                 **({"markLine": mark} if mark else {})},
            ],
        }})
        spend_boxes.append(
            f'<div class="card tight"><h3 style="margin-top:0">{_e(name)} '
            f'<span class="mini">每日广告花费构成</span></h3>'
            f'<div id="{cid_s}" class="chart" style="height:260px"></div></div>')

    gmv_html = f'<div class="grid g2">{"".join(gmv_boxes)}</div>' if gmv_boxes else '<p class="hint">暂无趋势数据。</p>'
    spend_html = f'<div class="grid g2">{"".join(spend_boxes)}</div>' if spend_boxes else ""
    return charts, gmv_html, spend_html


def _simple_skus(d) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    rows = []
    for g in d["groups"]:
        if g.get("orphan") or not g.get("isPlan"):
            continue
        for sid, sa, sb in g["sku"]:
            s = sb or sa
            if not s:
                continue
            ga = _f((sa or {}).get("gmv")) / da
            gb = _f((sb or {}).get("gmv")) / db
            if gb < 500:
                continue
            rows.append({
                "name": (s.get("name") or sid)[:28], "sid": sid,
                "fee": _f(s.get("fee")), "mg": _f(s.get("margin")),
                "gmv": gb, "spend": _f(s.get("spend")) / db,
                "gg": _chg(gb, ga) if ga else 0,
            })
    bad = [r for r in rows if r["mg"] > 0 and (r["fee"] >= r["mg"] * 0.75 or r["gg"] < -0.2)]
    bad.sort(key=lambda r: (-(r["fee"] - r["mg"]), r["gg"]))
    good = [r for r in rows if r["mg"] > 0 and r["fee"] < r["mg"] * 0.75 and r["gg"] >= -0.1]
    good.sort(key=lambda r: -r["gmv"])

    def line(r):
        if r["mg"] > 0 and r["fee"] >= r["mg"]:
            tag, txt = "b-bad", "费比高于毛利"
        elif r["mg"] > 0 and r["fee"] >= r["mg"] * 0.75:
            tag, txt = "b-warn", "接近保本"
        elif r["gg"] < -0.2:
            tag, txt = "b-warn", f"成交 {r['gg']*100:+.0f}%"
        else:
            tag, txt = "b-ok", "安全"
        return (f'<div class="vs-simple-row"><div class="nm" title="{_e(r["name"])}">{_e(r["name"])}</div>'
                f'<div class="mt">花 ¥{_money(r["spend"])} · 卖 ¥{_money(r["gmv"])} · 费比 {_pctv(r["fee"], 1)}</div>'
                f'<span class="badge {tag}">{txt}</span></div>')

    left = "".join(line(r) for r in bad[:6]) or '<p class="hint">没有明显风险 SKU。</p>'
    right = "".join(line(r) for r in good[:6]) or '<p class="hint">暂无明显表现好的 SKU。</p>'
    return (f'<div class="vs-simple-list">'
            f'<div class="vs-simple-col"><h3>⚠️ 需要关注（费比高或成交下滑 &gt;20%）</h3>{left}</div>'
            f'<div class="vs-simple-col"><h3>✅ 表现较好（费比安全）</h3>{right}</div></div>')


def _next_steps(d) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    control = _control_group(d)
    cg = _chg(_f(control["b"].get("gmv"))/db, _f(control["a"].get("gmv"))/da) if control else 0
    items = []
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        name = _chart_name(g)
        gp = _chg(_f(g["b"].get("profit"))/db, _f(g["a"].get("profit"))/da)
        gg = _chg(_f(g["b"].get("gmv"))/db, _f(g["a"].get("gmv"))/da)
        if gp >= 0 and gg >= -0.05:
            items.append(
                f"<li><b>{_e(name)}（净利 {gp*100:+.0f}%，成交 {gg*100:+.0f}%）：</b>"
                f"保留当前全站策略，先小步加 10%~20% 预算跑 7 天 A/B 验证增量，避免一步猛拉预算。</li>")
        elif gp < 0 and gg < -0.1:
            items.append(
                f"<li><b>{_e(name)}（净利 {gp*100:+.0f}%，成交 {gg*100:+.0f}%）：</b>"
                f"先排查 B 期是否有改价、活动结束或断货；若无外部因素，建议挑选 2~3 个核心 SKU 恢复快车精准词，做 7 天对照。</li>")
        else:
            items.append(
                f"<li><b>{_e(name)}（净利 {gp*100:+.0f}%，成交 {gg*100:+.0f}%）：</b>"
                f"表现分化（对照其他快车 {cg*100:+.1f}%），建议切到「③ 逐商品明细」把费比高于毛利的 SKU 单独控预算或踢出计划。</li>")
    if control:
        items.append(
            f"<li><b>{_e(_chart_name(control))}（非全站对照组）：</b>"
            f"与全站调整分开评估，单独排查拖累全账号费比的高花费低效计划。</li>")
    if db < 7:
        items.append(
            f"<li><b>样本期提醒：</b>当前 B 期仅 {db} 天，统计波动较大；建议观察满 7 天且待商智 T+2/T+3 结算定稿后再做放量决策。</li>")
    if not items:
        items.append("<li>当前区间暂无带 SPU 全站计划的分组，可在「② 分组与花费明细」核对各组费比变化。</li>")
    return f'<ol class="vs-steps">{"".join(items)}</ol>'


def _group_table(d) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    th = ("<tr><th class='l'>分组</th>"
          "<th data-num='1'>A 花费/天</th><th data-num='1'>B 花费/天</th>"
          "<th data-num='1'>B 快车/天</th><th data-num='1'>B 全站/天</th><th data-num='1'>花费Δ</th>"
          "<th data-num='1'>A 商智/天</th><th data-num='1'>B 商智/天</th><th data-num='1'>商智Δ</th>"
          "<th data-num='1'>A 费比</th><th data-num='1'>B 费比</th><th data-num='1'>费比Δ</th>"
          "<th data-num='1'>毛利率</th><th data-num='1'>A 净利/天</th><th data-num='1'>B 净利/天</th></tr>")
    trs = []
    for g in d["groups"]:
        a, b = g["a"], g["b"]
        sa, sb = _f(a.get("spend"))/da, _f(b.get("spend"))/db
        if g.get("orphan"):
            trs.append(
                f"<tr><td class='l'><b>{_e(g['label'][:34])}</b><div class='mini'>{_e(g['sub'])}</div></td>"
                f"<td class='mono' data-v='{sa:.2f}'>¥{_money(sa)}</td>"
                f"<td class='mono' data-v='{sb:.2f}'>¥{_money(sb)}</td>"
                f"<td class='mono' data-v='{_f(b.get('adCost'))/db:.2f}'>¥{_money(_f(b.get('adCost'))/db)}</td>"
                f"<td class='mono' data-v='{_f(b.get('swaCost'))/db:.2f}'>¥{_money(_f(b.get('swaCost'))/db)}</td>"
                f"<td class='mono' data-v='0'>—</td><td class='mono' data-v='0'>—</td>"
                f"<td class='mono' data-v='0'>—</td><td class='mono' data-v='0'>—</td>"
                f"<td class='mono' data-v='0'>—</td><td class='mono' data-v='0'>—</td>"
                f"<td class='mono' data-v='0'>—</td><td class='mono' data-v='0'>—</td>"
                f"<td class='l' colspan='2'><span class='badge b-warn'>纯广告，商智无成交</span></td></tr>")
            continue
        ga, gb = _f(a.get("gmv"))/da, _f(b.get("gmv"))/db
        pa, pb = _f(a.get("profit"))/da, _f(b.get("profit"))/db
        ds, _ = _delta_pct(sb, sa)
        dg, _ = _delta_pct(gb, ga)
        fa, fb = _f(a.get("fee")), _f(b.get("fee"))
        df = (fb - fa) * 100
        mg = _f(b.get("margin") or a.get("margin"))
        trs.append(
            f"<tr><td class='l'><b>{_e(g['label'][:34])}</b><div class='mini'>{_e(g['sub'])}</div></td>"
            f"<td class='mono' data-v='{sa:.2f}'>¥{_money(sa)}</td>"
            f"<td class='mono' data-v='{sb:.2f}'>¥{_money(sb)}</td>"
            f"<td class='mono' data-v='{_f(b.get('adCost'))/db:.2f}'>¥{_money(_f(b.get('adCost'))/db)}</td>"
            f"<td class='mono' data-v='{_f(b.get('swaCost'))/db:.2f}'>¥{_money(_f(b.get('swaCost'))/db)}</td>"
            f"<td class='mono' data-v='{_chg(sb, sa):.4f}'>{ds}</td>"
            f"<td class='mono' data-v='{ga:.2f}'>¥{_money(ga)}</td>"
            f"<td class='mono' data-v='{gb:.2f}'>¥{_money(gb)}</td>"
            f"<td class='mono' data-v='{_chg(gb, ga):.4f}'>{dg}</td>"
            f"<td class='mono' data-v='{fa:.4f}'>{_pctv(fa, 2)}</td>"
            f"<td class='mono' data-v='{fb:.4f}'>{_pctv(fb, 2)}</td>"
            f"<td class='mono {'up' if df < 0 else ('down' if df > 0 else '')}' data-v='{df:.4f}'>{df:+.2f}pp</td>"
            f"<td class='mono' data-v='{mg:.4f}'>{_pctv(mg, 1)}</td>"
            f"<td class='mono' data-v='{pa:.2f}'>¥{_money(pa)}</td>"
            f"<td class='mono {'up' if pb >= pa else 'down'}' data-v='{pb:.2f}'>¥{_money(pb)}</td></tr>")
    return f"<div class='tblwrap'><table><thead>{th}</thead><tbody>{''.join(trs)}</tbody></table></div>"


def _channel_table(d) -> str:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    rows = [(g["label"][:28], g["a"], g["b"]) for g in d["groups"] if g.get("isPlan") and not g.get("orphan")]
    t = d["totals"]
    rows.append(("有 SPU 计划的组合计", t["plan"]["a"], t["plan"]["b"]))
    rows.append(("全账号合计（含其他快车商品）", t["all"]["a"], t["all"]["b"]))
    trs = []
    for label, a, b in rows:
        ac_a, sw_a, sp_a = _f(a.get("adCost"))/da, _f(a.get("swaCost"))/da, _f(a.get("spend"))/da
        ac_b, sw_b, sp_b = _f(b.get("adCost"))/db, _f(b.get("swaCost"))/db, _f(b.get("spend"))/db
        trs.append(
            f"<tr><td class='l'><b>{_e(label)}</b></td>"
            f"<td class='mono' data-v='{ac_a:.2f}'>¥{_money(ac_a)}</td>"
            f"<td class='mono' data-v='{sw_a:.2f}'>¥{_money(sw_a)}</td>"
            f"<td class='mono' data-v='{sp_a:.2f}'><b>¥{_money(sp_a)}</b></td>"
            f"<td class='mono' data-v='{ac_b:.2f}'>¥{_money(ac_b)}</td>"
            f"<td class='mono' data-v='{sw_b:.2f}'>¥{_money(sw_b)}</td>"
            f"<td class='mono' data-v='{sp_b:.2f}'><b>¥{_money(sp_b)}</b></td></tr>")
    return ("<div class='tblwrap short'><table><thead><tr><th class='l'>分组</th>"
            "<th data-num='1'>A 快车/天</th><th data-num='1'>A 全站/天</th><th data-num='1'>A 合计/天</th>"
            "<th data-num='1'>B 快车/天</th><th data-num='1'>B 全站/天</th><th data-num='1'>B 合计/天</th></tr>"
            f"</thead><tbody>{''.join(trs)}</tbody></table></div>")


def _all_sku_table(d) -> tuple[str, int]:
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    valid_groups = [g for g in d["groups"] if not g.get("orphan")]
    tier_counts = {"bad": 0, "warn": 0, "ok": 0}
    trs = []
    idx = 0
    for g in valid_groups:
        gkey, gname = g["key"], _chart_name(g)
        for sid, sa, sb in g["sku"]:
            idx += 1
            s = sb or sa or {}
            fee, mg = _f(s.get("fee")), _f(s.get("margin"))
            if fee and mg:
                if fee >= mg:
                    verdict, cls = "亏损/临界", "bad"
                elif fee >= mg * 0.75:
                    verdict, cls = "接近保本", "warn"
                else:
                    verdict, cls = "安全", "ok"
            else:
                verdict, cls = "无成交", "warn"
            tier_counts[cls] = tier_counts.get(cls, 0) + 1
            ga = _f((sa or {}).get("gmv")) / da
            gb = _f((sb or {}).get("gmv")) / db
            sp_a = _f((sa or {}).get("spend")) / da
            sp_b = _f((sb or {}).get("spend")) / db
            dg, dg_cls = _delta_pct(gb, ga) if (ga or gb) else ("—", "")
            fa = _f((sa or {}).get("fee"))
            trs.append(
                f"<tr data-tier='{cls}' data-acct='{_e(gkey)}' "
                f"data-sum-cost='{sp_b:.2f}' data-sum-amt='{gb:.2f}'>"
                f"<td class='mono'>{idx}</td>"
                f"<td class='l'><b>{_e(sid)}</b><span class='vs-sku-name' title='{_e(s.get('name') or '')}'>"
                f"{_e((s.get('name') or '')[:56])}</span></td>"
                f"<td class='l'><span class='pill'>{_e(gname)}</span></td>"
                f"<td class='mono' data-v='{mg:.4f}'>{_pctv(mg, 1) if mg else '—'}</td>"
                f"<td class='mono' data-v='{_f(s.get('breakeven')):.2f}'>{_f(s.get('breakeven')):.2f}</td>"
                f"<td class='mono' data-v='{sp_a:.2f}'>¥{_money(sp_a)}</td>"
                f"<td class='mono' data-v='{sp_b:.2f}'>¥{_money(sp_b)}</td>"
                f"<td class='mono' data-v='{_f((sb or {}).get('adCost'))/db:.2f}'>¥{_money(_f((sb or {}).get('adCost'))/db)}</td>"
                f"<td class='mono' data-v='{_f((sb or {}).get('swaCost'))/db:.2f}'>¥{_money(_f((sb or {}).get('swaCost'))/db)}</td>"
                f"<td class='mono' data-v='{ga:.2f}'>¥{_money(ga)}</td>"
                f"<td class='mono' data-v='{gb:.2f}'>¥{_money(gb)}</td>"
                f"<td class='mono {dg_cls}' data-v='{_chg(gb, ga):.4f}'>{dg}</td>"
                f"<td class='mono' data-v='{fa:.4f}'>{_pctv(fa, 2) if sa else '—'}</td>"
                f"<td class='mono' data-v='{fee:.4f}'>{_pctv(fee, 2) if fee else '—'}</td>"
                f"<td class='l'><span class='badge b-{cls}'>{verdict}</span></td></tr>")
    tchips = "".join(
        f'<div class="chip{" on" if k == "all" else ""}" data-f="{k}" onclick="applyTier(this)">{v}</div>'
        for k, v in [
            ("all", f"全部 {idx}"),
            ("bad", f"亏损/临界 {tier_counts['bad']}"),
            ("warn", f"接近保本/无成交 {tier_counts['warn']}"),
            ("ok", f"安全 {tier_counts['ok']}"),
        ])
    gchips = "".join(
        f'<div class="chip{" on" if i == 0 else ""}" data-a="{_e(k)}" onclick="applyAcct(this)">{_e(v)}</div>'
        for i, (k, v) in enumerate([("all", "全部分组")] + [(g["key"], _chart_name(g)) for g in valid_groups]))
    th = ("<tr><th class='noSort'>#</th><th class='l noSort'>商品 / SKU</th><th class='l'>分组</th>"
          "<th data-num='1'>毛利率</th><th data-num='1'>保本ROI</th>"
          "<th data-num='1'>A 花费/天</th><th data-num='1'>B 花费/天</th>"
          "<th data-num='1'>B 快车/天</th><th data-num='1'>B 全站/天</th>"
          "<th data-num='1'>A 商智/天</th><th data-num='1'>B 商智/天</th><th data-num='1'>GMVΔ</th>"
          "<th data-num='1'>A 费比</th><th data-num='1'>B 费比</th><th class='l'>判断</th></tr>")
    empty = '<tr><td colspan="15" class="l">两个区间都没有可对比的商品。</td></tr>'
    html = f"""
  <div class="toolbar">{tchips}<span class="toolbar-sep"></span>{gchips}
    <span class="toolbar-sep"></span>
    <input type="text" class="srch" placeholder="🔍 搜商品名 / SKU / 分组…（按 / 快速聚焦）" oninput="applySearch(this)">
    <span class="mini srchcount"></span>
    <button type="button" class="btn ghost mini btn-reset-filter" style="display:none" onclick="resetCardFilter(this)">重置筛选</button>
    <button type="button" class="btn sec mini" onclick="exportTableCsv(this,'vs-skus')">导出 CSV</button>
  </div>
  <div class="filter-sum"></div>
  <div class="tblwrap"><table class="tbl-sticky"><thead>{th}</thead><tbody>{''.join(trs) or empty}</tbody></table></div>"""
    return html, idx


def _evidence_table(d) -> str:
    rows = [g for g in d["groups"] if g.get("isPlan") and not g.get("orphan")]
    if not rows:
        return '<p class="hint">两个区间都没有带 SPU 计划的分组，无法做“全站成交 vs 商智成交”对照。</p>'
    trs = []
    for g in rows:
        for tag, side in (("A 史上最强", g["a"]), ("B 现代最强", g["b"])):
            swa_amt, gmv = _f(side.get("swaAmt")), _f(side.get("gmv"))
            swa_ord, ordn = _f(side.get("swaOrd")), _f(side.get("ord"))
            if not swa_amt and not swa_ord:
                continue
            amt_ratio = swa_amt / gmv if gmv else 0
            ord_ratio = swa_ord / ordn if ordn else 0
            trs.append(
                f"<tr><td class='l'><b>{_e(g['label'][:28])}</b><div class='mini'>{tag}</div></td>"
                f"<td class='mono' data-v='{swa_amt:.2f}'>¥{_money(swa_amt)}</td>"
                f"<td class='mono' data-v='{gmv:.2f}'>¥{_money(gmv)}</td>"
                f"<td class='mono' data-v='{amt_ratio:.4f}'><b>{_pctv(amt_ratio, 1) if gmv else '—'}</b></td>"
                f"<td class='mono' data-v='{swa_ord:.0f}'>{swa_ord:.0f}</td>"
                f"<td class='mono' data-v='{ordn:.0f}'>{ordn:.0f}</td>"
                f"<td class='mono' data-v='{ord_ratio:.4f}'><b>{_pctv(ord_ratio, 1) if ordn else '—'}</b></td></tr>")
    if not trs:
        return '<p class="hint">两个区间都没有全站营销成交。</p>'
    return ("<div class='tblwrap short'><table><thead><tr><th class='l'>计划 / 区间</th>"
            "<th data-num='1'>全站成交</th><th data-num='1'>商智成交（同 SKU）</th><th data-num='1'>成交占比</th>"
            "<th data-num='1'>全站订单行</th><th data-num='1'>商智订单</th><th data-num='1'>订单占比</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table></div>")


def _same_sku_table(d) -> str:
    best = None
    for g in d["groups"]:
        if not g.get("isPlan") or g.get("orphan"):
            continue
        for sid, sa, sb in g["sku"]:
            score = max(_f((sa or {}).get("gmv")), _f((sb or {}).get("gmv")))
            if best is None or score > best[0]:
                best = (score, sid, sa, sb, g)
    if not best:
        return '<p class="hint">暂无可供双口径对比的 SKU。</p>'
    _, sid, sa, sb, _ = best
    name = ((sb or sa) or {}).get("name") or sid
    rows = []
    if sa:
        c, amt, gmv = _f(sa.get("adCost")), _f(sa.get("adAmt")), _f(sa.get("gmv"))
        if c:
            rows.append(("A 期快车口径", c, "点击归因成交", amt, amt / c))
            rows.append(("A 期同一花费按全站成交", c, "商智全店成交", gmv, gmv / c))
    if sb:
        c, amt = _f(sb.get("swaCost")), _f(sb.get("swaAmt"))
        if c:
            rows.append(("B 期全站营销", c, "全站成交", amt, amt / c))
    if not rows:
        return '<p class="hint">暂无可供双口径对比的 SKU。</p>'
    trs = "".join(
        f"<tr><td class='l'>{_e(label)}</td><td class='mono'>¥{_money(c)}</td>"
        f"<td class='l'>{_e(kind)}</td><td class='mono'>¥{_money(amount)}</td>"
        f"<td class='mono'><b>{roi:.2f}</b></td></tr>"
        for label, c, kind, amount, roi in rows)
    return f"""<p class="hint">以「{_e(name[:34])}」（SKU <code>{_e(sid)}</code>）为例：</p>
<div class="tblwrap short"><table><thead><tr><th class="l noSort">算法</th><th class="noSort">花费</th>
<th class="l noSort">分子用什么成交</th><th class="noSort">分子金额</th><th class="noSort">ROI</th></tr></thead>
<tbody>{trs}</tbody></table></div>
<div class="note">快车 ROI 只把点击归因成交算进来；同一花费如果按商智全店成交算，ROI 会高很多；
全站营销的 ROI 正是后者的口径。所以两个 ROI 的差距主要是<b>分子</b>，不是投放效率。</div>"""


def _conclusion_table(d) -> str:
    a, b, scope = _headline(d)
    da, db = d["meta"]["a"]["days"], d["meta"]["b"]["days"]
    dg, _ = _delta_pct(b["gmv"] / db, a["gmv"] / da)
    ds, _ = _delta_pct(b["spend"] / db, a["spend"] / da)
    net_a, net_b = a["profit"] / da, b["profit"] / db
    dn, _ = _delta_pct(net_b, net_a)
    return f"""<div class="tblwrap short"><table>
<thead><tr><th class="l noSort" style="width:20%">判断维度</th><th class="l noSort">结论</th></tr></thead>
<tbody>
<tr><td class="l"><b>平台 ROI</b></td><td class="l"><b class="down">不可比。</b>全站营销的 ROI 含整店成交；快车 ROI 只含点击归因。谁高谁低不能说明方案好坏。</td></tr>
<tr><td class="l"><b>同一把尺（费比）</b></td><td class="l"><b class="up">{_pctv(a['fee'],2)} → {_pctv(b['fee'],2)}</b>（{(b['fee']-a['fee'])*100:+.2f}pp）。
  花费 {ds}，商智成交 {dg}；这是“每 100 元成交里广告费占多少”的真实变化。</td></tr>
<tr><td class="l"><b>商智成交</b></td><td class="l">¥{_money(a['gmv']/da)}/天 → ¥{_money(b['gmv']/db)}/天（{dg}）。</td></tr>
<tr><td class="l"><b>利润</b></td><td class="l">净利/天 ¥{_money(net_a)} → ¥{_money(net_b)}（{dn}），按统一 ERP 成本重算。
  净利同时受毛利率、前台价和产品结构影响；只看广告效率请用上面的费比。</td></tr>
<tr><td class="l"><b>阶段结论</b></td><td class="l">{_e(scope)}：全站营销更适合“低成本收割已有流量、替代高价快车”；
  快车更适合“打新客、卡关键词、买高意向搜索流量”。是否继续放大，取决于增量验证，而不是平台 ROI。</td></tr>
</tbody></table></div>"""


def _glossary() -> str:
    return """<div class="card vs-glossary">
<h2>常用术语速查</h2>
<dl>
<div><dt>费比</dt><dd>广告花费占商智成交额的比例。10% = 每卖 100 元，花 10 元广告。</dd></div>
<div><dt>商智成交额</dt><dd>店铺后台统计的真实成交，包含自然流量订单，不全是广告带来的。</dd></div>
<div><dt>全站成交</dt><dd>全站营销报表里的成交，几乎等于整店成交；不能当成“纯广告成交”。</dd></div>
<div><dt>毛利率</dt><dd>扣掉货款、运费、包材后剩下的比例。比如卖 100 元剩 25 元，毛利 25%。</dd></div>
<div><dt>保本 ROI</dt><dd>1 ÷ 毛利率。毛利 25% 时保本 ROI = 4；广告 ROI 低于 4 就亏钱。</dd></div>
<div><dt>净利</dt><dd>商智成交 × 毛利率 − 广告花费（简化口径，不含人工/房租等）。</dd></div>
</dl></div>"""


def render_vs(d, accounts_all=None, preset: str = "") -> str:
    meta = d["meta"]
    a, b = meta["a"], meta["b"]
    da, db = a["days"], b["days"]
    selected = {y["key"] for y in meta["accounts"]}
    ai_args = json.dumps({"a_start": a["start"], "a_end": a["end"],
                          "b_start": b["start"], "b_end": b["end"],
                          "accounts": [y["key"] for y in meta["accounts"]]},
                         ensure_ascii=False)
    acc_boxes = "".join(
        f'<label><input type="checkbox" name="accounts" value="{_e(x["key"])}"'
        f'{" checked" if x["key"] in selected else ""}>{_e(x["label"])}</label>'
        for x in (accounts_all or meta["accounts"]))

    charts, gmv_html, spend_html = _build_vs_charts(d)
    chart_json = json.dumps(charts, ensure_ascii=False).replace("</", "<\\/")
    sku_table_html, sku_count = _all_sku_table(d)
    grp_count = len([g for g in d["groups"] if not g.get("orphan")])

    tab_counts = {"vs-grp": grp_count, "vs-sku": sku_count}
    tabs_html = "".join(
        f'<div class="tab{" active" if i == 0 else ""}" data-tab="{cid}" '
        f'onclick="showTab(\'{cid}\',this)">{_e(label)}'
        + (f'<span class="tab-cnt">{tab_counts[cid]}</span>' if tab_counts.get(cid) else "")
        + '</div>'
        for i, (cid, label) in enumerate(VS_TABS))

    page_ov = f"""
{_freshness_notice(d)}
{_verdict_banner(d)}
{_big_kpis(d)}
<p class="hint" style="margin-top:-6px">以上 KPI 统计<b>有 SPU 计划的组</b>（本次调整的商品）；
  全账号含其他快车商品，详见「② 分组与花费明细」。</p>
{_group_cards(d)}

<div class="card">
  <h2>每日趋势：商智成交额 × 费比 <span class="tagline">悬停查看每日数值 · 黄色虚线 = B 期开始</span></h2>
  {gmv_html}
</div>
<div class="card">
  <h2>每日花费构成：快车/智能 + 全站营销 <span class="tagline">堆叠柱状图</span></h2>
  {spend_html}
</div>
<div class="card">
  <h2>商品红黑榜 <span class="tagline">日均商智成交 ≥ ¥500</span></h2>
  {_simple_skus(d)}
  <p class="hint" style="margin-top:12px">“费比高于毛利”= 广告本身在亏钱；“接近保本”= 只剩很薄的安全垫。B 期全站花费按商智成交占比分摊，同一计划内费比相同。</p>
</div>
<div class="card">
  <h2>下一步行动建议</h2>
  {_next_steps(d)}
</div>
<div class="card">
  <h2>AI 用大白话解读这组对比 <span class="tagline">调用现有 LLM 接口，约 20~60 秒</span></h2>
  <button class="btn" id="vsAiBtn" onclick="vsAi(this)">AI 解读这组对比</button>
  <div id="vsAi" class="vs-ai-out"></div>
</div>
"""

    page_grp = f"""
<div class="card">
  <h2>完整分组对比表 <span class="tagline">点击表头排序</span>
    <button type="button" class="btn sec mini" style="margin-left:auto" onclick="exportTableCsv(this,'vs-groups')">导出 CSV</button>
  </h2>
  <p class="hint">A 列是「史上最强」区间，B 列是「现代最强」区间；花费包含快车/智能/全站智能 + 全站营销。
    费比 = 总花费 ÷ 商智成交额；净利 = 商智成交额 × 加权毛利率 − 总花费。</p>
  {_group_table(d)}
</div>
<div class="card">
  <h2>花费构成：快车/智能 vs 全站营销
    <button type="button" class="btn sec mini" style="margin-left:auto" onclick="exportTableCsv(this,'vs-channels')">导出 CSV</button>
  </h2>
  <p class="hint">有 SPU 计划的组只覆盖本次调整的商品；其他快车商品单列，避免与计划组混淆。</p>
  {_channel_table(d)}
</div>
"""

    page_sku = f"""
<div class="card">
  <h2>逐商品明细 <span class="tagline">按风险档位 / 分组 / 关键词筛选</span></h2>
  <p class="hint">判断规则：B 期费比 ≥ 毛利率 → 亏损/临界；≥ 毛利率 × 75% → 接近保本；否则安全。
    B 期全站营销花费按成员商智成交占比分摊，所以同一计划内各 SKU 的 B 费比相同。</p>
  {sku_table_html}
</div>
"""

    page_cal = f"""
<div class="card">
  <h2>1. 口径对照：为什么不能直接比平台 ROI</h2>
  <div class="tblwrap short"><table>
    <thead><tr><th class="l noSort" style="width:20%">维度</th><th class="l noSort" style="width:40%">快车 / 智能投放</th><th class="l noSort">全站营销（单品推广）</th></tr></thead>
    <tbody>
      <tr><td class="l"><b>计费方式</b></td><td class="l">CPC，按点击扣费</td><td class="l">CPC，按“目标成交投产比”智能出价</td></tr>
      <tr><td class="l"><b>成交口径</b></td><td class="l">点击后 15 天归因订单（广告成交）</td><td class="l">全站成交（含自然流量、全店归因订单）</td></tr>
      <tr><td class="l"><b>ROI 分子</b></td><td class="l">广告归因成交额</td><td class="l">商智全店成交额（几乎全部订单）</td></tr>
      <tr><td class="l"><b>能直接比吗</b></td><td class="l" colspan="2"><b class="down">不能。</b>分子不是同一批订单，全站 ROI 天然高 1~3 倍。</td></tr>
      <tr><td class="l"><b>应该比什么</b></td><td class="l" colspan="2"><b>费比 = 广告总花费 ÷ 商智成交额</b>；以及“多花 1 元带来的增量成交”。</td></tr>
    </tbody>
  </table></div>
</div>
<div class="card">
  <h2>2. 口径证据：全站成交 vs 商智成交</h2>
  <p class="hint">拿两个区间里带 SPU 计划的分组，把“全站成交/全站订单行”和同一批 SKU 的商智成交/订单对照：</p>
  {_evidence_table(d)}
  <div class="note"><b>结论：</b>全站营销的“全站订单行”与商智订单大体对应，覆盖比例取决于计划预算/投放范围，说明它统计的是<b>整店成交</b>，
    不是广告点击带来的成交。因此“全站 ROI 12”只代表“整店成交是广告花费的 12 倍”，并不代表“每花 1 元多赚 12 元”。</div>
</div>
<div class="card">
  <h2>3. 同一 SKU 的两个 ROI，差距来自分子</h2>
  {_same_sku_table(d)}
</div>
<div class="card">
  <h2>4. 结论表</h2>
  {_conclusion_table(d)}
</div>
{_glossary()}
<div class="card">
  <h2>数据口径与局限</h2>
  <ul class="sum">
    <li>成交/订单来自商智（T+1 结算，最近 15 天每晚重抓；最近 1~2 天可能仍低于终值）。</li>
    <li>广告花费来自京准通：快车/智能（jst_sku + kw_ad）、全站智能（jzt campaignType 153）、全站营销（swa_campaign）。</li>
    <li>全站营销只有 SPU 级花费，SKU 级花费按成员商智成交占比分摊，仅用于结构观察。</li>
    <li>毛利率统一用当前 ERP 成本缓存重算（A/B 同一份成本），但分母是各期自己的前台件单价；
      所以 A/B 的毛利率差异可能来自价格、产品结构或最近 1~2 天商智件数尚未结算，不完全是广告效率变化。</li>
    <li>“费比”是总花费/商智成交，天然含自然成交；它衡量“广告费占生意的比例”，不衡量纯增量。</li>
  </ul>
</div>
"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>史上最强 vs 现代最强 · 快车/全站区间对比</title>
<style>{BASE_CSS}{UI_CSS}{VS_EXTRA_CSS}</style>{_echarts_src()}</head>
<body>
{navtop("/vs")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{SPIKE_SVG}快车 / 全站营销 · 同口径区间对比</div>
  <h1>史上最强 vs 现代最强</h1>
  <div class="metaline">
    <span>A 期 {_e(a['start'])} ~ {_e(a['end'])}（{da}天）</span><span class="dot"></span>
    <span>B 期 {_e(b['start'])} ~ {_e(b['end'])}（{db}天）</span><span class="dot"></span>
    <span>账号 {' / '.join(_e(x['label']) for x in meta['accounts'])}</span><span class="dot"></span>
    <span>生成 {_e(meta['generatedAt'])}</span>
  </div>
  <form class="rangebar" method="get" action="/vs">
    <label>A 起 <input type="date" name="a_start" value="{_e(a['start'])}"></label>
    <label>止 <input type="date" name="a_end" value="{_e(a['end'])}"></label>
    <span class="sep"></span>
    <label>B 起 <input type="date" name="b_start" value="{_e(b['start'])}"></label>
    <label>止 <input type="date" name="b_end" value="{_e(b['end'])}"></label>
    <span class="sep"></span>
    <span class="accts">{acc_boxes}</span>
    <button type="submit">开始对比</button>
    <a href="/vs?preset=switch">本次切换（11/18）</a>
    <a href="/vs?preset=week">前7天 vs 后7天</a>
    <a href="/report">区间报表 →</a>
  </form>
</div></header>
<div class="tabs-sticky"><div class="wrap">
  <div class="tabs">{tabs_html}</div>
  <div class="tabs-kbd"><kbd>1</kbd>-<kbd>4</kbd> 切页 · <kbd>/</kbd> 搜索</div>
</div></div>
<div class="wrap">
  <section class="page active" id="vs-ov">{page_ov}</section>
  <section class="page" id="vs-grp">{page_grp}</section>
  <section class="page" id="vs-sku">{page_sku}</section>
  <section class="page" id="vs-cal">{page_cal}</section>
  <div class="foot">数据来源：京准通（概览 / 智能投放 / 快车关键词 / 全站营销）+ 商智（商品明细）。金额单位：元；ROI 为倍数。</div>
</div>
<button type="button" class="btt" id="bttBtn" title="回到顶部" aria-label="回到顶部">↑</button>
<script>{REPORT_JS}</script>
<script>window.__CHARTS={chart_json};</script>
<script>
var VS_ARGS = {ai_args};
async function vsAi(btn) {{
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
</body></html>"""


def render_vs_error(msg: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>无法对比</title><style>{BASE_CSS}{UI_CSS}</style></head><body>
{navtop("/vs")}
<div class="wrap" style="padding-top:36px"><div class="card">
<h2>这个区间没有数据</h2><div class="warnbox">{_e(msg)}</div>
<p class="hint">请换一个区间，或先在控制台补抓对应日期的数据。</p>
<p><a class="btn" href="/vs">← 返回对比</a> <a class="btn sec" href="/">回到控制台</a></p>
</div></div></body></html>"""
