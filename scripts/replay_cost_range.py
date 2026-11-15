# -*- coding: utf-8 -*-
"""离线复演：用线上真实成本表与 10 月利润缓存，验证「按区间重算毛利」在真实数据上的效果。

只读临时副本（.costprobe），不发任何接口请求、不碰线上数据目录。
"""
import json
import glob
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jd_roi import analyze2, profit, settings  # noqa: E402

DATA = Path(os.environ.get("JD_DATA_DIR", "/probe"))
costs = json.loads((DATA / "costs_cache.json").read_text(encoding="utf-8"))
q = costs.get("_query") or {}
age_h = int((time.time() - float(costs.get("_fetched_at") or 0)) / 3600)
print(f"线上成本表：实取区间 {q.get('date_from') or '—'} ~ {q.get('date_to') or '—'}"
      f"｜updatedAt {costs.get('updatedAt')}｜SKU {len(costs.get('skus') or {})} 个"
      f"｜拉取于 {age_h} 小时前")

files = sorted(glob.glob(str(DATA / "profit_2026-10-*.json")))
rows_by_file = {}
for f in files:
    d = json.loads(Path(f).read_text(encoding="utf-8"))
    rows_by_file[Path(f).name] = d
    t = d.get("totals") or {}
    print(f"  {Path(f).name}: 毛利 {t.get('gross')}｜到手 {t.get('arrival')}｜"
          f"costUpdatedAt {d.get('costUpdatedAt')}｜savedAt {d.get('savedAt')}"
          f"｜costRange {d.get('costRange') or '（旧结果没记）'}")

# 同一个 SKU 在不同区间的缓存里成本口径是否一致（不一致就是混档的证据）
sig: dict[str, dict] = {}
for name, d in rows_by_file.items():
    for r in d.get("rows") or []:
        sig.setdefault(str(r["skuId"]), {})[name] = (r.get("supply"), r.get("grossPerOrder"))
multi = [v for v in sig.values() if len(v) > 1]
mixed = [(sid, v) for sid, v in sig.items() if len(set(v.values())) > 1]
print(f"\n出现在多份缓存里的 SKU {len(multi)} 个，其中同一 SKU 的 supply/毛利单价"
      f"在不同区间不一致的：{len(mixed)} 个")
for sid, v in mixed[:4]:
    print(f"  SKU {sid}: " + "；".join(f"{n.split('profit_')[-1]} → 到手 {a}/贡献 {g}"
                                       for n, (a, g) in v.items()))

# 关键验证：每份缓存都按线上成本表（10/1~10/29 口径）重算一遍，看毛利被纠正多少
for name in files:
    base = json.loads(Path(name).read_text(encoding="utf-8"))
    before = dict(base.get("totals") or {})
    after = json.loads(json.dumps(base))       # 副本上重算，不改临时目录以外的东西
    changed = profit.reprice_payload(after, costs)
    ta = after.get("totals") or {}
    amt_b = round(sum(float(r.get("szAmt") or 0) for r in base.get("rows") or []), 2)
    amt_a = round(sum(float(r.get("szAmt") or 0) for r in (after.get("rows") or [])), 2)
    print(f"\n重算 {Path(name).name}（{base.get('start')} ~ {base.get('end')}）：影响 {changed} 行")
    print(f"  毛利 {before.get('gross')} → {ta.get('gross')}"
          f"（差 {round((ta.get('gross') or 0) - (before.get('gross') or 0), 2)}）"
          f"｜到手 {before.get('arrival')} → {ta.get('arrival')}")
    print(f"  不动的量：件数 {before.get('qty')} → {ta.get('qty')}｜成交额 {amt_b} → {amt_a}"
          f"｜CPS {before.get('cpsCost')} → {ta.get('cpsCost')}"
          f"｜京准通 {before.get('jztCost')} → {ta.get('jztCost')}"
          f"｜行数 {len(base.get('rows') or [])} → {len(after.get('rows') or [])}")
    assert (ta.get("qty"), amt_a, ta.get("cpsCost"), ta.get("jztCost")) == \
        (before.get("qty"), amt_b, before.get("cpsCost"), before.get("jztCost")), \
        "重算不该动件数/成交额/花费"
    assert after.get("costRange") == [q.get("date_from"), q.get("date_to")], after.get("costRange")

print("\n离线复演 OK：累加量一字未改，到手与毛利统一到成本表所属区间口径")
