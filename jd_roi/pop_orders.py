# -*- coding: utf-8 -*-
"""POP 店订单：按天缓存京东商家后台「订单列表」，供 /pop 页面筛选统计。

数据源
  https://shop.jd.com/jdm/trade/orders/order-list?tabType=allOrders
  页面背后的接口是 sff.jd.com 的 orderListBffService.queryOrderPage，
  请求头带 h5st 签名（由页面 JS 现场生成）。签名无法自己拼，所以这里用
  Playwright 打开真实页面，拦截这次请求并改写「下单时间 / 页码 / 每页条数」，
  让浏览器带着自己的签名发出去。实测改写 body 不会破坏签名校验
  （pageSize > 50 会被接口自己拒掉，说明服务端确实读到了改后的 body）。

落盘
  data/daily/<账号key>/<YYYY-MM-DD>/pop_order.json.gz
    {"rows": [订单...], "dateStart": day, "dateEnd": day,
     "fetchedAt": "...", "totalItem": N}

口径
  · 一天 = 下单时间（orderCreateTime）落在该日 00:00:00 ~ 23:59:59 的全部订单；
  · 「单量」按订单号计（一个订单含多个 SKU 仍是 1 单），件数按 skuTotalNum 累加；
  · 金额取 orderPaymentInfo.shouldPay（实付），未付款/异常时退回 orderSum。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from typing import Callable

from . import browser, dailystore as ds, settings

KIND = "pop_order"
ORDER_PAGE_URL = "https://shop.jd.com/jdm/trade/orders/order-list?tabType=allOrders"
_QUERY_RE = re.compile(r".*orderListBffService\.queryOrderPage.*")
PAGE_SIZE = 50                 # 接口最多 50 条/页（再大直接返回 201）

_JOB_LOCK = threading.Lock()
_JOB: dict = {"running": False, "startedAt": "", "finishedAt": "", "message": "",
              "error": None, "result": None, "log": []}


def _f(v) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _norm_time(v: str, default: str) -> str:
    """把 16:00 / 16:00:00 统一成 HH:MM:SS；空值或非法值用 default。"""
    s = str(v or "").strip()
    if not s:
        return default
    parts = s.split(":")
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3:
        return default
    try:
        h, m, sec = (int(x) for x in parts)
    except ValueError:
        return default
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
        return default
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _fmt_ts(ms) -> str:
    """接口给毫秒时间戳，按业务时区（Asia/Shanghai）格式化成字符串。"""
    try:
        ts = float(ms) / 1000.0
        return dt.datetime.fromtimestamp(ts, settings.now().tzinfo).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def account(key: str | None = None) -> dict | None:
    """取 POP 订单账号：优先显式 key，否则取第一个「只看订单」的账号。"""
    if key:
        acc = settings.get_account(key)
        return acc if settings.is_order_account(acc) else None
    got = settings.order_accounts()
    return got[0] if got else None


def _accounts(key: str | None = None) -> list:
    if key:
        acc = account(key)
        return [acc] if acc else []
    return settings.order_accounts()


def normalize(order: dict) -> dict:
    """把接口订单压缩成页面/统计需要的字段，原始订单不再落盘（体积太大）。"""
    items = []
    for it in (order.get("orderItems") or []):
        items.append({
            "skuId": str(it.get("skuId") or ""),
            "name": it.get("skuName") or "",
            "num": _i(it.get("num")),
            "jdPrice": round(_f(it.get("jdPrice")), 2),
            "supplyPrice": round(_f(it.get("supplyPrice")), 2),
            "img": ("https:" + str(it.get("imgUrl"))) if str(it.get("imgUrl") or "").startswith("//")
                   else (it.get("imgUrl") or ""),
        })
    pay = order.get("orderPaymentInfo") or {}
    status = order.get("orderStatusInfo") or {}
    cons = order.get("orderConsigneeInfo") or {}
    amount = _f(pay.get("shouldPay"))
    if amount <= 0:
        amount = _f(pay.get("orderSum"))
    total_num = _i(order.get("skuTotalNum")) or sum(x["num"] for x in items)
    created = _fmt_ts(order.get("orderCreateTime"))
    return {
        "orderId": str(order.get("orderId") or ""),
        "orderTime": created,
        "day": created[:10],
        "status": status.get("orderStatusName") or "",
        "statusCode": _i(status.get("orderStatus")),
        "amount": round(amount, 2),
        "orderSum": round(_f(pay.get("orderSum")), 2),
        "freight": round(_f(pay.get("freight")), 2),
        "commission": round(_f(pay.get("commission")), 2),
        "items": items,
        "itemCount": len(items),
        "skuTotalNum": total_num,
        "userPin": order.get("userPin") or "",
        "consName": cons.get("consName") or "",
        "skuText": " ".join(x["name"] for x in items if x.get("name")),
        "img": items[0]["img"] if items else "",
        "parentId": str(order.get("parentId") or ""),
    }


class Scraper:
    """在一张已登录的页面上改写 queryOrderPage 请求，按天翻页取订单。"""

    def __init__(self, page, log: Callable[[str], None] = print):
        self.page = page
        self.log = log
        self._want = {"day": "", "current": 1}
        page.route(_QUERY_RE, self._route)

    def _route(self, route) -> None:
        try:
            body = json.loads(route.request.post_data or "{}")
            data = ((body.get("request") or {}).get("data")) or {}
            data["orderCreateTimeBegin"] = f"{self._want['day']} 00:00:00"
            data["orderCreateTimeEnd"] = f"{self._want['day']} 23:59:59"
            data["orderTab"] = "allOrders"
            data["sortMode"] = "orderDateDesc"
            data["current"] = int(self._want["current"])
            data["pageSize"] = PAGE_SIZE
            body.setdefault("request", {})["data"] = data
            route.continue_(post_data=json.dumps(body, ensure_ascii=False))
        except Exception:  # noqa: BLE001  改不动就按原请求发（至少不阻塞页面）
            route.continue_()

    def _query(self, day: str, current: int, first: bool) -> dict:
        self._want.update(day=day, current=current)
        try:
            with self.page.expect_response(
                    lambda r: "queryOrderPage" in r.url, timeout=90000) as info:
                if first:
                    self.page.goto(ORDER_PAGE_URL, wait_until="domcontentloaded", timeout=90000)
                else:
                    self.page.reload(wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:  # noqa: BLE001
            if "passport.jd.com" in self.page.url:
                raise RuntimeError("登录态已失效，请到 /login 用该账号重新扫码") from exc
            raise RuntimeError(f"订单列表请求失败：{type(exc).__name__}") from exc
        return info.value.json()

    def fetch_day(self, day: str) -> tuple[list, int]:
        day = ds.norm_day(day)
        rows: list = []
        total = 0
        current = 1
        first = True
        while True:
            try:
                payload = self._query(day, current, first)
            finally:
                first = False
            code = payload.get("code")
            if code != 200:
                hint = ""
                if "passport.jd.com" in self.page.url:
                    hint = "；登录态已失效，请到 /login 用该账号重新扫码"
                raise RuntimeError(f"订单接口返回 {code}: {payload.get('msg')}{hint}")
            data = payload.get("data") or {}
            total = _i(data.get("totalItem"))
            got = data.get("results") or []
            rows.extend(got)
            if len(got) < PAGE_SIZE or len(rows) >= total:
                break
            current += 1
            if current > 200:
                self.log(f"  {day} 超过 200 页，提前结束")
                break
            self.page.wait_for_timeout(400)
        return rows, total


def _log(msg: str) -> None:
    print(msg, flush=True)
    with _JOB_LOCK:
        _JOB["log"] = (_JOB["log"] + [msg])[-200:]


def status() -> dict:
    with _JOB_LOCK:
        out = dict(_JOB)
        out["log"] = list(_JOB["log"])[-60:]
        return out


def _fetch_days(acc: dict, days: list, log: Callable[[str], None]) -> list:
    """同步抓取若干天并落盘；返回每天的结果摘要。"""
    key = acc["key"]
    label = acc.get("label") or key
    pw = ctx = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            pw, ctx = browser.launch_persistent(headless=settings.HEADLESS, account=key)
            break
        except Exception as exc:  # noqa: BLE001  Profile 可能正被保活线程占用
            last_err = exc
            log(f"[{label}] 打开浏览器失败（{type(exc).__name__}），"
                f"{20 * (attempt + 1)} 秒后重试…")
            time.sleep(20 * (attempt + 1))
    if ctx is None:
        raise RuntimeError(f"无法打开浏览器：{type(last_err).__name__}: {last_err}")
    saved = []
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        scraper = Scraper(page, log=log)
        for idx, day in enumerate(days, 1):
            log(f"[{label}] 抓取订单 {day}（{idx}/{len(days)}）…")
            raw, total = scraper.fetch_day(day)
            rows = [normalize(o) for o in raw]
            ds.save_day(key, day, KIND, {
                "rows": rows, "dateStart": day, "dateEnd": day,
                "fetchedAt": settings.now_str(), "totalItem": total,
            })
            saved.append({"day": day, "orders": len(rows), "totalItem": total})
            log(f"[{label}] {day} 订单 {len(rows)} 单 / {sum(r['skuTotalNum'] for r in rows)} 件")
    finally:
        try:
            ctx and ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw and pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return saved


def _run(days: list, key: str | None, log: Callable[[str], None] | None) -> None:
    started = settings.now_iso()

    def emit(msg: str) -> None:
        """任务日志给页面轮询看，同时转发给调用方（夜间任务写任务进度）。"""
        _log(msg)
        if log is not None:
            log(msg)

    with _JOB_LOCK:
        _JOB.update({"running": True, "startedAt": started, "finishedAt": "",
                     "message": f"抓取 {len(days)} 天订单", "error": None,
                     "result": None, "log": []})
    try:
        acc = account(key)
        if not acc:
            raise RuntimeError("没有配置 POP 订单账号：在 accounts.json 给该账号加 "
                               '"orders_only": true，或账号名里带 pop')
        done = _fetch_days(acc, days, emit)
        with _JOB_LOCK:
            _JOB.update({"running": False, "finishedAt": settings.now_iso(),
                         "message": f"完成：{len(done)} 天", "result": done})
        emit(f"[{acc.get('label') or acc['key']}] 订单抓取完成：{len(done)} 天")
    except Exception as exc:  # noqa: BLE001
        with _JOB_LOCK:
            _JOB.update({"running": False, "finishedAt": settings.now_iso(),
                         "message": "失败", "error": f"{type(exc).__name__}: {exc}"})
        emit(f"订单抓取失败：{type(exc).__name__}: {exc}")


def start(days: list, key: str | None = None,
          log: Callable[[str], None] | None = None) -> dict:
    """后台抓取指定日期（同一天重复抓会覆盖）。返回启动结果。"""
    days = sorted({ds.norm_day(d) for d in days if str(d).strip()})
    if not days:
        return {"ok": False, "error": "没有要抓取的日期"}
    with _JOB_LOCK:
        if _JOB["running"]:
            return {"ok": False, "error": "订单抓取正在进行，等它结束再试"}
        # 先占位，避免两个请求在同一瞬间都通过上面的检查
        _JOB.update({"running": True, "startedAt": settings.now_iso(), "finishedAt": "",
                     "message": f"抓取 {len(days)} 天订单", "error": None,
                     "result": None, "log": []})
    threading.Thread(target=_run, args=(days, key, log),
                     name="pop-orders", daemon=True).start()
    return {"ok": True, "days": days}


def recent_days() -> list:
    """夜间自动抓取的范围：本地有缓存就回刷最近 N 天，没有就补一大段。"""
    has_cache = bool(cached_days())
    n = settings.POP_ORDER_REFRESH_DAYS if has_cache else settings.POP_ORDER_BACKFILL_DAYS
    end = ds.today()
    start = end - dt.timedelta(days=max(n, 1) - 1)
    return ds.day_span(start.isoformat(), end.isoformat())


def cached_days(key: str | None = None) -> list:
    """已缓存的日期（按天，跨订单账号合并）。"""
    out: dict = {}
    for acc in _accounts(key):
        for day in ds.days_for(acc["key"], KIND):
            pl = ds.load_day(acc["key"], day, KIND) or {}
            rows = pl.get("rows") or []
            cur = out.get(day) or {"day": day, "orders": 0, "fetchedAt": ""}
            cur["orders"] += len(rows)
            stamp = pl.get("fetchedAt") or ""
            if stamp > cur["fetchedAt"]:
                cur["fetchedAt"] = stamp
            out[day] = cur
    return sorted(out.values(), key=lambda x: x["day"])


def _match(order: dict, kw: str, status: str) -> bool:
    if status and order.get("status") != status:
        return False
    if kw:
        hay = f"{order.get('skuText') or ''} {order.get('orderId') or ''}".lower()
        if kw not in hay:
            return False
    return True


def summary(start: str, end: str, keyword: str = "", status: str = "",
            key: str | None = None, start_time: str = "", end_time: str = "") -> dict:
    """读本地缓存，按区间 + 时刻 + 商品名/状态过滤后汇总（纯本地，不发请求）。"""
    start, end = ds.norm_day(start), ds.norm_day(end)
    days = ds.day_span(start, end)
    accs = _accounts(key)
    kw = (keyword or "").strip().lower()
    status = (status or "").strip()
    # 下单时间 = 起日期 + 起时刻 ~ 止日期 + 止时刻（默认两边 16:00:00 就是跨日 24 小时窗口）
    st = _norm_time(start_time, "00:00:00")
    et = _norm_time(end_time, "23:59:59")
    lo, hi = f"{start} {st}", f"{end} {et}"

    per_day: dict = {}
    cached: set = set()
    rows: list = []
    seen_orders: set = set()
    statuses: set = set()
    for acc in accs:
        for day in days:
            pl = ds.load_day(acc["key"], day, KIND)
            if pl is None:
                continue
            cached.add(day)
            for o in (pl.get("rows") or []):
                if not o.get("day"):
                    o["day"] = day
                oid = o.get("orderId")
                if oid in seen_orders:
                    continue
                seen_orders.add(oid)
                if o.get("status"):
                    statuses.add(o["status"])
                # 每日统计也要跟着商品名/状态/时刻筛选走，否则筛了条件、每日单量还是全店
                if not _match(o, kw, status):
                    continue
                created = o.get("orderTime") or ""
                if created < lo or created > hi:
                    continue
                e = per_day.setdefault(day, {"day": day, "orders": 0, "valid": 0,
                                             "items": 0, "amount": 0.0,
                                             "canceled": 0, "unpaid": 0})
                e["orders"] += 1
                e["items"] += _i(o.get("skuTotalNum"))
                e["amount"] += _f(o.get("amount"))
                if o.get("status") == "已取消":
                    e["canceled"] += 1
                elif o.get("status") == "未付款":
                    e["unpaid"] += 1
                else:
                    e["valid"] += 1
                rows.append(o)

    by_day = []
    for day in days:
        e = per_day.get(day) or {"day": day, "orders": 0, "valid": 0, "items": 0,
                                 "amount": 0.0, "canceled": 0, "unpaid": 0}
        e = dict(e)
        e["amount"] = round(e["amount"], 2)
        e["avg"] = round(e["amount"] / e["orders"], 2) if e["orders"] else None
        e["cached"] = day in cached
        by_day.append(e)

    # 商品汇总：一单含多个商品时，该单会计入每个商品（所以商品单量之和 ≥ 总单量）。
    # 接口只给订单级实付，没有单品实付；按 jdPrice×num 的占比把订单实付分摊到商品，
    # 这样商品金额之和 = 订单实付合计，不会拿标价当成交额。
    prod: dict = {}
    for o in rows:
        items = o.get("items") or []
        base = sum(_f(it.get("jdPrice")) * _i(it.get("num")) for it in items)
        for it in items:
            sid = it.get("skuId") or it.get("name") or ""
            if not sid:
                continue
            e = prod.setdefault(sid, {"skuId": it.get("skuId") or "",
                                      "name": it.get("name") or "",
                                      "img": it.get("img") or "",
                                      "orders": 0, "items": 0, "amount": 0.0})
            e["orders"] += 1
            e["items"] += _i(it.get("num"))
            weight = (_f(it.get("jdPrice")) * _i(it.get("num")) / base
                      if base > 0 else (1.0 / len(items) if items else 0))
            e["amount"] += _f(o.get("amount")) * weight
    by_prod = sorted(prod.values(), key=lambda x: x["amount"], reverse=True)
    for e in by_prod:
        e["amount"] = round(e["amount"], 2)

    rows.sort(key=lambda o: (o.get("orderTime") or "", o.get("orderId") or ""), reverse=True)
    tot_orders = sum(1 for o in rows)
    tot_items = sum(_i(o.get("skuTotalNum")) for o in rows)
    tot_amount = round(sum(_f(o.get("amount")) for o in rows), 2)
    return {
        "ok": True,
        "account": accs[0]["key"] if accs else "",
        "label": accs[0].get("label") if accs else "",
        "accounts": [a["key"] for a in accs],
        "start": start, "end": end, "days": days,
        "startTime": st, "endTime": et,
        "cachedDays": len(cached & set(days)),
        "missingDays": [d for d in days if d not in cached],
        "keyword": keyword or "", "status": status,
        "rows": rows,
        "byDay": by_day,
        "byProduct": by_prod,
        "statuses": sorted(statuses),
        "totals": {
            "orders": tot_orders,
            "items": tot_items,
            "amount": tot_amount,
            "avg": round(tot_amount / tot_orders, 2) if tot_orders else None,
            "days": len(days),
            "cachedDays": len(cached & set(days)),
        },
    }
