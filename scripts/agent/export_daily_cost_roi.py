# -*- coding: utf-8 -*-
"""导出 main / b 两账号最近 20 天（本地已有数据）的每日总花费与整体 ROI 到 data/agent/。"""
import csv
import json
from pathlib import Path

from jd_roi import dailystore, settings

KIND = "jzt_campaign"  # 京准通概览-计划：全广告口径（快车+智能+全站）
ACCOUNTS = ["main", "b"]


def _f(v, d=0.0):
    try:
        if v is None or v == "":
            return d
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return d


def rows_of(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("rows", "data", "list", "result", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for kk in ("rows", "list", "data"):
                    if isinstance(v.get(kk), list):
                        return v[kk]
    return []


def pick(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return _f(row[n])
    return 0.0


def day_agg(account, day):
    rows = rows_of(dailystore.load_day(account, day, KIND))
    cost = sum(pick(r, ["cost", "totalCost", "spend"]) for r in rows)
    gmv = sum(pick(r, ["totalOrderSum", "totalPayAmount", "payAmount", "orderSum"]) for r in rows)
    return cost, gmv, len(rows)


days = {a: set(dailystore.days_for(a, KIND)) for a in ACCOUNTS}
common = sorted(set.intersection(*days.values()))
sel = common[-20:]
print("common_days=%d selected=%s ~ %s (n=%d)" % (len(common), sel[0], sel[-1], len(sel)))

# 打印一天样本字段，校验字段名
sample = rows_of(dailystore.load_day("main", sel[-1], KIND))
print("sample_keys=%s" % (sorted(sample[0].keys()) if sample else []))
print("sample_row=%s" % json.dumps(sample[0], ensure_ascii=False)[:400] if sample else "empty")

out_dir = Path(getattr(settings, "DATA_DIR", "data")) / "agent"
out_dir.mkdir(parents=True, exist_ok=True)
out = out_dir / "daily_cost_roi_2accounts_20d.csv"

with out.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["date", "main_cost", "main_gmv", "main_roi",
                "b_cost", "b_gmv", "b_roi",
                "total_cost", "total_gmv", "total_roi"])
    for d in sel:
        rec = [d]
        tc = tg = 0.0
        for a in ACCOUNTS:
            c, g, _n = day_agg(a, d)
            tc += c
            tg += g
            rec += [round(c, 2), round(g, 2), (round(g / c, 2) if c > 0 else "")]
        rec += [round(tc, 2), round(tg, 2), (round(tg / tc, 2) if tc > 0 else "")]
        w.writerow(rec)

print("FILE=%s" % out)
print("--- head ---")
lines = out.read_text(encoding="utf-8-sig").splitlines()
for ln in lines[:4]:
    print(ln)
