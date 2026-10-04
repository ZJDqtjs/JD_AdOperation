# -*- coding: utf-8 -*-
"""新版可视化报告：跨账号 · 按 SKU 交叉 · 调整操作复盘。

自包含 HTML（内联 ECharts），6 个页签：
  总结 / 商品ROI / 调整复盘 / 计划明细 / 搜索词诊断 / 行动清单
"""
from __future__ import annotations

import html as _html
import json
import sys
from pathlib import Path

from . import config

ASSETS = Path(__file__).resolve().parent / "assets" / "echarts.min.js"

CSS = """:root{
  --bg:#f5f7fa; --card:#fff; --ink:#111827; --sub:#6b7280; --line:#e5e7eb;
  --green:#16a34a; --greenbg:#ecfdf5; --amber:#d97706; --amberbg:#fffbeb;
  --red:#dc2626; --redbg:#fef2f2; --blue:#2563eb; --bluebg:#eff6ff; --purple:#7c3aed;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1560px;margin:0 auto;padding:0 20px 60px}
header.top{background:linear-gradient(120deg,#0f172a,#1e3a8a);color:#fff;padding:26px 0 20px;margin-bottom:18px}
header.top .wrap{padding-bottom:0}
h1{margin:0 0 6px;font-size:24px;font-weight:700;letter-spacing:.5px}
.metaline{color:#c7d2fe;font-size:13px}
.tabs{display:flex;gap:6px;margin-top:16px;flex-wrap:wrap}
.tab{padding:9px 18px;border-radius:9px 9px 0 0;background:rgba(255,255,255,.12);color:#e0e7ff;
  cursor:pointer;font-weight:600;font-size:14px;border:1px solid transparent;border-bottom:none;user-select:none}
.tab:hover{background:rgba(255,255,255,.22)}
.tab.active{background:var(--bg);color:#1e3a8a}
section.page{display:none}
section.page.active{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px;
  box-shadow:0 1px 3px rgba(16,24,40,.04)}
.card h2{margin:0 0 4px;font-size:17px;font-weight:700;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card h2 .tagline{font-size:12px;font-weight:400;color:var(--sub)}
.card h3{margin:18px 0 8px;font-size:15px;font-weight:700}
.hint{color:var(--sub);font-size:12.5px;margin:2px 0 12px}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:1100px){.g3,.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:720px){.g2,.g3,.g4{grid-template-columns:1fr}}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .lbl{font-size:12.5px;color:var(--sub);margin-bottom:6px;display:flex;justify-content:space-between;gap:8px}
.kpi .val{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.kpi .sub{font-size:12px;color:var(--sub);margin-top:5px;font-variant-numeric:tabular-nums}
.up{color:var(--green)} .down{color:var(--red)} .flat{color:var(--sub)}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap}
.b-grow{background:var(--greenbg);color:var(--green)}
.b-opt{background:var(--amberbg);color:var(--amber)}
.b-stop{background:var(--redbg);color:var(--red)}
.b-bad{background:#fde8e8;color:#991b1b}
.b-idle{background:#f3f4f6;color:var(--sub)}
.b-ok{background:var(--greenbg);color:var(--green)}
.b-warn{background:var(--amberbg);color:var(--amber)}
.b-info{background:var(--bluebg);color:var(--blue)}
.b-vio{background:#f5f3ff;color:var(--purple)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
th,td{padding:8px 9px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}
th{background:#f9fafb;font-weight:700;color:#374151;position:sticky;top:0;z-index:2;
  cursor:pointer;white-space:nowrap;font-size:12.5px}
th.noSort{cursor:default}
th:hover{background:#f3f4f6}
td.l,th.l{text-align:left}
td.mono,th.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,Consolas,monospace}
tbody tr:hover{background:#f8fafc}
.tblwrap{overflow:auto;max-height:720px;border:1px solid var(--line);border-radius:10px}
.tblwrap.short{max-height:460px}
.prod{display:flex;align-items:center;gap:9px;min-width:250px}
.prod img{width:36px;height:36px;border-radius:7px;object-fit:cover;background:#f3f4f6;flex:none}
.prod .nm{font-weight:600;font-size:12.5px;line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.prod .sid{font-size:11px;color:var(--sub);font-family:ui-monospace,monospace}
.chart{width:100%;height:330px}
.chart.tall{height:430px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.chip{padding:6px 13px;border-radius:20px;border:1px solid var(--line);background:#fff;cursor:pointer;
  font-size:12.5px;font-weight:600;color:#374151}
.chip:hover{border-color:#94a3b8}
.chip.on{background:#1e3a8a;color:#fff;border-color:#1e3a8a}
.note{background:var(--bluebg);border-left:3px solid var(--blue);padding:10px 14px;border-radius:0 8px 8px 0;
  font-size:12.5px;color:#1e40af;margin:10px 0}
.warnbox{background:var(--amberbg);border-left:3px solid var(--amber);padding:10px 14px;border-radius:0 8px 8px 0;
  font-size:12.5px;color:#92400e;margin:10px 0}
.dangerbox{background:var(--redbg);border-left:3px solid var(--red);padding:10px 14px;border-radius:0 8px 8px 0;
  font-size:12.5px;color:#991b1b;margin:10px 0}
ul.sum{margin:6px 0 0;padding-left:20px}
ul.sum li{margin:5px 0}
.opDetail{font-size:11.5px;color:#475569;max-width:560px;word-break:break-word}
.evt{border-left:3px solid var(--line);padding:8px 0 8px 14px;position:relative;margin-left:6px}
.evt:before{content:"";position:absolute;left:-6.5px;top:14px;width:10px;height:10px;border-radius:50%;
  background:#cbd5e1;border:2px solid #fff}
.evt.ok{border-left-color:var(--green)} .evt.ok:before{background:var(--green)}
.evt.bad{border-left-color:var(--red)} .evt.bad:before{background:var(--red)}
.evt.warn{border-left-color:var(--amber)} .evt.warn:before{background:var(--amber)}
.evt .h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.evt .t{font-family:ui-monospace,monospace;font-size:12px;color:var(--sub);flex:none}
.evt .a{font-weight:700}
.mini{font-size:12px;color:var(--sub)}
.pill{display:inline-block;background:#f1f5f9;border-radius:6px;padding:1px 7px;font-size:11.5px;color:#475569;margin-right:4px}
.foot{color:var(--sub);font-size:12px;text-align:center;margin-top:26px;line-height:1.9}
a.lk{color:var(--blue);text-decoration:none}
a.lk:hover{text-decoration:underline}
"""

