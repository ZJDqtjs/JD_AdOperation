# -*- coding: utf-8 -*-
"""把历史抓取结果迁移进「按天库」。

只迁移真正的**逐日**数据，不做任何按比例摊派：
* jst_campaign_daily / jst_sku_daily（isDaily=true 拉到的逐日行）
* oplog*.json（按 optTime 切分到天）

用法：
    python -m jd_roi.migrate_daily            # 迁移全部账号
    python -m jd_roi.migrate_daily --account=b
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from . import config, dailystore as ds, settings, store


def _parse_day(s):
    t = str(s or "")
    if "~" in t:
        return None
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return None


def migrate_daily_report(key: str, legacy_name: str, kind: str, log=print) -> int:
    payload = store.load(legacy_name, key)
    if not payload:
        return 0
    by_day: dict = defaultdict(list)
    for r in payload.get("rows", []):
        day = _parse_day(r.get("date"))
        if day:
            by_day[day].append(r)
    for day, rows in sorted(by_day.items()):
        if ds.has_day(key, day, kind):
            continue
        ds.save_day(key, day, kind, {"rows": rows, "dateStart": day, "dateEnd": day})
    log(f"  [{key}] {kind}: {len(by_day)} 天")
    return len(by_day)


def migrate_oplog(key: str, path: Path, log=print) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    by_day: dict = defaultdict(list)
    for r in payload.get("rows", []):
        day = str(r.get("optTime") or "")[:10]
        if len(day) == 10:
            by_day[day].append(r)
    for day, rows in by_day.items():
        if ds.has_day(key, day, "oplog"):
            continue
        ds.save_day(key, day, "oplog", {"rows": rows, "dateStart": day, "dateEnd": day})
    log(f"  [{key}] oplog: {len(by_day)} 天")
    return len(by_day)


def main() -> int:
    account = None
    for a in sys.argv[1:]:
        if a.startswith("--account="):
            account = a.split("=", 1)[1]
    settings.ensure_dirs()
    accs = [settings.get_account(account)] if account else settings.ACCOUNTS
    for a in accs:
        key = a["key"]
        print(f"[migrate] {key} ({a.get('label')})")
        migrate_daily_report(key, "jst_campaign_daily", "jst_campaign")
        migrate_daily_report(key, "jst_sku_daily", "jst_sku")
        legacy_op = (config.account_dir(key) / "oplog.json")
        migrate_oplog(key, legacy_op)
        ds.invalidate_cache(key)
    print("[migrate] 完成。覆盖情况：")
    for k, v in ds.coverage(accs).items():
        print(f"  {k}: 天={v['days']['count']} {v['days']['first']}~{v['days']['last']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
