# -*- coding: utf-8 -*-
"""花费一览：按 SKU 抓「京准通 → 数据看板 → 商品数据」的 CPS 花费（付款佣金金额）。

数据接口（与页面同源，复用已登录 Profile 在页面内 fetch）：
    POST https://atoms-api.jd.com/union/dataBoard/chooseGoods    商品列表（该区间有数据的 SKU）
    POST https://atoms-api.jd.com/union/dataBoard/orderData      单个 SKU 的 CPS 收支（payCommAmount）
    POST https://atoms-api.jd.com/union/dataBoard/targetCompare  账户级分日趋势（合计校验用）

设计要点：
  - 单 SKU 串行请求，每次之间 0.8~1.6 秒随机抖动，避免请求过快触发风控（掉登录态要重扫码）
  - 整段抓取持有浏览器 Profile 锁（与登录 / 保活互斥），同一时刻只允许一个任务
  - 结果落盘 data/reports/paycomm_<account>_<start>_<end>.json / .xlsx，供下载与复查
"""
from __future__ import annotations

import datetime as dt
import io
import json
import random
import threading
import time
from pathlib import Path

from . import settings
from .browser import launch_persistent, session_ok

BOARD_PAGE = "https://jzt.jd.com/jtk/#/data-board"
BASE = "https://atoms-api.jd.com/union/dataBoard"

# 限速：单次请求之间留随机间隔（风控优先，宁可慢）
DELAY_SKU = (0.8, 1.6)
DELAY_PAGE = (0.6, 1.2)

# (接口字段, Excel 列名, 类型)
FIELDS = [
    ("payCommAmount", "付款佣金金额(元)", "money"),
    ("payCommRate", "付款佣金比例(%)", "num"),
    ("preCommAmount", "预估佣金金额(元)", "money"),
    ("preCommRate", "预估佣金比例(%)", "num"),
    ("clicks", "点击数", "int"),
    ("uv", "引入UV", "int"),
    ("orderCnt", "下单订单量", "int"),
    ("orderCvr", "下单流量转化率(%)", "num"),
    ("orderGmv", "下单订单金额(元)", "money"),
    ("payOrderCnt", "付款订单量", "int"),
    ("fiOrderCnt", "完成订单量", "int"),
    ("fiOrderGmv", "完成订单金额(元)", "money"),
    ("orderUserCnt", "下单用户数", "int"),
    ("roi", "ROI", "num"),
]

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json',
             'accept':'application/json, text/plain, */*',
             'referer':'https://jzt.jd.com/jtk/',
             'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _sleep(rng: tuple) -> None:
    time.sleep(random.uniform(*rng))


def norm_range(start: str, end: str) -> tuple[str, str]:
    """校验并归一化日期区间（ISO 字符串）。"""
    try:
        s = dt.date.fromisoformat(str(start).strip())
        e = dt.date.fromisoformat(str(end).strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"日期格式不对（要 YYYY-MM-DD）：{exc}") from exc
    if s > e:
        s, e = e, s
    if (e - s).days > 366:
        raise ValueError("区间最长 366 天")
    return s.isoformat(), e.isoformat()


def _post(page, path: str, payload: dict, retries: int = 2) -> dict:
    url = f"{BASE}/{path}?requestFrom=0"
    last = ""
    for _ in range(retries + 1):
        try:
            res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
            if res["status"] != 200:
                last = f"HTTP {res['status']}"
            else:
                data = json.loads(res["text"])
                if str(data.get("code")) == "1":
                    return data
                last = json.dumps(data, ensure_ascii=False)[:200]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
    raise RuntimeError(f"{path} 失败：{last}")


def list_goods(page, start: str, end: str, page_size: int = 100) -> list[dict]:
    rows: list[dict] = []
    idx = 1
    while True:
        d = _post(page, "chooseGoods", {"displayType": 1, "itemId": "", "pageIndex": idx,
                                       "pageSize": page_size, "startDate": start, "endDate": end})
        data = d.get("data") or {}
        batch = data.get("data") or []
        rows.extend(batch)
        total = int(data.get("total") or 0)
        if not batch or len(rows) >= total or idx > 50:
            break
        idx += 1
        _sleep(DELAY_PAGE)
    return rows


def sku_metrics(page, sku: int, start: str, end: str) -> dict:
    d = _post(page, "orderData", {"startDate": start, "endDate": end, "realTime": False,
                                  "skuId": sku, "spuId": None})
    return d.get("data") or {}


def daily_total(page, start: str, end: str) -> list[dict]:
    d = _post(page, "targetCompare", {"startDate": start, "endDate": end,
                                      "realTime": False, "skuId": None, "spuId": None})
    return d.get("data") or []


