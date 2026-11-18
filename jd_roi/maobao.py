# -*- coding: utf-8 -*-
"""京东毛保（毛利保证）测算。

口径（与利润页「SKU 明细」同一份数据）：
    平均前台价     利润行的 frontPrice（商智成交额 ÷ 成交件数）
    扣点后采购价   利润行的 supply（ERP 到手结算价，已扣点）
    京东毛利/件    = 平均前台价 − 扣点后采购价
    京东毛利率     = Σ 成交件数 × 京东毛利/件 ÷ Σ 成交件数 × 扣点后采购价
    触发毛保       = 毛利率 < 该温层的毛保线
    需补利润       = 毛保线 × 总结算采购额 − 实际毛利额（触发时，京东要向我们要回的差额）

雨润禾（账号 key=b）：常温品 10%、冷冻品 18%。
温层判定：先查已登记的 SKU；未登记的按商品名关键词判断，默认常温。
"""
from __future__ import annotations

# 雨润禾的毛保线：低于该毛利率，京东会要求补到这条线
ACCOUNT_RULES = {
    "b": {"normal": 0.10, "frozen": 0.18},   # 账号 key
}
LABEL_RULES = {
    "雨润禾": {"normal": 0.10, "frozen": 0.18},
}

TEMP_LABEL = {"frozen": "冷冻品", "normal": "常温品"}

# 图片里已登记的两个温层（SKU 是精确身份，商品名里可能同时出现「玉米粒」等词）
SKU_TEMP = {
    # 半加工 · 郑晓静 → 冷冻
    "100012397626": "frozen",   # 玉米粒1000g（速冻）
    "100006799721": "frozen",   # 欧式杂菜1000g
    "100066081615": "frozen",   # 毛豆粒1000g
    "100063434620": "frozen",   # 甜青豆粒1000g
    "100063434664": "frozen",   # 脱皮玉米粒500g
    "100304983034": "frozen",   # 纳豆167.1g
    "100234746567": "frozen",   # 冷冻细薯条
    # 半加工 · 叶梅 → 常温
    "100024877397": "normal",   # 白拇指玉米
    "100024227294": "normal",   # 紫拇指玉米
    "100051680966": "normal",   # 玉米段1.2kg
    "100012495050": "normal",   # 甜玉米粒1000g
    "100244806952": "normal",   # 花糯玉米3斤礼盒装
    "100296324589": "normal",   # 白拇指玉米2斤礼盒
    "100172098848": "normal",   # 七彩冻干花生228g
    "100306607178": "normal",   # 冻干黑花生228g
}

# 未登记 SKU 的兜底：先看「冻干」（常温），再看冷冻词，都没有按常温
NORMAL_WORDS = ("冻干",)
FROZEN_WORDS = ("冷冻", "速冻", "解冻", "薯条", "什锦蔬菜", "欧式杂菜",
                "毛豆", "青豆", "纳豆")


def rules_for(account_key: str | None = None, account_label: str | None = None) -> dict | None:
    """该账号的毛保线；没配置返回 None（只出毛利，不判定触发）。"""
    key = str(account_key or "")
    if key in ACCOUNT_RULES:
        return dict(ACCOUNT_RULES[key])
    label = str(account_label or "")
    if label in LABEL_RULES:
        return dict(LABEL_RULES[label])
    return None


def temp_of(sku_id, item_name: str = "") -> str:
    sid = str(sku_id if sku_id is not None else "")
    if sid in SKU_TEMP:
        return SKU_TEMP[sid]
    name = str(item_name or "")
    if any(w in name for w in NORMAL_WORDS):
        return "normal"
    if any(w in name for w in FROZEN_WORDS):
        return "frozen"
    return "normal"


def page_rules(accounts: list[dict]) -> dict:
    """传给页面的规则（账号 key → 毛保线），以及温层判定表。"""
    rules = {}
    for a in accounts or []:
        r = rules_for(a.get("key"), a.get("label"))
        if r:
            rules[str(a.get("key"))] = r
    return {
        "rules": rules,
        "skuTypes": dict(SKU_TEMP),
        "normalWords": list(NORMAL_WORDS),
        "frozenWords": list(FROZEN_WORDS),
        "tempLabels": dict(TEMP_LABEL),
    }


def diagnose(rows: list[dict], account_key: str | None = None,
             account_label: str | None = None) -> dict:
    """按温层汇总毛利并判定毛保；返回可直接渲染的字典。"""
    rules = rules_for(account_key, account_label)
    groups = {
        "frozen": {"temp": "frozen", "label": TEMP_LABEL["frozen"], "qty": 0,
                   "sale": 0.0, "settle": 0.0, "profit": 0.0, "items": []},
        "normal": {"temp": "normal", "label": TEMP_LABEL["normal"], "qty": 0,
                   "sale": 0.0, "settle": 0.0, "profit": 0.0, "items": []},
    }
    skipped: list[dict] = []
    for r in rows or []:
        try:
            qty = int(r.get("qty") or 0)
            front = float(r.get("frontPrice") or 0)
            supply = float(r.get("supply") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        item = {"skuId": r.get("skuId"), "itemName": r.get("itemName") or "",
                "qty": qty, "frontPrice": round(front, 2), "supply": round(supply, 2)}
        if front <= 0 or supply <= 0:
            item["reason"] = "缺平均前台价" if front <= 0 else "缺扣点后采购价"
            skipped.append(item)
            continue
        temp = temp_of(r.get("skuId"), r.get("itemName") or "")
        sale = qty * front
        settle = qty * supply
        profit = sale - settle
        item.update({"temp": temp, "unitProfit": round(front - supply, 2),
                     "sale": round(sale, 2), "settle": round(settle, 2),
                     "profit": round(profit, 2),
                     "margin": (profit / settle) if settle else None})
        g = groups[temp]
        g["qty"] += qty
        g["sale"] += sale
        g["settle"] += settle
        g["profit"] += profit
        g["items"].append(item)

    out_groups = []
    for key in ("frozen", "normal"):
        g = groups[key]
        margin = (g["profit"] / g["settle"]) if g["settle"] else None
        threshold = (rules or {}).get(key)
        triggered = bool(threshold is not None and margin is not None and margin < threshold)
        shortfall = max(0.0, threshold * g["settle"] - g["profit"]) if triggered else 0.0
        g.update({
            "qty": g["qty"], "sale": round(g["sale"], 2),
            "settle": round(g["settle"], 2), "profit": round(g["profit"], 2),
            "margin": margin, "threshold": threshold,
            "triggered": triggered, "shortfall": round(shortfall, 2),
        })
        g["items"].sort(key=lambda x: -(x.get("profit") or 0))
        out_groups.append(g)

    total = {
        "qty": sum(g["qty"] for g in out_groups),
        "sale": round(sum(g["sale"] for g in out_groups), 2),
        "settle": round(sum(g["settle"] for g in out_groups), 2),
        "profit": round(sum(g["profit"] for g in out_groups), 2),
    }
    total["margin"] = (total["profit"] / total["settle"]) if total["settle"] else None
    return {"ok": True, "rules": rules, "groups": out_groups,
            "total": total, "skipped": skipped}