JS = """window.__CH=window.__CH||{};
function showTab(id, el){
  document.querySelectorAll('section.page').forEach(function(s){s.classList.remove('active');});
  document.getElementById(id).classList.add('active');
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  Object.keys(window.__CH||{}).forEach(function(k){try{window.__CH[k].resize();}catch(e){}});
  window.scrollTo({top:0,behavior:'smooth'});
}
function sortTable(th){
  var tb=th.closest('table');
  var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
  var asc=!(th.dataset.asc==='1'); th.dataset.asc=asc?'1':'0';
  var rows=Array.prototype.slice.call(tb.tBodies[0].rows);
  var num=th.dataset.num==='1';
  rows.sort(function(a,b){
    var ca=a.cells[idx], cb=b.cells[idx];
    var x=ca?(ca.dataset.v!==undefined?ca.dataset.v:ca.innerText):'';
    var y=cb?(cb.dataset.v!==undefined?cb.dataset.v:cb.innerText):'';
    if(num){var fx=parseFloat(x), fy=parseFloat(y); if(isNaN(fx))fx=-1e18; if(isNaN(fy))fy=-1e18; return asc?fx-fy:fy-fx;}
    return asc?String(x).localeCompare(String(y),'zh'):String(y).localeCompare(String(x),'zh');
  });
  rows.forEach(function(r){tb.tBodies[0].appendChild(r);});
}
function applyTier(btn){
  var val=btn.dataset.f; var card=btn.closest('.card');
  card.querySelectorAll('.chip').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.querySelectorAll('table tbody tr').forEach(function(tr){
    var t=tr.dataset.tier||'';
    tr.style.display=(val==='all'||t===val)?'':'none';
  });
}
function applyAcct(btn){
  var val=btn.dataset.a; var card=btn.closest('.card');
  card.querySelectorAll('.chip').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.querySelectorAll('table tbody tr').forEach(function(tr){
    var t=(tr.dataset.acct||'').split(',');
    tr.style.display=(val==='all'||t.indexOf(val)>=0)?'':'none';
  });
}
function toggleWords(btn){
  var d=btn.nextElementSibling;
  if(!d) return;
  d.style.display=(d.style.display==='none'||!d.style.display)?'block':'none';
  btn.textContent=d.style.display==='block'?'收起':'展开';
}
function initCharts(){
  window.__CH=window.__CH||{};
  if(typeof echarts==='undefined'){
    document.querySelectorAll('.chart').forEach(function(d){
      d.innerHTML='<div style="padding:20px;color:#6b7280">ECharts 未内联</div>';});
    return;
  }
  (window.__CHARTS||[]).forEach(function(c){
    var el=document.getElementById(c.id); if(!el) return;
    var ch=echarts.init(el); ch.setOption(c.option); window.__CH[c.id]=ch;
  });
  window.addEventListener('resize',function(){
    Object.keys(window.__CH).forEach(function(k){try{window.__CH[k].resize();}catch(e){}});});
}
window.addEventListener('DOMContentLoaded', initCharts);
"""


# ---------------------------------------------------------------- 基础工具
def _e(s) -> str:
    # 接口返回里含 &amp; / &ldquo; 等实体，先解码再转义，避免页面出现 &amp;amp;
    return _html.escape(_html.unescape(str("" if s is None else s)), quote=True)


