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

花费只读 paycomm 的本地缓存（精确命中或多段拼接），不会在这里额外抓取；
缓存缺失时该列留空并在页面上标注，去对应页面抓一次即可。
"""
from __future__ import annotations

import datetime as dt
import io
import json
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


def build_rows(sz_rows: list[dict], cps: dict, jzt: dict,
               costs: dict | None = None) -> list[dict]:
    """把商智销量、ERP 成本、CPS/京准通花费拼成利润行（含只有花费没有销量的 SKU）。"""
    costs = settings.load_costs() if costs is None else costs
    model = analyze2.load_cost_model()
    units = settings.costs_unit_map()
    rows: dict[int, dict] = {}

    def _ensure(sid: int, name: str = "") -> dict:
        r = rows.get(sid)
        if r is None:
            r = {"skuId": sid, "itemName": name or "",
                 "category": catalog.category_of(name), "qty": 0, "szAmt": 0.0,
                 "frontPrice": 0.0, "supply": 0.0, "arrival": None,
                 "grossPerOrder": 0.0, "gross": 0.0,
                 "cpsCost": 0.0, "jztCost": 0.0, "net": 0.0, "netRate": None,
                 "costSource": ""}
            rows[sid] = r
        elif name and not r["itemName"]:
            r["itemName"] = name
            r["category"] = catalog.category_of(name)
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
        r = _ensure(sid, str(raw.get("name") or ""))
        r["qty"] = qty
        r["szAmt"] = round(amt, 2)

    # 只有广告花费、没有销量的 SKU 也要出现（那是纯亏）
    for sid in set(cps) | set(jzt):
        _ensure(int(sid))

    for sid, r in rows.items():
        qty = int(r["qty"] or 0)
        front = round(r["szAmt"] / qty, 2) if qty else 0.0
        cp = analyze2.sku_cost_of(str(sid), front, model, costs, units)
        supply = float(cp.get("supply") or 0)
        gpo = float(cp.get("grossPerOrder") or 0)
        r["frontPrice"] = front
        r["supply"] = round(supply, 2)
        r["grossPerOrder"] = round(gpo, 2)
        r["costSource"] = cp.get("costSource") or ""
        r["arrival"] = round(qty * supply, 2) if (qty and supply > 0) else None
        r["gross"] = round(qty * gpo, 2)
        r["cpsCost"] = round(cps.get(sid, 0.0), 2)
        r["jztCost"] = round(jzt.get(sid, 0.0), 2)
        r["net"] = round(r["gross"] - r["cpsCost"] - r["jztCost"], 2)
        base = r["arrival"] if r["arrival"] else (r["gross"] if r["gross"] else 0)
        r["netRate"] = round(r["net"] / base * 100, 2) if base else None

    out = [r for r in rows.values() if r["qty"] or r["cpsCost"] or r["jztCost"]]
    out.sort(key=lambda x: -(x["gross"] or 0))
    return out


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
    def snapshot(self, with_rows: bool = False) -> dict:
        with self._lock:
            state = dict(self._state)
        data = self._results.get(state.get("jobId") or "")
        if with_rows and data:
            state["rows"] = data.get("rows") or []
            state["totals"] = data.get("totals") or {}
            state["notes"] = data.get("notes") or []
        return state

    def _paths(self, account: str, start: str, end: str) -> Path:
        base = Path(settings.DATA_DIR) / "reports"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"profit_{account}_{start}_{end}.json"

    def load_cached(self, account: str, start: str, end: str) -> dict | None:
        fp = self._paths(settings.get_account(account)["key"], start, end)
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
        for r in data["rows"]:
            if not r.get("category"):
                r["category"] = catalog.category_of(r.get("itemName") or "")
        return data

    def run_background(self, account: str, start: str, end: str, force: bool = False) -> dict:
        start, end = norm_range(start, end)
        key = settings.get_account(account)["key"]
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": "已有「月度利润分析」任务在运行"}
            if not force:
                cached = self.load_cached(key, start, end)
                if cached:
                    job_id = cached.get("jobId") or f"profit-cache-{key}-{start}_{end}"
                    self._results[job_id] = cached
                    self._state.update({"running": False, "cached": True, "jobId": job_id,
                                        "account": key, "range": [start, end],
                                        "total": len(cached["rows"]), "done": len(cached["rows"]),
                                        "error": None, "finishedAt": settings.now_iso(),
                                        "message": f"命中本地缓存（{cached.get('savedAt')} 计算），"
                                                   f"需要最新数据请点「重新计算」"})
                    return {"ok": True, "cached": True, "jobId": job_id,
                            "savedAt": cached.get("savedAt"), "rows": len(cached["rows"])}
            job_id = time.strftime("%Y%m%d%H%M%S") + "-profit-" + key
            self._state.update({"running": True, "cached": False, "jobId": job_id,
                                "account": key, "range": [start, end], "total": 0, "done": 0,
                                "message": "正在启动浏览器…", "error": None,
                                "startedAt": settings.now_iso(), "finishedAt": None})
        t = threading.Thread(target=self._work, args=(job_id, key, start, end),
                             name=f"profit-{key}", daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "cached": False, "jobId": job_id, "account": key,
                "start": start, "end": end}

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

    # ---------------- 内部
    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _work(self, job_id: str, account: str, start: str, end: str) -> None:
        pw = ctx = None
        try:
            lock = self._profile_lock
            self._set(message="等待浏览器登录态空闲…")
            if not lock.acquire(timeout=600):
                raise RuntimeError("浏览器登录态被占用（登录/抓取/其他花费任务），请稍后重试")
            try:
                self._set(message="正在启动浏览器…")
                pw, ctx = launch_persistent(headless=True, account=account)
                page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                page.goto(BOARD_PAGE, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(8000)
                if not session_ok(page):
                    raise RuntimeError("登录态失效：请先到「登录」页扫码，或点控制台的「立即续期」")

                self._set(message=f"正在取商智成交件数（{start} ~ {end}）…")
                sz_rows = fetch_sz_rows(page, start, end)
                self._set(total=len(sz_rows), done=0,
                          message=f"商智返回 {len(sz_rows)} 行，正在读取 ERP 成本与花费缓存…")
            finally:
                for closer in (lambda: ctx and ctx.close(), lambda: pw and pw.stop()):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass
                lock.release()

            notes: list[str] = []
            self._set(message="正在读取 ERP 到手结算价…")
            costs = settings.load_costs(start, end)
            if not (costs or {}).get("skus"):
                notes.append("ERP 成本接口没有返回任何 SKU（可能 Token 失效或区间无数据），"
                             "到手结算价按 0 处理，请检查成本接口。")

            cps_data = paycomm.read_cached("cps", account, start, end)
            jzt_data = paycomm.read_cached("spend", account, start, end)
            if cps_data is None:
                notes.append(f"CPS 花费没有本地缓存（{start} ~ {end}）："
                             f"请到「CPS花费」页查询一次该区间（或相邻月份）再来算。")
            if jzt_data is None:
                notes.append(f"京准通花费没有本地缓存（{start} ~ {end}）："
                             f"请到「京准通花费」页查询一次该区间再来算。")
            cps, jzt = _cost_map(cps_data, jzt_data)

            self._set(message="正在计算到手金额 / 毛利 / 净利…")
            rows = build_rows(sz_rows, cps, jzt, costs=costs)
            if not rows:
                raise RuntimeError("该区间没有任何成交/花费数据（商智可能还没出数，或区间选到了今天）")

            no_supply = sum(1 for r in rows if not r.get("arrival"))
            if no_supply:
                notes.append(f"{no_supply} 个 SKU 在 ERP 里查不到到手结算价：到手金额留空、"
                             f"毛利按全店口径估算，净利率只按有到手结算价的 SKU 计算。")
            src = settings.COSTS_SOURCE or {}
            if src and src.get("kind") and src.get("kind") != "供货方接口":
                notes.append(f"成本口径：{src.get('kind')}（{src.get('detail')}）")

            totals = totals_of(rows)
            label = settings.get_account(account).get("label") or account
            payload = {"jobId": job_id, "account": account, "accountLabel": label,
                       "start": start, "end": end, "rows": rows, "totals": totals,
                       "notes": notes, "finishedAt": settings.now_iso(),
                       "costSource": settings.COSTS_SOURCE,
                       "hasCps": cps_data is not None, "hasJzt": jzt_data is not None}
            self._results[job_id] = payload
            try:
                self._paths(account, start, end).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            self._set(running=False, finishedAt=settings.now_iso(),
                      total=len(rows), done=len(rows), error=None,
                      message=(f"完成：{len(rows)} 个 SKU，到手金额 {totals['arrival']:,.2f}，"
                               f"毛利 {totals['gross']:,.2f}，净利 {totals['net']:,.2f} 元"))
        except Exception as exc:  # noqa: BLE001
            self._set(running=False, error=f"{type(exc).__name__}: {exc}",
                      finishedAt=settings.now_iso(), message=f"失败：{exc}")
