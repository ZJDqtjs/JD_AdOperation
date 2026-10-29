# -*- coding: utf-8 -*-
"""月度利润的账号归属：花费按京准通投放账号走，不再按商智成交件数搬运。

规则（LOGIC_VERSION=2）：
  - 谁在这个 SKU 上花了钱，花费就记在谁名下；
  - 商智只提供成交件数与前台价，不参与账号归属；
  - 两账号京准通合计 = 各账号花费之和（不重不漏，谁的表都能和京准通后台对上）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import profit  # noqa: E402


def _fake_finalize(collected: dict, costs: dict | None = None) -> list[dict]:
    """只保留花费/件数，避开 ERP 成本与成本模型（本测试只验归属逻辑）。"""
    out = []
    for sid, c in collected.items():
        out.append({"skuId": int(sid), "qty": int(c.get("qty") or 0),
                    "szAmt": float(c.get("szAmt") or 0),
                    "cpsCost": round(float(c.get("cpsCost") or 0), 2),
                    "jztCost": round(float(c.get("jztCost") or 0), 2),
                    "accts": list(c.get("accts") or []),
                    "spendAccts": list(c.get("spendAccts") or [])})
    return out


def main() -> int:
    profit.finalize = _fake_finalize

    # 1001 主账号在卖、B 在投（典型：B 投主账号的商品）
    # 1002 主账号在卖、没人投
    # 1003 只有 B 在卖、也只有 B 在投
    parts = {
        "main": {
            1001: {"qty": 10, "szAmt": 1000.0, "cpsCost": 0.0, "jztCost": 0.0,
                   "accts": ["main"], "spendAccts": []},
            1002: {"qty": 4, "szAmt": 200.0, "cpsCost": 0.0, "jztCost": 0.0,
                   "accts": ["main"], "spendAccts": []},
        },
        "b": {
            1001: {"qty": 0, "szAmt": 0.0, "cpsCost": 0.0, "jztCost": 500.0,
                   "accts": [], "spendAccts": ["b"]},
            1003: {"qty": 5, "szAmt": 500.0, "cpsCost": 20.0, "jztCost": 300.0,
                   "accts": ["b"], "spendAccts": ["b"]},
        },
    }

    attributed = profit.attribute_spend(parts)
    assert attributed[1001]["owners"] == ["b"], attributed[1001]
    assert attributed[1001]["jzt"] == 500.0, attributed[1001]
    assert attributed[1002]["owners"] == [], attributed[1002]
    assert attributed[1003]["owners"] == ["b"], attributed[1003]

    main_rows = {r["skuId"]: r for r in profit.rows_for_account(parts, attributed, "main")}
    b_rows = {r["skuId"]: r for r in profit.rows_for_account(parts, attributed, "b")}

    # 主账号：只有自己的成交与花费，B 投的 500 元一分都不搬过来
    assert main_rows[1001]["qty"] == 10, main_rows[1001]
    assert main_rows[1001]["jztCost"] == 0.0, main_rows[1001]
    assert main_rows[1001]["spendAccts"] == [], main_rows[1001]
    assert sum(r["jztCost"] for r in main_rows.values()) == 0.0
    assert 1003 not in main_rows, main_rows

    # 账号B：A 的钱留在 B（qty=0 的纯花费行），C 的花费也只在 B
    assert b_rows[1001]["jztCost"] == 500.0 and b_rows[1001]["qty"] == 0, b_rows[1001]
    assert b_rows[1001]["spendAccts"] == ["b"], b_rows[1001]
    assert b_rows[1003]["jztCost"] == 300.0, b_rows[1003]
    assert sum(r["jztCost"] for r in b_rows.values()) == 800.0

    # 两表合计 = 各账号花费之和，不重不漏
    total = (sum(r["jztCost"] for r in main_rows.values())
             + sum(r["jztCost"] for r in b_rows.values()))
    assert total == 800.0, total

    print("PASS 花费按京准通投放账号归集：不搬钱、不留重复、两表合计等于花费之和")
    return 0


if __name__ == "__main__":
    sys.exit(main())
