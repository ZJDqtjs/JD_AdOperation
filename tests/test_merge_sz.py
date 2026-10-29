# -*- coding: utf-8 -*-
"""商智按 SKU 归集：成交属于 SKU，不属于账号。

- 两个账号的商智明细合成一张 SKU 表；
- 同一个 SKU 两边都有时取件数大的那份，**绝不相加**（避免重复计成交）；
- 记录这份成交来自哪个账号的商智，供利润表标注「成交账号」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import indicators as ind, profit  # noqa: E402


def _row(sku, qty, amt, name=""):
    return {"sku_id": sku, ind.SZ_QTY: qty, ind.SZ_AMT: amt, "name": name}


def main() -> int:
    sz_parts = {
        "main": [
            _row(100000001, 10, 1000.0, "拇指小玉米"),
            _row(100000002, 4, 200.0, "雪莲果"),
            _row("合计", 14, 1200.0),                     # 汇总行必须忽略
        ],
        "b": [
            _row(100000001, 3, 300.0, "拇指小玉米(旧)"),   # 同一个 SKU 两边都有 → 取大的
            _row(100000003, 5, 500.0, "纳豆"),
        ],
    }

    got = profit.merge_sz_by_sku(sz_parts)

    assert set(got) == {100000001, 100000002, 100000003}, got
    assert got[100000001]["qty"] == 10 and got[100000001]["amt"] == 1000.0, got[100000001]
    assert got[100000001]["acc"] == "main", got[100000001]      # 件的多的那份胜出
    assert got[100000002]["acc"] == "main"
    assert got[100000003]["qty"] == 5 and got[100000003]["acc"] == "b"
    # 没有相加：10 而不是 13
    assert sum(v["qty"] for v in got.values()) == 19, got

    print("PASS 商智按 SKU 归集：跨账号合成一张表、同 SKU 取大的那份、不相加")
    return 0


if __name__ == "__main__":
    sys.exit(main())
