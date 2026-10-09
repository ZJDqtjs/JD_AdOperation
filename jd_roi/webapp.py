# -*- coding: utf-8 -*-
"""Web 控制台：任意区间分析与报表。

启动：
    uvicorn jd_roi.webapp:app --host 0.0.0.0 --port 8000

页面：
    /            控制台（区间选择、数据覆盖、立即抓取、扫码登录）
    /report      任意区间的聚合分析报表（自带区间选择器）
    /login       扫码登录

API：
    GET  /api/status            运行状态 / 登录状态 / 最近一次任务
    GET  /api/coverage          按天数据覆盖情况
    GET  /api/analysis          区间分析 JSON
    GET  /api/run/status        抓取进度 + 抓取锁状态
    POST /api/run               触发抓取（后台线程）
    POST /api/run/cancel        取消当前抓取 / 解锁卡死的抓取锁
    GET  /api/tasks             任务中心：运行中 + 历史任务
    GET  /api/tasks/{id}        单个任务详情（含完整日志）
    POST /api/tasks/{id}/cancel 取消该任务（仅运行中的可取消）
    POST /api/tasks/{id}/rerun  按原参数重跑
    POST /api/tasks/clear       清空历史任务
    POST /api/login/start       开始扫码登录
    GET  /api/login/qr          登录二维码 PNG
    GET  /api/login/status      登录状态
    POST /api/login/cancel      取消登录
"""
from __future__ import annotations

import datetime as dt
import html
import io
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import analyze2, dailystore as ds, report2, scrape_day, settings

settings.ensure_dirs()
app = FastAPI(title="京东广告运营分析服务", docs_url="/api/docs", redoc_url=None)

KINDS_UI = [("jzt_campaign", "概览计划"), ("jst_campaign", "投放计划"),
            ("jst_sku", "投放商品"), ("jst_searchword", "搜索词"),
            ("kw", "关键词"), ("sz_product", "商智商品"),
            ("sz_flow", "商智流量"), ("oplog", "操作日志")]


# ================================================================ 工具
def _e(s) -> str:
    return html.escape(str("" if s is None else s), quote=True)


def _today() -> dt.date:
    return dt.datetime.now().date()


def _yesterday() -> dt.date:
    return _today() - dt.timedelta(days=1)


def resolve_range(start: str | None, end: str | None, days: int | None) -> tuple:
    """把 (start,end,days) 归一成具体的 [start,end]。默认最近 7 天（到昨天）。"""
    if start and end:
        return ds.norm_day(start), ds.norm_day(end)
    n = int(days) if days else 7
    n = max(n, 1)
    e = _yesterday()
    s = e - dt.timedelta(days=n - 1)
    return s.isoformat(), e.isoformat()


def account_list(accounts) -> list:
    """把账号筛选参数归一成账号列表。

    复选框同名提交会变成 `?accounts=main&accounts=b`（多值），
    而 FastAPI 对 `str` 类型只取最后一个值、Starlette 的 `query_params.get` 也是取最后一个，
    结果就是「两个都勾选，只有最后一个生效」。所以这里统一按列表接收，
    同时兼容 `?accounts=main,b` 这种逗号串。空/无效值 = 全部账号。

    注意：以函数方式直接调用接口（测试、脚本）时，FastAPI 的 `Query(None)` 默认值
    会原样传进来，它不是字符串也不是列表 —— 这种情况按「未筛选」处理。
    """
    if isinstance(accounts, str):
        raw = [accounts]
    elif isinstance(accounts, (list, tuple, set)):
        raw = list(accounts)
    else:
        return settings.ACCOUNTS
    keys = set()
    for item in raw:
        keys.update(x.strip() for x in str(item).split(",") if x.strip())
    if not keys:
        return settings.ACCOUNTS
    got = [a for a in settings.ACCOUNTS if a["key"] in keys]
    return got or settings.ACCOUNTS


def _range_bar(start: str, end: str, keys: list, action: str = "/report",
               show_accounts: bool = True, extra: str = "") -> str:
    """区间选择器（控制台与报表共用）。"""
    n = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    presets = [("昨天", 1), ("近3天", 3), ("近7天", 7), ("近14天", 14), ("近30天", 30)]
    pre_html = "".join(
        f'<button type="button" class="pre" onclick="setRange({d})">{_e(lab)}</button>'
        for lab, d in presets)
    acc_html = ""
    if show_accounts:
        boxes = "".join(
            f'<label><input type="checkbox" name="accounts" value="{_e(a["key"])}"'
            f'{" checked" if a["key"] in keys else ""}>{_e(a["label"])}</label>'
            for a in settings.ACCOUNTS)
        acc_html = f'<span style="width:8px"></span>{boxes}'
    return f"""
<form class="rangebar" method="get" action="{_e(action)}" id="rangeForm">
  <label>起 <input type="date" name="start" id="rbStart" value="{_e(start)}"></label>
  <label>止 <input type="date" name="end" id="rbEnd" value="{_e(end)}"></label>
  <span class="mini" id="rbDays" style="color:#c7d2fe">共 {n} 天</span>
  {pre_html}{acc_html}
  <button type="submit">生成分析</button>
  {extra}
</form>
<script>
function setRange(n){{
  var e=new Date(rbEnd.value||"{_e(end)}");
  if(!rbEnd.value) e=new Date("{_e(end)}");
  var s=new Date(e.getTime()-(n-1)*86400000);
  rbStart.value=s.toISOString().slice(0,10);
  rbEnd.value=e.toISOString().slice(0,10);
  upd();
}}
function upd(){{
  var a=new Date(rbStart.value), b=new Date(rbEnd.value);
  if(isNaN(a)||isNaN(b)) return;
  document.getElementById('rbDays').textContent='共 '+(Math.round((b-a)/86400000)+1)+' 天';
}}
(function(){{var s=document.getElementById('rbStart'),e=document.getElementById('rbEnd');
  if(s&&e){{s.addEventListener('change',upd);e.addEventListener('change',upd);upd();}}}})();
</script>"""


# ================================================================ 控制台
DASH_CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#111827;--sub:#6b7280;--line:#e5e7eb;
 --green:#16a34a;--amber:#d97706;--red:#dc2626;--blue:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 60px}
