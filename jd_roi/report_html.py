# -*- coding: utf-8 -*-
"""把分析结果渲染为带图表的美观 HTML 报告（自包含，内联 ECharts）。

核心呈现：每个商品的「调整前 vs 调整后」两区间对比（花费/成交额/订单/ROI）。
用法:
    python -m jd_roi.report_html          # 生成
    python -m jd_roi.report_html --open   # 生成并打开
"""
from __future__ import annotations

import html
import json
import sys

from . import analyze, config

ASSETS = config.BASE_DIR / "jd_roi" / "assets" / "echarts.min.js"

TIER_CLS = {"放大": "t-grow", "优化": "t-opt", "止损": "t-stop", "未启动": "t-idle"}
TIER_COLOR = {"放大": "#16a34a", "优化": "#d97706", "止损": "#dc2626", "未启动": "#94a3b8"}
CUR_C = "#2563eb"   # 调整后
PRE_C = "#94a3b8"   # 调整前

CSS = """
:root{--bg:#f4f6fb;--card:#fff;--line:#e9eef7;--txt:#0f172a;--sub:#64748b;
--blue:#2563eb;--green:#16a34a;--amber:#d97706;--red:#dc2626;--teal:#0d9488;--pre:#94a3b8;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);line-height:1.55;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1440px;margin:0 auto;padding:26px 20px 70px}
.hero{background:linear-gradient(120deg,#1e3a8a,#2563eb 55%,#0ea5e9);color:#fff;
border-radius:20px;padding:26px 30px;box-shadow:0 10px 30px rgba(37,99,235,.25)}
.hero h1{margin:0 0 8px;font-size:25px;letter-spacing:.5px}
.hero .meta{font-size:13px;opacity:.92}
.hero .tags{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
.hero .tag{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.28);
border-radius:999px;padding:5px 13px;font-size:12.5px}
.hero .tag b{font-weight:700}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(196px,1fr));gap:14px;margin:20px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:15px 17px;
box-shadow:0 2px 8px rgba(15,23,42,.04);transition:.2s}
.kpi:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(15,23,42,.09)}
.kpi .l{color:var(--sub);font-size:12.5px}
.kpi .row{display:flex;align-items:baseline;gap:8px;margin-top:6px}
.kpi .pre{color:var(--pre);font-size:15px;text-decoration:line-through;text-decoration-color:#cbd5e1}
.kpi .arw{color:var(--sub);font-size:12px}
.kpi .v{font-size:23px;font-weight:800;letter-spacing:.3px}
.kpi .d{font-size:12.5px;margin-top:3px;font-weight:600}
.up{color:var(--green)}.down{color:var(--red)}.flat{color:var(--sub)}
.sec-title{font-size:17px;font-weight:700;margin:30px 0 12px;display:flex;align-items:center;gap:9px}
.sec-title::before{content:"";width:5px;height:19px;border-radius:3px;background:var(--blue)}
.sec-sub{color:var(--sub);font-size:12.5px;font-weight:400;margin-left:4px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:16px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px 6px;
box-shadow:0 2px 8px rgba(15,23,42,.04);overflow:auto}
.panel h4{margin:2px 0 6px;font-size:14px;color:#334155}
.chart{width:100%;height:340px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 16px}
.btn{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 15px;
font-size:13px;cursor:pointer;color:var(--txt);transition:.15s}
.btn:hover{border-color:#c7d2fe}
.btn.active{background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:0 4px 12px rgba(37,99,235,.3)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;
box-shadow:0 2px 8px rgba(15,23,42,.04);position:relative;overflow:hidden}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px}
.card[data-tier="放大"]::before{background:var(--green)}
.card[data-tier="优化"]::before{background:var(--amber)}
.card[data-tier="止损"]::before{background:var(--red)}
.card[data-tier="未启动"]::before{background:#cbd5e1}
.card h3{margin:0;font-size:16px;display:inline-block}
.head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:13px}
.badge{font-size:11.5px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);color:var(--sub)}
.t-grow{background:#e8f8ee;color:#12813f;border-color:#bfe9cf}
.t-opt{background:#fff4e5;color:#a15c00;border-color:#ffe0b3}
.t-stop{background:#fdeaea;color:#b91c1c;border-color:#f8c9c9}
.t-idle{background:#eef1f6;color:#6b7280}
.acct{background:#eef2ff;color:#3730a3;border-color:#c7d2fe}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:11px}
.m{background:#f8fafc;border:1px solid var(--line);border-radius:11px;padding:9px 10px}
.m .l{font-size:11.5px;color:var(--sub)}
.m .v{font-size:15px;font-weight:700;margin-top:2px}
.m .p{font-size:11px;color:var(--sub);margin-top:1px}
.cmp{font-size:12.5px;color:var(--sub);margin-bottom:10px}
.sz{display:flex;gap:11px;align-items:center;background:#f8fafc;border:1px solid var(--line);
border-radius:12px;padding:10px 12px;margin:10px 0}
.sz img{width:46px;height:46px;object-fit:cover;border-radius:9px;border:1px solid var(--line);background:#fff}
.sz .nm{font-size:12.5px;font-weight:600;color:#334155;margin-bottom:3px}
.sz .st{font-size:12px;color:var(--sub)}
.pbar{height:7px;background:#e2e8f0;border-radius:5px;overflow:hidden;margin-top:5px;width:150px}
.pbar i{display:block;height:100%;background:linear-gradient(90deg,#2563eb,#0ea5e9)}
.split{margin:8px 0 10px}
.bar{height:9px;border-radius:6px;overflow:hidden;display:flex;background:#eef1f6;margin-top:5px}
.bar .s{background:var(--blue)}.bar .r{background:#c7d2fe}
.legend{font-size:12px;color:var(--sub);display:flex;justify-content:space-between;margin-top:4px}
.acts{margin:8px 0 12px;padding-left:18px;font-size:13px}
.acts li{margin:3px 0}
.wlab{color:var(--sub);font-size:12px;margin:8px 0 4px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border-radius:8px;padding:3px 8px;font-size:12px;border:1px solid}
.c-good{background:#e8f8ee;border-color:#bfe9cf;color:#12813f}
.c-bad{background:#fdeaea;border-color:#f8c9c9;color:#b91c1c}
.c-low{background:#fff4e5;border-color:#ffe0b3;color:#a15c00}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--sub);font-weight:600;background:#f8fafc}
table.cmpt th{text-align:center;border-bottom:2px solid var(--line)}
table.cmpt td{text-align:center}
table.cmpt td:first-child{text-align:left}
table.cmpt .grp{border-left:2px solid var(--line)}
.pre{color:var(--pre)}
.star{color:var(--red);font-weight:700}
.note{color:var(--sub);font-size:12px;margin-top:6px}
.sku{display:flex;gap:9px;align-items:center}
.sku img{width:38px;height:38px;object-fit:cover;border-radius:8px;border:1px solid var(--line);background:#fff}
.sku .nm{font-size:12.5px;font-weight:600;color:#334155}
.sku .sid{font-size:11.5px;color:var(--sub)}
table.skut th:first-child,table.skut td:first-child{text-align:left}
.footer{color:var(--sub);font-size:12.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:14px}
.empty{color:var(--sub);font-size:12px}
.summary{background:linear-gradient(180deg,#fff,#f7faff);border:1px solid #dbe6ff;border-radius:16px;padding:16px 20px 16px 36px;
box-shadow:0 2px 8px rgba(37,99,235,.06)}
.summary li{margin:7px 0;font-size:13.5px}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px;margin-top:14px}
.bcol{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.bcol h5{margin:0 0 10px;font-size:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.bcol.stop h5{color:var(--red)}.bcol.grow h5{color:var(--green)}.bcol.neg h5{color:var(--amber)}
.brow{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px dashed var(--line);font-size:12.5px}
.brow:last-child{border-bottom:none}
.brow .nm{color:#334155}.brow .v{color:var(--sub);white-space:nowrap}
.upside{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:12px}
.upside .u{background:#eef7f0;border:1px solid #cde9d6;border-radius:12px;padding:10px 13px}
.upside .u .l{font-size:12px;color:#3f6b4e}
.upside .u .v{font-size:19px;font-weight:800;color:#12813f;margin-top:2px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 10px;position:sticky;top:0;z-index:20;
background:rgba(244,246,251,.94);backdrop-filter:blur(8px);padding:10px 0}
.tab{border:1px solid var(--line);background:#fff;border-radius:12px;padding:9px 18px;font-size:13.5px;
cursor:pointer;color:#334155;font-weight:600;transition:.15s}
.tab:hover{border-color:#c7d2fe;color:var(--blue)}
.tab.active{background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:0 4px 12px rgba(37,99,235,.3)}
.tabpage{display:none}
.tabpage.active{display:block;animation:fade .25s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.guide{display:grid;grid-template-columns:repeat(auto-fit,minmax(410px,1fr));gap:16px;margin-top:12px}
.gsec{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.gsec h5{margin:0 0 10px;font-size:14px}
.gsec.stop h5{color:var(--red)}.gsec.grow h5{color:var(--green)}.gsec.neg h5{color:var(--amber)}
.gsec.gain h5{color:var(--blue)}.gsec.ref h5{color:#7c3aed}
.gi{display:flex;gap:10px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:12.5px}
.gi:last-child{border-bottom:none}
.gi .n{color:#0f172a;font-weight:600;flex:0 0 42%}
.gi .d{color:var(--sub)}
"""

