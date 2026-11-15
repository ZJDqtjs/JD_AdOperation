# -*- coding: utf-8 -*-
"""命令行抓取京准通「账户中心-财务记录」的资金流水（充值/消费/分配/冻结/余额）。

核心逻辑在 jd_roi/cashflow.py（网页「广告费流水」页用的也是它），这里只是个命令行壳，
方便在容器里批量补抓 / 换区间时不用开页面。

用法（容器内）：
    python /app/scripts/fetch_cashflow.py --account=main --days=30 --out=/data/cashflow
    python /app/scripts/fetch_cashflow.py --all --days=90 --out=/data/cashflow

注意：抓取会占浏览器登录态，与线上的定时抓取 / 保活互斥，别和它们同时跑。
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, "/app")

from jd_roi import cashflow, settings  # noqa: E402


def _arg(argv: list[str], name: str, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def main() -> int:
    argv = sys.argv[1:]
    out = Path(_arg(argv, "out", "/data/cashflow"))
    days = max(1, int(_arg(argv, "days", "30") or 30))
    if "--all" in argv:
        keys = [a["key"] for a in settings.ACCOUNTS]
    else:
        keys = [_arg(argv, "account", settings.MAIN_ACCOUNT) or settings.MAIN_ACCOUNT]
    begin = (settings.today() - timedelta(days=days - 1)).isoformat()
    end = settings.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    print(f"账号: {keys}  区间: {begin} ~ {end}", flush=True)

    for i, key in enumerate(keys):
        label = settings.get_account(key).get("label") or key
        try:
            data = cashflow.fetch_account(key, begin, end)
            p = out / f"{key}.json"
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            bal = (data.get("balance") or {}).get("cashBalance")
            print(f"[{key}] {label} 余额={bal} 充值{len(data.get('recharge') or [])} "
                  f"消费{len(data.get('consume') or [])} 分配{len(data.get('assign') or [])} "
                  f"-> {p}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{key}] {label} 失败：{type(e).__name__}: {e}", flush=True)
        if i < len(keys) - 1:
            time.sleep(6 + random.random() * 4)     # 账号之间喘一口，别连打
    return 0


if __name__ == "__main__":
    sys.exit(main())