header.top{background:linear-gradient(120deg,#0f172a,#1e3a8a);color:#fff;padding:26px 0 22px;margin-bottom:18px}
h1{margin:0 0 6px;font-size:23px}
.metaline{color:#c7d2fe;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px;
 box-shadow:0 1px 3px rgba(16,24,40,.05)}
.card h2{margin:0 0 6px;font-size:16px}
.hint{color:var(--sub);font-size:12.5px;margin:2px 0 12px}
.grid{display:grid;gap:14px}
.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:900px){.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .lbl{font-size:12.5px;color:var(--sub)}
.kpi .val{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:700}
.b-ok{background:#ecfdf5;color:var(--green)}.b-warn{background:#fffbeb;color:var(--amber)}
.b-bad{background:#fef2f2;color:var(--red)}.b-idle{background:#f3f4f6;color:var(--sub)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}
th{background:#f9fafb;font-weight:700;color:#374151;font-size:12.5px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.btn{display:inline-block;background:#1e3a8a;color:#fff;border:none;border-radius:9px;padding:9px 18px;
 font-weight:700;cursor:pointer;font-size:13px;text-decoration:none}
.btn:hover{background:#1e40af}
.btn.sec{background:#fff;color:#1e3a8a;border:1px solid #c7d2fe}
.btn.green{background:#16a34a}.btn.green:hover{background:#15803d}
.note{background:#eff6ff;border-left:3px solid var(--blue);padding:10px 14px;border-radius:0 8px 8px 0;
 font-size:12.5px;color:#1e40af;margin:10px 0}
.warn{background:#fffbeb;border-left:3px solid var(--amber);padding:10px 14px;border-radius:0 8px 8px 0;
 font-size:12.5px;color:#92400e;margin:10px 0}
.days{display:flex;flex-wrap:wrap;gap:3px;margin-top:6px}
.days i{width:15px;height:15px;border-radius:3px;background:#e5e7eb;display:block}
.days i.on{background:#22c55e}
code{background:#f1f5f9;padding:1px 6px;border-radius:5px;font-size:12px}
footer{color:var(--sub);font-size:12px;text-align:center;margin-top:24px;line-height:1.9}
a{color:var(--blue)}
.kpi.click{cursor:pointer;transition:box-shadow .15s,border-color .15s}
.kpi.click:hover{border-color:#93c5fd;box-shadow:0 2px 10px rgba(37,99,235,.12)}
.btn.mini{padding:4px 10px;font-size:12px;border-radius:7px}
.btn.danger{background:#dc2626}.btn.danger:hover{background:#b91c1c}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.modal{display:none;position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:50;
 padding:28px 14px;overflow:auto}
.modal.open{display:block}
.modal-card{max-width:1000px;margin:0 auto;background:#fff;border-radius:14px;
 box-shadow:0 20px 50px rgba(2,6,23,.35)}
.modal-head{display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--line)}
.modal-head h2{margin:0;font-size:17px;flex:1}
.modal-body{padding:16px 20px;max-height:76vh;overflow:auto}
.tasklog{background:#0f172a;color:#e2e8f0;border-radius:10px;padding:10px 12px;font-size:12px;
 line-height:1.7;max-height:190px;overflow:auto;white-space:pre-wrap;word-break:break-all;
 font-family:Consolas,Menlo,monospace;margin-top:8px}
.tm{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:#374151;margin:6px 0}
.tm b{font-weight:700}
.trow{cursor:pointer}
.trow:hover td{background:#f8fafc}
.trow.open td{background:#eff6ff}
tbody .det td{background:#f8fafc;padding:0 9px 12px}
"""


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    start, end = resolve_range(request.query_params.get("start"), request.query_params.get("end"), 7)
    cov = ds.coverage()
    st = ds.read_state()
    lr = st.get("lastRun") or {}
    run = scrape_day.runtime_status()
    keys = [a["key"] for a in settings.ACCOUNTS]
    lr_html = "尚未执行过抓取"
    if lr.get("start"):
        lr_html = f"{_e(lr.get('start'))} ~ {_e(lr.get('end'))}（完成于 {_e(st.get('lastRunAt') or '-')}）"

    rows = []
    for a in settings.ACCOUNTS:
        k = a["key"]
        c = cov.get(k) or {}
        d = c.get("days") or {}
        per = c.get("kinds") or {}
        cells = ""
        for kind, label in KINDS_UI:
            info = per.get(kind) or {}
            cnt = info.get("count", 0)
            last = (info.get("last") or "—")[5:]
            cls = "" if cnt else ' style="color:#c7c7c7"'
            cells += (f'<td class="n"{cls}><b>{cnt}</b>'
                      f'<div style="font-size:11px;color:#9ca3af">{_e(last)}</div></td>')
        rows.append(
            f'<tr><td><b>{_e(a.get("label"))}</b><div style="color:#6b7280;font-size:12px">'
            f'{_e(k)} · ID {_e(a.get("account_id"))}</div></td>'
            f'<td class="n">{d.get("count", 0)}</td>'
            f'<td class="n" style="font-size:12px">{_e(d.get("first") or "—")}<br>{_e(d.get("last") or "—")}</td>'
            f'{cells}</tr>')

    kinds_head = "".join(f'<th class="n">{_e(lab)}<div style="font-weight:400;font-size:11px">'
                         f'天/最近</div></th>' for _k, lab in KINDS_UI)

    # 覆盖日历（近 30 天）
    gap = scrape_day.catchup_range()
    gap_html = ("数据已是最新" if gap is None
                else f'库内最新 {_e(scrape_day.newest_day() or "无")}，'
                     f'下次将抓 {_e(gap[0])} ~ {_e(gap[1])}'
                     f'（{len(ds.day_span(gap[0], gap[1]))} 天）')

    cal = ""
    for a in settings.ACCOUNTS:
        k = a["key"]
        have = set(ds.days_for(k, "jzt_campaign"))
        cells = ""
        for i in range(29, -1, -1):
            day = (_today() - dt.timedelta(days=i)).isoformat()
            on = "on" if day in have else ""
            cells += f'<i class="{on}" title="{day}"></i>'
        cal += (f'<div style="margin:8px 0"><b>{_e(a.get("label"))}</b>'
                f'<span class="hint" style="margin-left:8px">近30天（绿=已入库）</span>'
                f'<div class="days">{cells}</div></div>')

    login_state = _login.snapshot()
    login_html = {
        "idle": '<span class="badge b-idle">未开始</span>',
        "starting": '<span class="badge b-warn">启动中…</span>',
        "waiting": '<span class="badge b-warn">等待登录</span>',
        "need_verify": '<span class="badge b-warn">待人工验证</span>',
        "logged_in": '<span class="badge b-ok">已登录</span>',
        "timeout": '<span class="badge b-bad">超时</span>',
        "error": f'<span class="badge b-bad">失败</span>',
    }.get(login_state.get("state"), '<span class="badge b-idle">—</span>')

    busy = "（正在抓取：%s）" % _e(run.get("progress") or "") if run.get("active") else ""
    lock = run.get("lock") or {}
    if run.get("active"):
        task_html = '<span class="badge b-warn">运行中</span>'
    elif lock.get("stale"):
        task_html = '<span class="badge b-bad">锁卡死</span>'
    elif run.get("cancelled"):
        task_html = '<span class="badge b-warn">已取消</span>'
    else:
        task_html = '<span class="badge b-ok">空闲</span>'
    if lock.get("locked") and not run.get("active"):
        task_hint = ('抓取锁被占用（持有者 %s，开始于 %s），但没有存活的任务线程'
                     % (_e(lock.get("thread") or lock.get("name") or "未知"),
                        _e(lock.get("acquiredAt") or "-")))
    else:
        task_hint = _e(run.get("progress") or "")

    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>京东广告运营分析服务</title><style>{DASH_CSS}</style>
<style>{report2.UI_CSS}</style></head><body>
<header class="top"><div class="wrap">
  <h1>京东广告运营分析服务</h1>
  <div class="metaline">按天自动入库 · 任意区间聚合分析 · 每天 {settings.SCHEDULE_HOUR:02d}:{settings.SCHEDULE_MINUTE:02d}（±{settings.SCHEDULE_JITTER_MIN} 分钟浮动，{_e(settings.TZ)}）抓取前一天</div>
  {_range_bar(start, end, keys, "/report")}
</div></header>
<div class="wrap">

<div class="grid g4">
  <div class="kpi"><div class="lbl">最近一次自动抓取</div><div class="val" style="font-size:14px">{lr_html}</div></div>
  <div class="kpi click" onclick="openTasks()" title="点击查看任务中心：运行中与历史任务">
    <div class="lbl">当前任务</div><div class="val" style="font-size:16px">{task_html}</div>
    <div class="hint">{task_hint}{busy}</div>
    <div class="hint" style="color:#2563eb">点击查看任务中心 →</div></div>
  <div class="kpi"><div class="lbl">登录状态</div>
    <div class="val" style="font-size:16px" id="loginVal"><span class="badge b-idle">未检查</span></div>
    <div class="hint" id="loginHint">点击「检查登录」确认落盘登录态是否有效</div>
    <div class="hint" id="keepHint" style="color:#6b7280">保活：—</div>
    <div class="hint" style="margin-top:6px">
      <button class="btn sec mini" onclick="checkLogin()">检查登录</button>
      <button class="btn sec mini" onclick="keepNow()">立即续期</button>
      <a href="/login" style="margin-left:6px">扫码 / 账号密码登录 →</a>
    </div></div>
  <div class="kpi"><div class="lbl">抓取回刷窗口</div><div class="val" style="font-size:16px">最近 {settings.REFRESH_DAYS} 天</div>
    <div class="hint">每晚重刷以修正归因延迟</div></div>
</div>

<div class="card" style="margin-top:16px">
  <h2>数据覆盖</h2>
  <p class="hint">按天入库；<b>只要某天缺数据，区间分析就会按 0 计入</b>。缺哪几天可以用下方「立即抓取」补。</p>
  <div class="note">补数策略：<b>{gap_html}</b>。定时任务会把「停机期间的空洞」和「最近 {settings.REFRESH_DAYS} 天回刷」合并成一次抓取。</div>
  {cal}
  <div style="overflow:auto;margin-top:10px">
  <table><thead><tr><th>账号</th><th class="n">总天数</th><th class="n">区间</th>{kinds_head}</tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
  </div>
</div>

<div class="card">
  <h2>手动抓取 / 补数</h2>
  <p class="hint">选择要抓的日期（只抓缺失的天；勾选「强制」会覆盖重抓）。默认抓昨天。</p>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <label>从 <input type="date" id="runStart" value="{_e(start)}"></label>
    <label>到 <input type="date" id="runEnd" value="{_e(end)}"></label>
    <label><input type="checkbox" id="runForce"> 强制覆盖</label>
    <button class="btn green" onclick="doRun()">立即抓取</button>
    <button class="btn sec" onclick="quickRun(1)">补昨天</button>
    <button class="btn sec" onclick="quickRun(3)">补近3天</button>
    <button class="btn sec" onclick="doCancel()">取消 / 解锁</button>
    <span id="runMsg" class="hint"></span>
  </div>
  <div class="note">说明：京准通是「点击后 15 天归因」，昨天的数据在未来两周还会增长，所以每晚都会<b>回刷最近 {settings.REFRESH_DAYS} 天</b>。商智数据当天结算后不再变化。</div>
</div>

<div class="card">
  <h2>报表说明</h2>
  <p class="hint">
    顶部选择任意区间 → 「生成分析」。报表会以<b>所选区间</b>为最新一期，自动往前取两个等长区间做<b>环比</b>，
    并包含：商品(SKU)跨账号交叉、计划明细、<b>操作日志复盘</b>（每条调整对应前后 7 天效果）、搜索词诊断、行动清单。
  </p>
  <div class="grid g2">
    <div class="note">示例：选 <code>{_e((_yesterday()-dt.timedelta(days=2)).isoformat())}</code> ~ <code>{_e(_yesterday().isoformat())}</code>
      就是「近 3 天」聚合报告，对比前 3 天与前 6 天。</div>
    <div class="warn">区间越长，越早的日期受归因延迟影响越小；<b>看绝对效率用 7~14 天，看即时反应用 1~3 天</b>。</div>
  </div>
</div>

<footer>
  接口来源：京准通（概览/智能投放/关键词/搜索词/操作日志）+ 商智（商品明细/流量概况）<br>
  数据目录 <code>{_e(settings.DATA_DIR)}</code> ｜ 登录态 <code>{_e(settings.AUTH_DIR)}</code>
</footer>
</div>

<div class="modal" id="taskModal" onclick="if(event.target===this)closeTasks()">
  <div class="modal-card">
    <div class="modal-head">
      <h2>任务中心</h2>
      <span class="hint" id="taskMeta" style="margin:0"></span>
      <button class="btn sec mini" onclick="loadTasks()">刷新</button>
      <button class="btn sec mini" onclick="clearTasks()">清理历史</button>
      <button class="btn sec mini" onclick="closeTasks()">关闭</button>
    </div>
    <div class="modal-body">
      <h2 style="font-size:15px;margin:0 0 8px">运行中</h2>
      <div id="taskRunning"></div>
      <h2 style="font-size:15px;margin:22px 0 8px">历史任务</h2>
      <div style="overflow:auto"><table id="taskTable">
        <thead><tr><th>状态</th><th>触发</th><th>区间</th><th>账号</th>
          <th class="n">耗时</th><th>开始时间</th><th>操作</th></tr></thead>
        <tbody id="taskRows"><tr><td colspan="7" class="hint">加载中…</td></tr></tbody>
      </table></div>
    </div>
  </div>
</div>
<script>
function loginBadge(st){{
  if(st==='logged_in') return ['<span class="badge b-ok">已登录</span>','ok'];
  if(st==='logged_out') return ['<span class="badge b-bad">未登录</span>','bad'];
  if(st==='checking') return ['<span class="badge b-warn">检查中…</span>','warn'];
  if(st==='error') return ['<span class="badge b-bad">检查失败</span>','bad'];
  return ['<span class="badge b-idle">未检查</span>','idle'];
}}
function renderLogin(j){{
  var val=document.getElementById('loginVal'), hint=document.getElementById('loginHint');
  if(!val) return;
  var accs=j.accounts||[];
  var parts=accs.map(function(a){{
    var b=loginBadge(a.state)[0];
    return (accs.length>1 ? '<span class="mini" style="color:#64748b">'+(a.label||a.key)+' </span>' : '')+b;
  }});
  var overall='idle';
  if(accs.some(function(a){{return a.state==='checking';}})) overall='checking';
  else if(accs.length && accs.every(function(a){{return a.state==='logged_in';}})) overall='logged_in';
  else if(accs.some(function(a){{return a.state==='logged_out'||a.state==='error';}})) overall='logged_out';
  if(!accs.length){{ val.innerHTML=loginBadge(j.checkState==='checking'?'checking':'idle')[0]; }}
  else {{ val.innerHTML=parts.join('　'); }}
  hint.textContent = j.message || (j.checkState==='checking' ? '正在检查登录态…' : '点击「检查登录」确认落盘登录态是否有效');
}}
function loadLogin(){{
  fetch('/api/login/accounts').then(r=>r.json()).then(function(j){{
    renderLogin(j);
    if(j.checkState==='checking') setTimeout(loadLogin, 2500);
  }}).catch(function(e){{}});
}}
function checkLogin(){{
  var hint=document.getElementById('loginHint');
  if(hint) hint.textContent='正在检查登录态…（需打开浏览器，约 10~30 秒）';
  fetch('/api/login/check', {{method:'POST'}}).then(r=>r.json()).then(function(j){{
    if(!j.ok && hint) hint.textContent = j.error||'检查未能开始';
    setTimeout(loadLogin, 1500);
  }}).catch(function(e){{ if(hint) hint.textContent='请求失败 '+e; }});
}}
function fmtAgo(ts){{
  if(!ts) return '—';
  var s=Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(s<60) return s+' 秒前';
  if(s<3600) return Math.floor(s/60)+' 分钟前';
  return Math.floor(s/3600)+' 小时前';
}}
function renderKeep(j){{
  var el=document.getElementById('keepHint');
  if(!el) return;
  if(!j.enabled){{ el.textContent='保活：已关闭（JD_KEEPALIVE=0）'; return; }}
  var mins=Math.round((j.intervalSec||0)/60);
  var st={{running:'运行中', stopped:'已停止', error:'异常'}}[j.state]||j.state;
  el.textContent='保活：'+st+' · 每 '+mins+' 分钟自动续期 · 上次 '+fmtAgo(j.lastAt)
    + (j.count? ' · 已续 '+j.count+' 次':'');
}}
function loadKeep(){{
  fetch('/api/login/keepalive').then(r=>r.json()).then(renderKeep).catch(function(e){{}});
}}
function keepNow(){{
  var el=document.getElementById('keepHint');
  if(el) el.textContent='正在续期…（需打开浏览器，约 10~40 秒）';
  fetch('/api/login/keepalive/now', {{method:'POST'}}).then(r=>r.json()).then(function(j){{
    renderKeep(j); loadLogin();
  }}).catch(function(e){{ if(el) el.textContent='续期失败 '+e; }});
}}
loadLogin();
loadKeep();
setInterval(loadKeep, 30000);
function doRun(force, s, e){{
  var qs = new URLSearchParams();
  qs.set('start', s || document.getElementById('runStart').value);
  qs.set('end', e || document.getElementById('runEnd').value);
  if(force === true || document.getElementById('runForce').checked) qs.set('force','1');
  var msg = document.getElementById('runMsg');
  msg.textContent = '已提交，正在后台抓取…';
  fetch('/api/run?'+qs.toString(), {{method:'POST'}}).then(r=>r.json()).then(function(j){{
    msg.textContent = j.ok ? '已开始抓取：'+j.start+' ~ '+j.end : ('失败：'+(j.error||''));
    if(j.ok) setTimeout(poll, 2000);
  }}).catch(function(e){{ msg.textContent='请求失败 '+e; }});
}}
function quickRun(n){{
  var e=new Date('{_e(_yesterday().isoformat())}');
  var s=new Date(e.getTime()-(n-1)*86400000);
  document.getElementById('runStart').value=s.toISOString().slice(0,10);
  document.getElementById('runEnd').value=e.toISOString().slice(0,10);
  doRun(false, s.toISOString().slice(0,10), e.toISOString().slice(0,10));
}}
function doCancel(){{
  var msg = document.getElementById('runMsg');
  msg.textContent = '正在取消…';
  fetch('/api/run/cancel', {{method:'POST'}}).then(r=>r.json()).then(function(j){{
    if(!j.ok){{ msg.textContent = '取消失败：'+(j.error||''); return; }}
    if(j.cancelRequested){{
      msg.textContent = '已请求取消，任务将在当前这一天结束后停止'
        + (j.lockReleased ? '（并已解锁卡死的锁）' : '');
    }} else if(j.lockReleased){{
      msg.textContent = '已解锁卡死的抓取锁，现在可以重新抓取';
    }} else {{
      msg.textContent = '当前没有抓取任务';
    }}
    poll();
  }}).catch(function(e){{ msg.textContent='请求失败 '+e; }});
}}
function poll(){{
  fetch('/api/run/status').then(r=>r.json()).then(function(j){{
    var msg=document.getElementById('runMsg');
    if(j.active){{
      msg.textContent = '抓取中：'+(j.progress||'')
        + (j.cancelRequested ? '（正在取消…）' : '');
      setTimeout(poll, 3000);
      return;
    }}
    if(j.error){{ msg.textContent='失败：'+j.error; return; }}
    if(j.lock && j.lock.stale){{
      msg.textContent='抓取锁卡死（无存活任务），点「取消 / 解锁」即可恢复';
      return;
    }}
    msg.textContent = '已完成 '+(j.finishedAt||'')+(j.cancelled ? '（已取消）' : '');
  }}).catch(function(e){{
    var msg=document.getElementById('runMsg'); msg.textContent='状态查询失败 '+e;
  }});
}}
(function(){{ fetch('/api/run/status').then(r=>r.json()).then(function(j){{
  if(j.active || (j.lock && (j.lock.stale || j.lock.locked))) poll();
}}); }})();

/* ------------------------------------------------ 任务中心 */
var TASK_TIMER = null, TASK_OPEN = [];
var T_STATUS = {{running:['运行中','b-warn'], done:['成功','b-ok'],
                 failed:['失败','b-bad'], cancelled:['已取消','b-idle']}};
var T_TRIGGER = {{manual:'手动', schedule:'定时', rerun:'重跑', cli:'命令行'}};

function esc(s){{ return (s===null||s===undefined) ? '' : String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}

function fmtDur(sec){{
  if(sec===null||sec===undefined) return '—';
  var s=Math.max(0,Math.round(sec));
  if(s<60) return s+' 秒';
  var m=Math.floor(s/60);
  if(m<60) return m+' 分 '+(s%60)+' 秒';
  return Math.floor(m/60)+' 时 '+(m%60)+' 分';
}}

function durOf(t){{
  // 耗时一律用服务端算好的秒数：前端拿 startedAt 自己相减会受时区影响
  if(t.elapsedSec===null || t.elapsedSec===undefined) return '—';
  return fmtDur(t.elapsedSec) + (t.status==='running' ? '（进行中）' : '');
}}

function openTasks(){{
  document.getElementById('taskModal').classList.add('open');
  TASK_OPEN = [];
  loadTasks();
  if(TASK_TIMER) clearInterval(TASK_TIMER);
  TASK_TIMER = setInterval(loadTasks, 4000);
}}
function closeTasks(){{
  document.getElementById('taskModal').classList.remove('open');
  if(TASK_TIMER){{ clearInterval(TASK_TIMER); TASK_TIMER=null; }}
}}

function loadTasks(){{
  fetch('/api/tasks').then(r=>r.json()).then(function(j){{
    if(!j.ok) return;
    renderTasks(j);
  }}).catch(function(e){{
    document.getElementById('taskMeta').textContent = '加载失败 '+e;
  }});
}}

function renderTasks(j){{
  document.getElementById('taskMeta').textContent = '共 '+j.count+' 条历史';
  var box = document.getElementById('taskRunning');
  var cur = j.running;
  if(cur){{
    var cancelling = j.cancelRequested ? ' <span class="badge b-warn">正在取消…</span>' : '';
    box.innerHTML =
      '<div class="card" style="margin:0;border-color:#fcd34d;background:#fffbeb">'
      + '<div class="val" style="font-size:15px"><span class="badge b-warn">运行中</span>'
      + cancelling + ' <b>'+esc(cur.name)+'</b></div>'
      + '<div class="tm"><span>区间 <b>'+esc(cur.start)+' ~ '+esc(cur.end)+'</b></span>'
      + '<span>账号 <b>'+esc((cur.accounts||[]).join(', '))+'</b></span>'
      + '<span>触发 <b>'+esc(T_TRIGGER[cur.trigger]||cur.trigger||'')+'</b></span>'
      + '<span>'+(cur.force?'强制覆盖':'只补缺失')+'</span>'
      + '<span>已运行 <b>'+durOf(cur)+'</b></span>'
      + '<span>开始 <b>'+esc(cur.startedAt)+'</b></span></div>'
      + '<div class="tm"><span>'+esc(cur.progress||'')+'</span></div>'
      + '<button class="btn danger mini" onclick="taskCancel(\\'' + cur.id + '\\')">取消该任务</button>'
      + ' <button class="btn sec mini" onclick="doCancel()">强制解锁（锁卡死时用）</button>'
      + '<pre class="tasklog">'+esc((cur.log||[]).join('\\n') || '（暂无日志）')+'</pre>'
      + '</div>';
  }} else {{
    var lock = j.lock || {{}};
    box.innerHTML = lock.stale
      ? '<div class="warn">没有运行中的任务，但抓取锁被占用（无存活线程）。'
        + '点下方按钮解锁后即可重新抓取。<div style="margin-top:8px">'
        + '<button class="btn danger mini" onclick="doCancel()">解锁</button></div></div>'
      : '<div class="hint">当前没有运行中的任务。</div>';
  }}

  var rows = j.items || [];
  if(!rows.length){{
    document.getElementById('taskRows').innerHTML =
      '<tr><td colspan="7" class="hint">暂无历史任务</td></tr>';
    return;
  }}
  var html = '';
  for(var i=0;i<rows.length;i++){{
    var t = rows[i];
    var st = T_STATUS[t.status] || [t.status,'b-idle'];
    var open = TASK_OPEN.indexOf(t.id) >= 0;
    html += '<tr class="trow'+(open?' open':'')+'" onclick="toggleDetail(\\'' + t.id + '\\')">'
      + '<td><span class="badge '+st[1]+'">'+esc(st[0])+'</span></td>'
      + '<td>'+esc(T_TRIGGER[t.trigger]||t.trigger||'')+'</td>'
      + '<td>'+esc(t.start)+' ~ '+esc(t.end)+'<div class="hint" style="margin:0">'
      + esc(t.days)+' 天</div></td>'
      + '<td>'+esc((t.accounts||[]).join(', '))+'</td>'
      + '<td class="n">'+durOf(t)+'</td>'
      + '<td>'+esc(t.startedAt)+'</td>'
      + '<td onclick="event.stopPropagation()">'
      + '<button class="btn sec mini" onclick="toggleDetail(\\'' + t.id + '\\')">'
      + (open?'收起':'详情')+'</button> '
      + '<button class="btn sec mini" onclick="taskRerun(\\'' + t.id + '\\')">重跑</button></td></tr>';
    if(open) html += '<tr class="det"><td colspan="7" id="det-'+t.id+'">加载详情…</td></tr>';
  }}
  document.getElementById('taskRows').innerHTML = html;
  for(var k=0;k<TASK_OPEN.length;k++) loadDetail(TASK_OPEN[k]);
}}

function toggleDetail(id){{
  var i = TASK_OPEN.indexOf(id);
  if(i>=0) TASK_OPEN.splice(i,1); else TASK_OPEN.push(id);
  loadTasks();
}}

function loadDetail(id){{
  fetch('/api/tasks/'+encodeURIComponent(id)).then(r=>r.json()).then(function(j){{
    var td = document.getElementById('det-'+id);
    if(!td) return;
    if(!j.ok){{ td.textContent = j.error || '任务不存在'; return; }}
    var t = j.task;
    var errs = [];
    var accs = t.summary && t.summary.accounts || {{}};
    for(var k in accs){{ (accs[k].errors||[]).forEach(function(e){{ errs.push(k+': '+e); }}); }}
    var head = '<div class="tm"><span>任务 ID <b>'+esc(t.id)+'</b></span>'
      + '<span>完成 <b>'+esc(t.finishedAt||'—')+'</b></span>'
      + '<span>耗时 <b>'+durOf(t)+'</b></span>'
      + '<span>日志 <b>'+(t.logCount||0)+'</b> 行</span></div>';
    if(t.error) head += '<div class="warn" style="margin:6px 0">失败原因：'+esc(t.error)+'</div>';
    if(errs.length) head += '<div class="warn" style="margin:6px 0">抓取错误 '
      + errs.length+' 条：<br>'+esc(errs.slice(0,10).join('\\n'))+'</div>';
    head += '<pre class="tasklog">'+esc((t.log||[]).join('\\n')||'（无日志）')+'</pre>';
    td.innerHTML = head;
  }});
}}

function taskCancel(id){{
  if(!confirm('确认取消该抓取任务？')) return;
  fetch('/api/tasks/'+encodeURIComponent(id)+'/cancel', {{method:'POST'}})
    .then(r=>r.json()).then(function(j){{
      alert(j.ok ? (j.message||'已请求取消') : ('取消失败：'+(j.error||'')));
      loadTasks();
    }}).catch(function(e){{ alert('请求失败 '+e); }});
}}

function taskRerun(id){{
  if(!confirm('按原参数重新发起一次抓取？')) return;
  fetch('/api/tasks/'+encodeURIComponent(id)+'/rerun', {{method:'POST'}})
    .then(r=>r.json()).then(function(j){{
      if(j.ok){{ alert('已重新发起：'+j.start+' ~ '+j.end); loadTasks(); }}
      else alert('重跑失败：'+(j.error||''));
    }}).catch(function(e){{ alert('请求失败 '+e); }});
}}

function clearTasks(){{
  if(!confirm('清空已结束的历史任务？（正在运行的任务会保留）')) return;
  fetch('/api/tasks/clear', {{method:'POST'}}).then(r=>r.json()).then(function(j){{
    TASK_OPEN = [];
    loadTasks();
  }});
}}
</script>
</body></html>""")


# ================================================================ 报表
@app.get("/report", response_class=HTMLResponse)
def report(request: Request):
    q = request.query_params
    start, end = resolve_range(q.get("start"), q.get("end"), 7)
    accounts = account_list(q.getlist("accounts"))
    try:
        data = analyze2.build(start=start, end=end, accounts=accounts)
    except FileNotFoundError as exc:
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>缺少数据</title>
<style>{DASH_CSS}</style></head><body><div class="wrap" style="padding-top:40px">
<div class="card"><h2>该区间没有数据</h2>
<div class="warn">{_e(exc)}</div>
<p class="hint">区间 {_e(start)} ~ {_e(end)} 内没有任何一天的京准通数据。</p>
<p><a class="btn" href="/">← 回到控制台</a>
<a class="btn sec" href="/report?start={_e(start)}&end={_e(end)}&days=">重试</a></p></div></div></body></html>""",
            status_code=200)
    keys = [a["key"] for a in accounts]
    ui = _range_bar(start, end, keys, "/report",
                    extra='<a href="/">← 控制台</a>'
                          '<a href="/api/analysis?start=%s&end=%s&accounts=%s" target="_blank">看 JSON</a>'
                          % (_e(start), _e(end), _e(",".join(keys))))
    title = f"广告运营分析 {start} ~ {end}（{(dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1}天）"
    return HTMLResponse(report2.render(data, ui=ui, title=title))


# ================================================================ API
@app.get("/api/status")
def api_status():
    cached = settings._costs_cache_read() or {}
    return {"ok": True,
            "server": dt.datetime.now().isoformat(timespec="seconds"),
            "tz": settings.TZ,
            "costs": {"apiConfigured": bool(settings.COSTS_URL),
                      "intendedSource": ("供货方接口" if settings.COSTS_URL else
                                         ("config/costs.json" if settings.COSTS_FILE.exists()
                                          else "全店 Excel 口径")),
                      "lastRunSource": (settings.COSTS_SOURCE or {}).get("kind") or "",
                      "skus": len(cached.get("skus") or {}),
                      "updatedAt": cached.get("updatedAt") or "",
                      "ageSec": (None if not cached.get("_fetched_at")
                                 else round(settings.time_ago(cached.get("_fetched_at"))))},
            "schedule": {"hour": settings.SCHEDULE_HOUR, "minute": settings.SCHEDULE_MINUTE,
                         "jitterMin": settings.SCHEDULE_JITTER_MIN,
                         "todayAt": _schedule_target()[0].strftime("%Y-%m-%d %H:%M"),
                         "todayOffset": _schedule_target()[1],
                         "refreshDays": settings.REFRESH_DAYS},
            "accounts": [{"key": a["key"], "label": a.get("label")} for a in settings.ACCOUNTS],
            "scrape": scrape_day.runtime_status(),
            "login": _login.snapshot(),
            "state": ds.read_state()}


@app.get("/api/coverage")
def api_coverage(accounts: list[str] | None = Query(None)):
    return {"ok": True, "coverage": ds.coverage(account_list(accounts))}


@app.get("/api/analysis")
def api_analysis(start: str | None = None, end: str | None = None,
                 days: int | None = None, accounts: list[str] | None = Query(None),
                 save: int = 0):
    s, e = resolve_range(start, end, days)
    try:
        data = analyze2.build(start=s, end=e, accounts=account_list(accounts))
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    if save:
        out = Path(settings.DATA_DIR) / "analysis" / f"{s}_{e}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "range": {"start": s, "end": e}, "data": data}


@app.get("/api/run/status")
def api_run_status():
    return scrape_day.runtime_status()


@app.post("/api/run")
def api_run(start: str | None = None, end: str | None = None,
            days: int | None = None, force: int = 0,
            accounts: list[str] | None = Query(None)):
    s, e = resolve_range(start, end, days)
    accs = account_list(accounts)
    res = scrape_day.run_background(accs, s, e, force=bool(force),
                                     log=_log_progress, name="scrape")
    if not res.get("ok"):
        return res
    return {"ok": True, "start": s, "end": e, "accounts": [a["key"] for a in accs],
            "force": bool(force)}


@app.post("/api/run/cancel")
def api_run_cancel(force: int = 0):
    """有任务 → 请求取消；锁被卡死（无存活持有者）→ 直接解锁。"""
    return {"ok": True, **scrape_day.cancel_running(force=bool(force))}


# ---------------------------------------------------------------- 任务中心
@app.post("/api/tasks/clear")
def api_tasks_clear():
    return {"ok": True, "removed": scrape_day.clear_history()}


@app.get("/api/tasks")
def api_tasks(limit: int = 50):
    return {"ok": True, **scrape_day.task_list(limit)}


@app.get("/api/tasks/{task_id}")
def api_task_detail(task_id: str):
    rec = scrape_day.get_task(task_id)
    if rec is None:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    return {"ok": True, "task": rec}


@app.post("/api/tasks/{task_id}/cancel")
def api_task_cancel(task_id: str):
    return scrape_day.cancel_task(task_id)


@app.post("/api/tasks/{task_id}/rerun")
def api_task_rerun(task_id: str, force: int | None = None):
    return scrape_day.rerun_task(task_id, force=None if force is None else bool(force))


def _log_progress(msg: str) -> None:
    scrape_day.append_log(msg)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# ================================================================ 扫码登录
class LoginSession:
    """Playwright 同步 API 必须在独立线程里跑，这里用一个后台线程 + 状态快照。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.state = "idle"
        self.account = None
        self.error = None
        self.qr_png = None
        self.qr_at = 0.0
        self.message = ""
        self.updated_at = 0.0
        # 登录态检查（落盘 Profile 的真实状态，与控制台展示 / 登录流程状态相互独立）
        self._check_thread = None
        self._check_stop = threading.Event()
        self.accounts_state = {}          # {accountKey: {state, message, at}}
        self.check_state = "idle"         # idle | checking | done | error
        self.check_message = ""
        self.check_updated_at = 0.0
        # 登录态保活（京准通的 sdtoken 只有 ~30 分钟，必须定时「用一下」才不被踢）
        self._keep_thread = None
        self._keep_stop = threading.Event()
        self.keep_state = "idle"          # idle | running | stopped | error
        self.keep_message = ""
        self.keep_last_at = 0.0           # 最近一次保活时间
        self.keep_last_ok = {}            # {accountKey: bool}
        self.keep_last_requests = {}      # {accountKey: [保活类请求 URL]} 用于确认模仿生效
        self.keep_count = 0               # 累计保活次数
        # Profile 占用锁：同一个 user_data_dir 不能被两个上下文同时打开
        # （登录/检查/保活/导出都会启动浏览器，必须串行，否则报 Profile 被占用）
        self._profile_lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "account": self.account, "error": self.error,
                    "hasQr": self.qr_png is not None, "qrAt": self.qr_at,
                    "message": self.message, "updatedAt": self.updated_at,
                    "qrUrl": "/api/login/qr"}

    def accounts_snapshot(self) -> dict:
        """落盘登录态的真实快照（按账号），供控制台展示。"""
        with self._lock:
            return {"checkState": self.check_state, "message": self.check_message,
                    "updatedAt": self.check_updated_at,
                    "accounts": {k: dict(v) for k, v in self.accounts_state.items()}}

    def _set_check(self, **kv):
        with self._lock:
            self.__dict__.update(kv)
            self.check_updated_at = time.time()

    def _set_account_state(self, key: str, state: str, message: str) -> None:
        with self._lock:
            self.accounts_state[key] = {"state": state, "message": message, "at": time.time()}

    def start_check(self, account: str | None = None, force: bool = False) -> bool:
        """检查落盘登录态是否仍然有效（真实请求京准通接口，不是只看内存状态）。"""
        with self._lock:
            if self.check_state == "checking" and not force:
                return False
        self._check_stop.clear()
        self._set_check(check_state="checking", check_message="正在检查登录态…")
        keys = [account] if account else [a["key"] for a in settings.ACCOUNTS]
        self._check_thread = threading.Thread(target=self._loop_check, args=(keys,),
                                              name="login-check", daemon=True)
        self._check_thread.start()
        return True

    def stop_check(self) -> None:
        self._check_stop.set()

    def _loop_check(self, keys: list) -> None:
        from . import config
        from .browser import has_login, launch_persistent, session_ok
        n_ok = 0
        try:
            for key in keys:
                if self._check_stop.is_set():
                    break
                self._set_account_state(key, "checking", "检查中…")
                # 串行化浏览器启动，避免与保活/登录抢同一个 Profile
                if not self._profile_lock.acquire(timeout=180):
                    self._set_account_state(key, "error", "浏览器占用中，请稍后重试")
                    continue
                pw = ctx = None
                try:
                    pw, ctx = launch_persistent(headless=True, account=key)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    try:
                        page.set_viewport_size({"width": 1440, "height": 900})
                    except Exception:  # noqa: BLE001
                        pass
                    # 先看有没有登录凭证，再让服务端确认是否真的没过期
                    if not has_login(ctx):
                        self._set_account_state(key, "logged_out", "未登录：Profile 里没有登录票据")
                        continue
                    page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    if session_ok(page):
                        self._set_account_state(key, "logged_in", "已登录，登录态有效")
                        n_ok += 1
                    else:
                        self._set_account_state(key, "logged_out", "登录态已失效，需要重新登录")
                except Exception as exc:  # noqa: BLE001
                    self._set_account_state(key, "error", f"检查失败：{type(exc).__name__}")
                finally:
                    try:
                        ctx and ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        pw and pw.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        self._profile_lock.release()
                    except Exception:  # noqa: BLE001
                        pass
            if self._check_stop.is_set():
                self._set_check(check_state="done", check_message="检查已取消")
            else:
                self._set_check(check_state="done",
                                check_message=f"检查完成：{n_ok}/{len(keys)} 个账号登录有效")
        except Exception as exc:  # noqa: BLE001
            self._set_check(check_state="error", check_message=f"检查出错：{type(exc).__name__}")

    # ---------------- 登录态保活（方案 A） ----------------
    # 京准通会话凭据 sdtoken 约 30 分钟失效。京麦客户端不掉线的做法就是「后台定时轮询」，
    # 不断向服务端发请求把会话续上；无人值守的后台进程同理，必须主动「用一下」。
    # 这里每 KEEPALIVE_INTERVAL 秒（默认 20 分钟，< 30 分钟账期）逐个账号访问一次京准通，
    # 触发服务端下发新的 sdtoken，从而把登录态一直保持住。
    def keepalive_snapshot(self) -> dict:
        with self._lock:
            return {"enabled": settings.KEEPALIVE_ENABLED,
                    "state": self.keep_state, "message": self.keep_message,
                    "intervalSec": settings.KEEPALIVE_INTERVAL,
                    "lastAt": self.keep_last_at, "count": self.keep_count,
                    "pages": list(settings.KEEPALIVE_PAGES) or ["（默认：京准通首页）"],
                    "accounts": {k: bool(v) for k, v in self.keep_last_ok.items()},
                    "requests": {k: list(v) for k, v in self.keep_last_requests.items()}}

    def start_keepalive(self) -> bool:
        """启动保活线程（幂等：已在跑则不重复启动）。"""
        if not settings.KEEPALIVE_ENABLED:
            self._set_keep(state="stopped", message="保活已关闭（JD_KEEPALIVE=0）")
            return False
        with self._lock:
            if self._keep_thread is not None and self._keep_thread.is_alive():
                return False
            self.keep_state = "running"
            self.keep_message = "保活已启动"
            self.keep_last_at = time.time()
        self._keep_stop.clear()
        self._keep_thread = threading.Thread(target=self._loop_keepalive,
                                             name="login-keepalive", daemon=True)
        self._keep_thread.start()
        return True

    def stop_keepalive(self) -> None:
        self._keep_stop.set()
        self._set_keep(state="stopped", message="保活已停止")

    def _set_keep(self, **kv):
        with self._lock:
            self.__dict__.update(kv)

    def _loop_keepalive(self) -> None:
        import random
        interval = max(settings.KEEPALIVE_INTERVAL, 60)
        jitter = max(settings.KEEPALIVE_JITTER, 0)
        # 启动时先等一小会儿，避免和启动补跑/调度抢资源
        if self._keep_stop.wait(30):
            return
        while not self._keep_stop.is_set():
            n_ok = self._keepalive_once()
            with self._lock:
                self.keep_count += 1
                self.keep_last_at = time.time()
                self.keep_message = f"最近一次保活：{n_ok} 个账号会话有效"
            wait = interval + random.randint(0, jitter) if jitter else interval
            if self._keep_stop.wait(wait):
                break

    def _keepalive_pages(self) -> list:
        """保活时依次访问的页面。默认用京准通首页；可用 JD_KEEPALIVE_PAGES 覆盖。

        模仿京麦的关键：打开**真实业务页**后停留几秒，页面前端脚本会自动发出
        三层心跳 —— sso.jd.com/sso/rac（续期，带 SET-COOKIE flash）、
        blackhole bypass（风控）、sgm-w/h5（埋点）。我们只需"真的打开页面"，
        续期签名由页面 JS 自己生成，无需手搓。
        """
        from . import config
        pages = list(settings.KEEPALIVE_PAGES) or [config.JZT_HOME]
        return pages

    def _keepalive_once(self) -> int:
        """逐个账号「像真人一样」打开业务页并停留，让页面自发心跳续期会话。

        返回仍有效的账号数。同时把本次实际发出的保活类请求记进 last_requests，
        便于在控制台确认模仿是否生效。
        """
        from . import config
        from .browser import has_login, launch_persistent, session_ok
        pages = self._keepalive_pages()
        dwell = max(int(settings.KEEPALIVE_DWELL_MS), 1500)
        n_ok = 0
        keys = [a["key"] for a in settings.ACCOUNTS]
        for key in keys:
            if self._keep_stop.is_set():
                break
            # 有别的登录流程在跑（扫码/密码）就别抢同一个 Profile
            with self._lock:
                busy = self.state in ("starting", "waiting")
            if busy:
                time.sleep(2)
                continue
            # 串行化浏览器启动，避免与「检查登录 / 导出」抢同一个 user_data_dir
            if not self._profile_lock.acquire(timeout=120):
                with self._lock:
                    self.keep_last_ok[key] = False
                continue
            pw = ctx = None
            seen = []          # 本次观察到的保活类请求
            try:
                pw, ctx = launch_persistent(headless=True, account=key)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()

                def _on_req(req, seen=seen):
                    u = req.url
                    if any(k in u for k in ("sso.jd.com/sso/rac", "blackhole.m.jd.com/bypass",
                                             "sgm-w.jd.com/h5", "sgm-m.jd.com/h5/init")):
                        seen.append(u.split("?")[0])
                try:
                    page.on("request", _on_req)
                except Exception:  # noqa: BLE001
                    pass

                if not has_login(ctx):
                    with self._lock:
                        self.keep_last_ok[key] = False
                    continue
                # 依次打开业务页并停留，让页面 JS 把三层心跳都发完（模仿京麦）
                for purl in pages:
                    if self._keep_stop.is_set():
                        break
                    try:
                        page.goto(purl, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(dwell)
                    except Exception:  # noqa: BLE001  单页失败不影响整体
                        pass
                ok = session_ok(page)
                with self._lock:
                    self.keep_last_ok[key] = bool(ok)
                    self.keep_last_requests[key] = seen[:20]
                if ok:
                    n_ok += 1
                    if settings.KEEPALIVE_EXPORT_STATE:
                        self._save_state_snapshot(ctx, key)
            except Exception:  # noqa: BLE001  单账号失败不影响其他账号与后续循环
                with self._lock:
                    self.keep_last_ok[key] = False
                    self.keep_last_requests[key] = seen[:20]
            finally:
                try:
                    ctx and ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pw and pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._profile_lock.release()
                except Exception:  # noqa: BLE001
                    pass
        return n_ok

    # ---------------- 统一凭据源（方案 B） ----------------
    # 以前登录成功时同时写 Profile（cookies）和 auth/jd_state.json（storage_state），
    # 两份各自过期、互不同步，是「状态误判」的来源之一。
    # 现在 Profile 作为唯一权威来源；jd_state.json 只在需要时从 Profile 导出快照，
    # 保证它永远和 Profile 一致（不再是独立的一份登录态）。
    @staticmethod
    def _state_target(account: str):
        """主账号沿用 jd_state.json；其他账号用 jd_state_<key>.json。"""
        from . import config
        return (config.STORAGE_STATE if account == settings.MAIN_ACCOUNT
                else config.AUTH_DIR / f"jd_state_{account}.json")

    def _save_state_snapshot(self, ctx, account: str) -> None:
        """把当前持久化上下文导出为 storage_state 快照（备份用，Profile 仍是权威源）。

        仅作离线备份/迁移用途；抓取一律直接读 Profile，不再依赖这份 json。
        导出失败不影响登录成功判定。
        """
        try:
            target = self._state_target(account)
            target.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(target))
        except Exception:  # noqa: BLE001
            pass

    def export_state_from_profiles(self) -> dict:
        """把每个账号落盘 Profile 的当前 storage_state 导出到 jd_state.json（供迁移/备份）。"""
        from .browser import has_login, launch_persistent
        result = {}
        for a in settings.ACCOUNTS:
            key = a["key"]
            if not self._profile_lock.acquire(timeout=180):
                result[key] = "busy"
                continue
            pw = ctx = None
            try:
                pw, ctx = launch_persistent(headless=True, account=key)
                if not has_login(ctx):
                    result[key] = "no-login"
                    continue
                target = self._state_target(key)
                ctx.storage_state(path=str(target))
                result[key] = str(target)
            except Exception as exc:  # noqa: BLE001
                result[key] = f"error:{type(exc).__name__}"
            finally:
                try:
                    ctx and ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pw and pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._profile_lock.release()
                except Exception:  # noqa: BLE001
                    pass
        return result

    def _set(self, **kv):
        with self._lock:
            self.__dict__.update(kv)
            self.updated_at = time.time()

    def _join_previous(self, timeout: float = 10.0) -> None:
        """停掉上一个登录线程，并等它释放浏览器 Profile。

        同一个 user_data_dir 不能被两个持久化上下文同时打开，所以「换一种方式重新登录」
        必须先等旧线程退出、把旧浏览器关掉，否则新上下文会因 Profile 被占用而启动失败。
        另外，保活线程也会周期性地打开 Profile，登录前必须先把它停掉并拿住占用锁。
        """
        t = self._thread
        if t is not None and t.is_alive():
            self._stop.set()
            t.join(timeout)
        self._stop.clear()
        # 等保活当前这一轮结束并释放 Profile 锁（保活线程随后会自己跳过 busy 的账号）
        self._profile_lock.acquire(timeout=180)
        self._profile_lock.release()

    def start(self, account: str | None = None, replace: bool = False) -> bool:
        with self._lock:
            if self.state in ("starting", "waiting") and not replace:
                return False
        if replace:
            self._join_previous()
        with self._lock:
            self.state = "starting"
            self.error = None
            self.qr_png = None
            self.message = "正在打开登录页…"
            self.account = account or settings.MAIN_ACCOUNT
            self.updated_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(self.account,),
                                        name="login", daemon=True)
        self._thread.start()
        return True

    def start_password(self, account: str | None, username: str, password: str,
                       show_window: bool = True, replace: bool = False) -> bool:
        """账号密码登录：凭据只在内存里传给后台线程，不落盘、不写日志。"""
        with self._lock:
            if self.state in ("starting", "waiting") and not replace:
                return False
        if replace:
            self._join_previous()
        with self._lock:
            self.state = "starting"
            self.error = None
            self.qr_png = None
            self.message = "正在用账号密码登录…"
            self.account = account or settings.MAIN_ACCOUNT
            self.updated_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop_password,
                                        args=(self.account, username, password, bool(show_window)),
                                        name="login-pwd", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._stop.set()
        self._set(state="idle", message="已取消")

    # 京麦/京东商家登录页：右上角 .scan-login 图标切到扫码模式，二维码多在 .qrcode 里
    _QR_TOGGLES = (".scan-login", ".login-tab-qrcode", "[class*='scan-login']",
                   "[class*='qrcode-login']", "img[src*='qrcode']")
    _QR_SELECTORS = (".qrcode img", "#qrcode img", ".qrcode-img img", "img[src*='qr']",
                     ".qrcode", "#qrcode", ".login-qrcode", "[class*='qrcode']",
                     "[class*='qr-code']", "canvas")

    def _enter_qr_mode(self, page) -> bool:
        """点击「扫码登录」图标切到二维码模式。"""
        for sel in self._QR_TOGGLES:
            try:
                el = page.query_selector(sel)
                if el is None or not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box or box["width"] < 8:
                    continue
                el.click(timeout=3000)
                page.wait_for_timeout(3000)
                self._set(message="已切换到扫码登录，请用京东 App 扫描")
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _shot(self, page):
        """优先裁二维码元素；识别不到就整屏截图（保证用户一定能扫到）。"""
        png = None
        for sel in self._QR_SELECTORS:
            try:
                el = page.query_selector(sel)
                if not el:
                    continue
                box = el.bounding_box()
                if not box or box["width"] < 60 or box["height"] < 60:
                    continue
                if box["width"] > 700 or box["height"] > 700:
                    continue
                png = el.screenshot()
                break
            except Exception:  # noqa: BLE001
                continue
        if png is None:
            try:
                # 整页截图：京东/京麦登录页的二维码常被放在右侧面板，视口截图会截掉
                png = page.screenshot(full_page=True)
            except Exception:  # noqa: BLE001
                return
        self._set(qr_png=png, qr_at=time.time())

    def _refresh_qr(self, page):
        """二维码失效时点一下让它刷新，再重新截图。"""
        try:
            body = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body = ""
        if "失效" in body or "刷新" in body or "已过期" in body:
            for sel in self._QR_SELECTORS[:6] + self._QR_TOGGLES:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=2000)
                        page.wait_for_timeout(2000)
                        break
                except Exception:  # noqa: BLE001
                    continue
        self._shot(page)

    # ---- 账号密码登录：京东 passport 的表单 id 随版本/域名略有差异，按顺序探测 ----
    _PWD_TAB_TEXTS = ("账户登录", "账号登录", "密码登录", "帐号登录", "普通登录")
    _USER_SELECTORS = ("#loginname", "input[name='loginname']", "#username",
                       "input[name='username']", "input[name='loginName']",
                       "input[name='mobile']", "input[type='text']", "input[type='tel']")
    _PWD_SELECTORS = ("#nloginpwd", "input[name='nloginpwd']", "input[name='password']",
                      "#password", "input[type='password']")
    _SUBMIT_SELECTORS = ("#loginsubmit", "button[type='submit']", ".login-btn",
                         "a[class*='login-btn']", ".submit-btn", ".btn-login")

    def _click_text(self, page, texts) -> bool:
        """按可见文本点击（标签页/提交按钮的文案兜底）。"""
        js = """(texts) => {
          const els = Array.from(document.querySelectorAll('a,button,div,span,li,p'));
          for (const t of texts) {
            for (const el of els) {
              const s = (el.textContent || '').trim();
              if (s === t && el.offsetParent !== null) { el.click(); return t; }
            }
          }
          return null;
        }"""
        try:
            return bool(page.evaluate(js, list(texts)))
        except Exception:  # noqa: BLE001
            return False

    def _enter_password_mode(self, page) -> bool:
        """切到「账号/密码登录」标签（默认可能是扫码标签）。"""
        return self._click_text(page, self._PWD_TAB_TEXTS)

    def _visible(self, page, selectors):
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    def _agree(self, page) -> None:
        """勾选「同意协议」类复选框，否则部分登录页不允许提交。"""
        js = """() => {
          const cbs = Array.from(document.querySelectorAll("input[type='checkbox']"));
          for (const cb of cbs) {
            const lab = ((cb.closest('label') || cb.parentElement || {}).textContent || '');
            if (/协议|同意|已阅读/.test(lab) && !cb.checked) { cb.click(); return true; }
          }
          return false;
        }"""
        try:
            page.evaluate(js)
        except Exception:  # noqa: BLE001
            pass

    def _fill_credentials(self, page, username: str, password: str) -> bool:
        user = self._visible(page, self._USER_SELECTORS)
        if user is None:
            return False
        try:
            user.click(timeout=2000)
            user.fill(username)
        except Exception:  # noqa: BLE001
            return False
        pwd = self._visible(page, self._PWD_SELECTORS)
        if pwd is None:
            # 分步登录：先填账号 → 下一步 → 密码框出现在下一页
            self._submit(page)
            page.wait_for_timeout(2500)
            pwd = self._visible(page, self._PWD_SELECTORS)
        if pwd is None:
            return False
        try:
            pwd.click(timeout=2000)
            pwd.fill(password)
        except Exception:  # noqa: BLE001
            return False
        return True

    def _submit(self, page) -> bool:
        for sel in self._SUBMIT_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return self._click_text(page, ("登 录", "登录", "立即登录"))

    def _needs_verify(self, page) -> bool:
        try:
            body = page.inner_text("body")
        except Exception:  # noqa: BLE001
            return False
        return any(w in body for w in ("滑块", "拖动", "短信验证", "验证码", "安全验证"))

    def _loop_password(self, account: str, username: str, password: str,
                       show_window: bool) -> None:
        from . import config
        from .browser import has_login, launch_persistent, session_ok
        pw = ctx = None
        try:
            # 有头模式：遇到滑块/短信验证时用户可在窗口里手动完成
            pw, ctx = launch_persistent(headless=not show_window, account=account)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.set_viewport_size({"width": 1440, "height": 900})
            except Exception:  # noqa: BLE001
                pass
            page.goto(config.LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            if session_ok(page):
                self._set(state="logged_in", message="当前登录态仍然有效，无需重新登录")
                return
            self._set(state="waiting", message="正在切换到账号密码登录…")
            self._enter_password_mode(page)
            page.wait_for_timeout(1500)
            self._agree(page)
            filled = self._fill_credentials(page, username, password)
            if not filled and not show_window:
                self._set(state="error", message="未找到账号/密码输入框，请改用扫码登录")
                return
            if filled:
                self._set(state="waiting", message="已填入账号密码，正在提交…")
                self._submit(page)
            else:
                # 有头模式下留窗口给用户手动登录，别直接失败
                self._set(state="need_verify",
                          message="未能自动填入表单，请在弹出的浏览器窗口里手动完成登录")
            deadline = time.time() + int(os.getenv("JD_LOGIN_TIMEOUT", "600"))
            told_verify = False
            while time.time() < deadline and not self._stop.is_set():
                # 必须由服务端确认：cookie 还在但会话过期时 has_login 会误判
                if has_login(ctx) and session_ok(page):
                    self._save_state_snapshot(ctx, account)
                    self._set(state="logged_in", message="登录成功，登录态已保存", qr_png=None)
                    return
                if not told_verify and self._needs_verify(page):
                    told_verify = True
                    self._set(state="need_verify",
                              message="需要人工验证（滑块/短信验证码）：请在打开的浏览器窗口内完成后自动继续")
                time.sleep(2)
            self._set(state="idle" if self._stop.is_set() else "timeout",
                      message="已取消" if self._stop.is_set() else "等待超时，请重试")
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", error=str(exc)[:400], message="账号密码登录失败")
        finally:
            try:
                ctx and ctx.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                pw and pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self, account: str) -> None:
        from . import config
        from .browser import has_login, launch_persistent, session_ok
        pw = ctx = None
        try:
            pw, ctx = launch_persistent(headless=True, account=account)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.set_viewport_size({"width": 1440, "height": 900})
            except Exception:  # noqa: BLE001
                pass
            page.goto(config.LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            if session_ok(page):
                self._set(state="logged_in", message="当前登录态仍然有效，无需重新扫码")
                return
            self._set(state="waiting", message="正在切换到扫码登录…")
            self._enter_qr_mode(page)
            self._set(state="waiting", message="请用京东 App 扫描二维码")
            self._shot(page)
            deadline = time.time() + int(os.getenv("JD_LOGIN_TIMEOUT", "600"))
            last_shot = time.time()
            while time.time() < deadline and not self._stop.is_set():
                # 必须由服务端确认：只看 cookie 会在会话过期时误判为「已登录」
                if has_login(ctx) and session_ok(page):
                    self._save_state_snapshot(ctx, account)
                    self._set(state="logged_in", message="登录成功，登录态已保存", qr_png=None)
                    return
                if time.time() - last_shot > 20:
                    self._refresh_qr(page)
                    last_shot = time.time()
                time.sleep(2)
            self._set(state="idle" if self._stop.is_set() else "timeout",
                      message="已取消" if self._stop.is_set() else "等待超时（5分钟），请重试")
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", error=str(exc)[:400], message="启动浏览器失败")
        finally:
            try:
                ctx and ctx.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                pw and pw.stop()
            except Exception:  # noqa: BLE001
                pass


_login = LoginSession()

LOGIN_CSS = DASH_CSS + """
.qrbox{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;display:inline-block}
.qrbox img{display:block;max-width:340px;image-rendering:pixelated}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:14px}
.tabs button{background:none;border:none;padding:9px 14px;cursor:pointer;font-size:15px;color:#6b7280;border-bottom:2px solid transparent}
.tabs button.active{color:#111827;font-weight:600;border-bottom-color:#2563eb}
.fld{margin-bottom:10px}
.fld input{padding:9px 11px;border-radius:8px;border:1px solid var(--line);font-size:14px;width:320px;max-width:80vw}
"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    acc = request.query_params.get("account") or settings.MAIN_ACCOUNT
    opts = "".join(f'<option value="{_e(a["key"])}"{" selected" if a["key"] == acc else ""}>'
                   f'{_e(a.get("label"))}</option>' for a in settings.ACCOUNTS)
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录京东</title><style>{LOGIN_CSS}</style></head><body>
<header class="top"><div class="wrap"><h1>登录京东</h1>
<div class="metaline">登录态会写入 <code>{_e(settings.AUTH_DIR)}</code>，容器重启后依然有效</div></div></header>
<div class="wrap">
<div class="card">
  <div class="tabs">
    <button id="tabQr" class="active" onclick="switchTab('qr')">扫码登录</button>
    <button id="tabPwd" onclick="switchTab('pwd')">账号密码登录</button>
  </div>

  <div id="paneQr">
    <h2>1. 选择账号 → 2. 手机京东 App 扫码</h2>
    <p class="hint">每个账号一个独立浏览器 Profile，需要分别登录。二维码每 20 秒自动刷新。</p>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px">
      <select id="acc" style="padding:7px 10px;border-radius:8px;border:1px solid var(--line)">{opts}</select>
      <button class="btn" onclick="startLogin()">开始登录</button>
    </div>
    <div id="qrwrap"></div>
  </div>

  <div id="panePwd" style="display:none">
    <h2>用京麦 / 京东账号密码登录</h2>
    <p class="hint">账号密码只在本机内存中用于代填登录表单，<b>不会写入磁盘或日志</b>。</p>
    <div class="fld">
      <select id="accPwd" style="padding:7px 10px;border-radius:8px;border:1px solid var(--line);width:320px;max-width:80vw">{opts}</select>
    </div>
    <div class="fld"><input id="pwdUser" autocomplete="off" placeholder="京麦账号 / 手机号 / 邮箱"></div>
    <div class="fld"><input id="pwdPass" type="password" autocomplete="off" placeholder="密码"></div>
    <div class="fld"><label class="hint"><input type="checkbox" id="pwdWindow" checked> 显示浏览器窗口（遇到滑块/短信验证时可在窗口里手动完成）</label></div>
    <div style="margin-bottom:12px"><button class="btn" onclick="startPwdLogin()">登录</button></div>
    <div class="warn">若登录页要求滑块或短信验证：保持「显示浏览器窗口」勾选，在弹出的窗口里完成验证即可，
      页面状态会自动变为已登录。无桌面的容器环境请改为本地登录后挂载 Profile。</div>
  </div>

  <div style="display:flex;gap:10px;align-items:center;margin-top:14px">
    <button class="btn sec" onclick="cancelLogin()">取消</button>
    <a href="/">← 控制台</a>
  </div>
  <div id="status" class="hint" style="margin-top:10px">未开始</div>
</div>
</div>
<script>
function switchTab(t){{
  var qr = t === 'qr';
  document.getElementById('paneQr').style.display = qr ? '' : 'none';
  document.getElementById('panePwd').style.display = qr ? 'none' : '';
  document.getElementById('tabQr').className = qr ? 'active' : '';
  document.getElementById('tabPwd').className = qr ? '' : 'active';
}}
function startLogin(){{
  fetch('/api/login/start?account='+document.getElementById('acc').value, {{method:'POST'}})
    .then(r=>r.json()).then(function(j){{ if(!j.ok){{ document.getElementById('status').textContent='启动失败：'+(j.error||''); return; }} poll(); }});
}}
function startPwdLogin(){{
  var u = document.getElementById('pwdUser').value.trim();
  var p = document.getElementById('pwdPass').value;
  if(!u || !p){{ document.getElementById('status').textContent='请填写账号和密码'; return; }}
  document.getElementById('pwdPass').value = '';
  fetch('/api/login/password', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{account:document.getElementById('accPwd').value, username:u, password:p,
      showWindow:document.getElementById('pwdWindow').checked}})}})
    .then(r=>r.json()).then(function(j){{ if(!j.ok){{ document.getElementById('status').textContent='启动失败：'+(j.error||''); return; }} poll(); }});
}}
function cancelLogin(){{ fetch('/api/login/cancel',{{method:'POST'}}).then(()=>poll()); }}
function poll(){{
  fetch('/api/login/status').then(r=>r.json()).then(function(j){{
    document.getElementById('status').innerHTML='状态：<b>'+j.state+'</b> '+(j.message||'')+(j.error?(' —— '+j.error):'');
    var w=document.getElementById('qrwrap');
    if(j.hasQr){{ w.innerHTML='<div class="qrbox"><img src="/api/login/qr?t='+Date.now()+'"></div>'; }}
    else if(j.state!=='waiting'){{ w.innerHTML=''; }}
    if(j.state==='waiting'||j.state==='starting'||j.state==='need_verify') setTimeout(poll,2000);
  }});
}}
poll();
</script></body></html>""")


@app.post("/api/login/start")
def api_login_start(account: str | None = None):
    ok = _login.start(account, replace=True)
    return {"ok": ok, "error": None if ok else "已有登录流程在进行"}


@app.post("/api/login/password")
async def api_login_password(request: Request):
    """账号密码登录。凭据仅在内存中传递，不落盘、不写日志。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "请求格式错误"}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return {"ok": False, "error": "账号和密码不能为空"}
    ok = _login.start_password((body.get("account") or "").strip() or None, username, password,
                               show_window=bool(body.get("showWindow", True)), replace=True)
    return {"ok": ok, "error": None if ok else "已有登录流程在进行"}


@app.post("/api/login/check")
def api_login_check(account: str | None = None, force: int = 0):
    """检查落盘登录态是否仍然有效（真实请求京准通 logininfo）。"""
    ok = _login.start_check(account, force=bool(force))
    return {"ok": ok, "error": None if ok else "检查正在进行中"}


@app.get("/api/login/accounts")
def api_login_accounts():
    """各账号的落盘登录态快照。"""
    snap = _login.accounts_snapshot()
    labels = {a["key"]: a.get("label") for a in settings.ACCOUNTS}
    snap["accounts"] = [dict(v, key=k, label=labels.get(k, k))
                        for k, v in snap["accounts"].items()]
    return {"ok": True, **snap}


@app.get("/api/login/keepalive")
def api_login_keepalive():
    """登录态保活状态（每 N 分钟自动续一次会话）。"""
    return {"ok": True, **_login.keepalive_snapshot()}


@app.post("/api/login/keepalive/start")
def api_login_keepalive_start():
    ok = _login.start_keepalive()
    return {"ok": ok, "error": None if ok else "保活已在运行或已关闭"}


@app.post("/api/login/keepalive/stop")
def api_login_keepalive_stop():
    _login.stop_keepalive()
    return {"ok": True}


@app.post("/api/login/keepalive/now")
def api_login_keepalive_now():
    """立刻续一次（不想等下一个周期时用），同步执行、可能耗时数十秒。"""
    n = _login._keepalive_once()
    with _login._lock:
        _login.keep_last_at = time.time()
        _login.keep_message = f"手动续期：{n} 个账号会话有效"
    return {"ok": True, "alive": n, **_login.keepalive_snapshot()}


@app.post("/api/login/export-state")
def api_login_export_state():
    """把 Profile 的登录态导出成 storage_state（统一凭据源，方案 B）。"""
    return {"ok": True, "result": _login.export_state_from_profiles()}


@app.post("/api/login/cancel")
def api_login_cancel():
    _login.cancel()
    return {"ok": True}


@app.get("/api/login/status")
def api_login_status():
    return _login.snapshot()


@app.get("/api/login/qr")
def api_login_qr():
    png = _login.qr_png
    if not png:
        return Response(status_code=404)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ================================================================ 调度
def _nightly_job():
    """回刷最近 REFRESH_DAYS 天，并补齐停机期间的空洞。"""
    rng = scrape_day.catchup_range()
    if rng is None:
        _log_progress("数据已是最新，无需抓取")
        return
    start, end = rng
    newest = scrape_day.newest_day()
    _log_progress(f"定时任务 {start} ~ {end}（库内最新 {newest or '无'}）")
    res = scrape_day.run_background(settings.ACCOUNTS, start, end, force=False,
                                     log=_log_progress, name="nightly",
                                     trigger="schedule")
    if not res.get("ok"):
        _log_progress(f"定时任务未启动：{res.get('error')}")


def _schedule_target(dt=None):
    """算出「今天的抓取时刻」= 基准时间 ± SCHEDULE_JITTER_MIN 分钟（每天一个固定随机值）。

    同一天内多次调用返回同一时刻（用日期做随机种子），保证「检查型调度」不会
    重复触发或错过；跨天重新抽一次随机偏移。
    """
    from datetime import timedelta
    import random
    dt = dt or dt_now()
    rnd = random.Random(f"jdsched-{dt.date().isoformat()}")
    jit = max(settings.SCHEDULE_JITTER_MIN, 0)
    offset = rnd.randint(-jit, jit)
    base = dt.replace(hour=settings.SCHEDULE_HOUR,
                      minute=settings.SCHEDULE_MINUTE,
                      second=0, microsecond=0)
    return base + timedelta(minutes=offset), offset


def dt_now():
    """带时区的当前时间（用 settings.TZ）。"""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.TZ))
    except Exception:  # noqa: BLE001
        from datetime import timezone, timedelta
        return datetime.now(timezone(timedelta(hours=8)))


def _scheduler_tick():
    """每分钟检查：是否已到今天的抓取时刻（含随机浮动），到了就触发一次。"""
    now = dt_now()
    target, offset = _schedule_target(now)
    today = now.date().isoformat()
    with _sched_lock:
        if _sched_state.get("ran_date") == today:
            return                      # 今天已跑过
        if now < target:
            return                      # 还没到点
        _sched_state["ran_date"] = today
    _log_progress(f"定时调度触发（基准 {settings.SCHEDULE_HOUR:02d}:"
                  f"{settings.SCHEDULE_MINUTE:02d}，本次浮动 {offset:+d} 分钟，"
                  f"实际 {target.strftime('%H:%M')}）")
    try:
        _nightly_job()
    except Exception as exc:  # noqa: BLE001
        _log_progress(f"定时任务异常：{type(exc).__name__}: {exc}")


_sched_lock = threading.Lock()
_sched_state = {}


@app.on_event("startup")
def _startup():
    settings.ensure_dirs()
    if settings.KEEPALIVE_ENABLED:
        _login.start_keepalive()
        print(f"[webapp] 登录态保活已启动：每 {settings.KEEPALIVE_INTERVAL // 60} 分钟续一次会话",
              flush=True)
    if settings.ENABLE_SCHEDULER:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            sch = BackgroundScheduler(timezone=settings.TZ)
            # 每分钟检查一次：是否已到「今天的基准时间 ± 随机浮动」那一刻。
            # 用检查型而非固定 cron，才能实现每天不同的浮动时刻。
            sch.add_job(_scheduler_tick, IntervalTrigger(minutes=1, timezone=settings.TZ),
                        id="nightly", replace_existing=True,
                        max_instances=1, coalesce=True, misfire_grace_time=120)
            sch.start()
            app.state.scheduler = sch
            _t, _off = _schedule_target()
            print(f"[webapp] 调度已启动：基准 {settings.SCHEDULE_HOUR:02d}:"
                  f"{settings.SCHEDULE_MINUTE:02d} ± {settings.SCHEDULE_JITTER_MIN} 分钟"
                  f"（{settings.TZ}）｜今天实际 { _t.strftime('%H:%M') }", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[webapp] 调度启动失败：{exc}", flush=True)
    if settings.RUN_ON_START:
        rng = scrape_day.catchup_range()
        if rng is not None:
            def _catchup():
                time.sleep(20)
                _nightly_job()
            threading.Thread(target=_catchup, name="catchup", daemon=True).start()
            print(f"[webapp] 启动补跑：{rng[0]} ~ {rng[1]}"
                  f"（库内最新 {scrape_day.newest_day() or '无'}）", flush=True)
        else:
            print("[webapp] 数据已是最新，无需补跑", flush=True)


@app.on_event("shutdown")
def _shutdown():
    try:
        _login.stop_keepalive()
    except Exception:  # noqa: BLE001
        pass
    sch = getattr(app.state, "scheduler", None)
    if sch:
        try:
            sch.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