JS_INIT = """
var CH = {};
function initChart(c){
  var el = document.getElementById(c.id);
  if(!el) return;
  if(!(el.clientWidth || el.offsetWidth)) return;   // 容器不可见，切到该标签时再初始化
  if(CH[c.id]){ CH[c.id].resize(); return; }
  var ch = echarts.init(el); ch.setOption(c.option); CH[c.id] = ch;
}
window.addEventListener('load', function(){
  if(typeof echarts === 'undefined'){ document.querySelectorAll('.panel').forEach(function(p){
    p.insertAdjacentHTML('beforeend','<div class="note">图表库未加载（离线），数据仍见表格</div>');}); return; }
  (window.__CHARTS__||[]).forEach(initChart);
  window.addEventListener('resize', function(){
    Object.keys(CH).forEach(function(k){ CH[k].resize(); }); });
});
function flt(t, btn){
  document.querySelectorAll('.toolbar .btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  document.querySelectorAll('.card[data-tier]').forEach(function(c){
    c.style.display=(t==='all'||c.dataset.tier===t)?'':'none';});
}
function tab(id, btn){
  document.querySelectorAll('.tabs .tab').forEach(function(b){b.classList.remove('active');});
  if(btn) btn.classList.add('active');
  document.querySelectorAll('.tabpage').forEach(function(p){p.classList.toggle('active', p.id===id);});
  (window.__CHARTS__||[]).forEach(function(c){
    var el=document.getElementById(c.id);
    if(el && el.closest('.tabpage') && el.closest('.tabpage').id===id) initChart(c);
  });
  Object.keys(CH).forEach(function(k){ CH[k].resize(); });
  window.scrollTo({top:0, behavior:'smooth'});
}
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _short(name: str) -> str:
    t = analyze._core_token(name)
    return t[:8] if t else str(name)[:8]


def _d(v_now: float, v_pre: float, better_high: bool = True, pct: bool = True) -> tuple[str, str]:
    """返回 (显示文本, css类)。"""
    if v_pre in (None, 0):
        delta = v_now - (v_pre or 0)
        txt = f"{delta:+.0f}" if not pct else "—"
        return txt, "flat"
    diff = v_now - v_pre
    r = diff / abs(v_pre) * 100 if pct else diff
    good = (diff > 0) if better_high else (diff < 0)
    cls = "up" if good else ("down" if diff != 0 else "flat")
    txt = f"{r:+.1f}%" if pct else f"{diff:+.2f}"
    return txt, cls


def _metric_kpi(label: str, v_pre: float, v_now: float, fmt: str, better_high: bool = True) -> str:
    txt, cls = _d(v_now, v_pre, better_high)
    pre_s = fmt.format(v_pre)
    now_s = fmt.format(v_now)
    return (f'<div class="kpi"><div class="l">{_e(label)}</div>'
            f'<div class="row"><span class="pre">{pre_s}</span><span class="arw">→</span>'
            f'<span class="v">{now_s}</span></div>'
            f'<div class="d {cls}">{txt} vs 调整前</div></div>')


def _kpi(label, value, delta="", cls="") -> str:
    d = f'<div class="d {cls}">{delta}</div>' if delta else ""
    return f'<div class="kpi"><div class="l">{_e(label)}</div><div class="v">{value}</div>{d}</div>'


def _chips(items, kind, metric) -> str:
    if not items:
        return '<span class="empty">无</span>'
    return "".join(f'<span class="chip {kind}">{_e(i["word"])} · {metric(i)}</span>' for i in items)


def _word_block(title, items, kind, metric) -> str:
    return (f'<div class="wlab">{_e(title)}</div><div class="chips">{_chips(items, kind, metric)}</div>')


def _kw_table(kw) -> str:
    def row(title, items, color):
        if not items:
            return ""
        tr = "".join(
            f'<tr><td>{_e(i["word"])}</td><td>{i.get("cost","")}</td>'
            f'<td>{i.get("ord", i.get("clk",""))}</td><td>{i.get("roi","—")}</td></tr>'
            for i in items[:12])
        return f'<tr><td colspan="4" style="text-align:left;color:{color};font-weight:600">{_e(title)}</td></tr>' + tr
    return ('<table><tr><th>关键词</th><th>花费</th><th>订单/点击</th><th>ROI</th></tr>'
            + row("★ 高效词(提价)", kw.get("good"), "#12813f")
            + row("▲ 低于保本(降价)", kw.get("low"), "#a15c00")
            + row("✕ 0单废词(否定)", kw.get("waste"), "#b91c1c") + '</table>')


def _split_html(sp) -> str:
    s, r = sp["search"], sp["recommend"]
    return (f'<div class="split"><div class="wlab">搜索 vs 推荐流量（按花费）</div>'
            f'<div class="bar"><i class="s" style="width:{s["share"]}%"></i>'
            f'<i class="r" style="width:{r["share"]}%"></i></div>'
            f'<div class="legend"><span>搜索 {s["share"]}% · 花费{s["cost"]} · ROI <b>{s["roi"]}</b></span>'
            f'<span>推荐 {r["share"]}% · 花费{r["cost"]} · ROI <b>{r["roi"]}</b></span></div></div>')


def _sz_html(sz) -> str:
    if not sz:
        return ""
    img = sz.get("img") or ""
    if img.startswith("//"):
        img = "https:" + img
    share = sz.get("adShare")
    if share is None:
        pen = '<span class="empty">广告渗透率：口径不可比（广告为15天归因，可能跨期）</span>'
    else:
        pen = (f'<div class="st">广告渗透率 <b>{share}%</b>（广告成交/商智总成交）</div>'
               f'<div class="pbar"><i style="width:{min(share,100)}%"></i></div>')
    cmp_txt = ""
    if sz.get("cmpAmt") is not None:
        cmp_txt = f' · 上期总成交 ¥{sz["cmpAmt"]:,.0f}'
    shop = f' · 商智店铺 {_e(sz.get("shop"))}' if sz.get("shop") else ""
    return (f'<div class="sz"><img src="{_e(img)}" alt="">'
            f'<div style="flex:1"><div class="nm">商智 SKU {_e(sz["skuId"])} · {_e(str(sz["name"])[:34])}</div>'
            f'<div class="st">总成交 ¥{sz["amt"]:,.0f}{cmp_txt} · 访客 {sz["visitors"]:,} · '
            f'浏览量 {sz["views"]:,} · 转化率 {sz["cvr"]}%{shop}</div>'
            f'{pen}</div></div>')


def _card(p) -> str:
    c, cp = p["cur"], p["cmp"]
    def cell(label, now, pre, better_high=True, fmt="{:,.0f}"):
        txt, cls = _d(now, pre, better_high)
        return (f'<div class="m"><div class="l">{label}</div><div class="v">{fmt.format(now)}</div>'
                f'<div class="p">前 {fmt.format(pre)} <span class="{cls}">{txt}</span></div></div>')

    metrics = "".join([
        cell("广告花费", c["cost"], cp["cost"], False),
        cell("成交金额", c["amt"], cp["amt"], True),
        cell("投产比", c["roi"], cp["roi"], True, "{:.2f}"),
        cell("订单", c["ord"], cp["ord"], True, "{:,.0f}"),
    ])
    cmp_line = (f'展现 {c["imp"]:,}（前 {cp["imp"]:,}） · 点击 {c["clk"]:,}（前 {cp["clk"]:,}） · '
                f'加购 {c["cart"]:,}（前 {cp["cart"]:,}） · 目标ROI {p["targetRoi"] or "—"}')
    acts = "".join(f"<li>{_e(a)}</li>" for a in p["actions"]) or "<li>保持观察</li>"
    words = ""
    if p["goodWords"] or p["wasteWords"] or p["lowWords"]:
        words = ('<div class="wlab">关键词建议</div>'
                 + _word_block("★ 高效词（提价/加词）", p["goodWords"], "c-good", lambda i: f'ROI{i["roi"]}·{i["ord"]}单')
                 + _word_block("▲ 低于保本（降价）", p["lowWords"], "c-low", lambda i: f'ROI{i["roi"]}·花{i["cost"]}')
                 + _word_block("✕ 0单废词（否定）", p["wasteWords"], "c-bad", lambda i: f'花{i["cost"]}'))
    kw_html = _kw_table(p["kw"]) if p.get("kw") else ""
    if not words and not kw_html and p["tier"] != "未启动":
        words = '<div class="note">该计划为全站智能/无词级报表，建议以商品维度优化（主图、价格、评价）。</div>'
    split_html = _split_html(p["split"]) if p["split"] else ""
    return f"""
