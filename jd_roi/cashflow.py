# -*- coding: utf-8 -*-
"""京准通「账户中心-财务记录」资金流水：抓取、汇总、柱状图、导出。

数据来源（都用各账号自己的登录 Profile 直接 fetch，参数照抄页面真实请求体）：
    POST https://atoms-api.jd.com/financecore/subaccount/allbalance/get        余额
    POST .../account/cashrecharge/list                                          充值
    POST .../subaccount/cashconsume/list                                        消费
    POST .../account/cashassign/list                                            分配（内部划转）
    POST .../subaccount/freeze/list                                             冻结
    POST .../subaccount/consumer/sumCost/query                                  消费汇总

口径：
    * 交易日期 = creatTime / showDate（广告实际消耗的自然日）
    * 记账时间 = settleTime（次日 08:45 左右批量扣款）
    * 结余     = 接口 balanceAmount 原值；没有这个字段的账号按「期初+充值-消费」滚动推算
    * 分配     = 「商家账户 ↔ 投放子账户」内部划转，不改变账户现金总额，不进余额滚动
"""
from __future__ import annotations

import io
import json
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import settings

URL = "https://jzt.jd.com/account/#/financialRecord"
API = "https://atoms-api.jd.com/financecore/"
CACHE_DIR = settings.DATA_DIR / "cashflow"

GREEN = "2E9E5B"          # 充值（向上）
RED = "D64545"            # 消费（向下）
TYPE_ORDER = ["快车扣费", "全站营销扣费", "京东联盟扣费"]

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# 点 tab 触发页面自己发请求，才能偷到完整参数（orderTypes 那串长列表只有前端知道）
TABS = ["消费记录", "分配记录", "冻结记录", "充值记录"]
TPL_KEYS = ["account/cashrecharge/list", "account/cashrecharge/amountall/get",
            "subaccount/cashconsume/list", "subaccount/consumer/sumCost/query",
            "account/cashassign/list", "subaccount/freeze/list",
            "subaccount/subpin/list", "subaccount/allbalance/get"]

JS_CALL = """async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json',
              'accept': 'application/json, text/plain, */*',
              'referer': 'https://jzt.jd.com/account/',
              'language': 'zh_CN', 'siteid': '0', 'loginmode': '0'},
    body: JSON.stringify(body || {})});
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return {code: -1, msg: t.slice(0, 300)}; }
}"""


# ============================================================ 抓取
def _sleep() -> None:
    time.sleep(0.8 + random.random() * 0.8)


def _rows(payload) -> tuple[list, int]:
    """把各种嵌套的 {data:{data:[...], paginator:{total}}} 摊平成 (rows, total)。"""
    d = (payload or {}).get("data")
    total = 0
    for _ in range(4):
        if isinstance(d, dict):
            total = total or (d.get("paginator") or {}).get("total") or d.get("total") or 0
            if isinstance(d.get("data"), list):
                return d["data"], total or len(d["data"])
            if isinstance(d.get("list"), list):
                return d["list"], total or len(d["list"])
            d = d.get("data")
        elif isinstance(d, list):
            return d, total or len(d)
        else:
            break
    return [], total


def _collect_templates(page) -> dict:
    tpl: dict = {}

    def on_resp(resp):
        u = resp.url
        for k in TPL_KEYS:
            if k in u and k not in tpl:
                try:
                    tpl[k] = json.loads(resp.request.post_data or "{}")
                except Exception:  # noqa: BLE001
                    tpl[k] = {}

    page.on("response", on_resp)
    for tab in TABS:
        try:
            page.locator(f"text={tab}").first.click(timeout=8000)
            page.wait_for_timeout(5000)
        except Exception:  # noqa: BLE001
            pass
    return tpl


def _fetch_all(page, api: str, base: dict, begin: str, end: str,
               page_size: int = 1000, max_pages: int = 30) -> list:
    out: list = []
    p = 1
    while p <= max_pages:
        body = dict(base or {})
        body.update({"beginDate": begin, "endDate": end, "page": p, "pageSize": page_size})
        got = page.evaluate(JS_CALL, {"url": API + api, "body": body})
        rows, _ = _rows(got)
        out.extend(rows)
        # 只按「本页是否拿满」判断翻页：paginator 的层级各接口不一致，不能当唯一依据
        if not rows or len(rows) < page_size:
            break
        p += 1
        _sleep()
    return out


