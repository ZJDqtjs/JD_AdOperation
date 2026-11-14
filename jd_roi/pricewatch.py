# -*- coding: utf-8 -*-
"""SKU 前台价（商智成交件单价）按天走势。

口径：前台件单价 = 商智「商品明细」成交金额 ÷ 成交件数
      （indicators.SZ_AMT / SZ_QTY，消费者实付，与月度利润页 frontPrice 同一口径）。

数据只读本地按天入库的 sz_product（data/daily/<账号>/<日期>/sz_product.json.gz），
本模块不发任何请求。缺文件的天、文件在但 0 行的天（商智抓早了）都会在返回里点名，
页面据此提示，避免把「没数据」误读成「没成交」。

为什么不用商品页标价：商智不提供标价字段；成交额 ÷ 件数已经含了优惠券/满减后的
真实成交价，能反映前台价变化。代价是「有成交才有价」——某天 0 件就是那天没有价，
曲线在那天断开（前端画成缺口，不做插值）。
"""
from __future__ import annotations

import json
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path

from . import dailystore as ds, settings
from .indicators import SZ_AMT, SZ_ORDN, SZ_QTY, SZ_UV


def _f(v) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _price(amt: float, qty: float):
    """成交金额 ÷ 成交件数；件数为 0（或金额为 0）时没有价，返回 None。"""
    if qty <= 0 or amt <= 0:
        return None
    return round(amt / qty, 2)


def _pct(cur, base):
    if not base or cur is None:
        return None
    return round((cur - base) / base * 100, 2)


def build(account: str, start: str, end: str) -> dict:
    """区间内每个 SKU 的逐日前台价 + 区间汇总。"""
    start, end = ds.norm_day(start), ds.norm_day(end)
    days = ds.day_span(start, end)

    missing: list = []       # 这天没文件（从未抓过）
    empty: list = []         # 文件在但 0 行（抓早了 / 接口成功但空）
    data_days: list = []     # 真正有商品明细的天
    per: dict = {}

    for day in days:
        payload = ds.load_day(account, day, "sz_product")
        if payload is None:
            missing.append(day)
            continue
        rows = payload.get("rows") or []
        if not rows:
            empty.append(day)
            continue
        data_days.append(day)
        for r in rows:
            if r.get("$summary"):
                continue
            sid = str(r.get("sku_id") or "").strip()
            if not sid or sid == "合计":
                continue
            e = per.get(sid)
            if e is None:
                e = {"skuId": sid, "itemName": "", "img": "", "url": "", "byDay": {}}
                per[sid] = e
            if r.get("name"):
                e["itemName"] = r.get("name")
            if not e["img"] and r.get("img_src"):
                e["img"] = r.get("img_src")
            if not e["url"] and r.get("pro_url"):
                e["url"] = r.get("pro_url")
            e["byDay"][day] = {"amt": _f(r.get(SZ_AMT)), "qty": _f(r.get(SZ_QTY)),
                               "uv": _f(r.get(SZ_UV)), "ord": _f(r.get(SZ_ORDN))}

    rows_out: list = []
    tot_amt = tot_qty = tot_uv = 0.0
    for e in per.values():
        spark, spark_qty, valid = [], [], []
        amt = qty = uv = orders = 0.0
        for day in days:
            v = e["byDay"].get(day)
            if v:
                amt += v["amt"]
                qty += v["qty"]
                uv += v["uv"]
                orders += v["ord"]
            p = _price(v["amt"], v["qty"]) if v else None
            spark.append(p)
            spark_qty.append(int(v["qty"]) if v else 0)
            if p is not None:
                valid.append((day, p))
        if not valid:
            continue          # 区间内一天都没成交：没有价，也不该出现在价格表里

        prices = [p for _d, p in valid]
        last_day, last_price = valid[-1]
        first_day, first_price = valid[0]
        prev_price = valid[-2][1] if len(valid) > 1 else None
        row = {
            "skuId": e["skuId"], "itemName": e["itemName"],
            "img": e["img"], "url": e["url"],
            "priceLatest": last_price, "latestDay": last_day,
            "pricePrev": prev_price,
            "changeDod": (round(last_price - prev_price, 2)
                          if prev_price is not None else None),
            "changeDodPct": _pct(last_price, prev_price),
            "priceFirst": first_price, "firstDay": first_day,
            "change": round(last_price - first_price, 2),
            "changePct": _pct(last_price, first_price),
            "priceAvg": round(amt / qty, 2),
            "priceMin": min(prices), "priceMax": max(prices),
            "swingPct": _pct(max(prices), min(prices)),
            "amt": round(amt, 2), "qty": int(qty), "orders": int(orders),
            "uv": int(uv), "priceDays": len(prices),
            "spark": spark, "sparkQty": spark_qty,
        }
        rows_out.append(row)
        tot_amt += amt
        tot_qty += qty
        tot_uv += uv

    rows_out.sort(key=lambda r: r["amt"], reverse=True)
    return {
        "ok": True,
        "account": account,
        "start": start, "end": end,
        "days": days,
        "daysWithData": data_days,
        "missingDays": missing,
        "emptyDays": empty,
        "rows": rows_out,
        "totals": {
            "skuCount": len(rows_out),
            "dataDays": len(data_days),
            "rangeDays": len(days),
            "missing": len(missing),
            "empty": len(empty),
            "amt": round(tot_amt, 2),
            "qty": int(tot_qty),
            "uv": int(tot_uv),
            # 全店加权均价：Σ成交金额 ÷ Σ成交件数（不是各 SKU 均价的平均）
            "avgPrice": round(tot_amt / tot_qty, 2) if tot_qty else None,
        },
    }