<div class="card" data-tier="{_e(p['tier'])}">
  <div class="head">
    <h3>{_e(p['name'])}</h3>
    <span class="badge {TIER_CLS.get(p['tier'],'')}">{_e(p['tier'])}</span>
    <span class="badge">{_e(p['typeLabel'])}</span>
    <span class="badge acct">{_e(p.get('account') or '')}</span>
    <span class="badge">{_e(p['source'])}口径</span>
  </div>
  <div class="metrics">{metrics}</div>
  <div class="cmp">{cmp_line}</div>
  {_sz_html(p.get('sz'))}
  {split_html}
  <ul class="acts">{acts}</ul>
  {words}
  {kw_html}
</div>"""


def _cmp_table(products: list, be: float) -> str:
    """商品级 调整前 vs 调整后 对比大表。"""
    rows = [p for p in products if p["cur"]["cost"] > 0 or p["cmp"]["cost"] > 0]
    if not rows:
        return '<div class="note">无对比数据</div>'
    body = ""
    for p in rows:
        c, cp = p["cur"], p["cmp"]
        dc, cdc = _d(c["cost"], cp["cost"], False)
        da, cda = _d(c["amt"], cp["amt"], True)
        do, cdo = _d(c["ord"], cp["ord"], True)
        dr, cdr = _d(c["roi"], cp["roi"], True, pct=False)
        flag = '<span class="star">★亏</span>' if 0 < c["roi"] < be else ("—" if c["cost"] == 0 else "健康")
        body += (
            f'<tr><td>{_e(p["name"])}<div class="sku"><span class="sid">{_e(p.get("account") or "")} · {_e(p["typeLabel"])}</span></div></td>'
            f'<td>¥{cp["cost"]:,.0f}</td><td>¥{c["cost"]:,.0f}</td><td class="{cdc}">{dc}</td>'
            f'<td class="grp">¥{cp["amt"]:,.0f}</td><td>¥{c["amt"]:,.0f}</td><td class="{cda}">{da}</td>'
            f'<td class="grp">{cp["ord"]:,}</td><td>{c["ord"]:,}</td><td class="{cdo}">{do}</td>'
            f'<td class="grp">{cp["roi"]:.2f}</td><td><b>{c["roi"]:.2f}</b></td><td class="{cdr}">{dr}</td>'
            f'<td class="grp">{flag}</td></tr>')
    return (
        '<table class="cmpt"><thead>'
        '<tr><th rowspan="2">商品 / 计划</th><th colspan="3">广告花费(元)</th><th colspan="3">成交金额(元)</th>'
        '<th colspan="3">订单数</th><th colspan="3">投产比 ROI</th><th rowspan="2">判定</th></tr>'
        '<tr><th>调整前</th><th>调整后</th><th>Δ</th>'
        '<th class="grp">调整前</th><th>调整后</th><th>Δ</th>'
        '<th class="grp">调整前</th><th>调整后</th><th>Δ</th>'
        '<th class="grp">调整前</th><th>调整后</th><th>Δ</th></tr>'
        '</thead><tbody>' + body + '</tbody></table>')


def _build_charts(d: dict) -> list:
    ps = [p for p in d["products"] if p["cur"]["cost"] > 0 or p["cmp"]["cost"] > 0]
    be = d["meta"]["breakeven"]
    top = sorted(ps, key=lambda p: -max(p["cur"]["cost"], p["cmp"]["cost"]))[:12]
    cats = [_short(p["name"]) for p in top]
    charts = []

    def grouped(name, pre_v, cur_v, title, yname):
        return {"id": name, "title": title, "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"data": ["调整前", "调整后"], "top": 0},
            "grid": {"left": 8, "right": 12, "bottom": 6, "top": 38, "containLabel": True},
            "xAxis": {"type": "category", "data": cats, "axisLabel": {"interval": 0, "rotate": 30, "fontSize": 10}},
            "yAxis": {"type": "value", "name": yname},
            "series": [
                {"name": "调整前", "type": "bar", "data": pre_v, "itemStyle": {"color": PRE_C, "borderRadius": [4, 4, 0, 0]}},
                {"name": "调整后", "type": "bar", "data": cur_v, "itemStyle": {"color": CUR_C, "borderRadius": [4, 4, 0, 0]}},
            ]}}

    charts.append(grouped("c1", [p["cmp"]["cost"] for p in top], [p["cur"]["cost"] for p in top],
                          "① 各商品 广告花费：调整前 vs 调整后（元）", "元"))
    charts.append(grouped("c2", [p["cmp"]["amt"] for p in top], [p["cur"]["amt"] for p in top],
                          "② 各商品 成交金额：调整前 vs 调整后（元）", "元"))
    o = grouped("c3", [p["cmp"]["ord"] for p in top], [p["cur"]["ord"] for p in top],
                "③ 各商品 订单数：调整前 vs 调整后（单）", "单")
    charts.append(o)
    roi_chart = grouped("c4", [p["cmp"]["roi"] for p in top], [p["cur"]["roi"] for p in top],
                        f"④ 各商品 投产比：调整前 vs 调整后（红线=保本ROI {be}）", "ROI")
    roi_chart["option"]["series"][1]["markLine"] = {
        "silent": True, "symbol": "none", "lineStyle": {"color": "#dc2626", "type": "dashed"},
        "data": [{"yAxis": be, "label": {"formatter": f"保本 {be}", "color": "#dc2626"}}]}
    charts.append(roi_chart)

    sp = [p for p in ps if p["split"] and p["cur"]["cost"] > 0]
    if sp:
        charts.append({"id": "c5", "title": "⑤ 搜索 / 推荐 流量花费结构（调整后）", "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"data": ["搜索", "推荐"], "top": 0},
            "grid": {"left": 8, "right": 20, "bottom": 6, "top": 38, "containLabel": True},
            "xAxis": {"type": "value"},
            "yAxis": {"type": "category", "data": [_short(p["name"]) for p in sp], "axisLabel": {"fontSize": 10}},
            "series": [
                {"name": "搜索", "type": "bar", "stack": "t", "data": [p["split"]["search"]["cost"] for p in sp], "itemStyle": {"color": CUR_C}},
                {"name": "推荐", "type": "bar", "stack": "t", "data": [p["split"]["recommend"]["cost"] for p in sp], "itemStyle": {"color": "#c7d2fe"}},
            ]}})
    return charts


def _sku_table(skus: list) -> str:
    rows = [s for s in skus if s["cost"] > 0 or s["szAmt"] > 0][:40]
    if not rows:
        return '<div class="note">暂无 SKU 级数据（需先抓取「智能投放-商品」报表）</div>'
    body = ""
    for s in rows:
        share = "—" if s["adShare"] is None else f'{s["adShare"]}%'
        accts = "、".join(s["accts"]) if s["accts"] else "—"
        shops = " · ".join(f'{v.get("label", k)} ¥{v["amt"]:,.0f}' for k, v in (s.get("shops") or {}).items()) or "—"
        img = s.get("img") or ""
        if img.startswith("//"):
            img = "https:" + img
        dc, cdc = _d(s["cost"], s["cmpCost"], False)
        da, cda = _d(s["amt"], s["cmpAmt"], True)
        body += (
            f'<tr><td><div class="sku"><img src="{_e(img)}" alt="">'
            f'<div><div class="nm">{_e(str(s["name"])[:30])}</div>'
            f'<div class="sid">{_e(s["skuId"])} · 投放账号 {_e(accts)}</div></div></div></td>'
            f'<td>¥{s["cmpCost"]:,.0f}</td><td>¥{s["cost"]:,.0f}</td><td class="{cdc}">{dc}</td>'
            f'<td class="grp">¥{s["cmpAmt"]:,.0f}</td><td>¥{s["amt"]:,.0f}</td><td class="{cda}">{da}</td>'
            f'<td class="grp"><b>{s["roi"]:.2f}</b></td>'
            f'<td class="grp">¥{s["szAmt"]:,.0f}<div class="sid">{_e(shops)}</div></td>'
            f'<td>{s["szVisitors"]:,}</td><td>{share}</td></tr>')
    return ('<table class="skut cmpt"><thead>'
            '<tr><th rowspan="2">商品（SKU / 投放账号）</th><th colspan="3">广告花费</th><th colspan="3">广告成交</th>'
            '<th rowspan="2">后ROI</th><th colspan="2">商智（跨店铺）</th><th rowspan="2">渗透率</th></tr>'
            '<tr><th>调整前</th><th>调整后</th><th>Δ</th><th class="grp">调整前</th><th>调整后</th><th>Δ</th>'
            '<th class="grp">总成交(分店)</th><th>访客</th></tr></thead>'
            '<tbody>' + body + '</tbody></table>')


def _advice_html(d: dict) -> str:
    a = d.get("advice") or {}
    if not a:
        return ""
    be = d["meta"]["breakeven"]
    up = a.get("upside") or {}
    li = "".join(f"<li>{_e(s)}</li>" for s in a.get("summary", []))
    stop_rows = "".join(
        f'<div class="brow"><span class="nm">{_e(str(x["name"])[:22])}</span>'
        f'<span class="v">花¥{x["cost"]:,.0f} · ROI {x["roi"]} · 缺口¥{x["loss"]:,.0f}</span></div>'
        for x in (a.get("stop") or [])[:8])
    grow_rows = "".join(
        f'<div class="brow"><span class="nm">{_e(str(x["name"])[:22])}</span>'
        f'<span class="v">花¥{x["cost"]:,.0f} · ROI <b>{x["roi"]}</b> · 建议加投 +¥{x["add"]:,.0f}</span></div>'
        for x in (a.get("grow") or [])[:8])
    neg_rows = "".join(
        f'<div class="brow"><span class="nm">{_e(x["word"])}</span>'
        f'<span class="v">浪费 ¥{x["cost"]:,.1f} · 涉及{x["plans"]}个计划</span></div>'
        for x in (a.get("negatives") or [])[:12])
    upside = ""
    if up:
        upside = (
            '<div class="upside">'
            f'<div class="u"><div class="l">低于保本的花费（可腾挪）</div><div class="v">¥{up.get("stopCost",0):,.0f}</div></div>'
            f'<div class="u"><div class="l">当前平均ROI</div><div class="v">{up.get("stopRoi",0):.2f}</div></div>'
            f'<div class="u"><div class="l">高效计划ROI</div><div class="v">{up.get("targetRoi",0):.2f}</div></div>'
            f'<div class="u"><div class="l">预计多产出</div><div class="v">+¥{up.get("estGain",0):,.0f}</div></div>'
            f'<div class="u"><div class="l">优化后整体ROI</div><div class="v">{up.get("roiAfter",0):.2f}</div></div>'
            '</div>')
    return (
        '<div class="sec-title">一、决策摘要<span class="sec-sub">自动生成，直接指导投放</span></div>'
        f'<div class="summary"><ul>{li}</ul>{upside}</div>'
        '<div class="board">'
        f'<div class="bcol stop"><h5>立即止损 / 优化（ROI &lt; 保本 {be}）<span class="badge">合计 ¥{up.get("stopCost",0):,.0f}</span></h5>'
        f'{stop_rows or "<div class=\"note\">无</div>"}</div>'
        f'<div class="bcol grow"><h5>加大投入（ROI ≥ 4）<span class="badge">{len(a.get("grow") or [])} 个</span></h5>'
        f'{grow_rows or "<div class=\"note\">无</div>"}</div>'
        f'<div class="bcol neg"><h5>否定词清单（0单浪费）<span class="badge">合计 ¥{up.get("negCost",0):,.0f}</span></h5>'
        f'{neg_rows or "<div class=\"note\">无</div>"}</div>'
        '</div>')


def _shops_html(d: dict) -> str:
    shops = d.get("shops") or []
    if not shops:
        return ""
    cards = ""
    for s in shops:
        ad_c, ad_p = s["ad"]["cur"], s["ad"]["cmp"]
        fl_c, fl_p = (s["flow"]["cur"] or {}), (s["flow"]["cmp"] or {})
        scope = "本次分析账号" if s.get("inScope") else "参考店铺（仅商智）"
        cards += (
            f'<div class="bcol"><h5>{_e(s["label"])} <span class="badge">{scope}</span></h5>'
            f'<div class="brow"><span class="nm">广告花费</span><span class="v">¥{ad_p["cost"]:,.0f} → <b>¥{ad_c["cost"]:,.0f}</b></span></div>'
            f'<div class="brow"><span class="nm">广告投产比</span><span class="v">{ad_p["roi"]} → <b>{ad_c["roi"]}</b></span></div>'
            f'<div class="brow"><span class="nm">广告成交金额</span><span class="v">¥{ad_p["amt"]:,.0f} → ¥{ad_c["amt"]:,.0f}</span></div>'
            f'<div class="brow"><span class="nm">商智总成交（全渠道）</span><span class="v">¥{fl_p.get("amt", 0):,.0f} → <b>¥{fl_c.get("amt", 0):,.0f}</b></span></div>'
            f'<div class="brow"><span class="nm">商智访客数</span><span class="v">{fl_p.get("visitors", 0):,} → {fl_c.get("visitors", 0):,}</span></div>'
            f'<div class="brow"><span class="nm">商智转化率</span><span class="v">{fl_p.get("cvr", 0)}% → {fl_c.get("cvr", 0)}%</span></div>'
            '</div>')
    return ('<div class="sec-title">二、双店铺概览<span class="sec-sub">广告（京准通）与商智（店铺）分别前后对比</span></div>'
            f'<div class="board">{cards}</div>')


def _guide_html(d: dict) -> str:
    g = (d.get("advice") or {}).get("guide") or []
    if not g:
        return ""
    secs = ""
    for s in g:
        items = "".join(
            f'<div class="gi"><span class="n">{_e(x["name"])}</span><span class="d">{_e(x["detail"])}</span></div>'
            for x in s.get("items", []))
        secs += f'<div class="gsec {_e(s.get("icon", ""))}"><h5>{_e(s["title"])}</h5>{items or "<div class=\'note\'>—</div>"}</div>'
    return ('<div class="sec-title">运营指导<span class="sec-sub">按优先级排序，可直接执行</span></div>'
            f'<div class="guide">{secs}</div>')


def render(d: dict) -> str:
    m, t = d["meta"], d["totals"]
    c, cp = t["cur"], t["cmp"]
    charts = _build_charts(d)
    charts_json = json.dumps(charts, ensure_ascii=False)

    kpis = "".join([
        _metric_kpi("广告花费", cp["cost"], c["cost"], "¥{:,.0f}", better_high=False),
        _metric_kpi("成交金额", cp["amt"], c["amt"], "¥{:,.0f}", True),
        _metric_kpi("投产比 ROI", cp["roi"], c["roi"], "{:.2f}", True),
        _metric_kpi("订单数", cp["ord"], c["ord"], "{:,.0f}", True),
        _metric_kpi("点击数", cp["clk"], c["clk"], "{:,.0f}", True),
        _metric_kpi("展现数", cp["imp"], c["imp"], "{:,.0f}", True),
        _kpi("保本ROI", f'{m["breakeven"]}', "低于此值即亏损", "flat"),
    ])

    tiers = ["放大", "优化", "止损", "未启动"]
    counts = {k: sum(1 for p in d["products"] if p["tier"] == k) for k in tiers}
    btns = f'<button class="btn active" onclick="flt(\'all\',this)">全部 {len(d["products"])}</button>'
    for k in tiers:
        if counts[k]:
            btns += f'<button class="btn" onclick="flt(\'{k}\',this)">{k} {counts[k]}</button>'

    panel_html = "".join(
        f'<div class="panel"><h4>{_e(ch["title"])}</h4><div class="chart" id="{ch["id"]}"></div></div>'
        for ch in charts)
    cards = "".join(_card(p) for p in d["products"])

    echarts_src = ""
    if ASSETS.exists():
        echarts_src = "<script>" + ASSETS.read_text(encoding="utf-8") + "</script>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>京东广告运营分析报告 · 调整前后对比</title><style>{CSS}</style>{echarts_src}</head>
<body><div class="wrap">
  <div class="hero">
    <h1>京东广告运营分析报告 · 调整前后对比</h1>
    <div class="meta">调整前 <b>{m['cmpStart']} ~ {m['cmpEnd']}</b>　→　调整后 <b>{m['start']} ~ {m['end']}</b>　|　生成 {m['generatedAt']}</div>
    <div class="tags">
      <span class="tag">分析账号：<b>{_e('、'.join(a.get('label') or a.get('key') for a in (m.get('accounts') or [])))}</b></span>
      <span class="tag">商智店铺：{_e('、'.join(m.get('szShops') or []))}</span>
      <span class="tag">保本ROI <b>{m['breakeven']}</b></span>
      <span class="tag">口径：{_e(m['caliber'])}</span>
    </div>
  </div>
  <nav class="tabs">
    <button class="tab active" onclick="tab('p-ov',this)">总览</button>
    <button class="tab" onclick="tab('p-act',this)">决策与行动</button>
    <button class="tab" onclick="tab('p-cmp',this)">商品对比</button>
    <button class="tab" onclick="tab('p-chart',this)">图表</button>
    <button class="tab" onclick="tab('p-sku',this)">SKU关联</button>
    <button class="tab" onclick="tab('p-plan',this)">计划明细</button>
  </nav>

  <section class="tabpage active" id="p-ov">
    <div class="sec-title">核心指标：调整前 → 调整后<span class="sec-sub">分析账号 {_e('、'.join(a.get('label') or a.get('key') for a in (m.get('accounts') or [])))}</span></div>
    <div class="kpis">{kpis}</div>
    {_shops_html(d)}
  </section>

  <section class="tabpage" id="p-act">
    {_advice_html(d)}
    {_guide_html(d)}
  </section>

  <section class="tabpage" id="p-cmp">
    <div class="sec-title">商品级 前后对比总表<span class="sec-sub">按调整后花费排序；★亏 = 调整后 ROI 低于保本 {m['breakeven']}</span></div>
    <div class="panel">{_cmp_table(d['products'], m['breakeven'])}</div>
  </section>

  <section class="tabpage" id="p-chart">
    <div class="sec-title">可视化对比<span class="sec-sub">每张图均为「调整前 vs 调整后」</span></div>
    <div class="charts">{panel_html}</div>
  </section>

  <section class="tabpage" id="p-sku">
    <div class="sec-title">SKU 关联（京准通 × 商智，跨店铺）<span class="sec-sub">广告按 skuId 合并，商智自动取到对应店铺</span></div>
    <div class="panel">{_sku_table(d.get('skus') or [])}</div>
  </section>

  <section class="tabpage" id="p-plan">
    <div class="sec-title">计划明细与建议<span class="sec-sub">点下方按钮按档位筛选</span></div>
    <div class="toolbar">{btns}</div>
    <div class="grid">{cards}</div>
  </section>

  <div class="footer">
    保本ROI = 客单价 / 利润 = {m['breakeven']}（客单价{m['costParams']['price']}、成本{m['costParams']['product']}、运费{m['costParams']['shipping']}）；
    档位：放大 ROI≥4，优化 保本~4，止损 &lt;保本。SKU 优先按 skuId 精确关联（京准通商品报表 skuId = 商智 sku_id），
    商智可跨店铺取数（广告在账号B、商品成交在主账号 也能关联）。
  </div>
</div>
<script>window.__CHARTS__ = {charts_json};</script>
<script>{JS_INIT}</script></body></html>"""


def main() -> None:
    data = analyze.build_analysis()
    out = config.BASE_DIR / "ad_roi_report.html"
    out.write_text(render(data), encoding="utf-8")
    print(f"[OK] 报告已生成 -> {out}  ({out.stat().st_size/1024:.0f} KB)")
    if "--open" in sys.argv:
        import webbrowser

        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