def _n(v, d=0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _money(v, dec=0) -> str:
    return f"{_n(v):,.{dec}f}"


def _num(v, dec=2) -> str:
    return f"{_n(v):,.{dec}f}"


def _pct(v, dec=1) -> str:
    return "—" if v is None else f"{_n(v):.{dec}f}%"


def _delta(cur, pre, better_high=True, dec=1) -> tuple:
    cur, pre = _n(cur), _n(pre)
    if abs(pre) < 1e-9:
        return ("", "flat")
    d = (cur - pre) / abs(pre) * 100
    good = d > 0 if better_high else d < 0
    if abs(d) < 0.05:
        return (f"{d:+.{dec}f}%", "flat")
    return (f"{d:+.{dec}f}%", "up" if good else "down")


def _badge(tier) -> str:
    m = {"放大": "b-grow", "优化": "b-opt", "止损": "b-stop", "严重亏损": "b-bad",
         "未投放": "b-idle", "正确": "b-ok", "有问题": "b-bad", "有效但不彻底": "b-warn",
         "存疑": "b-warn", "效果不明显": "b-info", "已止损": "b-ok", "观察中": "b-info",
         "待观察": "b-idle", "无法评估": "b-idle", "无基线": "b-idle"}
    return f'<span class="badge {m.get(tier, "b-idle")}">{_e(tier)}</span>'


def _roi_cls(roi, be) -> str:
    roi, be = _n(roi), _n(be, 99)
    if roi <= 0:
        return "flat"
    if roi >= 4:
        return "up"
    if roi >= be:
        return ""
    return "down"


def _latest(s) -> dict:
    w2, w1 = s["wins"]["w2"], s["wins"]["w1"]
    return w2 if _n(w2["cost"]) > 0 else w1


def _latest_key(s) -> str:
    return "w2" if _n(s["wins"]["w2"]["cost"]) > 0 else "w1"


def _spark(vals, be) -> str:
    mx = max([_n(v) for v in vals] + [1e-9])
    out = []
    for v in vals:
        h = max(int(_n(v) / mx * 24), 2)
        cls = "hi" if _n(v) >= 4 else ("lo" if _n(v) < _n(be, 99) else "")
        out.append(f'<i class="{cls}" style="height:{h}px" title="ROI {_n(v):.2f}"></i>')
    return '<div class="bars">' + "".join(out) + "</div>"


def _sku_advice(s) -> str:
    be = _n(s.get("breakeven"), 3.27)
    cur = _latest(s)
    roi, cost = _n(cur["roi"]), _n(cur["cost"])
    tier = s["verdict"]["tier"]
    if tier == "放大":
        return f"ROI {roi:.2f} 明显高于保本 {be:.2f}：日预算 +20~30%，核心词出价 +10~20%，当作增量池"
    if tier == "优化":
        return f"ROI {roi:.2f} 刚过保本 {be:.2f}：先抠掉 0 单词与低效词，再谈加预算"
    if tier == "止损":
        return f"ROI {roi:.2f} < 保本 {be:.2f}：广告在亏（花 ¥{_money(cost)}），但自然成交兜住整体，降预算+否定废词，别直接关"
    if tier == "严重亏损":
        return f"ROI {roi:.2f} < 保本 {be:.2f} 且扣掉广告费为负：优先降预算 50% 或暂停，预算让给高 ROI 商品"
    return "本周无广告投放"


def _plan_advice(p) -> str:
    be = _n(p.get("breakeven"), 3.27)
    w0, w1, w2 = p["wins"]["w0"], p["wins"]["w1"], p["wins"]["w2"]
    cur = w2 if _n(w2["cost"]) > 0 else w1
    roi, cost = _n(cur["roi"]), _n(cur["cost"])
    t = p["tier"]
    if t == "放大":
        return f"ROI {roi:.2f} ≥ 4：加预算 20~30%，可复制到同款 SKU"
    if t == "优化":
        return f"ROI {roi:.2f} 在保本 {be:.2f} 之上：抠废词、压 CPC，目标提到 {max(roi + 0.4, be + 0.3):.1f}"
    if t == "止损":
        d = f"ROI {roi:.2f} < 保本 {be:.2f}，花费 ¥{_money(cost)}：先降到保本以上再加量"
        if _n(w1["cost"]) > 0 and _n(w0["cost"]) > 0:
            d += "；花费 " + _money(w0["cost"]) + " → " + _money(w1["cost"]) + " → " + _money(w2["cost"])
        return d
    return "本周无花费"

# ---------------------------------------------------------------- 图表
_PALETTE = ["#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#db2777", "#65a30d"]


def _win_labels(d):
    return [w["label"].replace(" ", "\n") for w in d["meta"]["windows"]]


def build_charts(d):
    comb = d["advice"]["combined"]
    wkeys = [w["key"] for w in d["meta"]["windows"]]
    cats = _win_labels(d)
    charts = []

    # 1) 合计：花费/成交 + ROI
    charts.append({"id": "chTrend", "option": {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["花费", "成交", "ROI"], "top": 0},
        "grid": {"left": 70, "right": 60, "top": 44, "bottom": 52},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"lineHeight": 15}},
        "yAxis": [
            {"type": "value", "name": "金额(元)"},
            {"type": "value", "name": "ROI", "splitLine": {"show": False}}],
        "series": [
            {"name": "花费", "type": "bar", "data": [round(_n(comb[k]["cost"]), 0) for k in wkeys],
             "itemStyle": {"color": "#f59e0b", "borderRadius": [4, 4, 0, 0]}, "barMaxWidth": 46,
             "label": {"show": True, "position": "top", "fontSize": 11}},
            {"name": "成交", "type": "bar", "data": [round(_n(comb[k]["amt"]), 0) for k in wkeys],
             "itemStyle": {"color": "#2563eb", "borderRadius": [4, 4, 0, 0]}, "barMaxWidth": 46,
             "label": {"show": True, "position": "top", "fontSize": 11}},
            {"name": "ROI", "type": "line", "yAxisIndex": 1, "smooth": True, "symbolSize": 10,
             "data": [round(_n(comb[k]["roi"]), 2) for k in wkeys],
             "itemStyle": {"color": "#16a34a"}, "lineStyle": {"width": 3},
             "label": {"show": True, "fontSize": 12, "fontWeight": "bold"}}]}})

    # 2) 分账号 花费 & ROI
    accts = d["accounts"]
    series = []
    for i, a in enumerate(accts):
        series.append({"name": a["label"] + " 花费", "type": "bar", "barMaxWidth": 34,
                       "itemStyle": {"color": _PALETTE[i * 2 % len(_PALETTE)], "borderRadius": [4, 4, 0, 0]},
                       "data": [round(_n(a["totals"][k]["cost"]), 0) for k in wkeys]})
    for i, a in enumerate(accts):
        series.append({"name": a["label"] + " ROI", "type": "line", "yAxisIndex": 1, "smooth": True,
                       "symbolSize": 9, "itemStyle": {"color": _PALETTE[(i * 2 + 1) % len(_PALETTE)]},
                       "lineStyle": {"width": 3, "type": "dashed"},
                       "data": [round(_n(a["totals"][k]["roi"]), 2) for k in wkeys]})
    charts.append({"id": "chAcct", "option": {
        "tooltip": {"trigger": "axis"},
        "legend": {"top": 0, "type": "scroll"},
        "grid": {"left": 70, "right": 60, "top": 52, "bottom": 52},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"lineHeight": 15}},
        "yAxis": [{"type": "value", "name": "花费(元)"},
                  {"type": "value", "name": "ROI", "splitLine": {"show": False}}],
        "series": series}})

    # 3) SKU：实际 ROI vs 保本 ROI（花费前 15）
    skus = [s for s in d["skus"] if _n(_latest(s)["cost"]) > 0][:15]
    names = [(s["name"] or s["skuId"])[:16] for s in skus][::-1]
    roi = [round(_n(_latest(s)["roi"]), 2) for s in skus][::-1]
    be = [round(_n(s["breakeven"]), 2) for s in skus][::-1]
    colors = ["#16a34a" if _n(_latest(s)["roi"]) >= _n(s["breakeven"]) else "#dc2626" for s in skus][::-1]
    charts.append({"id": "chSkuRoi", "option": {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["实际ROI", "保本ROI"], "top": 0},
        "grid": {"left": 130, "right": 50, "top": 40, "bottom": 30},
        "xAxis": {"type": "value", "name": "ROI"},
        "yAxis": {"type": "category", "data": names, "axisLabel": {"fontSize": 11}},
        "series": [
            {"name": "实际ROI", "type": "bar", "data": [{"value": v, "itemStyle": {"color": c}}
                                                        for v, c in zip(roi, colors)],
             "barMaxWidth": 16, "label": {"show": True, "position": "right", "fontSize": 11}},
            {"name": "保本ROI", "type": "bar", "data": be, "barMaxWidth": 16,
             "itemStyle": {"color": "#cbd5e1"},
             "label": {"show": True, "position": "right", "fontSize": 11}}]}})

    # 4) 广告渗透率
    pen = [s for s in d["skus"] if s["wins"]["w2"].get("adShare") or s["wins"]["w1"].get("adShare")][:15]
    pnames = [(s["name"] or s["skuId"])[:16] for s in pen][::-1]
    pval = [round(_n(_latest(s).get("adShare")), 1) for s in pen][::-1]
    charts.append({"id": "chShare", "option": {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 130, "right": 60, "top": 24, "bottom": 30},
        "xAxis": {"type": "value", "name": "广告成交/店铺成交 %", "max": 100},
        "yAxis": {"type": "category", "data": pnames, "axisLabel": {"fontSize": 11}},
        "series": [{"type": "bar", "data": pval, "barMaxWidth": 16,
                    "itemStyle": {"color": "#7c3aed", "borderRadius": [0, 4, 4, 0]},
                    "label": {"show": True, "position": "right", "fontSize": 11, "formatter": "{c}%"},
                    "markLine": {"silent": True, "symbol": "none",
                                 "lineStyle": {"color": "#dc2626", "type": "dashed"},
                                 "data": [{"xAxis": 100, "label": {"formatter": "广告=自然"}}]}}]}})

    # 5) 人工调整前后 ROI
    _seen_plan = set()
    _adj_all = [a for a in d["adjustments"]
                if a.get("before") and a.get("after") and _n(a["before"]["cost"]) > 1]
    adj = []
    for a in sorted(_adj_all, key=lambda x: x["time"], reverse=True):
        key = (a["account"], a["plan"])
        if key in _seen_plan:
            continue
        _seen_plan.add(key)
        adj.append(a)
    adj = sorted(adj, key=lambda x: -(x["after"]["roi"] - x["before"]["roi"]))[:18]
    if adj:
        an = [(a["plan"] or "")[:18] for a in adj][::-1]
        b = [round(_n(a["before"]["roi"]), 2) for a in adj][::-1]
        aft = [round(_n(a["after"]["roi"]), 2) for a in adj][::-1]
        charts.append({"id": "chAdj", "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"data": ["调整前7天ROI", "调整后7天ROI"], "top": 0},
            "grid": {"left": 140, "right": 50, "top": 40, "bottom": 30},
            "xAxis": {"type": "value", "name": "ROI"},
            "yAxis": {"type": "category", "data": an, "axisLabel": {"fontSize": 11}},
            "series": [
                {"name": "调整前7天ROI", "type": "bar", "data": b, "barMaxWidth": 13,
                 "itemStyle": {"color": "#94a3b8"}, "label": {"show": True, "position": "right", "fontSize": 10}},
                {"name": "调整后7天ROI", "type": "bar", "data": aft, "barMaxWidth": 13,
                 "itemStyle": {"color": "#2563eb"}, "label": {"show": True, "position": "right", "fontSize": 10}}]}})

    # 6) 生意大盘：商智访客 / 广告点击 / 订单
    sv, sc, so = [], [], []
    for k in wkeys:
        tot_v = 0
        for a in accts:
            f = a["totals"][k].get("flow") or {}
            tot_v += _n(f.get("visitors"))
        sv.append(int(tot_v))
        sc.append(int(sum(_n(a["totals"][k]["clk"]) for a in accts)))
        so.append(int(sum(_n(a["totals"][k]["ord"]) for a in accts)))
    charts.append({"id": "chFlow", "option": {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["店铺访客", "广告点击", "广告订单"], "top": 0},
        "grid": {"left": 70, "right": 30, "top": 44, "bottom": 52},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"lineHeight": 15}},
        "yAxis": {"type": "value", "name": "数量"},
        "series": [
            {"name": "店铺访客", "type": "bar", "data": sv, "barMaxWidth": 44,
             "itemStyle": {"color": "#0891b2", "borderRadius": [4, 4, 0, 0]}},
            {"name": "广告点击", "type": "bar", "data": sc, "barMaxWidth": 44,
             "itemStyle": {"color": "#f59e0b", "borderRadius": [4, 4, 0, 0]}},
            {"name": "广告订单", "type": "bar", "data": so, "barMaxWidth": 44,
             "itemStyle": {"color": "#16a34a", "borderRadius": [4, 4, 0, 0]}}]}})
    return charts


