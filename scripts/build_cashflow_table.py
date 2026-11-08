# -*- coding: utf-8 -*-
"""把 fetch_cashflow.py 抓到的资金流水 JSON 汇总成「账号广告费流水表」。

用法（容器内，fetch_cashflow 跑完之后）：
    python /tmp/build_cashflow_table.py --src=/data/cashflow --out=/data/cashflow/out

产出：
    <out>/账号广告费流水表_<begin>_<end>.xlsx   （5 个 sheet，含充值/消费柱状图）
    <out>/账号广告费流水表_<begin>_<end>.md     （人看的摘要）

口径（与京准通页面一致）：
    * 交易日期 = creatTime（广告实际消耗的自然日）
    * 记账时间 = settleTime（次日 08:45 左右批量扣款的时点）
    * 结余     = 该笔记账后的账户现金余额（接口 balanceAmount 原值）
    * 期初余额 = 最后一笔已结算结余 + 区间消费合计 - 区间充值合计
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ORDER = ["main", "b", "acct1", "acct2", "acct3"]
TYPE_ORDER = ["快车扣费", "全站营销扣费", "京东联盟扣费"]
GREEN = "2E9E5B"      # 充值（向上）
RED = "D64545"        # 消费（向下）

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


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

    子账户直接投放的账号（b / acct2）消费记录里 settleTime 与 balanceAmount 都是 null，
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


def _arg(argv: list[str], name: str, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def load_accounts(src: Path) -> list[dict]:
    items = []
    for p in src.glob("*.json"):
        if p.name in ("report.json",):
            continue
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    idx = {k: i for i, k in enumerate(ORDER)}
    items.sort(key=lambda x: (idx.get(x.get("account"), 99), x.get("account") or ""))
    return items


def build(data: dict) -> dict:
    """单账号：标准化成事件列表 + 汇总数字。"""
    acc = data.get("account")
    label = data.get("label") or acc
    aid = data.get("account_id") or 0
    bal = data.get("balance") or {}
    cash = _f(bal.get("cashBalance"))
    award = _f(bal.get("awardBalance"))
    commission = _f(bal.get("commissionBalance"))

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
            "amount": _f(a.get("amount")), "balance": None,
            "no": a.get("pk"),
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

    # 期初余额：最后一笔"有结余"的已结算记录往回推。
    # b / acct2 这类子账户直接投放的账号，接口不给 balanceAmount，退回用当前余额推
    # （代价是把"当日尚未批量扣款"的那部分算进期初，误差已在口径页说明）。
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
    commission_cost = _f(details[0].get("commissionCost") if details else 0)
    total_cost = _f((details[0].get("totalCost") if details else 0) or cash_cost)

    days = sorted({e["date"] for e in events if e["kind"] == "消费" and e["date"]})
    day_avg = round(consume_sum / len(days), 2) if days else 0.0
    last_day = days[-1] if days else ""

    return {
        "account": acc, "label": label, "account_id": aid,
        "range": data.get("range") or {},
        "cash_balance": cash, "award_balance": award, "commission_balance": commission,
        "begin_balance": begin_bal,
        "settled_balance": round(last_bal, 2) if last_bal is not None else None,
        "begin_from": begin_from,
        "recharge_sum": recharge_sum, "consume_sum": consume_sum,
        "cash_cost": cash_cost, "award_cost": award_cost,
        "commission_cost": commission_cost, "total_cost": total_cost,
        "assign_in": assign_in, "assign_out": assign_out,
        "active_days": len(days), "day_avg": day_avg, "last_cost_day": last_day,
        "cover_days": round(cash / day_avg, 1) if day_avg else None,
        "events": events,
    }


def daily_rows(accs: list[dict], begin: str, end: str) -> list[dict]:
    """日期 × 账号 的每日流水（消耗按交易日期归属）。"""
    dates = []
    d0 = datetime.strptime(begin, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    while d0 <= d1:
        dates.append(d0.strftime("%Y-%m-%d"))
        d0 = d0.fromtimestamp(d0.timestamp() + 86400)

    out = []
    for a in accs:
        if not a["consume_sum"] and not a["recharge_sum"]:
            continue                              # 无投放的账号不占每日表
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
        # 滚动余额：接口给了结余就用接口值（权威），没给就按 期初+充值-消费 推算。
        # 分配不计入：它只是「商家账户 ↔ 投放子账户」的内部划转，不改变账户现金总额。
        rolling = a["begin_balance"]
        for day in dates:
            rec = byday.get(day)
            cost = round(sum((rec["types"] or {}).values()), 2) if rec else 0.0
            recharge = round(rec["recharge"], 2) if rec else 0.0
            assign = round(rec["assign"], 2) if rec else 0.0
            rolling = round(rolling + recharge - cost, 2)
            bal = rec["balance"] if rec and rec["balance"] is not None else rolling
            if rec and rec["balance"] is not None:
                rolling = rec["balance"]          # 以接口为准，顺便纠掉推算误差
            out.append({"date": day, "account": a["account"], "label": a["label"],
                        "types": dict(rec["types"]) if rec else {}, "consume": cost,
                        "recharge": recharge, "assign": assign, "balance": bal})
    return out


def _sheet(wb, title: str, head: list[str], widths: list[int]):
    ws = wb.create_sheet(title)
    ws.append(head)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 28
    return ws


def _fmt(ws, money_cols: list[int], start: int = 2):
    for row in ws.iter_rows(min_row=start):
        for c in row:
            c.border = BORDER
            if c.column in money_cols:
                c.number_format = "#,##0.00"
                c.alignment = Alignment(horizontal="right")


def write_chart_sheet(wb, accs: list[dict], daily: list[dict], rng: str) -> None:
    """充值 / 消费柱状图：上绿=充值、下红=消费，上下各自独立刻度。

    对齐参考图的形式（京准通「收支流水」那张）：按日排列、绿上红下、中间是 0 轴。
    关键在于**上下两块各用自己的刻度**——充值单日动辄 2~7 万，日消费才 1 万，
    放在同一刻度里红柱会被压成一条线，看不出波动。
    """
    names = [a for a in accs if a["consume_sum"] or a["recharge_sum"]]
    dates = sorted({r["date"] for r in daily})
    if not dates:
        return
    ws = wb.create_sheet("05 充值vs消费")
    mat = {(r["date"], r["account"]): r for r in daily}

    head = (["日期"] + [f"{a['label']}-充值" for a in names]
            + [f"{a['label']}-消费" for a in names] + ["合计-充值", "合计-消费"])
    ws.append(head)
    for c in ws[1]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    ws.column_dimensions["A"].width = 10
    for i in range(2, len(head) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 13

    for d in dates:
        row = [d[5:]]                                   # 10-10，轴上不必带年份
        tr = tc = 0.0
        for a in names:
            r = mat.get((d, a["account"])) or {}
            tr += r.get("recharge") or 0
            tc += r.get("consume") or 0
            row.append(round(r.get("recharge") or 0, 2) or None)
        for a in names:
            r = mat.get((d, a["account"])) or {}
            row.append(-round(r.get("consume") or 0, 2) or None)   # 负数=向下
        row += [round(tr, 2) or None, -round(tc, 2) or None]
        ws.append(row)
    last = 1 + len(dates)

    def col_max(col: int) -> float:
        return max((abs(float(ws.cell(row=i, column=col).value or 0))
                    for i in range(2, last + 1)), default=0.0)

    CHART_W = 15.2
    CHART_H = 3.45          # 7 行 × 14pt(0.494cm) ≈ 3.46cm，上下两张正好拼成一张
    GROUP_ROWS = 16

    def add_half(title: str, col: int, anchor: str, negative: bool,
                 show_cat: bool, height: float = CHART_H) -> None:
        ch = BarChart()
        ch.type = "col"
        ch.grouping = "clustered"
        ch.gapWidth = 45
        ch.title = title
        ch.height = height
        ch.width = CHART_W
        ch.legend = None
        ch.add_data(Reference(ws, min_col=col, min_row=1, max_row=last),
                    titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last))
        ser = ch.series[0]
        ser.graphicalProperties = GraphicalProperties(solidFill=RED if negative else GREEN)
        ser.graphicalProperties.line.noFill = True
        m = (col_max(col) or 1.0) * 1.15
        if negative:
            ch.y_axis.scaling.min = -m
            ch.y_axis.scaling.max = 0
        else:
            ch.y_axis.scaling.min = 0
            ch.y_axis.scaling.max = m
        # 千元单位：上下两块的刻度宽度一致，x 轴才能对得齐
        ch.y_axis.numFmt = "#,##0.0,"
        ch.y_axis.majorGridlines = None
        ch.y_axis.delete = False
        # 上半图只藏标签、不删轴：删掉整条 x 轴会让绘图区变宽，上下两块就对不齐了
        ch.x_axis.delete = False
        if not show_cat:
            ch.x_axis.tickLblPos = "none"
        ch.x_axis.tickLblSkip = 2
        ch.x_axis.tickMarkSkip = 2
        ws.add_chart(ch, anchor)

    groups = [("全账号合计", 2 + len(names) * 2, 2 + len(names) * 2 + 1)]
    groups += [(a["label"], 2 + i, 2 + len(names) + i) for i, a in enumerate(names)]
    row0 = last + 3
    for g, (label, col_rec, col_con) in enumerate(groups):
        top = row0 + g * GROUP_ROWS
        has_rec = col_max(col_rec) > 0
        cell = ws.cell(row=top, column=1,
                       value=f"{label}　充值（绿·向上） / 消费（红·向下）　单位：千元　{rng}")
        cell.font = Font(bold=True, size=10, color="1F4E79")
        for r in range(top + 1, top + GROUP_ROWS):
            ws.row_dimensions[r].height = 14
        if has_rec:
            add_half("充值（绿，向上）", col_rec, f"A{top + 1}", False, False)
            add_half("消费（红，向下）", col_con, f"A{top + 8}", True, True)
        else:
            # 本期一笔充值都没有（全靠内部划转的账号）：只画消费，占满整组
            ws.cell(row=top + 1, column=1, value="（本期无充值记录）").font = \
                Font(size=9, color="888888")
            add_half("消费（红，向下）　本期无充值", col_con, f"A{top + 1}", True, True,
                     height=CHART_H * 2 + 0.5)


def write_xlsx(accs: list[dict], daily: list[dict], path: Path) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    rng = f"{accs[0]['range'].get('begin', '')} ~ {accs[0]['range'].get('end', '')}"

    # ---- 01 账号汇总 ----
    head = ["账号", "账户ID", "当前可用余额(¥)", "区间期初余额(¥)", "区间充值(¥)",
            "区间消费(¥)", "其中现金(¥)", "其中红包(¥)", "分配净额(¥)",
            "有消耗天数", "日均消费(¥)", "余额可撑(天)", "口径说明"]
    ws = _sheet(wb, "01 账号汇总", head, [12, 14, 16, 18, 14, 14, 13, 12, 13, 11, 13, 11, 46])
    for a in accs:
        ws.append([a["label"], a["account_id"], a["cash_balance"], a["begin_balance"],
                   a["recharge_sum"], a["consume_sum"], a["cash_cost"], a["award_cost"],
                   round(a["assign_in"] - a["assign_out"], 2), a["active_days"],
                   a["day_avg"] or None, a["cover_days"],
                   f"{rng}｜期初{a['begin_from']}"])
    tot = ["合计", "", round(sum(a["cash_balance"] for a in accs), 2),
           round(sum(a["begin_balance"] for a in accs), 2),
           round(sum(a["recharge_sum"] for a in accs), 2),
           round(sum(a["consume_sum"] for a in accs), 2),
           round(sum(a["cash_cost"] for a in accs), 2),
           round(sum(a["award_cost"] for a in accs), 2),
           round(sum(a["assign_in"] - a["assign_out"] for a in accs), 2), "", "", "", ""]
    ws.append(tot)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    _fmt(ws, [3, 4, 5, 6, 7, 8, 9, 11])

    # ---- 02 每日流水 ----
    head = ["日期", "账号", "快车(¥)", "全站营销(¥)", "京东联盟(¥)", "其他(¥)",
            "当日消费合计(¥)", "当日充值(¥)", "分配净额(¥)", "结算后结余(¥)"]
    ws = _sheet(wb, "02 每日流水", head, [12, 12, 12, 14, 14, 11, 16, 13, 13, 15])
    for r in daily:
        types = r["types"] or {}
        known = sum(v for k, v in types.items() if k in TYPE_ORDER)
        ws.append([r["date"], r["label"], types.get("快车扣费") or None,
                   types.get("全站营销扣费") or None, types.get("京东联盟扣费") or None,
                   round(sum(types.values()) - known, 2) or None,
                   r["consume"] or None, r["recharge"] or None,
                   r["assign"] or None, r["balance"]])
    _fmt(ws, [3, 4, 5, 6, 7, 8, 9, 10])

    # ---- 03 明细流水 ----
    head = ["记账时间", "交易日期", "账号", "账户ID", "类型", "方向", "金额(¥)",
            "记账后结余(¥)", "单号", "资金账户", "备注"]
    ws = _sheet(wb, "03 明细流水", head, [20, 12, 12, 14, 16, 8, 13, 15, 16, 26, 26])
    rows = [e for a in accs for e in a["events"]]
    rows.sort(key=lambda e: (e["book_time"] or e["date"]), reverse=True)
    for e in rows:
        ws.append([e["book_time"], e["date"], e["label"], e["account_id"], e["type"],
                   e["dir"], e["amount"], e["balance"], str(e["no"] or ""),
                   e["fund"], e["remark"]])
    _fmt(ws, [7, 8])

    # ---- 04 口径说明 ----
    ws = wb.create_sheet("04 口径说明")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 110
    notes = [
        ("数据区间", rng),
        ("数据来源", "京准通 账户中心 → 财务记录（https://jzt.jd.com/account/#/financialRecord）"),
        ("余额口径", "subaccount/allbalance/get 的 cashBalance（现金可用余额）；红包/返佣余额另计"),
        ("充值", "现金充值的「到账时间」；amountall/get 给区间充值合计，可对账"),
        ("消费", "cashconsume/list；creatTime = 广告消耗日，settleTime = 次日 08:45 左右批量扣款"),
        ("交易日期", "广告实际消耗的自然日（报表口径，与「推广报表」的日期一致）"),
        ("记账时间", "实际扣款的时点，通常为交易日期次日 08:45~08:55"),
        ("结余", "detail 页的 balanceAmount 原值；b/acct2 这类子账户直接投放的账号接口不给结余，"
                 "按「期初 + 充值 - 消费」逐日滚动推算（与实时余额可对上）"),
        ("分配", "同一资金户内「商家账户 ↔ 投放子账户」的划转，不改变账户现金总额，故不计入余额滚动"),
        ("冻结", "货款账扣等冻结/解冻记录，仅列明细；金额为 0 的占位行已忽略"),
        ("期初余额", "= 最后一笔已结算结余 + 区间消费合计 - 区间充值合计（与首笔充值前后的结余互相印证）"),
        ("当前可用余额", "抓取时刻的实时余额，比「结算后结余」少了当天尚未批量扣款的消耗"),
        ("红包/返佣", "sumCost/query 的 awardCost / commissionCost；消耗时先扣红包再扣现金"),
        ("账号范围", "各账号用各自的登录 Profile 分别抓取，互不串号；无投放的账号流水为空"),
        ("接口清单", "POST https://atoms-api.jd.com/financecore/ + "
                   "subaccount/allbalance/get、account/cashrecharge/list、"
                   "subaccount/cashconsume/list、account/cashassign/list、"
                   "subaccount/freeze/list、subaccount/consumer/sumCost/query"),
    ]
    ws.append(["项目", "说明"])
    for c in ws[1]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
        c.alignment = Alignment(horizontal="center")
    for k, v in notes:
        ws.append([k, v])
    for row in ws.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    # ---- 05 充值 vs 消费 柱状图 ----
    write_chart_sheet(wb, accs, daily, rng)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_md(accs: list[dict], daily: list[dict], path: Path) -> None:
    rng = f"{accs[0]['range'].get('begin', '')} ~ {accs[0]['range'].get('end', '')}"
    L = [f"# 账号广告费流水表（{rng}）", "",
         "> 充值 / 消费柱状图见同名 `.html`（浏览器打开，绿上红下、上下独立刻度），"
         "Excel 里在「05 充值vs消费」页。", "",
         "## 一、账号汇总", "",
         "| 账号 | 账户ID | 当前余额 | 期初余额 | 充值 | 消费 | 现金/红包 | 日均消费 | 可撑天数 |",
         "|---|---|---:|---:|---:|---:|---|---:|---:|"]
    for a in accs:
        L.append(f"| {a['label']} | {a['account_id']} | {a['cash_balance']:,.2f} | "
                 f"{a['begin_balance']:,.2f} | {a['recharge_sum']:,.2f} | "
                 f"{a['consume_sum']:,.2f} | {a['cash_cost']:,.2f}/{a['award_cost']:,.2f} | "
                 f"{a['day_avg']:,.2f} | {a['cover_days'] if a['cover_days'] is not None else '—'} |")
    L.append(f"| **合计** | | **{sum(a['cash_balance'] for a in accs):,.2f}** | "
             f"**{sum(a['begin_balance'] for a in accs):,.2f}** | "
             f"**{sum(a['recharge_sum'] for a in accs):,.2f}** | "
             f"**{sum(a['consume_sum'] for a in accs):,.2f}** | | | |")
    L += ["", "## 二、每日消费（按账号）", "",
          "| 日期 | " + " | ".join(a["label"] for a in accs) + " | 合计 |",
          "|---|" + "---:|" * (len(accs) + 1)]
    byday: dict[str, dict[str, float]] = defaultdict(dict)
    for r in daily:
        if r["consume"]:
            byday[r["date"]][r["account"]] = r["consume"]
    for day in sorted(byday):
        vals = [byday[day].get(a["account"], 0) for a in accs]
        L.append(f"| {day} | " + " | ".join(f"{v:,.2f}" if v else "—" for v in vals) +
                 f" | {sum(vals):,.2f} |")
    L += ["", "## 三、口径", "",
          "- 交易日期 = 广告消耗日；记账时间 = 次日 08:45 左右批量扣款",
          "- 结余取接口 balanceAmount 原值；期初余额 = 末笔结余 + 消费 - 充值 反推",
          "- 分配（商家账户↔投放子账户）不改变账户现金总额，单列不计入余额滚动",
          ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")


def write_html_chart(accs: list[dict], daily: list[dict], rng: str, path: Path) -> None:
    """生成参考图同款的柱状图（SVG，浏览器直接打开）：上绿=充值、下红=消费、各自刻度。

    为什么另出一份 HTML：Excel 原生图表画不出「上下两块独立刻度」，而这张图的重点
    恰恰是两条量级差 6 倍的曲线各自可读。SVG 版是像素级可控的，也方便截图贴群。
    """
    names = [a for a in accs if a["consume_sum"] or a["recharge_sum"]]
    dates = sorted({r["date"] for r in daily})
    if not dates:
        return
    mat = {(r["date"], r["account"]): r for r in daily}
    groups = [("全账号合计", None)] + [(a["label"], a["account"]) for a in names]

    W, PAD_L, PAD_R, HALF, TOP, BOT = 1400, 78, 22, 120, 26, 34
    H = TOP + HALF * 2 + BOT
    plot_w = W - PAD_L - PAD_R
    n = len(dates)
    step = plot_w / max(n, 1)
    bar_w = max(6.0, min(24.0, step * 0.55))
    y0 = TOP + HALF

    def fmt(v: float) -> str:
        return f"{v / 10000:.1f}万" if abs(v) >= 10000 else f"{v:,.0f}"

    def series_of(key):
        rec, con = [], []
        for d in dates:
            if key is None:
                rows = [mat.get((d, a["account"])) or {} for a in names]
                rec.append(sum(r.get("recharge") or 0 for r in rows))
                con.append(sum(r.get("consume") or 0 for r in rows))
            else:
                r = mat.get((d, key)) or {}
                rec.append(r.get("recharge") or 0)
                con.append(r.get("consume") or 0)
        return rec, con

    blocks = []
    for label, key in groups:
        rec, con = series_of(key)
        mr = max(rec) or 1.0
        mc = max(con) or 1.0
        p = [f'<div class="card"><div class="hd"><b>{label}</b>'
             f'<span class="lg"><i class="g"></i>充值（向上）<i class="r"></i>消费（向下）'
             f'　单位：元</span></div>',
             f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" '
             f'preserveAspectRatio="none">']
        has_rec = mr > 0 and max(rec) > 0
        if has_rec:
            for frac in (0.0, 0.5, 1.0):
                y = y0 - HALF * frac
                p.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                         f'stroke="#eef0f2"/>')
                p.append(f'<text x="{PAD_L - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
                         f'class="ax">{fmt(mr * frac)}</text>')
        else:
            p.append(f'<text x="{PAD_L + plot_w / 2:.1f}" y="{TOP + HALF / 2:.1f}" '
                     f'text-anchor="middle" class="empty">本期无充值记录（余额由内部划转补入）</text>')
        for frac in (0.0, 0.5, 1.0):
            y2 = y0 + HALF * frac
            p.append(f'<line x1="{PAD_L}" y1="{y2:.1f}" x2="{W - PAD_R}" y2="{y2:.1f}" '
                     f'stroke="#eef0f2"/>')
            if frac:
                p.append(f'<text x="{PAD_L - 8}" y="{y2 + 3.5:.1f}" text-anchor="end" '
                         f'class="ax">{fmt(mc * frac)}</text>')
        p.append(f'<line x1="{PAD_L}" y1="{y0}" x2="{W - PAD_R}" y2="{y0}" stroke="#c9ced6"/>')
        for i, d in enumerate(dates):
            x = PAD_L + step * i + (step - bar_w) / 2
            v = rec[i]
            if v:
                h = max(1.5, v / mr * (HALF - 10))
                p.append(f'<rect x="{x:.1f}" y="{y0 - h:.1f}" width="{bar_w:.1f}" '
                         f'height="{h:.1f}" fill="#{GREEN}" rx="1.5">'
                         f'<title>{d} 充值 {v:,.2f}</title></rect>')
                if h >= 34:
                    p.append(f'<text x="{x + bar_w / 2:.1f}" y="{y0 - h - 4:.1f}" '
                             f'text-anchor="middle" class="v gv">{fmt(v)}</text>')
            v = con[i]
            if v:
                h = max(1.5, v / mc * (HALF - 10))
                p.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bar_w:.1f}" '
                         f'height="{h:.1f}" fill="#{RED}" rx="1.5">'
                         f'<title>{d} 消费 {v:,.2f}</title></rect>')
                if h >= 34:
                    p.append(f'<text x="{x + bar_w / 2:.1f}" y="{y0 + h + 11:.1f}" '
                             f'text-anchor="middle" class="v rv">{fmt(v)}</text>')
        for i, d in enumerate(dates):
            if i % 2:
                continue
            x = PAD_L + step * i + step / 2
            p.append(f'<text x="{x:.1f}" y="{y0 + HALF + 18:.1f}" text-anchor="middle" '
                     f'class="ax">{d[5:]}</text>')
        p.append("</svg></div>")
        blocks.append("".join(p))

    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>账号广告费流水图 {rng}</title><style>
body{{background:#f5f6f8;margin:0;padding:20px;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#222}}
h1{{font-size:18px;margin:0 0 4px}}
.sub{{color:#888;font-size:12px;margin-bottom:14px}}
.card{{background:#fff;border-radius:8px;padding:10px 14px 6px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
.hd{{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;font-size:13px}}
.lg{{color:#888;font-size:12px}}
.lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:-1px}}
.g{{background:#{GREEN}}}.r{{background:#{RED}}}
text.ax{{font-size:11px;fill:#9aa0a6}}
text.v{{font-size:9.5px}}
text.gv{{fill:#{GREEN}}}text.rv{{fill:#{RED}}}
.note{{color:#888;font-size:12px;line-height:1.7}}
text.empty{{fill:#b9bec6;font-size:12px}}
</style></head><body>
<h1>账号广告费流水图（{rng}）</h1>
<div class="sub">绿色向上 = 当日充值到账，红色向下 = 当日广告消耗；<b>上下两块各自独立刻度</b>，
所以两向柱高不可直接比（左侧刻度分别是各自的满格值）。鼠标悬停柱子看明细。</div>
{"".join(blocks)}
<div class="note">口径：充值按到账日、消费按广告消耗日；次日 08:45 左右批量扣款。
数据来源：京准通 → 账户中心 → 财务记录。</div>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main() -> int:
    src = Path(_arg(sys.argv[1:], "src", "/data/cashflow"))
    out = Path(_arg(sys.argv[1:], "out", str(src / "out")))
    accs = [build(d) for d in load_accounts(src)]
    accs = [a for a in accs if a["events"] or a["cash_balance"] or a["account"]]
    if not accs:
        print("没有可用数据，请先跑 fetch_cashflow.py")
        return 1
    rng = accs[0]["range"]
    stem = f"账号广告费流水表_{rng.get('begin', '')}_{rng.get('end', '')}"
    daily = daily_rows(accs, rng.get("begin"), rng.get("end"))
    write_xlsx(accs, daily, out / f"{stem}.xlsx")
    write_md(accs, daily, out / f"{stem}.md")
    write_html_chart(accs, daily, f"{rng.get('begin', '')} ~ {rng.get('end', '')}",
                     out / f"{stem}.html")
    print(f"[OK] {out / (stem + '.xlsx')}")
    print(f"[OK] {out / (stem + '.md')}")
    print(f"[OK] {out / (stem + '.html')}")
    for a in accs:
        print(f"  {a['label']:<8} 余额 {a['cash_balance']:>10,.2f} 期初 {a['begin_balance']:>10,.2f} "
              f"充值 {a['recharge_sum']:>10,.2f} 消费 {a['consume_sum']:>10,.2f} "
              f"日均 {a['day_avg']:>8,.2f} 可撑 {a['cover_days']} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