# ---------------------------------------------------------------- 夜间自动核算
# 凌晨抓完昨天数据后，把「这一天」的逐 SKU 前台价和相对上一个有价日的变动算好落盘，
# 第二天打开价格页就能直接看到摘要，不用再自己挑区间、也不会因为商智回补而看不出来。
# 为什么存文件而不是每次现算：抓取进程和 Web 进程是两个进程，页面需要能看到
# 「夜间到底核算过没有、核的是哪一天」，文件本身就是这条记录（也方便回看）。
SNAP_SUBDIR = "price"
_SNAP_KEYS = ("skuId", "itemName", "priceLatest", "pricePrev", "changeDod",
              "changeDodPct")


def _snap_dir() -> Path:
    d = settings.DATA_DIR / SNAP_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def snap_path(account: str, day: str) -> Path:
    return _snap_dir() / f"{account}_{ds.norm_day(day)}.json"


def snapshot(account: str, day: str, lookback: int = 30) -> dict:
    """核算某一天：当日有成交的 SKU，以及每个 SKU 相对自己上一个有价日的变动。"""
    day = ds.norm_day(day)
    start = (_date.fromisoformat(day)
             - _timedelta(days=max(int(lookback), 2) - 1)).isoformat()
    data = build(account, start, day)
    # build() 里的 amt/qty 是**整个区间**的合计，摘要要的是「那一天」的量，单独读一次当天文件
    day_amt: dict = {}
    day_qty: dict = {}
    for r in (ds.load_day(account, day, "sz_product") or {}).get("rows") or []:
        if r.get("$summary"):
            continue
        sid = str(r.get("sku_id") or "").strip()
        if not sid or sid == "合计":
            continue
        day_amt[sid] = day_amt.get(sid, 0.0) + _f(r.get(SZ_AMT))
        day_qty[sid] = day_qty.get(sid, 0.0) + _f(r.get(SZ_QTY))

    rows = []
    for r in data["rows"]:
        if r["latestDay"] != day:
            continue
        item = {k: r[k] for k in _SNAP_KEYS}
        item["amt"] = round(day_amt.get(r["skuId"], 0.0), 2)
        item["qty"] = int(day_qty.get(r["skuId"], 0.0))
        rows.append(item)
    amt = sum(day_amt.values())
    qty = sum(day_qty.values())
    up = sum(1 for r in rows if (r["changeDodPct"] or 0) > 0.5)
    down = sum(1 for r in rows if (r["changeDodPct"] or 0) < -0.5)
    return {
        "account": account,
        "label": settings.get_account(account).get("label") or account,
        "day": day,
        "generatedAt": settings.now_iso(),
        "skuCount": len(rows),
        "up": up,
        "down": down,
        "amt": round(amt, 2),
        "qty": int(qty),
        "avgPrice": round(amt / qty, 2) if qty else None,
        "rows": rows,
    }


def save_snapshot(snap: dict) -> Path:
    p = snap_path(snap["account"], snap["day"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def latest_snapshot(account: str):
    """该账号最近一次核算（按日期取最新，不按文件时间，避免历史重算抢镜）。"""
    d = settings.DATA_DIR / SNAP_SUBDIR
    if not d.exists():
        return None
    files = sorted(d.glob(f"{account}_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_nightly(accounts=None, days_back: int = 3, log=None) -> list:
    """夜间抓取成功后调用：核算最近几个「有商智数据」的日前台价并落盘。

    算的不只是最新一天：商智 T+1 只有一半、T+2~T+3 才定稿，夜里会回刷最近 15 天，
    所以把最近 3 个有数据的天一起重算覆盖，历史那两天会逐步修正成定稿值。
    """
    out = []
    for acc in (accounts or settings.ACCOUNTS):
        key = acc.get("key") if isinstance(acc, dict) else str(acc)
        # 只回看最近 10 天：再往前不会在本轮回刷，没必要每天读一遍
        recent = ds.days_for(key, "sz_product")[-10:]
        days = [d for d in recent
                if ds.payload_rows(ds.load_day(key, d, "sz_product")) > 0][-max(days_back, 1):]
        for day in days:
            snap = snapshot(key, day)
            save_snapshot(snap)
            if log:
                log(f"[{snap['label']}] 前台价已核算 {day}：{snap['skuCount']} 个 SKU"
                    f"（涨 {snap['up']} / 跌 {snap['down']}）")
            out.append({k: snap[k] for k in
                        ("account", "label", "day", "skuCount", "up", "down", "avgPrice")})
    return out