# ================================================================ Excel
def build_xlsx(rows: list[dict], daily: list[dict], start: str, end: str,
               account_label: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "SKU花费"
    ws.append(["SKU", "商品名称"] + [name for _, name, _ in FIELDS])
    fill = PatternFill("solid", fgColor="DDEBF7")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        ws.append([r.get("skuId"), r.get("itemName")] + [r.get(k) for k, _, _ in FIELDS])
    total_pay = round(sum((r.get("payCommAmount") or 0) for r in rows), 2)
    ws.append([])
    ws.append(["合计", f"{len(rows)} 个 SKU", total_pay])
    ws.freeze_panes = "C2"
    for i, w in enumerate([15, 46] + [17] * len(FIELDS), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row[2:]:
            if isinstance(c.value, float):
                c.number_format = "#,##0.00"
            elif isinstance(c.value, int):
                c.number_format = "#,##0"

    ws2 = wb.create_sheet("分日合计")
    ws2.append(["日期", "付款佣金金额(元)", "预估佣金金额(元)", "点击数", "下单订单量",
                "下单订单金额(元)", "完成订单量", "完成订单金额(元)", "ROI"])
    for c in ws2[1]:
        c.font = Font(bold=True)
        c.fill = fill
    for d in daily:
        ws2.append([d.get("date"), d.get("payCommAmount"), d.get("preCommAmount"), d.get("clicks"),
                    d.get("orderCnt"), d.get("orderGmv"), d.get("fiOrderCnt"),
                    d.get("fiOrderGmv"), d.get("roi")])
    if daily:
        ws2.append([])
        ws2.append(["合计", round(sum((d.get("payCommAmount") or 0) for d in daily), 2),
                    round(sum((d.get("preCommAmount") or 0) for d in daily), 2),
                    sum((d.get("clicks") or 0) for d in daily),
                    sum((d.get("orderCnt") or 0) for d in daily),
                    round(sum((d.get("orderGmv") or 0) for d in daily), 2),
                    sum((d.get("fiOrderCnt") or 0) for d in daily),
                    round(sum((d.get("fiOrderGmv") or 0) for d in daily), 2), None])
    for i, w in enumerate([13, 18, 18, 12, 13, 18, 13, 18, 10], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "B2"

    ws3 = wb.create_sheet("说明")
    ws3.append(["统计区间", f"{start} ~ {end}"])
    ws3.append(["账号", account_label or "-"])
    ws3.append(["数据来源", "京准通 数据看板 → 商品数据（atoms-api /union/dataBoard）"])
    ws3.append(["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    ws3.append(["口径", "每个 SKU 单独取 orderData；付款佣金金额=payCommAmount（与页面「商品数据」一致）"])
    ws3.column_dimensions["A"].width = 14
    ws3.column_dimensions["B"].width = 90

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ================================================================ 任务
class PayCommRunner:
    """花费一览的后台任务（单线程串行 + 进度快照）。

    profile_lock 由调用方注入（webapp 的 LoginSession._profile_lock），
    与扫码登录 / 登录态检查 / 保活互斥，避免同一 Profile 被两个上下文打开。
    """

    def __init__(self, profile_lock: threading.Lock | None = None):
        self._lock = threading.Lock()
        self._profile_lock = profile_lock or threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._results: dict[str, dict] = {}
        self._state = {
            "running": False, "jobId": None, "account": None, "range": None,
            "total": 0, "done": 0, "message": "", "error": None,
            "startedAt": None, "finishedAt": None, "cancelled": False,
        }

    # -------------------------------------------------- 对外
    def snapshot(self, with_rows: bool = False) -> dict:
        with self._lock:
            state = dict(self._state)
        job_id = state.get("jobId")
        data = self._results.get(job_id) if job_id else None
        if with_rows and data is None and not state.get("running"):
            # 进程重启 / 换了浏览器标签：把最近一次落盘结果恢复出来
            data = self._latest_from_disk()
            if data:
                state.update(jobId=data.get("jobId"), account=data.get("account"),
                             range=[data.get("start"), data.get("end")],
                             message=state.get("message") or "以上是最近一次保存的结果")
        if with_rows and data:
            state["rows"] = data.get("rows") or []
            state["daily"] = data.get("daily") or []
            state["totalPay"] = round(sum((r.get("payCommAmount") or 0)
                                          for r in state["rows"]), 2)
        return state

    def run_background(self, account: str, start: str, end: str) -> dict:
        start, end = norm_range(start, end)
        key = settings.get_account(account)["key"]
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": "已有「花费一览」任务在运行，请等它结束"}
            job_id = time.strftime("%Y%m%d%H%M%S") + "-" + key
            self._cancel.clear()
            self._state.update({
                "running": True, "jobId": job_id, "account": key, "range": [start, end],
                "total": 0, "done": 0, "message": "正在启动浏览器…", "error": None,
                "startedAt": settings.now_iso(), "finishedAt": None, "cancelled": False,
            })
        t = threading.Thread(target=self._work, args=(job_id, key, start, end),
                             name=f"paycomm-{key}", daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "jobId": job_id, "account": key, "start": start, "end": end}

    def cancel(self) -> dict:
        with self._lock:
            running = bool(self._state.get("running"))
        if running:
            self._cancel.set()
        return {"ok": True, "cancelRequested": running}

    def export(self, job_id: str | None = None) -> tuple[bytes, str] | None:
        """取任务结果的 Excel（内存优先，回退到落盘 JSON）。"""
        job_id = job_id or self.snapshot().get("jobId")
        data = self._results.get(job_id) if job_id else None
        if not data and job_id:
            data = self._load_from_disk(job_id)
        if not data:
            data = self._latest_from_disk()   # 兜底：最近一次落盘结果
        if not data:
            return None
        xlsx = build_xlsx(data["rows"], data.get("daily") or [],
                          data["start"], data["end"], data.get("accountLabel") or data.get("account"))
        fname = f"花费一览_{data.get('account')}_{data['start']}_{data['end']}.xlsx"
        return xlsx, fname

    # -------------------------------------------------- 内部
    def _disk_paths(self, account: str, start: str, end: str) -> tuple[Path, Path]:
        base = Path(settings.DATA_DIR) / "reports"
        base.mkdir(parents=True, exist_ok=True)
        stem = f"paycomm_{account}_{start}_{end}"
        return base / f"{stem}.json", base / f"{stem}.xlsx"

    def _load_from_disk(self, job_id: str) -> dict | None:
        base = Path(settings.DATA_DIR) / "reports"
        for fp in base.glob("paycomm_*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if data.get("jobId") == job_id:
                return data
        return None

    def _latest_from_disk(self) -> dict | None:
        """最近一次落盘结果（进程重启后仍能把上次的表恢复出来）。"""
        base = Path(settings.DATA_DIR) / "reports"
        files = sorted(base.glob("paycomm_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for fp in files[:3]:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if data.get("rows"):
                return data
        return None

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _work(self, job_id: str, account: str, start: str, end: str) -> None:
        pw = ctx = None
        rows: list[dict] = []
        daily: list[dict] = []
        try:
            lock = self._profile_lock
            self._set(message="等待浏览器 Profile 空闲…")
            if not lock.acquire(timeout=600):
                raise RuntimeError("浏览器 Profile 被占用（登录 / 保活进行中），请稍后重试")
            try:
                self._set(message="正在启动浏览器…")
                pw, ctx = launch_persistent(headless=True, account=account)
                page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                page.goto(BOARD_PAGE, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(10000)
                if not session_ok(page):
                    raise RuntimeError("登录态失效：请先到「登录」页扫码，或点控制台的「立即续期」")

                self._set(message="正在读取商品列表…")
                goods = list_goods(page, start, end)
                if not goods:
                    raise RuntimeError("该区间没有商品数据（页面口径：这段日期内无 CPS 成交）")
                self._set(total=len(goods), message=f"共 {len(goods)} 个 SKU，正在逐个取数…")

                for i, g in enumerate(goods, 1):
                    if self._cancel.is_set():
                        break
                    sku = int(g["itemId"])
                    try:
                        od = sku_metrics(page, sku, start, end)
                    except Exception as exc:  # noqa: BLE001
                        od = {}
                        self._set(message=f"{sku} 取数失败（{type(exc).__name__}），已跳过")
                    rec = {"skuId": sku, "itemName": g.get("itemName")}
                    for k, _, _ in FIELDS:
                        rec[k] = od.get(k)
                    rows.append(rec)
                    self._set(done=i, message=f"已完成 {i}/{len(goods)}：{sku} "
                                              f"付款佣金 {rec.get('payCommAmount') or 0:,.2f} 元")
                    _sleep(DELAY_SKU)

                if not self._cancel.is_set():
                    self._set(message="正在取账户分日合计…")
                    try:
                        daily = daily_total(page, start, end)
                    except Exception:  # noqa: BLE001
                        daily = []
            finally:
                try:
                    ctx and ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pw and pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                lock.release()

            cancelled = self._cancel.is_set()
            if not rows:
                if cancelled:
                    self._set(running=False, cancelled=True, finishedAt=settings.now_iso(),
                              message="已取消（未取到数据）", error=None)
                    return
                raise RuntimeError("没有抓到任何 SKU 数据")

            rows.sort(key=lambda r: (r.get("payCommAmount") or -1), reverse=True)
            label = settings.get_account(account).get("label") or account

            payload = {"jobId": job_id, "account": account, "accountLabel": label,
                       "start": start, "end": end, "rows": rows, "daily": daily,
                       "finishedAt": settings.now_iso()}
            self._results[job_id] = payload
            try:
                jf, _ = self._disk_paths(account, start, end)
                jf.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

            total_pay = round(sum((r.get("payCommAmount") or 0) for r in rows), 2)
            self._set(running=False, cancelled=cancelled, finishedAt=settings.now_iso(),
                      message=(f"{'已取消，' if cancelled else ''}完成：{len(rows)} 个 SKU，"
                               f"付款佣金合计 {total_pay:,.2f} 元"), error=None)
        except Exception as exc:  # noqa: BLE001
            self._set(running=False, error=f"{type(exc).__name__}: {exc}",
                      finishedAt=settings.now_iso(), message=f"失败：{exc}")