def _chart_html(cid, tall=False):
    return f'<div class="chart{" tall" if tall else ""}" id="{cid}"></div>'


def _kpi(label, main, sub="", cls="") -> str:
    return (f'<div class="kpi"><div class="lbl">{label}</div>'
            f'<div class="val {cls}">{main}</div><div class="sub">{sub}</div></div>')


# ---------------------------------------------------------------- 页面：总览
def _seq(vals, fmt=lambda v: f"{_n(v):,.0f}") -> str:
    return " → ".join(fmt(v) for v in vals)


def page_overview(d) -> str:
    comb = d["advice"]["combined"]
    wkeys = [w["key"] for w in d["meta"]["windows"]]
    cur = wkeys[-1] if _n(comb[wkeys[-1]]["cost"]) > 0 else wkeys[-2]
    pre = wkeys[0]
    c = comb[cur]
    adv = d["advice"]
    manual = [o for o in d["ops"] if o["manual"]]
    right = [a for a in d["adjustments"] if a["verdict"] == "正确"]
    wrong = [a for a in d["adjustments"] if a["verdict"] == "有问题"]

    dc, dccls = _delta(c["cost"], comb[pre]["cost"], better_high=False)
    da, dacls = _delta(c["amt"], comb[pre]["amt"])
    dr, drcls = _delta(c["roi"], comb[pre]["roi"])

    kpis = "".join([
        _kpi("两账号广告花费（" + d["meta"]["windows"][-1]["label"][:6] + "）",
             "¥" + _money(c["cost"]),
             f'<span class="{dccls}">{dc}</span> vs 调整前 ¥{_money(comb[pre]["cost"])}'),
        _kpi("广告成交额", "¥" + _money(c["amt"]),
             f'<span class="{dacls}">{da}</span> vs 调整前 ¥{_money(comb[pre]["amt"])}'),
        _kpi("整体投产比 ROI", f'{_n(c["roi"]):.2f}',
             f'<span class="{drcls}">{dr}</span> ｜ 全店保本线 {_n(d["meta"]["breakevenFlat"]):.2f}',
             drcls),
        _kpi("店铺总成交（商智）", "¥" + _money(c.get("szAmt")),
             f'广告贡献 {_pct(c.get("adShare"))} ｜ 自然成交 ¥{_money(max(_n(c.get("szAmt")) - _n(c["amt"]), 0))}'),
        _kpi("广告订单 / CPA", f'{int(_n(c["ord"])):,} 单',
             f'单均获客成本 ¥{_n(c["cpa"]):.2f} ｜ 点击 {int(_n(c["clk"])):,} ｜ CPC ¥{_n(c["cpc"]):.2f}'),
        _kpi("人工调整 / 系统自动改预算",
             f'{len(manual)} / {len([o for o in d["ops"] if o["kind"] == "autoBudget"])} 条',
             f'可对比前后效果 {len(right) + len(wrong) + len([o for o in d["adjustments"] if o["verdict"] in ("效果不明显", "存疑", "已止损")])} 条：'
             f'正确 {len(right)} ｜ 有问题 {len(wrong)}'),
    ])

    acct_rows = []
    for a in d["accounts"]:
        t = a["totals"]
        cells = f'<td class="l"><b>{_e(a["label"])}</b><div class="mini">ID {_e(a["accountId"])}</div></td>'
        for k in wkeys:
            m = t[k]
            cls = _roi_cls(m["roi"], d["meta"]["breakevenFlat"])
            cells += (f'<td class="mono">{_money(m["cost"])}</td>'
                      f'<td class="mono">{_money(m["amt"])}</td>'
                      f'<td class="mono {cls}"><b>{_n(m["roi"]):.2f}</b></td>'
                      f'<td class="mono">{_pct(m.get("adShare"))}</td>')
        acct_rows.append("<tr>" + cells + "</tr>")

    ths = '<th class="l noSort">账号</th>'
    for w in d["meta"]["windows"]:
        ths += f'<th class="mono noSort" colspan="4">{_e(w["label"])}</th>'

    sz_notes = []
    for a in d["accounts"]:
        f = a["totals"][wkeys[-1]].get("flow") or a["totals"][wkeys[-2]].get("flow") or {}
        if f:
            sz_notes.append(f'{_e(a["label"])} 店铺访客 {int(_n(f.get("visitors"))):,}、'
                            f'浏览量 {int(_n(f.get("views"))):,}、转化率 {_n(f.get("cvr")):.2f}%')

    return f"""
<div class="card">
  <h2>核心结论 <span class="tagline">两账号合并口径</span></h2>
  <ul class="sum">{''.join(f"<li>{_e(s)}</li>" for s in adv["summary"])}</ul>
</div>

<div class="grid g3">{kpis}</div>

<div class="card" style="margin-top:16px">
  <h2>三窗口趋势 <span class="tagline">调整前 / 调整后一周 / 最近一周</span></h2>
  <p class="hint">金额单位：元。ROI 走右轴。{"；".join(_e(x) for x in sz_notes)}</p>
  {_chart_html("chTrend")}
</div>

<div class="card">
  <h2>分账号表现</h2>
  <p class="hint">主账号以「全站智能推广」为主、账号B以「智能化/关键词」为主，两者打法不同，需分开看。</p>
  {_chart_html("chAcct")}
  <div class="tblwrap short" style="margin-top:14px">
    <table><thead><tr>{ths}</tr></thead><tbody>{''.join(acct_rows)}</tbody></table>
  </div>
</div>

<div class="card">
  <h2>流量与转化大盘</h2>
  <p class="hint">店铺访客来自商智；广告点击/订单来自京准通。若访客远大于广告点击，说明自然流量占比高。</p>
  {_chart_html("chFlow")}
</div>

<div class="card">
  <h2>口径与成本假设 <span class="tagline">重要：这些是推算值</span></h2>
  <div class="note">
    <b>数据口径</b>：广告=京准通概览（点击15天/成交订单）；智能投放=智能投放报表；商智=各店铺成交口径。
    搜索词报表按「花费」降序取前 2000 行，覆盖约 99.9% 花费。
  </div>
  <div class="warnbox">
    <b>成本假设</b>：Excel「计算公式」只给了<b>全店一套</b>成本 —— 客单价 ¥{_n(d["meta"]["costModel"]["refPrice"])}、
    产品成本 ¥{_n(d["meta"]["costModel"]["product"])}、京东扣点 ¥{_n(d["meta"]["costModel"]["platform"])}、
    包装 ¥{_n(d["meta"]["costModel"]["package"])}、运费 ¥{_n(d["meta"]["costModel"]["shipping"])}、
    退货率 {_n(d["meta"]["costModel"]["returnRate"]) * 100:.0f}%，算得毛利率 {(1 - 0) and (1 / _n(d["meta"]["breakevenFlat"], 1) * 100):.1f}%、
    <b>保本 ROI = {_n(d["meta"]["breakevenFlat"]):.2f}</b>。<br>
    所以本报告的「保本」对所有商品都是同一条 {_n(d["meta"]["breakevenFlat"]):.2f} 线；
    「毛利/单」= 该商品<b>真实客单价</b> × 全店毛利率；
    「预估净利」= 商智成交额（缺失时用广告成交额兜底）× 全店毛利率 − 广告花费。<br>
    <b>如果各商品真实毛利率差异大（生鲜尤其如此），请提供每个 SKU 的供货价/运费，我可以替换成本模型重算 —— 那会让「哪些商品其实在亏」这一结论变得确定。</b>
  </div>
  <div class="note">
    <b>毛利率敏感性</b>：保本 ROI = 1 ÷ 毛利率。毛利率 20% → 保本 5.00；25% → 4.00；30% → 3.33；35% → 2.86。
    当前按 {_n(d["meta"]["breakevenFlat"]):.2f} 判定，若你的实际毛利率更低，下面标「放大」的商品里会有一部分其实只是打平。
  </div>
  <p class="hint">生成时间 {_e(d["meta"]["generatedAt"])} ｜ 页签顺序即为建议的阅读顺序</p>
</div>
"""