def fetch_account(key: str, begin: str, end: str) -> dict:
    """抓一个账号的财务记录；调用方负责持有 Profile 锁。"""
    from .browser import launch_persistent, session_ok

    acc = settings.get_account(key)
    pw, context = launch_persistent(headless=True, account=key)
    try:
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)
        if not session_ok(page):
            raise RuntimeError("登录态失效，请先到「登录」页扫码")

        tpl = _collect_templates(page)
        balance = page.evaluate(JS_CALL, {"url": API + "subaccount/allbalance/get", "body": {}})
        _sleep()
        pins = page.evaluate(JS_CALL, {
            "url": API + "subaccount/subpin/list",
            "body": tpl.get("subaccount/subpin/list") or
                    {"page": 1, "pageSize": 65535, "dimension": 1, "sortType": 3}})
        pin_rows, _ = _rows(pins)
        _sleep()

        recharge = _fetch_all(page, "account/cashrecharge/list",
                              tpl.get("account/cashrecharge/list") or {}, begin, end)
        consume = _fetch_all(page, "subaccount/cashconsume/list",
                             tpl.get("subaccount/cashconsume/list") or {}, begin, end)
        assign = _fetch_all(page, "account/cashassign/list",
                            tpl.get("account/cashassign/list") or {}, begin, end)

        fbase = dict(tpl.get("subaccount/freeze/list") or {})
        fbase["pin"] = (pin_rows[0].get("pinName") if pin_rows else "") or ""
        freeze = _fetch_all(page, "subaccount/freeze/list", fbase, begin, end)

        sbase = dict(tpl.get("subaccount/consumer/sumCost/query") or {})
        sbase.pop("page", None)
        sbase.pop("pageSize", None)
        summary = page.evaluate(JS_CALL, {"url": API + "subaccount/consumer/sumCost/query",
                                          "body": dict(sbase, beginDate=begin, endDate=end)})
        _sleep()
        info = page.evaluate(JS_CALL, {"url": API + "subaccount/consumer/info",
                                       "body": {"beginDate": begin, "endDate": end,
                                                "timeFilterType": 0}})

        return {
            "account": key, "label": acc.get("label") or key,
            "account_id": acc.get("account_id") or 0,
            "range": {"begin": begin, "end": end},
            "fetched_at": settings.now_iso(),
            "balance": (balance or {}).get("data"),
            "pins": pin_rows,
            "summary": (summary or {}).get("data"),
            "consume_info": (info or {}).get("data"),
            "recharge": recharge, "consume": consume,
            "assign": assign, "freeze": freeze,
        }
    finally:
        try:
            context.close()
        finally:
            pw.stop()


