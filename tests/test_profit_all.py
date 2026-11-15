# -*- coding: utf-8 -*-
"""「全部账号」总览的合并规则。

硬要求：
  - 花费（京准通 / CPS）相加：一笔钱只有一个投放账号，不会重复；
  - 成交件数 / 毛利按 SKU **只取一份**：同一 SKU 两个账号都在投时，两张表带的是同一份商智成交，
    直接相加会把毛利算两遍；
  - 未归因行（负 SKU）也是花费，相加。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import profit  # noqa: E402


def _row(sid, qty, gross, jzt=0.0, cps=0.0, name=""):
    r = {"skuId": sid, "itemName": name or f"SKU{sid}", "qty": qty,
         "szAmt": qty * 10.0, "arrival": qty * 5.0, "gross": gross,
         "grossPerOrder": round(gross / qty, 2) if qty else 0.0,
         "cpsCost": cps, "jztCost": jzt, "category": "蔬菜"}
    r["net"] = round(gross - jzt - cps, 2)
    return r


def main() -> int:
    by = {
        "main": {"rows": [_row(1001, 10, 100.0, jzt=5.0), _row(-9101, 0, 0.0, jzt=2.0)],
                 "totals": {}, "label": "梓云茶"},
        "b": {"rows": [_row(1001, 10, 100.0, jzt=7.0), _row(1002, 5, 50.0, jzt=3.0),
                       _row(-9101, 0, 0.0, jzt=3.0)],
              "totals": {}, "label": "雨润禾"},
    }
    got = profit.merge_all_accounts(by)
    rows = {int(r["skuId"]): r for r in got["rows"]}

    assert rows[1001]["qty"] == 10, rows[1001]          # 成交只取一份
    assert rows[1001]["gross"] == 100.0, rows[1001]     # 毛利不重复
    assert rows[1001]["jztCost"] == 12.0, rows[1001]    # 花费相加
    assert rows[1001]["net"] == 88.0, rows[1001]
    assert rows[1002]["jztCost"] == 3.0, rows[1002]
    assert rows[-9101]["jztCost"] == 5.0, rows[-9101]   # 未归因也相加

    t = got["totals"]
    assert t["qty"] == 15, t                            # 10 + 5
    assert t["gross"] == 150.0, t
    assert t["jztCost"] == 20.0, t                       # 5+7+3+2+3
    assert t["net"] == 130.0, t
    assert got["label"] == "全部账号"

    print("PASS 全部账号总览：花费相加、成交/毛利按 SKU 只取一份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