# ---------------------------------------------------------------- 页面：商品 ROI 交叉表
def _sku_row(i, s, labels) -> str:
    cur = _latest(s)
    wk = _latest_key(s)
    roi, be = _n(cur["roi"]), _n(s["breakeven"])
    gap = roi - be
    accts = ",".join(s.get("adAccounts") or [])
    pills = "".join(f'<span class="pill">{_e(labels.get(x, x))}</span>' for x in (s.get("adAccounts") or []))
    img = (f'<img src="{_e(s["img"])}" loading="lazy" alt="" onerror="this.style.visibility=\'hidden\'">'
           if s.get("img") else '<img alt="" style="visibility:hidden">')
    shops = "／".join(f'{_e(k)} ¥{_money(v)}' for k, v in (s.get("shops") or {}).items())
    spark = _spark([s["wins"][k]["roi"] for k in ("w0", "w1", "w2")], be)
    tier = s["verdict"]["tier"]
    return (
        f'<tr data-tier="{_e(tier)}" data-acct="{_e(accts)}">'
        f'<td class="mono">{i}</td>'
        f'<td class="l"><div class="prod">{img}<div><div class="nm" title="{_e(s["name"])}｜{_e(shops)}">'
        f'{_e(s["name"])}</div><div class="sid">{_e(s["skuId"])}</div></div></div></td>'
        f'<td class="l">{pills or "—"}</td>'
        f'<td class="mono" data-v="{_n(cur["cost"])}">{_money(cur["cost"])}</td>'
        f'<td class="mono" data-v="{_n(cur["amt"])}">{_money(cur["amt"])}</td>'
        f'<td class="mono {_roi_cls(roi, be)}" data-v="{roi}"><b>{roi:.2f}</b></td>'
        f'<td class="mono" data-v="{be}">{be:.2f}</td>'
        f'<td class="mono {"up" if gap >= 0 else "down"}" data-v="{gap}">{gap:+.2f}</td>'
        f'<td class="mono" data-v="{_n(cur["ord"])}">{int(_n(cur["ord"]))}</td>'
        f'<td class="mono" data-v="{_n(cur["cpa"])}">{_num(cur["cpa"], 2)}</td>'
        f'<td class="mono" data-v="{_n(s["aov"])}">{_money(s["aov"], 1)}</td>'
        f'<td class="mono" data-v="{_n(s["grossPerOrder"])}">{_num(s["grossPerOrder"], 1)}</td>'
        f'<td class="mono" data-v="{_n(cur.get("adShare"))}">{_pct(cur.get("adShare"))}</td>'
        f'<td class="mono" data-v="{_n(cur.get("szAmt"))}">{_money(cur.get("szAmt"))}</td>'
        f'<td class="mono" data-v="{_n(cur.get("naturalAmt"))}">{_money(cur.get("naturalAmt"))}</td>'
        f'<td class="mono {"up" if _n(s["est"][wk]["netProfit"]) > 0 else "down"}" '
        f'data-v="{_n(s["est"][wk]["netProfit"])}">{_money(s["est"][wk]["netProfit"])}</td>'
        f'<td data-v="{roi}">{spark}</td>'
        f'<td class="l" title="{_e(_sku_advice(s))}">{_badge(tier)}</td>'
        f'</tr>')


def page_sku(d) -> str:
    labels = {a["key"]: a["label"] for a in d["accounts"]}
    skus = [s for s in d["skus"] if _n(s["wins"]["w0"]["cost"]) + _n(s["wins"]["w1"]["cost"]) + _n(s["wins"]["w2"]["cost"]) > 0]
    rows = [_sku_row(i + 1, s, labels) for i, s in enumerate(skus)]
    tot_cost = sum(_n(_latest(s)["cost"]) for s in skus)
    tot_amt = sum(_n(_latest(s)["amt"]) for s in skus)
    tot_sz = sum(_n(_latest(s).get("szAmt")) for s in skus)
    tot_net = sum(_n(s["est"][_latest_key(s)]["netProfit"]) for s in skus)
    tier_count = {}
    for s in skus:
        tier_count[s["verdict"]["tier"]] = tier_count.get(s["verdict"]["tier"], 0) + 1
    chips = "".join(
        f'<div class="chip{" on" if k == "all" else ""}" data-f="{k}" onclick="applyTier(this)">{v}</div>'
        for k, v in [("all", f"全部 {len(skus)}")] +
        [(t, f"{t} {tier_count.get(t, 0)}") for t in ["放大", "优化", "止损", "严重亏损"] if tier_count.get(t)])
    achips = "".join(
        f'<div class="chip{" on" if k == "all" else ""}" data-a="{k}" onclick="applyAcct(this)">{v}</div>'
        for k, v in [("all", "全部账号")] + [(a["key"], a["label"]) for a in d["accounts"]])
    return f"""
<div class="card">
  <h2>商品 ROI 交叉表 <span class="tagline">按 SKU 合并两个账号的广告，商智成交分店展示</span></h2>
  <p class="hint">
    共 {len(skus)} 个有广告投放的 SKU。近周花费合计 <b>¥{_money(tot_cost)}</b>、广告成交 <b>¥{_money(tot_amt)}</b>
    （ROI {_n(tot_amt) / max(tot_cost, 1):.2f}），这些商品商智总成交 <b>¥{_money(tot_sz)}</b>，
    按成本假设估算净利 <b>¥{_money(tot_net)}</b>。
    「保本」采用 Excel 算出的全店保本线 {_n(d["meta"]["breakevenFlat"]):.2f}；「毛利/单」= 该商品真实客单价 × 全店毛利率。
  </p>
  <div class="toolbar" id="skuChips">{chips}<span style="width:14px"></span>{achips}
    <span class="mini" style="margin-left:8px">点表头排序 · 鼠标悬停商品名看分店成交</span>
  </div>
  <div class="tblwrap">
    <table id="skuTable"><thead><tr>
      <th class="noSort">#</th>
      <th class="l noSort">商品</th>
      <th class="l noSort">投放账号</th>
      <th>近周花费</th><th>成交额</th><th>ROI</th><th>保本</th><th>ROI-保本</th>
      <th>订单</th><th>CPA</th><th>客单价</th><th>毛利/单</th>
      <th>广告占比</th><th>商智总成交</th><th>自然成交</th><th>预估净利</th>
      <th data-num="1">近3周ROI</th><th class="l">档位</th>
    </tr></thead><tbody>{''.join(rows)}</tbody></table>
  </div>
</div>

<div class="card">
  <h2>ROI vs 保本线 <span class="tagline">花费前 15 的 SKU</span></h2>
  <p class="hint">绿色=实际 ROI 达到该商品自己的保本线；红色=低于保本线，广告纯亏。</p>
  {_chart_html("chSkuRoi", tall=True)}
</div>

<div class="card">
  <h2>广告渗透率 <span class="tagline">广告成交 ÷ 该商品店铺总成交</span></h2>
  <p class="hint">越低说明越靠自然流量；接近 100% 说明这个商品的成交基本靠广告买来，一旦停投会掉量。</p>
  {_chart_html("chShare", tall=True)}
</div>
"""