def save(data: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{data.get('account')}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def load_all() -> list[dict]:
    items = []
    for a in settings.ACCOUNTS:
        p = CACHE_DIR / f"{a['key']}.json"
        if not p.exists():
            continue
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return items


# ============================================================ 汇总
def _ts(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000)
    except (TypeError, ValueError, OSError):
        return None


def _d(ms) -> str:
    t = _ts(ms)
    return t.strftime("%Y-%m-%d") if t else ""


def _day(e: dict) -> str:
    """交易日期：优先 showDate(YYYYMMDD)，其次 creatTime。

    子账户直接投放的账号（消费记录里 settleTime / balanceAmount 全为 null）
    只有 showDate 标明消耗归属日，用它才不会把整月消耗挤到一天。
    """
    sd = e.get("showDate")
    if sd:
        s = str(sd)
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return _d(e.get("creatTime"))


def _t(ms) -> str:
    t = _ts(ms)
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else ""


def _f(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def build(data: dict) -> dict:
    """单账号：标准化成事件列表 + 汇总数字。"""
    acc = data.get("account")
    label = data.get("label") or acc
    aid = data.get("account_id") or 0
    bal = data.get("balance") or {}
    cash = _f(bal.get("cashBalance"))

    events: list[dict] = []
    for r in data.get("recharge") or []:
        if r.get("statusName") and "成功" not in str(r.get("statusName")):
            continue
        events.append({
            "account": acc, "label": label, "account_id": aid,
            "kind": "充值", "dir": "收入", "type": r.get("orderTypeName") or "充值",
            "date": _d(r.get("createDate")), "book_time": _t(r.get("updateDate")),
            "amount": _f(r.get("amount")),
            "balance": _f(r.get("balanceAmount")) if r.get("balanceAmount") is not None else None,
            "no": r.get("orderFrom") or r.get("pk"),
            "fund": r.get("amountOwnership") or r.get("nickname") or "",
            "remark": r.get("remarkDesc") or r.get("remark") or "",
        })
    for c in data.get("consume") or []:
        events.append({
            "account": acc, "label": label, "account_id": aid,
            "kind": "消费", "dir": "支出", "type": c.get("orderTypeName") or "扣费",
            "date": _day(c), "book_time": _t(c.get("settleTime")),
            "amount": _f(c.get("amount")),
            "balance": _f(c.get("balanceAmount")) if c.get("balanceAmount") is not None else None,
            "no": c.get("pk"), "fund": c.get("pin") or c.get("nickname") or "",
            "remark": c.get("invoiceApplyTypeName") or "",
        })
    for a in data.get("assign") or []:
        if a.get("statusName") and "成功" not in str(a.get("statusName")):
            continue
        events.append({
            "account": acc, "label": label, "account_id": aid,
            "kind": "分配", "dir": "转入" if a.get("assignType") == 1 else "转出",
            "type": a.get("assignTypeName") or "分配",
            "date": _d(a.get("createTime")), "book_time": _t(a.get("createTime")),
            "amount": _f(a.get("amount")), "balance": None, "no": a.get("pk"),
            "fund": f"{a.get('assignPin') or ''} → {a.get('targetPin') or ''}",
            "remark": a.get("remark") or "",
        })
    for f in data.get("freeze") or []:
        amt = _f(f.get("costAmount") or f.get("amount"))
        if not amt:
            continue
        events.append({
            "account": acc, "label": label, "account_id": aid,
            "kind": "冻结", "dir": "支出", "type": f.get("channelName") or "冻结",
            "date": _d(f.get("createDate")), "book_time": str(f.get("actionTime") or "")[:19],
            "amount": amt, "balance": None, "no": f.get("pk"),
            "fund": f.get("pin") or "", "remark": f.get("remark") or "",
        })
    events.sort(key=lambda e: (e["book_time"] or e["date"], e["kind"]))

    consume_sum = round(sum(e["amount"] for e in events if e["kind"] == "消费"), 2)
    recharge_sum = round(sum(e["amount"] for e in events if e["kind"] == "充值"), 2)
    assign_in = round(sum(e["amount"] for e in events
                          if e["kind"] == "分配" and e["dir"] == "转入"), 2)
    assign_out = round(sum(e["amount"] for e in events
                           if e["kind"] == "分配" and e["dir"] == "转出"), 2)

    # 期初余额：最后一笔"有结余"的已结算记录往回推；没有结余字段就退回用当前余额推
    settled = [e for e in events
               if e["kind"] in ("充值", "消费") and e["balance"] is not None and e["balance"] > 0]
    if settled:
        last_bal = settled[-1]["balance"]
        begin_bal = round(last_bal + consume_sum - recharge_sum, 2)
        begin_from = "末笔结余倒推"
    else:
        last_bal = None
        begin_bal = round(cash + consume_sum - recharge_sum, 2)
        begin_from = "当前余额倒推"

    s = data.get("summary") or {}
    info = data.get("consume_info") or {}
    details = info.get("details") or []
    cash_cost = _f(s.get("rtbTotalCost") or (details[0].get("cashCost") if details else 0))
    award_cost = _f(details[0].get("awardCost") if details else 0)
    total_cost = _f((details[0].get("totalCost") if details else 0) or cash_cost)

    days = sorted({e["date"] for e in events if e["kind"] == "消费" and e["date"]})
    day_avg = round(consume_sum / len(days), 2) if days else 0.0

    return {
        "account": acc, "label": label, "account_id": aid,
        "range": data.get("range") or {}, "fetched_at": data.get("fetched_at") or "",
        "cash_balance": cash, "award_balance": _f(bal.get("awardBalance")),
        "begin_balance": begin_bal,
        "settled_balance": round(last_bal, 2) if last_bal is not None else None,
        "begin_from": begin_from,
        "recharge_sum": recharge_sum, "consume_sum": consume_sum,
        "cash_cost": cash_cost, "award_cost": award_cost, "total_cost": total_cost,
        "assign_in": assign_in, "assign_out": assign_out,
        "active_days": len(days), "day_avg": day_avg,
        "last_cost_day": days[-1] if days else "",
        "cover_days": round(cash / day_avg, 1) if day_avg else None,
        "events": events,
    }


def date_list(begin: str, end: str) -> list[str]:
    out = []
    try:
        d0 = datetime.strptime(begin, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
    except (TypeError, ValueError):
        return out
    while d0 <= d1:
        out.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    return out


def daily_rows(accs: list[dict], begin: str, end: str) -> list[dict]:
    """日期 × 账号 的每日流水（消耗按交易日期归属）。"""
    dates = date_list(begin, end)
    out = []
    for a in accs:
        if not a["consume_sum"] and not a["recharge_sum"]:
            continue                      # 无投放的账号不占每日表
        byday: dict[str, dict] = defaultdict(lambda: {"types": defaultdict(float),
                                                      "recharge": 0.0, "assign": 0.0,
                                                      "balance": None})
        for e in a["events"]:
            day = e["date"]
            if not day:
                continue
            rec = byday[day]
            if e["kind"] == "消费":
                rec["types"][e["type"]] += e["amount"]
            elif e["kind"] == "充值":
                rec["recharge"] += e["amount"]
            elif e["kind"] == "分配":
                rec["assign"] += e["amount"] if e["dir"] == "转入" else -e["amount"]
            if e["balance"] is not None:
                rec["balance"] = e["balance"]
        rolling = a["begin_balance"]
        for day in dates:
            rec = byday.get(day)
            cost = round(sum((rec["types"] or {}).values()), 2) if rec else 0.0
            recharge = round(rec["recharge"], 2) if rec else 0.0
            assign = round(rec["assign"], 2) if rec else 0.0
            rolling = round(rolling + recharge - cost, 2)
            bal = rec["balance"] if rec and rec["balance"] is not None else rolling
            if rec and rec["balance"] is not None:
                rolling = rec["balance"]      # 以接口为准，顺便纠掉推算误差
            out.append({"date": day, "account": a["account"], "label": a["label"],
                        "types": dict(rec["types"]) if rec else {}, "consume": cost,
                        "recharge": recharge, "assign": assign, "balance": bal})
    return out


def snapshot() -> dict:
    """页面用的数据快照：读本地缓存文件，不触发任何请求。"""
    raw = load_all()
    accs = [build(d) for d in raw]
    if not accs:
        return {"ok": False, "empty": True, "accounts": [], "daily": [], "labels": []}
    ranges = {(a["range"].get("begin"), a["range"].get("end")) for a in accs if a["range"]}
    begin = min((r[0] for r in ranges if r[0]), default="")
    end = max((r[1] for r in ranges if r[1]), default="")
    daily = daily_rows(accs, begin, end)
    return {
        "ok": True, "empty": False, "accounts": accs, "daily": daily,
        "begin": begin, "end": end,
        "fetched_at": max((a["fetched_at"] for a in accs), default=""),
        "ranges_mixed": len(ranges) > 1,
        "labels": [a["label"] for a in accs],
        "totals": {
            "cash": round(sum(a["cash_balance"] for a in accs), 2),
            "begin": round(sum(a["begin_balance"] for a in accs), 2),
            "recharge": round(sum(a["recharge_sum"] for a in accs), 2),
            "consume": round(sum(a["consume_sum"] for a in accs), 2),
        },
    }


# ============================================================ 柱状图
def _fmt_money(v: float) -> str:
    return f"{v / 10000:.1f}万" if abs(v) >= 10000 else f"{v:,.0f}"


def chart_svg(label: str, dates: list[str], rec: list[float], con: list[float],
              w: int = 1400, half: int = 120, top: int = 26, bottom: int = 34) -> str:
    """一张「上绿=充值、下红=消费、上下各自独立刻度」的图。

    上下高度按「有充值的天数 : 有消费的天数」分配（限制在 1:2 ~ 1:1）：
    充值通常一个月只发生几天，固定对半分会让上半区大片留白、绿柱被压扁，
    所以默认给下方消费留 2/3 的高度；充值频繁的账号自动回到 1:1。
    """
    pad_l, pad_r = 78, 22
    span = half * 2                              # 0 线上下合计高度
    h = top + span + bottom
    plot_w = w - pad_l - pad_r
    n = max(len(dates), 1)
    step = plot_w / n
    bar_w = max(6.0, min(24.0, step * 0.55))
    mr = max(rec) or 1.0
    mc = max(con) or 1.0
    has_rec = max(rec) > 0

    n_rec = sum(1 for v in rec if v)
    n_con = sum(1 for v in con if v)
    if not has_rec:
        ratio = 0.24                             # 无充值：上面只留一行提示文字的位置
    else:
        ratio = min(max(n_rec / max(n_rec + n_con, 1), 1 / 3), 0.5)
    h_up = round(span * ratio)
    h_dn = span - h_up
    y0 = top + h_up

    p = [f'<div class="cfchart"><div class="cfchd"><b>{label}</b><span>'
         f'<i class="g"></i>充值（上）<i class="r"></i>消费（下）　单位：元</span></div>',
         f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none">']
    if has_rec:
        for frac in (0.0, 0.5, 1.0):
            y = y0 - h_up * frac
            p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" stroke="#eeeae4"/>')
            p.append(f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" class="ax">'
                     f'{_fmt_money(mr * frac)}</text>')
    else:
        p.append(f'<text x="{pad_l + plot_w / 2:.1f}" y="{top + h_up / 2 + 4:.1f}" '
                 f'text-anchor="middle" class="empty">本期无充值记录（余额由内部划转补入）</text>')
    for frac in (0.0, 0.5, 1.0):
        y = y0 + h_dn * frac
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" stroke="#eeeae4"/>')
        if frac:
            p.append(f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" class="ax">'
                     f'{_fmt_money(mc * frac)}</text>')
    p.append(f'<line x1="{pad_l}" y1="{y0}" x2="{w - pad_r}" y2="{y0}" stroke="#c9c2b8"/>')
    for i, d in enumerate(dates):
        x = pad_l + step * i + (step - bar_w) / 2
        if i < len(rec) and rec[i]:
            v = rec[i]
            bh = max(1.5, v / mr * (h_up - 10))
            p.append(f'<rect x="{x:.1f}" y="{y0 - bh:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
                     f'fill="#{GREEN}" rx="1.5"><title>{d} 充值 {v:,.2f}</title></rect>')
            if bh >= h_up * 0.3:
                p.append(f'<text x="{x + bar_w / 2:.1f}" y="{y0 - bh - 4:.1f}" text-anchor="middle" '
                         f'class="v gv">{_fmt_money(v)}</text>')
        if i < len(con) and con[i]:
            v = con[i]
            bh = max(1.5, v / mc * (h_dn - 10))
            p.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
                     f'fill="#{RED}" rx="1.5"><title>{d} 消费 {v:,.2f}</title></rect>')
            if bh >= h_dn * 0.3:
                p.append(f'<text x="{x + bar_w / 2:.1f}" y="{y0 + bh + 11:.1f}" text-anchor="middle" '
                         f'class="v rv">{_fmt_money(v)}</text>')
    for i, d in enumerate(dates):
        if i % 2:
            continue
        x = pad_l + step * i + step / 2
        p.append(f'<text x="{x:.1f}" y="{y0 + h_dn + 18:.1f}" text-anchor="middle" '
                 f'class="ax">{d[5:]}</text>')
    p.append("</svg></div>")
    return "".join(p)


def charts_html(snap: dict) -> str:
    accs = [a for a in snap["accounts"] if a["consume_sum"] or a["recharge_sum"]]
    dates = date_list(snap["begin"], snap["end"])
    mat = {(r["date"], r["account"]): r for r in snap["daily"]}
    blocks = []
    groups = [("全账号合计", None)] + [(a["label"], a["account"]) for a in accs]
    for label, key in groups:
        rec, con = [], []
        for d in dates:
            if key is None:
                rows = [mat.get((d, a["account"])) or {} for a in accs]
                rec.append(sum(r.get("recharge") or 0 for r in rows))
                con.append(sum(r.get("consume") or 0 for r in rows))
            else:
                r = mat.get((d, key)) or {}
                rec.append(r.get("recharge") or 0)
                con.append(r.get("consume") or 0)
        blocks.append(chart_svg(label, dates, rec, con))
    return "".join(blocks)


# ============================================================ 导出
def xlsx_bytes(accs: list[dict], daily: list[dict], begin: str, end: str) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    rng = f"{begin} ~ {end}"

    head = ["账号", "账户ID", "当前可用余额(¥)", "区间期初余额(¥)", "区间充值(¥)",
            "区间消费(¥)", "其中现金(¥)", "其中红包(¥)", "分配净额(¥)",
            "有消耗天数", "日均消费(¥)", "余额可撑(天)", "口径说明"]
    ws = wb.create_sheet("01 账号汇总")
    ws.append(head)
    for i, w in enumerate([12, 14, 16, 18, 14, 14, 13, 12, 13, 11, 13, 11, 40], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for a in accs:
        ws.append([a["label"], a["account_id"], a["cash_balance"], a["begin_balance"],
                   a["recharge_sum"], a["consume_sum"], a["cash_cost"], a["award_cost"],
                   round(a["assign_in"] - a["assign_out"], 2), a["active_days"],
                   a["day_avg"] or None, a["cover_days"], f"{rng}｜期初{a['begin_from']}"])
    ws.append(["合计", "", round(sum(a["cash_balance"] for a in accs), 2),
               round(sum(a["begin_balance"] for a in accs), 2),
               round(sum(a["recharge_sum"] for a in accs), 2),
               round(sum(a["consume_sum"] for a in accs), 2),
               round(sum(a["cash_cost"] for a in accs), 2),
               round(sum(a["award_cost"] for a in accs), 2),
               round(sum(a["assign_in"] - a["assign_out"] for a in accs), 2), "", "", "", ""])

    ws2 = wb.create_sheet("02 每日流水")
    ws2.append(["日期", "账号", "快车(¥)", "全站营销(¥)", "京东联盟(¥)", "其他(¥)",
                "当日消费合计(¥)", "当日充值(¥)", "分配净额(¥)", "结算后结余(¥)"])
    for i, w in enumerate([12, 12, 12, 14, 14, 11, 16, 13, 13, 15], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    for r in daily:
        types = r["types"] or {}
        known = sum(v for k, v in types.items() if k in TYPE_ORDER)
        ws2.append([r["date"], r["label"], types.get("快车扣费") or None,
                    types.get("全站营销扣费") or None, types.get("京东联盟扣费") or None,
                    round(sum(types.values()) - known, 2) or None,
                    r["consume"] or None, r["recharge"] or None,
                    r["assign"] or None, r["balance"]])

    ws3 = wb.create_sheet("03 明细流水")
    ws3.append(["记账时间", "交易日期", "账号", "账户ID", "类型", "方向", "金额(¥)",
                "记账后结余(¥)", "单号", "资金账户", "备注"])
    for i, w in enumerate([20, 12, 12, 14, 16, 8, 13, 15, 16, 26, 26], 1):
        ws3.column_dimensions[get_column_letter(i)].width = w
    rows = [e for a in accs for e in a["events"]]
    rows.sort(key=lambda e: (e["book_time"] or e["date"]), reverse=True)
    for e in rows:
        ws3.append([e["book_time"], e["date"], e["label"], e["account_id"], e["type"],
                    e["dir"], e["amount"], e["balance"], str(e["no"] or ""),
                    e["fund"], e["remark"]])

    for w in (ws, ws2, ws3):
        for c in w[1]:
            c.fill = HEAD_FILL
            c.font = HEAD_FONT
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        w.freeze_panes = "A2"
        w.row_dimensions[1].height = 26
        for row in w.iter_rows(min_row=2):
            for c in row:
                c.border = BORDER
                if c.column >= 3 and isinstance(c.value, (int, float)):
                    c.number_format = "#,##0.00"

    # 04 口径说明
    ws4 = wb.create_sheet("04 口径说明")
    ws4.column_dimensions["A"].width = 22
    ws4.column_dimensions["B"].width = 110

    def note(k, v):
        ws4.append([k, v])

    ws4.append(["项目", "说明"])
    note("数据区间", rng)
    note("数据来源", "京准通 账户中心 → 财务记录（https://jzt.jd.com/account/#/financialRecord）")
    note("余额口径", "subaccount/allbalance/get 的 cashBalance（现金可用余额）")
    note("消费", "cashconsume/list；creatTime = 消耗日，settleTime = 次日 08:45 左右批量扣款")
    note("结余", "balanceAmount 原值；接口不返回结余的账号按「期初+充值-消费」滚动推算")
    note("分配", "「商家账户 ↔ 投放子账户」内部划转，不改变账户现金总额，不进余额滚动")
    note("期初余额", "= 末笔已结算结余 + 区间消费 - 区间充值 反推")
    note("接口", "POST https://atoms-api.jd.com/financecore/ + allbalance/get、"
                "cashrecharge/list、cashconsume/list、cashassign/list、freeze/list、sumCost/query")
    for c in ws4[1]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
    for row in ws4.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    # 05 充值 vs 消费 柱状图（上下两张拼一张，Excel 原生不支持双刻度共用 0 轴）
    ws5 = wb.create_sheet("05 充值vs消费")
    dates = date_list(begin, end)
    names = [a for a in accs if a["consume_sum"] or a["recharge_sum"]]
    mat = {(r["date"], r["account"]): r for r in daily}
    ws5.append(["日期"] + [f"{a['label']}-充值" for a in names]
               + [f"{a['label']}-消费" for a in names] + ["合计-充值", "合计-消费"])
    ws5.column_dimensions["A"].width = 10
    for i in range(2, len(names) * 2 + 3):
        ws5.column_dimensions[get_column_letter(i)].width = 13
    for d in dates:
        row = [d[5:]]
        tr = tc = 0.0
        for a in names:
            r = mat.get((d, a["account"])) or {}
            tr += r.get("recharge") or 0
            tc += r.get("consume") or 0
            row.append(round(r.get("recharge") or 0, 2) or None)
        for a in names:
            r = mat.get((d, a["account"])) or {}
            row.append(-round(r.get("consume") or 0, 2) or None)
        row += [round(tr, 2) or None, -round(tc, 2) or None]
        ws5.append(row)
    last = 1 + len(dates)

    def col_max(col: int) -> float:
        return max((abs(float(ws5.cell(row=i, column=col).value or 0))
                    for i in range(2, last + 1)), default=0.0)

    def add_half(title, col, anchor, negative, show_cat, height=3.45):
        ch = BarChart()
        ch.type = "col"
        ch.grouping = "clustered"
        ch.gapWidth = 45
        ch.title = title
        ch.height = height
        ch.width = 15.2
        ch.legend = None
        ch.add_data(Reference(ws5, min_col=col, min_row=1, max_row=last), titles_from_data=True)
        ch.set_categories(Reference(ws5, min_col=1, min_row=2, max_row=last))
        ser = ch.series[0]
        ser.graphicalProperties = GraphicalProperties(solidFill=RED if negative else GREEN)
        ser.graphicalProperties.line.noFill = True
        m = (col_max(col) or 1.0) * 1.15
        ch.y_axis.scaling.min = -m if negative else 0
        ch.y_axis.scaling.max = 0 if negative else m
        ch.y_axis.numFmt = "#,##0.0,"        # 千元，上下刻度宽度一致
        ch.y_axis.majorGridlines = None
        ch.y_axis.delete = False
        ch.x_axis.delete = False
        if not show_cat:
            ch.x_axis.tickLblPos = "none"    # 只藏标签不删轴，两块才能对齐
        ch.x_axis.tickLblSkip = 2
        ch.x_axis.tickMarkSkip = 2
        ws5.add_chart(ch, anchor)

    groups = [("全账号合计", 2 + len(names) * 2, 2 + len(names) * 2 + 1)]
    groups += [(a["label"], 2 + i, 2 + len(names) + i) for i, a in enumerate(names)]
    row0 = last + 3
    for g, (label, col_rec, col_con) in enumerate(groups):
        top = row0 + g * 16
        cell = ws5.cell(row=top, column=1,
                        value=f"{label}　充值（绿·向上） / 消费（红·向下）　单位：千元　{rng}")
        cell.font = Font(bold=True, size=10, color="1F4E79")
        for r in range(top + 1, top + 16):
            ws5.row_dimensions[r].height = 14
        if col_max(col_rec) > 0:
            # 上下按 5:9 行分配（≈2.45cm : 4.43cm）：充值一个月只发生几天，
            # 对半分会让上半区大片留白；行高 14pt≈0.494cm，5 行+9 行=14 行正好拼合
            add_half("充值（绿，向上）", col_rec, f"A{top + 1}", False, False, 2.45)
            add_half("消费（红，向下）", col_con, f"A{top + 6}", True, True, 4.43)
        else:
            ws5.cell(row=top + 1, column=1,
                     value="（本期无充值记录）").font = Font(size=9, color="888888")
            add_half("消费（红，向下）　本期无充值", col_con, f"A{top + 1}", True, True, 7.4)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================ 后台抓取任务
class Runner:
    """财务流水抓取（单线程串行，逐个账号开一次浏览器）。

    profile_lock 由调用方注入（webapp 的 LoginSession._profile_lock），
    与扫码登录 / 保活 / 其它抓取任务互斥，避免两个 Chromium 抢同一个 Profile。
    """

    def __init__(self, profile_lock: threading.Lock | None = None):
        self._lock = threading.Lock()
        self._profile_lock = profile_lock or threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state = {
            "running": False, "days": 30, "account": None, "total": 0, "done": 0,
            "message": "", "error": None, "startedAt": None, "finishedAt": None,
            "range": None, "failed": [],
        }

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, days: int = 30) -> dict:
        with self._lock:
            if self._state["running"]:
                return {"ok": False, "error": "已经在抓了，等这一轮结束"}
            days = max(1, min(int(days or 30), 120))
            self._state.update({"running": True, "days": days, "done": 0,
                                "total": len(settings.ACCOUNTS), "account": None,
                                "message": "排队等待浏览器登录态…", "error": None,
                                "startedAt": settings.now_iso(), "finishedAt": None,
                                "failed": []})
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(days,),
                                        name="cashflow-run", daemon=True)
        self._thread.start()
        return {"ok": True, "days": days}

    def _run(self, days: int) -> None:
        begin = (settings.today() - timedelta(days=days - 1)).isoformat()
        end = settings.today().isoformat()
        keys = [a["key"] for a in settings.ACCOUNTS]
        try:
            if not self._profile_lock.acquire(timeout=300):
                raise RuntimeError("浏览器登录态被其它任务占用（登录/保活/抓取），稍后再试")
            try:
                for i, key in enumerate(keys):
                    if self._cancel.is_set():
                        break
                    label = settings.get_account(key).get("label") or key
                    with self._lock:
                        self._state.update({"account": label,
                                            "message": f"正在抓「{label}」的充值/消费/分配流水…"})
                    try:
                        data = fetch_account(key, begin, end)
                        save(data)
                    except Exception as e:  # noqa: BLE001
                        with self._lock:
                            self._state["failed"].append(f"{label}：{type(e).__name__}: {e}")
                    with self._lock:
                        self._state["done"] = i + 1
                    if i < len(keys) - 1:
                        time.sleep(6 + random.random() * 4)
            finally:
                self._profile_lock.release()
            with self._lock:
                failed = self._state["failed"]
                self._state.update({
                    "running": False, "account": None, "range": [begin, end],
                    "finishedAt": settings.now_iso(),
                    "message": ("抓取完成" if not failed else
                                f"完成，但有 {len(failed)} 个账号失败"),
                })
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._state.update({"running": False, "error": f"{type(e).__name__}: {e}",
                                    "message": "抓取失败", "finishedAt": settings.now_iso()})


_runner: Runner | None = None


def runner(profile_lock: threading.Lock | None = None) -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner(profile_lock=profile_lock)
    return _runner
