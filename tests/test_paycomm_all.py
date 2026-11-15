# -*- coding: utf-8 -*-
"""花费一览「全部账号」合并：花费相加、比率列不瞎合并。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import paycomm  # noqa: E402


def _spend_block(label, rows, unattr=None):
    return {"accountLabel": label, "rows": rows,
            "spend": {"unattrRows": unattr or [], "warnings": []}}


def main() -> int:
    # ---- 京准通花费：同一 SKU 两个账号都投 → 花费相加
    blocks = {
        "main": _spend_block("梓云茶", [
            {"skuId": 1001, "itemName": "A", "category": "蔬菜", "jztCost": 100.0,
             "jztJstCost": 60.0, "jztShopCost": 40.0, "jztKwCost": 0.0, "jztSwaCost": 0.0},
            {"skuId": -9101, "itemName": "未归因", "category": "未归因", "jztCost": 5.0,
             "jztJstCost": 0.0, "jztShopCost": 5.0, "jztKwCost": 0.0, "jztSwaCost": 0.0}],
            [{"key": "shop", "name": "快车-已删", "cost": 5.0}]),
        "b": _spend_block("雨润禾", [
            {"skuId": 1001, "itemName": "A", "category": "蔬菜", "jztCost": 50.0,
             "jztJstCost": 0.0, "jztShopCost": 50.0, "jztKwCost": 0.0, "jztSwaCost": 0.0},
            {"skuId": 1002, "itemName": "B", "category": "蔬菜", "jztCost": 30.0,
             "jztJstCost": 30.0, "jztShopCost": 0.0, "jztKwCost": 0.0, "jztSwaCost": 0.0}],
            [{"key": "shop", "name": "快车-已删", "cost": 7.0}]),
    }
    got = paycomm.merge_all_blocks(blocks, "spend", "2026-10-01", "2026-10-31")
    rows = {r["skuId"]: r for r in got["rows"]}
    assert rows[1001]["jztCost"] == 150.0, rows[1001]
    assert rows[1001]["jztShopCost"] == 90.0, rows[1001]
    assert rows[1002]["jztJstCost"] == 30.0, rows[1002]
    t = got["spend"]["totals"]
    assert t["jztCost"] == 180.0 and t["unattr"] == 12.0 and t["total"] == 192.0, t
    assert got["spend"]["unattrRows"][0]["cost"] == 12.0, got["spend"]["unattrRows"]

    # ---- CPS：可加字段相加，口径不明的比率置空
    cps = {
        "main": {"accountLabel": "梓云茶", "rows": [
            {"skuId": 2001, "itemName": "C", "category": "水果", "payCommAmount": 10.0,
             "preCommAmount": 12.0, "clicks": 5, "uv": 4, "orderCnt": 2, "orderGmv": 100.0,
             "payOrderCnt": 2, "fiOrderCnt": 1, "fiOrderGmv": 50.0, "orderUserCnt": 2,
             "payCommRate": 8.8, "preCommRate": 12.0, "orderCvr": 50.0, "roi": 10.0}],
            "daily": [{"date": "2026-10-01", "payCommAmount": 10.0, "clicks": 5, "orderCnt": 2,
                       "orderGmv": 100.0, "preCommAmount": 12.0, "fiOrderCnt": 1,
                       "fiOrderGmv": 50.0, "roi": 10.0}]},
        "b": {"accountLabel": "雨润禾", "rows": [
            {"skuId": 2001, "itemName": "C", "category": "水果", "payCommAmount": 5.0,
             "preCommAmount": 6.0, "clicks": 3, "uv": 2, "orderCnt": 1, "orderGmv": 60.0,
             "payOrderCnt": 1, "fiOrderCnt": 1, "fiOrderGmv": 30.0, "orderUserCnt": 1,
             "payCommRate": 9.0, "preCommRate": 10.0, "orderCvr": 50.0, "roi": 12.0}],
            "daily": [{"date": "2026-10-01", "payCommAmount": 5.0, "clicks": 3, "orderCnt": 1,
                       "orderGmv": 60.0, "preCommAmount": 6.0, "fiOrderCnt": 1,
                       "fiOrderGmv": 30.0, "roi": 12.0}]},
    }
    got2 = paycomm.merge_all_blocks(cps, "cps", "2026-10-01", "2026-10-31")
    r = {x["skuId"]: x for x in got2["rows"]}[2001]
    assert r["payCommAmount"] == 15.0 and r["orderGmv"] == 160.0, r
    assert r["clicks"] == 8 and r["uv"] == 6, r
    assert r["preCommRate"] == round(18.0 / 160.0 * 100, 2), r      # 口径明确 → 重算
    assert r["orderCvr"] == round(3 / 6 * 100, 2), r
    assert r["payCommRate"] is None and r["roi"] is None, r          # 口径不明 → 置空
    assert len(got2["daily"]) == 1 and got2["daily"][0]["payCommAmount"] == 15.0, got2["daily"]

    print("PASS 花费一览「全部账号」：花费相加、CPS 比率不瞎合并")
    return 0


if __name__ == "__main__":
    sys.exit(main())
