# -*- coding: utf-8 -*-
"""ERP 成本表按区间隔离 + 拼接结果按本次区间统一重算毛利。

为什么必须有这两条：供货方接口给的是「**该查询区间的均价**」，实测同一个 SKU
的货款成本会随区间从 ~16 元漂到 ~117 元。以前本地只有一份 costs_cache.json，
后查的区间会把先查的那份覆盖掉，于是月度利润表里混着别的日期的成本
（整月毛利一度算成 −103,333，正确约 +81,128）。
1) 每个区间一份缓存，谁也不覆盖谁；本区间没拉过时明说「借用了哪个区间」；
2) 多段拼接 / 断点续抓的结果，件数、成交额、CPS 与京准通花费原样保留，
   到手结算价与毛利用本次区间的成本表整表重算。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jd_roi import analyze2, profit, settings  # noqa: E402


def _table(goods: float, updated: str) -> dict:
    """造一份 ERP 成本表：SKU 1001/1002，前台件单价 100、到手结算价 80、货款成本随区间变。"""
    return {"updatedAt": updated,
            "skus": {str(sid): {"price": 100.0, "supply": 80.0, "_goodsCost": goods,
                               "_name": f"测试商品{sid}"}
                    for sid in (1001, 1002)}}


def _row(sku: int, qty: int, supply: float, gpo: float) -> dict:
    """一条已经算好的利润行（只要累加字段是真的，重算字段故意用旧口径）。"""
    return {"skuId": sku, "itemName": f"测试商品{sku}", "category": "",
            "qty": qty, "szAmt": qty * 100.0, "frontPrice": 100.0,
            "arrival": round(qty * supply, 2), "supply": round(supply, 2),
            "grossPerOrder": round(gpo, 2), "gross": round(qty * gpo, 2),
            "cpsCost": 30.0, "jztCost": 70.0, "roi": 1.0, "adShare": 0.5,
            "net": round(qty * gpo - 100.0, 2), "netRate": 0.0, "costSource": "erp",
            "accts": ["main"], "spendAccts": ["main"]}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="costs-"))
    settings.DATA_DIR = str(tmp)
    settings.COSTS_CACHE_FILE = tmp / "costs_cache.json"
    settings.COSTS_CACHE_DIR = tmp / "costs"
    settings.COSTS_URL = "http://fake-erp/costs"
    settings.COSTS_TTL = 3600.0
    # 成本模型与单位换算都清空：本测试只验「区间隔离 + 重算」，不掺全店口径
    analyze2.load_cost_model = lambda *a, **k: {}
    settings.costs_unit_map = lambda: {}
    calls: list[tuple[str, str]] = []

    def fake_fetch(date_from: str = "", date_to: str = "") -> dict:
        calls.append((date_from, date_to))
        goods = {("2026-10-01", "2026-10-27"): 20.0,
                 ("2026-10-01", "2026-10-29"): 45.0}.get((date_from, date_to), 25.0)
        data = _table(goods, f"{date_to} 02:00")
        data["_fetched_at"] = time.time()
        data["_query"] = {"date_from": date_from, "date_to": date_to}
        return data

    settings._costs_fetch = fake_fetch

    # ---------- 1) 两个区间各自成表，后者不再覆盖前者
    a = settings.load_costs("2026-10-01", "2026-10-27", force=True)
    b = settings.load_costs("2026-10-01", "2026-10-29", force=True)
    assert a["skus"]["1001"]["_goodsCost"] == 20.0, a
    assert b["skus"]["1001"]["_goodsCost"] == 45.0, b
    again = settings.costs_cached("2026-10-01", "2026-10-27")
    assert again and again["skus"]["1001"]["_goodsCost"] == 20.0, \
        "拉了 10/29 之后，10/27 的成本表被覆盖了（正是要修的 bug）"
    assert (tmp / "costs" / "costs_2026-10-01__2026-10-27.json").exists()
    # 「最近一次」全局文件仍在，状态页与成本护栏靠它对比
    assert settings._costs_cache_read()["skus"]["1001"]["_goodsCost"] == 45.0

    # ---------- 2) 本区间的表在 TTL 内直接命中，一次接口都不发
    n = len(calls)
    hit = settings.load_costs("2026-10-01", "2026-10-27")
    assert len(calls) == n, "TTL 内不该再打接口"
    assert hit["skus"]["1001"]["_goodsCost"] == 20.0
    assert settings.COSTS_SOURCE["detail"].startswith("2026-10-01 ~ 2026-10-27"), \
        settings.COSTS_SOURCE
    assert settings.costs_cached("2026-08-01", "2026-08-31") is None
    assert len(calls) == n, "costs_cached 只读本地，绝不能发请求"

    # ---------- 3) 新区间没拉过：先借最近一版立即出表（写明借了谁），后台补本区间
    got = settings.load_costs("2026-09-01", "2026-09-30")
    assert "借用" in settings.COSTS_SOURCE["kind"], settings.COSTS_SOURCE
    assert "2026-10" in settings.COSTS_SOURCE["detail"], settings.COSTS_SOURCE
    assert got["skus"]["1001"]["_goodsCost"] == 45.0, "借的必须是最近一次那份"
    for _ in range(200):
        if settings.costs_cached("2026-09-01", "2026-09-30"):
            break
        time.sleep(0.05)
    sep = settings.costs_cached("2026-09-01", "2026-09-30")
    assert sep and sep["skus"]["1001"]["_goodsCost"] == 25.0, "后台没按本区间补拉成本表"

    # ---------- 4) 拼接/续抓：件数与花费不动，到手与毛利按本次区间重算
    prefix = {"start": "2026-10-01", "end": "2026-10-25", "account": "main",
              "savedAt": "2026-10-26 03:00", "costRange": ["2026-10-01", "2026-10-25"],
              "byAccount": {"main": {"label": "主账号", "rows": [_row(1001, 10, 80.0, 60.0)],
                                     "totals": {}}},
              "rows": [], "totals": {}, "owner": {}, "perAccount": {}, "notes": []}
    tail = {"start": "2026-10-26", "end": "2026-10-29", "account": "main",
            "savedAt": "2026-10-29 03:00", "costRange": ["2026-10-26", "2026-10-29"],
            "byAccount": {"main": {"label": "主账号", "rows": [_row(1001, 4, 80.0, 60.0)],
                                   "totals": {}}},
            "rows": [], "totals": {}, "owner": {}, "perAccount": {}, "notes": []}
    for d in (prefix, tail):
        d["byAccount"]["main"]["totals"] = profit.totals_of(d["byAccount"]["main"]["rows"])
        d["rows"] = d["byAccount"]["main"]["rows"]
        d["totals"] = d["byAccount"]["main"]["totals"]
        d["perAccount"] = {"main": {"rows": 1, "spend": 100.0}}

    parts = [(prefix, prefix["savedAt"]), (tail, tail["savedAt"])]
    by_account = profit.merge_parts_rows(parts)
    assert by_account["main"]["rows"][0]["qty"] == 14, by_account["main"]["rows"]

    mixed = profit.stitched_payload(dict(merge_copy(by_account)), "2026-10-01", "2026-10-29",
                                    [("2026-10-01", "2026-10-25"),
                                     ("2026-10-26", "2026-10-29")], parts)
    # 不传成本表 → 沿用各段原值：毛利是 14 件 × 60（两段都是旧口径）
    assert mixed["totals"]["gross"] == round(14 * 60.0, 2), mixed["totals"]

    # 传本次区间（货款成本 45）→ 到手/毛利重算，件数、成交额、花费一字不改
    fresh = settings.load_costs("2026-10-01", "2026-10-29")
    priced = profit.stitched_payload(dict(merge_copy(by_account)), "2026-10-01", "2026-10-29",
                                     [("2026-10-01", "2026-10-25"),
                                      ("2026-10-26", "2026-10-29")], parts,
                                     costs=fresh, cost_range=("2026-10-01", "2026-10-29"))
    row = priced["byAccount"]["main"]["rows"][0]
    tot = priced["totals"]
    assert row["qty"] == 14 and row["szAmt"] == 1400.0, row
    # 花费是累加量：两段各 30/70，合并后 60/140，重算不会动它
    assert row["cpsCost"] == 60.0 and row["jztCost"] == 140.0, row
    assert tot["cpsCost"] == 60.0 and tot["jztCost"] == 140.0, tot
    assert row["arrival"] == round(14 * 80.0, 2), row
    assert row["supply"] == 80.0, row
    assert tot["gross"] != mixed["totals"]["gross"], "毛利没有按新区间成本重算"
    assert tot["gross"] == round(14 * (80.0 - 45.0), 2), tot           # 到手 80 − 货款 45
    assert priced["costRange"] == ["2026-10-01", "2026-10-29"], priced.get("costRange")
    assert any("整表重算" in n for n in priced["notes"]), priced["notes"]

    # ---------- 5) 只重拉成本不会动件数与花费（「只更新 ERP 成本」按钮的口径）
    data = json.loads(json.dumps(priced))
    changed = profit.reprice_payload(data, settings.load_costs("2026-10-01", "2026-10-27",
                                                               force=True))
    assert changed == 1, changed
    assert data["byAccount"]["main"]["rows"][0]["qty"] == 14
    assert data["totals"]["gross"] == round(14 * (80.0 - 20.0), 2), data["totals"]

    print("成本区间隔离 OK：一区间一表 / TTL 内不发请求 / 缺表明说借用并后台补 / "
          "拼接与续抓按本次区间重算毛利（件数与花费不动）")
    return 0


def merge_copy(by_account: dict) -> dict:
    """浅拷贝一份 byAccount，让每次 stitched_payload 都从同样的原始行出发。"""
    out = {}
    for acc, blk in by_account.items():
        out[acc] = dict(blk, rows=[dict(r) for r in blk["rows"]])
    return out


if __name__ == "__main__":
    raise SystemExit(main())
