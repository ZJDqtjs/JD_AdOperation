# -*- coding: utf-8 -*-
"""利润缓存要按「算过哪些账号」校验：新登录的店不能命中只有老账号的表。

2026-11-01 实测 bug：登录蛇吞象（acct1）/ 沛沛佳（acct2）后查 10 月，
profit_2026-10-01_2026-10-31.json 里只有 main/b，load_cached 却判命中，
页面把 main 的表当成 acct1 的结果，还提示「命中本地缓存」。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import profit, settings  # noqa: E402

START, END = "2026-10-01", "2026-10-31"


def _payload(accounts: list[str]) -> dict:
    by = {k: {"label": k,
              "rows": [{"skuId": 1001, "itemName": k + "-品", "net": 1.0}],
              "totals": {"net": 1.0}} for k in accounts}
    return {"logicVersion": profit.LOGIC_VERSION, "accounts": accounts,
            "byAccount": by, "rows": by[accounts[0]]["rows"],
            "account": accounts[0], "jobId": "t", "start": START, "end": END}


def _write(root: Path, start: str, end: str, payload: dict) -> None:
    d = root / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"profit_{start}_{end}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got} want={want}")
        if not ok:
            fails.append(label)

    old_dir = settings.DATA_DIR
    old_get = settings.get_account
    tmp = Path(tempfile.mkdtemp(prefix="jdroi_profit_cache_"))
    settings.DATA_DIR = tmp
    settings.get_account = lambda a: {"key": a, "label": a}   # 不依赖真实 accounts.json
    try:
        r = profit.ProfitRunner()

        print("[1] 缓存只算过 main/b：新账号 acct1 不能命中")
        _write(tmp, START, END, _payload(["main", "b"]))
        check("acct1 未算过 → None", r.load_cached("acct1", START, END), None)
        check("main 带 need 缺 acct1 → None",
              r.load_cached("main", START, END, need=["main", "b", "acct1"]), None)
        check("main 带 need 齐全 → 命中",
              bool(r.load_cached("main", START, END, need=["main", "b"])), True)

        print("[2] 缓存里已有 acct1：正常命中，且只回自己的表")
        _write(tmp, START, END, _payload(["main", "b", "acct1"]))
        got = r.load_cached("acct1", START, END)
        check("acct1 命中", bool(got), True)
        check("acct1 的行是它自己的", (got or {}).get("account"), "acct1")
        check("acct1 只有自己的行", len((got or {}).get("rows") or []), 1)

        print("[3] 多段拼接：老表缺账号时不能拿它当前缀")
        _write(tmp, "2026-08-01", "2026-08-31", _payload(["main", "b"]))
        _write(tmp, "2026-09-01", "2026-09-30", _payload(["main", "b"]))
        check("拼 8+9 缺 acct1 → None",
              r.use_stitched("2026-08-01", "2026-09-30", ["main", "b", "acct1"]), None)
        check("拼 8+9 账号齐全 → 有结果",
              bool(r.use_stitched("2026-08-01", "2026-09-30", ["main", "b"])), True)

        print("[4] 旧口径缓存（logicVersion 不符）仍然不命中")
        bad = _payload(["main", "acct1"])
        bad["logicVersion"] = profit.LOGIC_VERSION - 1
        _write(tmp, START, END, bad)
        check("旧口径 → None", r.load_cached("acct1", START, END), None)
    finally:
        settings.DATA_DIR = old_dir
        settings.get_account = old_get
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("失败", len(fails), "项：" + "；".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
