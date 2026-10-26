# -*- coding: utf-8 -*-
"""月度利润分析：按 SKU 把「销量 → 到手金额 → 毛利 → 净利」串起来。

口径（每一环都能追溯到具体接口/文件）：
    成交件数     商智「商品明细」 jdsz.jd.com/szweb/view/product/productDetail.html
                 （szgateway /lowcode/productDetail/table/productTable.ajax，指标 SZ_QTY）
    到手结算价   ERP 成本接口 settings.load_costs()["skus"][skuId]["supply"]
                 = 京东结算给我们的每件金额（已扣点，见《SKU成本接口对接说明.md》）
    到手金额     = 成交件数 × 到手结算价
    毛利/件      analyze2.sku_cost_of() 的 grossPerOrder
                 （= 到手结算价 − 货款 − 运费 − 包材 − 人工，未扣广告费）
    毛利         = 成交件数 × 毛利/件
    CPS 花费     data/reports/paycomm_<账号>_<区间>.json → rows[].payCommAmount
    京准通花费   data/reports/paycomm_spend_<账号>_<区间>.json → rows[].jztCost
    净利         = 毛利 − CPS 花费 − 京准通花费

花费优先读 paycomm 的本地缓存（精确命中或多段拼接）；缓存缺失时，在同一个浏览器
会话里自动补抓该账号该区间并写入缓存，用户不需要再去「花费一览」页手动抓。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import threading
import time
from pathlib import Path

from . import analyze2, catalog, indicators as ind, paycomm, scrape_sz, settings
from .browser import launch_persistent, session_ok

BOARD_PAGE = "https://jzt.jd.com/jtk/#/data-board"

# (字段, 列名, 类型)；ratio 类在按品类汇总时不求和
FIELDS = [
    ("qty", "成交件数", "int"),
    ("frontPrice", "前台件单价(元)", "money"),
    ("supply", "到手结算价(元)", "money"),
    ("arrival", "到手金额(元)", "money"),
    ("grossPerOrder", "毛利/件(元)", "money"),
    ("gross", "毛利(元)", "money"),
    ("cpsCost", "CPS花费(元)", "money"),
    ("jztCost", "京准通花费(元)", "money"),
    ("net", "净利(元)", "money"),
    ("netRate", "净利率(%)", "num"),
]
# 按品类汇总时只加「可加的」：单价类（前台件单价/到手结算价/毛利每件）与比率不能求和
RATIO_KEYS = {"frontPrice", "supply", "grossPerOrder", "netRate"}
SUM_KEYS = ["qty", "arrival", "gross", "cpsCost", "jztCost", "net"]


def norm_range(start: str, end: str) -> tuple[str, str]:
    try:
        s = dt.date.fromisoformat(str(start).strip())
        e = dt.date.fromisoformat(str(end).strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"日期格式不对（要 YYYY-MM-DD）：{exc}") from exc
    if s > e:
        s, e = e, s
    if (e - s).days > 366:
        raise ValueError("区间最长 366 天")
    # 商智只到昨天：endDate >= 今天会静默返回空
    yesterday = settings.today() - dt.timedelta(days=1)
    if e > yesterday:
        e = yesterday
    if s > e:
        raise ValueError("区间内没有可查询的日期（商智数据只到昨天）")
    return s.isoformat(), e.isoformat()


# ================================================================ 取数
def fetch_sz_rows(page, start: str, end: str) -> list[dict]:
    """商智商品明细（一次区间请求），返回原始行（含 $summary 汇总行，调用方过滤）。"""
    sz = scrape_sz.SzSession(page, "product")
    sz.capture()
    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    cs = (dt.date.fromisoformat(start) - dt.timedelta(days=days)).isoformat()
    ce = (dt.date.fromisoformat(start) - dt.timedelta(days=1)).isoformat()
    d = sz.post(scrape_sz.URL_PRODUCT, scrape_sz._base(
        start, end, cs, ce, sz.brands, sz.cates,
        {"proType": "sku", "indicators": scrape_sz.INDICATORS, "onlyAttention": False}))
    code = (d.get("header") or {}).get("code")
    if code != 0:
        raise RuntimeError(f"商智返回异常 code={code}: {json.dumps(d, ensure_ascii=False)[:200]}")
    return (d.get("body") or {}).get("data") or []


def _cost_map(cps_data: dict | None, jzt_data: dict | None) -> tuple[dict, dict]:
    def _one(data, key):
        out = {}
        for r in (data or {}).get("rows") or []:
            try:
                sid = int(r.get("skuId") or 0)
            except (TypeError, ValueError):
                continue
            if sid:
                out[sid] = out.get(sid, 0.0) + float(r.get(key) or 0)
        return out

    return _one(cps_data, "payCommAmount"), _one(jzt_data, "jztCost")


def collect(sz_rows: list[dict], cps: dict, jzt: dict,
            names: dict | None = None, accounts: list[str] | None = None) -> dict[int, dict]:
    """把一个账号的商智成交与花费汇总成 {skuId: 原始量}（不做成本/利润计算）。

    单独拆出来是为了多账号按 SKU 合并：同一个 SKU 可能在 A 账号成交、在 B 账号投广告。
    """
    accs = list(accounts or [])
    out: dict[int, dict] = {}

    def _slot(sid: int, name: str = "") -> dict:
        r = out.get(sid)
        if r is None:
            r = {"skuId": int(sid), "itemName": "", "qty": 0, "szAmt": 0.0,
                 "cpsCost": 0.0, "jztCost": 0.0, "accts": [], "spendAccts": []}
            out[int(sid)] = r
        if name and not r["itemName"]:
            r["itemName"] = name
        return r

    for raw in sz_rows:
        sid_raw = raw.get("sku_id")
        if sid_raw in (None, "", "合计") or raw.get("$summary"):
            continue
        try:
            sid = int(sid_raw)
        except (TypeError, ValueError):
            continue
        qty = int(float(raw.get(ind.SZ_QTY) or 0))
        amt = float(raw.get(ind.SZ_AMT) or 0)
        if qty <= 0 and amt <= 0:
            continue
        r = _slot(sid, str(raw.get("name") or ""))
        r["qty"] += qty
        r["szAmt"] += amt
        for a in accs:
            if a not in r["accts"]:
                r["accts"].append(a)

    for sid, cost in (cps or {}).items():
        r = _slot(int(sid), (names or {}).get(int(sid), ""))
        r["cpsCost"] += float(cost or 0)
        for a in accs:
            if a not in r["spendAccts"]:
                r["spendAccts"].append(a)
    for sid, cost in (jzt or {}).items():
        r = _slot(int(sid), (names or {}).get(int(sid), ""))
        r["jztCost"] += float(cost or 0)
        for a in accs:
            if a not in r["spendAccts"]:
                r["spendAccts"].append(a)
    return out


def merge_collected(parts: list[dict]) -> dict[int, dict]:
    """把多个账号的 collect 结果按 SKU 合并（成交与花费都累加）。"""
    out: dict[int, dict] = {}
    for part in parts:
        for sid, r in (part or {}).items():
            tgt = out.get(sid)
            if tgt is None:
                tgt = {"skuId": int(sid), "itemName": "", "qty": 0, "szAmt": 0.0,
                       "cpsCost": 0.0, "jztCost": 0.0, "accts": [], "spendAccts": []}
                out[int(sid)] = tgt
            if r.get("itemName") and not tgt["itemName"]:
                tgt["itemName"] = r["itemName"]
            tgt["qty"] += int(r.get("qty") or 0)
            tgt["szAmt"] += float(r.get("szAmt") or 0)
            tgt["cpsCost"] += float(r.get("cpsCost") or 0)
            tgt["jztCost"] += float(r.get("jztCost") or 0)
            for a in r.get("accts") or []:
                if a not in tgt["accts"]:
                    tgt["accts"].append(a)
            for a in r.get("spendAccts") or []:
                if a not in tgt["spendAccts"]:
                    tgt["spendAccts"].append(a)
    return out


def finalize(collected: dict[int, dict], costs: dict | None = None) -> list[dict]:
    """把合并后的原始量算成利润行（成本、毛利、净利）。"""
    costs = settings.load_costs() if costs is None else costs
    model = analyze2.load_cost_model()
    units = settings.costs_unit_map()
    rows: list[dict] = []
    for sid, c in collected.items():
        qty = int(c.get("qty") or 0)
        amt = float(c.get("szAmt") or 0)
        name = c.get("itemName") or ""
        front = round(amt / qty, 2) if qty else 0.0
        cp = analyze2.sku_cost_of(str(sid), front, model, costs, units)
        supply = float(cp.get("supply") or 0)
        gpo = float(cp.get("grossPerOrder") or 0)
        r = {"skuId": int(sid), "itemName": name, "category": catalog.category_of(name),
             "qty": qty, "szAmt": round(amt, 2), "frontPrice": front,
             "supply": round(supply, 2),
             "arrival": round(qty * supply, 2) if (qty and supply > 0) else None,
             "grossPerOrder": round(gpo, 2), "gross": round(qty * gpo, 2),
             "cpsCost": round(float(c.get("cpsCost") or 0), 2),
             "jztCost": round(float(c.get("jztCost") or 0), 2),
             "costSource": cp.get("costSource") or "",
             "accts": list(c.get("accts") or []),
             "spendAccts": list(c.get("spendAccts") or [])}
        _recalc(r)
        if r["qty"] or r["cpsCost"] or r["jztCost"]:
            rows.append(r)
    rows.sort(key=lambda x: -(x.get("gross") or 0))
    return rows


def build_rows(sz_rows: list[dict], cps: dict, jzt: dict,
               costs: dict | None = None, names: dict | None = None,
               accounts: list[str] | None = None) -> list[dict]:
    """单账号口径：商智成交 + CPS/京准通花费 → 利润行（不做自动分摊）。"""
    got = collect(sz_rows, cps, jzt, names=names, accounts=accounts)
    return finalize(got, costs=costs)



def attribute_spend(parts: dict[str, dict[int, dict]]) -> dict[int, dict]:
    """把各账号的花费按 SKU 归给「真正卖出这个 SKU 的账号」。

    规则（按优先级）：
      1) SKU 在哪个账号有商智成交（件数最多者；件数相同看成交额）→ 该 SKU 的
         CPS / 京准通花费整体归到那个账号；
      2) 所有账号都没有成交（例如全站智能推广的 spu）→ 各自保留自己投的那份，不合并。
    返回 {skuId: {"owner", "cps", "jzt", "byAcc": {acc: {"cps", "jzt"}}}}。
    """
    owners: dict[int, str] = {}
    weights: dict[int, tuple] = {}
    for acc, coll in parts.items():
        for sid, c in coll.items():
            qty = int(c.get("qty") or 0)
            if qty <= 0:
                continue
            w = (qty, float(c.get("szAmt") or 0))
            if sid not in weights or w > weights[sid]:
                weights[sid] = w
                owners[sid] = acc

    out: dict[int, dict] = {}
    for acc, coll in parts.items():
        for sid, c in coll.items():
            slot = out.setdefault(int(sid), {"cps": 0.0, "jzt": 0.0,
                                             "byAcc": {}, "spendAccts": []})
            cps = float(c.get("cpsCost") or 0)
            jzt = float(c.get("jztCost") or 0)
            slot["cps"] += cps
            slot["jzt"] += jzt
            slot["byAcc"][acc] = {"cps": round(cps, 2), "jzt": round(jzt, 2)}
            if cps or jzt:
                slot["spendAccts"].append(acc)
    for sid, slot in out.items():
        slot["owner"] = owners.get(sid) or ""
        slot["cps"] = round(slot["cps"], 2)
        slot["jzt"] = round(slot["jzt"], 2)
    return out


def rows_for_account(parts: dict[str, dict[int, dict]], attributed: dict[int, dict],
                     acc: str, costs: dict | None = None) -> list[dict]:
    """生成某个账号的利润行：成交取自己的，花费取「归集到自己」的那份。

    有归属人的 SKU：花费整体归到成交账号（只算一次）；
    没有归属人的 SKU：各账号只保留自己投的那份，避免两张表重复计算。
    """
    coll = parts.get(acc) or {}
    merged: dict[int, dict] = {}

    def _put(sid: int, item: dict) -> None:
        merged[int(sid)] = item

    for sid, c in coll.items():
        slot = attributed.get(int(sid)) or {}
        owner = slot.get("owner") or ""
        if owner:
            mine = (owner == acc)
            cps = float(slot.get("cps") or 0) if mine else 0.0
            jzt = float(slot.get("jzt") or 0) if mine else 0.0
        else:
            own = (slot.get("byAcc") or {}).get(acc) or {}
            cps = float(own.get("cps") or 0)
            jzt = float(own.get("jzt") or 0)
        _put(sid, {"skuId": int(sid), "itemName": c.get("itemName") or "",
                   "qty": int(c.get("qty") or 0), "szAmt": float(c.get("szAmt") or 0),
                   "cpsCost": cps, "jztCost": jzt,
                   "accts": list(c.get("accts") or [acc]),
                   "spendAccts": list(slot.get("spendAccts") or [acc])})

    # 花费归到本账号、但本账号商智里没有这个 SKU（极少见）：补一行，便于看到这笔钱
    for sid, slot in attributed.items():
        if sid in merged:
            continue
        owner = slot.get("owner") or ""
        if owner and owner != acc:
            continue
        if not owner:
            own = (slot.get("byAcc") or {}).get(acc) or {}
            cps, jzt = float(own.get("cps") or 0), float(own.get("jzt") or 0)
        else:
            cps, jzt = float(slot.get("cps") or 0), float(slot.get("jzt") or 0)
        if not cps and not jzt:
            continue
        _put(sid, {"skuId": int(sid), "itemName": "", "qty": 0, "szAmt": 0.0,
                   "cpsCost": cps, "jztCost": jzt,
                   "accts": [], "spendAccts": list(slot.get("spendAccts") or [])})
    return finalize(merged, costs=costs)


def totals_of(rows: list[dict]) -> dict:
    def _sum(k, subset=None):
        return round(sum(float(r.get(k) or 0) for r in (subset if subset is not None else rows)), 2)

    arrival = _sum("arrival")
    net = _sum("net")
    # 净利率只在「有到手结算价」的 SKU 上算，否则分母少一截会虚高
    with_arr = [r for r in rows if r.get("arrival")]
    net_rate = (round(_sum("net", with_arr) / arrival * 100, 2)
                if arrival and with_arr else None)
    return {"skuCount": len(rows), "qty": int(sum(int(r.get("qty") or 0) for r in rows)),
            "arrival": arrival, "gross": _sum("gross"),
            "cpsCost": _sum("cpsCost"), "jztCost": _sum("jztCost"),
            "net": net, "netRate": net_rate,
            "noSupply": len(rows) - len(with_arr)}


# ================================================================ 小工具
def _recalc(r: dict) -> None:
    r["net"] = round(float(r.get("gross") or 0) - float(r.get("cpsCost") or 0)
                     - float(r.get("jztCost") or 0), 2)
    base = r.get("arrival") if r.get("arrival") else (r.get("gross") if r.get("gross") else 0)
    r["netRate"] = round(r["net"] / base * 100, 2) if base else None


# ================================================================ Excel
def build_xlsx(rows: list[dict], start: str, end: str, account_label: str = "",
               totals: dict | None = None, notes: list | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    totals = totals or totals_of(rows)
    wb = Workbook()
    fill = PatternFill("solid", fgColor="DDEBF7")
    bold, center = Font(bold=True), Alignment(horizontal="center", vertical="center")

    def _head(ws, cols):
        for c in ws[1]:
            c.font = bold
            c.fill = fill
            c.alignment = center
        for i, w in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    ws = wb.active
    ws.title = "SKU利润"
    ws.append(["SKU", "商品名称"] + [lab for _, lab, _ in FIELDS])
    _head(ws, [15, 46] + [17] * len(FIELDS))
    for r in rows:
        ws.append([r.get("skuId"), r.get("itemName")] + [r.get(k) for k, _, _ in FIELDS])
    ws.append([])
    ws.append(["合计", f"{len(rows)} 个 SKU"] + [
        totals.get(k) if k in ("qty", "arrival", "gross", "cpsCost", "jztCost", "net") else None
        for k, _, _ in FIELDS])
    ws.freeze_panes = "C2"
    for row in ws.iter_rows(min_row=2):
        for c in row[2:]:
            if isinstance(c.value, float):
                c.number_format = "#,##0.00"
            elif isinstance(c.value, int):
                c.number_format = "#,##0"

    cats = catalog.aggregate(rows, SUM_KEYS)
    cats.sort(key=lambda x: -(x.get("gross") or 0))
    ws2 = wb.create_sheet("品类汇总")
    labels = {k: lab for k, lab, _ in FIELDS}
    ws2.append(["品类", "SKU数"] + [labels[k] for k in SUM_KEYS])
    _head(ws2, [20, 10] + [17] * len(SUM_KEYS))
    for it in cats:
        ws2.append([it["category"], it["skuCount"]] + [it[k] for k in SUM_KEYS])
    ws2.append([])
    ws2.append(["合计", f"{len(rows)} 个 SKU"] + [
        totals.get(k) if k in ("arrival", "gross", "cpsCost", "jztCost", "net") else None
        for k in SUM_KEYS])

    ws3 = wb.create_sheet("说明")
    ws3.append(["统计区间", f"{start} ~ {end}"])
    ws3.append(["账号", account_label or "-"])
    ws3.append(["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    ws3.append(["成交件数", "商智 商品明细（指标 SZ_QTY），一次区间查询"])
    ws3.append(["到手结算价", "ERP /api/open/sku-costs 的 supply（京东结算给我们的每件金额，已扣点）"])
    ws3.append(["到手金额", "成交件数 × 到手结算价"])
    ws3.append(["毛利/件", "analyze2.sku_cost_of().grossPerOrder = 到手结算价 − 货款 − 运费 − 包材 − 人工（未扣广告）"])
    ws3.append(["毛利", "成交件数 × 毛利/件"])
    ws3.append(["CPS 花费", "花费一览 → CPS 花费的本地缓存 payCommAmount（缺失则为 0，请看备注）"])
    ws3.append(["京准通花费", "花费一览 → 京准通花费的本地缓存 jztCost"])
    ws3.append(["净利", "毛利 − CPS 花费 − 京准通花费；净利率 = 净利 ÷ 到手金额"])
    ws3.append([])
    for n in notes or []:
        ws3.append(["备注", n])
    ws3.column_dimensions["A"].width = 18
    ws3.column_dimensions["B"].width = 100

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ================================================================ 任务
class ProfitRunner:
    """月度利润的后台任务（与登录 / 花费任务共用同一把 Profile 锁）。"""

    def __init__(self, profile_lock: threading.Lock | None = None):
        self._lock = threading.Lock()
        self._profile_lock = profile_lock or threading.Lock()
        self._thread: threading.Thread | None = None
        self._results: dict[str, dict] = {}
        self._state = {"running": False, "jobId": None, "account": None, "range": None,
                       "total": 0, "done": 0, "message": "", "error": None,
                       "startedAt": None, "finishedAt": None, "cached": False}

    # ---------------- 对外
    def snapshot(self, with_rows: bool = False, job: str | None = None) -> dict:
        with self._lock:
            state = dict(self._state)
        if job and job != state.get("jobId"):
            # 查看另一份结果（多为本地缓存）：只回它的数据，不掺当前任务进度，
            # 这样后台任务还在跑的时候也能看已经算好的区间。
            data = self._results.get(job)
            if data is not None:
                when = data.get("savedAt") or data.get("finishedAt") or ""
                busy = "，后台还有一个任务在跑，互不影响" if state.get("running") else ""
                out = {"running": False, "cached": True, "jobId": job, "error": None,
                       "account": data.get("account"),
                       "accounts": data.get("accounts") or [],
                       "range": [data.get("start"), data.get("end")],
                       "total": len(data.get("rows") or []),
                       "done": len(data.get("rows") or []),
                       "savedAt": data.get("savedAt"),
                       "finishedAt": data.get("finishedAt"),
                       "message": f"本次结果（{when}）{busy}"}
                if with_rows:
                    out.update(rows=data.get("rows") or [], totals=data.get("totals") or {},
                               notes=data.get("notes") or [], merges=data.get("merges") or [],
                               byAccount=data.get("byAccount") or {},
                               accounts=data.get("accounts") or [])
                return out
        data = self._results.get(state.get("jobId") or "")
        if with_rows and data:
            state["rows"] = data.get("rows") or []
            state["totals"] = data.get("totals") or {}
            state["notes"] = data.get("notes") or []
            state["merges"] = data.get("merges") or []
            state["byAccount"] = data.get("byAccount") or {}
            state["accounts"] = data.get("accounts") or []
        return state

    def _paths(self, account: str, start: str, end: str) -> Path:
        """缓存按「区间」一份（里面同时存各账号的表），切换账号不用重新抓。"""
        base = Path(settings.DATA_DIR) / "reports"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"profit_{start}_{end}.json"

    def load_cached(self, account: str, start: str, end: str) -> dict | None:
        key = account if account == "all" else settings.get_account(account)["key"]
        fp = self._paths(key, start, end)
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not data.get("rows"):
            return None
        st = fp.stat()
        data["savedAt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        by = data.get("byAccount") or {}
        if key != "all" and key in by:
            # 原地改，别用 dict(...) 复制：调用方（reprice）持有的可能是同一份对象的浅拷贝，
            # 复制会导致内存里的 totals 不更新，只有磁盘是新的
            data["rows"] = by[key].get("rows") or []
            data["totals"] = by[key].get("totals") or {}
            data["account"] = key
            data["accountLabel"] = by[key].get("label") or key
        for r in data.get("rows") or []:
            if not r.get("category"):
                r["category"] = catalog.category_of(r.get("itemName") or "")
        return data

    def run_background(self, account: str, start: str, end: str, force: bool = False,
                       accounts: list[str] | None = None) -> dict:
        start, end = norm_range(start, end)
        keys = [settings.get_account(a)["key"] for a in (accounts or [account])]
        key = keys[0] if len(keys) == 1 else "all"
        with self._lock:
            running = bool(self._state.get("running"))
            # 先查缓存：命中缓存不需要抓取，跟正在跑的任务互不冲突，
            # 除非用户明确要求「重新计算」（force），才排队等当前任务结束。
            if not force:
                cached = self.load_cached(account, start, end)
                if cached:
                    job_id = cached.get("jobId") or f"profit-cache-{key}-{start}_{end}"
                    cached["jobId"] = job_id
                    self._results[job_id] = cached
                    if not running:
                        self._state.update({"running": False, "cached": True, "jobId": job_id,
                                            "account": key, "accounts": keys,
                                            "range": [start, end],
                                            "total": len(cached["rows"]),
                                            "done": len(cached["rows"]),
                                            "error": None, "finishedAt": settings.now_iso(),
                                            "message": f"命中本地缓存（{cached.get('savedAt')} 计算），"
                                                       f"需要最新数据请点「重新计算」"})
                    return {"ok": True, "cached": True, "jobId": job_id,
                            "savedAt": cached.get("savedAt"), "rows": len(cached["rows"]),
                            "busy": running}
            if running:
                cur = self._state.get("range") or []
                more = f"（正在算 {' ~ '.join(cur)}）" if cur else ""
                tail = ("重新计算要抓商智，请等它结束再点"
                        if force else f"{start} ~ {end} 没有本地缓存，请等它结束再查")
                return {"ok": False,
                        "error": f"已有「月度利润分析」任务在运行{more}，{tail}"}
            job_id = time.strftime("%Y%m%d%H%M%S") + "-profit-" + key
            self._state.update({"running": True, "cached": False, "jobId": job_id,
                                "account": key, "accounts": keys,
                                "range": [start, end], "total": 0, "done": 0,
                                "message": "正在启动浏览器…", "error": None,
                                "startedAt": settings.now_iso(), "finishedAt": None})
        t = threading.Thread(target=self._work, args=(job_id, keys, start, end),
                             name=f"profit-{key}", daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "cached": False, "jobId": job_id, "account": key,
                "accounts": keys, "start": start, "end": end}

    def export(self, job_id: str | None = None, account: str | None = None,
               start: str | None = None, end: str | None = None,
               dim: str = "sku") -> tuple[bytes, str] | None:
        data = self._results.get(job_id or "") or None
        if data is None and account and start and end:
            data = self.load_cached(account, start, end)
        if data is None:
            latest = sorted((Path(settings.DATA_DIR) / "reports").glob("profit_*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
            if latest:
                try:
                    data = json.loads(latest[0].read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = None
        if not data:
            return None
        rows = data.get("rows") or []
        if dim == "cat":
            keep = [c for c in catalog.aggregate(rows, SUM_KEYS)]
            keep.sort(key=lambda x: -(x.get("gross") or 0))
            rows_cat = [{"skuId": f"{c['skuCount']} 个SKU", "itemName": c["category"], **c}
                        for c in keep]
            xlsx = build_xlsx(rows_cat, data["start"], data["end"],
                              data.get("accountLabel") or data.get("account"),
                              totals=data.get("totals"), notes=data.get("notes"))
        else:
            xlsx = build_xlsx(rows, data["start"], data["end"],
                              data.get("accountLabel") or data.get("account"),
                              totals=data.get("totals"), notes=data.get("notes"))
        suffix = "_品类" if dim == "cat" else ""
        return xlsx, f"月度利润{suffix}_{data.get('account')}_{data['start']}_{data['end']}.xlsx"


    def _payload(self, job_id: str | None = None, account: str | None = None,
                 start: str | None = None, end: str | None = None) -> dict | None:
        data = self._results.get(job_id or "") or None
        if data is None and account and start and end:
            data = self.load_cached(account, start, end)
        return data

    @staticmethod
    def _reprice_payload(data: dict, costs: dict, model: dict, units: dict) -> int:
        """按给定成本重算一份 payload，返回发生变化的行数。"""
        by = data.get("byAccount") or {}
        if not by:
            by = {data.get("account") or "main": {"rows": data.get("rows") or [],
                                                  "totals": data.get("totals") or {}}}
            data["byAccount"] = by
        changed = 0
        for acc, block in by.items():
            rows = block.get("rows") or []
            for r in rows:
                qty = int(r.get("qty") or 0)
                amt = float(r.get("szAmt") or 0)
                front = r.get("frontPrice") or (round(amt / qty, 2) if qty else 0)
                cp = analyze2.sku_cost_of(str(r["skuId"]), float(front or 0), model, costs, units)
                supply = float(cp.get("supply") or 0)
                gpo = float(cp.get("grossPerOrder") or 0)
                if r.get("supply") != round(supply, 2) or r.get("grossPerOrder") != round(gpo, 2):
                    changed += 1
                r["frontPrice"] = round(float(front or 0), 2)
                r["supply"] = round(supply, 2)
                r["grossPerOrder"] = round(gpo, 2)
                r["costSource"] = cp.get("costSource") or ""
                r["arrival"] = round(qty * supply, 2) if (qty and supply > 0) else None
                r["gross"] = round(qty * gpo, 2)
                _recalc(r)
            rows.sort(key=lambda x: -(x.get("gross") or 0))
            block["rows"] = rows
            block["totals"] = totals_of(rows)
            if acc == data.get("account"):
                data["rows"] = rows
                data["totals"] = block["totals"]
        data["costSource"] = settings.COSTS_SOURCE
        data["costUpdatedAt"] = costs.get("updatedAt")
        data["notes"] = [n for n in (data.get("notes") or [])
                         if not str(n).startswith("成本口径：")
                         and "在 ERP 里查不到到手结算价" not in str(n)]
        no_supply = sum(1 for b in by.values() for r in (b.get("rows") or [])
                        if not r.get("arrival"))
        if no_supply:
            data["notes"].append(f"{no_supply} 个 SKU 在 ERP 里查不到到手结算价："
                                 f"到手金额留空、毛利按全店口径估算。")
        src = settings.COSTS_SOURCE or {}
        if src.get("kind") and src.get("kind") != "供货方接口":
            data["notes"].append(f"成本口径：{src.get('kind')}（{src.get('detail')}）")
        return changed

    def reprice(self, job_id: str | None = None, account: str | None = None,
                start: str | None = None, end: str | None = None,
                force: bool = True, all_ranges: bool = True) -> dict:
        """只重拉 ERP 成本，重算利润（商智成交与 CPS/京准通花费都不动）。

        默认把 data/reports 下所有已缓存的月度利润表一起重算，避免另一个月份还是旧成本。
        """
        costs = settings.load_costs(start or "", end or "", force=force)
        if not (costs or {}).get("skus"):
            return {"ok": False,
                    "error": "ERP 成本接口没有返回任何 SKU（接口可能仍在报错），已保留旧数据。"
                             f"来源：{(settings.COSTS_SOURCE or {}).get('detail')}"}
        model = analyze2.load_cost_model()
        units = settings.costs_unit_map()

        data = self._payload(job_id, account, start, end)
        if not data:
            return {"ok": False, "error": "没有已算好的结果，请先查询一次"}
        changed = self._reprice_payload(data, costs, model, units)
        try:
            self._paths(data.get("account") or "main",
                        data["start"], data["end"]).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        files, extra = 1, 0
        if all_ranges:
            base = Path(settings.DATA_DIR) / "reports"
            for fp in sorted(base.glob("profit_2*-*-*_2*-*-*.json")):
                if not re.fullmatch(r"profit_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.json",
                                    fp.name):
                    continue
                try:
                    other = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if (other.get("start"), other.get("end")) == (data.get("start"), data.get("end")):
                    continue
                if not other.get("byAccount") and not other.get("rows"):
                    continue
                extra += self._reprice_payload(other, costs, model, units)
                files += 1
                try:
                    fp.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": True, "changed": changed + extra, "files": files,
                "costUpdatedAt": costs.get("updatedAt"),
                "costSku": len(costs.get("skus") or {}),
                "source": (settings.COSTS_SOURCE or {}).get("kind"),
                "totals": data.get("totals")}

    # ---------------- 内部
    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _work(self, job_id: str, accounts: list[str], start: str, end: str) -> None:
        """逐账号抓商智、读花费缓存，最后按 SKU 合并成一张利润表。

        为什么要跨账号：京准通的花费是「谁投的广告」记在谁的账户下，
        而商智的成交是「谁卖出去的」记在谁的店铺下。实测账号B 的京准通里
        有主账号的商品（拇指小玉米等）在跑，按 SKU 归集才对得上。
        """
        pw = ctx = None
        try:
            lock = self._profile_lock
            parts: dict[str, dict] = {}
            notes: list[str] = []
            per_acc: dict[str, dict] = {}
            for idx, account in enumerate(accounts, 1):
                self._set(message=f"等待浏览器登录态（{account}，{idx}/{len(accounts)}）…")
                if not lock.acquire(timeout=900):
                    raise RuntimeError("浏览器登录态被占用（登录/抓取/其他任务），请稍后重试")
                pw = ctx = None
                try:
                    self._set(message=f"正在启动浏览器（{account}）…")
                    pw, ctx = launch_persistent(headless=True, account=account)
                    page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                    page.goto(BOARD_PAGE, wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(8000)
                    if not session_ok(page):
                        raise RuntimeError(f"{account} 登录态失效：请先到「登录」页扫码，或点控制台的「立即续期」")

                    self._set(message=f"正在取商智成交件数（{account}，{start} ~ {end}）…")
                    sz_rows = fetch_sz_rows(page, start, end)
                    self._set(message=f"{account} 商智返回 {len(sz_rows)} 行，检查花费缓存…")
                    # 花费缓存缺失就地补齐：同一个浏览器、同一个账号、同一个区间，
                    # 抓完写进「花费一览」同一套缓存文件，不用再让用户去别的页面手动补。
                    for mode, label in (("cps", "CPS 花费"), ("spend", "京准通花费")):
                        if paycomm.read_cached(mode, account, start, end) is not None:
                            continue
                        try:
                            got = paycomm.fetch_into_cache(
                                page, mode, account, start, end,
                                on_progress=lambda m, lb=label: self._set(
                                    message=f"自动补抓{lb}（{account}）：{m}"))
                        except Exception as exc:  # noqa: BLE001
                            notes.append(
                                f"{account} 的{label}自动补抓失败（{type(exc).__name__}: {exc}），"
                                f"本次该列按 0 计；可稍后到「{label}」页重试。")
                            continue
                        if got:
                            notes.append(
                                f"{account} 的{label}本地没有 {start} ~ {end} 的缓存，"
                                f"已在本次查询中自动抓取（{len(got.get('rows') or [])} 个 SKU）并写入缓存。")
                finally:
                    for closer in (lambda: ctx and ctx.close(), lambda: pw and pw.stop()):
                        try:
                            closer()
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        lock.release()
                    except Exception:  # noqa: BLE001
                        pass

                cps_data = paycomm.read_cached("cps", account, start, end)
                jzt_data = paycomm.read_cached("spend", account, start, end)
                if cps_data is None:
                    notes.append(f"{account} 的 CPS 花费在 {start} ~ {end} 既没有缓存、"
                                 f"自动补抓也没取到数据（该区间可能没有 CPS 成交）。")
                if cps_data is None and jzt_data is None:
                    notes.append(f"{account} 没有任何花费数据，本次只统计成交与毛利。")
                cps, jzt = _cost_map(cps_data, jzt_data)
                names: dict[int, str] = {}
                for src in (jzt_data, cps_data):
                    for r in (src or {}).get("rows") or []:
                        try:
                            sid = int(r.get("skuId") or 0)
                        except (TypeError, ValueError):
                            continue
                        if sid and r.get("itemName"):
                            names.setdefault(sid, r["itemName"])
                per_acc[account] = {"rows": len(sz_rows), "spend": round(sum(jzt.values()), 2)}
                parts[account] = collect(sz_rows, cps, jzt, names=names, accounts=[account])

            self._set(message="正在读取 ERP 到手结算价…")
            costs = settings.load_costs(start, end)
            if not (costs or {}).get("skus"):
                notes.append("ERP 成本接口没有返回任何 SKU（可能 Token 失效或区间无数据），"
                             "到手结算价按 0 处理，请检查成本接口。")

            self._set(message="正在按 SKU 把花费归集到对应账号…")
            attributed = attribute_spend(parts)
            by_account: dict[str, dict] = {}
            for acc in accounts:
                rows = rows_for_account(parts, attributed, acc, costs=costs)
                by_account[acc] = {"rows": rows, "totals": totals_of(rows),
                                   "label": settings.get_account(acc).get("label") or acc}
            main_acc = accounts[0]
            rows = by_account[main_acc]["rows"]
            totals = by_account[main_acc]["totals"]
            if not any(b["rows"] for b in by_account.values()):
                raise RuntimeError("该区间没有任何成交/花费数据（商智可能还没出数，或区间选到了今天）")

            moved = [sid for sid, v in attributed.items()
                     if v.get("owner") and any(a != v["owner"] for a in (v.get("spendAccts") or []))]
            if moved:
                notes.append(
                    f"已按 SKU 归集：{len(moved)} 个 SKU 的广告花费由投放账号归到了一直出单的账号"
                    f"（例如账号B 投主账号的商品，这笔钱记在主账号那一行），"
                    f"所以两个账号的利润表都能对上，不会再出现「有花费没成交」的虚行。")

            no_supply = sum(1 for b in by_account.values() for r in b["rows"] if not r.get("arrival"))
            if no_supply:
                notes.append(f"{no_supply} 个 SKU 在 ERP 里查不到到手结算价：到手金额留空、"
                             f"毛利按全店口径估算，净利率只按有到手结算价的 SKU 计算。")
            src = settings.COSTS_SOURCE or {}
            if src and src.get("kind") and src.get("kind") != "供货方接口":
                notes.append(f"成本口径：{src.get('kind')}（{src.get('detail')}）")

            label = " + ".join(b["label"] for b in by_account.values())
            payload = {"jobId": job_id, "account": main_acc, "accounts": accounts,
                       "accountLabel": label, "start": start, "end": end,
                       "rows": rows, "totals": totals, "byAccount": by_account,
                       "owner": {str(k): v.get("owner") for k, v in attributed.items()
                                 if v.get("owner")},
                       "notes": notes, "finishedAt": settings.now_iso(),
                       "costSource": settings.COSTS_SOURCE, "perAccount": per_acc}
            self._results[job_id] = payload
            try:
                self._paths(main_acc, start, end).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            self._set(running=False, finishedAt=settings.now_iso(), error=None,
                      total=len(rows), done=len(rows),
                      message=(f"完成：{label} 各一张表（共 {sum(len(b['rows']) for b in by_account.values())} 行），"
                               f"{by_account[main_acc]['label']} 净利 {totals['net']:,.2f} 元"))
        except Exception as exc:  # noqa: BLE001
            self._set(running=False, error=f"{type(exc).__name__}: {exc}",
                      finishedAt=settings.now_iso(), message=f"失败：{exc}")

