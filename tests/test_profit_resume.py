# -*- coding: utf-8 -*-
"""月度利润的断点续抓：本地算过的段不重抓，只补抓缺的尾段。

为什么要有这个测试：查询 10/1~10/27 时如果本地只有 10/1~10/25，
旧逻辑会判定「没命中」并从 10/1 整月重抓 —— 既慢，又高频打京东接口。
现在要求：复用连续前缀、只抓断点之后，并把两段按 SKU 相加。
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import dailystore as ds, indicators as ind, profit, settings  # noqa: E402


def _row(sid: int, qty: int, sz_amt: float, cps: float, jzt: float,
         supply: float, front: float, gross_unit: float) -> dict:
    """造一行「已算好的」缓存行（字段与 finalize 产出一致）。"""
    arrival = round(qty * supply, 2)
    gross = round(qty * gross_unit, 2)
    return {"skuId": sid, "itemName": f"商品{sid}", "category": "蔬菜",
            "qty": qty, "szAmt": sz_amt, "frontPrice": front, "supply": supply,
            "arrival": arrival, "grossPerOrder": gross_unit, "gross": gross,
            "cpsCost": cps, "jztCost": jzt, "net": round(gross - cps - jzt, 2),
            "accts": ["main"], "spendAccts": ["main"], "costSource": "ERP"}


def _payload(start: str, end: str, rows: list[dict]) -> dict:
    by = {"main": {"rows": rows, "totals": profit.totals_of(rows), "label": "梓云茶"}}
    return {"jobId": f"profit-{start}_{end}", "account": "main", "accounts": ["main"],
            "accountLabel": "梓云茶", "start": start, "end": end,
            "rows": rows, "totals": profit.totals_of(rows), "byAccount": by,
            "owner": {str(r["skuId"]): ["main"] for r in rows},
            "perAccount": {"main": {"rows": len(rows),
                                    "spend": round(sum(r["cpsCost"] + r["jztCost"] for r in rows), 2)}},
            "notes": ["口径说明"], "logicVersion": profit.LOGIC_VERSION,
            "costSource": {"kind": "供货方接口"}}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="profit-resume-")
    # 缓存与按天存储都落到临时目录：DAILY_DIR/REPORT_DIR 是 import 时算好的，必须一起改，
    # 否则测试会写进真实数据目录（容器里是 /data，本机就是 data/）。
    settings.DATA_DIR = tmp
    settings.DAILY_DIR = Path(tmp) / "daily"
    settings.REPORT_DIR = Path(tmp) / "reports"
    settings.DAILY_DIR.mkdir(parents=True, exist_ok=True)
    runner = profit.ProfitRunner()
    base = Path(tmp) / "reports"
    base.mkdir(parents=True, exist_ok=True)

    # ---------- 1) 本地只有 10/1~10/25，查 10/1~10/27：断点应落在 10/26
    head = _payload("2026-10-01", "2026-10-25",
                    [_row(1001, 10, 100.0, 5.0, 20.0, 6.0, 10.0, 3.0),
                     _row(1002, 4, 200.0, 10.0, 60.0, 30.0, 50.0, 8.0)])
    (base / "profit_2026-10-01_2026-10-25.json").write_text(
        json.dumps(head, ensure_ascii=False), encoding="utf-8")

    cov = runner.covered_prefix("2026-10-01", "2026-10-27")
    assert cov, "有连续前缀却没识别出断点"
    prefix, picked, tail_start = cov
    assert tail_start == "2026-10-26", tail_start
    assert picked == [("2026-10-01", "2026-10-25")], picked
    assert prefix["start"] == "2026-10-01" and prefix["end"] == "2026-10-25", prefix

    # ---------- 2) 断点之后只抓 10/26~10/27，再并进前缀
    tail = _payload("2026-10-26", "2026-10-27",
                    [_row(1001, 4, 40.0, 2.0, 8.0, 6.0, 10.0, 3.0),
                     _row(1003, 2, 80.0, 0.0, 30.0, 20.0, 40.0, 5.0)])
    tail["jobId"] = "20261027-job"
    merged = runner._apply_prefix(tail, prefix, picked, "2026-10-01", "2026-10-27")
    assert merged["start"] == "2026-10-01" and merged["end"] == "2026-10-27", merged["range"] if "range" in merged else merged
    assert merged["jobId"] == "20261027-job"
    assert merged["logicVersion"] == profit.LOGIC_VERSION
    rows = {r["skuId"]: r for r in merged["rows"]}
    a = rows[1001]
    assert a["qty"] == 14 and abs(a["szAmt"] - 140.0) < 1e-6, a
    assert abs(a["arrival"] - 84.0) < 1e-6, a                    # 14 件 × 到手 6 元
    assert abs(a["gross"] - 42.0) < 1e-6, a                      # 14 件 × 毛利/件 3 元
    assert abs(a["cpsCost"] - 7.0) < 1e-6 and abs(a["jztCost"] - 28.0) < 1e-6, a
    assert abs(a["net"] - (42.0 - 7.0 - 28.0)) < 1e-6, a
    assert abs(a["frontPrice"] - 10.0) < 1e-6 and abs(a["supply"] - 6.0) < 1e-6, a
    assert rows[1003]["qty"] == 2 and rows[1003]["itemName"] == "商品1003", rows[1003]
    assert merged["totals"]["qty"] == 20, merged["totals"]        # 10+4 + 4+2
    assert merged["totals"]["skuCount"] == 3, merged["totals"]    # 1001 合并成一行
    assert merged["resumedFrom"] == ["2026-10-01 ~ 2026-10-25"], merged.get("resumedFrom")
    assert merged["tail"] == ["2026-10-26", "2026-10-27"], merged.get("tail")
    assert "断点续抓" in merged["notes"][0] and "只补抓 2026-10-26" in merged["notes"][0]
    assert merged["perAccount"]["main"]["spend"] == 135.0, merged["perAccount"]

    # ---------- 2b) 尾段是「今天」、一行都没出：保住前缀，不判失败
    empty_tail = _payload("2026-10-28", "2026-10-28", [])
    empty_tail["jobId"] = "20261028-empty"
    kept = runner._apply_prefix(empty_tail, prefix, [("2026-10-01", "2026-10-27")],
                                "2026-10-01", "2026-10-28")
    assert kept["totals"]["qty"] == 14, kept["totals"]           # 只剩前缀的 10+4
    assert sorted(r["skuId"] for r in kept["rows"]) == [1001, 1002], kept["rows"]
    assert kept["tail"] == ["2026-10-28", "2026-10-28"], kept["tail"]
    assert kept.get("tailEmpty") == ["2026-10-28", "2026-10-28"], kept.get("tailEmpty")
    assert "还没出数" in kept["notes"][-1], kept["notes"][-1]
    assert runner._paths("main", "2026-10-28", "2026-10-28").exists() is False, \
        "空尾段不能落缓存，否则明天再查会被空数据挡住"

    # ---------- 3) 铺满 / 没缓存 两种情况都不该走续抓
    (base / "profit_2026-10-26_2026-10-27.json").write_text(
        json.dumps(tail, ensure_ascii=False), encoding="utf-8")
    assert runner.covered_prefix("2026-10-01", "2026-10-27") is None, "已铺满还续抓"
    full = runner.use_stitched("2026-10-01", "2026-10-27")
    assert full and full["totals"]["qty"] == 20, full and full["totals"]
    assert runner.covered_prefix("2026-09-01", "2026-09-27") is None, "没缓存却续抓"

    # ---------- 4) 断点必须「连续」：中间断了就不能把后面那段当前缀
    (base / "profit_2026-10-05_2026-10-10.json").write_text(
        json.dumps(_payload("2026-10-05", "2026-10-10", [_row(1001, 1, 10.0, 0.0, 0.0, 6.0, 10.0, 3.0)]),
                   ensure_ascii=False), encoding="utf-8")
    (base / "profit_2026-10-01_2026-10-03.json").write_text(
        json.dumps(_payload("2026-10-01", "2026-10-03", [_row(1001, 3, 30.0, 1.0, 4.0, 6.0, 10.0, 3.0)]),
                   ensure_ascii=False), encoding="utf-8")
    cov2 = runner.covered_prefix("2026-10-01", "2026-10-20")
    assert cov2 and cov2[2] == "2026-10-04", cov2          # 10/4 缺 → 只能铺到 10/3
    assert cov2[1] == [("2026-10-01", "2026-10-03")], cov2[1]

    # ---------- 5) 旧口径缓存不可信，不拿来续抓
    stale = _payload("2026-11-01", "2026-11-05", [_row(1001, 2, 20.0, 0.0, 0.0, 6.0, 10.0, 3.0)])
    stale["logicVersion"] = 1
    (base / "profit_2026-11-01_2026-11-05.json").write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    assert runner.covered_prefix("2026-11-01", "2026-11-08") is None, "旧口径缓存被复用了"

    # ---------- 6) 商智按天数据齐 → 直接吃本地，不发请求
    today = ds.today()
    d1, d2, d3 = (today - timedelta(days=n) for n in (5, 4, 3))

    def _day_rows(qty: int, amt: float) -> dict:
        return {"rows": [{"sku_id": 1001, "name": "商品1001", ind.SZ_QTY: qty, ind.SZ_AMT: amt},
                         {"$summary": True, "theDate": d1.isoformat()}]}

    for d, q, a_ in ((d1, 3, 30.0), (d2, 4, 40.0), (d3, 5, 50.0)):
        ds.save_day("main", d.isoformat(), "sz_product", _day_rows(q, a_))
    s, e = d1.isoformat(), d3.isoformat()
    local = profit.sz_rows_local("main", s, e)
    assert local, "按天数据齐却没走本地"
    got = {int(r["sku_id"]): r for r in local}
    assert got[1001][ind.SZ_QTY] == 12 and abs(got[1001][ind.SZ_AMT] - 120.0) < 1e-6, got[1001]
    # 缺一天就必须退回抓取（不能拿不完整的区间算钱）
    assert profit.sz_rows_local("main", d1.isoformat(), d2.isoformat())
    ds.day_path("main", d2.isoformat(), "sz_product").unlink()
    assert profit.sz_rows_local("main", s, e) is None, "缺数据却当成有本地数据"
    # 最近 15 天内的 0 行文件 = 抓早了，同样要重抓
    ds.save_day("main", (today - timedelta(days=1)).isoformat(), "sz_product", {"rows": []})
    assert profit.sz_rows_local("main", (today - timedelta(days=1)).isoformat(),
                               (today - timedelta(days=1)).isoformat()) is None

    print("断点续抓 OK：前缀复用 / 尾段合并 / 连续性 / 旧口径拒绝 / 商智按天本地优先")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