# ---------------------------------------------------------------- 页面：调整复盘
def _adj_block(a) -> str:
    cls = {"正确": "ok", "已止损": "ok", "有问题": "bad", "有效但不彻底": "warn", "存疑": "warn"}.get(a["verdict"], "")
    b, af = a.get("before"), a.get("after")
    perf = ""
    if b and af:
        dcost, _c1 = _delta(af["dailyCost"], b["dailyCost"], better_high=False)
        droi = af["roi"] - b["roi"]
        perf = (f'<div class="mini">调整前7天：日均花费 ¥{_num(b["dailyCost"], 1)} · ROI {b["roi"]:.2f} · 日单 {_num(b["dailyOrd"], 1)}'
                f' ｜ 调整后7天：日均花费 ¥{_num(af["dailyCost"], 1)}'
                f'<span class="{_c1}">({dcost})</span> · ROI <b>{af["roi"]:.2f}</b>'
                f'<span class="{"up" if droi >= 0 else "down"}">({droi:+.2f})</span></div>')
    return (f'<div class="evt {cls}"><div class="h"><span class="t">{_e(a["time"])}</span>'
            f'<span class="a">{_e(a["plan"])}</span>{_badge(a["verdict"])}'
            f'<span class="mini">{_e(a["accountLabel"])} · {_e(a["content"])} · {_e(a["operator"])}</span></div>'
            f'<div class="opDetail">{_e(a["detail"])}</div>{perf}'
            f'<div class="mini" style="color:#1e40af">{_e(a["note"])}</div></div>')


def page_adj(d) -> str:
    adjs = d["adjustments"]
    manual = [o for o in d["ops"] if o["manual"]]
    auto = [o for o in d["ops"] if o["kind"] == "autoBudget"]
    right = [a for a in adjs if a["verdict"] == "正确"]
    wrong = [a for a in adjs if a["verdict"] == "有问题"]
    mid = [a for a in adjs if a["verdict"] in ("有效但不彻底", "存疑", "效果不明显")]
    by_acct = {}
    for a in adjs:
        by_acct.setdefault(a["account"], []).append(a)

    _order = {"有问题": 0, "正确": 1, "有效但不彻底": 2, "存疑": 3, "效果不明显": 4, "已止损": 5,
              "待观察": 6, "观察中": 7, "无基线": 8, "无花费": 9, "无法评估": 10}
    blocks = ""
    for k, lst in by_acct.items():
        lst = sorted(lst, key=lambda a: (_order.get(a["verdict"], 99), a["time"]))
        blocks += f'<h3>{_e(lst[0]["accountLabel"])}（{len(lst)} 条调整记录）</h3>'
        blocks += "".join(_adj_block(a) for a in lst)

    # 系统自动改预算 Top
    auto_by_plan = {}
    for o in auto:
        e = auto_by_plan.setdefault(o.get("target") or "未知计划", {"n": 0, "last": "", "acct": o["accountLabel"], "detail": ""})
        e["n"] += 1
        if o["time"] > e["last"]:
            e["last"] = o["time"]
            e["detail"] = o["detail"]
    auto_rows = sorted(auto_by_plan.items(), key=lambda x: -x[1]["n"])[:15]
    auto_html = "".join(
        f'<tr><td class="l">{_e(k)}</td><td class="l">{_e(v["acct"])}</td>'
        f'<td class="mono" data-v="{v["n"]}">{v["n"]}</td>'
        f'<td class="mono">{_e(v["last"])}</td>'
        f'<td class="l opDetail">{_e(v["detail"])}</td></tr>' for k, v in auto_rows)

    return f"""
<div class="card">
  <h2>调整操作复盘 <span class="tagline">来自京准通「操作日志」，用日粒度数据对比前后 7 天</span></h2>
  <p class="hint">
    数据窗口内共 <b>{len(d["ops"])}</b> 条操作日志：人工操作 <b>{len(manual)}</b> 条、
    系统「自动提升预算」<b>{len(auto)}</b> 条。进入复盘时间线 <b>{len(adjs)}</b> 条，
    其中<b>能真正对比调整前后 7 天效果</b>的 {len(right) + len(wrong) + len(mid) + len([a for a in adjs if a["verdict"] == "已止损"])} 条 →
    <span class="up">正确 {len(right)}</span>、<span class="down">有问题 {len(wrong)}</span>、
    效果不明显/存疑 {len(mid)}、止损 {len([a for a in adjs if a["verdict"] == "已止损"])}；
    其余因新建计划 / 已删除 / 无花费，没有基线可比（报告里保留原始操作，供核对）。
  </p>
  <div class="warnbox">
    <b>最重要的发现</b>：账号B 的「自动提升预算」一直在自动加预算
    （如 入仓拇指玉米 反复 300→450→675、入仓品 200→300→450），
    人工几乎没在管预算上限。系统加预算是「因为花得出去」，不等于「因为赚得回来」——
    必须用 ROI 反过来约束它，否则预算会被低效流量吃掉。
  </div>
  {_chart_html("chAdj", tall=True)}
</div>

<div class="card">
  <h2>人工调整时间线 <span class="tagline">按账号分组，绿=正确 红=有问题 黄=存疑</span></h2>
  <p class="hint">
    「调整前7天 / 调整后7天」为该计划在操作当日的日粒度前后对比；新计划或无数据的调整标注为无法评估。
  </p>
  {blocks or '<p class="hint">窗口内没有可评估的人工调整。</p>'}
</div>

<div class="card">
  <h2>系统自动改预算 Top <span class="tagline">这些不是人做的，但直接影响花费</span></h2>
  <p class="hint">同一计划被系统反复加预算的次数。「自动提升预算」通常由「稳赚计划 / 智能出价」触发，建议改为固定预算或关闭自动提预算。</p>
  <div class="tblwrap short">
    <table><thead><tr><th class="l noSort">计划</th><th class="l noSort">账号</th>
      <th data-num="1">自动加预算次数</th><th class="mono noSort">最近一次</th><th class="l noSort">最近内容</th></tr></thead>
      <tbody>{auto_html}</tbody></table>
  </div>
</div>
"""


