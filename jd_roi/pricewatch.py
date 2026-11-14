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

from . import dailystore as ds
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
