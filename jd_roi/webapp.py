# -*- coding: utf-8 -*-
"""Web 控制台：任意区间分析与报表。

启动：
    uvicorn jd_roi.webapp:app --host 0.0.0.0 --port 8000

页面：
    /            控制台（区间选择、数据覆盖、立即抓取、扫码登录）
    /report      任意区间的聚合分析报表（自带区间选择器；⑧行业大盘页签）
    /ai          独立 AI 分析台：每日 AI 报告 + 可追问对话框（含思考过程流式展示）
    /paycomm     CPS 花费：按 SKU 看付款佣金金额，可导出 Excel
    /paycomm/spend  京准通花费：按 SKU 看广告花费（智能投放 + 全站智能推广），可导出 Excel
    /price      SKU 前台价：逐日看每个 SKU 的成交均价怎么变（读本地按天入库的商智明细）
    /subsidy    超级补贴：京麦「消息 → 店铺 → 超级补贴」按 SKU 汇总补贴活动状态
    /cashflow    广告费流水：账户余额、充值/消费柱状图、每日消耗（财务记录口径）
    /login       扫码登录

API：
    GET  /api/status            运行状态 / 登录状态 / 最近一次任务
    GET  /api/coverage          按天数据覆盖情况
    GET  /api/analysis          区间分析 JSON
    GET  /api/industry/status   行业大盘缓存补齐进度
    POST /api/industry/run      起后台任务补齐/重抓行业大盘
    POST /api/industry/refresh  报表页「重新抓取」→ 重抓后跳回报表
    GET  /api/run/status        抓取进度 + 抓取锁状态
    POST /api/run               触发抓取（后台线程）
    POST /api/run/cancel        取消当前抓取 / 解锁卡死的抓取锁
    GET  /api/ai/status         AI 生成状态（含当前 job 的思考/成文进度）
    POST /api/ai/run            生成整份 AI 报告（后台线程，返回 jobId）
    POST /api/ai/chat           对话框追问（后台线程，返回 jobId，**立即返回**）
    GET  /api/ai/stream         SSE：某 job 的 phase/reasoning/content/done 事件流（可续传）
    GET  /api/ai/jobs           最近 AI 任务快照（刷新后找回未完成生成）
    GET  /api/compete           SKU 竞品/关键词分析缓存（前台价页右侧面板）
    POST /api/compete/run       后台抓取某个 SKU 的同款/行业词/京东推词
    GET  /api/compete/status    竞品抓取进度
    POST /api/compete/ai        用现有 LLM 配置解读竞品与关键词数据
    POST /api/price/probe       探寻降价商品的前台原因（商品页价 + 活动标识，后台线程）
    GET  /api/price/probe/status 商品页探寻进度
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
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response, StreamingResponse)

from . import (activity, aijobs, ai_page, analyze2, analyze_ai, cashflow, compete, config,
               dailystore as ds, industry, itemprobe, maobao, paycomm, pricewatch,
               profit, report2, scrape_day, settings, subsidy)

settings.ensure_dirs()
app = FastAPI(title="京东广告运营分析服务", docs_url="/api/docs", redoc_url=None)

# ================================================================ 子路径挂载
# 公网 nginx 把本服务反代到子路径（如 http://<ip>/jdfenxi/）时，页面里的根路径
# 链接（href="/login"、fetch('/api/...')）和 302 的 Location 都要补上前缀，
# 否则浏览器会把这些地址解析到主站根路径，跑到别的应用上。
# 前缀按请求决定：nginx 反代时带 X-Forwarded-Prefix（如 /jdfenxi），直连
# （局域网 http://<ip>:8011/）没有该头，回退到环境变量 JD_URL_PREFIX；都为空时
# 行为与原来完全一致。
URL_PREFIX = (os.getenv("JD_URL_PREFIX", "") or "").rstrip("/")

_ATTR_RE = re.compile(r'\b(href|src|action)=(["\'])/(?!/)')
# 只给已知的首段路径补前缀，避免误伤 JS 里的 split('/') 之类的普通字符串
_URL_STR_RE = re.compile(
    r'(["\'])/(?=(?:api|login|report|ai|assets|healthz|favicon\.ico'
    r'|cashflow|paycomm|profit|price|subsidy|cache)\b)')


@app.middleware("http")
async def _prefix_urls(request: Request, call_next):
    prefix = (request.headers.get("x-forwarded-prefix") or URL_PREFIX or "").rstrip("/")
    resp = await call_next(request)
    if not prefix:
        return resp
    loc = resp.headers.get("location")
    if loc and loc.startswith("/") and not loc.startswith("//"):
        resp.headers["location"] = prefix + loc
    # SSE（/api/ai/stream）等非 HTML 响应原样放行，不能缓冲
    if "text/html" not in resp.headers.get("content-type", ""):
        return resp
    buf = bytearray()
    async for chunk in resp.body_iterator:
        buf += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
    text = buf.decode("utf-8", "replace")
    text = _ATTR_RE.sub(lambda m: f"{m.group(1)}={m.group(2)}{prefix}/", text)
    text = _URL_STR_RE.sub(lambda m: f"{m.group(1)}{prefix}/", text)
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in ("content-length", "content-type")}
    return Response(content=text.encode("utf-8"), status_code=resp.status_code,
                    headers=headers, media_type="text/html; charset=utf-8")

KINDS_UI = [("jzt_campaign", "概览计划"), ("jst_campaign", "投放计划"),
            ("jst_sku", "投放商品"), ("jst_searchword", "搜索词"),
            ("kw", "关键词"), ("kw_ad", "关键词创意(按品)"), ("sz_product", "商智商品"),
            ("sz_flow", "商智流量"), ("oplog", "操作日志"),
            ("hourly", "分时")]


# ================================================================ 工具
def _e(s) -> str:
    return html.escape(str("" if s is None else s), quote=True)


def _today() -> dt.date:
    # 走 settings 的目标时区；裸 now() 在进程时区被污染成 UTC 时会少一天
    return settings.today()


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


def ui_range(start: str | None, end: str | None, days: int | None,
             default_days: int = 7) -> tuple:
    """界面默认区间：未显式给日期时，**贴着本地已抓取的数据**（最新一天往前 N 天）。

    这样打开报表/看 JSON 默认落在「有数据的区间」，
    也与夜间 AI 解读的区间一致（否则默认区间会跨到还没抓的日期）。
    """
    if start and end:
        return ds.norm_day(start), ds.norm_day(end)
    n = int(days) if days else default_days
    rng = analyze_ai.local_range(settings.ACCOUNTS, max(n, 1))
    if rng:
        return rng
    return resolve_range(None, None, n)


def _last_month_range() -> tuple:
    """上个自然月整月（花费一览的默认区间：CPS 佣金结算有滞后，上月最稳）。"""
    today = _today()
    last_end = today.replace(day=1) - dt.timedelta(days=1)
    return last_end.replace(day=1).isoformat(), last_end.isoformat()


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


def _ensure_industry(keys: list, start: str, end: str, *, force: bool = False) -> dict:
    """报表/AI 生成前先命中本地缓存；缺的账号起后台补齐，不阻塞页面渲染。"""
    try:
        return industry.ensure(keys, start, end, force=force)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "blocks": {}, "missing": list(keys), "status": {}}


def _range_bar(start: str, end: str, keys: list, action: str = "/report",
               show_accounts: bool = True, extra: str = "") -> str:
    """区间选择器（控制台与报表共用）。"""
    n = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    presets = [("昨天", 1), ("近3天", 3), ("近7天", 7), ("近14天", 14), ("近30天", 30)]
    pre_html = "".join(
        f'<button type="button" class="pre" onclick="setRange({d})">{_e(lab)}</button>'
        for lab, d in presets)
    # 日历口径的快捷区间（与花费/利润页一致）：本月 / 上月 / 上上月
    today = _today()
    first = today.replace(day=1)
    lm_end = first - dt.timedelta(days=1)
    lm_start = lm_end.replace(day=1)
    pm_end = lm_start - dt.timedelta(days=1)
    pm_start = pm_end.replace(day=1)
    # 今天的数据还没出全（商智只到昨天），所以「本月」一律止于昨天；
    # 每月 1 号本月还没有完整一天，此时不显示「本月」，避免点出一个空区间。
    yday = today - dt.timedelta(days=1)
    cal = ([("本月", first, yday)] if first <= yday else []) + [
        ("上月", lm_start, lm_end), ("上上月", pm_start, pm_end)]
    cal_html = "".join(
        f'<button type="button" class="pre" onclick="setSpan(\'{s.isoformat()}\',\'{e.isoformat()}\')">'
        f'{_e(lab)}</button>'
        for lab, s, e in cal)
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
  <span class="mini" id="rbDays" style="color:var(--muted)">共 {n} 天</span>
  {cal_html}<span class="sep"></span>{pre_html}{acc_html}
  <button type="submit">生成分析</button>
  {extra}
</form>
<script>
function setSpan(a,b){{ rbStart.value=a; rbEnd.value=b; upd(); }}
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
# 控制台复用报表页的同一套设计系统（奶油画布 × 珊瑚 × 深墨），只做局部覆盖，
# 避免"两个页面两套风格"的割裂。
DASH_CSS = report2.CSS + """
.wrap{max-width:1300px;padding:0 24px 64px}
header.top{padding:26px 0 0;margin-bottom:24px}
header.top h1{font-size:36px}
.topnav{margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
/* 覆盖日历 */
.days{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.days i{width:16px;height:16px;border-radius:4px;background:var(--hair);display:block}
.days i.on{background:var(--green)}
/* 数字列 */
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
/* 任务中心弹窗 */
.modal{display:none;position:fixed;inset:0;background:rgba(20,20,19,.5);z-index:80;
  -webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);padding:32px 16px;overflow:auto}
.modal.open{display:block}
.modal-card{max-width:1040px;margin:0 auto;background:var(--canvas);border:1px solid var(--hair);
  border-radius:var(--r-lg);box-shadow:var(--shadow-lift);animation:fadeup .22s ease}
.modal-head{display:flex;align-items:center;gap:10px;padding:18px 22px;border-bottom:1px solid var(--hair);
  position:sticky;top:0;background:var(--canvas);border-radius:var(--r-lg) var(--r-lg) 0 0;z-index:2}
.modal-head h2{margin:0;font-size:20px;font-weight:400;font-family:var(--serif);flex:1;color:var(--ink)}
.modal-body{padding:18px 22px;max-height:76vh;overflow:auto}
.tasklog{background:var(--dark);color:var(--on-dark);border-radius:var(--r-sm);padding:12px 14px;
  font-size:12px;line-height:1.75;max-height:200px;overflow:auto;white-space:pre-wrap;
  word-break:break-all;font-family:var(--mono);margin-top:10px}
.tm{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:12.5px;color:var(--body);margin:8px 0}
.tm b{font-weight:600;color:var(--ink)}
.trow{cursor:pointer}
.trow:hover td{background:var(--soft)}
.trow.open td{background:var(--surface-card)}
tbody .det td{background:var(--soft);padding:12px 11px}
/* 登录页 */
.fld{margin-bottom:12px}
.fld input{padding:10px 12px;border-radius:var(--r-sm);border:1px solid var(--hair);font-size:14px;
  width:340px;max-width:82vw;background:var(--canvas);color:var(--ink)}
.fld input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(204,120,92,.15)}
.qrbox{background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md);padding:16px;
  display:inline-block}
.qrbox img{display:block;max-width:320px;image-rendering:pixelated}
.warn{background:var(--amberbg);border-left:3px solid var(--amber);padding:12px 16px;
  border-radius:0 var(--r-sm) var(--r-sm) 0;font-size:12.5px;color:var(--amberink);margin:12px 0;line-height:1.75}
details>summary{cursor:pointer}
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
            cls = "" if cnt else ' style="color:var(--muted2)"'
            cells += (f'<td class="n"{cls}><b>{cnt}</b>'
                      f'<div style="font-size:11px;color:var(--muted2)">{_e(last)}</div></td>')
        rows.append(
            f'<tr><td class="l"><b>{_e(a.get("label"))}</b>'
            f'<div class="mini">{_e(k)} · ID {_e(a.get("account_id"))}</div></td>'
            f'<td class="n"><b>{d.get("count", 0)}</b></td>'
            f'<td class="n mini">{_e(d.get("first") or "—")}<br>{_e(d.get("last") or "—")}</td>'
            f'{cells}</tr>')

    kinds_head = "".join(f'<th class="n">{_e(lab)}'
                         f'<div style="font-weight:400;font-size:10.5px;color:var(--muted2)">'
                         f'天 / 最近</div></th>' for _k, lab in KINDS_UI)

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
        cal += (f'<div style="margin:10px 0"><b>{_e(a.get("label"))}</b>'
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
{report2.navtop("/")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}广告运营控制台</div>
  <h1>京东广告运营分析服务</h1>
  <div class="metaline">
    <span>按天自动入库</span><span class="dot"></span>
    <span>任意区间聚合分析</span><span class="dot"></span>
    <span>每天 {settings.SCHEDULE_HOUR:02d}:{settings.SCHEDULE_MINUTE:02d}
      （±{settings.SCHEDULE_JITTER_MIN} 分钟，{_e(settings.TZ)}）抓取前一天</span>
  </div>
  {_range_bar(start, end, keys, "/report")}
</div></header>
<div class="wrap">

<div class="grid g4">
  <div class="kpi">
    <div class="lbl">最近一次自动抓取</div>
    <div class="val" style="font-size:15px;line-height:1.45">{lr_html}</div>
  </div>
  <div class="kpi click" onclick="openTasks()" title="点击查看任务中心：运行中与历史任务">
    <div class="lbl">当前任务</div>
    <div class="val" style="font-size:17px">{task_html}</div>
    <div class="sub">{task_hint}{busy}</div>
    <div class="sub" style="color:var(--primary);font-weight:600">查看任务中心 →</div>
  </div>
  <div class="kpi">
    <div class="lbl">登录状态</div>
    <div class="val" style="font-size:17px" id="loginVal"><span class="badge b-idle">未检查</span></div>
    <div class="sub" id="loginHint">点击「检查登录」确认落盘登录态是否有效</div>
    <div class="sub" id="keepHint">保活：—</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">
      <button class="btn sec mini" onclick="checkLogin()">检查登录</button>
      <button class="btn sec mini" onclick="keepNow()">立即续期</button>
      <a class="btn ghost mini" href="/login">登录 →</a>
    </div>
  </div>
  <div class="kpi">
    <div class="lbl">抓取回刷窗口</div>
    <div class="val">最近 {settings.REFRESH_DAYS} 天</div>
    <div class="sub">每晚重刷以修正归因延迟</div>
  </div>
</div>

<div class="card" style="margin-top:18px">
  <h2>数据覆盖 <span class="tagline">按天入库</span></h2>
  <p class="hint">按天入库；<b>只要某天缺数据，区间分析就会按 0 计入</b>。缺哪几天可以用下方「立即抓取」补。</p>
  <div class="note">补数策略：<b>{gap_html}</b>。定时任务会把「停机期间的空洞」和「最近 {settings.REFRESH_DAYS} 天回刷」合并成一次抓取。</div>
  <div class="grid g2">{cal}</div>
  <div class="tblwrap short" style="margin-top:16px">
    <table><thead><tr><th class="l noSort">账号</th><th class="n noSort">总天数</th>
      <th class="n noSort">区间</th>{kinds_head}</tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
  </div>
</div>

<div class="card">
  <h2>手动抓取 / 补数 <span class="tagline">只补缺失，或强制覆盖</span></h2>
  <p class="hint">选择要抓的日期（只抓缺失的天；勾选「强制」会覆盖重抓）。默认抓昨天。</p>
  <div class="toolbar">
    <label class="mini">从 <input type="date" id="runStart" value="{_e(start)}"></label>
    <label class="mini">到 <input type="date" id="runEnd" value="{_e(end)}"></label>
    <label class="mini"><input type="checkbox" id="runForce"> 强制覆盖</label>
    <button class="btn" onclick="doRun()">立即抓取</button>
    <button class="btn sec" onclick="quickRun(1)">补昨天</button>
    <button class="btn sec" onclick="quickRun(3)">补近3天</button>
    <button class="btn sec" onclick="doCancel()">取消 / 解锁</button>
    <span id="runMsg" class="hint" style="margin:0"></span>
  </div>
  <div class="note">说明：京准通是「点击后 15 天归因」，昨天的数据在未来两周还会增长，所以每晚都会<b>回刷最近 {settings.REFRESH_DAYS} 天</b>。商智数据当天结算后不再变化。</div>
</div>

<div class="card">
  <h2>报表说明 <span class="tagline">区间 → 环比</span></h2>
  <p class="hint">
    顶部选择任意区间 → 「生成分析」。报表会以<b>所选区间</b>为最新一期，自动往前取两个等长区间做<b>环比</b>，
    并包含：商品(SKU)跨账号交叉、计划明细、<b>操作日志复盘</b>（每条调整对应前后 7 天效果）、搜索词诊断、行动清单。
  </p>
  <div class="grid g2">
    <div class="note" style="margin-top:0">示例：选 <code>{_e((_yesterday()-dt.timedelta(days=2)).isoformat())}</code> ~ <code>{_e(_yesterday().isoformat())}</code>
      就是「近 3 天」聚合报告，对比前 3 天与前 6 天。</div>
    <div class="warn" style="margin-top:0">区间越长，越早的日期受归因延迟影响越小；<b>看绝对效率用 7~14 天，看即时反应用 1~3 天</b>。</div>
  </div>
</div>

<div class="foot">
  接口来源：京准通（概览/智能投放/关键词/搜索词/操作日志）+ 商智（商品明细/流量概况）<br>
  数据目录 <code>{_e(settings.DATA_DIR)}</code> ｜ 登录态 <code>{_e(settings.AUTH_DIR)}</code>
</div>
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
    return (accs.length>1 ? '<span class="mini" style="color:var(--muted)">'+(a.label||a.key)+' </span>' : '')+b;
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
      '<div class="card" style="margin:0;border-color:var(--amber);background:var(--amberbg)">'
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
    start, end = ui_range(q.get("start"), q.get("end"), q.get("days"), 7)
    accounts = account_list(q.getlist("accounts"))
    keys = [a["key"] for a in accounts]
    _ensure_industry(keys, start, end)
    t0 = time.time()
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
    ui = _range_bar(start, end, keys, "/report",
                    extra='<a href="/">← 控制台</a>'
                          '<a href="/ai?start=%s&end=%s&accounts=%s">AI 分析台（可追问）→</a>'
                          '<a href="/api/analysis?start=%s&end=%s&accounts=%s" target="_blank">看 JSON</a>'
                          % (_e(start), _e(end), _e(",".join(keys)),
                             _e(start), _e(end), _e(",".join(keys))))
    title = f"广告运营分析 {start} ~ {end}（{(dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1}天）"
    cached = analyze_ai.load_latest()
    ai = cached if analyze_ai.matches(cached, start, end, keys) else None
    t1 = time.time()
    html_out = report2.render(data, ui=ui, title=title, ai=ai)
    print(f"[report] {start}~{end} 账号={','.join(keys)} build {t1 - t0:.2f}s "
          f"render {time.time() - t1:.2f}s 成本={(settings.COSTS_SOURCE or {}).get('kind')}",
          flush=True)
    return HTMLResponse(html_out)


# ================================================================ AI 分析台
@app.get("/ai", response_class=HTMLResponse)
def ai_console(request: Request):
    """独立 AI 分析台：左侧每日详细 AI 报告，右侧可追问的对话框。"""
    q = request.query_params
    start, end = ui_range(q.get("start"), q.get("end"), q.get("days"), 7)
    accounts = account_list(q.getlist("accounts"))
    keys = [a["key"] for a in accounts]
    _ensure_industry(keys, start, end)
    try:
        data = analyze2.build(start=start, end=end, accounts=accounts)
    except FileNotFoundError as exc:
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>缺少数据</title>
<style>{DASH_CSS}</style></head><body><div class="wrap" style="padding-top:40px">
<div class="card"><h2>该区间没有数据</h2>
<div class="warn">{_e(exc)}</div>
<p class="hint">区间 {_e(start)} ~ {_e(end)} 内没有任何一天的京准通数据。</p>
<p><a class="btn" href="/">← 回到控制台</a></p></div></div></body></html>""",
            status_code=200)
    ui = _range_bar(start, end, keys, "/ai")
    title = f"AI 运营分析台 {start} ~ {end}"
    cached = analyze_ai.load_latest()
    ai = cached if analyze_ai.matches(cached, start, end, keys) else None
    return HTMLResponse(ai_page.render(ai, data, ui=ui, title=title))


# ================================================================ 花费一览
# 两个独立页面（不混在一张表里）：
#   /paycomm        CPS 花费（数据看板 → 商品数据的付款佣金）
#   /paycomm/spend  京准通花费（智能投放 + 全站智能推广，按 SKU）
# 抓取/限速/导出的实现在 jd_roi/paycomm.py，这里只做页面与接口。
PAYCOMM_KINDS = {
    "cps": {
        "nav": "/paycomm",
        "api": "/api/paycomm",
        "title": "CPS 花费",
        "eyebrow": "花费一览 · CPS",
        "metaline": ["京准通「数据看板 → 商品数据」口径",
                     "按 SKU 逐个取「付款佣金金额」",
                     "请求已限速（每个 SKU 间隔 0.8~1.6 秒）"],
        "switch": ("/paycomm/spend", "看京准通花费 →"),
        "webKeys": ["payCommAmount", "payCommRate", "preCommAmount", "clicks", "uv",
                    "orderCnt", "orderGmv", "fiOrderCnt", "orderUserCnt", "roi"],
        "kpis": [
            {"label": "SKU 数", "key": None, "type": "int"},
            {"label": "付款佣金合计", "key": "payCommAmount", "type": "money",
             "sub": "与页面「商品数据」同口径"},
            {"label": "预估佣金合计", "key": "preCommAmount", "type": "money"},
            {"label": "下单订单金额", "key": "orderGmv", "type": "money"},
        ],
        "sort": "payCommAmount",
        "ratioKeys": ["payCommRate", "orderCvr", "roi"],
        "showUnattr": False,
    },
    "spend": {
        "nav": "/paycomm/spend",
        "api": "/api/paycomm/spend",
        "title": "京准通花费",
        "eyebrow": "花费一览 · 京准通",
        "metaline": ["快车：智能投放 / 关键词 / 全站智能推广（含已删计划）",
                     "全站营销：单品推广按 sxuId",
                     "对不上商品的花费单列「未归因」，不摊到商品",
                     "约 30 秒完成，结果写本地缓存"],
        "grid": "g6",
        "switch": ("/paycomm", "看 CPS 花费 →"),
        "webKeys": ["jztCost", "jztJstCost", "jztKwCost", "jztShopCost", "jztSwaCost"],
        "kpis": [
            {"label": "SKU 数", "key": None, "type": "int"},
            {"label": "京准通花费合计", "key": "jztCost", "type": "money", "subKind": "spend"},
            {"label": "智能投放", "key": "jztJstCost", "type": "money"},
            {"label": "关键词计划", "key": "jztKwCost", "type": "money"},
            {"label": "全站智能推广", "key": "jztShopCost", "type": "money"},
            {"label": "全站营销", "key": "jztSwaCost", "type": "money"},
        ],
        "sort": "jztCost",
        "ratioKeys": [],
        "showUnattr": True,
    },
}

PAYCOMM_NOTES = {
    "cps": """
<p><b>付款佣金金额</b> = 已结算/付款口径的佣金（接口 <code>payCommAmount</code>），与「数据看板 → 商品数据」页面一致；
<b>预估佣金</b>是下单时预估、后续可能因退款变化。</p>
<p>区间内没有任何 CPS 成交的商品不会出现在列表里。抓取按 SKU 串行（每个 SKU 间隔 0.8~1.6 秒），
100 个 SKU 约 2~3 分钟，全程占用浏览器登录态，期间不要同时扫码登录。</p>""",
    "spend": """
<p><b>智能投放</b>：智能投放「商品报表」按 SKU 的花费（<code>/reweb/jst/account/sku/list</code>），含已下线计划。</p>
<p><b>关键词计划</b>：先取快车推广管理的创意接口（<code>/dspad/msa/promolist/item/keyword/ad</code>），
它直接返回每条创意推的 <code>skuId</code>（要进「计划 → 单元 → 创意」才看得到，这里一次拿全）；
拿不到时退回报表中心创意报表（<code>/reweb/msa/base/ad/list</code>）的落地页 SKU，
那个报表<b>包含已删除的计划</b>（例如 8 月有一个已删关键词计划 6,012 元仍在花费）。</p>
<p><b>全站智能推广</b>：以报表中心计划报表（<code>/reweb/msa/base/campaign/list</code>）的计划级金额为准，
<b>含已删除计划</b>。计划按 SPU 投放、SPU 下的 SKU 可拉黑，所以再查推广管理
（<code>/dspad/smartmax/forbidden/list</code>）拿「生效 SKU」（SPU 全部 SKU − 黑名单），花费在生效 SKU 间<b>平分</b>。
拿不到名单的退回整笔记 spuId；已删计划拿不到 spuId 时按同名计划回找。</p>
<p><b>全站营销</b>：报表（<code>/reweb/swa/account/campaign/list</code>）的单品推广只给到 SPU，
真正投放的是该 SPU 下没被拉黑的 SKU——再查推广管理（<code>/swa/ad/list</code> +
<code>/swa/forbidden/list</code>）拿 <code>deliverySkuIds</code>，一个计划投几个 SKU 就<b>平分</b>（尾差给最后一个）。
全店/新品推广没有 SKU，进「未归因」。</p>
<p>快车三路 + 全站营销 = 「京准通花费合计」，应等于京准通首页的「效果广告花费」
（快车 + 全站营销 + 站外广告）。商品名优先用数据看板的商品名；该区间没有 CPS 成交时，
用广告计划名里的商品名兜底显示。定位不到 SKU 的花费一律单列在下方「未归因」表格里，<b>绝不按比例摊到商品上</b>。</p>""",

}

PAYCOMM_CSS = """
.pcbar{height:8px;background:var(--hair);border-radius:9999px;overflow:hidden;margin-top:14px}
.pcbar i{display:block;height:100%;width:0;background:var(--primary);transition:width .35s ease}
#pcTable th{cursor:pointer;user-select:none;white-space:nowrap}
.tblwrap.long{max-height:72vh;overflow:auto}
.tblwrap.long thead th{position:sticky;top:0;background:var(--canvas);z-index:1}
td.nm{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.hot{font-weight:700;color:var(--ink)}
.toolbar .fld{display:inline-flex;align-items:center;gap:6px;margin:0 10px 8px 0}
.grid.g5{grid-template-columns:repeat(5,minmax(0,1fr))}
.grid.g6{grid-template-columns:repeat(6,minmax(0,1fr))}
@media(max-width:1100px){.grid.g5,.grid.g6{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:760px){.grid.g5,.grid.g6{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""

# ---------------------------------------------------------------- 快捷区间
# 花费一览（CPS / 京准通）与月度利润共用的时间快捷按钮：昨天 / 本月 / 上月 / 上上月。
# 不放「今天」：花费当天不结算、商智成交只到昨天，这个区间查不出东西。
# 基准日期由服务端注入（window.QRANGE_TODAY），避免浏览器时区或页面开着过夜把日期算错。
QUICK_PRESETS = [("yesterday", "昨天", "昨天一天"),
                 ("month", "本月", "本月 1 号 ~ 昨天（今天数据没出全，不含今天）"),
                 ("lastmonth", "上月", "上个自然月整月"),
                 ("prevmonth", "上上月", "上上个自然月整月")]

QUICK_RANGE_CSS = """
.qrow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:-4px 0 12px}
.qrow .qlbl{font-size:12px;color:var(--muted);margin-right:2px}
.qrow .chip.on{background:var(--surface-card);color:var(--ink);border-color:var(--cream);font-weight:600}
"""

QUICK_RANGE_JS = r"""
window.QRANGE = (function(){
  var BASE = window.QRANGE_TODAY || '';
  function pad(n){ return (n<10?'0':'')+n; }
  function iso(d){ return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()); }
  function srvToday(){
    var p = String(BASE).split('-');
    if(p.length !== 3) return null;
    var d = new Date(+p[0], +p[1]-1, +p[2]);
    return isNaN(d) ? null : d;
  }
  function today(){
    var n = new Date();
    var local = new Date(n.getFullYear(), n.getMonth(), n.getDate());
    var srv = srvToday();
    // 页面开着过夜时跟着浏览器当天走；只在浏览器更晚时前进，不倒退
    return (srv && local > srv) ? local : (srv || local);
  }
  function span(k){
    var t = today(), y = t.getFullYear(), m = t.getMonth(), d = t.getDate();
    if(k === 'yesterday') return [iso(new Date(y,m,d-1)), iso(new Date(y,m,d-1))];
    if(k === 'month'){
      // 本月 = 1 号 ~ 昨天：今天数据没出全（商智只到昨天、花费当天不结算）。
      // 每月 1 号本月还没有完整一天，这时退回「昨天」，不给自己点出一个空区间。
      var yday = new Date(y,m,d-1);
      return (d === 1) ? [iso(yday), iso(yday)] : [iso(new Date(y,m,1)), iso(yday)];
    }
    if(k === 'lastmonth') return [iso(new Date(y,m-1,1)), iso(new Date(y,m,0))];
    if(k === 'prevmonth') return [iso(new Date(y,m-2,1)), iso(new Date(y,m-1,0))];
    return null;
  }
  function mark(box, s, e){
    var nodes = document.querySelectorAll(box + ' .chip[data-k]');
    for(var i=0;i<nodes.length;i++){
      var r = span(nodes[i].getAttribute('data-k'));
      nodes[i].className = 'chip' + ((r && s && e && r[0] === s && r[1] === e) ? ' on' : '');
    }
  }
  return {span: span, mark: mark};
})();
"""


def _quick_html(prefix: str) -> str:
    """快捷区间按钮组。prefix 是页面 JS 命名空间（PC / PF）。"""
    out = ['<span class="qlbl">快捷区间</span>']
    for k, lab, hint in QUICK_PRESETS:
        out.append(f'<button type="button" class="chip" data-k="{k}" '
                   f'title="{lab} = {hint}，点一下直接查询（有缓存秒出）" '
                   f'onclick="{prefix}.quick(\'{k}\')">{lab}</button>')
    return f'<div class="qrow" id="{prefix.lower()}Quick">{"".join(out)}</div>'


PAYCOMM_JS = """
(function(){
var INIT = window.PC_INIT || {};
var API = INIT.api || '/api/paycomm';
var FIELDS = INIT.fields || [];
var WEB = INIT.webKeys || [];
var KPIS = INIT.kpis || [];
var RATIO = INIT.ratioKeys || [];
var LBL = {}, TYP = {};
FIELDS.forEach(function(f){ LBL[f[0]] = f[1]; TYP[f[0]] = f[2]; });
var DIM = 'sku', JOB = null, ROWS = [], SPEND = {}, WARN = [],
    SORT = {k: (INIT.sort || 'payCommAmount'), d: -1}, TIMER = null;

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
function num(v){ if(v===null||v===undefined||v==='') return null; var n=Number(v); return isNaN(n)?null:n; }
function fmt(v,t){ var n=num(v); if(n===null) return '—';
  if(t==='int') return n.toLocaleString('zh-CN');
  return n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function msg(s,kind){ var el=$('pcMsg'); if(!el) return; el.textContent=s||'';
  el.style.color = kind==='bad' ? 'var(--red)' : (kind==='ok' ? 'var(--greenink)' : 'var(--muted)'); }
function kpi(lbl,val,sub){ return '<div class="kpi"><div class="lbl">'+esc(lbl)+'</div><div class="val">'+val+'</div>'
  + (sub?'<div class="sub">'+esc(sub)+'</div>':'') + '</div>'; }

function cols(){
  if(DIM !== 'cat') return WEB;
  return WEB.filter(function(k){ return RATIO.indexOf(k) < 0; });
}
function aggregate(list){
  var map = {}, order = [];
  list.forEach(function(r){
    var k = r.category || '未分类';
    var b = map[k];
    if(!b){
      b = {category: k, __n: 0};
      cols().forEach(function(kk){ b[kk] = 0; });
      map[k] = b; order.push(k);
    }
    b.__n += 1;
    cols().forEach(function(kk){ var v = num(r[kk]); if(v !== null){ b[kk] += v; } });
  });
  return order.map(function(k){
    var b = map[k];
    cols().forEach(function(kk){ b[kk] = Math.round(b[kk]*100)/100; });
    return b;
  });
}
function head(){
  var cat = (DIM === 'cat');
  var h = cat
    ? '<tr><th class="l noSort" data-key="category">品类</th>'
      + '<th class="n noSort" data-key="__n">SKU数</th>'
    : '<tr><th class="l noSort" data-key="skuId">SKU</th>'
      + '<th class="l noSort" data-key="itemName">商品名称</th>';
  cols().forEach(function(k){
    var mark = SORT.k===k ? (SORT.d<0?' ↓':' ↑') : '';
    h += '<th class="n noSort" data-key="' + k + '">' + esc(LBL[k]||k) + mark + '</th>';
  });
  return h + '</tr>';
}
function rowsHtml(list){
  var cat = (DIM === 'cat');
  var span = cols().length + 2;
  if(!list.length) return '<tr><td colspan="'+span+'" class="l">没有匹配的行</td></tr>';
  return list.map(function(r){
    var tds = cols().map(function(k){
      var hot = (k==='payCommAmount'||k==='jztCost') ? ' hot' : '';
      return '<td class="n'+hot+'">'+fmt(r[k],TYP[k])+'</td>';
    }).join('');
    if(cat){
      return '<tr><td class="l"><b>'+esc(r.category)+'</b></td>'
        + '<td class="n">'+r.__n+'</td>'+tds+'</tr>';
    }
    return '<tr><td class="l"><b>'+esc(r.skuId)+'</b></td>'
      + '<td class="l nm" title="'+esc(r.itemName)+'">'+esc(r.itemName || '—')+'</td>'+tds+'</tr>';
  }).join('');
}
function kpisHtml(){
  var out = '';
  KPIS.forEach(function(k){
    var val, sub = k.sub || '';
    if(!k.key){
      val = ROWS.length.toLocaleString('zh-CN');
    } else {
      var s = 0;
      ROWS.forEach(function(r){ s += num(r[k.key]) || 0; });
      val = (k.type === 'int' ? fmt(s,'int') : ('¥' + fmt(s,'money')));
    }
    if(k.subKind === 'spend'){
      var st = SPEND.totals || {};
      var un = num(st.unattr);
      if(un === null) un = (num(st.keyword) || 0) + (num(st.other) || 0);
      sub = un ? ('未归因 ' + fmt(un,'money') + ' 元（无 SKU）')
               : '全部已按 SKU 归因';
    }
    out += kpi(k.label, val, sub);
  });
  return out;
}
function render(){
  var cat = (DIM === 'cat');
  var kw = ($('pcFilter').value||'').trim().toLowerCase();
  var base = cat ? aggregate(ROWS) : ROWS;
  var list = base.filter(function(r){
    if(!kw) return true;
    if(cat) return String(r.category||'').toLowerCase().indexOf(kw) >= 0;
    return String(r.skuId).indexOf(kw) >= 0
      || String(r.itemName||'').toLowerCase().indexOf(kw) >= 0
      || String(r.category||'').toLowerCase().indexOf(kw) >= 0;
  });
  list = list.slice().sort(function(a,b){
    var k = SORT.k;
    if(cat && (k==='skuId' || k==='itemName' || k==='category')){
      return String(a.category||'').localeCompare(String(b.category||''))*SORT.d;
    }
    if(k==='itemName') return String(a[k]||'').localeCompare(String(b[k]||''))*SORT.d;
    if(k==='skuId') return String(a.skuId).localeCompare(String(b.skuId))*SORT.d;
    var x = num(a[k]), y = num(b[k]);
    if(x===null) x = -1e18;
    if(y===null) y = -1e18;
    return (x-y)*SORT.d;
  });
  $('pcHead').innerHTML = head();
  $('pcBody').innerHTML = rowsHtml(list);
  $('pcKpis').innerHTML = kpisHtml();
  $('pcKpis').style.display = '';
  if($('pcWarn')){
    var ws = (SPEND && SPEND.warnings) || WARN || [];
    if(ws.length){ $('pcWarn').innerHTML = '⚠ ' + ws.map(esc).join('<br>⚠ '); $('pcWarn').style.display=''; }
    else { $('pcWarn').innerHTML = ''; $('pcWarn').style.display='none'; }
  }
  var cnt = cat ? ('共 '+list.length+' 个品类 / '+ROWS.length+' 个 SKU')
                : ('共 '+ROWS.length+' 个 SKU'
                   + (list.length!==ROWS.length ? ('，筛选后 '+list.length+' 行') : ''));
  $('pcCount').textContent = cnt;
  renderUnattr();
}
function renderUnattr(){
  var us = SPEND.unattrRows || [];
  var kws = SPEND.keywordRows || [];
  if(!INIT.showUnattr || (!us.length && !kws.length)){
    if($('pcUnattrCard')) $('pcUnattrCard').style.display = 'none';
    return;
  }
  var uh = '', ut = 0;
  us.forEach(function(u){
    ut += num(u.cost) || 0;
    uh += '<tr><td class="l nm">'+esc(u.name)+'</td><td class="n">'+fmt(u.cost,'money')+'</td></tr>';
  });
  if(us.length){
    uh += '<tr><td class="l"><b>合计</b></td><td class="n"><b>'+fmt(ut,'money')+'</b></td></tr>';
  }
  $('pcUnattrBody').innerHTML = uh;
  var kwBox = $('pcUnattrKw');
  if(kwBox){
    if(kws.length){
      var kh = kws.map(function(c){
        return '<tr><td class="l">'+esc(c.campaignId)+'</td><td class="l nm">'+esc(c.campaignName)
          + '</td><td class="n">'+fmt(c.cost,'money')+'</td></tr>';
      }).join('');
      var kt = 0; kws.forEach(function(c){ kt += num(c.cost)||0; });
      kh += '<tr><td class="l"><b>合计</b></td><td class="l">'+kws.length+' 个创意</td>'
          + '<td class="n"><b>'+fmt(kt,'money')+'</b></td></tr>';
      $('pcUnattrKwBody').innerHTML = kh;
      kwBox.style.display = '';
    } else {
      kwBox.style.display = 'none';
    }
  }
  $('pcUnattrCard').style.display = '';
}
function dim(k){
  DIM = (k === 'cat') ? 'cat' : 'sku';
  $('pcDimSku').className = 'tab' + (DIM === 'sku' ? ' active' : '');
  $('pcDimCat').className = 'tab' + (DIM === 'cat' ? ' active' : '');
  $('pcFilter').placeholder = (DIM === 'cat') ? '筛选品类' : '筛选 SKU / 商品名 / 品类';
  if($('pcTableTitle')) $('pcTableTitle').textContent = (DIM === 'cat') ? '品类汇总' : 'SKU 明细';
  if(ROWS.length) render();
}
function showTable(){ $('pcTableCard').style.display=''; $('pcExport').disabled=false; render(); }

var POLL_FAILS = 0;
function poll(silent){
  fetch(API + '/status?rows=1').then(function(r){ return r.json(); }).then(function(j){
    POLL_FAILS = 0;
    if(!j.ok){ msg(j.error||'状态查询失败','bad'); return; }
    if(j.active){
      if(j.jobId) JOB = j.jobId;
      $('pcRun').disabled = true;
      $('pcRefetch').disabled = true;
      $('pcCancel').style.display = '';
      var pct = j.total ? Math.round(j.done*100/j.total) : 0;
      $('pcBar').style.width = pct+'%';
      // 「全部账号」部分命中：先把已有缓存的账号照出来，缺的账号后台补抓，抓完自动整体刷新
      if(j.partial && j.rows && j.rows.length){
        var rg0 = j.range || [];
        if(!rg0.length || (rg0[0] === $('pcStart').value && rg0[1] === $('pcEnd').value)){
          ROWS = j.rows; SPEND = j.spend || {};
          WARN = (SPEND && SPEND.warnings) || j.warnings || [];
          showTable();
        }
      }
      msg((j.message||'抓取中') + (j.total ? ('　'+j.done+'/'+j.total+'（'+pct+'%）') : ''));
      clearTimeout(TIMER); TIMER = setTimeout(poll, 1500);
      return;
    }
    $('pcRun').disabled = false;
    $('pcRefetch').disabled = false;
    $('pcCancel').style.display = 'none';
    $('pcBar').style.width = j.error ? '0' : '100%';
    if(!silent){
      if(j.error){
        msg(j.message || j.error, 'bad');
        // 失败时清掉上一次（可能是别的账号）的 KPI 与表格，避免误读成本账号数据
        ROWS = []; SPEND = {};
        if($('pcKpis')){ $('pcKpis').innerHTML = ''; $('pcKpis').style.display = 'none'; }
        $('pcTableCard').style.display = 'none'; $('pcCount').textContent = '';
      }
      else if(j.message){ msg(j.message, 'ok'); }
    }
    if(j.rows && j.rows.length){
      var rg = j.range || [];
      var same = rg.length ? (rg[0] === $('pcStart').value && rg[1] === $('pcEnd').value) : true;
      if(same){
        JOB = j.jobId; ROWS = j.rows; SPEND = j.spend || {};
        WARN = (SPEND && SPEND.warnings) || j.warnings || [];
        showTable();
      } else {
        msg('本页最近一次结果是 ' + rg[0] + ' ~ ' + rg[1] + '；当前区间点「查询」即可（有缓存秒出）');
      }
    }
  }).catch(function(e){
    POLL_FAILS += 1;
    if(POLL_FAILS <= 4){
      msg('状态查询失败（' + e + '），2 秒后重试…');
      clearTimeout(TIMER); TIMER = setTimeout(poll, 2000);
      return;
    }
    // 连续失败：多半是服务在重启，恢复按钮，别把页面卡死
    $('pcRun').disabled = false;
    $('pcRefetch').disabled = false;
    $('pcCancel').style.display = 'none';
    $('pcBar').style.width = '0';
    msg('与服务端的连接中断了（常见于服务重启）。刷新页面即可恢复；后台任务不受影响。', 'bad');
  });
}
function markQuick(){
  if(!window.QRANGE) return;
  window.QRANGE.mark('#pcQuick', $('pcStart').value, $('pcEnd').value);
}
function quick(k){
  var r = window.QRANGE && window.QRANGE.span(k);
  if(!r) return;
  $('pcStart').value = r[0];
  $('pcEnd').value = r[1];
  markQuick();
  run();
}
function run(force){
  var acc = $('pcAcc').value, s = $('pcStart').value, e = $('pcEnd').value;
  if(!s || !e){ msg('请选择起止日期','bad'); return; }
  if(s > e){ msg('起始日期不能晚于结束日期','bad'); return; }
  ROWS = []; JOB = null; SPEND = {};
  $('pcTableCard').style.display = 'none';
  if($('pcKpis')){ $('pcKpis').innerHTML = ''; $('pcKpis').style.display = 'none'; }
  if($('pcWarn')){ $('pcWarn').style.display = 'none'; $('pcWarn').innerHTML = ''; }
  $('pcCount').textContent = '';
  $('pcExport').disabled = true;
  $('pcRun').disabled = true;
  $('pcRefetch').disabled = true;
  $('pcBar').style.width = '0';
  msg(force ? '正在重新抓取（会覆盖该区间的本地缓存）…' : '查询中（先看本地缓存）…');
  var qs = new URLSearchParams({account: acc, start: s, end: e});
  if(force) qs.set('force', '1');
  fetch(API + '/run?' + qs.toString(), {method:'POST'}).then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){
        msg('失败：'+(j.error||''),'bad');
        $('pcRun').disabled=false; $('pcRefetch').disabled=false; return;
      }
      JOB = j.jobId;
      if(j.cached){
        if(j.partial){
          msg('先用已缓存的 ' + (j.rows||0) + ' 个 SKU 出表；'
              + (j.missing||[]).join('、') + ' 还没有该区间缓存，正在后台补抓（抓完自动刷新）');
        } else {
          msg(j.stitched
            ? ('命中本地缓存拼接：' + (j.pieces||[]).join(' + ') + '（共 ' + (j.rows||0) + ' 个 SKU），未请求京准通')
            : ('命中本地缓存（' + (j.savedAt||'') + ' 抓取，共 ' + (j.rows||0) + ' 个 SKU），未请求京准通'));
        }
      } else {
        msg('该区间没有完整缓存，已开始在后台抓取（约 20 秒 / 或几分钟）；完成后自动写入缓存');
      }
      poll();
    }).catch(function(e){
      msg('请求失败 '+e,'bad');
      $('pcRun').disabled=false; $('pcRefetch').disabled=false;
    });
}
function refetch(){
  var s = $('pcStart').value, e = $('pcEnd').value;
  var tip = '重新抓取 ' + s + ' ~ ' + e + '，会真实请求京准通并覆盖该区间缓存。继续？';
  if(!confirm(tip)) return;
  run(true);
}
function cancel(){
  fetch(API + '/cancel', {method:'POST'}).then(function(r){ return r.json(); })
    .then(function(j){ msg(j.cancelRequested ? '已请求取消，当前 SKU 取完后停止' : '当前没有任务'); });
}
function expView(){
  if(!JOB){ msg('还没有数据，先点「查询」','bad'); return; }
  var qs = new URLSearchParams({job: JOB, account: $('pcAcc').value,
    start: $('pcStart').value, end: $('pcEnd').value, dim: DIM});
  window.location = API + '/export?' + qs.toString();
}
function exp(){
  var s = $('pcStart').value, e = $('pcEnd').value;
  if(!JOB && !s){ msg('还没有数据，先点「查询」','bad'); return; }
  var qs = new URLSearchParams({job: JOB || '', account: $('pcAcc').value, start: s, end: e});
  window.location = API + '/export?' + qs.toString();
}
function sort(k){
  if(SORT.k === k) SORT.d = -SORT.d;
  else SORT = {k: k, d: (k==='itemName'||k==='skuId') ? 1 : -1};
  render();
}
window.PC = {sort: sort, run: run, refetch: refetch, exp: exp, cancel: cancel,
             render: render, dim: dim, expView: expView, quick: quick};
function boot(){
  poll();
  // 从「缓存管理」点「查看」带区间打开：自动查一次，有缓存就直接秒出
  if(INIT.auto) run();
  $('pcTable').addEventListener('click', function(e){
    var th = e.target.closest('th');
    if(!th || !th.getAttribute('data-key')) return;
    sort(th.getAttribute('data-key'));
  });
  $('pcStart').addEventListener('change', markQuick);
  $('pcEnd').addEventListener('change', markQuick);
  markQuick();
}
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
})();
"""


def _paycomm_page(kind: str, request: Request) -> HTMLResponse:
    cfg = PAYCOMM_KINDS[kind]
    q = request.query_params
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = q.get("account") or settings.MAIN_ACCOUNT
    if acc != "all" and acc not in keys:
        acc = settings.MAIN_ACCOUNT
    start, end = _last_month_range()
    if q.get("start") and q.get("end"):
        try:
            start, end = paycomm.norm_range(q.get("start"), q.get("end"))
        except ValueError:
            pass
    opts = ('<option value="all"%s>全部账号（总览）</option>'
            % (" selected" if acc == "all" else "")) + "".join(
        f'<option value="{_e(a["key"])}"{" selected" if a["key"] == acc else ""}>'
        f'{_e(a.get("label"))}</option>' for a in settings.ACCOUNTS)
    fields = paycomm.FIELDS if kind == "cps" else paycomm.SPEND_FIELDS
    init = {"mode": kind, "api": cfg["api"], "sort": cfg["sort"],
            # 带 start/end 打开（缓存管理点「查看」）：进页面自动查一次，命中缓存直接秒出
            "auto": bool(q.get("start") and q.get("end")),
            "fields": [[k, lab, t] for k, lab, t in fields],
            "webKeys": cfg["webKeys"], "kpis": cfg["kpis"], "showUnattr": cfg["showUnattr"],
            "ratioKeys": cfg.get("ratioKeys", [])}
    unattr_html = """
<div class="card" id="pcUnattrCard" style="display:none">
  <h2>未归因花费 <span class="tagline">定位不到 SKU 的钱单列在这里，绝不按比例摊到商品</span></h2>
  <p class="hint">这些花费已经在合计里（所以能对上京准通），但没有落到具体商品上；
     要缩小这块，只能让计划带上 spuId / 创意带落地页 / 全站营销走单品推广。</p>
  <div class="tblwrap short">
    <table><thead><tr><th class="l noSort">项目</th>
      <th class="n noSort">花费(元)</th></tr></thead><tbody id="pcUnattrBody"></tbody></table>
  </div>
  <div id="pcUnattrKw" style="display:none;margin-top:16px">
    <h2>未归因的关键词创意明细</h2>
    <div class="tblwrap short">
      <table><thead><tr><th class="l noSort">计划ID</th><th class="l noSort">创意/计划名称</th>
        <th class="n noSort">花费(元)</th></tr></thead><tbody id="pcUnattrKwBody"></tbody></table>
    </div>
  </div>
</div>""" if cfg["showUnattr"] else ""
    metaline = "".join(
        f'<span>{_e(t)}</span>'
        + ('<span class="dot"></span>' if i < len(cfg["metaline"]) - 1 else "")
        for i, t in enumerate(cfg["metaline"]))
    quick_html = _quick_html("PC")
    today_iso = _today().isoformat()
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(cfg["title"])} · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{PAYCOMM_CSS}</style>
<style>{QUICK_RANGE_CSS}</style>
</head><body>
{report2.navtop(cfg["nav"])}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}{_e(cfg["eyebrow"])}</div>
  <h1>{_e(cfg["title"])}</h1>
  <div class="metaline">{metaline}</div>
  <div class="actions">
    <a class="btn ghost mini" href="{_e(cfg["switch"][0])}">{_e(cfg["switch"][1])}</a>
    <a class="btn ghost mini" href="/cache?kind={kind}">缓存管理 →</a>
    <a class="btn ghost mini" href="/login">登录 / 检查登录态</a>
  </div>
</div></header>
<div class="wrap">

<div class="card">
  <div class="toolbar">
    <label class="fld">账号 <select id="pcAcc">{opts}</select></label>
    <label class="fld">起 <input type="date" id="pcStart" value="{_e(start)}"></label>
    <label class="fld">止 <input type="date" id="pcEnd" value="{_e(end)}"></label>
    <button class="btn" id="pcRun" onclick="PC.run()">查询</button>
    <button class="btn ghost" id="pcRefetch" onclick="PC.refetch()" title="忽略本地缓存，重新请求京准通">重新抓取</button>
    <button class="btn sec" id="pcExport" onclick="PC.exp()" disabled>导出完整 Excel</button>
    <button class="btn ghost" id="pcCancel" onclick="PC.cancel()" style="display:none">取消</button>
  </div>
{quick_html}
  <div class="hint" id="pcMsg" style="min-height:20px">选账号与区间后点「查询」：有缓存直接读本地（不请求京准通），没有才去抓。</div>
  <div class="pcbar" id="pcBarWrap"><i id="pcBar"></i></div>
</div>

<div class="grid {cfg.get("grid", "g4")}" id="pcKpis" style="display:none;margin-top:18px"></div>
<div class="hint" id="pcWarn" style="display:none;margin-top:10px;color:var(--red)"></div>

<div class="card" id="pcCacheCard">
  <h2>本地缓存 <span class="tagline">不自动清理，删除由你决定</span></h2>
  <p class="hint">抓一次存一份：<code>{_e(str(settings.DATA_DIR))}/reports/</code>。
  同一账号 + 同一区间再点「查询」直接读文件，<b>不会请求京准通</b>；需要最新数据时点「重新抓取」覆盖这一份。
  没有任何自动过期、也没有自动清理逻辑。</p>
  <p class="hint">缓存清单（按时间 / 按账号、搜索、删除、旧口径清理）已独立成页：</p>
  <p><a class="btn ghost" href="/cache?kind={kind}">打开缓存管理 →</a></p>
</div>

<div class="card" id="pcTableCard" style="display:none">
  <h2><span id="pcTableTitle">SKU 明细</span> <span class="tagline">点表头可换排序</span></h2>
  <div class="toolbar">
    <span class="tabs" style="margin:0 12px 8px 0">
      <button type="button" class="tab active" id="pcDimSku" onclick="PC.dim('sku')">按 SKU</button>
      <button type="button" class="tab" id="pcDimCat" onclick="PC.dim('cat')">按品类</button>
    </span>
    <input id="pcFilter" placeholder="筛选 SKU / 商品名 / 品类" oninput="PC.render()"
           style="min-width:260px;padding:7px 10px;border-radius:8px;border:1px solid var(--line)">
    <span class="hint" id="pcCount"></span>
    <button class="btn sec mini" id="pcExportView" onclick="PC.expView()"
            style="margin-left:auto" title="按当前维度导出（SKU 或 品类）">导出当前视图 Excel</button>
  </div>
  <div class="tblwrap long">
    <table id="pcTable"><thead id="pcHead"></thead><tbody id="pcBody"></tbody></table>
  </div>
</div>
{unattr_html}

<div class="note" style="margin-top:18px">{PAYCOMM_NOTES[kind]}
<p><a href="{_e(cfg["switch"][0])}">{_e(cfg["switch"][1])}</a>　·　
<a href="/login">登录 / 检查登录态</a></p>
</div>

</div>
<script>window.QRANGE_TODAY = "{today_iso}";</script>
<script>{QUICK_RANGE_JS}</script>
<script>window.PC_INIT = {json.dumps(init, ensure_ascii=False)};</script>
<script>{PAYCOMM_JS}</script>
</body></html>""")


@app.get("/paycomm", response_class=HTMLResponse)
def paycomm_page(request: Request):
    """CPS 花费：数据看板 → 商品数据的付款佣金（按 SKU）。"""
    return _paycomm_page("cps", request)


@app.get("/paycomm/spend", response_class=HTMLResponse)
def paycomm_spend_page(request: Request):
    """京准通花费：智能投放 + 全站智能推广（按 SKU）。"""
    return _paycomm_page("spend", request)


def _paycomm_other_running(mode: str) -> str | None:
    other = _paycomm_spend if mode == "cps" else _paycomm
    if other.snapshot().get("running"):
        label = "「京准通花费」" if mode == "cps" else "「CPS 花费」"
        return f"{label}任务正在运行，等它结束再查（两者都要占用浏览器登录态）"
    return None


def _paycomm_start(runner, account, start, end, force: bool = False) -> dict:
    """查询入口：默认先吃本地缓存（同账号+同区间），force=1 才重新请求京准通。"""
    acc = account or settings.MAIN_ACCOUNT
    if str(acc).lower() == "all":
        # 「全部账号」：先用已有的缓存秒出总览；缺的账号在后台补抓，抓完前端自动刷新。
        # 一个账号都没有缓存时才整批抓（否则页面白等几分钟）。
        if not force:
            got = runner.all_payload(start, end, allow_missing=True)
            if got is not None and got.get("rows"):
                job_id = f"all-{runner.mode}-{start}_{end}"
                got["jobId"] = job_id
                missing = [settings.get_account(k).get("label") or k
                           for k in (got.get("missing") or [])]
                runner._results[job_id] = got
                runner._state.update(
                    {"running": False, "cached": True, "jobId": job_id, "account": "all",
                     "range": [start, end], "total": len(got["rows"]),
                     "done": len(got["rows"]), "error": None,
                     "finishedAt": settings.now_iso(),
                     "message": ("命中本地缓存：各账号已合并成一张总览" if not missing else
                                 f"先用已有缓存出总览；还有 {len(missing)} 个账号在后台补抓："
                                 f"{'、'.join(missing)}")})
                if not missing:
                    return {"ok": True, "cached": True, "jobId": job_id, "rows": len(got["rows"])}
                out = {"ok": True, "cached": True, "partial": True, "jobId": job_id,
                       "rows": len(got["rows"]), "missing": missing}
                busy = _paycomm_other_running(runner.mode)
                info = scrape_day.lock_info()
                if busy or (info.get("locked") and info.get("alive")):
                    out["warnings"] = ["现在有别的抓取任务占用浏览器登录态，"
                                       "等它结束后再点「查询」会自动补抓缺的账号"]
                    return out
                started = runner.run_all_background(
                    start, end,
                    note=f"先用已有缓存出总览，正在补抓缺的 {len(missing)} 个账号…")
                if not started.get("ok"):
                    out["warnings"] = [started.get("error") or "后台补抓没能启动"]
                    return out
                # 补抓任务换了新 jobId：把这份部分结果挂上去，轮询时先显示这批数据
                got["partial"] = True
                got["missing"] = missing[:]
                got["warnings"] = list(got.get("warnings") or [])
                got["warnings"].append(f"以下账号还没有该区间缓存，正在后台补抓："
                                       f"{'、'.join(missing)}（抓完自动刷新）")
                runner._results[started["jobId"]] = got
                out["backfill"] = started["jobId"]
                return out
        busy = _paycomm_other_running(runner.mode)
        if busy:
            return {"ok": False, "error": busy}
        info = scrape_day.lock_info()
        if info.get("locked") and info.get("alive"):
            who = info.get("name") or info.get("thread") or "抓取任务"
            return {"ok": False,
                    "error": f"正在执行数据抓取（{who}，{info.get('acquiredAt') or ''} 开始），"
                             f"浏览器登录态被占用，等它结束再查"}
        note = ("「全部账号」：有账号还没有该区间缓存，正在逐个补抓…" if not force
                else "「全部账号」重新抓取中（会覆盖各账号该区间缓存）…")
        return runner.run_all_background(start, end, note=note)
    try:
        if not force:
            # 1) 精确命中（账号+起+止完全一致）  2) 多段相邻缓存拼接（8月 + 9月 → 8/1~9/30）
            for lookup in (runner.use_cache, runner.use_stitched):
                got = lookup(acc, start, end)
                if got is not None:
                    return got
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    busy = _paycomm_other_running(runner.mode)
    if busy:
        return {"ok": False, "error": busy}
    # 定时/手动抓取也开同一个浏览器 Profile：同一时刻只能有一个浏览器，否则会互相顶掉登录态
    info = scrape_day.lock_info()
    if info.get("locked") and info.get("alive"):
        who = info.get("name") or info.get("thread") or "抓取任务"
        return {"ok": False,
                "error": f"正在执行数据抓取（{who}，{info.get('acquiredAt') or ''} 开始），"
                         f"浏览器登录态被占用，等它结束再查；这期间可以先看已有缓存"}
    try:
        note = ("该区间没有完整缓存（现有缓存见下方列表），开始抓取…" if not force
                else "重新抓取中（会覆盖该区间缓存）…")
        return runner.run_background(acc, start, end, note=note)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def _paycomm_snapshot(runner, rows: int) -> dict:
    snap = runner.snapshot(with_rows=bool(rows))
    snap["ok"] = True
    snap["active"] = snap.pop("running", False)
    return snap


def _paycomm_export(runner, job: str | None, account: str | None = None,
                    start: str | None = None, end: str | None = None,
                    dim: str = "sku"):
    from urllib.parse import quote
    got = runner.export(job, account=account, start=start, end=end, dim=dim)
    if not got:
        return JSONResponse({"ok": False, "error": "没有可导出的结果，请先查询一次"}, status_code=404)
    data, fname = got
    disp = f"attachment; filename=\"paycomm.xlsx\"; filename*=UTF-8''{quote(fname)}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp, "Cache-Control": "no-store"})


@app.post("/api/paycomm/run")
def api_paycomm_run(account: str | None = None, start: str = "", end: str = "", force: int = 0):
    """CPS 花费查询：默认命中缓存直接返回，force=1 才重新抓取。"""
    return _paycomm_start(_paycomm, account, start, end, force=bool(force))


@app.get("/api/paycomm/status")
def api_paycomm_status(rows: int = 0):
    """CPS 花费进度；rows=1 时附带最近一次结果。"""
    return _paycomm_snapshot(_paycomm, rows)


@app.post("/api/paycomm/cancel")
def api_paycomm_cancel():
    return _paycomm.cancel()


@app.get("/api/paycomm/export")
def api_paycomm_export(job: str | None = None, account: str | None = None,
                       start: str | None = None, end: str | None = None,
                       dim: str = "sku"):
    return _paycomm_export(_paycomm, job, account, start, end, dim)


@app.post("/api/paycomm/spend/run")
def api_paycomm_spend_run(account: str | None = None, start: str = "", end: str = "", force: int = 0):
    """京准通花费查询：默认命中缓存直接返回，force=1 才重新抓取。"""
    return _paycomm_start(_paycomm_spend, account, start, end, force=bool(force))


@app.get("/api/paycomm/spend/status")
def api_paycomm_spend_status(rows: int = 0):
    return _paycomm_snapshot(_paycomm_spend, rows)


@app.post("/api/paycomm/spend/cancel")
def api_paycomm_spend_cancel():
    return _paycomm_spend.cancel()


@app.get("/api/paycomm/spend/export")
def api_paycomm_spend_export(job: str | None = None, account: str | None = None,
                             start: str | None = None, end: str | None = None,
                             dim: str = "sku"):
    return _paycomm_export(_paycomm_spend, job, account, start, end, dim)


# ---------------------------------------------------------------- 本地缓存
# 缓存 = data/reports/<mode>_<账号>_<区间>.json，一次抓取一份；
# 不会自动过期、不会自动清理，删除只由用户显式触发。
@app.get("/api/paycomm/cache")
def api_paycomm_cache():
    return {"ok": True, "mode": "cps", "entries": _paycomm.cache_entries()}


@app.post("/api/paycomm/cache/delete")
def api_paycomm_cache_delete(account: str | None = None, start: str = "", end: str = ""):
    if not (account and start and end):
        return {"ok": False, "error": "缺少 account / start / end"}
    return _paycomm.delete_cache(account, start, end)


@app.get("/api/paycomm/spend/cache")
def api_paycomm_spend_cache():
    return {"ok": True, "mode": "spend", "entries": _paycomm_spend.cache_entries()}


@app.post("/api/paycomm/spend/cache/delete")
def api_paycomm_spend_cache_delete(account: str | None = None, start: str = "", end: str = ""):
    if not (account and start and end):
        return {"ok": False, "error": "缺少 account / start / end"}
    return _paycomm_spend.delete_cache(account, start, end)


# ================================================================ 缓存管理（二级页）
# 四类缓存各自的落盘文件：paycomm_*.json / paycomm_spend_*.json / profit_*.json / industry_*.json
CACHE_KINDS = [("all", "全部"), ("cps", "CPS 花费"), ("spend", "京准通花费"),
               ("profit", "月度利润"), ("industry", "行业大盘"),
               ("cashflow", "广告费流水")]
CACHE_LABELS = {"cps": "CPS 花费", "spend": "京准通花费",
                "profit": "月度利润", "industry": "行业大盘",
                "cashflow": "广告费流水"}
CACHE_HREF = {"cps": "/paycomm", "spend": "/paycomm/spend",
              "profit": "/profit", "industry": "/report", "cashflow": "/cashflow"}

CACHE_CSS = """
.cgGrp{margin-bottom:18px}
.cgGrp h3{font-size:14px;margin:0 0 10px;color:var(--ink)}
.cgGrp h3 span{font-weight:400;color:var(--muted);margin-left:10px;font-size:12px}
.cgBadge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
  border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.cgBadge.warn{color:var(--red);border-color:var(--red)}
.cgBadge.legacy{border-style:dashed}
"""

CACHE_JS = r"""
(function(){
var INIT = window.CACHE_INIT || {};
var ALL = [], KIND = INIT.kind || 'all', VIEW = 'time', Q = '';
var LABEL = INIT.labels || {}, HREF = INIT.href || {};
function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
function fmtKB(kb){ var n = Number(kb||0);
  return n >= 1024 ? (n/1024).toFixed(1)+' MB' : (Math.round(n*10)/10)+' KB'; }
function money(v){ var n = Number(v||0);
  return n.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}); }
function match(en){
  if(KIND !== 'all' && en.kind !== KIND) return false;
  if(!Q) return true;
  return [en.kindLabel, en.account, en.accountLabel, en.start, en.end]
    .join(' ').toLowerCase().indexOf(Q) >= 0;
}
function sumKB(list){ var t = 0; list.forEach(function(e){ t += Number(e.sizeKB||0); }); return t; }
function badges(en){
  var out = '';
  if(en.legacy) out += ' <span class="cgBadge legacy" title="旧命名/按账号存的缓存，现在的查询不会命中">旧格式</span>';
  if(en.stale) out += ' <span class="cgBadge warn" title="口径版本已升级，这份缓存不会被命中，需要重算">口径过期</span>';
  return out;
}
function buttons(en){
  return '<button class="btn ghost mini" data-act="open" data-kind="'+esc(en.kind)+'" '
    + 'data-acc="'+esc(en.account||'')+'" data-s="'+esc(en.start)+'" data-e="'+esc(en.end)+'">查看</button> '
    + '<button class="btn danger mini" data-act="del" data-kind="'+esc(en.kind)+'" '
    + 'data-acc="'+esc(en.account||'')+'" data-s="'+esc(en.start)+'" data-e="'+esc(en.end)+'" '
    + 'data-file="'+esc((en.files||[])[0]||'')+'">删除</button>';
}
function rowsHtml(list, withKind){
  return list.map(function(en){
    return '<tr>'
      + (withKind ? '<td class="l"><span class="cgBadge">'+esc(en.kindLabel)+'</span></td>' : '')
      + '<td class="l">'+esc(en.accountLabel || en.account)+badges(en)+'</td>'
      + '<td class="l">'+esc(en.start)+' ~ '+esc(en.end)+'</td>'
      + '<td class="n">'+esc(en.rows)+'</td>'
      + '<td class="n" title="'+(en.kind==='profit'?'净利合计':(en.kind==='cps'?'付款佣金合计':(en.kind==='industry'?'本店广告花费（行业数据为指数，不计费）':(en.kind==='cashflow'?'区间消费合计':'京准通花费合计'))))+'">'
      + money(en.total)+(en.kind==='profit'?' <span class="cgBadge">净利</span>':'')+'</td>'
      + '<td class="l">'+esc(en.savedAt)+'</td>'
      + '<td class="n">'+fmtKB(en.sizeKB)+'</td>'
      + '<td class="n" style="white-space:nowrap">'+buttons(en)+'</td></tr>';
  }).join('');
}
function byTime(list, withKind){
  return '<div class="card"><div class="tblwrap long"><table><thead><tr>'
    + (withKind ? '<th class="l noSort">类型</th>' : '')
    + '<th class="l noSort">账号</th><th class="l noSort">区间</th><th class="n noSort">行数</th>'
    + '<th class="n noSort">合计(元)</th><th class="l noSort">抓取时间</th>'
    + '<th class="n noSort">大小</th><th class="n noSort">操作</th>'
    + '</tr></thead><tbody>' + rowsHtml(list, withKind) + '</tbody></table></div></div>';
}
function byAcct(list){
  var groups = {}, order = [];
  list.forEach(function(en){
    var k = en.kind + '|' + (en.account || 'all');
    if(!groups[k]){
      groups[k] = {kind: en.kind, label: en.accountLabel || en.account || '全部账号', items: []};
      order.push(k);
    }
    groups[k].items.push(en);
  });
  return order.map(function(k){
    var g = groups[k];
    return '<div class="card cgGrp"><h3>' + esc(g.label)
      + '<span>' + esc(LABEL[g.kind] || g.kind) + ' · ' + g.items.length + ' 份 · '
      + fmtKB(sumKB(g.items)) + '</span></h3>'
      + '<div class="tblwrap short"><table><thead><tr>'
      + '<th class="l noSort">区间</th><th class="n noSort">行数</th><th class="n noSort">合计(元)</th>'
      + '<th class="l noSort">抓取时间</th><th class="n noSort">大小</th><th class="n noSort">操作</th>'
      + '</tr></thead><tbody>' + rowsHtml(g.items, false) + '</tbody></table></div></div>';
  }).join('');
}
function render(){
  var list = ALL.filter(match);
  var stat = '共 ' + list.length + ' 份 · ' + fmtKB(sumKB(list));
  if(list.length !== ALL.length) stat += '（全部 ' + ALL.length + ' 份）';
  $('cgStat').textContent = stat;
  if(!list.length){
    $('cgBody').innerHTML = '<div class="card"><p class="hint">没有匹配的缓存：换个类型或清空搜索试试。</p></div>';
    return;
  }
  $('cgBody').innerHTML = (VIEW === 'acct')
    ? byAcct(list)
    : byTime(list, KIND === 'all');
}
function syncTabs(){
  var nodes = document.querySelectorAll('#cgKinds [data-kind]');
  for(var i=0;i<nodes.length;i++){
    nodes[i].className = 'tab' + (nodes[i].getAttribute('data-kind') === KIND ? ' active' : '');
  }
}
function openCache(d){
  var base = HREF[d.kind] || '/';
  var qs = new URLSearchParams({start: d.start, end: d.end});
  if(d.kind === 'industry') qs.set('accounts', d.account || 'all');
  else qs.set('account', d.kind === 'profit' ? 'all' : (d.account || 'all'));
  window.location = base + '?' + qs.toString();
}
function delCache(d){
  var tip = '删除「' + (LABEL[d.kind] || d.kind) + '」' + (d.account || '') + ' '
    + d.start + ' ~ ' + d.end + ' 的本地缓存？\n删掉后下次查询会重新请求京东接口。';
  if(!confirm(tip)) return;
  var qs = new URLSearchParams({kind: d.kind, account: d.account || '',
                                start: d.start, end: d.end, file: d.file || ''});
  fetch('/api/cache/delete?' + qs.toString(), {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      $('cgMsg').textContent = (j.ok && (j.deleted||[]).length)
        ? ('已删除：' + j.deleted.join('、')) : (j.error || '删除失败');
      load();
    }).catch(function(e){ $('cgMsg').textContent = '删除失败：' + e; });
}
function load(){
  fetch('/api/cache/list').then(function(r){ return r.json(); }).then(function(j){
    if(!j.ok){ $('cgMsg').textContent = '读取缓存清单失败' + (j.error ? '：' + j.error : ''); return; }
    ALL = j.entries || [];
    var parts = [];
    ['cps','spend','profit','industry','cashflow'].forEach(function(k){
      var n = (j.counts || {})[k] || 0;
      if(n) parts.push((LABEL[k] || k) + ' ' + n + ' 份');
    });
    $('cgMsg').textContent = parts.length
      ? ('当前：' + parts.join(' · ') + '；删除后下次查询会重新请求京东接口。')
      : '还没有任何缓存，去花费/利润/分析报表页查询一次就会存下来。';
    render();
  }).catch(function(e){ $('cgMsg').textContent = '读取缓存清单失败：' + e; });
}
function kind(k){ KIND = k; syncTabs(); render(); }
function view(v){
  VIEW = (v === 'acct') ? 'acct' : 'time';
  $('cgVTime').className = 'tab' + (VIEW === 'time' ? ' active' : '');
  $('cgVAcc').className = 'tab' + (VIEW === 'acct' ? ' active' : '');
  render();
}
function search(){ Q = ($('cgQ').value || '').trim().toLowerCase(); render(); }
window.CG = {kind: kind, view: view, search: search, load: load};
document.getElementById('cgBody').addEventListener('click', function(ev){
  var b = ev.target.closest ? ev.target.closest('button[data-act]') : null;
  if(!b) return;
  var d = {kind: b.getAttribute('data-kind'), account: b.getAttribute('data-acc') || '',
           start: b.getAttribute('data-s'), end: b.getAttribute('data-e'),
           file: b.getAttribute('data-file') || ''};
  if(b.getAttribute('data-act') === 'open') openCache(d); else delCache(d);
});
load();
})();
"""


@app.get("/api/cache/list")
def api_cache_list():
    """各类缓存合成一张清单：花费两份 + 利润 + 行业大盘 + 广告费流水，条目自带 kind。"""
    entries: list[dict] = []
    for en in _paycomm.cache_entries():
        en["kind"] = "cps"
        entries.append(en)
    for en in _paycomm_spend.cache_entries():
        en["kind"] = "spend"
        entries.append(en)
    for en in profit.cache_entries():
        en["kind"] = "profit"
        entries.append(en)
    for en in industry.cache_entries():
        en["kind"] = "industry"
        entries.append(en)
    for en in cashflow.cache_entries():
        en["kind"] = "cashflow"
        entries.append(en)
    counts = {"cps": 0, "spend": 0, "profit": 0, "industry": 0, "cashflow": 0}
    for en in entries:
        en["kindLabel"] = CACHE_LABELS.get(en["kind"], en["kind"])
        counts[en["kind"]] = counts.get(en["kind"], 0) + 1
    entries.sort(key=lambda e: e.get("savedAt") or "", reverse=True)
    return {"ok": True, "entries": entries, "count": len(entries), "counts": counts,
            "totalKB": round(sum(float(e.get("sizeKB") or 0) for e in entries), 1)}


@app.post("/api/cache/delete")
def api_cache_delete(kind: str = "", account: str = "", start: str = "",
                     end: str = "", file: str = ""):
    if kind == "cps":
        return _paycomm.delete_cache(account, start, end)
    if kind == "spend":
        return _paycomm_spend.delete_cache(account, start, end)
    if kind == "profit":
        return profit.delete_cache(start, end, file=file)
    if kind == "industry":
        return industry.delete_cache(account, start, end)
    if kind == "cashflow":
        return cashflow.delete_cache(account, start, end)
    return {"ok": False,
            "error": "未知的缓存类型（kind 只能是 cps / spend / profit / industry / cashflow）"}


@app.get("/cache", response_class=HTMLResponse)
def cache_page(request: Request):
    """本地缓存管理（二级页）：按时间 / 按账号两种视图，可搜索、可删。"""
    kind = request.query_params.get("kind") or "all"
    if kind not in CACHE_LABELS:
        kind = "all"
    tabs = "".join(
        f'<button type="button" class="tab{" active" if k == kind else ""}" '
        f'data-kind="{k}" onclick="CG.kind(\'{k}\')">{lab}</button>'
        for k, lab in CACHE_KINDS)
    init = {"kind": kind, "labels": CACHE_LABELS, "href": CACHE_HREF}
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地缓存管理 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{PAYCOMM_CSS}</style>
<style>{CACHE_CSS}</style>
</head><body>
{report2.navtop("/cache")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}缓存</div>
  <h1>本地缓存管理</h1>
  <div class="metaline">
    <span>一次抓取 = 一份文件（<code>{_e(str(settings.DATA_DIR))}/reports/</code>）</span><span class="dot"></span>
    <span>不自动过期，删除由你决定</span><span class="dot"></span>
    <span>旧格式 / 口径过期的缓存不会被命中</span>
  </div>
  <div class="actions">
    <a class="btn ghost mini" href="/paycomm">CPS 花费 →</a>
    <a class="btn ghost mini" href="/paycomm/spend">京准通花费 →</a>
    <a class="btn ghost mini" href="/profit">月度利润 →</a>
    <a class="btn ghost mini" href="/report">行业大盘（报表页签）</a>
    <a class="btn ghost mini" href="/cashflow">广告费流水 →</a>
  </div>
</div></header>
<div class="wrap">

<div class="card">
  <div class="toolbar">
    <span class="tabs" id="cgKinds">{tabs}</span>
    <span class="tabs" style="margin-left:16px">
      <button type="button" class="tab active" id="cgVTime" onclick="CG.view('time')">按时间</button>
      <button type="button" class="tab" id="cgVAcc" onclick="CG.view('acct')">按账号</button>
    </span>
    <input id="cgQ" placeholder="搜索账号 / 区间 / 类型" oninput="CG.search()"
           style="min-width:240px;padding:7px 10px;border-radius:8px;border:1px solid var(--line);margin-left:16px">
    <span class="hint" id="cgStat" style="margin-left:auto"></span>
  </div>
  <div class="hint" id="cgMsg" style="min-height:20px">读取缓存清单…</div>
</div>

<div id="cgBody" style="margin-top:18px"></div>

<div class="note" style="margin-top:18px">
  <p><b>CPS 花费 / 京准通花费</b>：每个「账号 + 区间」一份，点「查看」会带着区间跳到对应页面并直接命中这份缓存。</p>
  <p><b>月度利润</b>：每个区间一份，里面同时装各账号的表；切账号不用重算。口径版本升级后旧文件不再命中，标「口径过期」的可以删掉。</p>
  <p>删除只影响本地文件，京东侧数据不受影响；下次查询会重新抓取一次。</p>
</div>

</div>
<script>window.CACHE_INIT = {json.dumps(init, ensure_ascii=False)};</script>
<script>{CACHE_JS}</script>
</body></html>""")


# ================================================================ 广告费流水
# 数据在 jd_roi/cashflow.py：逐账号抓「账户中心 → 财务记录」的充值/消费/分配/冻结，
# 这里只做页面与接口。页面查询时按天拼接本地缓存（不发请求）；「抓取」只补缓存里
# 缺的日期，缓存清单统一在 /cache 页看。
CASHFLOW_CSS = """
.cfkpi{margin:14px 0 4px}
.cfchart{background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md);
  padding:10px 14px 4px;margin-bottom:14px}
.cfchd{display:flex;justify-content:space-between;align-items:center;font-size:13px;
  color:var(--body);margin-bottom:2px}
.cfchd b{font-weight:600}
.cfchd span{color:var(--muted2);font-size:12px}
.cfchd i{display:inline-block;width:10px;height:10px;border-radius:2px;
  margin:0 3px 0 10px;vertical-align:-1px}
.cfchd i.g{background:#2E9E5B}.cfchd i.r{background:#D64545}
.cfchart text.ax{font-size:11px;fill:#9d968b}
.cfchart text.v{font-size:9.5px}
.cfchart text.gv{fill:#2E9E5B}.cfchart text.rv{fill:#D64545}
.cfchart text.empty{fill:#bdb6aa;font-size:12px}
table.cftable{width:100%;border-collapse:collapse;font-size:12.5px}
table.cftable th{background:var(--soft);color:var(--muted);font-weight:600;text-align:right;
  padding:7px 10px;border-bottom:1px solid var(--hair);white-space:nowrap}
table.cftable th:first-child,table.cftable td:first-child{text-align:left}
table.cftable td{padding:6px 10px;border-bottom:1px solid var(--hair2);text-align:right;
  font-variant-numeric:tabular-nums}
table.cftable tbody tr:hover td{background:#fbf8f3}
table.cftable td.dim{color:var(--muted2)}
table.cftable tfoot td{font-weight:600;background:var(--soft);border-top:1px solid var(--hair)}
.cfscroll{max-height:620px;overflow:auto;border:1px solid var(--hair);border-radius:var(--r-md)}
.cfscroll thead th{position:sticky;top:0;z-index:2}
.cfwarn{color:var(--amberink);background:var(--amberbg);border-radius:8px;padding:8px 12px;
  font-size:12.5px;margin:10px 0}
"""

CASHFLOW_JS = r"""
(function(){
function $(id){ return document.getElementById(id); }
function msg(s, bad){ var el=$('cfMsg'); if(!el) return; el.textContent=s||'';
  el.style.color = bad ? 'var(--redink)' : 'var(--muted)'; }
function poll(){
  fetch('/api/cashflow/status').then(function(r){return r.json();}).then(function(j){
    if(j.running){
      msg('抓取中 ' + (j.done||0) + '/' + (j.total||0) + '　' + (j.account||'') +
          '　' + (j.message||''));
      setTimeout(poll, 2500);
    } else if(j.error){
      msg('失败：' + j.error, true);
      var b=$('cfRun'); if(b) b.disabled=false;
    } else if(j.finishedAt){
      msg((j.message||'完成') + '，正在刷新页面…');
      setTimeout(function(){ location.reload(); }, 900);
    }
  }).catch(function(){ setTimeout(poll, 4000); });
}
window.CFRUN = function(){
  var bEl = $('cfStart'), eEl = $('cfEnd'), btn = $('cfRun');
  var b = (bEl && bEl.value) || '', e = (eEl && eEl.value) || '';
  if(!b || !e){ msg('请先选好起止日期', true); return; }
  if(b > e){ msg('结束日不能早于开始日', true); return; }
  if(btn) btn.disabled = true;
  msg('已提交（' + b + ' ~ ' + e + '），等待浏览器登录态…');
  fetch('/api/cashflow/run?start=' + encodeURIComponent(b) + '&end=' + encodeURIComponent(e),
        {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){ msg(j.error || '启动失败', true); if(btn) btn.disabled=false; return; }
      if(j.nothing){
        msg(j.message || '该区间每一天都有缓存，无需补抓');
        if(btn) btn.disabled=false;
        return;
      }
      poll();
    })
    .catch(function(e2){ msg('启动失败：' + e2, true); if(btn) btn.disabled=false; });
};
/* 快捷区间：改完日期直接把页面带到该区间（有缓存就秒开） */
window.CFSET = function(days){
  var e = new Date(), s = new Date();
  s.setDate(e.getDate() - (days - 1));
  function iso(d){ return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0')
    + '-' + String(d.getDate()).padStart(2,'0'); }
  location.href = '/cashflow?start=' + iso(s) + '&end=' + iso(e);
};
window.CFSPAN = function(b, e){
  location.href = '/cashflow?start=' + b + '&end=' + e;
};
/* 查询：按输入框里的区间重新拼缓存（不发请求） */
window.CFQUERY = function(){
  var bEl = $('cfStart'), eEl = $('cfEnd');
  var b = (bEl && bEl.value) || '', e = (eEl && eEl.value) || '';
  if(!b || !e){ msg('请先选好起止日期', true); return; }
  if(b > e){ msg('结束日不能早于开始日', true); return; }
  location.href = '/cashflow?start=' + b + '&end=' + e;
};
if(window.CF_RUNNING) poll();
})();
"""


def _cf_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


@app.get("/cashflow", response_class=HTMLResponse)
def cashflow_page(request: Request):
    """广告费流水：余额、充值/消费柱状图、每日流水（读本地缓存，不触发抓取）。

    区间由 URL 的 start/end 决定，默认近 30 天；各账号的缓存分片按天拼接，
    所以查任意区间都能出结果；「抓取」只补缓存里缺的日期。
    """
    q = request.query_params
    d_begin, d_end = cashflow.default_range(30)
    begin = q.get("start") or d_begin
    end = q.get("end") or d_end
    snap = cashflow.snapshot(begin, end)
    begin, end = snap.get("begin") or begin, snap.get("end") or end
    st = _cashflow.status()
    running = bool(st.get("running"))
    # 「上月」快捷区间（与其它页的日历口径一致）
    first = _today().replace(day=1)
    lm_end_d = first - dt.timedelta(days=1)
    lm_start, lm_end = lm_end_d.replace(day=1).isoformat(), lm_end_d.isoformat()

    gap_days = int(snap.get("gapDays") or 0)
    gap_txt = ""
    if gap_days:
        gr = snap.get("gapRange") or []
        gap_txt = (f"{_e(gr[0])} ~ {_e(gr[1])}" if len(gr) == 2 and gr[0] != gr[1]
                   else _e(gr[0]) if gr else "")

    if snap.get("empty"):
        lack = f"还缺 {gap_days} 天数据" if gap_days else "这个区间还没抓过"
        meta = (f"<span>{begin} ~ {end}</span><span class='dot'></span>"
                f"<span>{lack}</span>")
        kpis = ""
        body = ('<div class="card"><p class="hint">该区间还没有流水数据。点右上角「抓取」，'
                f'只补缓存里缺的日期（{begin} ~ {end}），已有的日期不会重抓。</p>'
                '<p class="hint">缓存清单（区间 / 大小 / 删除）都在「缓存」页。</p></div>')
    else:
        accs = snap["accounts"]
        begin, end = snap["begin"], snap["end"]
        totals = snap["totals"]
        day_avg = round(sum(a["day_avg"] for a in accs), 2)
        cover = round(totals["cash"] / day_avg, 1) if day_avg else None
        meta = (f"<span>{begin} ~ {end}</span><span class='dot'></span>"
                f"<span>抓取于 {snap['fetched_at'][:16].replace('T', ' ')}</span>"
                f"<span class='dot'></span><span>来源：京准通 → 账户中心 → 财务记录</span>"
                + (f"<span class='dot'></span><span>还缺 {gap_days} 天</span>"
                   if gap_days else ""))
        kpis = (
            '<div class="grid g4 cfkpi">'
            f'<div class="kpi"><div class="lbl">可用余额合计</div>'
            f'<div class="val">¥ {_cf_money(totals["cash"])}</div>'
            f'<div class="sub">期初 {_cf_money(totals["begin"])}</div></div>'
            f'<div class="kpi"><div class="lbl">区间充值</div>'
            f'<div class="val">¥ {_cf_money(totals["recharge"])}</div>'
            f'<div class="sub">{len([a for a in accs if a["recharge_sum"]])} 个账号有充值</div></div>'
            f'<div class="kpi"><div class="lbl">区间消费</div>'
            f'<div class="val">¥ {_cf_money(totals["consume"])}</div>'
            f'<div class="sub">日均 {_cf_money(day_avg)}</div></div>'
            f'<div class="kpi"><div class="lbl">余额可撑</div>'
            f'<div class="val">{cover if cover is not None else "—"} 天</div>'
            f'<div class="sub">按近 {len(accs)} 个账号合计日均消耗估算</div></div>'
            '</div>')

        # 账号汇总表
        rows = []
        for a in accs:
            cover_a = a["cover_days"]
            rows.append(
                f'<tr><td><b>{_e(a["label"])}</b></td>'
                f'<td class="dim">{a["account_id"]}</td>'
                f'<td>{_cf_money(a["cash_balance"])}</td>'
                f'<td>{_cf_money(a["begin_balance"])}</td>'
                f'<td>{_cf_money(a["recharge_sum"])}</td>'
                f'<td>{_cf_money(a["consume_sum"])}</td>'
                f'<td>{_cf_money(a["day_avg"])}</td>'
                f'<td>{"—" if cover_a is None else f"{cover_a:g} 天"}</td>'
                f'<td class="dim">{_e(a["begin_from"])}</td></tr>')
        rows.append(
            f'<tr><td><b>合计</b></td><td class="dim"></td>'
            f'<td><b>{_cf_money(totals["cash"])}</b></td>'
            f'<td><b>{_cf_money(totals["begin"])}</b></td>'
            f'<td><b>{_cf_money(totals["recharge"])}</b></td>'
            f'<td><b>{_cf_money(totals["consume"])}</b></td>'
            f'<td><b>{_cf_money(day_avg)}</b></td>'
            f'<td><b>{"—" if cover is None else f"{cover:g} 天"}</b></td><td></td></tr>')
        table = (
            '<div class="card"><h2>账号汇总 <span class="tagline">按账号看资金</span></h2>'
            '<div style="overflow:auto"><table class="cftable"><thead><tr>'
            '<th>账号</th><th>账户ID</th><th>当前可用余额</th><th>区间期初</th>'
            '<th>区间充值</th><th>区间消费</th><th>日均消费</th><th>可撑</th>'
            '<th>期初口径</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
            '<p class="hint">消费 = 财务记录的「消费记录」合计（现金 + 红包）；'
            '可撑 = 当前余额 ÷ 日均消费，<b>不足 2 天要补钱</b>。</p></div>')

        # 柱状图
        charts = ('<div class="card"><h2>充值 vs 消费 <span class="tagline">绿上红下 · 上下各自独立刻度</span></h2>'
                  '<p class="hint">绿色向上是当日充值到账，红色向下是当日广告消耗。'
                  '上下两块刻度独立，所以两向柱高不能直接比（左侧数字是各自的满格值）。'
                  '鼠标悬停柱子可看精确金额。</p>'
                  + cashflow.charts_html(snap) + '</div>')

        # 每日流水（日期 × 账号 的消费）
        dates = cashflow.date_list(begin, end)
        mat = {(r["date"], r["account"]): r for r in snap["daily"]}
        head = "".join(f"<th>{_e(a['label'])}</th>" for a in accs)
        drows = []
        for d in reversed(dates):
            vals = [(mat.get((d, a["account"])) or {}).get("consume") or 0 for a in accs]
            tot = round(sum(vals), 2)
            if not tot:
                continue
            cells = "".join(
                (f"<td>{_cf_money(v)}</td>" if v else '<td class="dim">—</td>') for v in vals)
            drows.append(f"<tr><td>{d}</td>{cells}<td><b>{_cf_money(tot)}</b></td></tr>")
        daily_table = (
            '<div class="card"><h2>每日消费 <span class="tagline">按消耗日归属</span></h2>'
            '<div class="cfscroll"><table class="cftable"><thead><tr>'
            f'<th>日期</th>{head}<th>合计</th></tr></thead><tbody>'
            + "".join(drows) + '</tbody></table></div>'
            '<p class="hint">次日 08:45 左右批量扣款，所以「今天的消耗」要到明早才从余额里扣掉。'
            '逐笔明细（充值/分配/冻结）在导出的 Excel 里。</p></div>')

        body = kpis + table + charts + daily_table
        if gap_days:
            body = (f'<div class="cfwarn">本区间还缺 {gap_days} 天缓存'
                    f'（{gap_txt}），点「抓取」只补这些天，已有日期不会重抓。</div>' + body)

    run_hint = f'<span id="cfMsg" style="font-size:12px;color:var(--muted)">{"抓取中…" if running else ""}</span>'
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>广告费流水 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{CASHFLOW_CSS}</style>
</head><body>
{report2.navtop("/cashflow")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}财务</div>
  <h1>广告费流水</h1>
  <div class="metaline">{meta}</div>
  <div class="actions">
    <label style="font-size:12px;color:var(--muted)">起
      <input id="cfStart" type="date" value="{begin}"
             style="padding:6px 9px;border-radius:8px;border:1px solid var(--line)"></label>
    <label style="font-size:12px;color:var(--muted)">止
      <input id="cfEnd" type="date" value="{end}"
             style="padding:6px 9px;border-radius:8px;border:1px solid var(--line)"></label>
    <button class="btn ghost mini" onclick="CFSET(7)">近7天</button>
    <button class="btn ghost mini" onclick="CFSET(30)">近30天</button>
    <button class="btn ghost mini" onclick="CFSPAN('{lm_start}','{lm_end}')">上月</button>
    <button class="btn" id="cfRun" onclick="CFRUN()" {"disabled" if running else ""}
            title="只补缓存里缺的日期，已有的日期不重抓">抓取</button>
    <a class="btn ghost mini" href="/api/cashflow/export?start={begin}&end={end}">导出 Excel</a>
    <button class="btn ghost mini" onclick="CFQUERY()"
            title="用已有缓存拼接出这个区间，不发请求">查询</button>
    <a class="btn ghost mini" href="/cache?kind=cashflow">缓存管理 →</a>
    <a class="btn ghost mini" href="/paycomm/spend">京准通花费 →</a>
    {run_hint}
  </div>
</div></header>
<div class="wrap">
{body}
<div class="note" style="margin-top:18px">
  <p><b>口径</b>：交易日期 = 广告实际消耗日；记账时间 = 次日 08:45 左右批量扣款；
  结余取接口原值，接口不返回结余的账号按「期初 + 充值 − 消费」逐日滚动推算。</p>
  <p><b>分配</b>是同一资金户内「商家账户 ↔ 投放子账户」的划转，不改变账户现金总额，不计入余额滚动。</p>
  <p>本地缓存<b>按天拼接</b>：查询任意区间都会用已有缓存拼出结果，不会重新请求京东；
  「抓取」只补缓存里缺的日期。缓存清单在「缓存」页，要强制刷新可先在那里删掉对应区间再抓。</p>
</div>
</div>
<script>window.CF_RUNNING = {"true" if running else "false"};</script>
<script>{CASHFLOW_JS}</script>
</body></html>""")


@app.post("/api/cashflow/run")
def api_cashflow_run(start: str = "", end: str = "", days: int = 0):
    """起后台任务抓财务流水（逐账号，约 2~4 分钟）。

    start/end 为 YYYY-MM-DD；只给 days 时按「含今天往前 N 天」换算（兼容老链接）。
    """
    if not (start and end):
        start, end = cashflow.default_range(days or 30)
    return _cashflow.start(start, end)


@app.get("/api/cashflow/status")
def api_cashflow_status():
    return _cashflow.status()


@app.get("/api/cashflow/export")
def api_cashflow_export(start: str = "", end: str = ""):
    """导出与页面同口径的 Excel（汇总/每日/明细/口径/柱状图）。"""
    snap = cashflow.snapshot(start, end)
    if not snap.get("ok"):
        return JSONResponse({"ok": False, "error": "还没有数据，请先抓取一次"}, status_code=404)
    data = cashflow.xlsx_bytes(snap["accounts"], snap["daily"], snap["begin"], snap["end"])
    from urllib.parse import quote
    fname = f"账号广告费流水表_{snap['begin']}_{snap['end']}.xlsx"
    disp = f"attachment; filename=\"cashflow.xlsx\"; filename*=UTF-8''{quote(fname)}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp, "Cache-Control": "no-store"})


# ================================================================ 月度利润分析
# 商智成交件数 × ERP 到手结算价 / 毛利每件，再扣 CPS 与京准通花费 → 净利。
# 取数与计算在 jd_roi/profit.py，这里只做页面与接口。
PROFIT_WEB_KEYS = ["qty", "frontPrice", "supply", "arrival", "grossPerOrder",
                   "gross", "cpsCost", "jztCost", "net", "netRate"]

PROFIT_KPIS = [
    {"label": "SKU 数", "key": None, "type": "int"},
    {"label": "成交件数", "key": "qty", "type": "int"},
    {"label": "到手金额", "key": "arrival", "type": "money", "sub": "件数 × ERP 到手结算价"},
    {"label": "毛利", "key": "gross", "type": "money", "sub": "件数 × 毛利/件（未扣广告）"},
    {"label": "CPS 花费", "key": "cpsCost", "type": "money"},
    {"label": "京准通花费", "key": "jztCost", "type": "money"},
    {"label": "净利", "key": "net", "type": "money", "subKind": "netRate"},
]

PROFIT_JS = r"""
(function(){
var INIT = window.PC_INIT || {};
var API = INIT.api || '/api/profit';
var FIELDS = INIT.fields || [];
var WEB = INIT.webKeys || [];
var KPIS = INIT.kpis || [];
var RATIO = INIT.ratioKeys || [];
var LBL = {}, TYP = {};
FIELDS.forEach(function(f){ LBL[f[0]] = f[1]; TYP[f[0]] = f[2]; });
var DIM = 'sku', JOB = null, ROWS = [], TOTALS = {}, DATA = {}, SORT = {k:'gross', d:-1}, TIMER = null;
var FAILS = 0;

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
function num(v){ if(v===null||v===undefined||v==='') return null; var n=Number(v); return isNaN(n)?null:n; }
function fmt(v,t){ var n=num(v); if(n===null) return '—';
  if(t==='int') return n.toLocaleString('zh-CN');
  return n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function msg(s,kind){ var el=$('pfMsg'); if(!el) return; el.textContent=s||'';
  el.style.color = kind==='bad' ? 'var(--red)' : (kind==='ok' ? 'var(--greenink)' : 'var(--muted)'); }
function kpi(lbl,val,sub){ return '<div class="kpi"><div class="lbl">'+esc(lbl)+'</div><div class="val">'+val+'</div>'
  + (sub?'<div class="sub">'+esc(sub)+'</div>':'') + '</div>'; }
function cols(){ return (DIM==='cat') ? WEB.filter(function(k){ return RATIO.indexOf(k)<0; }) : WEB; }
function accTip(r){
  var L = INIT.labels || {};
  function j(a){ return (a||[]).map(function(k){ return L[k] || k; }).join('、'); }
  var sale = j(r.accts), spend = j(r.spendAccts);
  var nm = r.itemName || '';
  if(!sale && !spend) return nm;
  return nm + ' ｜ 成交账号：' + (sale||'—') + ' ｜ 花费账号：' + (spend||'—');
}

function aggregate(list){
  var map = {}, order = [];
  list.forEach(function(r){
    var k = r.category || '未分类';
    var b = map[k];
    if(!b){ b = {category:k, __n:0}; cols().forEach(function(kk){ b[kk]=0; }); map[k]=b; order.push(k); }
    b.__n += 1;
    cols().forEach(function(kk){ var v = num(r[kk]); if(v!==null) b[kk]+=v; });
  });
  return order.map(function(k){
    var b = map[k];
    cols().forEach(function(kk){ b[kk] = Math.round(b[kk]*100)/100; });
    return b;
  });
}
function head(){
  var cat = (DIM==='cat');
  var h = cat
    ? '<tr><th class="l noSort" data-key="category">品类</th><th class="n noSort" data-key="__n">SKU数</th>'
    : '<tr><th class="l noSort" data-key="skuId">SKU</th><th class="l noSort" data-key="itemName">商品名称</th>';
  cols().forEach(function(k){
    var mark = SORT.k===k ? (SORT.d<0?' ↓':' ↑') : '';
    h += '<th class="n noSort" data-key="'+k+'">'+esc(LBL[k]||k)+mark+'</th>';
  });
  return h+'</tr>';
}
function rowsHtml(list){
  var cat = (DIM==='cat');
  if(!list.length) return '<tr><td colspan="'+(cols().length+2)+'" class="l">没有匹配的行</td></tr>';
  return list.map(function(r){
    var tds = cols().map(function(k){
      var hot = (k==='net') ? ' hot' : '';
      return '<td class="n'+hot+'">'+fmt(r[k],TYP[k])+'</td>';
    }).join('');
    if(cat){
      return '<tr><td class="l"><b>'+esc(r.category)+'</b></td><td class="n">'+r.__n+'</td>'+tds+'</tr>';
    }
    return '<tr><td class="l"><b>'+esc(r.skuId)+'</b></td>'
      + '<td class="l nm" title="'+esc(accTip(r))+'">'+esc(r.itemName||'—')+'</td>'+tds+'</tr>';
  }).join('');
}
function kpisHtml(){
  var out = '';
  KPIS.forEach(function(k){
    var val, sub = k.sub || '';
    if(!k.key){
      val = ROWS.length.toLocaleString('zh-CN');
    } else {
      var s = 0;
      ROWS.forEach(function(r){ s += num(r[k.key]) || 0; });
      val = (k.type==='int') ? fmt(s,'int') : ('¥' + fmt(s,'money'));
    }
    if(k.subKind === 'netRate'){
      sub = TOTALS.netRate ? ('净利率 ' + fmt(TOTALS.netRate,'num') + '%') : '净利 = 毛利 − 广告花费';
    }
    out += kpi(k.label, val, sub);
  });
  return out;
}
function render(){
  var cat = (DIM==='cat');
  var kw = ($('pfFilter').value||'').trim().toLowerCase();
  var base = cat ? aggregate(ROWS) : ROWS;
  var list = base.filter(function(r){
    if(!kw) return true;
    if(cat) return String(r.category||'').toLowerCase().indexOf(kw) >= 0;
    return String(r.skuId).indexOf(kw) >= 0
      || String(r.itemName||'').toLowerCase().indexOf(kw) >= 0
      || String(r.category||'').toLowerCase().indexOf(kw) >= 0;
  });
  list = list.slice().sort(function(a,b){
    var k = SORT.k;
    if(cat && (k==='skuId'||k==='itemName'||k==='category')){
      return String(a.category||'').localeCompare(String(b.category||''))*SORT.d;
    }
    if(k==='itemName') return String(a[k]||'').localeCompare(String(b[k]||''))*SORT.d;
    if(k==='skuId') return String(a.skuId).localeCompare(String(b.skuId))*SORT.d;
    var x = num(a[k]), y = num(b[k]);
    if(x===null) x = -1e18;
    if(y===null) y = -1e18;
    return (x-y)*SORT.d;
  });
  $('pfHead').innerHTML = head();
  $('pfBody').innerHTML = rowsHtml(list);
  $('pfKpis').innerHTML = kpisHtml();
  $('pfKpis').style.display = '';
  $('pfCount').textContent = cat
    ? ('共 ' + list.length + ' 个品类 / ' + ROWS.length + ' 个 SKU')
    : ('共 ' + ROWS.length + ' 个 SKU' + (list.length!==ROWS.length ? ('，筛选后 '+list.length+' 行') : ''));
}
function renderNotes(notes){
  var el = $('pfNotes');
  if(!el) return;
  if(!notes || !notes.length){ el.style.display='none'; el.innerHTML=''; return; }
  el.style.display = '';
  el.innerHTML = '<b>数据提示</b><ul style="margin:6px 0 0 18px;padding:0">'
    + notes.map(function(n){ return '<li>'+esc(n)+'</li>'; }).join('') + '</ul>';
}
function showTable(){ $('pfTableCard').style.display=''; $('pcExportView').disabled=false; render(); }
function applyAcc(){
  var b = DATA[$('pfAcc').value];
  if(!b){ return; }
  ROWS = b.rows || [];
  TOTALS = b.totals || {};
  if(ROWS.length){ showTable(); }
  else { $('pfTableCard').style.display='none'; msg('该账号在这个区间没有数据'); }
}
function switchAcc(){ if(Object.keys(DATA).length){ applyAcc(); } else { poll(); } }

function poll(silent){
  var qs = new URLSearchParams({rows: '1'});
  if(JOB) qs.set('job', JOB);
  fetch(API + '/status?' + qs.toString()).then(function(r){ return r.json(); }).then(function(j){
    FAILS = 0;
    if(!j.ok){ if(!silent){ msg(j.error||'状态查询失败','bad'); } return; }
    if(j.active){
      if(j.jobId) JOB = j.jobId;
      $('pfRun').disabled = true; $('pfRefetch').disabled = true;
      var pct = j.total ? Math.round(j.done*100/j.total) : 0;
      $('pfBar').style.width = pct+'%';
      if(!silent){ msg((j.message||'计算中') + (j.total ? ('　'+j.done+'/'+j.total) : '')); }
      clearTimeout(TIMER); TIMER = setTimeout(poll, 1500);
      return;
    }
    $('pfRun').disabled = false; $('pfRefetch').disabled = false;
    $('pfBar').style.width = j.error ? '0' : '100%';
    if(!silent){
      if(j.error){ msg(j.message || j.error, 'bad'); }
      else if(j.message){ msg(j.message, 'ok'); }
    }
    if(j.rows && j.rows.length){
      var rg = j.range || [];
      var same = rg.length ? (rg[0] === $('pfStart').value && rg[1] === $('pfEnd').value) : true;
      if(!same){
        msg('本页最近一次结果是 ' + rg[0] + ' ~ ' + rg[1] + '；当前区间点「查询」即可（有缓存秒出）');
        return;
      }
      JOB = j.jobId;
      DATA = j.byAccount || {};
      renderNotes(j.notes || []);
      if(Object.keys(DATA).length){
        if(!DATA[$('pfAcc').value]){ $('pfAcc').value = Object.keys(DATA)[0]; }
        applyAcc();
      } else {
        ROWS = j.rows || []; TOTALS = j.totals || {}; showTable();
      }
    }
  }).catch(function(e){
    FAILS += 1;
    if(FAILS <= 4){ msg('状态查询失败（'+e+'），2 秒后重试…'); clearTimeout(TIMER); TIMER=setTimeout(poll,2000); return; }
    $('pfRun').disabled = false; $('pfRefetch').disabled = false;
    msg('与服务端连接中断（常见于服务重启），刷新页面即可恢复', 'bad');
  });
}
function markQuick(){
  if(!window.QRANGE) return;
  window.QRANGE.mark('#pfQuick', $('pfStart').value, $('pfEnd').value);
}
function quick(k){
  var r = window.QRANGE && window.QRANGE.span(k);
  if(!r) return;
  $('pfStart').value = r[0];
  $('pfEnd').value = r[1];
  markQuick();
  run();
}
function run(force){
  var acc = $('pfAcc').value, s = $('pfStart').value, e = $('pfEnd').value;
  if(!s || !e){ msg('请选择起止日期','bad'); return; }
  if(s > e){ msg('起始日期不能晚于结束日期','bad'); return; }
  ROWS = []; JOB = null; TOTALS = {};
  $('pfTableCard').style.display='none';
  $('pcExportView').disabled = true;
  $('pfRun').disabled = true; $('pfRefetch').disabled = true;
  $('pfBar').style.width = '0';
  msg(force ? '正在重新计算（会覆盖该区间缓存）…' : '查询中（先看本地缓存）…');
  var qs = new URLSearchParams({account: acc, start: s, end: e});
  if(force) qs.set('force','1');
  fetch(API + '/run?' + qs.toString(), {method:'POST'}).then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){ msg('失败：'+(j.error||''),'bad'); $('pfRun').disabled=false; $('pfRefetch').disabled=false; return; }
      JOB = j.jobId;
      if(j.cached){
        var head = j.stitched
          ? ('命中本地缓存拼接：' + (j.pieces||[]).join(' + ') + '（共 ' + (j.rows||0) + ' 个 SKU），未请求商智')
          : ('命中本地缓存（'+(j.savedAt||'')+' 计算，共 '+(j.rows||0)+' 个 SKU），未请求商智');
        msg(head + (j.busy ? '；后台还有任务在跑，本次只读缓存' : ''));
      } else if(j.resumed){
        // 断点续抓：上次算到哪，这次就从哪接着抓，不回头重抓整段
        var rp = j.resumed;
        msg('复用本地 ' + (rp.pieces||[]).join(' + ') + '，只补抓 '
            + rp.tail[0] + ' ~ ' + rp.tail[1] + '…');
      }
      poll();
    }).catch(function(e){ msg('请求失败 '+e,'bad'); $('pfRun').disabled=false; $('pfRefetch').disabled=false; });
}
function reprice(){
  if(!JOB){ msg('还没有结果，先点「查询」','bad'); return; }
  if(!confirm('只重新拉取 ERP 成本并重算到手价/毛利/净利？\n\n商智成交与 CPS/京准通花费都沿用本地缓存，不会重新抓。')) return;
  msg('正在重拉 ERP 成本…');
  $('pfReprice').disabled = true;
  var qs = new URLSearchParams({job: JOB, account: $('pfAcc').value,
    start: $('pfStart').value, end: $('pfEnd').value});
  fetch(API + '/reprice?' + qs.toString(), {method:'POST'}).then(function(r){ return r.json(); })
    .then(function(j){
      $('pfReprice').disabled = false;
      if(!j.ok){ msg('更新失败：'+(j.error||''),'bad'); return; }
      msg('已按最新 ERP 成本重算：' + j.costSku + ' 个 SKU 有成本，'
          + (j.files || 1) + ' 个区间的表已更新，' + j.changed + ' 行发生变化（接口更新于 '
          + (j.costUpdatedAt || '—') + '）', 'ok');
      poll(true);
    }).catch(function(e){ $('pfReprice').disabled = false; msg('更新失败 '+e,'bad'); });
}
function refetch(){
  if(!confirm('重新计算 ' + $('pfStart').value + ' ~ ' + $('pfEnd').value + '？\n\n会真实请求商智并覆盖该区间缓存。')) return;
  run(true);
}
function exp(){
  if(!JOB){ msg('还没有数据，先点「查询」','bad'); return; }
  var qs = new URLSearchParams({job: JOB, account: $('pfAcc').value,
    start: $('pfStart').value, end: $('pfEnd').value, dim: DIM});
  window.location = API + '/export?' + qs.toString();
}
function expView(){ exp(); }
function dim(k){
  DIM = (k === 'cat') ? 'cat' : 'sku';
  $('pfDimSku').className = 'tab' + (DIM==='sku' ? ' active' : '');
  $('pfDimCat').className = 'tab' + (DIM==='cat' ? ' active' : '');
  $('pfTableTitle').textContent = (DIM==='cat') ? '品类汇总' : 'SKU 明细';
  if(ROWS.length) render();
}
function sort(k){
  if(SORT.k === k) SORT.d = -SORT.d;
  else SORT = {k:k, d:(k==='itemName'||k==='skuId'||k==='category')?1:-1};
  render();
}

/* ---------------- 毛利诊断（京东毛保） ---------------- */
function mbConf(){ return (INIT.maobao || {}); }
function mbRules(){
  var key = $('pfAcc').value;
  return (mbConf().rules || {})[key] || null;
}
function mbTemp(r){
  var C = mbConf(), sid = String(r.skuId == null ? '' : r.skuId);
  if((C.skuTypes || {})[sid]) return C.skuTypes[sid];
  var nm = String(r.itemName || ''), i;
  for(i=0;i<(C.normalWords || []).length;i++){
    if(nm.indexOf(C.normalWords[i]) >= 0) return 'normal';
  }
  for(i=0;i<(C.frozenWords || []).length;i++){
    if(nm.indexOf(C.frozenWords[i]) >= 0) return 'frozen';
  }
  return 'normal';
}
function mbPct(v){
  var n = num(v); if(n===null) return '—';
  return (n*100).toFixed(2) + '%';
}
function mbGroupCard(g, rules){
  var line = (rules && g.threshold!=null) ? mbPct(g.threshold) : '未配置';
  var verdict;
  if(!rules || g.threshold==null) verdict = '<span class="badge b-idle">未配置毛保线</span>';
  else if(g.settle<=0) verdict = '<span class="badge b-idle">无结算数据</span>';
  else if(g.triggered) verdict = '<span class="badge b-bad">触发毛保</span>';
  else verdict = '<span class="badge b-ok">达标</span>';
  return '<div class="card" style="margin-top:0">'
    + '<h2 style="margin:0 0 10px;font-size:16px">'+esc(g.label)
      + ' <span class="tagline">毛保线 '+esc(line)+'</span> '+verdict+'</h2>'
    + '<div class="tm" style="margin:0">'
      + '<span>成交件数 <b>'+fmt(g.qty,'int')+'</b></span>'
      + '<span>前台成交额 <b>¥'+fmt(g.sale,'money')+'</b></span>'
      + '<span>结算采购额 <b>¥'+fmt(g.settle,'money')+'</b></span>'
      + '<span>京东毛利额 <b>¥'+fmt(g.profit,'money')+'</b></span>'
      + '<span>京东毛利率 <b>'+mbPct(g.margin)+'</b></span>'
      + (rules && g.triggered ? '<span>需补利润 <b style="color:var(--redink)">¥'+fmt(g.shortfall,'money')+'</b></span>' : '')
    + '</div></div>';
}
function mbTable(groups){
  var rows = [];
  groups.forEach(function(g){ g.items.forEach(function(it){ rows.push(it); }); });
  if(!rows.length) return '<div class="hint" style="padding:12px 0">没有可用于测算的 SKU：需要同时有平均前台价与扣点后采购价。</div>';
  var h = '<div class="tblwrap long" style="margin-top:14px"><table><thead><tr>'
    + '<th class="l noSort">温层</th><th class="l noSort">SKU</th><th class="l noSort">商品名称</th>'
    + '<th class="n noSort">成交件数</th><th class="n noSort">平均前台价</th><th class="n noSort">扣点后采购价</th>'
    + '<th class="n noSort">京东毛利/件</th><th class="n noSort">京东毛利额</th><th class="n noSort">结算采购额</th>'
    + '<th class="n noSort">毛利率</th></tr></thead><tbody>';
  rows.forEach(function(it){
    h += '<tr><td class="l">'+(it.temp==='frozen'?'冷冻':'常温')+'</td>'
      + '<td class="l"><b>'+esc(it.skuId)+'</b></td>'
      + '<td class="l nm" title="'+esc(it.itemName)+'">'+esc(it.itemName||'—')+'</td>'
      + '<td class="n">'+fmt(it.qty,'int')+'</td>'
      + '<td class="n">¥'+fmt(it.frontPrice,'money')+'</td>'
      + '<td class="n">¥'+fmt(it.supply,'money')+'</td>'
      + '<td class="n">¥'+fmt(it.unitProfit,'money')+'</td>'
      + '<td class="n">¥'+fmt(it.profit,'money')+'</td>'
      + '<td class="n">¥'+fmt(it.settle,'money')+'</td>'
      + '<td class="n">'+mbPct(it.margin)+'</td></tr>';
  });
  return h+'</tbody></table></div>';
}
function maobao(){
  if(!ROWS.length){ msg('先查询出利润明细，再做毛利诊断','bad'); return; }
  var acc = $('pfAcc').value;
  if(acc === 'all'){ msg('毛利诊断按账号结算，请先选择具体账号（例如雨润禾）','bad'); return; }
  var rules = mbRules();
  var groups = {
    frozen:{temp:'frozen',label:'冷冻品',qty:0,sale:0,settle:0,profit:0,items:[]},
    normal:{temp:'normal',label:'常温品',qty:0,sale:0,settle:0,profit:0,items:[]}
  };
  var skipped = [];
  ROWS.forEach(function(r){
    var qty = num(r.qty) || 0, front = num(r.frontPrice) || 0, supply = num(r.supply) || 0;
    if(qty <= 0) return;
    if(front <= 0 || supply <= 0){ skipped.push(r); return; }
    var t = mbTemp(r);
    var sale = qty*front, settle = qty*supply, profit = sale - settle;
    var it = {skuId:r.skuId, itemName:r.itemName||'', temp:t, qty:qty,
              frontPrice:front, supply:supply, unitProfit:front-supply,
              sale:sale, settle:settle, profit:profit,
              margin: settle>0 ? profit/settle : null};
    var g = groups[t];
    g.qty += qty; g.sale += sale; g.settle += settle; g.profit += profit; g.items.push(it);
  });
  ['frozen','normal'].forEach(function(k){
    var g = groups[k];
    g.margin = g.settle>0 ? g.profit/g.settle : null;
    g.threshold = rules ? rules[k] : null;
    g.triggered = !!(rules && g.threshold!=null && g.margin!=null && g.margin < g.threshold);
    g.shortfall = g.triggered ? Math.max(0, g.threshold*g.settle - g.profit) : 0;
    g.items.sort(function(a,b){ return b.profit - a.profit; });
  });
  var tot = {qty:0, sale:0, settle:0, profit:0};
  ['frozen','normal'].forEach(function(k){
    var g = groups[k];
    tot.qty += g.qty; tot.sale += g.sale; tot.settle += g.settle; tot.profit += g.profit;
  });
  tot.margin = tot.settle>0 ? tot.profit/tot.settle : null;
  var shortfall = groups.frozen.shortfall + groups.normal.shortfall;

  var accLabel = (INIT.labels || {})[acc] || acc;
  $('mbMeta').textContent = accLabel + ' · ' + $('pfStart').value + ' ~ ' + $('pfEnd').value;
  var warn = '';
  if(!rules){
    warn = '<div class="warn" style="margin:0 0 14px">当前账号「'+esc(accLabel)+'」没有配置毛保线，下面只测算京东毛利，不判定触发。'
      + '已配置：雨润禾（常温 &lt; 10%、冷冻 &lt; 18%）。</div>';
  } else if(skipped.length){
    warn = '<div class="warn" style="margin:0 0 14px">'+skipped.length+' 个 SKU 缺平均前台价或扣点后采购价，未计入测算：'
      + skipped.slice(0,5).map(function(r){ return esc(r.skuId)+' '+esc(String(r.itemName||'').slice(0,16)); }).join('、')
      + (skipped.length>5?' 等':'') + '。可先点「只更新 ERP 成本」再试。</div>';
  }
  $('mbBody').innerHTML = warn
    + '<div class="grid g2">'+mbGroupCard(groups.frozen,rules)+mbGroupCard(groups.normal,rules)+'</div>'
    + '<div class="tm" style="margin-top:14px"><span>合计：成交 <b>'+fmt(tot.qty,'int')+'</b> 件</span>'
      + '<span>前台成交额 <b>¥'+fmt(tot.sale,'money')+'</b></span>'
      + '<span>结算采购额 <b>¥'+fmt(tot.settle,'money')+'</b></span>'
      + '<span>京东毛利额 <b>¥'+fmt(tot.profit,'money')+'</b></span>'
      + '<span>京东毛利率 <b>'+mbPct(tot.margin)+'</b></span>'
      + '<span>触发补差合计 <b>¥'+fmt(shortfall,'money')+'</b></span></div>'
    + mbTable([groups.frozen, groups.normal])
    + '<div class="note" style="margin-top:16px">'
      + '<p><b>口径</b>：平均前台价取利润行；扣点后采购价 = ERP 到手结算价 supply。'
      + '京东毛利/件 = 平均前台价 − 扣点后采购价；毛利率 = 京东毛利合计 ÷ 结算给我们的扣点后采购价合计。</p>'
      + '<p><b>毛保</b>：雨润禾常温品低于 10%、冷冻品低于 18% 触发。'
      + '需补利润 = 毛保线 × 结算采购额 − 实际毛利额，即京东要补到毛保线还差的利润。</p>'
      + '<p><b>温层判定</b>：已登记 SKU 按登记表；未登记的按商品名关键词（冷冻/速冻/解冻/薯条/什锦/杂菜/毛豆/青豆/纳豆 → 冷冻，'
      + '冻干 → 常温），都没有按常温。</p></div>';
  $('mbModal').classList.add('open');
  msg('毛利诊断：' + accLabel + '，触发补差 ¥' + fmt(shortfall,'money'));
}
function closeMaobao(){ var m = $('mbModal'); if(m) m.classList.remove('open'); }
window.PF = {run:run, refetch:refetch, exp:exp, expView:expView, dim:dim, sort:sort,
             render:render, switchAcc:switchAcc, reprice:reprice, quick:quick,
             maobao:maobao, closeMaobao:closeMaobao};
function boot(){
  poll();
  // 从「缓存管理」点「查看」带区间打开：自动查一次，有缓存就直接秒出
  if(INIT.auto) run();
  $('pfTable').addEventListener('click', function(e){
    var th = e.target.closest('th');
    if(!th || !th.getAttribute('data-key')) return;
    sort(th.getAttribute('data-key'));
  });
  $('pfStart').addEventListener('change', markQuick);
  $('pfEnd').addEventListener('change', markQuick);
  markQuick();
}
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
})();
"""


@app.get("/profit", response_class=HTMLResponse)
def profit_page(request: Request):
    q = request.query_params
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = q.get("account") or settings.MAIN_ACCOUNT
    if acc != "all" and acc not in keys:
        acc = settings.MAIN_ACCOUNT
    start, end = _last_month_range()
    if q.get("start") and q.get("end"):
        try:
            start, end = profit.norm_range(q.get("start"), q.get("end"))
        except ValueError:
            pass
    opts = ('<option value="all"%s>全部账号（总览）</option>'
            % (" selected" if acc == "all" else "")) + "".join(
        f'<option value="{_e(a["key"])}"{" selected" if a["key"] == acc else ""}>'
        f'{_e(a.get("label"))}</option>' for a in settings.ACCOUNTS)
    init = {"api": "/api/profit", "fields": [[k, lab, t] for k, lab, t in profit.FIELDS],
            # 带 start/end 打开（缓存管理点「查看」）：进页面自动查一次，命中缓存直接秒出
            "auto": bool(q.get("start") and q.get("end")),
            "labels": {a["key"]: (a.get("label") or a["key"]) for a in settings.ACCOUNTS},
            "webKeys": PROFIT_WEB_KEYS, "kpis": PROFIT_KPIS,
            "ratioKeys": ["frontPrice", "supply", "grossPerOrder", "netRate"],
            "maobao": maobao.page_rules(settings.ACCOUNTS)}
    quick_html = _quick_html("PF")
    today_iso = _today().isoformat()
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>月度利润分析 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{PAYCOMM_CSS}</style>
<style>{QUICK_RANGE_CSS}</style>
</head><body>
{report2.navtop("/profit")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}利润</div>
  <h1>月度利润分析</h1>
  <div class="metaline">
    <span>成交件数：商智商品明细</span><span class="dot"></span>
    <span>到手结算价：ERP 成本接口</span><span class="dot"></span>
    <span>毛利/件：报表分析口径（未扣广告）</span>
  </div>
  <div class="actions">
    <a class="btn ghost mini" href="/paycomm">CPS 花费 →</a>
    <a class="btn ghost mini" href="/paycomm/spend">京准通花费 →</a>
    <a class="btn ghost mini" href="/cache?kind=profit">缓存管理 →</a>
    <a class="btn ghost mini" href="/login">登录 / 检查登录态</a>
  </div>
</div></header>
<div class="wrap">

<div class="card">
  <div class="toolbar">
    <label class="fld">账号 <select id="pfAcc" onchange="PF.switchAcc()">{opts}</select></label>
    <label class="fld">起 <input type="date" id="pfStart" value="{_e(start)}"></label>
    <label class="fld">止 <input type="date" id="pfEnd" value="{_e(end)}"></label>
    <button class="btn" id="pfRun" onclick="PF.run()">查询</button>
    <button class="btn ghost" id="pfRefetch" onclick="PF.refetch()" title="忽略缓存，重新请求商智">重新计算</button>
    <button class="btn sec" id="pfReprice" onclick="PF.reprice()"
            title="只重拉 ERP 成本，商智与花费不动">只更新 ERP 成本</button>
    <button class="btn sec mini" id="pcExportView" onclick="PF.expView()" style="margin-left:auto"
            title="按当前维度导出（SKU 或 品类）" disabled>导出当前视图 Excel</button>
  </div>
{quick_html}
  <div class="hint" id="pfMsg" style="min-height:20px">选账号与区间后点「查询」：有缓存直接读本地，没有才去商智取数。</div>
  <div class="pcbar"><i id="pfBar"></i></div>
</div>

<div class="grid g4" id="pfKpis" style="display:none;margin-top:18px"></div>

<div class="card" id="pfTableCard" style="display:none">
  <h2><span id="pfTableTitle">SKU 明细</span> <span class="tagline">点表头可换排序</span>
    <button class="btn sec mini" id="pfMaobao" onclick="PF.maobao()"
            style="margin-left:12px"
            title="按「平均前台价 − 扣点后采购价」测算京东毛利，判断是否触发毛保">毛利诊断</button>
  </h2>
  <div class="toolbar">
    <span class="tabs" style="margin:0 12px 8px 0">
      <button type="button" class="tab active" id="pfDimSku" onclick="PF.dim('sku')">按 SKU</button>
      <button type="button" class="tab" id="pfDimCat" onclick="PF.dim('cat')">按品类</button>
    </span>
    <input id="pfFilter" placeholder="筛选 SKU / 商品名 / 品类" oninput="PF.render()"
           style="min-width:240px;padding:7px 10px;border-radius:8px;border:1px solid var(--line)">
    <span class="hint" id="pfCount"></span>
  </div>
  <div class="tblwrap long">
    <table id="pfTable"><thead id="pfHead"></thead><tbody id="pfBody"></tbody></table>
  </div>
</div>

<div class="note" id="pfNotes" style="display:none;margin-top:16px"></div>

<div class="modal" id="mbModal" onclick="if(event.target===this)PF.closeMaobao()">
  <div class="modal-card" style="max-width:1120px">
    <div class="modal-head">
      <h2>毛利诊断 · 京东毛保测算</h2>
      <span class="hint" id="mbMeta" style="margin:0"></span>
      <button class="btn sec mini" onclick="PF.closeMaobao()">关闭</button>
    </div>
    <div class="modal-body" id="mbBody"></div>
  </div>
</div>

<div class="note" style="margin-top:18px">
  <p><b>到手金额</b> = 商智成交件数 × ERP「到手结算价 supply」（京东结算给我们的每件金额，已扣点）。</p>
  <p><b>毛利</b> = 成交件数 × 「毛利/件」= 到手结算价 − 货款 − 运费 − 包材 − 人工（<b>未扣广告</b>），
     与「分析报表」里的同一列完全同口径。</p>
  <p><b>净利</b> = 毛利 − CPS 花费（付款佣金）− 京准通花费（快车：智能投放 / 关键词 / 全站智能推广 + 全站营销）。
     两个花费都只读「花费一览」的本地缓存，本页不会额外抓取；缺失时会在上方提示，去对应页面抓一次即可。</p>
  <p><b>只列本账号京准通有投放的商品</b>：行集由京准通决定，商智里有成交但本账号没投的商品不会出现在这张表里
     （它属于投放它的那个账号）。本表「京准通花费」合计 = 该账号「京准通花费」页的合计。</p>
  <p><b>成交按 SKU 取</b>：成交件数、前台价、成交额是「这个 SKU 卖了多少」，不属于某个账号，
     所以会把两个账号的商智合成一张 SKU 表——本账号投的商品即使卖在另一个账号的店铺里，
     这里也能看到件数与毛利（悬停可看「成交账号」）。两个账号的商智都没有它时，才是 0 件数、毛利 0 的纯花费行。</p>
  <p>注意：同一个 SKU 若两个账号都在投，会同时出现在两张表里且各自带全额成交，跨账号相加会重复计成交与毛利，
     按账号单独看才准。</p>
  <p><b>毛利诊断</b>（「SKU 明细」右侧按钮）：京东毛利 = 平均前台价 − 扣点后采购价，
     毛利率 = 京东毛利合计 ÷ 结算给我们的扣点后采购价合计。雨润禾账号常温品低于 10%、冷冻品低于 18% 触发毛保，
     诊断里给出触发温层与需补利润（= 毛保线 × 结算采购额 − 实际毛利额）。</p>
</div>

</div>
<script>window.QRANGE_TODAY = "{today_iso}";</script>
<script>{QUICK_RANGE_JS}</script>
<script>window.PC_INIT = {json.dumps(init, ensure_ascii=False)};</script>
<script>{PROFIT_JS}</script>
</body></html>""")


def _mb_num(v, kind: str = "money") -> str:
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if kind == "int":
        return f"{int(n):,}"
    if kind == "pct":
        return f"{n * 100:.2f}%"
    return f"{n:,.2f}"


def _mb_group_card(g: dict) -> str:
    threshold = g.get("threshold")
    if threshold is None:
        badge = '<span class="badge b-idle">未配置毛保线</span>'
    elif (g.get("settle") or 0) <= 0:
        badge = '<span class="badge b-idle">无结算数据</span>'
    elif g.get("triggered"):
        badge = '<span class="badge b-bad">触发毛保</span>'
    else:
        badge = '<span class="badge b-ok">达标</span>'
    line = _mb_num(threshold, "pct") if threshold is not None else "未配置"
    shortfall = (f'<span>需补利润 <b style="color:var(--redink)">¥{_mb_num(g.get("shortfall"))}</b></span>'
                 if g.get("triggered") else "")
    return (
        '<div class="card" style="margin-top:0">'
        f'<h2 style="margin:0 0 10px;font-size:16px">{_e(g.get("label"))}'
        f' <span class="tagline">毛保线 {_e(line)}</span> {badge}</h2>'
        '<div class="tm" style="margin:0">'
        f'<span>成交件数 <b>{_mb_num(g.get("qty"), "int")}</b></span>'
        f'<span>前台成交额 <b>¥{_mb_num(g.get("sale"))}</b></span>'
        f'<span>结算采购额 <b>¥{_mb_num(g.get("settle"))}</b></span>'
        f'<span>京东毛利额 <b>¥{_mb_num(g.get("profit"))}</b></span>'
        f'<span>京东毛利率 <b>{_mb_num(g.get("margin"), "pct")}</b></span>'
        f'{shortfall}</div></div>'
    )


def _mb_html(diag: dict, acc_label: str, start: str, end: str, saved_at: str = "") -> str:
    groups = diag.get("groups") or []
    total = diag.get("total") or {}
    skipped = diag.get("skipped") or []
    shortfall = sum(float(g.get("shortfall") or 0) for g in groups)

    warn = ""
    if not diag.get("rules"):
        warn = ('<div class="warn">当前账号「' + _e(acc_label) + '」没有配置毛保线，下面只测算京东毛利，不判定触发。'
                '已配置：雨润禾（常温 &lt; 10%、冷冻 &lt; 18%）。</div>')
    elif skipped:
        names = "、".join(f'{_e(x.get("skuId"))} {_e(str(x.get("itemName") or "")[:16])}'
                         for x in skipped[:5])
        warn = (f'<div class="warn">{len(skipped)} 个 SKU 缺平均前台价或扣点后采购价，未计入测算：'
                f'{names}{" 等" if len(skipped) > 5 else ""}。可先到「月度利润」点「只更新 ERP 成本」再试。</div>')

    rows = []
    for g in groups:
        for it in (g.get("items") or []):
            rows.append(
                '<tr>'
                f'<td class="l">{"冷冻" if it.get("temp") == "frozen" else "常温"}</td>'
                f'<td class="l"><b>{_e(it.get("skuId"))}</b></td>'
                f'<td class="l nm" title="{_e(it.get("itemName"))}">{_e(it.get("itemName") or "—")}</td>'
                f'<td class="n">{_mb_num(it.get("qty"), "int")}</td>'
                f'<td class="n">¥{_mb_num(it.get("frontPrice"))}</td>'
                f'<td class="n">¥{_mb_num(it.get("supply"))}</td>'
                f'<td class="n">¥{_mb_num(it.get("unitProfit"))}</td>'
                f'<td class="n">¥{_mb_num(it.get("profit"))}</td>'
                f'<td class="n">¥{_mb_num(it.get("settle"))}</td>'
                f'<td class="n">{_mb_num(it.get("margin"), "pct")}</td>'
                '</tr>')
    table = ('<div class="tblwrap long" style="margin-top:14px"><table><thead><tr>'
             '<th class="l">温层</th><th class="l">SKU</th><th class="l">商品名称</th>'
             '<th class="n">成交件数</th><th class="n">平均前台价</th><th class="n">扣点后采购价</th>'
             '<th class="n">京东毛利/件</th><th class="n">京东毛利额</th><th class="n">结算采购额</th>'
             '<th class="n">毛利率</th></tr></thead><tbody>'
             + ("".join(rows) or '<tr><td class="l" colspan="10">没有可用于测算的 SKU：需要同时有平均前台价与扣点后采购价。</td></tr>')
             + '</tbody></table></div>')

    return (
        f'<div class="hint" style="margin:0 0 12px">{_e(acc_label)} · {_e(start)} ~ {_e(end)}'
        + (f' · 利润缓存 {_e(saved_at)}' if saved_at else '') + '</div>'
        + warn
        + '<div class="grid g2">' + "".join(_mb_group_card(g) for g in groups) + '</div>'
        + '<div class="tm" style="margin-top:14px">'
        f'<span>合计：成交 <b>{_mb_num(total.get("qty"), "int")}</b> 件</span>'
        f'<span>前台成交额 <b>¥{_mb_num(total.get("sale"))}</b></span>'
        f'<span>结算采购额 <b>¥{_mb_num(total.get("settle"))}</b></span>'
        f'<span>京东毛利额 <b>¥{_mb_num(total.get("profit"))}</b></span>'
        f'<span>京东毛利率 <b>{_mb_num(total.get("margin"), "pct")}</b></span>'
        f'<span>触发补差合计 <b>¥{_mb_num(shortfall)}</b></span></div>'
        + table
    )


@app.get("/maobao", response_class=HTMLResponse)
def maobao_page(request: Request):
    """毛利诊断（京东毛保）独立页：读本地利润缓存，按温层判定是否触发毛保。"""
    q = request.query_params
    keys = [a["key"] for a in settings.ACCOUNTS]
    labels = {a["key"]: (a.get("label") or a["key"]) for a in settings.ACCOUNTS}
    acc = q.get("account") or next((k for k in keys if maobao.rules_for(k)), settings.MAIN_ACCOUNT)
    if acc not in keys:
        acc = settings.MAIN_ACCOUNT
    start, end = _last_month_range()
    if q.get("start") and q.get("end"):
        try:
            start, end = profit.norm_range(q.get("start"), q.get("end"))
        except ValueError:
            pass

    opts = "".join(
        f'<option value="{_e(k)}"{" selected" if k == acc else ""}>{_e(labels[k])}'
        f'{"（未配置毛保线）" if not maobao.rules_for(k) else ""}</option>'
        for k in keys)

    data = _profit.load_cached(acc, start, end)
    if not data:
        body = ('<div class="warn">本地没有 '
                f'{_e(start)} ~ {_e(end)} 的 {_e(labels.get(acc, acc))} 利润缓存。先到 '
                f'<a href="/profit?account={_e(acc)}&start={_e(start)}&end={_e(end)}">月度利润</a>'
                ' 查询这个区间（有缓存秒出），再回本页诊断。</div>')
    else:
        diag = maobao.diagnose(data.get("rows") or [], acc, labels.get(acc))
        body = _mb_html(diag, data.get("accountLabel") or labels.get(acc, acc),
                        start, end, data.get("savedAt") or "")

    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>毛利诊断 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{PAYCOMM_CSS}</style>
</head><body>
{report2.navtop("/maobao")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}毛保</div>
  <h1>毛利诊断 · 京东毛保测算</h1>
  <div class="metaline">
    <span>京东毛利 = 平均前台价 − 扣点后采购价</span><span class="dot"></span>
    <span>数据与「月度利润」同一份本地缓存</span>
  </div>
  <div class="actions">
    <a class="btn ghost mini" href="/profit?account={_e(acc)}&start={_e(start)}&end={_e(end)}">月度利润 →</a>
    <a class="btn ghost mini" href="/cache?kind=profit">缓存管理 →</a>
  </div>
</div></header>
<div class="wrap">

<div class="card">
  <form class="toolbar" method="get" action="/maobao">
    <label class="fld">账号 <select name="account" onchange="this.form.submit()">{opts}</select></label>
    <label class="fld">起 <input type="date" name="start" value="{_e(start)}"></label>
    <label class="fld">止 <input type="date" name="end" value="{_e(end)}"></label>
    <button class="btn" type="submit">查询</button>
  </form>
  <div class="hint" style="margin-bottom:0">只读本地缓存，不触发抓取；缓存缺失或过期时去「月度利润」重算。</div>
</div>

<div style="margin-top:18px">{body}</div>

<div class="note" style="margin-top:18px">
  <p><b>口径</b>：平均前台价取利润行（商智成交额 ÷ 成交件数）；扣点后采购价 = ERP 到手结算价。
     京东毛利/件 = 平均前台价 − 扣点后采购价；毛利率 = 京东毛利合计 ÷ 结算给我们的扣点后采购价合计。</p>
  <p><b>毛保</b>：雨润禾常温品低于 10%、冷冻品低于 18% 触发。
     需补利润 = 毛保线 × 结算采购额 − 实际毛利额，即京东要补到毛保线还差的利润。</p>
  <p><b>温层判定</b>：已登记 SKU 按登记表；未登记的按商品名关键词（冷冻 / 速冻 / 解冻 / 薯条 / 什锦蔬菜 /
     欧式杂菜 / 毛豆 / 青豆 / 纳豆 → 冷冻，冻干 → 常温），都没有按常温。</p>
</div>

</div></body></html>""")


@app.post("/api/profit/run")
def api_profit_run(account: str | None = None, start: str = "", end: str = "", force: int = 0,
                   accounts: str = ""):
    """按 SKU 跨账号合并（花费跟着投放账号走）。

    页面下拉选中的账号排第一：结果默认展示它，其余账号只为跨账号归集成交/花费。
    """
    try:
        wanted = [x for x in (accounts or "").split(",") if x.strip()]
        all_keys = [a["key"] for a in settings.ACCOUNTS]
        if wanted:
            keys = [k for k in all_keys if k in wanted] or wanted
        elif account and account in all_keys:
            keys = [account] + [k for k in all_keys if k != account]
        else:
            keys = all_keys
        if not keys:
            return {"ok": False, "error": "没有可用的账号"}
        return _profit.run_background(keys[0], start, end, force=bool(force), accounts=keys)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/profit/status")
def api_profit_status(rows: int = 0, job: str | None = None):
    snap = _profit.snapshot(with_rows=bool(rows), job=job)
    snap["ok"] = True
    snap["active"] = snap.pop("running", False)
    return snap


@app.post("/api/profit/reprice")
def api_profit_reprice(job: str | None = None, account: str | None = None,
                       start: str | None = None, end: str | None = None):
    """只重拉 ERP 成本并重算利润；商智成交与 CPS/京准通花费沿用缓存。"""
    return _profit.reprice(job, account, start, end)


@app.get("/api/profit/export")
def api_profit_export(job: str | None = None, account: str | None = None,
                      start: str | None = None, end: str | None = None,
                      dim: str = "sku"):
    from urllib.parse import quote
    got = _profit.export(job, account=account, start=start, end=end, dim=dim)
    if not got:
        return JSONResponse({"ok": False, "error": "没有可导出的结果，请先查询一次"}, status_code=404)
    data, fname = got
    disp = f"attachment; filename=\"profit.xlsx\"; filename*=UTF-8''{quote(fname)}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp, "Cache-Control": "no-store"})


# ================================================================ 团长招商活动
ACTIVITY_CSS = """
.acttabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
.acttabs a{padding:6px 14px;border-radius:9999px;border:1px solid var(--hair);
  color:var(--muted);text-decoration:none;font-size:12.5px;background:var(--canvas)}
.acttabs a.on{background:var(--surface-card);color:var(--ink);border-color:var(--cream);font-weight:600}
.actrow{display:grid;grid-template-columns:74px 1fr 64px 64px;gap:10px;align-items:center;
  font-size:12.5px;padding:3px 0;border-bottom:1px dashed var(--hair2)}
.actrow:last-child{border-bottom:none}
.actrow .bar{height:10px;border-radius:9999px;background:var(--hair);overflow:hidden}
.actrow .bar i{display:block;height:100%;background:var(--primary);border-radius:9999px}
.actrow .n{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)}
.actsec{margin-top:26px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.actsec h2{font-family:var(--serif);font-weight:400;font-size:22px;color:var(--ink);margin:0}
.actsec .hint{margin:0}
td.sku{max-width:340px}
td.sku .nm{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.sku .sub,td.act .sub{font-size:11.5px;color:var(--muted2)}
td.act{max-width:260px}
td.act .nm{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.act .sub{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tagb{display:inline-block;padding:1px 8px;border-radius:9999px;font-size:11px;font-weight:600;
  white-space:nowrap}
.tagb.on{background:var(--greenbg);color:var(--greenink)}
.tagb.end{background:var(--soft);color:var(--muted)}
.tagb.wait{background:var(--amberbg);color:var(--amberink)}
.tagb.off{background:var(--redbg);color:var(--redink)}
tr.rowlink{cursor:pointer}
tr.rowlink:hover td{background:var(--soft)}
tr.selrow td{background:#fbf1eb}
/* 效果区紧凑化：5 个 KPI 一行、卡片留白收窄、表格行高压缩 */
.grid.g5{grid-template-columns:repeat(5,minmax(0,1fr))}
#effectWrap .actsec{margin-top:18px}
#effectWrap .kpi{padding:12px 14px}
#effectWrap .kpi .lbl{font-size:11px}
#effectWrap .kpi .val{font-size:22px}
#effectWrap .kpi .sub{font-size:11.5px}
#effectWrap .card{padding:16px 18px}
#effectWrap .toolbar{margin-bottom:10px}
#effectWrap th,#effectWrap td{padding:7px 9px;font-size:12.5px}
#effectWrap .actrow{padding:2px 0}
#effectWrap .tblwrap{max-height:320px}
#effectWrap .acttabs{margin-bottom:8px}
#effectWrap .acttabs a{padding:4px 12px;font-size:12px}
#effectWrap .warn{margin:8px 0}
.actctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between}
.actctl .toolbar{margin:0}
.inpq{width:190px;height:30px;border:1px solid var(--hair);border-radius:8px;padding:0 10px;
  font-size:12.5px;background:#fff;color:var(--ink);outline:none}
.inpq:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(196,90,59,.10)}
@media(max-width:1100px){.grid.g5{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:760px){.grid.g5{grid-template-columns:repeat(2,minmax(0,1fr))}}
/* 报名日历（GitHub 贡献图风格）：行=星期，列=上周/本周/下周 */
.hmwrap{display:flex;gap:26px;align-items:flex-start;flex-wrap:wrap}
.hm{display:grid;grid-template-columns:34px repeat(3,34px);gap:5px;justify-content:start}
.hmwd{display:flex;align-items:center;justify-content:flex-end;padding-right:6px;
  font-size:11px;color:var(--muted)}
.hmwk{display:flex;flex-direction:column;align-items:center;gap:1px;padding:0 0 2px;
  font-size:11px;font-weight:600;color:var(--strong);line-height:1.25}
.hmwk span{font-weight:400;color:var(--muted2);font-size:9px}
.hmcell{display:flex;justify-content:center;align-items:center;width:34px;height:34px;
  padding:0 4px;border-radius:7px;border:1px solid var(--hair);text-decoration:none;
  color:var(--ink);background:var(--soft);transition:transform .12s,box-shadow .12s}
.hmcell .hmd{font-size:10px;color:var(--muted)}
.hmcell .hmc{font-family:var(--serif);font-size:14px;line-height:1}
.hmcell.lv0{background:var(--soft);color:var(--muted2)}
.hmcell.lv1{background:#dcece1;border-color:#c7e0cf}
.hmcell.lv2{background:#cfe6d5;border-color:#b4d9bf}
.hmcell.lv3{background:#9ed3ac;border-color:#86c898}
.hmcell.lv4{background:#5db872;border-color:#4aa463;color:#fff}
.hmcell.lv4 .hmd{color:#eaf5ec}
.hmcell.today{box-shadow:inset 0 0 0 2px var(--primary)}
.hmcell.sel{outline:2px solid var(--primary);outline-offset:2px}
.hmcell.future{border-style:dashed}
.hmcell:hover{transform:translateY(-1px);box-shadow:var(--shadow-soft)}
/* 右侧：近 21 天报名分布 */
.hmside{flex:1;min-width:260px}
.hmside-h{font-size:12px;font-weight:600;color:var(--strong);margin-bottom:12px}
.hmchart{display:flex;align-items:flex-end;gap:4px}
.hmbar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;min-width:0}
.hmbar i{display:block;width:100%;border-radius:3px 3px 0 0;background:var(--soft);
  border:1px solid var(--hair);border-bottom:none}
.hmbar i.lv1{background:#dcece1;border-color:#c7e0cf}
.hmbar i.lv2{background:#cfe6d5;border-color:#b4d9bf}
.hmbar i.lv3{background:#9ed3ac;border-color:#86c898}
.hmbar i.lv4{background:#5db872;border-color:#4aa463}
.hmbar i.today{box-shadow:inset 0 0 0 2px var(--primary)}
.hmbar b{font-size:9.5px;font-weight:400;color:var(--muted2);line-height:14px}
.hmbar .hmv{font-size:10px;font-weight:600;color:var(--muted);line-height:13px;height:13px}
.hmmeta{display:flex;flex-wrap:wrap;gap:16px;margin-top:10px;font-size:11.5px;color:var(--muted)}
.hmmeta b{color:var(--strong);font-weight:600}
.hmfoot{display:flex;justify-content:space-between;align-items:center;gap:12px;
  flex-wrap:wrap;margin-top:12px}
.hmleg{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;color:var(--muted)}
.hmleg i{display:block;width:14px;height:14px;border-radius:4px;border:1px solid var(--hair)}
.hmleg i.lv0{background:var(--soft)}
.hmleg i.lv1{background:#dcece1;border-color:#c7e0cf}
.hmleg i.lv2{background:#cfe6d5;border-color:#b4d9bf}
.hmleg i.lv3{background:#9ed3ac;border-color:#86c898}
.hmleg i.lv4{background:#5db872;border-color:#4aa463}
"""

ACT_JS = r"""
(function(){
  var TIMER = null;
  function tip(s, cls){
    var el = document.getElementById('actTip');
    if(!el) return;
    el.className = 'note' + (cls ? ' ' + cls : '');
    el.style.display = s ? 'block' : 'none';
    el.textContent = s || '';
  }
  window.actRun = function(){
    var b = document.getElementById('actRunBtn');
    if(b){ b.disabled = true; b.textContent = '抓取中…'; }
    tip('已提交抓取任务，等待后台执行…');
    fetch('/api/activity/run', {method:'POST'}).then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){
          tip('启动失败：' + (j.error || '未知错误'));
          if(b){ b.disabled = false; b.textContent = '立即抓取'; }
          return;
        }
        if(TIMER) clearInterval(TIMER);
        TIMER = setInterval(poll, 3000);
      }).catch(function(e){
        tip('启动失败：' + e);
        if(b){ b.disabled = false; b.textContent = '立即抓取'; }
      });
  };
  function poll(){
    fetch('/api/activity/status').then(function(r){ return r.json(); }).then(function(s){
      if(s.active){ tip('抓取中：' + (s.progress || '执行中…')); return; }
      if(TIMER){ clearInterval(TIMER); TIMER = null; }
      var b = document.getElementById('actRunBtn');
      if(b){ b.disabled = false; b.textContent = '立即抓取'; }
      if(s.error){ tip('抓取失败：' + s.error); return; }
      tip('抓取完成，正在刷新…');
      setTimeout(function(){ location.reload(); }, 600);
    }).catch(function(){});
  }
  if(window.ACT_RUNNING){ TIMER = setInterval(poll, 3000); }

  // 报名明细整行可点：AJAX 局部刷新「活动效果」，不跳 URL、不刷新整页
  function actParam(over){
    var p = {}, base = window.ACT_CTX || {};
    Object.keys(base).forEach(function(k){ p[k] = base[k]; });
    Object.keys(over || {}).forEach(function(k){ p[k] = over[k]; });
    var out = [];
    Object.keys(p).forEach(function(k){
      var v = p[k];
      if(v !== '' && v !== null && v !== undefined){
        out.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      }
    });
    return out.join('&');
  }
  window.actLoad = function(opts, tr){
    opts = opts || {};
    var ctx = window.ACT_CTX || {};
    var over = {act: ('act' in opts) ? opts.act : (ctx.act || ''),
                sku: ('sku' in opts) ? opts.sku : (ctx.sku || '')};
    if(opts.start || opts.end){
      over.start = opts.start || '';
      over.end = opts.end || '';
      over.days = '';
    } else if(opts.days){
      over.days = String(opts.days);
      over.start = '';
      over.end = '';
    }
    var wrap = document.getElementById('effectWrap');
    if(!wrap) return;
    wrap.style.opacity = '.45';
    fetch('/api/activity/effect?' + actParam(over))
      .then(function(r){ return r.text(); })
      .then(function(html){
        wrap.innerHTML = html;
        wrap.style.opacity = '1';
        // 记住当前活动与区间，后续点行 / 切区间都沿用，不会回到“今天”
        ctx.act = over.act;
        ctx.sku = over.sku;
        if('start' in over){ ctx.start = over.start; ctx.end = over.end; ctx.days = ''; }
        if('days' in over){ ctx.days = over.days; ctx.start = ''; ctx.end = ''; }
        var rs = document.querySelectorAll('tr.rowlink');
        for(var i = 0; i < rs.length; i++){ rs[i].classList.remove('selrow'); }
        if(tr){ tr.classList.add('selrow'); }
        try{ wrap.scrollIntoView({behavior: 'smooth', block: 'start'}); }catch(e){}
      })
      .catch(function(){ wrap.style.opacity = '1'; });
  };
  window.actShowAll = function(){ window.actLoad({act: '', sku: ''}); };
  window.actFilterRows = function(){
    var box = document.getElementById('signupQ');
    var tb = document.querySelector('#signup tbody');
    if(!box || !tb) return;
    var q = (box.value || '').trim().toLowerCase();
    var rs = tb.querySelectorAll('tr'), n = 0;
    for(var i = 0; i < rs.length; i++){
      var tr = rs[i];
      // 只匹配「商品」和「团长」两列
      var txt = ((tr.cells[2] ? tr.cells[2].innerText : '') + ' ' +
                 (tr.cells[4] ? tr.cells[4].innerText : '')).toLowerCase();
      var ok = !q || txt.indexOf(q) >= 0;
      tr.style.display = ok ? '' : 'none';
      if(ok) n++;
    }
    var c = document.getElementById('signupCount');
    if(c) c.textContent = n;
  };
  window.actRange = function(d){ window.actLoad({days: d}); };
  window.actRangeCustom = function(){
    var wrap = document.getElementById('effectWrap');
    if(!wrap) return;
    var form = wrap.querySelector('form');
    if(!form) return;
    var s = form.querySelector('input[name="start"]');
    var e = form.querySelector('input[name="end"]');
    if(!s || !e || !s.value || !e.value) return;
    window.actLoad({start: s.value, end: e.value});
  };
  document.querySelectorAll('tr.rowlink[data-act]').forEach(function(tr){
    tr.addEventListener('click', function(e){
      if(e.target.closest('a,button,input,select')) return;
      e.preventDefault();
      var s = tr.getAttribute('data-start') || '';
      var en = tr.getAttribute('data-end') || '';
      var opts = {act: tr.getAttribute('data-act'), sku: tr.getAttribute('data-sku')};
      if(s && en){ opts.start = s; opts.end = en; }   // 直接定位到该商品的活动起止
      window.actLoad(opts, tr);
    });
  });
})();
"""


def _an(v, digits: int = 0) -> str:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    if digits:
        return f"{f:,.{digits}f}"
    return f"{int(round(f)):,}"


def _amoney(v) -> str:
    return _an(v, 2)


def _act_sku_names(keys: list) -> dict:
    """从本地商智商品明细拼 skuId→商品名（报名日志里只有 SKU）。"""
    names: dict = {}
    for key in keys:
        try:
            days = ds.days_for(key, "sz_product")[-7:]
        except Exception:  # noqa: BLE001
            days = []
        for day in reversed(days):
            payload = ds.load_day(key, day, "sz_product") or {}
            for r in payload.get("rows") or []:
                sid = str(r.get("sku_id") or "").strip()
                if sid and sid != "合计" and r.get("name"):
                    names.setdefault(sid, str(r.get("name")))
    return names


def _act_status(start: str, end: str) -> tuple:
    day = _today().isoformat()
    if start and day < start:
        return ("未开始", "wait")
    if end and day > end:
        return ("已结束", "end")
    if start or end:
        return ("进行中", "on")
    return ("—", "end")


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    q = request.query_params
    keys = [a["key"] for a in settings.ACCOUNTS]
    labels = {a["key"]: (a.get("label") or a["key"]) for a in settings.ACCOUNTS}
    sel = q.get("account") or ""
    acc_keys = [sel] if sel in keys else keys

    # 效果区间：默认昨天；支持 days=1/7/30 或自定义 start/end
    start, end = resolve_range(q.get("start"), q.get("end"), q.get("days") or 1)
    since = q.get("since") or (_today() - dt.timedelta(days=90)).isoformat()
    kw = (q.get("q") or "").strip()
    union_sel = q.get("union") or ""

    signups = activity.load_signups(acc_keys, since=since)
    acts = activity.load_activities(acc_keys)
    items, unions = acts.get("items") or {}, acts.get("unions") or {}
    sku_names = _act_sku_names(acc_keys)

    # 团长筛选选项（报名记录里出现过的 + 本地活动快照里的）
    union_opts = {}
    for r in signups:
        uid = str(r.get("cpUnionId") or "")
        if uid:
            union_opts[uid] = r.get("cpUnionName") or (unions.get(uid) or {}).get("name") or uid
    for uid, u in unions.items():
        union_opts.setdefault(uid, u.get("name") or uid)

    # 报名日历：start = 按该商品活动开始日（含未来/预报名），signup = 按报名操作时间
    hm_mode = q.get("hm") if q.get("hm") in ("start", "signup") else "start"
    day_sel = (q.get("day") or "").strip()
    act_sel = (q.get("act") or "").strip()
    sku_sel = (q.get("sku") or "").strip()

    filtered = []
    for r in signups:
        if union_sel and str(r.get("cpUnionId") or "") != union_sel:
            continue
        name = sku_names.get(str(r.get("skuId"))) or ""
        hay = " ".join(str(r.get(x) or "") for x in
                       ("skuId", "activityName", "activityId", "cpUnionName", "activityName"))
        if kw and kw.lower() not in (hay + " " + name).lower():
            continue
        filtered.append((r, name))

    def _hm_day(r) -> str:
        if hm_mode == "signup":
            return str(r.get("optTime") or "")[:10]
        return str(r.get("startDate") or "")

    hm_counts: dict = {}
    for r, _n in filtered:
        d = _hm_day(r)
        if d:
            hm_counts[d] = hm_counts.get(d, 0) + 1

    # 点击报名行 → 只看这个活动的数据（按该活动报名过的 SKU 归集）
    act_all = [r for r in signups if str(r.get("activityId")) == act_sel] if act_sel else []
    act_skus = {str(r.get("skuId")) for r in act_all if r.get("skuId")}
    if sku_sel:
        act_skus.add(sku_sel)
    act_info = (items.get(act_sel) or {}) if act_sel else {}
    act_union_ids = {str(r.get("cpUnionId")) for r in act_all if r.get("cpUnionId")}
    if act_info.get("cpUnionId"):
        act_union_ids.add(str(act_info["cpUnionId"]))
    act_title = act_info.get("title") or (act_all[0].get("activityName") if act_all else "") or sku_sel

    rows = filtered
    if act_sel:
        rows = [(r, n) for r, n in rows if str(r.get("activityId")) == act_sel]
    if day_sel:
        rows = [(r, n) for r, n in rows if _hm_day(r) == day_sel]

    n_sku = len({r.get("skuId") for r, _ in rows})
    n_union = len({r.get("cpUnionId") for r, _ in rows if r.get("cpUnionId")})
    n_act = len({r.get("activityId") for r, _ in rows})

    # ---- 近 21 天报名日历（上周 / 本周 / 下周）----
    from urllib.parse import quote
    today_iso = _today().isoformat()
    monday = _today() - dt.timedelta(days=_today().weekday())
    weeks = [monday - dt.timedelta(days=7), monday, monday + dt.timedelta(days=7)]
    wk_labels = ["上周", "本周", "下周"]
    days_q = q.get("days") or ""

    def _qlink(**over) -> str:
        base = {"account": sel, "since": since, "q": kw, "union": union_sel,
                "days": days_q, "hm": hm_mode, "day": day_sel,
                "act": act_sel, "sku": sku_sel}
        base.update(over)
        return "?" + "&".join(f"{k}={quote(str(v))}" for k, v in base.items()
                              if v not in (None, ""))

    max_c = max([hm_counts.get((ws + dt.timedelta(days=i)).isoformat(), 0)
                 for ws in weeks for i in range(7)] or [0]) or 1
    wd_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    hm_head = '<div class="hmwd"></div>' + "".join(
        f'<div class="hmwk" title="{ws.isoformat()} ~ '
        f'{(ws + dt.timedelta(days=6)).isoformat()}">{lab}'
        f'<span>{ws.month:02d}/{ws.day:02d}</span></div>'
        for lab, ws in zip(wk_labels, weeks))
    hm_cells = ""
    for i, wd in enumerate(wd_names):
        hm_cells += f'<div class="hmwd">{wd}</div>'
        for ws in weeks:
            d = ws + dt.timedelta(days=i)
            ds_iso = d.isoformat()
            c = hm_counts.get(ds_iso, 0)
            lv = 0 if not c else max(1, min(4, int(round(c / max_c * 4)) or 1))
            cls = f"lv{lv}"
            if ds_iso == today_iso:
                cls += " today"
            if ds_iso == day_sel:
                cls += " sel"
            if ds_iso > today_iso:
                cls += " future"
            href = _qlink(day="" if ds_iso == day_sel else ds_iso) + "#signup"
            tip = f"{ds_iso} · {c} 条报名" + ("（未来开始，预报名）" if ds_iso > today_iso else "")
            hm_cells += (f'<a class="hmcell {cls}" href="{href}" title="{_e(tip)}">'
                         f'<span class="hmd">{d.day}</span></a>')

    # 右侧：近 21 天报名分布条 + 概览
    days_all = [weeks[0] + dt.timedelta(days=i) for i in range(21)]
    bars = []
    for d in days_all:
        ds_iso = d.isoformat()
        c = hm_counts.get(ds_iso, 0)
        lv = 0 if not c else max(1, min(4, int(round(c / max_c * 4)) or 1))
        cls = f"lv{lv}" if c else "lv0"
        if ds_iso == today_iso:
            cls += " today"
        hpx = max(3, round(c / max_c * 118)) if c else 3
        tip = f"{ds_iso} · {c} 条报名" + ("（未来开始，预报名）" if ds_iso > today_iso else "")
        bars.append(f'<div class="hmbar" title="{_e(tip)}">'
                    f'<b class="hmv">{c if c else ""}</b>'
                    f'<i class="{cls}" style="height:{hpx}px"></i>'
                    f'<b>{d.day}</b></div>')
    active_days = sum(1 for d in days_all if hm_counts.get(d.isoformat(), 0))
    week_cnt = sum(hm_counts.get((monday + dt.timedelta(days=i)).isoformat(), 0)
                   for i in range(7))
    peak = max(days_all, key=lambda d: hm_counts.get(d.isoformat(), 0), default=None)
    peak_c = hm_counts.get(peak.isoformat(), 0) if peak else 0
    peak_txt = f"峰值 {peak.isoformat()[5:]} · {peak_c} 条" if peak_c else "暂无报名"
    hm_side = (
        '<div class="hmside">'
        '<div class="hmside-h">近 21 天报名分布</div>'
        f'<div class="hmchart">{"".join(bars)}</div>'
        '<div class="hmmeta">'
        f'<span>本周 <b>{week_cnt}</b> 条</span>'
        f'<span>有报名 <b>{active_days}</b> 天</span>'
        f'<span>{peak_txt}</span></div></div>')

    total_hm = sum(hm_counts.values())
    future_cnt = (sum(1 for r, _n in filtered
                      if str(r.get("startDate") or "") > today_iso) if hm_mode == "start" else 0)
    no_start = (sum(1 for r, _n in filtered if not r.get("startDate"))
                if hm_mode == "start" else 0)
    hm_tabs = (
        f'<a class="chip{" on" if hm_mode == "start" else ""}" '
        f'href="{_qlink(hm="start", day="")}">按活动开始日（含预报名）</a>'
        f'<a class="chip{" on" if hm_mode == "signup" else ""}" '
        f'href="{_qlink(hm="signup", day="")}">按报名时间</a>')
    hm_card = (
        '<div class="card" style="margin-top:16px">'
        '<div class="toolbar" style="justify-content:space-between;margin-bottom:12px">'
        f'<div><b>报名日历</b> <span class="hint" style="margin:0">近 21 天 · 共 {total_hm} 条'
        f'{"（未来开始 " + str(future_cnt) + " 条预报名）" if future_cnt else ""}</span></div>'
        f'<div class="toolbar" style="margin:0">{hm_tabs}</div></div>'
        f'<div class="hmwrap"><div class="hm">{hm_head}{hm_cells}</div>{hm_side}</div>'
        '<div class="hmfoot"><span class="hint" style="margin:0">点击格子筛选当天明细，再点一次取消；'
        '虚线格 = 未来日期（预报名）'
        + (f'；官方活动无开始日，未计入 {no_start} 条' if no_start and hm_mode == "start" else "")
        + '</span><span class="hmleg">少 <i class="lv0"></i><i class="lv1"></i>'
        '<i class="lv2"></i><i class="lv3"></i><i class="lv4"></i> 多</span></div>'
        '</div>')

    # ---- 报名明细 ----
    trs = []
    for r, name in rows[:800]:
        st_txt, st_cls = _act_status(r.get("startDate") or "", r.get("endDate") or "")
        act_sub = ""
        if r.get("actStart") or r.get("actEnd"):
            act_sub = (f'<span class="sub">活动期 {_e(r.get("actStart") or "?")} ~ '
                       f'{_e(r.get("actEnd") or "?")}</span>')
        union_txt = r.get("cpUnionName") or ""
        if not union_txt and r.get("cpUnionId"):
            union_txt = (unions.get(str(r.get("cpUnionId"))) or {}).get("name") or ""
        union_id_html = (f'<br><span class="sub">ID {_e(r.get("cpUnionId"))}</span>'
                         if r.get("cpUnionId") else "")
        trs.append(
            f'<tr class="rowlink{" selrow" if str(r.get("activityId")) == act_sel and str(r.get("skuId")) == sku_sel else ""}"'
            f' data-act="{_e(r.get("activityId"))}" data-sku="{_e(r.get("skuId"))}"'
            f' data-start="{_e(r.get("startDate") or "")}" data-end="{_e(r.get("endDate") or "")}"'
            f' data-href="{_e(_qlink(act=r.get("activityId"), sku=r.get("skuId")))}#effect">'
            f'<td class="n mono" style="white-space:nowrap">{_e(str(r.get("optTime") or "")[5:16])}</td>'
            f'<td class="l">{_e(r.get("accountLabel") or labels.get(r.get("account"), ""))}</td>'
            f'<td class="l sku"><span class="nm" title="{_e(name)}">{_e(name or "—")}</span>'
            f'<span class="sub">SKU {_e(r.get("skuId"))}</span></td>'
            f'<td class="l act"><span class="nm">{_e(r.get("activityName") or "—")}</span>'
            f'<span class="sub">ID {_e(r.get("activityId"))}'
            f'{" · 官方活动" if r.get("official") else ""}</span>{act_sub}</td>'
            f'<td class="l">{_e(union_txt or "—")}{union_id_html}</td>'
            f'<td class="n" style="white-space:nowrap">{_e(r.get("startDate") or "—")}'
            f'<br><span class="sub">至 {_e(r.get("endDate") or "—")}</span></td>'
            f'<td class="n">{_e(r.get("commission") if r.get("commission") is not None else "—")}%'
            f'<br><span class="sub">服务费 {_e(r.get("serviceFee") if r.get("serviceFee") is not None else "—")}%</span></td>'
            f'<td class="n"><span class="tagb {st_cls}">{st_txt}</span></td>'
            '</tr>')
    if not trs:
        trs.append('<tr><td colspan="8" class="l hint" style="padding:18px">'
                   '本地还没有报名记录。点右上角「立即抓取」，或等凌晨自动任务抓完后刷新。</td></tr>')

    day_note = ""
    if day_sel:
        metric = "活动开始" if hm_mode == "start" else "报名时间"
        day_note = (f' · <b>{metric} {_e(day_sel)}</b> '
                    f'<a class="btn ghost mini" href="{_qlink(day="")}">清除筛选</a>')
    act_note = ""
    if act_sel:
        act_note = (f' · <b>活动 {_e(act_title)}</b> '
                    f'<a class="btn ghost mini" href="{_qlink(act="", sku="")}">查看全部</a>')
    signup_card = (
        '<div class="card" id="signup">'
        '<div class="toolbar" style="justify-content:space-between">'
        '<div><b>报名明细</b> <span class="hint" style="margin:0">'
        f'<span id="signupCount">{len(rows)}</span> 条 · 报名时间 ≥ {_e(since)}'
        f'{" · 关键词「" + _e(kw) + "」" if kw else ""}{act_note}{day_note}</span></div>'
        '<div style="display:flex;gap:10px;align-items:center">'
        '<input id="signupQ" class="inpq" placeholder="搜商品 / 团长" oninput="actFilterRows()">'
        '<span class="hint" style="margin:0">点击任意一行 → 查看该活动自己的效果数据</span></div></div>'
        '<div class="tblwrap long" style="max-height:64vh"><table><thead><tr>'
        '<th class="l">报名时间</th><th class="l">账号</th><th class="l">商品</th>'
        '<th class="l">活动</th><th class="l">团长</th><th class="n">活动起止（该商品）</th>'
        '<th class="n">佣金 / 服务费</th><th class="n">状态</th>'
        '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></div></div>')

    # ---- 效果数据 ----
    perf = activity.load_perf(acc_keys, start, end)
    days = perf.get("days") or []
    tot = {"click": 0.0, "order": 0.0, "gmv": 0.0, "fee": 0.0}
    cp_map: dict = {}
    sku_map: dict = {}
    daily: dict = {}
    daily_act: dict = {}
    for d in days:
        s = d.get("summary") or {}
        tot["click"] += float(s.get("clickCnt") or 0)
        tot["order"] += float(s.get("inOrderCnt") or 0)
        day = d.get("date") or ""
        cur = daily.setdefault(day, {"click": 0.0, "order": 0.0})
        cur["click"] += float(s.get("clickCnt") or 0)
        cur["order"] += float(s.get("inOrderCnt") or 0)
        for row in d.get("cp") or []:
            uid = str(row.get("cpUnionId") or "")
            e = cp_map.setdefault(uid, {"name": row.get("cpUnionName") or
                                       (unions.get(uid) or {}).get("name") or uid,
                                       "accounts": set(), "click": 0.0, "order": 0.0,
                                       "gmv": 0.0, "fee": 0.0, "skus": set()})
            e["accounts"].add(d.get("account") or "")
            e["click"] += float(row.get("clickCnt") or 0)
            e["order"] += float(row.get("estimateOrderCnt") or 0)
            e["gmv"] += float(row.get("estimateGMV") or 0)
            e["fee"] += float(row.get("estimateFee") or 0)
            for spu in row.get("spuList") or []:
                e["skus"].add(str(spu))
        for row in d.get("sku") or []:
            sid = str(row.get("skuId") or "")
            e = sku_map.setdefault(sid, {"name": row.get("skuName") or sku_names.get(sid) or sid,
                                         "click": 0.0, "order": 0.0, "gmv": 0.0,
                                         "fee": 0.0, "unions": set()})
            e["click"] += float(row.get("clickCnt") or 0)
            e["order"] += float(row.get("estimateOrderCnt") or 0)
            e["gmv"] += float(row.get("estimateGMV") or 0)
            e["fee"] += float(row.get("estimateFee") or 0)
            if float(row.get("estimateOrderCnt") or 0) > 0:
                e["unions"].add(str(d.get("account")))
            if act_sel and sid in act_skus:
                ca = daily_act.setdefault(day, {"click": 0.0, "order": 0.0})
                ca["click"] += float(row.get("clickCnt") or 0)
                ca["order"] += float(row.get("estimateOrderCnt") or 0)
        tot["gmv"] += sum(float(r.get("estimateGMV") or 0) for r in d.get("sku") or [])
        tot["fee"] += sum(float(r.get("estimateFee") or 0) for r in d.get("sku") or [])

    sku_items = [(sid, e) for sid, e in sku_map.items()
                 if not act_sel or sid in act_skus]
    cp_items = [(uid, e) for uid, e in cp_map.items()
                if not act_sel or uid in act_union_ids]

    # 活动视图优先用订单明细：每单带 cpActId，能精确算“这个活动自己的订单/GMV/费用”
    act_ord = None
    order_days: dict = {}
    order_cov = 0
    if act_sel:
        odata = activity.load_orders(acc_keys, start, end)
        order_cov = len({x.get("date") for x in odata.get("days") or []})
        if order_cov:
            act_ord = {"orders": 0, "qty": 0, "gmv": 0.0, "fee": 0.0, "skus": {}}
            for x in odata["days"]:
                a = (x.get("acts") or {}).get(act_sel)
                if not a:
                    continue
                day = x.get("date") or ""
                day_fee = float(a.get("cosFee") or 0) + float(a.get("cpFee") or 0)
                act_ord["orders"] += int(a.get("orders") or 0)
                act_ord["qty"] += int(a.get("qty") or 0)
                act_ord["gmv"] += float(a.get("gmv") or 0)
                act_ord["fee"] += day_fee
                dcur = order_days.setdefault(day, {"orders": 0.0, "gmv": 0.0})
                dcur["orders"] += int(a.get("orders") or 0)
                dcur["gmv"] += float(a.get("gmv") or 0)
                for sid, sk in (a.get("skus") or {}).items():
                    sid = str(sid)
                    e = act_ord["skus"].setdefault(sid, {
                        "name": sk.get("name") or sku_names.get(sid) or sid,
                        "orders": 0, "qty": 0, "gmv": 0.0, "fee": 0.0})
                    e["orders"] += int(sk.get("orders") or 0)
                    e["qty"] += int(sk.get("qty") or 0)
                    e["gmv"] += float(sk.get("gmv") or 0)
                    e["fee"] += float(sk.get("cosFee") or 0) + float(sk.get("cpFee") or 0)
    use_orders = bool(act_sel and act_ord is not None)
    if act_sel and not use_orders:
        # 没有订单明细缓存时，退回按“报名商品”维度估算
        tot = {
            "click": sum(e["click"] for _sid, e in sku_items),
            "order": sum(e["order"] for _sid, e in sku_items),
            "gmv": sum(e["gmv"] for _sid, e in sku_items),
            "fee": sum(e["fee"] for _sid, e in sku_items),
        }
    roi = (tot["gmv"] / tot["fee"]) if tot["fee"] else 0.0
    act_roi = ((act_ord["gmv"] / act_ord["fee"]) if (act_ord and act_ord["fee"]) else 0.0)
    act_sku_cnt = len([e for e in (act_ord or {}).get("skus", {}).values() if e["orders"] > 0])
    n_order_union = len([e for _u, e in cp_items if e["order"] > 0])
    n_order_sku = len([e for _s, e in sku_items if e["order"] > 0])

    trend_src = order_days if use_orders else (daily_act if act_sel else daily)
    day_rows = []
    max_order = max([v.get("orders", v.get("order", 0)) for v in trend_src.values()] or [0]) or 1
    for day in ds.day_span(start, end):
        v = trend_src.get(day) or {"orders": 0.0, "order": 0.0, "click": 0.0, "gmv": 0.0}
        od = v.get("orders", v.get("order", 0))
        w = 0 if not od else max(int(round(od / max_order * 100)), 3)
        right = _amoney(v.get("gmv") or 0) if use_orders else f'{_an(v.get("click") or 0)} 点'
        day_rows.append(
            f'<div class="actrow"><div>{day[5:]}</div>'
            f'<div class="bar"><i style="width:{w}%"></i></div>'
            f'<div class="n">{_an(od)} 单</div>'
            f'<div class="n">{right}</div></div>')
    trend_hint = ("条形 = 该活动有效订单量；右侧为 GMV（订单明细口径）" if use_orders
                  else "条形 = 预估订单量；右侧为点击量")
    trend_card = ('<div class="card"><h3 style="margin-top:0">每日走势</h3>'
                  f'<div class="hint">{trend_hint}</div>'
                  + "".join(day_rows) + '</div>')

    cp_rows = sorted(cp_items, key=lambda kv: kv[1]["gmv"], reverse=True)
    cp_html = "".join(
        '<tr>'
        f'<td class="l">{_e(e["name"])}<span class="sub">ID {_e(uid)}</span></td>'
        f'<td class="l">{_e(" / ".join(sorted(labels.get(k, k) for k in e["accounts"] if k)))}</td>'
        f'<td class="n">{_an(e["click"])}</td><td class="n">{_an(e["order"])}</td>'
        f'<td class="n">{_amoney(e["gmv"])}</td><td class="n">{_amoney(e["fee"])}</td>'
        f'<td class="n"><b>{_an(e["gmv"] / e["fee"] if e["fee"] else 0, 2)}</b></td>'
        f'<td class="n">{len(e["skus"])}</td></tr>'
        for uid, e in cp_rows) or (
        '<tr><td colspan="8" class="l hint" style="padding:18px">'
        + (('该团长在所选区间没有出单数据' if act_union_ids
            else '该活动没有团长归因（官方活动）') if act_sel else '该区间没有出单团长数据')
        + '</td></tr>')

    if use_orders:
        sku_head = ('<th class="l">商品</th><th class="n">订单量</th><th class="n">件数</th>'
                    '<th class="n">预估 GMV</th><th class="n">预估费用</th><th class="n">ROI</th>')
        _ord_rows = sorted((act_ord or {}).get("skus", {}).items(),
                           key=lambda kv: kv[1]["gmv"], reverse=True)
        sku_html = "".join(
            '<tr>'
            f'<td class="l sku"><span class="nm" title="{_e(e["name"])}">{_e(e["name"])}</span>'
            f'<span class="sub">SKU {_e(sid)}</span></td>'
            f'<td class="n">{_an(e["orders"])}</td><td class="n">{_an(e["qty"])}</td>'
            f'<td class="n">{_amoney(e["gmv"])}</td><td class="n">{_amoney(e["fee"])}</td>'
            f'<td class="n"><b>{_an(e["gmv"] / e["fee"] if e["fee"] else 0, 2)}</b></td></tr>'
            for sid, e in _ord_rows) or (
            '<tr><td colspan="6" class="l hint" style="padding:18px">'
            '该活动在所选区间没有有效订单</td></tr>')
    else:
        sku_head = ('<th class="l">商品</th><th class="n">点击量</th><th class="n">预估订单</th>'
                    '<th class="n">预估 GMV</th><th class="n">预估费用</th><th class="n">ROI</th>'
                    '<th class="n">出单团长数</th>')
        sku_rows = sorted(sku_items, key=lambda kv: kv[1]["gmv"], reverse=True)
        sku_html = "".join(
            '<tr>'
            f'<td class="l sku"><span class="nm" title="{_e(e["name"])}">{_e(e["name"])}</span></td>'
            f'<td class="n">{_an(e["click"])}</td><td class="n">{_an(e["order"])}</td>'
            f'<td class="n">{_amoney(e["gmv"])}</td><td class="n">{_amoney(e["fee"])}</td>'
            f'<td class="n"><b>{_an(e["gmv"] / e["fee"] if e["fee"] else 0, 2)}</b></td>'
            f'<td class="n">{len(e["unions"])}</td></tr>'
            for _sid, e in sku_rows) or (
            '<tr><td colspan="7" class="l hint" style="padding:18px">'
            + ('该活动报名的商品在这个区间没有出单' if act_sel else '该区间没有商品出单数据')
            + '</td></tr>')

    # 活动视图的标题与横幅
    eff_title = "活动效果"
    eff_hint = f"{_e(start)} ~ {_e(end)} · 出单概览 / 出单团长 / 商品效果（按天累加）"
    cp_title = "出单团长"
    sku_title = "商品效果"
    act_banner = ""
    if act_sel:
        unames = "、".join((unions.get(u) or {}).get("name") or u
                           for u in sorted(act_union_ids)) or "—"
        period = ""
        if act_info.get("startTime") or act_info.get("endTime"):
            period = f" · 活动期 {act_info.get('startTime') or '?'} ~ {act_info.get('endTime') or '?'}"
        cp_tot = {"click": 0.0, "order": 0.0, "gmv": 0.0, "fee": 0.0}
        for _u, e in cp_items:
            for k in cp_tot:
                cp_tot[k] += e[k]
        if cp_items:
            cp_roi = cp_tot["gmv"] / cp_tot["fee"] if cp_tot["fee"] else 0
            only_union = sorted(act_union_ids)[0] if len(act_union_ids) == 1 else ""
            cp_link = (f' <a class="btn ghost mini" href="{_qlink(act="", sku="", union=only_union)}">'
                       '只看团长 →</a>') if only_union else ""
            cp_line = (f"所属团长整体（团长诊断口径，含其他活动）：{_an(cp_tot['click'])} 点击 · "
                       f"{_an(cp_tot['order'])} 单 · GMV {_amoney(cp_tot['gmv'])} · "
                       f"费用 {_amoney(cp_tot['fee'])} · ROI {_an(cp_roi, 2)}{cp_link}")
        else:
            cp_line = "所属团长在所选区间没有出单数据"
        eff_title = "活动效果 · " + _e(act_title)
        if use_orders:
            eff_hint = (f"{_e(start)} ~ {_e(end)} · 订单明细按活动ID精确归集"
                        f"（覆盖 {order_cov} 天）")
            sku_title = "商品效果（该活动有效订单）"
        else:
            eff_hint = (f"{_e(start)} ~ {_e(end)} · 暂无订单明细缓存，"
                        f"按系统记录的报名商品（{len(act_skus)} 个）估算")
            sku_title = "商品效果（系统记录的该活动报名商品）"
        cp_title = "出单团长（该团长整体，含其他活动）"
        banner_note = ("点击/订单/GMV/费用来自订单明细（每单按 cpActId 归集到本活动）；"
                       "团长表是该团长整体数据，含其他活动。")
        if not use_orders:
            banner_note = ("当前区间没有订单明细缓存，先按系统记录的报名商品估算；"
                           "团长表是该团长整体数据。")
        act_banner = (
            '<div class="card" id="effect" style="margin-top:16px;border-color:var(--primary)">'
            '<div class="toolbar" style="justify-content:space-between;margin-bottom:4px">'
            f'<div><b>正在查看：{_e(act_title)}</b>'
            f'<span class="hint" style="margin:0"> · 活动ID {_e(act_sel)} · 团长 {_e(unames)}{_e(period)}</span></div>'
            f'<div style="display:flex;gap:8px"><a class="btn ghost mini" href="#signup">看报名明细 ↑</a>'
            '<a class="btn sec mini" href="javascript:void(0)" '
            'onclick="actShowAll();return false;">查看全部</a></div></div>'
            f'<div class="hint" style="margin:0">{banner_note}</div>'
            f'<div class="hint" style="margin:6px 0 0;color:var(--ink)">{cp_line}</div>'
            '</div>')

    acc_opts = '<option value="">全部账号</option>' + "".join(
        f'<option value="{_e(k)}"{" selected" if k == sel else ""}>{_e(labels[k])}</option>'
        for k in keys)
    union_list_opts = '<option value="">全部团长</option>' + "".join(
        f'<option value="{_e(uid)}"{" selected" if uid == union_sel else ""}>{_e(name)}</option>'
        for uid, name in sorted(union_opts.items(), key=lambda kv: kv[1]))

    days_param = q.get("days") or ("1" if (start == end and end == _yesterday().isoformat()) else "")
    chips = []
    for d, lab in ((1, "昨天"), (7, "近 7 天"), (30, "近 30 天")):
        on = " on" if str(days_param) == str(d) else ""
        chips.append(f'<a class="chip{on}" href="javascript:void(0)" data-days="{d}" '
                     f'onclick="actRange({d});return false;">{lab}</a>')

    last_state = {}
    for k in acc_keys:
        st = activity._read(activity.acct_dir(k) / "state.json", {}) if k else {}
        if st.get("lastRun") and (not last_state.get("lastRun")
                                  or st["lastRun"] > last_state["lastRun"]):
            last_state = st
    run_state = activity.runtime_status()
    wait_days = max(len(ds.day_span(start, end)) - len({d.get("date") for d in days}), 0)
    warn = ""
    if not days:
        warn = ('<div class="warn">本地还没有这个区间的效果数据。点「立即抓取」补抓，'
                '或等凌晨自动任务；默认只回刷最近 '
                f'{settings.REFRESH_DAYS} 天。</div>')
    elif wait_days:
        warn = (f'<div class="hint">该区间有 {wait_days} 天还没有效果数据（可能超出回刷窗口）。</div>')
    act_warn = ""
    if act_sel and act_info:
        a_s = str(act_info.get("startTime") or "")
        a_e = str(act_info.get("endTime") or "")
        if a_s and a_e and (a_e < start or a_s > end):
            act_warn = (f'<div class="hint">该活动期为 {_e(a_s)} ~ {_e(a_e)}，与当前统计区间 '
                        f'{_e(start)} ~ {_e(end)} 不重叠；可切换「近 30 天」或自定义区间查看。</div>')
    act_zero = ""
    if use_orders and act_ord["orders"] == 0:
        act_zero = (f'<div class="warn">该活动在 {_e(start)} ~ {_e(end)} 没有有效订单'
                    '（订单明细口径，已按活动ID精确归集）。'
                    '若活动刚开始/还没开始，0 属正常；可切换更长区间查看。</div>')
    elif act_sel and not use_orders and tot["click"] == 0 and tot["order"] == 0:
        act_zero = (f'<div class="warn">系统记录的该活动报名商品在 {_e(start)} ~ {_e(end)} 没有出单'
                    '（活动可能刚开始、还没开始，或这段时间没有推客成交）。'
                    '上面的 KPI 为 0 属正常；可切换「近 30 天」看更长区间，'
                    '或参考横幅里的「所属团长整体」数据。</div>')
    order_warn = ""
    if act_sel:
        total_days = len(ds.day_span(start, end))
        if use_orders and order_cov < total_days:
            order_warn = (f'<div class="hint">订单明细覆盖 {order_cov}/{total_days} 天'
                          f'（夜间只回刷最近 {settings.REFRESH_DAYS} 天），更早的日期没有活动维度数据。</div>')
        elif not use_orders:
            order_warn = ('<div class="hint">该区间没有订单明细缓存，活动数据暂按报名商品维度估算；'
                          f'切换近 {settings.REFRESH_DAYS} 天或点「立即抓取」可拿到精确的活动订单。</div>')

    if use_orders:
        kpi_html = (
            '<div class="grid g5" style="margin-top:12px">'
            f'<div class="kpi"><div class="lbl">出单商品数</div><div class="val">{act_sku_cnt}</div>'
            '<div class="sub">该活动有效订单</div></div>'
            f'<div class="kpi"><div class="lbl">引入订单量</div><div class="val">{_an(act_ord["orders"])}</div>'
            f'<div class="sub">件数 {_an(act_ord["qty"])}</div></div>'
            f'<div class="kpi"><div class="lbl">预估 GMV</div><div class="val">{_amoney(act_ord["gmv"])}</div>'
            '<div class="sub">商品金额合计</div></div>'
            f'<div class="kpi"><div class="lbl">预估费用</div><div class="val">{_amoney(act_ord["fee"])}</div>'
            '<div class="sub">佣金 + 团长服务费</div></div>'
            f'<div class="kpi"><div class="lbl">ROI</div><div class="val">{_an(act_roi, 2)}</div>'
            '<div class="sub">GMV ÷ 预估费用</div></div>'
            '</div>')
    else:
        kpi_html = (
            '<div class="grid g5" style="margin-top:12px">'
            f'<div class="kpi"><div class="lbl">点击量</div><div class="val">{_an(tot["click"])}</div></div>'
            f'<div class="kpi"><div class="lbl">引入订单量</div><div class="val">{_an(tot["order"])}</div>'
            f'<div class="sub">出单团长 {n_order_union} · 出单商品 {n_order_sku}</div></div>'
            f'<div class="kpi"><div class="lbl">预估 GMV</div><div class="val">{_amoney(tot["gmv"])}</div></div>'
            f'<div class="kpi"><div class="lbl">预估费用</div><div class="val">{_amoney(tot["fee"])}</div>'
            '<div class="sub">佣金 + 团长服务费</div></div>'
            f'<div class="kpi"><div class="lbl">ROI</div><div class="val">{_an(roi, 2)}</div>'
            '<div class="sub">GMV ÷ 预估费用</div></div>'
            '</div>')

    act_ctx_json = json.dumps({
        "account": sel, "since": since, "q": kw, "union": union_sel,
        "hm": hm_mode, "day": day_sel, "start": start, "end": end,
        "days": days_param or "", "act": act_sel, "sku": sku_sel,
    }, ensure_ascii=False).replace("</", "<\\/")

    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>团长招商活动 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{ACTIVITY_CSS}</style>
</head><body>
{report2.navtop("/activity")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}团长招商</div>
  <h1>团长招商活动 · 报名与效果</h1>
  <div class="metaline">
    <span>报名明细来自操作日志「活动」(businessType=32)</span><span class="dot"></span>
    <span>效果来自团长招商页 · 团长诊断</span><span class="dot"></span>
    <span>每天 {settings.SCHEDULE_HOUR:02d}:{settings.SCHEDULE_MINUTE:02d} 自动抓取</span>
  </div>
  <div class="actions">
    <button class="btn" id="actRunBtn" onclick="actRun()">立即抓取</button>
    <a class="btn ghost mini" href="https://jzt.jd.com/jtk/#/activity-cp" target="_blank">团长招商页 ↗</a>
  </div>
</div></header>
<div class="wrap">

<div class="note" id="actTip" style="display:{'block' if run_state.get('active') else 'none'}">
  {('抓取中：' + _e(run_state.get('progress') or '执行中…')) if run_state.get('active') else ''}
</div>

<div class="card">
  <form class="toolbar" method="get" action="/activity">
    <label class="fld">账号 <select name="account">{acc_opts}</select></label>
    <label class="fld">团长 <select name="union">{union_list_opts}</select></label>
    <label class="fld">报名起始 <input type="date" name="since" value="{_e(since)}"></label>
    <label class="fld">关键词 <input type="text" name="q" value="{_e(kw)}" placeholder="SKU / 活动名 / 团长"></label>
    <input type="hidden" name="days" value="{_e(days_param or 1)}">
    <button class="btn" type="submit">查询</button>
  </form>
  <div class="hint" style="margin-bottom:0">
    最近一次抓取：{_e(last_state.get('lastRun') or '尚未执行')}
    {('· 报名库存 ' + str(last_state.get('signupRows')) + ' 条') if last_state.get('signupRows') else ''}
    · 自动任务每天回刷最近 {settings.REFRESH_DAYS} 天效果 + 最近 30 天报名日志
  </div>
</div>

<div class="grid g4" style="margin-top:16px">
  <div class="kpi"><div class="lbl">报名记录</div><div class="val">{len(rows)}</div>
    <div class="sub">按商品拆开的报名条数</div></div>
  <div class="kpi"><div class="lbl">报名商品</div><div class="val">{n_sku}</div>
    <div class="sub">去重 SKU</div></div>
  <div class="kpi"><div class="lbl">合作团长</div><div class="val">{n_union}</div>
    <div class="sub">去重团长</div></div>
  <div class="kpi"><div class="lbl">参与活动</div><div class="val">{n_act}</div>
    <div class="sub">去重活动</div></div>
</div>

<div style="margin-top:16px">{hm_card}</div>

<div style="margin-top:16px">{signup_card}</div>

<div id="effectWrap">
<!--EFFECT_START-->
<div class="actsec">
  <h2>{eff_title}</h2>
  <span class="hint">{eff_hint}</span>
</div>
{act_banner}
<div class="actctl">
  <div class="acttabs">{''.join(chips)}</div>
  <form class="toolbar" onsubmit="return false" method="get" action="/activity">
    <input type="hidden" name="account" value="{_e(sel)}">
    <input type="hidden" name="since" value="{_e(since)}">
    <input type="hidden" name="q" value="{_e(kw)}">
    <input type="hidden" name="union" value="{_e(union_sel)}">
    <input type="hidden" name="act" value="{_e(act_sel)}">
    <input type="hidden" name="sku" value="{_e(sku_sel)}">
    <label class="fld">起 <input type="date" name="start" value="{_e(start)}"></label>
    <label class="fld">止 <input type="date" name="end" value="{_e(end)}"></label>
    <button class="btn sec" type="button" onclick="actRangeCustom()">查询区间</button>
  </form>
</div>
{warn}
{act_warn}
{act_zero}
{order_warn}

{kpi_html}

<div class="card" style="margin-top:12px">
  <h3 style="margin-top:0">{cp_title}</h3>
  <div class="tblwrap short"><table><thead><tr>
    <th class="l">团长</th><th class="l">账号</th><th class="n">点击量</th>
    <th class="n">预估订单</th><th class="n">预估 GMV</th><th class="n">预估费用</th>
    <th class="n">ROI</th><th class="n">出单商品数</th>
  </tr></thead><tbody>{cp_html}</tbody></table></div>
</div>

<div class="card" style="margin-top:12px">
  <h3 style="margin-top:0">{sku_title}</h3>
  <div class="tblwrap short"><table><thead><tr>
    {sku_head}
  </tr></thead><tbody>{sku_html}</tbody></table></div>
</div>

<div style="margin-top:12px">{trend_card}</div>
<!--EFFECT_END-->
</div>

<div class="note" style="margin-top:18px">
  <p><b>数据口径</b>：报名明细逐条解析自京准通「操作日志 → 活动」的“报名团长招商活动 /
     报名cps官方活动”，活动起止是该商品自己的提报时段（同一活动里不同 SKU 可能不同）；
     团长由「已报名活动」接口的活动 ID → 团长 ID 映射，再用团长信息接口补名称。</p>
  <p><b>效果口径</b>：出单概览 / 出单团长 / 商品效果与团长招商页「团长诊断」一致；
     预估费用含佣金与团长服务费，ROI = 预估 GMV ÷ 预估费用。按天入库后区间内直接相加。</p>
  <p><b>活动视图</b>：点击报名明细某一行后，优先用「订单明细」里每单的 cpActId 汇总该活动的
     有效订单（订单量 / GMV / 佣金 + 团长服务费 / ROI 都是活动自己的数）；点击量平台没有活动维度，
     用所属团长的整体点击作参考。没有订单明细缓存的日期会退回“按报名商品估算”。</p>
  <p><b>注意</b>：CPS 是点击后 15 天归因，最近 15 天的数据每晚会被回刷修正。</p>
</div>

</div>
<script>window.ACT_RUNNING = {'true' if run_state.get('active') else 'false'};</script>
<script>window.ACT_CTX = {act_ctx_json};</script>
<script>{ACT_JS}</script>
</body></html>""", headers={"Cache-Control": "no-store"})


@app.post("/api/activity/run")
def api_activity_run(start: str = "", end: str = "", days: int = 0, perf_days: int = 0):
    s, e = None, None
    if start and end:
        s, e = ds.norm_day(start), ds.norm_day(end)
    elif days:
        e = _today().isoformat()
        s = (_today() - dt.timedelta(days=max(int(days), 1) - 1)).isoformat()
    pd = min(max(int(perf_days), 1), 90) if perf_days else None
    return activity.run_background(accounts=settings.ACCOUNTS, start=s, end=e,
                                   perf_days=pd, log=_log_progress)


@app.get("/api/activity/status")
def api_activity_status():
    return activity.runtime_status()


@app.get("/api/activity/effect")
def api_activity_effect(act: str = "", sku: str = "", start: str = "", end: str = "",
                        days: str = "", account: str = "", since: str = "",
                        q: str = "", union: str = "", hm: str = "start", day: str = ""):
    """局部刷新用：只返回「活动效果」那一段 HTML，点击行不再整页跳转。"""
    from urllib.parse import urlencode
    params = {"act": act, "sku": sku, "start": start, "end": end, "days": days,
              "account": account, "since": since, "q": q, "union": union,
              "hm": hm, "day": day}
    qs = urlencode({k: v for k, v in params.items() if v not in ("", None)})
    req = Request({"type": "http", "method": "GET", "path": "/activity",
                   "query_string": qs.encode(), "headers": [],
                   "scheme": "http", "server": ("127.0.0.1", 80), "root_path": ""})
    html = activity_page(req).body.decode("utf-8")
    if "<!--EFFECT_START-->" not in html:
        return HTMLResponse("<div class='hint'>加载失败，请刷新页面重试</div>")
    frag = html.split("<!--EFFECT_START-->", 1)[1].split("<!--EFFECT_END-->", 1)[0]
    return HTMLResponse(frag, headers={"Cache-Control": "no-store"})


# ================================================================ SKU 前台价
# 数据只读本地按天入库的商智商品明细（jd_roi/pricewatch.py），不发任何请求。
# 前台件单价 = 成交金额 ÷ 成交件数，所以「有成交才有价」：某天 0 件，这天就没有价。
PRICE_CSS = """
#pwTable td.l{text-align:left}
/* 10 列要塞进一屏：单元格留白收窄一点，并禁止「↑ 0.22%」被折成两行 */
#pwTable th,#pwTable td{padding:9px 8px}
#pwTable td{white-space:nowrap}
.qrow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.tblwrap.long{max-height:74vh}
/* 页头快捷链接：不让「→」被挤到下一行 */
.pagehead .actions a.btn{white-space:nowrap}
.pwsku{display:flex;align-items:center;gap:9px;min-width:0}
.pwsku img,.pwsku .pwph{width:34px;height:34px;border-radius:6px;object-fit:cover;
  background:var(--soft);flex:0 0 34px;display:block}
.pwsku .nm{font-size:12.5px;line-height:1.35;color:var(--ink);max-width:300px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.pwsku .id{font-size:11px;color:var(--muted2);font-family:var(--mono)}
.pwcur{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
.pwrt{color:var(--primary);font-weight:600}
.pwq{display:inline-block;width:11px;height:11px;line-height:11px;text-align:center;
  font-size:8px;border-radius:50%;background:var(--hair);color:var(--muted2);
  cursor:help;vertical-align:1px}
.skulink{color:var(--muted2);text-decoration:none;border-bottom:1px dashed var(--hair)}
.skulink:hover{color:var(--primary);border-bottom-color:var(--primary)}
.pwrtlist{max-height:302px;overflow:auto;border:1px solid var(--hair);
  border-radius:var(--r-md);margin-top:10px}
.pwrtlist thead th{position:sticky;top:0;z-index:2;background:var(--soft)}
.pwsub{font-size:11px;color:var(--muted2);font-variant-numeric:tabular-nums;line-height:1.5;
  white-space:nowrap}
.pwspark{width:118px;height:26px;display:block;margin-left:auto}
tr.pwrow{cursor:pointer}
tr.pwrow:hover td{background:#fbf8f3}
tr.pwrow.on td{background:var(--surface-card)}
#pwChart{height:340px;width:100%}
.pwcover{display:flex;gap:8px 18px;flex-wrap:wrap;align-items:center;font-size:12px;
  color:var(--muted);margin:2px 0 0}
.pwcover b{color:var(--ink)}
.pwsubonly{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  color:var(--muted);cursor:pointer;user-select:none}
.pwsubonly input{accent-color:var(--primary)}
#pwSubTable td.l.pwsbnm{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pwempty{color:var(--muted);font-size:13px;padding:26px 0;text-align:center}
/* 商品页探寻：只保留秒杀图标 */
.pwbadges{display:inline-flex;gap:3px;flex-wrap:wrap;vertical-align:middle}
.pwbadge{display:inline-block;padding:0 6px;border-radius:4px;font-size:10.5px;line-height:17px;
  font-weight:600;border:1px solid transparent;white-space:nowrap}
.pb-seckill{background:#fdeaea;color:#c0272d;border-color:#f6c9c9}
.pwreason{font-size:11.5px;line-height:1.5;color:var(--body);white-space:normal;max-width:260px}
.pwreason .bad{color:var(--redink)}
"""

PRICE_JS = r"""
(function(){
var INIT = window.PW_INIT || {};
var API = INIT.api || '/api/price';
var DAYS = [], ROWS = [], TOT = {}, SEL = null, LAST = null, CH = null;
var RT = null, SNAP = null, PROBE_BUSY = false;
var PENDING = [], PENDING_FORCE = false;
var SORT = {k:'amt', d:-1};
var SUBONLY = false;

var SUB_LBL = {win:'在线', substitute:'替补', offline:'已下线', expired:'已失效',
               deleted:'已删除', quit:'已退出', lose:'竞价失败', onfail:'上场失败'};
var SUB_CLS = {win:'b-ok', substitute:'b-warn', offline:'b-bad', expired:'b-bad',
               deleted:'b-bad', quit:'b-idle', lose:'b-bad', onfail:'b-warn'};

var COLS = [
  {k:'__item',  t:'商品',      l:1},
  {k:'priceLatest', t:'最新前台价'},
  {k:'subsidyPrice', t:'超补价',
   hint:'京东超级补贴「补贴后价格」（报名价）。页面按消息时间还原每天是否在线；'
      + '前台价低于超补价说明还有券/满减叠加，高于超补价说明当天不是所有成交都走了超补'},
  {k:'changeDodPct', t:'较上日',
   hint:'真的是紧邻的上一天：今天对比昨天同一时刻；中间有 0 成交的天就不给涨跌幅'},
  {k:'changePct', t:'较区间初'},
  {k:'priceAvg', t:'区间均价'},
  {k:'__band',  t:'区间价带'},
  {k:'qty',     t:'成交件数'},
  {k:'amt',     t:'成交金额'},
  {k:'uv',      t:'访客数'},
  {k:'__spark', t:'走势'}
];

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
function num(v){ if(v===null||v===undefined||v==='') return null; var n=Number(v);
  return isNaN(n)?null:n; }
function fx(v,dec){ var n=num(v); if(n===null) return '—';
  return n.toLocaleString('zh-CN',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
function money(v){ var n=num(v); return n===null?'—':('¥'+fx(n,2)); }
function cnt(v){ var n=num(v); return n===null?'—':n.toLocaleString('zh-CN'); }
/* 超补价：报名价 + 当前状态；再和最新前台价比一次，价差就是券/满减的叠加幅度 */
function subBadge(k){
  if(!k) return '<span class="badge b-idle" style="padding:1px 7px;font-size:10.5px">无记录</span>';
  return '<span class="badge '+(SUB_CLS[k]||'b-idle')+'" style="padding:1px 7px;font-size:10.5px">'
       + (SUB_LBL[k]||k) + '</span>';
}
function subCell(r){
  var sp = num(r.subsidyPrice);
  if(sp===null) return '<span class="flat">—</span><div class="pwsub">未报名 / 无消息</div>';
  var lp = num(r.priceLatest), d = (lp===null?null:lp-sp);
  var pct = (d===null||!sp)?null:d/sp*100;
  return '<span class="pwcur">'+money(sp)+'</span>'
    + '<div class="pwsub">'+subBadge(r.subsidyStatus)
    + (r.subsidyOnlineDays?(' 在线'+r.subsidyOnlineDays+'天'):'')+'</div>'
    + (d===null?'':'<div class="pwsub">前台价'+(d<0?'低':'高')+' '
        + fx(Math.abs(d),2)+'元 / '+fx(Math.abs(pct),1)+'%</div>');
}
/* SKU 编号 → 京东商品页；新标签打开，且不触发行点击（行点击是看曲线） */
function skuLink(sid){
  var s = String(sid == null ? '' : sid);
  return '<a class="skulink" href="https://item.jd.com/' + esc(s) + '.html"'
       + ' target="_blank" rel="noopener noreferrer"'
       + ' onclick="event.stopPropagation()" title="在新标签打开京东商品页">'
       + esc(s) + '</a>';
}
/* 活动图标：只展示秒杀 */
var PB_LBL = {seckill:'秒杀'};
function badges(list, ms){
  list = (list || []).filter(function(b){ return b && b.key === 'seckill'; });
  var sp = (ms && num(ms.price) !== null) ? num(ms.price) : null;
  if(!list.length && sp === null) return '<span class="flat">—</span>';
  if(!list.length) list = [{key:'seckill', label:'秒杀', text:'京东秒杀'}];
  return '<span class="pwbadges">' + list.map(function(b){
    return '<span class="pwbadge pb-seckill" title="'
      + esc((b.text || b.label || '') + (sp !== null ? (' · 秒杀价 ¥' + fx(sp,2)) : '')) + '">'
      + esc(b.label || PB_LBL[b.key] || b.key)
      + (sp !== null ? ' ¥' + fx(sp,2) : '') + '</span>';
  }).join('') + '</span>';
}
/* 探寻原因：秒杀标识 + 商品页价与昨日同刻的价差 */
function probeReason(r){
  if(r.probeError) return '<span class="bad">' + esc(r.probeError) + '</span>';
  if(!r.probeAt) return '<span class="flat">未探寻</span>';
  var pp = num(r.probePrice), cp = num(r.cmpPrice), bits = [];
  var ms = r.probeSeckill || {};
  var acts = (r.probeBadges || []).filter(function(b){ return b.key === 'seckill'; })
    .map(function(b){ return b.label; });
  if(acts.length) bits.push(acts.join(' + '));
  if(ms.on){
    var sp = num(ms.price);
    bits.push('秒杀价 ' + (sp !== null ? money(sp) : '—')
      + (num(ms.finalPrice) !== null && num(ms.finalPrice) !== sp
          ? ('（到手 ' + money(ms.finalPrice) + '）') : '')
      + (ms.remainSeconds ? ('，剩余 ' + fx(ms.remainSeconds / 3600, 1) + ' 小时') : ''));
  }
  if(pp !== null && cp !== null && cp){
    var d = pp - cp;
    if(d < -0.01){
      bits.push('商品页价较昨日同刻低 ' + money(-d) + '（' + fx(d / cp * 100, 1) + '%）');
    } else if(d > 0.01){
      bits.push('商品页价较昨日同刻高 ' + money(d));
    } else {
      bits.push('商品页价与昨日同刻持平');
    }
  }
  return bits.join('；') || '—';
}
function msg(s,bad){ var el=$('pwMsg'); if(!el) return; el.textContent=s||'';
  el.style.color = bad ? 'var(--redink)' : 'var(--muted)'; }

function pctCell(v, extra){
  var n = num(v);
  if(n===null) return '<span class="flat">—</span>'+(extra?'<div class="pwsub">'+extra+'</div>':'');
  // ±0.05% 以内算平稳：显示 0.00% 却配个红绿箭头会让人误判
  var c = n > 0.05 ? 'up' : (n < -0.05 ? 'down' : 'flat');
  var a = n > 0.05 ? '↑' : (n < -0.05 ? '↓' : '·');
  return '<span class="delta '+c+'">'+a+' '+fx(Math.abs(n),2)+'%</span>'
       + (extra?'<div class="pwsub">'+extra+'</div>':'');
}
/* 行内走势：把每日价画成折线；没有价的天断开（不插值，避免编出一个不存在的价格） */
function spark(arr){
  arr = arr || [];
  var vals = [], i;
  for(i=0;i<arr.length;i++){ if(num(arr[i])!==null) vals.push(+arr[i]); }
  if(vals.length < 2) return '<span class="pwsub" style="display:block;text-align:right">—</span>';
  var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
  var span = (max - min) || 1, W = 118, H = 26, P = 3;
  function x(i){ return P + (W-2*P) * (arr.length<2 ? 0 : i/(arr.length-1)); }
  function y(v){ return P + (H-2*P) * (1 - (v-min)/span); }
  var segs = [], cur = [];
  for(i=0;i<arr.length;i++){
    var v = num(arr[i]);
    if(v===null){ if(cur.length){ segs.push(cur); cur = []; } }
    else { cur.push([i, +v]); }
  }
  if(cur.length) segs.push(cur);
  var d = segs.map(function(s){
    return s.map(function(p,k){ return (k?'L':'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1); }).join('');
  }).join('');
  var last = vals[vals.length-1], first = vals[0];
  var col = last > first ? '#2E9E5B' : (last < first ? '#D64545' : '#8e8b82');
  var lastPt = null;
  for(i=arr.length-1;i>=0;i--){ if(num(arr[i])!==null){ lastPt = [i, +arr[i]]; break; } }
  var dot = lastPt ? ('<circle cx="'+x(lastPt[0]).toFixed(1)+'" cy="'+y(lastPt[1]).toFixed(1)
    +'" r="2.2" fill="'+col+'"/>') : '';
  return '<svg class="pwspark" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" aria-hidden="true">'
    + '<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.4" stroke-linejoin="round"/>'
    + dot + '</svg>';
}

function head(){
  var h = '<tr>';
  COLS.forEach(function(c){
    var mark = (SORT.k === c.k) ? (SORT.d<0 ? ' ↓' : ' ↑') : '';
    h += '<th class="'+(c.l?'l ':'')+'noSort" data-key="'+c.k+'"'
       + (c.hint ? ' title="'+esc(c.hint)+'"' : '')
       + ' onclick="PW.sort(\''+c.k+'\')">'
       + esc(c.t) + (c.hint ? ' <span class="pwq">?</span>' : '') + mark + '</th>';
  });
  return h + '</tr>';
}

function rowHtml(r){
  var on = (SEL === r.skuId) ? ' on' : '';
  var img = r.img ? '<img src="'+esc(r.img)+'" alt="" loading="lazy">' : '<span class="pwph"></span>';
  var dod = num(r.changeDod), chg = num(r.change);
  var dodExtra = dod===null ? '' : ((dod>0?'+':'')+fx(dod,2)+' 元');
  var chgExtra = chg===null ? '' : ((chg>0?'+':'')+fx(chg,2)+' 元');
  var dodSub = r.prevSameTime ? 'vs 昨天同刻'
             : (dod !== null ? 'vs ' + String(r.prevDay||'').slice(5)
                             : (r.prevDay ? '上次有价 ' + String(r.prevDay).slice(5) : '仅 1 天有价'));
  // 有商品页价就用商品页价，不再展示商智最新价
  var pp = num(r.probePrice);
  var showPrice = pp !== null ? pp : r.priceLatest;
  return '<tr class="pwrow'+on+'" data-sku="'+esc(r.skuId)+'" onclick="PW.pick(this)">'
    + '<td class="l"><div class="pwsku">'+img
      + '<div><div class="nm" title="'+esc(r.itemName)+'">'+esc(r.itemName||'—')+'</div>'
      + '<div class="id">'+skuLink(r.skuId)+'</div></div></div></td>'
    + '<td><span class="pwcur">'+money(showPrice)+'</span>'
      + (pp !== null ? ' '+badges(r.probeBadges, r.probeSeckill) : '')
      + '<div class="pwsub">'+esc(String(r.latestDay||'').slice(5))
      + (r.realtime ? ' <span class="pwrt">实时</span>' : '')
      + ' · '+cnt(r.priceDays)+'天有价</div></td>'
    + '<td>'+subCell(r)+'</td>'
    + '<td>'+pctCell(r.changeDodPct, dodExtra)
      + '<div class="pwsub">'+esc(dodSub)+'</div></td>'
    + '<td>'+pctCell(r.changePct, chgExtra)+'</td>'
    + '<td>'+money(r.priceAvg)+'</td>'
    + '<td>'+money(r.priceMin)+'~'+money(r.priceMax)
      + '<div class="pwsub">振幅 '+fx(r.swingPct,1)+'%</div></td>'
    + '<td>'+cnt(r.qty)+'</td>'
    + '<td>'+money(r.amt)+'</td>'
    + '<td>'+cnt(r.uv)+'</td>'
    + '<td>'+spark(r.spark)+'</td>'
    + '</tr>';
}

function sortVal(r, k){
  if(k==='__item') return String(r.itemName||'');
  if(k==='__band') return num(r.swingPct);
  if(k==='__spark') return num(r.changePct);
  return num(r[k]);
}

function render(){
  var kw = ($('pwFilter') ? $('pwFilter').value : '').trim().toLowerCase();
  var list = ROWS.filter(function(r){
    if(SUBONLY && !(r.subsidyStatus==='win' && num(r.subsidyPrice)!==null)) return false;
    if(!kw) return true;
    return String(r.skuId||'').indexOf(kw) >= 0
        || String(r.itemName||'').toLowerCase().indexOf(kw) >= 0;
  });
  list = list.slice().sort(function(a,b){
    var x = sortVal(a, SORT.k), y = sortVal(b, SORT.k);
    if(typeof x === 'string' || typeof y === 'string'){
      return String(x||'').localeCompare(String(y||'')) * SORT.d;
    }
    if(x===null) x = -1e18;
    if(y===null) y = -1e18;
    return (x-y) * SORT.d;
  });
  $('pwHead').innerHTML = head();
  $('pwBody').innerHTML = list.map(rowHtml).join('')
    || '<tr><td colspan="'+COLS.length+'" class="l pwempty">没有匹配的商品</td></tr>';
  $('pwCount').textContent = '共 ' + ROWS.length + ' 个有成交的 SKU'
    + (list.length !== ROWS.length ? ('，筛选后 ' + list.length + ' 行') : '');
}

/* 超补报名 SKU 清单：即使区间内没有成交（没进价格表）也列出来 */
function renderSub(sb){
  var card = $('pwSubCard'); if(!card) return;
  card.style.display = '';
  var acc = encodeURIComponent($('pwAcc') ? $('pwAcc').value : '');
  if(!sb || !sb.available){
    $('pwSubTag').textContent = '未抓取';
    $('pwSubMsg').innerHTML = '还没有该店铺的超级补贴消息：'
      + '<a href="/subsidy?account='+acc+'" target="_blank">去抓取一次 →</a>';
    $('pwSubBody').innerHTML = '<tr><td colspan="9" class="l pwempty">暂无数据</td></tr>';
    return;
  }
  var list = sb.list || [];
  $('pwSubTag').textContent = '报名 '+cnt(sb.skuCount)+' 个 · 在线 '+cnt(sb.onlineCount)
    + ' · 价格表内 '+cnt(sb.inPriceCount);
  $('pwSubMsg').innerHTML = '消息抓取于 <b>'+esc(String(sb.fetchedAt||'—').slice(0,16))+'</b>。'
    + '超补 SKU 在商智区间没有成交时不会出现在上面的价格表，但仍列在这里，'
    + '用来区分「没卖」与「没抓」；要更新消息去「超级补贴」页。';
  $('pwSubBody').innerHTML = list.map(function(x){
    return '<tr>'
      + '<td>'+subBadge(x.status)+'</td>'
      + '<td class="mono">'+skuLink(x.skuId)+'</td>'
      + '<td class="l pwsbnm">'+esc(x.itemName||'—')+'</td>'
      + '<td>'+money(x.price)+'</td>'
      + '<td class="mono">'+esc(x.enrollId||'—')+'</td>'
      + '<td>'+(x.inPrice
          ? '<span class="badge b-ok" style="padding:1px 7px;font-size:10.5px">在表</span>'
          : '<span class="pwsub">区间无成交</span>')+'</td>'
      + '<td>'+cnt(x.qty)+'</td>'
      + '<td>'+money(x.priceAvg)+'</td>'
      + '<td class="mono">'+esc((x.lastTime||'').slice(5))+'</td>'
      + '</tr>';
  }).join('') || '<tr><td colspan="9" class="l pwempty">没有报名 SKU</td></tr>';
}

function kpiBox(label, val, sub){
  return '<div class="kpi"><div class="lbl">'+esc(label)+'</div><div class="val">'+val+'</div>'
    + (sub?'<div class="sub">'+sub+'</div>':'') + '</div>';
}

function kpis(){
  var up = 0, down = 0, flat = 0, best = null, worst = null;
  ROWS.forEach(function(r){
    var n = num(r.changePct);
    if(n===null) return;
    // 选「最大涨幅」时跳过件数太少的：个位数成交的均价会被规格/优惠组合带飞
    if(n > 0.5){
      up++; if(r.qty >= 10 && (!best || n > +best.changePct)) best = r;
    } else if(n < -0.5){
      down++; if(r.qty >= 10 && (!worst || n < +worst.changePct)) worst = r;
    } else { flat++; }
  });
  var t = TOT || {};
  var box = '';
  box += kpiBox('有价 SKU', cnt(t.skuCount),
      '商智明细 <b>'+cnt(t.dataDays)+'</b>/'+cnt(t.rangeDays)+' 天有数据');
  box += kpiBox('全店加权均价', money(t.avgPrice),
      '成交 '+cnt(t.qty)+' 件 · '+money(t.amt));
  box += kpiBox('较区间初 涨 / 跌', '<span class="up">'+up+'</span> / <span class="down">'+down+'</span>',
      '平稳 '+flat+' 个（±0.5% 内）');
  if(best){
    box += kpiBox('最大涨幅（≥10 件）', '<span class="up">+'+fx(best.changePct,1)+'%</span>',
        esc(String(best.itemName || best.skuId).slice(0, 26))
        + '<br>'+money(best.priceFirst)+' → '+money(best.priceLatest)
        + ' · '+cnt(best.qty)+' 件');
  } else {
    box += kpiBox('最大跌幅（≥10 件）',
        worst ? ('<span class="down">'+fx(worst.changePct,1)+'%</span>') : '—',
        worst ? (esc(String(worst.itemName||worst.skuId).slice(0,26))
                 + '<br>'+money(worst.priceFirst)+' → '+money(worst.priceLatest)
                 + ' · '+cnt(worst.qty)+' 件')
              : '区间内没有可比的两天');
  }
  $('pwKpis').innerHTML = box;
  $('pwKpis').style.display = '';
}

function cover(){
  var t = TOT || {}, bits = [];
  bits.push('区间 <b>'+esc(LAST.start)+' ~ '+esc(LAST.end)+'</b>');
  bits.push('商智明细已入库 <b>'+cnt(t.dataDays)+'</b>/'+cnt(t.rangeDays)+' 天');
  if(LAST.realtimeDay){
    bits.push('今天 <b>'+esc(String(LAST.realtimeDay).slice(5))+'</b> 用实时快照（未结算）');
  }
  var miss = (LAST.missingDays || []), empty = (LAST.emptyDays || []);
  var sb = LAST.subsidy || {};
  var sbAcc = encodeURIComponent($('pwAcc') ? $('pwAcc').value : '');
  if(sb.available){
    bits.push('超补在线 <b>'+cnt(sb.onlineCount)+'</b> 个 SKU'
      + (sb.belowCount ? ('（其中 <b>'+cnt(sb.belowCount)+'</b> 个前台价低于超补价）') : '')
      + '，消息抓取于 ' + esc(String(sb.fetchedAt||'—').slice(0,16))
      + ' <a href="/subsidy?account='+sbAcc+'" target="_blank">更新</a>');
  } else {
    bits.push('没有超级补贴消息缓存，超补价列会全是「—」：'
      + '<a href="/subsidy?account='+sbAcc+'" target="_blank">先抓一次</a>');
  }
  if(miss.length){
    bits.push('缺 <b>'+miss.length+'</b> 天未抓：'+esc(miss.slice(0,4).join('、'))
      + (miss.length>4?' …':''));
  }
  if(empty.length){
    bits.push('<b>'+empty.length+'</b> 天 0 行（抓早了，商智还没出数）');
  }
  var html = bits.join(' <span class="dot" style="display:inline-block;width:3px;height:3px;'
    + 'border-radius:50%;background:var(--muted2);margin:0 4px;vertical-align:middle"></span> ');
  if(miss.length || empty.length){
    html += ' <button class="btn sec mini" style="margin-left:8px" onclick="PW.sync()">'
      + '补齐缺失天数</button>';
  }
  $('pwCover').innerHTML = html;
}

function detail(r){
  $('pwDetail').style.display = '';
  $('pwDTitle').textContent = r.itemName || r.skuId;
  var pp = num(r.probePrice);
  $('pwDSub').innerHTML = 'SKU '+skuLink(r.skuId)
    + '　最新 <b>'+money(pp!==null?pp:r.priceLatest)+'</b>'
    + '（'+(pp!==null?'商品页 · ':'')+esc(r.latestDay||'—')
    + (r.realtime ? ' 实时' : '') + '）'
    + '　区间均价 <b>'+money(r.priceAvg)+'</b>'
    + '　区间价带 '+money(r.priceMin)+' ~ '+money(r.priceMax)
    + '　成交 '+cnt(r.qty)+' 件 / '+money(r.amt)
    + '　访客 '+cnt(r.uv)
    + (r.subsidyPrice!=null
        ? ('　超补 '+subBadge(r.subsidyStatus)+' <b>'+money(r.subsidyPrice)+'</b>'
           + (r.subsidyOnlineDays?('（区间在线 '+r.subsidyOnlineDays+' 天）'):'')
           + (r.subsidyEnrollId?('　报名ID '+esc(r.subsidyEnrollId)):''))
        : '　超补：无报名消息')
    + (pp!==null
        ? ('　'+badges(r.probeBadges, r.probeSeckill)+'　<span class="pwsub">'+probeReason(r)+'</span>')
        : (r.probeError ? ('　商品页：<span class="down">'+esc(r.probeError)+'</span>') : ''));
  drawChart(r);
  // 宽屏时右侧面板与价格表并排，别把页面滚到下方图表、把面板挤出视口
  if(window.innerWidth <= 1280){
    $('pwDetail').scrollIntoView({behavior:'smooth', block:'nearest'});
  }
}

function drawChart(r){
  var box = $('pwChart');
  if(!box || !window.echarts) return;
  if(!CH) CH = echarts.init(box);
  var xs = DAYS.map(function(d){
    return String(d).slice(5) + (LAST && d === LAST.realtimeDay ? '*' : '');
  });
  // 超级补贴报名价：只在「当天在线」的日子画点，替补/已下线断线（不插值）
  var subData = r.subsidyByDay || [], hasSub = false, si;
  for(si=0; si<subData.length; si++){
    if(num(subData[si])!==null){ hasSub = true; break; }
  }
  var subSeries = hasSub ? [{name:'超补价', type:'line', step:'end', data:subData,
      connectNulls:false, symbol:'circle', symbolSize:4,
      lineStyle:{width:1.6, type:'dashed', color:'#2f9e8f'},
      itemStyle:{color:'#2f9e8f'}}] : [];
  CH.setOption({
    animationDuration: 260,
    grid:{left:58, right:58, top:34, bottom:36},
    tooltip:{trigger:'axis', formatter:function(ps){
      if(!ps || !ps.length) return '';
      var out = DAYS[ps[0].dataIndex] || '';
      ps.forEach(function(p){
        var v = (p.value===null || p.value===undefined) ? null : p.value;
        out += '<br>' + p.marker + p.seriesName + '：'
          + (v===null ? '—' : ((p.seriesName==='前台价'||p.seriesName==='超补价')
              ? (v+' 元/件') : (v+' 件')));
      });
      return out;
    }},
    legend:{top:0, right:56, itemWidth:12, itemHeight:8,
      textStyle:{fontSize:11, color:'#6c6a64'}},
    xAxis:{type:'category', data:xs,
      axisLabel:{fontSize:10, color:'#8e8b82', showMaxLabel:true},
      axisLine:{lineStyle:{color:'#e6dfd8'}},
      axisTick:{show:false}},
    yAxis:[
      {type:'value', name:'元/件', scale:true,
       nameTextStyle:{fontSize:10, color:'#8e8b82'},
       axisLabel:{fontSize:10, color:'#8e8b82'},
       splitLine:{lineStyle:{color:'#f1ece4'}}},
      {type:'value', name:'件', nameTextStyle:{fontSize:10, color:'#8e8b82'},
       axisLabel:{fontSize:10, color:'#8e8b82'}, splitLine:{show:false}}
    ],
    series:[
      {name:'前台价', type:'line', data:r.spark, connectNulls:false,
       symbol:'circle', symbolSize:5,
       lineStyle:{width:2, color:'#cc785c'}, itemStyle:{color:'#cc785c'},
       areaStyle:{color:'rgba(204,120,92,.07)'},
       markLine:{silent:true, symbol:'none',
         data:[{yAxis:r.priceAvg, lineStyle:{type:'dashed', color:'#8e8b82'},
           label:{formatter:'区间均价 '+r.priceAvg, position:'insideEndTop',
                  fontSize:10, color:'#6c6a64'}}]}}
    ].concat(subSeries).concat([
      {name:'成交件数', type:'bar', yAxisIndex:1, data:r.sparkQty,
       barMaxWidth:16, itemStyle:{color:'rgba(108,106,100,.16)'}}
    ])
  }, true);
  CH.resize();
}

function clearDetail(){
  SEL = null;
  $('pwDetail').style.display = 'none';
}

function poll(){
  fetch('/api/run/status').then(function(r){ return r.json(); }).then(function(j){
    if(j.active){
      msg('补抓中：' + (j.progress || '') + (j.account ? ('　' + j.account) : ''));
      setTimeout(poll, 3000);
    } else if(j.error){
      msg('补抓失败：' + String(j.error).slice(0, 300), true);
    } else {
      msg('补抓完成，正在刷新…');
      setTimeout(function(){ location.reload(); }, 800);
    }
  }).catch(function(){ setTimeout(poll, 5000); });
}

function load(acc, s, e){
  msg('读取本地商智明细…');
  LAST = null; ROWS = []; DAYS = []; TOT = {}; clearDetail();
  $('pwKpis').style.display = 'none';
  fetch(API + '?account=' + encodeURIComponent(acc)
        + '&start=' + encodeURIComponent(s) + '&end=' + encodeURIComponent(e),
        {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){ msg(j.error || '读取失败', true); loadSnap(acc); return; }
      LAST = j; DAYS = j.days || []; ROWS = j.rows || []; TOT = j.totals || {};
      if(!ROWS.length){
        msg('该区间没有有成交的商品：可能还没抓这几天的商智明细（见下方覆盖提示）', true);
      } else {
        msg('已读取本地商智明细（未请求京东）。点任意商品看逐日价格曲线。');
      }
      render(); kpis(); cover(); renderSub(j.subsidy);
      loadRt(acc);
      loadSnap(acc);
      history.replaceState(null, '', location.pathname + '?account=' + encodeURIComponent(acc)
        + '&start=' + encodeURIComponent(s) + '&end=' + encodeURIComponent(e));
    })
    .catch(function(err){ msg('读取失败：' + err, true); loadRt(acc); loadSnap(acc); });
}

/* 今日实时：商智 realtime 口径（今天 00:00 到此刻，对比昨天同一时刻）
   降价的行会自动去前台商品页探寻原因；商品页展示价带回后当作「今日前台价」，
   商智成交均价作为小字对照，活动标识（秒杀/百补/便宜包邮…）单独一列。 */
function loadRt(acc){
  var m = $('pwRtMsg'), box = $('pwRtBody');
  if(!m || !box) return;
  m.textContent = '读取今日实时…';
  box.innerHTML = '';
  fetch('/api/price/realtime?account=' + encodeURIComponent(acc || $('pwAcc').value),
        {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){ m.textContent = j.error || '今天还没有实时数据'; return; }
      RT = j;
      var diff = '';
      var a = num(j.avgPrice), b = num(j.cmpAvgPrice);
      if(a !== null && b){
        var p = (a - b) / b * 100;
        var cls = p > 0.05 ? 'up' : (p < -0.05 ? 'down' : 'flat');
        diff = '（昨日同刻 ' + money(b) + '，<span class="' + cls + '">'
             + (p > 0 ? '↑' : (p < 0 ? '↓' : '·')) + ' ' + fx(Math.abs(p), 2) + '%</span>）';
      }
      m.innerHTML = (j.account === 'all' ? '<b>全部账号</b>　' : '')
        + '截至 <b>' + esc(j.at || '—') + '</b>'
        + '　成交 <b>' + cnt(j.qty) + '</b> 件 / ' + money(j.amt)
        + '　全店均价 <b>' + money(j.avgPrice) + '</b>' + diff
        + '　涨 <span class="up">' + cnt(j.up) + '</span> / 跌 <span class="down">' + cnt(j.down) + '</span>'
        + (j.probe && j.probe.count
            ? '　商品页已探寻 <b>' + cnt(j.probe.count) + '</b> 个' : '');
      var list = (j.rows || []).filter(function(r){
        var p = num(r.price), c = num(r.cmpPrice);
        return p !== null && c !== null && Math.abs(p - c) > 1;
      });
      function frontOf(r){
        var pp = num(r.probePrice);
        return pp !== null ? pp : num(r.price);
      }
      list.sort(function(a2, b2){
        return Math.abs(frontOf(b2) - num(b2.cmpPrice))
             - Math.abs(frontOf(a2) - num(a2.cmpPrice));
      });
      if(!list.length){
        box.innerHTML = '<div class="pwsub">今天还没有「变动超过 ¥1」的 SKU。</div>';
        return;
      }
      box.innerHTML = '<div class="pwrtlist"><table><thead><tr>'
        + '<th class="l noSort">商品（变动超过 ¥1 的 ' + list.length + ' 个，按变动金额排序）</th>'
        + '<th class="n noSort">今日前台价</th><th class="n noSort">昨日同刻</th>'
        + '<th class="n noSort">变动</th><th class="l noSort">活动图标</th>'
        + '<th class="n noSort">今日件数</th>'
        + '</tr></thead><tbody>'
        + list.map(function(r){
            var pp = num(r.probePrice), sp = num(r.price);
            var front = (pp !== null ? pp : sp);
            var cmp = num(r.cmpPrice);
            var d = (front !== null && cmp !== null) ? front - cmp : null;
            var extra = d === null ? '' : ((d > 0 ? '+' : '') + fx(d, 2) + ' 元');
            var pct = (d !== null && cmp) ? (d / cmp * 100) : r.changePct;
            return '<tr class="pwrow" data-sku="' + esc(r.skuId)
              + '" onclick="PW.pickSku(this.getAttribute(\'data-sku\'))">'
              + '<td class="l"><div class="nm" title="' + esc(r.itemName) + '">'
              + esc(r.itemName || r.skuId) + '</div>'
              + '<div class="id">' + skuLink(r.skuId) + '</div></td>'
              + '<td title="' + esc(r.probePriceText || '') + '">'
              + '<span class="pwcur">' + money(front) + '</span>'
              + '</td>'
              + '<td>' + money(r.cmpPrice) + '</td>'
              + '<td>' + pctCell(pct, extra) + '</td>'
              + '<td class="l">' + badges(r.probeBadges, r.probeSeckill) + '</td>'
              + '<td>' + cnt(r.qty) + '</td></tr>';
          }).join('')
        + '</tbody></table></div>';
      // 降价的行自动探寻一次（每次会话只自动跑一次，结果当天复用）；
      // 如果服务端已经有探寻任务在跑（刷新页面 / 另一个标签），接着显示进度
      var drops = list.filter(function(r){
        var p = num(r.price), c = num(r.cmpPrice);
        return p !== null && c !== null && p < c - 0.01;
      });
      fetch('/api/price/probe/status', {cache:'no-store'})
        .then(function(r){ return r.json(); })
        .then(function(s){
          if(s.active && !PROBE_BUSY){ PROBE_BUSY = true; pollProbe(); return; }
          maybeAutoProbe(drops);
        })
        .catch(function(){ maybeAutoProbe(drops); });
    })
    .catch(function(){ m.textContent = '读取今日实时失败'; });
}

/* 自动探寻：本会话只触发一次，跳过 15 分钟内已探过的 SKU（服务端也会再筛一遍） */
function maybeAutoProbe(drops){
  if(PROBE_BUSY || !drops.length) return;
  var key = 'pwProbe:' + String(INIT.today || '');
  try {
    if(sessionStorage.getItem(key) === '1') return;
    sessionStorage.setItem(key, '1');
  } catch(e) {}
  var todo = drops.filter(function(r){ return !r.probeAt; });
  if(!todo.length) return;
  // 自动只探前 15 个（按变动金额已排序），避免一开页面就占用浏览器好几分钟
  startProbe(todo.slice(0, 15).map(function(r){ return r.skuId; }).join(','), false);
}

function startProbe(skus, force){
  var m = $('pwRtMsg');
  var ids = String(skus || '').split(',').filter(Boolean);
  if(PROBE_BUSY){
    // 已有任务：把 SKU 排队，等当前任务结束再跑，避免丢请求
    ids.forEach(function(s){ if(PENDING.indexOf(s) < 0) PENDING.push(s); });
    PENDING_FORCE = PENDING_FORCE || !!force;
    if(m) m.textContent = '正在探寻商品页，已把 ' + ids.length + ' 个 SKU 排队…';
    return;
  }
  if(!ids.length){ if(m) m.textContent = '没有需要探寻的 SKU'; return; }
  PROBE_BUSY = true;
  if(m) m.textContent = '正在前往商品页探寻 ' + ids.length + ' 个 SKU'
    + '（需要浏览器登录态，每个约 4~6 秒）…';
  fetch('/api/price/probe?skus=' + encodeURIComponent(ids.join(','))
        + '&account=' + encodeURIComponent($('pwAcc').value)
        + (force ? '&force=1' : ''), {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){
        // 别的标签页/后台已经在跑：接管它的进度，不要当成失败
        if(/已有探寻任务/.test(j.error || '')){ pollProbe(); return; }
        PROBE_BUSY = false;
        if(m) m.textContent = '探寻未启动：' + (j.error || '');
        return;
      }
      if(!j.total){ afterProbe(); return; }
      pollProbe();
    })
    .catch(function(err){ PROBE_BUSY = false; if(m) m.textContent = '探寻启动失败：' + err; });
}

/* 一轮探寻结束：有排队的先跑排队，否则刷新今日实时与夜间核算两张卡片 */
function afterProbe(){
  if(PENDING.length){
    var next = PENDING.splice(0, 20);
    var f = PENDING_FORCE;
    PENDING_FORCE = false;
    startProbe(next.join(','), f);
    return;
  }
  loadRt($('pwAcc').value);
  loadSnap($('pwAcc').value);
}

function pollProbe(){
  var m = $('pwRtMsg');
  fetch('/api/price/probe/status', {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(s){
      if(s.active){
        if(m) m.textContent = '探寻中 ' + cnt(s.done) + '/' + cnt(s.total)
          + '（成功 ' + cnt(s.okCount) + ' / 失败 ' + cnt(s.failCount) + '）'
          + (s.current ? (' · ' + s.current) : '') + '…';
        setTimeout(pollProbe, 2500);
        return;
      }
      PROBE_BUSY = false;
      if(s.error){
        PENDING = []; PENDING_FORCE = false;
        if(m) m.textContent = '探寻失败：' + String(s.error).slice(0, 200)
          + '（可点「探寻今日价」重试）';
        return;
      }
      afterProbe();
    })
    .catch(function(){ setTimeout(pollProbe, 3000); });
}

/* 夜间核算卡片固定看「核算日变动最大的 6 个」 */
function snapList(j){
  var list = (j.rows || []).filter(function(r){
    var n = num(r.changeDodPct);
    return n !== null && Math.abs(n) >= 2 && num(r.qty) >= 5;
  });
  list.sort(function(a, b){
    return Math.abs(num(b.changeDodPct)) - Math.abs(num(a.changeDodPct));
  });
  return list.slice(0, 6);
}

/* 夜间自动核算：每天凌晨抓完数据后由服务端算好落盘，这里只读结果做摘要 */
function loadSnap(acc){
  var m = $('pwAutoMsg'), box = $('pwAutoBody');
  if(!m || !box) return;
  SNAP = null;
  m.textContent = '读取最近一次夜间核算…';
  box.innerHTML = '';
  fetch('/api/price/snapshot?account=' + encodeURIComponent(acc || $('pwAcc').value),
        {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(j.ok && j.all){
        // 全部账号：夜间核算是按账号分别算的，这里逐账号列出来
        m.textContent = '全部账号：每个账号各自核算（不合并价格）';
        box.innerHTML = '<div class="pwrtlist"><table><thead><tr>'
          + '<th class="l noSort">账号</th><th class="n noSort">核算日</th>'
          + '<th class="n noSort">有成交 SKU</th><th class="n noSort">涨 / 跌</th>'
          + '<th class="n noSort">当日均价</th></tr></thead><tbody>'
          + j.all.map(function(x){
              return '<tr><td class="l"><b>' + esc(x.label || x.account) + '</b></td>'
                + '<td>' + esc(x.day || '—') + '</td><td>' + cnt(x.skuCount) + '</td>'
                + '<td><span class="up">' + cnt(x.up) + '</span> / <span class="down">'
                + cnt(x.down) + '</span></td><td>' + money(x.avgPrice) + '</td></tr>';
            }).join('')
          + '</tbody></table></div>';
        return;
      }
      if(!j.ok){
        m.textContent = j.error || '还没有核算记录';
        return;
      }
      SNAP = j;
      var at = String(j.generatedAt || '').replace('T', ' ').slice(0, 16);
      m.innerHTML = '最近核算 <b>' + esc(j.day) + '</b>'
        + (at ? '（' + esc(at) + '）' : '')
        + '　核算日有成交 <b>' + cnt(j.skuCount) + '</b> 个 SKU'
        + '　涨 <span class="up">' + cnt(j.up) + '</span> / 跌 <span class="down">' + cnt(j.down) + '</span>'
        + '　核算日均价 <b>' + money(j.avgPrice) + '</b>（成交 ' + cnt(j.qty) + ' 件）'
        + '　<span class="flat">今日前台价：商品页优先'
        + (j.probe && j.probe.count ? ('，已探寻 ' + cnt(j.probe.count) + ' 个') : '')
        + '</span>';
      // 核算日落后于「昨天」时说明商智还没出数，避免以为核算没跑
      var y = parseIso(INIT.today);
      if(y){
        y.setDate(y.getDate() - 1);
        if(j.day < iso(y)){
          m.innerHTML += '　<span class="flat">（' + iso(y)
            + ' 的商智还没出数，白天会自动补抓后重算）</span>';
        }
      }
      var list = snapList(j);
      if(!list.length){
        box.innerHTML = '<div class="pwsub">核算日没有「变动 ≥2% 且件数 ≥5」的 SKU。</div>';
        return;
      }
      box.innerHTML = '<div class="tblwrap short" style="margin-top:10px"><table><thead><tr>'
        + '<th class="l noSort">商品（核算日变动最大的 6 个）</th>'
        + '<th class="n noSort">今日前台价</th><th class="n noSort">核算日前台价</th>'
        + '<th class="n noSort">今日变动</th><th class="n noSort">核算日件数</th>'
        + '</tr></thead><tbody>'
        + list.map(function(r){
            var tp = num(r.todayPrice);
            var fromProbe = num(r.probePrice) !== null;
            var todayCell = tp !== null
              ? ('<span class="pwcur">' + money(tp) + '</span> '
                 + (fromProbe ? badges(r.probeBadges, r.probeSeckill) : '')
                 + '<div class="pwsub">' + (fromProbe ? '商品页' : '商智实时') + '</div>')
              : '<span class="flat">待探寻</span>';
            var snapSub = num(r.changeDodPct) !== null
              ? ('核算日较上次 ' + fx(r.changeDodPct, 1) + '%') : '';
            var chg = num(r.todayChangePct);
            var chgExtra = num(r.todayChange) !== null
              ? ((r.todayChange > 0 ? '+' : '') + fx(r.todayChange, 2) + ' 元') : '';
            return '<tr class="pwrow" data-sku="' + esc(r.skuId) + '" onclick="PW.pickSku(this.getAttribute(\'data-sku\'))">'
              + '<td class="l"><div class="nm" title="' + esc(r.itemName) + '">' + esc(r.itemName || r.skuId) + '</div>'
              + '<div class="id">' + skuLink(r.skuId) + '</div></td>'
              + '<td>' + todayCell + '</td>'
              + '<td>' + money(r.priceLatest)
                + (snapSub ? '<div class="pwsub">' + snapSub + '</div>' : '') + '</td>'
              + '<td>' + (chg !== null ? pctCell(chg, chgExtra) : '<span class="flat">—</span>') + '</td>'
              + '<td>' + cnt(r.qty) + '</td></tr>';
          }).join('')
        + '</tbody></table></div>';
      // 卡片里这 6 个 SKU 自动补今日商品页价（缺失或超过 15 分钟才重探）
      var need = list.filter(function(r){
        if(!r.probeAt) return true;
        if(r.probeError) return false;
        var t = Date.parse(r.probeAt);
        return !isNaN(t) && (Date.now() - t) > 15 * 60 * 1000;
      }).slice(0, 6);
      if(need.length){
        m.innerHTML += '　<span class="flat">正在探寻今日价…</span>';
        startProbe(need.map(function(r){ return r.skuId; }).join(','), false);
      }
    })
    .catch(function(){ m.textContent = '读取夜间核算记录失败'; });
}

window.PW = {
  run: function(){
    var acc = $('pwAcc').value, s = $('pwStart').value, e = $('pwEnd').value;
    if(!s || !e){ msg('请先选好起止日期', true); return; }
    if(s > e){ msg('结束日不能早于开始日', true); return; }
    load(acc, s, e);
  },
  sort: function(k){
    if(SORT.k === k) SORT.d = -SORT.d;
    else { SORT.k = k; SORT.d = (k==='__item') ? 1 : -1; }
    render();
  },
  pick: function(tr){
    var sid = tr.getAttribute('data-sku');
    var r = null;
    ROWS.forEach(function(x){ if(String(x.skuId) === String(sid)) r = x; });
    if(!r) return;
    SEL = r.skuId;
    var all = document.querySelectorAll('#pwTable tr.pwrow');
    for(var i=0;i<all.length;i++){
      all[i].className = 'pwrow' + (all[i].getAttribute('data-sku') === String(sid) ? ' on' : '');
    }
    detail(r);
    if(window.CMP) CMP.show(r.skuId, r.itemName, r.priceLatest);
  },
  /* 夜间核算摘要里点某个 SKU：滚到主表并打开它的曲线 */
  pickSku: function(sid){
    var tr = document.querySelector('#pwBody tr.pwrow[data-sku="' + String(sid) + '"]');
    if(!tr){
      msg('当前区间的表里没有这个 SKU，把区间调到包含核算日再看', true);
      return;
    }
    tr.scrollIntoView({behavior:'smooth', block:'center'});
    window.PW.pick(tr);
  },
  render: render,
  subOnly: function(on){ SUBONLY = !!on; render(); },
  /* 立刻抓一次今日实时（只抓商智这一个报表） */
  rtFetch: function(){
    var m = $('pwRtMsg');
    if(m) m.textContent = '正在抓取今日实时（需要浏览器登录态，约 1 分钟）…';
    fetch('/api/price/realtime/fetch', {method:'POST'})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){ if(m) m.textContent = '刷新失败：' + (j.error || ''); return; }
        (function wait(n){
          if(n <= 0){ if(m) m.textContent = '刷新超时，稍后再试一次'; return; }
          setTimeout(function(){
            fetch('/api/run/status', {cache:'no-store'})
              .then(function(r){ return r.json(); })
              .then(function(s){
                if(s.active){
                  if(m) m.textContent = '刷新中：' + (s.progress || '');
                  wait(n - 1);
                  return;
                }
                if(s.error){
                  if(m) m.textContent = '刷新失败：' + String(s.error).slice(0, 160);
                  return;
                }
                loadRt($('pwAcc').value);
              })
              .catch(function(){ wait(n - 1); });
          }, 3000);
        })(40);
      })
      .catch(function(err){ if(m) m.textContent = '刷新失败：' + err; });
  },
  /* 夜间核算卡片：手动刷新这 6 个 SKU 的今日商品页价 */
  snapProbe: function(){
    var list = SNAP ? snapList(SNAP) : [];
    if(!list.length){ msg('夜间核算卡片里没有可探寻的 SKU'); return; }
    startProbe(list.map(function(r){ return r.skuId; }).join(','), true);
  },
  /* 按本地现有数据立刻重算一次核算（补完商智后不用等夜里） */
  snapRun: function(){
    var m = $('pwAutoMsg');
    if(m) m.textContent = '正在按本地数据重算…';
    fetch('/api/price/snapshot/run', {method:'POST'})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){ if(m) m.textContent = '重算失败：' + (j.error || ''); return; }
        loadSnap($('pwAcc').value);
      })
      .catch(function(err){ if(m) m.textContent = '重算失败：' + err; });
  },
  /* 手动探寻：把当前「今日实时」里所有降价的 SKU 重新去商品页看一遍 */
  probeDrops: function(){
    var drops = ((RT && RT.rows) || []).filter(function(r){
      var p = num(r.price), c = num(r.cmpPrice);
      return p !== null && c !== null && p < c - 0.5;
    });
    if(!drops.length){ msg('当前没有降价的 SKU 需要探寻'); return; }
    startProbe(drops.map(function(r){ return r.skuId; }).join(','), true);
  },
  sync: function(){
    if(!LAST) return;
    var days = (LAST.missingDays || []).concat(LAST.emptyDays || []).sort();
    if(!days.length){ msg('没有缺失的天'); return; }
    var s = days[0], e = days[days.length-1];
    msg('正在补抓商智明细 ' + s + ' ~ ' + e + '（会占用浏览器登录态，只抓商智）…');
    fetch('/api/price/backfill?account=' + encodeURIComponent(LAST.account)
          + '&start=' + encodeURIComponent(s) + '&end=' + encodeURIComponent(e),
          {method:'POST'})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){ msg(j.error || '启动失败', true); return; }
        // 等后台线程真的把 _RUNNING 置成 active 再轮询，否则第一次会误判成「已完成」
        setTimeout(poll, 2500);
      })
      .catch(function(err){ msg('启动失败：' + err, true); });
  }
};

function parseIso(s){
  var p = String(s || '').split('-');
  return (p.length === 3) ? new Date(+p[0], +p[1]-1, +p[2]) : null;
}
function iso(d){
  function pad(n){ return (n<10?'0':'') + n; }
  return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
}
window.PWQUICK = function(k){
  var t = parseIso(INIT.today) || new Date();
  var end = new Date(t.getFullYear(), t.getMonth(), t.getDate());   // 含今天（有实时快照）
  var start;
  if(k === 'd7'){ start = new Date(end.getFullYear(), end.getMonth(), end.getDate()-6); }
  else if(k === 'd14'){ start = new Date(end.getFullYear(), end.getMonth(), end.getDate()-13); }
  else if(k === 'd30'){ start = new Date(end.getFullYear(), end.getMonth(), end.getDate()-29); }
  else if(k === 'month'){
    start = new Date(t.getFullYear(), t.getMonth(), 1);
  }
  else if(k === 'lastmonth'){
    end = new Date(t.getFullYear(), t.getMonth(), 0);
    start = new Date(end.getFullYear(), end.getMonth(), 1);
  }
  if(!start) return;
  $('pwStart').value = iso(start);
  $('pwEnd').value = iso(end);
  window.PW.run();
};

window.addEventListener('resize', function(){ if(CH) CH.resize(); });
window.PW.run();
})();
"""


# ---------------------------------------------------------------- 竞品与关键词面板
COMPETE_CSS = """
.pwSplit{display:grid;grid-template-columns:minmax(0,1fr) minmax(360px,430px);gap:18px;
  align-items:start}
.pwSplit.collapsed{grid-template-columns:minmax(0,1fr)}
.pwSplit.collapsed #cmpPanel{display:none}
.pwSplit #pwTableCard{min-width:0}
#cmpPanel{margin-top:18px;position:sticky;top:75px;max-height:calc(100vh - 87px);overflow:auto}
@media(max-width:1280px){
  .pwSplit{grid-template-columns:1fr}
  #cmpPanel{position:static;max-height:none}
}
.cmp h2{margin-bottom:6px}
.cmp h3{margin:16px 0 8px;font-size:14px;font-weight:600;color:var(--ink);
  display:flex;align-items:center;gap:8px}
.cmp h3 .tagline{margin-left:auto;font-weight:400}
.cmp table{font-size:12px}
.cmp th,.cmp td{padding:6px 7px}
.cmp .cmpname{font-size:13px;line-height:1.5;color:var(--ink);margin:0 0 8px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cmp .cmptools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px}
.cmp .cmpsku{font-family:var(--mono);font-size:11.5px;color:var(--muted2)}
.cmp .cmpchips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 2px}
.cmp .cmpchip{background:var(--soft);border:1px solid var(--hair);border-radius:999px;
  padding:3px 9px;font-size:12px;color:var(--body);white-space:nowrap}
.cmp .cmpchip b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
.cmp .cmpai{font-size:13px;line-height:1.8;color:var(--body);white-space:pre-wrap;
  background:var(--soft);border:1px solid var(--hair);border-radius:var(--r-md);
  padding:12px;max-height:46vh;overflow:auto}
.cmp .cmpai .think{color:var(--muted2)}
.cmp .cmpempty{font-size:12.5px;color:var(--muted);line-height:1.7;padding:6px 0}
.cmp .pwsub{white-space:normal}
"""

COMPETE_JS = r"""
(function(){
var CUR = null, POLL = null, ES = null, RAW = '', TRIED = {};

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
function num(v){ if(v===null||v===undefined||v==='') return null;
  var n=Number(v); return isNaN(n)?null:n; }
function fx(v,d){ var n=num(v); if(n===null) return '—';
  return n.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}); }
function money(v){ var n=num(v); return n===null?'—':('¥'+fx(n,2)); }
function cnt(v){ var n=num(v); return n===null?'—':n.toLocaleString('zh-CN'); }
function acc(){ var e=$('pwAcc'); return e ? e.value : ''; }
function msg(s,bad){ var e=$('cmpMsg'); if(!e) return; e.textContent=s||'';
  e.style.color = bad ? 'var(--redink)' : 'var(--muted)'; }
function skuLink(s){
  return '<a class="skulink" href="https://item.jd.com/'+esc(s)+'.html" target="_blank"'
    + ' rel="noopener noreferrer" title="在新标签打开京东商品页">'+esc(s)+'</a>';
}
function section(title, sub, body){
  return '<h3>'+esc(title)+(sub?'<span class="tagline">'+sub+'</span>':'')+'</h3>'+body;
}
function table(head, rows){
  return '<div class="tblwrap short"><table><thead><tr>'+head+'</tr></thead><tbody>'
    + rows + '</tbody></table></div>';
}

function render(d){
  var own = num(d.price);
  var band = d.priceBand || {};
  var cps = (d.competitorShown || []).slice(0, 8);
  var html = '';

  var adv = (band.adCount !== undefined && band.adCount !== null)
    ? band.adCount : cps.filter(function(x){ return x.ad; }).length;
  if(cps.length){
    html += section('同款竞品', '共 '+cnt((d.competitors||[]).length)+' 个 · 广告 '+cnt(adv)+' 个',
      table('<th class="l noSort">商品</th><th class="n noSort">价格</th>'
            + '<th class="n noSort">较你</th>',
        cps.map(function(x){
          var p = num(x.price);
          var diff = (p!==null && own!==null) ? (p-own) : null;
          return '<tr><td class="l">'
            + '<div class="pwsub" style="color:var(--ink)">'
            + esc(String(x.title||'').slice(0,44))+'</div>'
            + '<div class="pwsub">'+skuLink(x.sku)
            + (x.ad ? ' · <span style="color:var(--primary)">广告</span>' : '')
            + (x.shop ? ' · '+esc(x.shop) : '')+'</div></td>'
            + '<td>'+money(p)+'</td>'
            + '<td>'+(diff===null?'—':((diff>0?'+':'')+fx(diff,2)))+'</td></tr>';
        }).join('')));
  } else {
    html += section('同款竞品', '', '<div class="cmpempty">没有抓到同款，换个核心词或稍后重试。</div>');
  }
  if(band.count){
    html += '<div class="pwsub" style="margin-top:6px">竞品价带 '
      + money(band.min)+' ~ '+money(band.max)+'，中位 '+money(band.median)
      + (band.basis ? ('（'+esc(band.basis)+'）') : '')
      + (band.ownRank ? ('；你在第 '+band.ownRank+' 低（共 '+(band.count+1)+' 个）') : '')
      + '</div>';
  }

  var tr = d.trends || [];
  if(tr.length){
    html += section('行业关键词', '近7天 · 指数口径',
      table('<th class="l noSort">词</th><th class="n noSort">展现</th>'
            + '<th class="n noSort">点击</th><th class="n noSort">CTR</th>'
            + '<th class="n noSort">CVR</th><th class="n noSort">出价</th>',
        tr.map(function(x){
          return '<tr><td class="l">'+esc(x.word)+'</td><td>'+cnt(x.imp)+'</td>'
            + '<td>'+cnt(x.clicks)+'</td><td>'+fx(x.ctr,2)+'%</td>'
            + '<td>'+fx(x.cvr,2)+'%</td><td>'+money(x.bid)+'</td></tr>';
        }).join('')));
  }

  var recs = d.recs || {}, order = ['热搜词','潜力词','竞品词','竞品争夺词'], chips = '';
  order.forEach(function(k){
    var arr = (recs[k]||[]).slice(0, k==='竞品词' ? 8 : 6);
    if(!arr.length) return;
    chips += '<div class="pwsub" style="margin:7px 0 2px">'+esc(k)+'</div>'
      + '<div class="cmpchips">' + arr.map(function(x){
          return '<span class="cmpchip" title="搜索量 '+cnt(x.pv)+' · 建议出价 '
            + money(x.bid)+'">'+esc(x.word)+' <b>'+cnt(x.pv)+'</b></span>';
        }).join('') + '</div>';
  });
  if(chips) html += section('京东推词', '搜索量 + 建议出价', chips);

  var own2 = d.ownTerms || [];
  if(own2.length){
    html += section('本店搜索词', '近30天 · 按花费',
      table('<th class="l noSort">词</th><th class="n noSort">花费</th>'
            + '<th class="n noSort">订单</th><th class="n noSort">ROI</th>',
        own2.map(function(x){
          return '<tr><td class="l">'+esc(x.word)+'</td><td>'+money(x.cost)+'</td>'
            + '<td>'+cnt(x.orders)+'</td><td>'+fx(x.roi,2)+'</td></tr>';
        }).join('')));
  }

  html += '<div class="pwsub" style="margin-top:12px">抓取于 '
    + esc(String(d.fetchedAt||'').replace('T',' ').slice(0,16))
    + ' · 账号 '+esc(d.accountLabel || d.account || '')
    + ' · 核心词「'+esc(d.keyword||'')+'」</div>'
    + '<div class="pwsub" style="margin-top:4px">'+esc(d.caliber||'')+'</div>';

  $('cmpContent').innerHTML = html;
}

function paint(){
  var b = $('cmpAi'); if(!b) return;
  var txt = (RAW || 'AI 正在思考…')
    .replace(/\*\*/g, '')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/^[-*_]{3,}\s*$/gm, '');
  b.textContent = txt;
  b.scrollTop = b.scrollHeight;
  var p = $('cmpPanel');
  if(p) p.scrollTop = p.scrollHeight;
}

function start(){
  if(!CUR) return;
  msg('开始抓取（约 30~60 秒，期间占用浏览器登录态）…');
  var q = 'sku='+encodeURIComponent(CUR.sku)
        + '&account='+encodeURIComponent(acc())
        + '&name='+encodeURIComponent(CUR.name||'');
  if(CUR.price !== null && CUR.price !== undefined && CUR.price !== ''){
    q += '&price='+encodeURIComponent(CUR.price);
  }
  fetch('/api/compete/run?'+q, {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){ msg(j.error||'启动失败', true); return; }
      poll();
    })
    .catch(function(e){ msg('启动失败：'+e, true); });
}

function poll(){
  clearTimeout(POLL);
  if(!CUR) return;
  fetch('/api/compete/status?sku='+encodeURIComponent(CUR.sku)
        + '&account='+encodeURIComponent(acc()), {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(j.running){ msg(j.progress||'抓取中…'); POLL = setTimeout(poll, 2000); return; }
      if(j.error){ msg('抓取失败：'+j.error, true); return; }
      load(true);
    })
    .catch(function(){ POLL = setTimeout(poll, 2500); });
}

function load(afterRun){
  if(!CUR) return;
  var sku = CUR.sku;
  if(!afterRun) msg('读取缓存…');
  fetch('/api/compete?sku='+encodeURIComponent(sku)
        + '&account='+encodeURIComponent(acc()), {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(j.ok && j.data){
        if(!j.data.name && CUR.name) j.data.name = CUR.name;
        // 价格以左侧表格当前值为准（缓存里的价格可能是上次抓取时的）
        if(CUR.price !== null && CUR.price !== undefined && CUR.price !== ''){
          j.data.price = CUR.price;
        }
        render(j.data);
        msg(j.fresh ? '缓存 ' + String(j.data.fetchedAt||'').replace('T',' ').slice(0,16)
              : '缓存已过期，可点「重新抓取」');
        return;
      }
      if(afterRun){
        msg('抓取完成但没有数据，请稍后重试', true);
        return;
      }
      if(TRIED[sku]){ msg('没有可用数据，可点「重新抓取」', true); return; }
      TRIED[sku] = 1;
      start();
    })
    .catch(function(e){ msg('读取失败：'+e, true); });
}

function show(sku, name, price){
  CUR = {sku:String(sku), name:name||'', price:price};
  $('cmpEmpty').style.display = 'none';
  $('cmpBody').style.display = '';
  $('cmpTag').textContent = 'SKU '+sku;
  $('cmpName').textContent = name || ('SKU '+sku);
  $('cmpSku').innerHTML = skuLink(sku);
  $('cmpContent').innerHTML = '';
  $('cmpAiBox').style.display = 'none';
  $('cmpAi').textContent = '';
  if(ES){ try{ ES.close(); }catch(e){} ES = null; }
  load(false);
}

function refresh(){
  if(!CUR) return;
  delete TRIED[CUR.sku];
  start();
}

function togglePanel(){
  var sp = document.querySelector('.pwSplit');
  if(!sp) return;
  sp.classList.toggle('collapsed');
  try{ localStorage.setItem('cmpCollapsed', sp.classList.contains('collapsed') ? '1' : '0'); }catch(e){}
}

function ai(){
  if(!CUR) return;
  if(ES){ try{ ES.close(); }catch(e){} ES = null; }
  RAW = '';
  $('cmpAiBox').style.display = '';
  $('cmpAi').textContent = 'AI 正在读取数据…';
  var panel = $('cmpPanel');
  if(panel) panel.scrollTop = panel.scrollHeight;
  fetch('/api/compete/ai?sku='+encodeURIComponent(CUR.sku)
        + '&account='+encodeURIComponent(acc()), {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok){ $('cmpAi').textContent = j.error || 'AI 启动失败'; return; }
      var es = new EventSource('/api/ai/stream?job='+encodeURIComponent(j.jobId));
      ES = es;
      es.addEventListener('content', function(e){
        var d = JSON.parse(e.data); RAW += d.t; paint();
      });
      es.addEventListener('reasoning', function(){ if(!RAW) paint(); });
      es.addEventListener('phase', function(e){
        if(RAW) return;
        var d = JSON.parse(e.data);
        $('cmpAi').textContent = d.t || 'AI 正在思考…';
      });
      es.addEventListener('error', function(e){
        try{ var d = JSON.parse(e.data); RAW += '\n[出错] '+(d.t||''); paint(); }catch(_){}
      });
      es.addEventListener('done', function(){
        try{ es.close(); }catch(e){}
        ES = null; paint();
      });
    })
    .catch(function(e){ $('cmpAi').textContent = 'AI 启动失败：'+e; });
}

window.CMP = {show: show, refresh: refresh, ai: ai, togglePanel: togglePanel};
try{
  if(localStorage.getItem('cmpCollapsed') === '1'){
    var sp = document.querySelector('.pwSplit');
    if(sp) sp.classList.add('collapsed');
  }
}catch(e){}
})();
"""


@app.get("/price", response_class=HTMLResponse)
def price_page(request: Request):
    """SKU 前台价：逐日看每个 SKU 的「成交金额 ÷ 成交件数」怎么变。

    只读本地按天入库的商智商品明细（不发请求），所以打开即出数；
    缺天 / 0 行天会在页面上点名，并可一键补抓（只抓 sz_product）。
    """
    q = request.query_params
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = q.get("account") or settings.MAIN_ACCOUNT
    if acc != "all" and acc not in keys:
        acc = settings.MAIN_ACCOUNT
    end = _today()
    start = end - dt.timedelta(days=29)
    if q.get("start") and q.get("end"):
        try:
            s0, e0 = dt.date.fromisoformat(ds.norm_day(q.get("start"))), \
                dt.date.fromisoformat(ds.norm_day(q.get("end")))
            if s0 <= e0:
                start, end = s0, e0
        except ValueError:
            pass
    opts = ('<option value="all"%s>全部账号（总览）</option>'
            % (" selected" if acc == "all" else "")) + "".join(
        f'<option value="{_e(a["key"])}"{" selected" if a["key"] == acc else ""}>'
        f'{_e(a.get("label"))}</option>' for a in settings.ACCOUNTS)
    init = {"api": "/api/price", "account": acc, "today": _today().isoformat(),
            "start": start.isoformat(), "end": end.isoformat()}
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SKU 前台价 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{PRICE_CSS}</style><style>{COMPETE_CSS}</style>
</head><body>
{report2.navtop("/price")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}价格</div>
  <h1>SKU 前台价走势</h1>
  <div class="metaline">
    <span>前台件单价 = 商智成交金额 ÷ 成交件数</span><span class="dot"></span>
    <span>消费者实付（含优惠券 / 满减）</span><span class="dot"></span>
    <span>叠加超级补贴报名价对账</span><span class="dot"></span>
    <span>价格表只读本地；右侧竞品分析按需抓取</span>
  </div>
  <div class="actions">
    <a class="btn ghost mini" href="/profit">月度利润 →</a>
    <a class="btn ghost mini" href="/report">分析报表 →</a>
    <a class="btn ghost mini" href="/cache?kind=sz_product">缓存管理 →</a>
  </div>
</div></header>
<div class="wrap">

<div class="card">
  <div class="toolbar">
    <label class="fld">账号 <select id="pwAcc" onchange="PW.run()">{opts}</select></label>
    <label class="fld">起 <input type="date" id="pwStart" value="{_e(start.isoformat())}"></label>
    <label class="fld">止 <input type="date" id="pwEnd" value="{_e(end.isoformat())}"></label>
    <button class="btn" onclick="PW.run()">查询</button>
    <span class="qrow" style="margin:0">
      <button type="button" class="chip" onclick="PWQUICK('d7')">近 7 天</button>
      <button type="button" class="chip" onclick="PWQUICK('d14')">近 14 天</button>
      <button type="button" class="chip" onclick="PWQUICK('d30')">近 30 天</button>
      <button type="button" class="chip" onclick="PWQUICK('month')">本月</button>
      <button type="button" class="chip" onclick="PWQUICK('lastmonth')">上月</button>
    </span>
    <button type="button" class="btn sec mini" style="margin-left:auto"
            onclick="CMP.togglePanel()">竞品面板</button>
  </div>
  <div class="hint" id="pwMsg" style="min-height:20px">读取本地商智商品明细…</div>
  <div class="pwcover" id="pwCover"></div>
</div>

<div class="card" id="pwRtCard" style="margin-top:18px">
  <h2>今日实时 <span class="tagline">商智实时口径 · 对比昨天同一时刻 · 降价自动探寻前台原因</span>
    <button class="btn sec mini" style="margin-left:auto" onclick="PW.probeDrops()"
            title="把今天降价的 SKU 逐个打开京东商品页，带回页面展示价和活动标识（秒杀/百补/便宜包邮…）">探寻降价原因</button>
    <button class="btn sec mini" style="margin-left:6px" onclick="PW.rtFetch()"
            title="立刻抓一次今天的商智实时商品明细（只抓这一个报表）">刷新今日实时</button></h2>
  <div class="hint" id="pwRtMsg" style="margin-bottom:0">读取今日实时…</div>
  <div id="pwRtBody"></div>
</div>

<div class="card" id="pwAutoCard" style="margin-top:18px">
  <h2>夜间自动核算 <span class="tagline">凌晨核算昨天 · 今日前台价优先取商品页</span>
    <button class="btn sec mini" style="margin-left:auto" onclick="PW.snapProbe()"
            title="把这 6 个 SKU 逐个打开京东商品页，取今天的展示价">探寻今日价</button>
    <button class="btn sec mini" style="margin-left:6px" onclick="PW.snapRun()"
            title="按本地现有数据立刻重算一次（补完商智后不用等夜里）">立即重算</button></h2>
  <div class="hint" id="pwAutoMsg" style="margin-bottom:0">读取最近一次夜间核算…</div>
  <div id="pwAutoBody"></div>
</div>

<div class="grid g4" id="pwKpis" style="display:none;margin-top:18px"></div>

<div class="pwSplit">
<div class="card" id="pwTableCard" style="margin-top:18px">
  <h2>价格变化 <span class="tagline">点任意一行看逐日曲线</span></h2>
  <div class="toolbar">
    <input id="pwFilter" placeholder="筛选 SKU / 商品名" oninput="PW.render()"
           style="min-width:240px;padding:7px 10px;border-radius:8px;border:1px solid var(--line)">
    <label class="pwsubonly"><input type="checkbox" onchange="PW.subOnly(this.checked)">
      只看超补在线</label>
    <span class="hint" id="pwCount" style="margin:0"></span>
  </div>
  <div class="tblwrap long">
    <table id="pwTable"><thead id="pwHead"></thead><tbody id="pwBody"></tbody></table>
  </div>
</div>

<aside class="card cmp" id="cmpPanel">
  <h2>竞品与关键词 <span class="tagline" id="cmpTag">点左侧 SKU 行开始</span></h2>
  <div class="cmpempty" id="cmpEmpty">选一个 SKU，自动分析同款竞品、价格带、行业关键词和京东推词；
    也可以让 AI 基于这些数据给出投放动作。</div>
  <div id="cmpBody" style="display:none">
    <div class="cmpname" id="cmpName"></div>
    <div class="cmptools">
      <span class="cmpsku" id="cmpSku"></span>
      <button class="btn ghost mini" onclick="CMP.togglePanel()">收起</button>
      <button class="btn sec mini" onclick="CMP.refresh()">重新抓取</button>
      <button class="btn mini" onclick="CMP.ai()">AI 解读</button>
    </div>
    <div class="hint" id="cmpMsg" style="min-height:18px;margin:0 0 4px"></div>
    <div id="cmpContent"></div>
    <div id="cmpAiBox" style="display:none">
      <h3>AI 解读<span class="tagline">基于本面板数据</span></h3>
      <div class="cmpai" id="cmpAi"></div>
    </div>
  </div>
</aside>
</div>

<div class="card" id="pwSubCard" style="display:none;margin-top:18px">
  <h2>超级补贴报名 SKU
    <span class="tagline" id="pwSubTag">京麦消息 · 店铺 → 超级补贴</span>
    <a class="btn ghost mini" style="margin-left:auto" href="/subsidy" target="_blank">更新消息 →</a></h2>
  <div class="hint" id="pwSubMsg" style="margin-bottom:10px"></div>
  <div class="tblwrap short">
    <table id="pwSubTable"><thead><tr>
      <th class="noSort l">状态</th><th class="noSort">SKU</th>
      <th class="noSort l">商品名称</th><th class="noSort">超补价</th>
      <th class="noSort">报名ID</th><th class="noSort">价格表</th>
      <th class="noSort">区间成交</th><th class="noSort">前台均价</th>
      <th class="noSort">最近变动</th>
    </tr></thead><tbody id="pwSubBody"></tbody></table>
  </div>
</div>

<div class="card" id="pwDetail" style="display:none">
  <h2><span id="pwDTitle">—</span>
    <span class="tagline">逐日前台件单价（成交金额 ÷ 成交件数）</span></h2>
  <div class="hint" id="pwDSub"></div>
  <div id="pwChart"></div>
</div>

<div class="note" style="margin-top:18px">
  <p><b>右侧「竞品与关键词」</b>：点任意 SKU 行后按需抓取京东搜索前台的同款在售商品、
     行业关键词效率（京准通流量解析）、京东推词（热搜/潜力/竞品/争夺）和本店近 30 天搜索词，
     结果按 SKU + 账号缓存 12 小时，可点「重新抓取」刷新；「AI 解读」用现有模型配置生成投放建议。
     京东看不到竞品后台的计划/预算/出价，面板给的是公开前台数据 + 平台侧指标。</p>
  <p><b>前台件单价</b> = 商智「商品明细」成交金额 ÷ 成交件数（消费者实付，已含优惠券/满减），
     与「月度利润」页的前台件单价是同一口径；不是商品页标价（商智没有标价字段）。</p>
  <p><b>有成交才有价</b>：某天 0 件，这天就没有价，曲线断开、不做插值——
     不要把一个缺口当成涨价或降价。成交件数很小（个位数）时，均价还会被不同规格/优惠组合带偏，
     看「区间均价 + 件数」一起判断更稳。</p>
  <p><b>商智成交会回补</b>：T+1 大约只有一半、T+2~T+3 才定稿，最近一两天的价格会随夜间回补变化
     （每晚自动重抓近 15 天）。所以「今天看到的跌价」先别急着改价，等定稿再看。</p>
  <p><b>怎么用来调广告</b>：降价后同样的点击更容易成交，转化率上升是预期内的；
     若降价后转化没起来，多半是流量/竞价问题而不是价格。涨价期 CTR 与转化下滑同样属正常，
     可对照本页的访客数与成交件数判断是否要收预算。</p>
  <p><b>超补价对账（本页新增）</b>：超补价来自京麦「消息 → 店铺 → 超级补贴」的
     <b>补贴后价格（报名价）</b>，按消息时间还原每个 SKU 每天是否还在活动中，
     替补 / 已下线 / 已失效 / 已删除 / 已退出之后不再画线。它和前台价是两种口径，
     不要混为一谈：前台价是商智成交实付均价（已含券 / 满减 / 新人价），超补价只是
     报名时的活动价。对照关系：
     <b>前台价 &lt; 超补价</b>＝当天还叠加了其它优惠，实际到手更低；
     <b>前台价 &gt; 超补价</b>＝当天并非全部成交都走超补（部分原价成交，或活动前成交）；
     <b>两者接近</b>＝价格基本由超补价决定，可以把它当真实到手价来算投产比。
     超补消息在「超级补贴」页抓取/更新，本页顶部覆盖行会显示消息抓取时间；
     「只看超补在线」可以快速筛出当前在活动中的 SKU。</p>
  <p><b>夜间自动核算</b>：每天凌晨抓完昨天的商智数据后，服务端会把那一天的逐 SKU 前台价
     （以及相对各自上一个有价日的变动）算好存一份，就是顶部「夜间自动核算」卡片；
     因为商智 T+1 只出一半、T+2~T+3 才定稿，每次会把最近 3 个有数据的日子一起重算覆盖。
     凌晨抓「昨天」时商智常常还没出数（0 行），白天 08:00~23:00 会每小时自动只补商智，
     补到就立刻重算，任务日志里能看到「商智补抓」「前台价已核算 &lt;日期&gt;」。
     卡片里的「核算日前台价」才是商智结算价；「今日前台价」会优先用今天的商品页价
     （打开卡片时自动探寻这 6 个 SKU，超过 15 分钟会重探），取不到才退回商智实时价，
     所以刷新页面能看到今天的前台价，而不是一直停在核算日。</p>
  <p><b>全部账号</b>：按 SKU 合并各店 —— 成交金额 / 成交件数相加，价格仍是 Σ金额 ÷ Σ件数；
     同一个 SKU 在多个店都有成交时合并成一行。「夜间自动核算」那张卡片按账号各列一行
     （核算是按账号分别做的，不强行合并价格）。</p>
  <p><b>今天的价格</b>：表格里最新一行如果是今天，会标橙色「实时」、曲线横轴那天带 * ——
     那是商智实时快照（今天 00:00 到此刻，<b>未结算</b>），件数/金额白天还会继续涨；
     想排除今天只看到结算日，把「止」改成昨天即可。</p>
  <p><b>今日实时</b>：商智「商品明细 → 实时」能给当天 00:00 到此刻的按 SKU 成交，
     页面白天每 30 分钟自动刷新一次（也可点「刷新今日实时」）。因为今天还没结束，
     对比用的是<b>昨天同一时刻</b>而不是昨天全天，避免「今天只过了一半」的误判；
     数字会随白天继续成交变化，只看趋势，不要当成最终价。</p>
  <p><b>降价探寻（本页新增）</b>：商智成交均价只能看出「降了」，看不出「为什么降」。
     页面对今天降价的 SKU 自动打开对应京东商品页（<code>item.jd.com/&lt;SKU&gt;.html</code>），
     把<b>页面展示价</b>和<b>秒杀标识</b>带回来。一旦取到商品页价，就用它显示「今日前台价」，
     <b>不再显示商智成交均价</b>；「今日实时」去掉了价格下的小字与「探寻原因」列，
     「活动图标」列保留秒杀标识，点开详情也能看到秒杀价。
     秒杀判定以商品页接口 <code>pc_detailpage_wareBusiness</code> 的 <code>miaoshaInfo</code> 为准，
     并带回秒杀价 / 到手价 / 剩余时间——2026 版商品页的「京东秒杀」是图片 logo，
     页面文案里没有「秒杀」二字，只靠文字会漏判（例如 SKU 100087667252 天麻）。
     结果按天保存在本地（<code>data/price/probe_&lt;日期&gt;.json</code>），15 分钟内不重复抓，
     也可点「探寻降价原因」强制重探；抓取需要浏览器登录态，和夜间抓取共用同一把锁，
     遇到抓取任务在跑会提示稍后再试。</p>
</div>

</div>
{report2._echarts_src()}
<script>window.PW_INIT = {json.dumps(init, ensure_ascii=False)};</script>
<script>{PRICE_JS}</script>
<script>{COMPETE_JS}</script>
</body></html>""")


@app.get("/api/price")
def api_price(account: str | None = None, start: str = "", end: str = ""):
    """某个账号某个区间内，每个 SKU 的逐日前台价与区间汇总（纯本地读）。"""
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = account or settings.MAIN_ACCOUNT
    if acc != "all" and acc not in keys:
        acc = settings.MAIN_ACCOUNT
    if not (start and end):
        e = _today()
        start, end = (e - dt.timedelta(days=29)).isoformat(), e.isoformat()
    try:
        span = ds.day_span(start, end)
    except ValueError:
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    if len(span) > 400:
        return {"ok": False, "error": "区间最长 400 天，请缩小范围"}
    # 价格会随着商智回补/补抓变化，别让浏览器缓存住上一次的结果
    data = pricewatch.build(acc, span[0], span[-1])
    # 叠加超级补贴：商智只能看到「成交实付」，看不出这笔价是不是超补活动价；
    # 把每个 SKU 的报名价与逐日在线状态补进同一份行数据，页面才能对账。
    try:
        data["subsidy"] = subsidy.attach_price(
            data.get("rows") or [], data.get("days") or [], acc)
    except Exception as exc:  # noqa: BLE001
        data["subsidy"] = {"available": False,
                           "error": f"{type(exc).__name__}: {exc}"[:200]}
    # 商品页探寻结果：只补齐「最新价就是今天」的行，避免把今天的商品页价贴到历史日
    try:
        data["probe"] = itemprobe.attach(data.get("rows") or [],
                                         ds.today().isoformat(),
                                         only_day_field="latestDay")
    except Exception as exc:  # noqa: BLE001
        data["probe"] = {"available": False,
                         "error": f"{type(exc).__name__}: {exc}"[:200]}
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/price/snapshot")
def api_price_snapshot(account: str | None = None):
    """最近一次「夜间自动核算」结果（凌晨抓完数据后由 pricewatch.run_nightly 落盘）。"""
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = account or settings.MAIN_ACCOUNT
    if acc != "all" and acc not in keys:
        acc = settings.MAIN_ACCOUNT
    if str(acc).lower() == "all":
        # 全部账号：夜间核算是按账号各算一份，这里列成一张表（不强行合并价格）
        items = []
        for a in settings.ACCOUNTS:
            s = pricewatch.latest_snapshot(a["key"])
            if s:
                items.append({k: s.get(k) for k in
                              ("account", "label", "day", "skuCount", "up", "down",
                               "avgPrice", "generatedAt")})
        if not items:
            return JSONResponse({"ok": False, "empty": True,
                                 "error": "还没有夜间核算记录（每天凌晨抓完数据后自动生成）"},
                                headers={"Cache-Control": "no-store"})
        return JSONResponse({"ok": True, "all": items},
                            headers={"Cache-Control": "no-store"})
    snap = pricewatch.latest_snapshot(acc)
    if not snap:
        return JSONResponse({"ok": False, "empty": True,
                             "error": "还没有夜间核算记录（每天凌晨抓完数据后自动生成）；"
                                      "下面价格表本身是现算的，可以先直接看"},
                            headers={"Cache-Control": "no-store"})
    # 夜间核算的是「已结算的昨天」；卡片要展示今天的前台价，得叠加：
    #   ① 今天商品页探寻价（优先，用户看到的前台标价）
    #   ② 今天商智实时价（商品页价没抓到时兜底）
    today = ds.today().isoformat()
    try:
        snap["probe"] = itemprobe.attach(snap.get("rows") or [], today)
    except Exception as exc:  # noqa: BLE001
        snap["probe"] = {"available": False,
                         "error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        rt = pricewatch.realtime_snapshot(acc, today)
    except Exception:  # noqa: BLE001
        rt = None
    rtmap = {str(x.get("skuId") or ""): x for x in ((rt or {}).get("rows") or [])}
    for r in snap.get("rows") or []:
        t = rtmap.get(str(r.get("skuId") or ""))
        if t:
            r["todaySzPrice"] = t.get("price")
            r["todayQty"] = t.get("qty")
        price = r.get("probePrice")
        if price is None:
            price = r.get("todaySzPrice")
        r["todayPrice"] = price
        base = r.get("priceLatest")
        if price is not None and base:
            r["todayChange"] = round(price - base, 2)
            r["todayChangePct"] = round((price - base) / base * 100, 2)
    snap["today"] = today
    return JSONResponse({"ok": True, **snap}, headers={"Cache-Control": "no-store"})


@app.post("/api/price/snapshot/run")
def api_price_snapshot_run(account: str | None = None):
    """立刻重算一次前台价核算（与夜间任务同一个函数），补完数据后不用等夜里。"""
    accounts = [settings.get_account(account)] if account else settings.ACCOUNTS
    try:
        done = pricewatch.run_nightly(accounts, log=_log_progress)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "done": done,
            "errors": [x for x in done if x.get("error")]}


@app.get("/api/price/realtime")
def api_price_realtime(account: str | None = None):
    """今日实时前台价（商智 realtime 口径，对比昨天同一时刻），读本地快照。"""
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = account or settings.MAIN_ACCOUNT
    if acc != "all" and acc not in keys:
        acc = settings.MAIN_ACCOUNT
    snap = pricewatch.realtime_snapshot(acc)
    if not snap:
        return JSONResponse(
            {"ok": False, "empty": True,
             "error": "今天还没有实时数据：白天每 "
                      f"{max(settings.SZ_RT_INTERVAL // 60, 1)} 分钟自动刷新一次，"
                      "也可点右上角「刷新今日实时」"},
            headers={"Cache-Control": "no-store"})
    # 商品页探寻：把商品页展示价 / 活动标识补进每一行，并用它更新「今日前台价」
    try:
        snap["probe"] = itemprobe.attach(snap.get("rows") or [], snap.get("day"))
        for r in snap.get("rows") or []:
            r["priceFront"] = (r.get("probePrice")
                               if r.get("probePrice") is not None else r.get("price"))
    except Exception as exc:  # noqa: BLE001
        snap["probe"] = {"available": False,
                         "error": f"{type(exc).__name__}: {exc}"[:200]}
    return JSONResponse({"ok": True, **snap}, headers={"Cache-Control": "no-store"})


@app.post("/api/price/probe")
def api_price_probe(skus: str = "", account: str = "", force: int = 0):
    """探寻降价商品的前台原因：逐个打开商品页取展示价 + 活动标识（后台线程）。"""
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = account if account in keys else settings.MAIN_ACCOUNT
    ids = [s for s in re.split(r"[,\s]+", skus or "") if s.strip()]
    res = itemprobe.start(ids, day=ds.today().isoformat(), account=acc,
                          force=bool(force))
    return JSONResponse(res, headers={"Cache-Control": "no-store"})


@app.get("/api/price/probe/status")
def api_price_probe_status():
    """商品页探寻进度（页面轮询用）。"""
    st = itemprobe.status()
    # 内部 _RUN 里的 ok / fail 是成功/失败计数，避免盖掉接口的 ok=true
    st["okCount"] = st.pop("ok", 0)
    st["failCount"] = st.pop("fail", 0)
    return JSONResponse({"ok": True, **st},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/price/realtime/fetch")
def api_price_realtime_fetch(account: str | None = None):
    """立刻抓一次今日实时商品明细（只抓 sz_product_rt，需要浏览器登录态）。"""
    accs = (settings.ACCOUNTS if not account or str(account).lower() == "all"
            else [settings.get_account(account)])
    info = scrape_day.lock_info()
    if info.get("locked") and info.get("alive"):
        return {"ok": False, "error": "正在执行其它抓取任务，等它结束再刷新"}
    today = ds.today().isoformat()
    try:
        return scrape_day.run_background(accs, today, today, force=True,
                                         kinds=["sz_product_rt"],
                                         name="sz-realtime", trigger="manual")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ================================================================ 超级补贴
# 数据在 jd_roi/subsidy.py：京麦「消息 → 店铺 → 超级补贴」按 SKU 汇总补贴活动状态。
# 消息接口带 h5st 签名，只能先让页面真实发一次请求再截获头重放；
# 这里只做页面与接口，抓取走后台线程，页面读本地缓存。
SUBSIDY_CSS = """
.sbstat{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.sbstat .chip b{font-variant-numeric:tabular-nums;margin-left:5px;color:var(--muted)}
.sbstat .chip.on b{color:var(--ink)}
.sbtbl td.l{text-align:left}
.sbtbl tr.row{cursor:pointer}
.sbtbl td.nm{max-width:430px;text-align:left}
.sbtbl td.nm span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sbtbl tr.det td{background:var(--soft);padding:4px 16px 16px}
.sbhist{border-left:2px solid var(--hair);margin:8px 0 0 10px;padding-left:16px;
  display:flex;flex-direction:column;gap:12px}
.sbhist .ev{position:relative}
.sbhist .ev::before{content:"";position:absolute;left:-21px;top:5px;width:8px;height:8px;
  border-radius:50%;background:var(--muted2);border:2px solid var(--soft)}
.sbhist .ev.un::before{background:var(--primary);border-color:var(--soft)}
.sbhist .evt{font-size:11.5px;color:var(--muted);font-family:var(--mono);
  display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sbhist .evc{font-size:12.5px;line-height:1.7;color:var(--body);margin-top:3px}
.sbmsg .row{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid var(--hair)}
.sbmsg .row:last-child{border-bottom:0}
.sbmsg .t{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;
  white-space:nowrap;width:134px;flex:none;padding-top:2px}
.sbmsg .c{font-size:12.5px;line-height:1.7;color:var(--muted)}
.sbmsg .row.unread .c{color:var(--ink)}
.sbmsg .dot{width:6px;height:6px;border-radius:50%;background:var(--primary);
  flex:none;margin-top:8px}
.sbmsg .mh{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.sbempty{padding:34px 8px;text-align:center;color:var(--muted);font-size:13px;line-height:1.9}
"""

SUBSIDY_JS = """
(function(){
  var LBL = {win:'竞价成功', substitute:'替补', offline:'已下线', expired:'已失效',
             deleted:'已删除', quit:'已退出', lose:'竞价失败', onfail:'上场失败',
             invite:'报名邀请', other:'其他'};
  var CLS = {win:'b-ok', substitute:'b-warn', offline:'b-bad', expired:'b-bad',
             deleted:'b-bad', quit:'b-idle', lose:'b-bad', onfail:'b-warn',
             invite:'b-info', other:'b-idle'};
  var S = {account:'', data:null, status:'all', q:'', open:''};
  function $(id){ return document.getElementById(id); }
  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function badge(k){
    if(!k) return '<span class="badge b-idle">—</span>';
    return '<span class="badge '+(CLS[k]||'b-idle')+'">'+esc(LBL[k]||k)+'</span>';
  }
  function price(v){ return (v===null||v===undefined||v==='')?'—':'¥'+Number(v).toFixed(2); }
  function num(v){ return (v===null||v===undefined||v==='')?'—':Number(v).toLocaleString('zh-CN'); }
  function msg(t){ var el=$('sbMsg'); if(el) el.textContent=t||''; }

  function pass(r){
    if(S.status!=='all' && r.status!==S.status) return false;
    var q=S.q.trim().toLowerCase();
    if(!q) return true;
    return (r.skuId||'').indexOf(q)>=0 ||
      (r.itemName||'').toLowerCase().indexOf(q)>=0 ||
      (r.enrollId||'').indexOf(q)>=0;
  }

  function kpis(d){
    var c=d.counts||{};
    var bad=(c.offline||0)+(c.expired||0)+(c.deleted||0)+(c.quit||0)+
            (c.lose||0)+(c.onfail||0);
    var html=''+
      '<div class="kpi"><div class="lbl">SKU 总数</div><div class="val">'+(d.skuCount||0)+
        '</div><div class="sub">覆盖 '+(d.messageCount||0)+' 条超级补贴消息</div></div>'+
      '<div class="kpi"><div class="lbl">竞价成功</div><div class="val">'+(c.win||0)+
        '</div><div class="sub">替补 '+(c.substitute||0)+' 个</div></div>'+
      '<div class="kpi"><div class="lbl">已下线 / 失效</div><div class="val">'+bad+
        '</div><div class="sub">下线 '+(c.offline||0)+' · 失效 '+(c.expired||0)+
        ' · 删除 '+(c.deleted||0)+' · 退出 '+(c.quit||0)+'</div></div>'+
      '<div class="kpi"><div class="lbl">未读消息</div><div class="val">'+(d.unread||0)+
        '</div><div class="sub">抓取于 '+esc(d.fetchedAt||'—')+'</div></div>';
    $('sbKpis').innerHTML=html;
    $('sbKpis').style.display='';
  }

  function chips(d){
    var c=d.counts||{};
    var defs=[['all','全部'],['win','竞价成功'],['substitute','替补'],['offline','已下线'],
              ['expired','已失效'],['deleted','已删除'],['quit','已退出'],
              ['lose','竞价失败'],['onfail','上场失败']];
    var html=defs.map(function(x){
      var n=x[0]==='all'?(d.skuCount||0):(c[x[0]]||0);
      if(x[0]!=='all' && !n) return '';
      return '<button type="button" class="chip'+(S.status===x[0]?' on':'')+
        '" onclick="SB.flt(\\u0027'+x[0]+'\\u0027)">'+x[1]+'<b>'+n+'</b></button>';
    }).join('');
    $('sbChips').innerHTML=html;
  }

  function detail(r){
    var hist=(r.history||[]).slice().reverse();
    return hist.map(function(h){
      return '<div class="ev'+(h.unread?' un':'')+'">'+
        '<div class="evt"><span>'+esc(h.time||'')+'</span>'+badge(h.kind)+
        (h.detailUrl?'<a href="'+esc(h.detailUrl)+'" target="_blank">查看详情 →</a>':'')+
        '</div><div class="evc">'+esc(h.content||'')+'</div></div>';
    }).join('');
  }

  function table(d){
    var list=(d.skus||[]).filter(pass);
    $('sbCount').textContent='共 '+list.length+' 个 SKU';
    if(!list.length){
      $('sbBody').innerHTML='<tr><td colspan="8"><div class="sbempty">'+
        (d.skuCount?'没有符合筛选条件的 SKU':'这些消息里没有解析到带 SKU 的补贴活动')+
        '</div></td></tr>';
      return;
    }
    var html=list.map(function(r,i){
      var open=S.open===r.skuId;
      var main='<tr class="row" onclick="SB.toggle(\\u0027'+esc(r.skuId)+'\\u0027)">'+
        '<td>' + badge(r.status) + '</td>'+
        '<td class="mono">' + esc(r.skuId) + '</td>'+
        '<td class="nm"><span title="'+esc(r.itemName||'')+'">'+esc(r.itemName||'—')+'</span></td>'+
        '<td class="mono">' + price(r.price) + '</td>'+
        '<td class="mono">' + num(r.promoQty) + '</td>'+
        '<td class="mono">' + esc(r.enrollId||'—') + '</td>'+
        '<td class="mono">' + (r.events||0) + '</td>'+
        '<td class="mono">' + esc((r.lastTime||'').slice(5)) + '</td></tr>';
      var det=open?('<tr class="det"><td colspan="8"><div class="sbhist">'+
        detail(r)+'</div></td></tr>'):'';
      return main+det;
    }).join('');
    $('sbBody').innerHTML=html;
  }

  function messages(d){
    var q=S.q.trim().toLowerCase();
    var list=(d.messages||[]).filter(function(m){
      if(!q) return true;
      return (m.skuId||'').indexOf(q)>=0 ||
        (m.itemName||'').toLowerCase().indexOf(q)>=0 ||
        (m.content||'').toLowerCase().indexOf(q)>=0;
    });
    if(!list.length){
      $('sbMsgs').innerHTML='<div class="sbempty">没有消息</div>';
      return;
    }
    $('sbMsgs').innerHTML=list.map(function(m){
      return '<div class="row'+(m.unread?' unread':'')+'">'+
        (m.unread?'<span class="dot"></span>':'<span class="dot" style="background:transparent"></span>')+
        '<div class="t">'+esc(m.time||'')+'</div><div class="c">'+
        '<div class="mh">'+badge(m.kind)+
        (m.skuId?'<span class="mono" style="font-size:11.5px;color:var(--muted)">SKU '+esc(m.skuId)+'</span>':'')+
        (m.enrollId?'<span style="font-size:11.5px;color:var(--muted)">报名ID '+esc(m.enrollId)+'</span>':'')+
        (m.detailUrl?'<a href="'+esc(m.detailUrl)+'" target="_blank" style="font-size:11.5px">查看详情 →</a>':'')+
        '</div>'+esc(m.content||'')+'</div></div>';
    }).join('');
  }

  function render(){
    var d=S.data;
    if(!d) return;
    kpis(d); chips(d); table(d); messages(d);
    $('sbAt').textContent=d.fetchedAt?('抓取于 '+d.fetchedAt):'';
  }

  function empty(text){
    $('sbKpis').style.display='none';
    $('sbChips').innerHTML='';
    $('sbCount').textContent='';
    $('sbBody').innerHTML='<tr><td colspan="8"><div class="sbempty">'+esc(text)+
      '<br><button class="btn mini" style="margin-top:12px" onclick="SB.run()">抓取一次</button>'+
      '</div></td></tr>';
    $('sbMsgs').innerHTML='<div class="sbempty">'+esc(text)+'</div>';
    $('sbAt').textContent='';
  }

  async function load(){
    msg('读取本地缓存…');
    try{
      var r=await fetch('/api/subsidy?account='+encodeURIComponent(S.account), {cache:'no-store'});
      var j=await r.json();
      if(!j.ok){ S.data=null; msg(j.error||'暂无数据'); empty(j.error||'暂无数据'); return; }
      S.data=j; msg(''); render();
    }catch(e){ msg('读取失败：'+e); }
  }

  async function poll(){
    try{
      var r=await fetch('/api/subsidy/status?account='+encodeURIComponent(S.account), {cache:'no-store'});
      var j=await r.json();
      if(j.running){
        msg(j.progress||'抓取中…');
        setTimeout(poll, 1600);
        return false;
      }
      $('sbRun').disabled=false;
      if(j.error){ msg('抓取失败：'+j.error); return false; }
      await load(); return true;
    }catch(e){ $('sbRun').disabled=false; msg('抓取失败：'+e); return false; }
  }

  async function run(){
    var btn=$('sbRun'); if(btn){ btn.disabled=true; }
    msg('正在打开京麦消息中心…');
    try{
      var r=await fetch('/api/subsidy/run?account='+encodeURIComponent(S.account), {method:'POST'});
      var j=await r.json();
      if(!j.ok){ if(btn) btn.disabled=false; msg(j.error||'启动失败'); return; }
      setTimeout(poll, 1200);
    }catch(e){ if(btn) btn.disabled=false; msg('启动失败：'+e); }
  }

  function toggle(sku){ S.open = (S.open===sku?'':sku); if(S.data) table(S.data); }
  function flt(k){ S.status=k; if(S.data){ chips(S.data); table(S.data); } }
  function search(v){ S.q=v||''; if(S.data){ table(S.data); messages(S.data); } }
  function acc(v){ S.account=v; S.status='all'; S.open=''; load(); }

  window.SB={load:load, run:run, toggle:toggle, flt:flt, search:search, acc:acc};
  var INIT=window.SB_INIT||{};
  S.account=INIT.account||'';
  load();
})();
"""

_SUBSIDY_JOBS: dict[str, dict] = {}
_SUBSIDY_LOCK = threading.Lock()


def _subsidy_account(account: str | None) -> str:
    keys = [a["key"] for a in settings.ACCOUNTS]
    acc = account or settings.MAIN_ACCOUNT
    return acc if acc in keys else settings.MAIN_ACCOUNT


def _subsidy_state(acc: str) -> dict:
    with _SUBSIDY_LOCK:
        return dict(_SUBSIDY_JOBS.get(acc) or {})


@app.get("/subsidy", response_class=HTMLResponse)
def subsidy_page(request: Request):
    """超级补贴：京麦消息 → 店铺 → 超级补贴，按 SKU 汇总补贴活动状态。"""
    acc = _subsidy_account(request.query_params.get("account"))
    opts = "".join(
        f'<option value="{_e(a["key"])}"{" selected" if a["key"] == acc else ""}>'
        f'{_e(a.get("label"))}</option>' for a in settings.ACCOUNTS)
    init = {"account": acc}
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>超级补贴 · 京东广告运营</title>
<style>{DASH_CSS}</style><style>{report2.UI_CSS}</style><style>{SUBSIDY_CSS}</style>
</head><body>
{report2.navtop("/subsidy")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}营销活动</div>
  <h1>超级补贴</h1>
  <div class="metaline">
    <span>来源：京麦「消息 → 店铺 → 超级补贴」</span><span class="dot"></span>
    <span>按 SKU 汇总最新补贴状态</span><span class="dot"></span>
    <span>页面读本地缓存，点「重新抓取」才访问京东</span>
  </div>
  <div class="actions">
    <a class="btn ghost mini" href="/activity">团长活动 →</a>
    <a class="btn ghost mini" href="/price">SKU 前台价 →</a>
  </div>
</div></header>
<div class="wrap">

<div class="card">
  <div class="toolbar">
    <label class="fld">店铺 <select id="sbAcc" onchange="SB.acc(this.value)">{opts}</select></label>
    <button class="btn" id="sbRun" onclick="SB.run()">重新抓取</button>
    <span class="hint" id="sbMsg" style="margin:0">读取本地缓存…</span>
    <span class="hint" id="sbAt" style="margin:0 0 0 auto"></span>
  </div>
</div>

<div class="grid g4" id="sbKpis" style="display:none;margin-top:18px"></div>

<div class="card" style="margin-top:18px">
  <h2>SKU 补贴状态 <span class="tagline">点任意一行展开该 SKU 的完整消息时间线</span></h2>
  <div class="sbstat" id="sbChips"></div>
  <div class="toolbar">
    <input id="sbQ" placeholder="搜索 SKU / 商品名 / 报名ID" oninput="SB.search(this.value)"
           style="min-width:260px;padding:7px 10px;border-radius:8px;border:1px solid var(--hair)">
    <span class="hint" id="sbCount" style="margin:0"></span>
  </div>
  <div class="tblwrap long">
    <table class="sbtbl">
      <thead><tr>
        <th class="noSort l">状态</th><th class="noSort">SKU</th>
        <th class="noSort l">商品名称</th><th class="noSort">补贴后价</th>
        <th class="noSort">促销数量</th><th class="noSort">报名ID</th>
        <th class="noSort">消息</th><th class="noSort">最近</th>
      </tr></thead>
      <tbody id="sbBody"></tbody>
    </table>
  </div>
</div>

<div class="card" style="margin-top:18px">
  <h2>全部消息 <span class="tagline">未读优先展示 · 原样保留京东消息内容</span></h2>
  <div class="sbmsg" id="sbMsgs"></div>
</div>

<div class="note" style="margin-top:18px">
  <p><b>数据来源</b>：京麦工作台 <code>shop.jd.com/jdm/home</code> 右上角「消息」→ 顶部「店铺」页签
     → 左侧分类「超级补贴」（消息分类 <code>marketing / 262</code>）。接口带 h5st 签名，
     抓取时先让页面真实发一次请求、截获请求头再放大分页重放，因此一次能拿全历史消息（当前
     {_e(str(len(settings.ACCOUNTS)))} 个店铺各抓各的）。</p>
  <p><b>状态口径</b>：按消息时间从旧到新滚动，同一个 SKU 以<b>最后一条</b>业务消息为准。
     「竞价成功」消息后紧跟的「已审核通过成为替补」与「在线转替补」都记为<b>替补</b>；
     商品汰换下线、活动失效、促销删除、主动退出、竞价失败、上场失败分别单列。
     一条竞价成功消息里同时出现 SKU、促销数量、补贴后价格、报名ID 时才会进上面的 SKU 表，
     没有 SKU 的通知只保留在「全部消息」里。</p>
  <p><b>补贴金额 / 补贴数量为 0</b>：近期消息里平台下发的补贴金额与补贴数量基本都是 0.00 / 0，
     说明当前是商家自补的竞价活动（补贴后价格才是实际报名价），不要把它当成抓取缺失。</p>
  <p><b>缓存</b>：每个店铺一份 <code>{_e(str(settings.REPORT_DIR))}/subsidy_&lt;账号&gt;.json</code>，
     不自动过期；想更新点「重新抓取」，抓取与其它页面共用浏览器登录态锁，遇到夜间补数会排队等待。</p>
</div>

</div>
<script>window.SB_INIT = {json.dumps(init, ensure_ascii=False)};</script>
<script>{SUBSIDY_JS}</script>
</body></html>""")


@app.get("/api/subsidy")
def api_subsidy(account: str | None = None):
    """读某个店铺的超级补贴汇总缓存（纯本地）。"""
    acc = _subsidy_account(account)
    payload = subsidy.load_cache(acc)
    if not payload:
        return JSONResponse(
            {"ok": False, "empty": True,
             "error": "还没有抓取过该店铺的超级补贴消息，点「重新抓取」拉一次"},
            headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True, **payload}, headers={"Cache-Control": "no-store"})


@app.post("/api/subsidy/run")
def api_subsidy_run(account: str | None = None):
    """后台抓一次超级补贴消息（与其它抓取共用 Profile 锁）。"""
    acc = _subsidy_account(account)
    if _subsidy_state(acc).get("running"):
        return {"ok": True, "running": True}
    with _SUBSIDY_LOCK:
        _SUBSIDY_JOBS[acc] = {"running": True, "progress": "排队中…", "error": None,
                              "startedAt": settings.now_iso(), "finishedAt": None}

    def _log(text: str) -> None:
        with _SUBSIDY_LOCK:
            job = _SUBSIDY_JOBS.get(acc)
            if job is not None:
                job["progress"] = str(text)[:140]

    def _work() -> None:
        try:
            subsidy.run(account=acc, log=_log)
            with _SUBSIDY_LOCK:
                job = _SUBSIDY_JOBS.get(acc) or {}
                job.update({"running": False, "progress": "完成", "error": None,
                            "finishedAt": settings.now_iso()})
        except Exception as exc:  # noqa: BLE001
            with _SUBSIDY_LOCK:
                job = _SUBSIDY_JOBS.get(acc) or {}
                job.update({"running": False,
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                            "finishedAt": settings.now_iso()})

    threading.Thread(target=_work, daemon=True, name=f"subsidy-{acc}").start()
    return {"ok": True, "running": True}


@app.get("/api/subsidy/status")
def api_subsidy_status(account: str | None = None):
    st = _subsidy_state(_subsidy_account(account))
    if not st:
        return {"ok": True, "running": False}
    return {"ok": True, **{k: st.get(k) for k in
                           ("running", "progress", "error", "startedAt", "finishedAt")}}


# ---------------------------------------------------------------- 竞品与关键词分析
_COMPETE_JOBS: dict[str, dict] = {}
_COMPETE_LOCK = threading.Lock()


def _compete_state(sku: str) -> dict:
    with _COMPETE_LOCK:
        return dict(_COMPETE_JOBS.get(str(sku)) or {})


def _sku_arg(sku: str) -> str:
    sku = str(sku or "").strip()
    return sku if re.fullmatch(r"\d{5,20}", sku) else ""


@app.get("/api/compete")
def api_compete(sku: str, account: str | None = None):
    """读某个 SKU 的竞品/关键词分析缓存（12 小时内返回 fresh=True）。"""
    sku = _sku_arg(sku)
    if not sku:
        return JSONResponse({"ok": False, "error": "SKU 不合法"},
                            headers={"Cache-Control": "no-store"})
    acc = compete._account_for(account) if account else None
    fresh = compete.load_cache(sku, acc)
    if fresh:
        return JSONResponse({"ok": True, "data": fresh, "fresh": True},
                            headers={"Cache-Control": "no-store"})
    old = compete.load_cache(sku, acc, max_age=None)
    if old:
        return JSONResponse({"ok": True, "data": old, "fresh": False},
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": False, "empty": True},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/compete/run")
def api_compete_run(sku: str, account: str | None = None,
                    keyword: str | None = None, name: str | None = None,
                    price: float | None = None):
    """后台抓一次：前台同款 + 行业词 + 京东推词 + 本店搜索词。"""
    sku = str(sku or "").strip()
    if not re.fullmatch(r"\d{5,20}", sku):
        return {"ok": False, "error": "SKU 不合法"}
    key = f"{sku}:{compete._account_for(account)}"
    if _compete_state(key).get("running"):
        return {"ok": True, "running": True}
    with _COMPETE_LOCK:
        _COMPETE_JOBS[key] = {"running": True, "progress": "排队中…", "error": None,
                              "startedAt": settings.now_iso(), "finishedAt": None}

    def _log(text: str) -> None:
        with _COMPETE_LOCK:
            job = _COMPETE_JOBS.get(key)
            if job is not None:
                job["progress"] = str(text)[:140]

    def _work() -> None:
        try:
            compete.collect(sku, account=account, keyword=keyword,
                            name=name, price=price, log=_log)
            with _COMPETE_LOCK:
                job = _COMPETE_JOBS.get(key) or {}
                job.update({"running": False, "progress": "完成", "error": None,
                            "finishedAt": settings.now_iso()})
        except Exception as exc:  # noqa: BLE001
            with _COMPETE_LOCK:
                job = _COMPETE_JOBS.get(key) or {}
                job.update({"running": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                            "finishedAt": settings.now_iso()})

    threading.Thread(target=_work, daemon=True, name=f"compete-{sku}").start()
    return {"ok": True, "running": True}


@app.get("/api/compete/status")
def api_compete_status(sku: str, account: str | None = None):
    sku = _sku_arg(sku)
    if not sku:
        return {"ok": False, "error": "SKU 不合法"}
    st = _compete_state(f"{sku}:{compete._account_for(account)}")
    if not st:
        return {"ok": True, "running": False}
    return {"ok": True, **{k: st.get(k) for k in
                           ("running", "progress", "error", "startedAt", "finishedAt")}}


@app.post("/api/compete/ai")
def api_compete_ai(sku: str, account: str | None = None):
    """用现有 LLM 配置对某个 SKU 的竞品/关键词数据做解读（SSE 走 /api/ai/stream）。"""
    if not settings.AI_ENABLED:
        return {"ok": False, "error": "AI 解读已禁用（JD_AI_ENABLED=0）"}
    sku = _sku_arg(sku)
    if not sku:
        return {"ok": False, "error": "SKU 不合法"}
    data = compete.load_cache(sku, compete._account_for(account), max_age=None)
    if not data:
        return {"ok": False, "error": "先抓取分析数据，再让 AI 解读"}
    job = aijobs.JOBS.new("compete", {"sku": str(sku)})

    def _work() -> None:
        try:
            job.emit("phase", "读取竞品与关键词数据…")
            compete.ai_run(data, on_event=lambda kind, text="": job.emit(kind, text))
            job.finish(result={"sku": str(sku)})
        except Exception as exc:  # noqa: BLE001
            job.finish(error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=_work, daemon=True, name=f"compete-ai-{sku}").start()
    return {"ok": True, "jobId": job.id}


@app.post("/api/price/backfill")
def api_price_backfill(account: str = "", start: str = "", end: str = ""):
    """只补抓商智商品明细（sz_product），给「补齐缺失天数」用。

    走与夜间抓取同一个后台任务 + 同一把浏览器锁；已经在抓时会直接返回占用提示。
    """
    if not (start and end):
        return {"ok": False, "error": "缺少 start / end"}
    try:
        ds.day_span(start, end)
    except ValueError:
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    acc = settings.get_account(account)
    try:
        return scrape_day.run_background([acc], start, end, kinds=["sz_product"],
                                         name="price-backfill")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ================================================================ API
@app.get("/api/status")
def api_status():
    cached = settings._costs_cache_read() or {}
    return {"ok": True,
            "server": settings.now_iso(),
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
    s, e = ui_range(start, end, days)
    try:
        data = analyze2.build(start=s, end=e, accounts=account_list(accounts))
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    if save:
        out = Path(settings.DATA_DIR) / "analysis" / f"{s}_{e}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "range": {"start": s, "end": e}, "data": data}


# ---------------------------------------------------------------- 行业大盘接口
@app.get("/api/industry/status")
def api_industry_status():
    """后台补齐进度（报表页据此自动刷新）。"""
    return {"ok": True, **industry.status()}


@app.post("/api/industry/run")
def api_industry_run(start: str | None = None, end: str | None = None,
                     days: int | None = None, accounts: list[str] | None = Query(None),
                     force: int = 0):
    """手动起一次补齐；force=1 才强制重抓（平时一律先命中本地缓存）。"""
    s, e = ui_range(start, end, days)
    keys = [a["key"] for a in account_list(accounts)]
    res = industry.ensure(keys, s, e, force=bool(force), auto=True)
    return {"ok": True, "range": {"start": s, "end": e}, "accounts": keys, **res}


@app.post("/api/industry/refresh")
def api_industry_refresh(start: str, end: str, accounts: str = ""):
    """报表页表单：显式重抓行业大盘后跳回报表（避免刷新页面时反复 force）。"""
    keys = [a["key"] for a in account_list(accounts)]
    industry.ensure(keys, start, end, force=True, auto=True)
    return RedirectResponse(
        f"/report?start={start}&end={end}&accounts={','.join(keys)}", status_code=303)


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


# ---------------------------------------------------------------- AI 解读
@app.get("/api/ai/status")
def api_ai_status():
    st = _ai.snapshot()
    cur = analyze_ai.load_latest()
    job = aijobs.JOBS.get(st.get("jobId")) if st.get("jobId") else None
    return {"ok": True, "enabled": settings.AI_ENABLED, **st,
            "job": job.to_dict() if job else None,
            "cached": (None if not cur else {
                "range": cur.get("range"), "accounts": cur.get("accounts"),
                "generatedAt": cur.get("generatedAt"), "model": cur.get("model")})}


@app.post("/api/ai/run")
def api_ai_run(start: str | None = None, end: str | None = None,
               accounts: str | None = None):
    """生成 AI 解读（后台线程）。区间缺省时**贴着本地已抓取的数据**。"""
    if not settings.AI_ENABLED:
        return {"ok": False, "error": "AI 解读已禁用（JD_AI_ENABLED=0）"}
    accs = account_list([accounts] if accounts else None)
    if start and end:
        s, e = start, end
    else:
        rng = analyze_ai.local_range(accs)
        if not rng:
            return {"ok": False, "error": "本地还没有已抓取的数据，请先抓取"}
        s, e = rng
    return _ai.run_background(s, e, accs)


# ---------------------------------------------------------------- AI 对话（分析台）
# 会话缓存：把「本地已抓取数据摘要 + 方法论」按 (区间, 账号) 备好，同一区间内多次追问不再重算。
# 摘要构建纯本地、很快；这里缓存只是为了省掉重复读盘与 JSON 序列化，不是硬需求。
_CHAT_CACHE: dict = {}
_CHAT_LOCK = threading.Lock()


def _chat_context(start: str, end: str, accs):
    """准备（并缓存）一次对话所需的上下文：digest + methodology。"""
    ck = (start, end, ",".join(a["key"] for a in accs))
    with _CHAT_LOCK:
        hit = _CHAT_CACHE.get(ck)
    if hit:
        return hit
    data = analyze2.build(start=start, end=end, accounts=accs)
    digest, methodology = analyze_ai.prepare_chat_context(data, None)
    with _CHAT_LOCK:
        _CHAT_CACHE.clear()          # 只保留最近一次区间，避免无限增长
        _CHAT_CACHE[ck] = (digest, methodology)
    return digest, methodology


@app.post("/api/ai/chat")
async def api_ai_chat(request: Request):
    """对话框追问：**立即返回 jobId**，生成在后台线程里跑。

    这样用户切走页面/刷新/断网都不会中断生成；回来后用 jobId 挂
    `GET /api/ai/stream?job=...` 即可续看（含思考过程）。

    请求体 JSON：{start, end, accounts, question, history:[{role,content}]}
    """
    if not settings.AI_ENABLED:
        return JSONResponse({"ok": False, "error": "AI 解读已禁用（JD_AI_ENABLED=0）"},
                            status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"ok": False, "error": "问题为空"}, status_code=400)
    accs = account_list([body.get("accounts")] if body.get("accounts") else None)
    start, end = body.get("start"), body.get("end")
    if not (start and end):
        rng = analyze_ai.local_range(accs)
        if not rng:
            return JSONResponse({"ok": False, "error": "本地还没有已抓取的数据，请先抓取"},
                                status_code=400)
        start, end = rng
    history = body.get("history") or []
    keys = [a["key"] for a in accs]

    job = aijobs.JOBS.new("chat", {"start": start, "end": end, "accounts": keys,
                                   "question": question[:120]})

    def _work():
        try:
            job.emit("phase", f"正在读取本地已抓取数据（{start} ~ {end}）…")
            digest, methodology = _chat_context(start, end, accs)
            job.emit("phase", "上下文就绪，正在思考…")
            last_exc = None
            for attempt in range(2):          # 中途断流重试一次；重试前发 reset，前端清空重画
                try:
                    if attempt:
                        job.emit("reset", "")
                        job.emit("phase", "重试中…")
                    for kind, text in analyze_ai.chat_stream_events_ctx(digest, methodology,
                                                                       history, question):
                        job.emit(kind, text)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            if last_exc is not None:
                raise last_exc
            job.finish(result={"answer": job.content})
        except Exception as exc:  # noqa: BLE001
            job.finish(error=f"{type(exc).__name__}: {exc}")
            _log_progress(f"AI 对话失败：{type(exc).__name__}: {exc}")

    threading.Thread(target=_work, name=f"ai-chat-{job.id}", daemon=True).start()
    return {"ok": True, "jobId": job.id, "start": start, "end": end, "accounts": keys}


@app.post("/api/ai/agent")
async def api_ai_agent(request: Request):
    """**自主执行**：把任务交给内部 Agent（可调内部接口 / 抓数据 / 写并运行脚本）。

    与 /api/ai/chat 一样**立即返回 jobId**，事件走 `GET /api/ai/stream?job=`；
    新增事件类型：`plan`（本轮打算做什么）、`tool`（调用的工具）、`obs`（工具返回摘要）。
    请求体 JSON：{start, end, accounts, task}
    """
    if not settings.AI_ENABLED:
        return JSONResponse({"ok": False, "error": "AI 解读已禁用（JD_AI_ENABLED=0）"},
                            status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    task = (body.get("task") or body.get("question") or "").strip()
    if not task:
        return JSONResponse({"ok": False, "error": "任务为空"}, status_code=400)
    accs = account_list([body.get("accounts")] if body.get("accounts") else None)
    start, end = body.get("start"), body.get("end")
    keys = [a["key"] for a in accs]
    job = aijobs.JOBS.new("agent", {"start": start, "end": end, "accounts": keys,
                                    "task": task[:160]})

    def _work():
        try:
            from . import agent as agent_mod
            agent_mod.run(job, task, accounts=accs, start=start, end=end)
            if job.status == "running":
                job.finish(result={"answer": job.content})
        except Exception as exc:  # noqa: BLE001
            job.finish(error=f"{type(exc).__name__}: {exc}")
            _log_progress(f"Agent 执行失败：{type(exc).__name__}: {exc}")

    threading.Thread(target=_work, name=f"ai-agent-{job.id}", daemon=True).start()
    return {"ok": True, "jobId": job.id, "start": start, "end": end, "accounts": keys}


@app.get("/api/ai/stream")
def api_ai_stream(request: Request, job: str, after: int = 0):
    """把某个 AI 任务的「思考过程 + 正式回答」以 SSE 推给浏览器。

    - 与生成解耦：只读 job，断开不影响生成；
    - 支持续传：`?after=<已收到的序号>`，或浏览器 SSE 自动带的 `Last-Event-ID`；
    - 事件类型：phase / plan / tool / obs / reasoning / content / reset / error / done / state。
    """
    j = aijobs.JOBS.get(job)
    if not j:
        return JSONResponse({"ok": False, "error": "任务不存在或已过期（服务可能重启过）"},
                            status_code=404)
    cursor = max(int(after or 0), 0)
    lei = request.headers.get("last-event-id")
    if lei:
        try:
            cursor = max(cursor, int(lei))
        except ValueError:
            pass

    def _gen():
        nonlocal cursor
        while True:
            evs = j.wait(cursor, timeout=15.0)
            if not evs:
                if j.status == "running":
                    yield ": ping\n\n"       # 心跳，防中间层掐连接
                    continue
                break
            for seq, kind, text in evs:
                cursor = seq
                data = json.dumps({"t": text, "seq": seq}, ensure_ascii=False)
                yield f"id: {seq}\nevent: {kind}\ndata: {data}\n\n"
        yield ("event: state\ndata: "
               + json.dumps(j.to_dict(), ensure_ascii=False) + "\n\n")

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/ai/jobs")
def api_ai_jobs():
    """最近 AI 任务快照（含运行中的），供页面在刷新后找回未完成的生成。"""
    return {"ok": True, "jobs": aijobs.JOBS.snapshot()}


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
                        _autofill_account_id(page, key)
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
        # 与保活/检查/抓取共用同一个 Profile：不拿锁就起浏览器，会在「别的任务正开着
        # 同一个 user_data_dir」时直接失败（页面表现为一直出不来二维码/窗口）。
        if not self._profile_lock.acquire(timeout=60):
            self._set(state="error",
                      message="浏览器登录态正被其他任务占用（抓取/保活/检查），请等它结束再登录")
            return
        try:
            # 容器里没有 X 显示：有头 Chromium 起不来（Playwright 只会抛一句难懂的
            # "headed browser without having a XServer"），这里先给出可操作的提示。
            if show_window and os.name != "nt" and not os.getenv("DISPLAY"):
                self._set(state="error",
                          message="当前环境没有图形界面（容器内没有 X 显示），勾选「显示浏览器窗口」"
                                  "也弹不出窗口。请改用扫码登录；如果必须过滑块，在 WSL 终端执行 "
                                  "./scripts/login_headed.sh <账号key>，窗口会出现在 Windows 桌面，"
                                  "登录完这里刷新就是已登录")
                return
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
                    _autofill_account_id(page, self.account)
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
            try:
                self._profile_lock.release()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self, account: str) -> None:
        from . import config
        from .browser import has_login, launch_persistent, session_ok
        pw = ctx = None
        if not self._profile_lock.acquire(timeout=60):
            self._set(state="error",
                      message="浏览器登录态正被其他任务占用（抓取/保活/检查），请等它结束再扫码")
            return
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
                    _autofill_account_id(page, self.account)
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
            try:
                self._profile_lock.release()
            except Exception:  # noqa: BLE001
                pass


def _autofill_account_id(page, key: str) -> None:
    """新加的账号没填「京准通账户ID」时，登录成功后就地识别并回填（花费/关键词接口要用它）。"""
    try:
        acc = settings.get_account(key)
        if int(acc.get("account_id") or 0) > 0:
            return
        from .browser import login_info
        info = login_info(page) or {}
        aid = int(info.get("userId") or 0)
        if aid > 0:
            settings.set_account_id(key, aid)
            _log_progress(f"账号 {key} 自动识别账户ID：{aid}")
    except Exception:  # noqa: BLE001
        pass


_login = LoginSession()
# 行业大盘补齐与扫码登录 / 保活 / 花费抓取共用同一把 Profile 锁，
# 避免两个 Chromium 同时打开同一个 user_data_dir 把登录态顶掉。
industry.set_profile_lock(_login._profile_lock)
# 花费一览两个独立任务（CPS 付款佣金 / 京准通广告花费）：与登录 / 保活共用同一把 Profile 锁
_paycomm = paycomm.PayCommRunner("cps", profile_lock=_login._profile_lock)
_paycomm_spend = paycomm.PayCommRunner("spend", profile_lock=_login._profile_lock)
# 月度利润分析：商智销量 + ERP 成本 + 上面两套花费缓存
_profit = profit.ProfitRunner(profile_lock=_login._profile_lock)
# 广告费流水（账户中心-财务记录）：与登录 / 保活 / 其它抓取共用同一把 Profile 锁
_cashflow = cashflow.runner(_login._profile_lock)


class AiRunner:
    """后台生成 AI 运营解读（单线程串行，防重复触发）。

    生成过程通过内存 job 暴露「思考过程」事件流（`GET /api/ai/stream?job=`），
    页面可以随时挂上/离开/回来续看，**离开页面不会中断生成**。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"running": False, "startedAt": None, "finishedAt": None,
                       "error": None, "range": None, "accounts": None, "ok": True,
                       "jobId": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def run_background(self, start: str, end: str, accounts) -> dict:
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": "已有 AI 生成任务在运行"}
            keys = [a.get("key") for a in (accounts or settings.ACCOUNTS)]
            job = aijobs.JOBS.new("report", {"start": start, "end": end, "accounts": keys})
            self._state.update({"running": True, "startedAt": settings.now_iso(),
                                "finishedAt": None, "error": None, "ok": True,
                                "range": [start, end], "accounts": keys,
                                "jobId": job.id})

        def _work():
            try:
                job.emit("phase", f"正在读取本地按天库（{start} ~ {end}）…")
                # 行业大盘缺缓存就地自动补齐；等落盘后再生成解读，AI 才拿得到行业基准
                ind_res = industry.ensure(keys, start, end)
                if ind_res.get("missing"):
                    job.emit("phase", "行业大盘缓存补齐中（地域/关键词/客户/趋势）…")
                    industry.wait_for(ind_res["missing"], start, end, timeout=300)
                data = analyze2.build(start=start, end=end,
                                      accounts=[a for a in settings.ACCOUNTS if a["key"] in keys])
                job.emit("phase", "数据就绪（保本线 / 词分层 / 定向缺口已算好），交给模型套方法论判断…")
                out = analyze_ai.analyze_data(data, None, on_event=job.emit)
                job.emit("phase", "生成完成，正在落盘…")
                path, md = analyze_ai.save(out)
                ok = not (out.get("validation") or {}).get("issues")
                with self._lock:
                    self._state.update({"running": False, "ok": ok,
                                        "finishedAt": settings.now_iso()})
                job.finish(result={"range": out.get("range"),
                                   "generatedAt": out.get("generatedAt"), "ok": ok,
                                   "validation": out.get("validation"),
                                   "accounts": out.get("accounts")})
                _log_progress(f"AI 解读已生成：{start} ~ {end} -> {path.name}")
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._state.update({"running": False,
                                        "error": f"{type(exc).__name__}: {exc}",
                                        "finishedAt": settings.now_iso()})
                job.finish(error=f"{type(exc).__name__}: {exc}")
                _log_progress(f"AI 解读生成失败：{type(exc).__name__}: {exc}")

        threading.Thread(target=_work, name="ai-advice", daemon=True).start()
        return {"ok": True, "range": [start, end], "jobId": job.id}


_ai = AiRunner()


def _ai_after_scrape(summary: dict) -> None:
    """夜间抓取完成后的后置任务：对「本地刚抓到的数据」生成 AI 解读。

    区间取自 analyze_ai.local_range()（贴着本地实际已抓到的天），
    而不是自己按天数拍一个区间 —— 避免分析到没抓过的日期。
    """
    if not settings.AI_ENABLED:
        return
    if summary.get("cancelled"):
        return
    rng = analyze_ai.local_range(settings.ACCOUNTS)
    if rng:
        start, end = rng
    else:
        start, end = summary.get("start"), summary.get("end")
    if not (start and end):
        _log_progress("AI 解读：本地没有可用数据，跳过")
        return
    _log_progress(f"开始生成 AI 解读（贴本地数据 {start} ~ {end}）")
    _ai.run_background(start, end, settings.ACCOUNTS)


def _price_snapshot() -> None:
    """核算前台价并落盘。单独 try 住：算价格失败不能连累 AI 解读 / 抓取结果。

    注意：这个回调跑在 `finish_task` 之后，任务日志（append_log）已经写不进去了，
    所以同时 print 到容器日志 —— 否则夜里核算失败会完全看不到原因。
    """
    try:
        done = pricewatch.run_nightly(settings.ACCOUNTS, log=_log_progress)
    except Exception as exc:  # noqa: BLE001
        text = f"前台价核算失败：{type(exc).__name__}: {exc}"
        _log_progress(text)
        print(f"[price] {text}", flush=True)
        return
    errs = [x for x in done if x.get("error")]
    if done:
        text = "前台价核算完成：" + "；".join(
            f"{x.get('label') or x.get('account')} {x.get('day')} "
            f"{x.get('skuCount', '—')} 个 SKU" for x in done if not x.get("error"))
    else:
        text = "前台价核算：没有可用的商智商品明细，跳过"
    _log_progress(text)
    print(f"[price] {text}", flush=True)
    for x in errs:
        print(f"[price] 失败 {x.get('account')} {x.get('day')}：{x.get('error')}", flush=True)


def _after_scrape(summary: dict) -> None:
    """夜间抓取成功后的后置任务：先核算前台价，再生成 AI 解读。

    前台价核算放在这里而不是页面里现算：抓完就有、第二天打开就能看到，
    而且「算过哪一天」会作为文件留下来（页面顶部的「夜间自动核算」卡片读它）。
    """
    if summary.get("cancelled"):
        return
    _price_snapshot()
    _ai_after_scrape(summary)


LOGIN_CSS = DASH_CSS + """
/* 登录页：按钮式页签（复用 .tabs 容器，但登录用 button） */
.tabs{border-bottom:1px solid var(--hair);margin-bottom:18px;gap:2px;margin-top:0}
.tabs button{background:none;border:none;padding:10px 16px;cursor:pointer;font-size:14px;font-family:inherit;
  color:var(--muted);border-bottom:2px solid transparent;font-weight:500;transition:color .15s,border-color .15s}
.tabs button:hover{color:var(--ink)}
.tabs button.active{color:var(--ink);font-weight:600;border-bottom-color:var(--primary)}
.loginwrap{max-width:1040px}
.loginwrap .card{padding:28px 32px}
"""


LOGIN_ACC_JS = r"""
(function(){
  var LBL = {logged_in:'已登录', logged_out:'未登录', error:'异常', unknown:'未检查'};
  var CLS = {logged_in:'b-ok', logged_out:'b-bad', error:'b-bad', unknown:'b-idle'};
  function $(id){ return document.getElementById(id); }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
  function post(url){ return fetch(url,{method:'POST'}).then(function(r){ return r.json(); }); }
  function tip(s, kind){
    var el = $('accMsg'); if(!el) return;
    el.textContent = s || '';
    el.style.color = (kind === 'bad') ? 'var(--red)' : (kind === 'ok' ? 'var(--greenink)' : 'var(--muted)');
  }
  function loadAccounts(){
    fetch('/api/accounts').then(function(r){ return r.json(); }).then(function(j){
      if(!j.ok) return;
      var list = j.accounts || [];
      var rows = list.map(function(a, i){
        var st = '<span class="badge ' + (CLS[a.state] || 'b-idle') + '">'
               + (LBL[a.state] || a.state) + '</span>';
        var main = a.isMain ? ' <span class="badge b-warn">主账号</span>' : '';
        var empty = a.profileExists ? '' : ' <span class="hint">（还没登录过）</span>';
        return '<tr><td class="l"><b>' + esc(a.label) + '</b>' + main + '</td>'
          + '<td class="l"><code>' + esc(a.key) + '</code></td>'
          + '<td class="n">' + (a.account_id || '—') + '</td>'
          + '<td class="l">' + st + ' ' + esc(a.message || '') + empty + '</td>'
          + '<td class="n">'
          + '<button class="btn ghost mini" data-act="up" data-key="' + esc(a.key) + '" '
          + (i === 0 ? 'disabled ' : '') + 'title="上移（同时调整控制台显示顺序）">↑</button> '
          + '<button class="btn ghost mini" data-act="down" data-key="' + esc(a.key) + '" '
          + (i === list.length - 1 ? 'disabled ' : '') + 'title="下移（同时调整控制台显示顺序）">↓</button> '
          + '<button class="btn sec mini" data-act="rename" data-key="' + esc(a.key) + '">改名</button> '
          + '<button class="btn ghost mini" data-act="detect" data-key="' + esc(a.key) + '">识别账户ID</button> '
          + '<button class="btn danger mini" data-act="delete" data-key="' + esc(a.key) + '">删除</button>'
          + '</td></tr>';
      }).join('');
      $('accBody').innerHTML = rows || '<tr><td colspan="5" class="l">还没有账号</td></tr>';
      if(j.checkMessage) tip(j.checkMessage);
    }).catch(function(){});
  }
  function addAccount(){
    var name = ($('newAccName').value || '').trim();
    var aid = ($('newAccId').value || '').trim();
    if(!name){ tip('请先填账号名称', 'bad'); return; }
    post('/api/accounts/add?name=' + encodeURIComponent(name) + '&account_id=' + encodeURIComponent(aid || '0'))
      .then(function(j){
        if(!j.ok){ tip('添加失败：' + (j.error || ''), 'bad'); return; }
        tip('已添加「' + j.account.label + '」，key=' + j.account.key + '，正在刷新…', 'ok');
        setTimeout(function(){ location.reload(); }, 700);
      });
  }
  function moveAccount(key, dir){
    post('/api/accounts/move?key=' + encodeURIComponent(key) + '&dir=' + encodeURIComponent(dir))
      .then(function(j){
        if(!j.ok){ tip('调整顺序失败：' + (j.error || ''), 'bad'); return; }
        tip('已调整顺序，控制台与各页面下拉框会同步生效', 'ok');
        setTimeout(function(){ location.reload(); }, 400);
      });
  }
  function renameAccount(key){
    var cur = '';
    var tr = document.querySelector('button[data-act="rename"][data-key="' + key + '"]');
    if(tr){ var cell = tr.closest('tr'); if(cell) cur = cell.querySelector('b').textContent; }
    var name = prompt('给 ' + key + ' 起个新名称：', cur);
    if(name === null) return;
    name = name.trim();
    if(!name){ tip('名称不能为空', 'bad'); return; }
    post('/api/accounts/rename?key=' + encodeURIComponent(key) + '&name=' + encodeURIComponent(name))
      .then(function(j){
        if(!j.ok){ tip('改名失败：' + (j.error || ''), 'bad'); return; }
        tip('已改名为「' + j.account.label + '」，正在刷新…', 'ok');
        setTimeout(function(){ location.reload(); }, 600);
      });
  }
  function delAccount(key){
    if(!confirm('从账号列表删除 ' + key + ' ？')) return;
    var rm = confirm('是否同时删除它的浏览器 Profile（登录态文件）？\n\n确定 = 连 Profile 一起删；取消 = 只从列表移除，Profile 保留。');
    post('/api/accounts/delete?key=' + encodeURIComponent(key) + '&remove_profile=' + (rm ? 1 : 0))
      .then(function(j){
        if(!j.ok){ tip('删除失败：' + (j.error || ''), 'bad'); return; }
        tip('已删除 ' + key + (rm && j.profileRemoved ? '（含 Profile）' : '（Profile 保留）'), 'ok');
        setTimeout(function(){ location.reload(); }, 600);
      });
  }
  function detectId(key){
    tip('正在用 ' + key + ' 的登录态查询账户ID…');
    post('/api/accounts/detect?account=' + encodeURIComponent(key)).then(function(j){
      if(!j.ok){ tip('识别失败：' + (j.error || ''), 'bad'); return; }
      tip('识别到账户ID ' + j.accountId + '（pin=' + (j.pin || '-') + '）', 'ok');
      loadAccounts();
    });
  }
  document.getElementById('accBody').addEventListener('click', function(e){
    var b = e.target.closest('button[data-act]');
    if(!b) return;
    var key = b.getAttribute('data-key');
    var act = b.getAttribute('data-act');
    if(act === 'rename') renameAccount(key);
    else if(act === 'delete') delAccount(key);
    else if(act === 'detect') detectId(key);
    else if(act === 'up' || act === 'down') moveAccount(key, act);
  });
  document.getElementById('addAccBtn').addEventListener('click', addAccount);
  loadAccounts();
  window.addEventListener('beforeunload', function(){});
})();
"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    acc = request.query_params.get("account") or settings.MAIN_ACCOUNT
    opts = "".join(f'<option value="{_e(a["key"])}"{" selected" if a["key"] == acc else ""}>'
                   f'{_e(a.get("label"))}</option>' for a in settings.ACCOUNTS)
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录京东</title><style>{LOGIN_CSS}</style></head><body>
{report2.navtop("/login")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{report2.SPIKE_SVG}账号登录</div>
  <h1>登录京东</h1>
  <div class="metaline">登录态会写入 <code>{_e(settings.AUTH_DIR)}</code><span class="dot"></span>容器重启后依然有效</div>
</div></header>
<div class="wrap loginwrap">
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
    <div class="warn">若登录页要求滑块或短信验证：需要真实浏览器窗口。线上容器是无头环境（没有 X 显示），
      勾选「显示浏览器窗口」也弹不出来 —— 请改用上面的「扫码登录」，或在 WSL 终端执行
      <code>./scripts/login_headed.sh main</code>（把 main 换成对应账号 key），
      窗口会出现在 Windows 桌面，登录完成后刷新本页即为已登录。</div>
  </div>

  <div style="display:flex;gap:10px;align-items:center;margin-top:14px">
    <button class="btn sec" onclick="cancelLogin()">取消</button>
    <a href="/">← 控制台</a>
  </div>
  <div id="status" class="hint" style="margin-top:10px">未开始</div>
</div>

<div class="card" style="margin-top:18px">
  <h2>账号管理 <span class="tagline">添加 / 改名 / 删除；改动写回 config/accounts.json</span></h2>
  <p class="hint">新账号先在上面选中并扫码登录，登录成功后会自动识别「京准通账户ID」。
    每个账号一个独立浏览器 Profile，互不影响。</p>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
    <input id="newAccName" placeholder="账号名称，例如「梓云茶2店」"
           style="padding:7px 10px;border-radius:8px;border:1px solid var(--line);width:220px">
    <input id="newAccId" placeholder="京准通账户ID（可留空）" inputmode="numeric"
           style="padding:7px 10px;border-radius:8px;border:1px solid var(--line);width:190px">
    <button class="btn" id="addAccBtn">添加账号</button>
  </div>
  <div class="tblwrap short">
    <table><thead><tr>
      <th class="l noSort">名称</th><th class="l noSort">key</th><th class="n noSort">账户ID</th>
      <th class="l noSort">登录态</th><th class="n noSort">操作</th>
    </tr></thead><tbody id="accBody">
      <tr><td colspan="5" class="l">加载中…</td></tr>
    </tbody></table>
  </div>
  <div class="hint" id="accMsg" style="margin-top:10px"></div>
</div>
</div>
<script>{LOGIN_ACC_JS}</script>
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


# ---------------------------------------------------------------- 账号管理
def _account_state_map() -> dict:
    try:
        snap = _login.accounts_snapshot()
    except Exception:  # noqa: BLE001
        return {}
    raw = snap.get("accounts") or {}
    if isinstance(raw, list):
        raw = {x.get("key"): x for x in raw}
    return raw


@app.get("/api/accounts")
def api_accounts():
    """账号列表（含登录态快照），供登录页的账号管理表格使用。"""
    states = _account_state_map()
    out = []
    for a in settings.ACCOUNTS:
        st = states.get(a["key"]) or {}
        prof = Path(a.get("user_data_dir") or "")
        out.append({
            "key": a["key"],
            "label": a.get("label") or a["key"],
            "account_id": int(a.get("account_id") or 0),
            "isMain": a["key"] == settings.MAIN_ACCOUNT,
            "profileExists": bool(prof and prof.exists()),
            "state": st.get("state") or "unknown",
            "message": st.get("message") or "",
        })
    return {"ok": True, "accounts": out, "main": settings.MAIN_ACCOUNT,
            "file": str(settings.ACCOUNTS_FILE)}


@app.post("/api/accounts/add")
def api_accounts_add(name: str = "", account_id: str = "", key: str = ""):
    """新增账号（只写配置，profile 在登录后生成）。"""
    try:
        aid = int(str(account_id).strip() or 0)
    except ValueError:
        return {"ok": False, "error": "账户ID 只能是数字"}
    try:
        acc = settings.add_account(name, aid, key or None)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "account": {"key": acc["key"], "label": acc["label"],
                                    "account_id": acc.get("account_id") or 0}}


@app.post("/api/accounts/rename")
def api_accounts_rename(key: str = "", name: str = ""):
    try:
        acc = settings.rename_account(key, name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "account": {"key": acc["key"], "label": acc["label"],
                                    "account_id": acc.get("account_id") or 0}}


@app.post("/api/accounts/move")
def api_accounts_move(key: str = "", dir: str = "up"):
    """调整账号显示顺序（up/down）：写回 accounts.json，控制台与各页下拉框同步。

    只改顺序，不改主账号：主账号由 accounts.json 的 `main: true` 显式记录。
    """
    if dir not in ("up", "down"):
        return {"ok": False, "error": "dir 只能是 up 或 down"}
    try:
        accs = settings.move_account(key, dir)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "main": settings.MAIN_ACCOUNT,
            "accounts": [{"key": a["key"], "label": a.get("label") or a["key"]} for a in accs]}


@app.post("/api/accounts/setmain")
def api_accounts_setmain(key: str = ""):
    """把某个账号设为「主账号」（之后调整顺序不会改变它）。"""
    try:
        acc = settings.set_main_account(key)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "account": {"key": acc["key"], "label": acc.get("label") or acc["key"]},
            "main": settings.MAIN_ACCOUNT}


@app.post("/api/accounts/delete")
def api_accounts_delete(key: str = "", remove_profile: int = 0):
    """删除账号；remove_profile=1 时连浏览器 Profile 一起删。"""
    snap = _login.snapshot()
    if snap.get("account") == key and snap.get("state") in ("starting", "waiting", "need_verify"):
        return {"ok": False, "error": "该账号正在登录中，先取消登录再删除"}
    try:
        settings.delete_account(key)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    removed = False
    if remove_profile:
        # 账号已从列表移除，按默认规则推算 profile 目录
        prof = Path(settings.default_profile_dir(key))
        auth = Path(settings.AUTH_DIR).resolve()
        try:
            target = prof.resolve()
            if target != auth and str(target).startswith(str(auth) + os.sep) and target.exists():
                import shutil
                shutil.rmtree(target, ignore_errors=True)
                removed = True
        except Exception:  # noqa: BLE001
            removed = False
    return {"ok": True, "key": key, "profileRemoved": removed}


@app.post("/api/accounts/detect")
def api_accounts_detect(account: str = ""):
    """用该账号已登录的 Profile 查一次 logininfo，回填京准通账户ID。"""
    key = account or settings.MAIN_ACCOUNT
    if not any(a["key"] == key for a in settings.ACCOUNTS):
        return {"ok": False, "error": "账号不存在"}
    lock = _login._profile_lock
    if not lock.acquire(timeout=90):
        return {"ok": False, "error": "浏览器登录态正被占用（登录/保活/抓取中），稍后再试"}
    pw = ctx = None
    try:
        from .browser import launch_persistent as _launch, login_info, session_ok as _ok
        pw, ctx = _launch(headless=True, account=key)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if not _ok(page):
            return {"ok": False, "error": "该账号登录态已失效，请重新扫码登录"}
        info = login_info(page) or {}
        aid = int(info.get("userId") or 0)
        if aid <= 0:
            return {"ok": False, "error": "接口没返回账户ID（可能是子账号）"}
        settings.set_account_id(key, aid)
        return {"ok": True, "accountId": aid, "pin": info.get("pin") or ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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
            lock.release()
        except Exception:  # noqa: BLE001
            pass


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
def _activity_nightly():
    """凌晨团长活动抓取：报名日志（近 30 天）+ 效果（近 REFRESH_DAYS 天）。

    与按天抓取共用同一把抓取锁 / 同一个浏览器 Profile，所以必须在主抓取
    释放之后再跑（activity.run 会阻塞等锁，最长 1 小时）。
    """
    try:
        today = _today()
        start = (today - dt.timedelta(days=30)).isoformat()
        _log_progress("团长活动：开始抓取报名日志（近 30 天）与效果数据")
        res = activity.run(accounts=settings.ACCOUNTS, start=start,
                           end=today.isoformat(), perf_days=settings.REFRESH_DAYS,
                           log=_log_progress)
        total = sum(v.get("signups", 0) for v in (res.get("accounts") or {}).values())
        _log_progress(f"团长活动：完成，本次解析报名 {total} 条")
    except Exception as exc:  # noqa: BLE001
        _log_progress(f"团长活动抓取失败：{type(exc).__name__}: {exc}")


def _activity_fresh(hours: int = 6) -> bool:
    """全部账号都在最近 N 小时内抓过，就不再重复跑（容器频繁重启时省时间）。"""
    now = settings.now().replace(tzinfo=None)
    for a in settings.ACCOUNTS:
        st = activity._read(activity.acct_dir(a["key"]) / "state.json", {})
        last = st.get("lastRun")
        if not last:
            return False
        try:
            if (now - dt.datetime.fromisoformat(last)).total_seconds() > hours * 3600:
                return False
        except ValueError:
            return False
    # 还要确认「昨天」的订单明细在：夜间任务偶尔会被抓取锁/重启打断，
    # 只按 lastRun 判断会漏掉整天的数据。
    yday = (settings.today() - dt.timedelta(days=1)).isoformat()
    for a in settings.ACCOUNTS:
        od = activity.acct_dir(a["key"]) / "orders"
        if od.is_dir() and not (od / f"{yday}.json").exists():
            return False
    return True


_ACT_SPAWN_AT = {"t": 0.0}


def _spawn_activity_nightly() -> None:
    if activity.runtime_status().get("active"):
        return
    if _activity_fresh():
        # 不用 _log_progress：它会覆盖「当前抓取进度」，主抓取还在跑时容易误导
        print("[activity] 最近已抓过，跳过", flush=True)
        return
    # 启动补跑与定时调度可能在同一分钟内各调一次，只允许安排一个线程去等锁
    if time.time() - _ACT_SPAWN_AT["t"] < 7200:
        return
    _ACT_SPAWN_AT["t"] = time.time()
    threading.Thread(target=_activity_nightly, name="activity-nightly", daemon=True).start()


def _nightly_job():
    """回刷最近 REFRESH_DAYS 天，并补齐停机期间的空洞。"""
    # 行业大盘按「本地实际有数据的区间」单独补一份缓存：
    # 即使按天数据已经最新，也要让明天的报表 / AI 解读能直接命中行业基准。
    try:
        lr = analyze_ai.local_range(settings.ACCOUNTS)
        if lr:
            industry.ensure([a["key"] for a in settings.ACCOUNTS], lr[0], lr[1])
    except Exception as exc:  # noqa: BLE001
        _log_progress(f"行业大盘补齐未启动：{type(exc).__name__}: {exc}")
    rng = scrape_day.catchup_range()
    if rng is None:
        _log_progress("数据已是最新，无需抓取")
        # 没有抓取也要核算：可能是白天手动补过数，页面摘要不该停在更早的一天
        _price_snapshot()
        _spawn_activity_nightly()
        return
    start, end = rng
    newest = scrape_day.newest_day()
    _log_progress(f"定时任务 {start} ~ {end}（库内最新 {newest or '无'}）")
    res = scrape_day.run_background(settings.ACCOUNTS, start, end, force=False,
                                     log=_log_progress, name="nightly",
                                     trigger="schedule",
                                     after=_after_scrape)
    if not res.get("ok"):
        _log_progress(f"定时任务未启动：{res.get('error')}")
    # 主抓取已占锁，团长活动线程会等它结束后再跑（同一个 Profile 不能并发）
    _spawn_activity_nightly()


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
    """带时区的当前时间。统一走 settings.now()（显式 ZoneInfo(TZ)）。"""
    return settings.now()


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

# 商智「昨天」的商品明细在凌晨 1 点还没出数（接口成功但 0 行），
# 而夜间任务只跑一次 → 前台价会一直停在两天前。这里白天定时只补商智。
_sz_retry_state = {"last": 0.0}


def _sz_retry_days() -> list:
    """最近几天里「商智商品明细还没文件 / 0 行」的天（不算今天）。"""
    today = _today()
    out = set()
    for a in settings.ACCOUNTS:
        for i in range(1, max(settings.SZ_RETRY_LOOKBACK, 1) + 1):
            day = (today - dt.timedelta(days=i)).isoformat()
            if ds.payload_rows(ds.load_day(a["key"], day, "sz_product")) == 0:
                out.add(day)
    return sorted(out)


def _after_sz_retry(summary: dict) -> None:
    """补到商智数据后重算一次前台价（页面「夜间自动核算」卡片跟着更新）。"""
    if summary.get("cancelled"):
        return
    _price_snapshot()


def _sz_retry_loop() -> None:
    """白天维护商智数据（只在 08:00~23:00，且没有别的任务在跑时）：

    ① 今日实时：按 SZ_RT_INTERVAL 刷新 sz_product_rt，前台价页「今日实时」卡片用；
    ② 0 行天补抓：凌晨抓「昨天」时商智常还没出数，补上后重算前台价。
    """
    while True:
        time.sleep(300)
        try:
            if not (settings.SZ_RETRY_ENABLED or settings.SZ_RT_ENABLED):
                continue
            now = dt_now()
            if not (settings.SZ_RETRY_START_HOUR <= now.hour <= settings.SZ_RETRY_END_HOUR):
                continue
            if scrape_day.is_running():
                continue
            # ① 今日实时（每账号 1 个请求）
            if (settings.SZ_RT_ENABLED
                    and time.time() - _sz_retry_state.get("rt_at", 0.0)
                    >= settings.SZ_RT_INTERVAL):
                _sz_retry_state["rt_at"] = time.time()
                today = ds.today().isoformat()
                res = scrape_day.run_background(
                    settings.ACCOUNTS, today, today, force=True, kinds=["sz_product_rt"],
                    log=_log_progress, name="sz-realtime", trigger="schedule")
                if not res.get("ok"):
                    _log_progress(f"今日实时刷新未启动：{res.get('error')}")
                continue          # 与下面的补抓分开跑，别抢同一把锁
            # ② 0 行天补抓
            if not settings.SZ_RETRY_ENABLED:
                continue
            if time.time() - _sz_retry_state["last"] < settings.SZ_RETRY_INTERVAL:
                continue
            days = _sz_retry_days()
            if not days:
                continue
            _sz_retry_state["last"] = time.time()
            _log_progress(f"商智补抓：{days[0]} ~ {days[-1]} 仍是 0 行，只补商智商品/流量")
            res = scrape_day.run_background(
                settings.ACCOUNTS, days[0], days[-1], kinds=["sz_product", "sz_flow"],
                log=_log_progress, name="sz-retry", trigger="schedule",
                after=_after_sz_retry)
            if not res.get("ok"):
                _log_progress(f"商智补抓未启动：{res.get('error')}")
        except Exception as exc:  # noqa: BLE001
            _log_progress(f"商智补抓异常：{type(exc).__name__}: {exc}")


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
        if settings.SZ_RETRY_ENABLED:
            threading.Thread(target=_sz_retry_loop, name="sz-retry", daemon=True).start()
            print("[webapp] 商智补抓已启动：白天每 "
                  f"{max(settings.SZ_RETRY_INTERVAL // 60, 1)} 分钟检查一次「0 行天」",
                  flush=True)
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