# ---------------------------------------------------------------- 页面：计划明细
def page_plan(d) -> str:
    plans = [p for p in d["plans"] if _n(p["wins"]["w0"]["cost"]) + _n(p["wins"]["w1"]["cost"]) + _n(p["wins"]["w2"]["cost"]) > 0]
    plans.sort(key=lambda p: -(_n(p["wins"]["w1"]["cost"]) + _n(p["wins"]["w2"]["cost"])))
    tier_count = {}
    for p in plans:
        tier_count[p["tier"]] = tier_count.get(p["tier"], 0) + 1
    rows = []
    for i, p in enumerate(plans):
        cells = (f'<td class="mono">{i + 1}</td>'
                 f'<td class="l"><b>{_e(p["name"])}</b><div class="sid">{_e(p["campaignId"])}</div></td>'
                 f'<td class="l">{_e(p["accountLabel"])}</td>'
                 f'<td class="l">{_e(p["typeLabel"])}</td>')
        for k in ("w0", "w1", "w2"):
            m = p["wins"][k]
            cells += (f'<td class="mono" data-v="{_n(m["cost"])}">{_money(m["cost"])}</td>'
                      f'<td class="mono {_roi_cls(m["roi"], p["breakeven"])}" data-v="{_n(m["roi"])}">{_n(m["roi"]):.2f}</td>')
        droi = _n(p["wins"]["w2"]["roi"]) - _n(p["wins"]["w0"]["roi"])
        cells += (f'<td class="mono {"up" if droi >= 0 else "down"}" data-v="{droi}">{droi:+.2f}</td>'
                  f'<td data-v="{_n(p["wins"]["w2"]["roi"])}">{_spark([p["wins"][k]["roi"] for k in ("w0", "w1", "w2")], p["breakeven"])}</td>'
                  f'<td class="l" title="{_e(_plan_advice(p))}">{_badge(p["tier"])}</td></tr>')
        rows.append(f'<tr data-acct="{_e(p["account"])}" data-tier="{_e(p["tier"])}">' + cells)

    achips = "".join(f'<div class="chip{" on" if k == "all" else ""}" data-a="{k}" onclick="applyAcct(this)">{v}</div>'
                     for k, v in [("all", f"全部 {len(plans)}")] + [(a["key"], a["label"]) for a in d["accounts"]])
    return f"""
<div class="card">
  <h2>计划明细 <span class="tagline">概览口径，按账号区分</span></h2>
  <p class="hint">
    「概览」里的计划包含智能化 / 关键词 / 全站智能推广三种类型，口径与智能投放报表略有差异（订单口径）。
    放大 {tier_count.get("放大", 0)} 个 · 优化 {tier_count.get("优化", 0)} 个 ·
    止损 {tier_count.get("止损", 0)} 个 · 未投放 {tier_count.get("未投放", 0)} 个。
  </p>
  <div class="toolbar" id="planChips">{achips}<span class="mini" style="margin-left:8px">悬停档位看具体建议</span></div>
  <div class="tblwrap">
    <table><thead><tr><th class="noSort">#</th><th class="l noSort">计划</th><th class="l noSort">账号</th>
      <th class="l noSort">类型</th>
      <th class="mono noSort">w0花费</th><th class="mono noSort">w0ROI</th>
      <th class="mono noSort">w1花费</th><th class="mono noSort">w1ROI</th>
      <th class="mono noSort">w2花费</th><th class="mono noSort">w2ROI</th>
      <th data-num="1">ROI变化</th><th data-num="1">3周ROI</th><th class="l noSort">档位</th>
    </tr></thead><tbody>{''.join(rows)}</tbody></table>
  </div>
</div>
"""


# ---------------------------------------------------------------- 页面：搜索词诊断
def _word_pills(items, kind) -> str:
    if not items:
        return '<span class="mini">无</span>'
    out = []
    for w in items[:40]:
        cls = "b-grow" if kind == "good" else ("b-stop" if kind == "waste" else "b-opt")
        out.append(f'<span class="badge {cls}" style="margin:2px 4px 2px 0;font-weight:600" '
                   f'title="花费 ¥{_money(w["cost"])} · 成交 ¥{_money(w["amt"])} · {w["ord"]}单 · ROI {_n(w["roi"]):.2f}">'
                   f'{_e(w["word"])} <span style="opacity:.75">¥{_money(w["cost"])}/{_n(w["roi"]):.1f}</span></span>')
    return "".join(out)


def page_word(d) -> str:
    pmap = {}
    out = []
    for a in d["accounts"]:
        k = a["key"]
        wins = d["words"].get(k) or {}
        wk = "w2" if wins.get("w2") else ("w1" if wins.get("w1") else "w0")
        per = wins.get(wk) or {}
        agg = {"good": [], "waste": [], "low": []}
        blocks = []
        for cid, v in sorted(per.items(), key=lambda x: -sum(w["cost"] for w in x[1]["sw"]))[:14]:
            sw = v.get("sw") or []
            if not sw:
                continue
            g = [w for w in sw if w["kind"] == "good"][:12]
            wa = [w for w in sw if w["kind"] == "waste"][:12]
            lo = [w for w in sw if w["kind"] == "low"][:8]
            agg["good"] += [w for w in sw if w["kind"] == "good"]
            agg["waste"] += [w for w in sw if w["kind"] == "waste"]
            agg["low"] += [w for w in sw if w["kind"] == "low"]
            cost = sum(w["cost"] for w in sw)
            roi = sum(w["amt"] for w in sw) / cost if cost else 0
            blocks.append(f"""
<div style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;flex-wrap:wrap">
    <b>{_e(cid)}</b>
    <span class="mini">搜索词下：花费 ¥{_money(cost)} · ROI {roi:.2f} · 高效 {len(g)} / 废词 {len(wa)} / 低效 {len(lo)}</span>
  </div>
  <div style="margin-top:8px"><span class="mini">✅ 高效词（提价/加词）</span><br>{_word_pills(g, "good")}</div>
  <div style="margin-top:8px"><span class="mini">🚫 0单废词（直接否定）</span><br>{_word_pills(wa, "waste")}</div>
  <div style="margin-top:8px"><span class="mini">⚠️ 低效词（降价或精准否定）</span><br>{_word_pills(lo, "low")}</div>
</div>""")
        wa_cost = sum(w["cost"] for w in agg["waste"])
        wa_top = sorted(agg["waste"], key=lambda x: -x["cost"])[:8]
        out.append(f"""
<div class="card">
  <h2>{_e(a["label"])} · 搜索词诊断 <span class="tagline">{_e(wk)} 窗口，按花费排序前 14 个计划</span></h2>
  <p class="hint">
    高效词 {len(agg["good"])} 个 · 0单废词 {len(agg["waste"])} 个（浪费 ¥{_money(wa_cost)}）· 低效词 {len(agg["low"])} 个。
    判定标准：高效=订单≥2 且 ROI&gt;3.27；废词=花费≥3 且 0 单；低效=花费≥10 且 ROI&lt;3.27。
  </p>
  <div class="dangerbox"><b>最该否定的词</b>：{'、'.join(f'{_e(w["word"])}(¥{_money(w["cost"])})' for w in wa_top) or '无'}</div>
  {''.join(blocks) or '<p class="hint">该账号本窗口没有搜索词数据。</p>'}
</div>""")
    return "".join(out)


