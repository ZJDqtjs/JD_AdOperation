# -*- coding: utf-8 -*-
import sys
from pathlib import Path
import csv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jd_roi import dailystore


def as_rows(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "list", "rows", "result", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
        vals = [v for v in payload.values() if isinstance(v, dict)]
        if vals:
            return vals
    return []


def fnum(v):
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except Exception:
        return 0.0


accounts = ["main", "b"]
rows = []

for acc in accounts:
    days = sorted(dailystore.days_for(acc, "jzt_campaign"))
    last20 = days[-20:]
    for day in last20:
        payload = dailystore.load_day(acc, day, "jzt_campaign")
        cost = 0.0
        order_sum = 0.0
        for r in as_rows(payload):
            cost += fnum(r.get("cost"))
            order_sum += fnum(r.get("totalOrderSum"))
        roi = order_sum / cost if cost else None
        rows.append({
            "account": acc,
            "date": day,
            "cost": round(cost, 2),
            "totalOrderSum": round(order_sum, 2),
            "roi": round(roi, 4) if roi is not None else "",
        })

out = Path("data/agent/daily_cost_roi_20d.csv")
out.parent.mkdir(parents=True, exist_ok=True)

with out.open("w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=["account", "date", "cost", "totalOrderSum", "roi"])
    writer.writeheader()
    writer.writerows(rows)

print(str(out))
with out.open("r", encoding="utf-8-sig") as f:
    for i, line in enumerate(f):
        if i < 4:
            print(line.rstrip())