# ---------------------------------------------------------------- 页面：行动清单
def page_action(d) -> str:
    adv = d["advice"]
    right = [a for a in d["adjustments"] if a["verdict"] == "正确"]
    wrong = [a for a in d["adjustments"] if a["verdict"] == "有问题"]
    stop = adv["stop"]
    grow = adv["grow"]
    up = adv["upside"]

    stop_rows = "".join(
        f'<tr><td class="l"><b>{_e(s["name"])}</b></td><td class="mono" data-v="{_n(s["cost"])}">¥{_money(s["cost"])}</td>'
        f'<td class="mono down" data-v="{_n(s["roi"])}">{_n(s["roi"]):.2f}</td>'
        f'<td class="mono" data-v="{_n(s["breakeven"])}">{_n(s["breakeven"]):.2f}</td>'
        f'<td class="mono" data-v="{_n(s["netProfit"])}">¥{_money(s["netProfit"])}</td>'
        f'<td class="l">{_e(_sku_advice(next(x for x in d["skus"] if x["skuId"] == s["skuId"])))}</td></tr>'
        for s in stop)
    grow_rows = "".join(
        f'<tr><td class="l"><b>{_e(s["name"])}</b></td><td class="mono">¥{_money(s["cost"])}</td>'
        f'<td class="mono up">{_n(s["roi"]):.2f}</td>'
        f'<td class="mono">¥{_money(_n(s["cost"]) * 0.3)}</td>'
        f'<td class="l">{_e(_sku_advice(next(x for x in d["skus"] if x["skuId"] == s["skuId"])))}</td></tr>'
        for s in grow[:15])
    wrong_html = "".join(
        f'<div class="evt bad"><div class="h"><span class="t">{_e(a["time"])}</span><span class="a">{_e(a["plan"])}</span>'
        f'<span class="mini">{_e(a["accountLabel"])} · {_e(a["content"])}</span></div>'
        f'<div class="mini">{_e(a["note"])}</div></div>' for a in wrong)
    right_html = "".join(
        f'<div class="evt ok"><div class="h"><span class="t">{_e(a["time"])}</span><span class="a">{_e(a["plan"])}</span>'
        f'<span class="mini">{_e(a["accountLabel"])} · {_e(a["content"])}</span></div>'
        f'<div class="mini">{_e(a["note"])}</div></div>' for a in right)

    return f"""
<div class="card">
  <h2>第一步 · 止血 <span class="tagline">最近一周 ROI 低于保本线 {_n(d["meta"]["breakevenFlat"]):.2f} 的 SKU</span></h2>
  <p class="hint">
    合计花费 <b>¥{_money(up["stopCost"])}</b>，占最近一周总花费
    {_pct(_n(up["stopCost"]) / max(_n(up["lastCost"]), 1) * 100)}。逐个处理，不要一刀切关闭。
  </p>
  <div class="tblwrap short"><table><thead><tr>
    <th class="l noSort">商品</th><th data-num="1">近周花费</th><th data-num="1">ROI</th>
    <th data-num="1">保本ROI</th><th data-num="1">预估净利</th><th class="l noSort">动作</th>
  </tr></thead><tbody>{stop_rows or '<tr><td colspan="6" class="l">没有低于保本线的商品 👍</td></tr>'}</tbody></table></div>
</div>

<div class="card">
  <h2>第二步 · 加投 <span class="tagline">ROI ≥ 4 的 SKU，是增量池</span></h2>
  <p class="hint">按 30% 加预算试探，加完盯 3 天 ROI 是否守住。</p>
  <div class="tblwrap short"><table><thead><tr>
    <th class="l noSort">商品</th><th data-num="1">近周花费</th><th data-num="1">ROI</th>
    <th data-num="1">建议加投</th><th class="l noSort">动作</th>
  </tr></thead><tbody>{grow_rows or '<tr><td colspan="5" class="l">暂无明显高效商品</td></tr>'}</tbody></table></div>
</div>

<div class="card">
  <h2>第三步 · 量化收益 <span class="tagline">把低效花费挪到高效 SKU</span></h2>
  <div class="grid g4">
    {_kpi("可腾挪花费", "¥" + _money(up["stopCost"]), "来自低于保本线的 SKU")}
    {_kpi("目标 ROI", f'{_n(up["targetRoi"]):.2f}', "高效 SKU 的加权 ROI")}
    {_kpi("预计多产出", "¥" + _money(up["estGain"]), "同样花费下的增量成交")}
    {_kpi("整体 ROI", f'{_n(adv["combined"]["w2"]["roi"]):.2f} → {_n(up["roiAfter"]):.2f}', "调整后估算")}
  </div>
</div>

<div class="card">
  <h2>第四步 · 调整纠错 <span class="tagline">哪些调整做错了</span></h2>
  <p class="hint">这些调整执行后 7 天，计划 ROI 明显下滑。建议回滚到调整前的出价/投产比目标。</p>
  {wrong_html or '<p class="hint">窗口内没有判定为「有问题」的人工调整。</p>'}
  <h3>做对了的（保持）</h3>
  {right_html or '<p class="hint">—</p>'}
</div>

<div class="card">
  <h2>第五步 · 盯住系统自动加预算</h2>
  <div class="warnbox">
    账号B 的「自动提升预算」在窗口内触发了几十次，把日预算从 200/300 一路抬到 450/675。
    这类加预算由系统按「花得出去」触发，<b>不代表赚得回来</b>。
    建议：对 ROI 已低于保本的计划，<b>关闭自动提升预算 / 稳赚计划</b>，或改成固定日预算。
    操作路径：京准通 → 推广计划 → 计划 → 编辑 → 预算设置。
  </div>
</div>
"""


# ---------------------------------------------------------------- 组装
TABS = [("p-ov", "① 总结"), ("p-sku", "② 商品ROI"), ("p-adj", "③ 调整复盘"),
        ("p-plan", "④ 计划明细"), ("p-word", "⑤ 搜索词诊断"), ("p-act", "⑥ 行动清单")]


def render(d) -> str:
    charts = build_charts(d)
    chart_json = json.dumps(charts, ensure_ascii=False).replace("</", "<\\/")
    echarts_src = ""
    if ASSETS.exists():
        ec = ASSETS.read_text(encoding="utf-8").replace("</script", "<\\/script")
        echarts_src = "<script>" + ec + "</script>"
    tabs = "".join(
        f'<div class="tab{" active" if i == 0 else ""}" onclick="showTab(\'{cid}\',this)">{_e(label)}</div>'
        for i, (cid, label) in enumerate(TABS))
    pages = [
        ("p-ov", page_overview(d)),
        ("p-sku", page_sku(d)),
        ("p-adj", page_adj(d)),
        ("p-plan", page_plan(d)),
        ("p-word", page_word(d)),
        ("p-act", page_action(d)),
    ]
    body = "".join(
        f'<section class="page{" active" if i == 0 else ""}" id="{cid}">{html}</section>'
        for i, (cid, html) in enumerate(pages))
    meta = d["meta"]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>京东广告运营分析 · 两账号按SKU交叉 + 调整复盘</title>
<style>{CSS}</style>{echarts_src}</head>
<body>
<header class="top"><div class="wrap">
  <h1>京东广告运营分析 · 跨账号 × 按商品 × 调整复盘</h1>
  <div class="metaline">
    账号：{' / '.join(_e(a["label"]) for a in meta["accounts"])}
    ｜ 窗口：{_e(meta["windows"][0]["label"])} → {_e(meta["windows"][1]["label"])} → {_e(meta["windows"][2]["label"])}
    ｜ 生成 {_e(meta["generatedAt"])}
  </div>
  <div class="tabs">{tabs}</div>
</div></header>
<div class="wrap">{body}
  <div class="foot">
    数据来源：京准通（概览 / 智能投放 / 快车关键词 / 操作日志）+ 商智（商品明细 / 流量概况）。<br>
    「保本ROI」「预估净利」为按 Excel 成本结构推算，非真实财务数据；其余指标均为接口真实返回值。<br>
    操作日志覆盖 2026-08-15 ~ 2026-10-05，按 7 天分段抓取合并（接口对超长区间会静默截断）。
  </div>
</div>
<script>{JS}</script>
<script>window.__CHARTS={chart_json};</script>
</body></html>"""


def main() -> int:
    src = config.BASE_DIR / "data" / "analysis2.json"
    if not src.exists():
        print("[ERR] 未找到 data/analysis2.json，请先运行 python -m jd_roi.analyze2")
        return 1
    d = json.loads(src.read_text(encoding="utf-8"))
    html = render(d)
    out = config.BASE_DIR / "ad_roi_report_v2.html"
    out.write_text(html, encoding="utf-8")
    print(f"[OK] 报告已生成 -> {out}  ({len(html) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
