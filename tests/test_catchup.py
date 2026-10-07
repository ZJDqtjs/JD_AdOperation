# -*- coding: utf-8 -*-
"""停机补数（catchup_range）测试。

容器停几天甚至一个月再起来时，只抓「最近 N 天」会留下空洞。
这里验证：补数区间 = 缺口 ∪ 回刷窗口，并有 MAX_BACKFILL_DAYS 上限。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_catchup_", dir=str(_TMPROOT)))

os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ.setdefault("JD_COSTS_URL", "")   # 离线：测试不连供货方成本接口
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_ACCOUNTS"] = json.dumps([{"key": "main", "label": "主账号", "account_id": 111}])
os.environ["JD_REFRESH_DAYS"] = "3"
os.environ["JD_MAX_BACKFILL_DAYS"] = "120"
os.environ["JD_ENABLE_SCHEDULER"] = "0"
os.environ["JD_RUN_ON_START"] = "0"
sys.path.insert(0, str(ROOT))

from jd_roi import dailystore as ds, scrape_day, settings  # noqa: E402

FAILS = []
YESTERDAY = ds.today() - dt.timedelta(days=1)


def check(name, got, want):
    ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)


def d(days_ago):
    return (YESTERDAY - dt.timedelta(days=days_ago)).isoformat()


def seed_upto(days_ago):
    """写入从很久以前一直到「days_ago 天前」的数据。"""
    for i in range(days_ago, min(days_ago + 5, 400)):
        ds.save_day("main", d(i), "jzt_campaign", {"rows": [], "dateStart": d(i), "dateEnd": d(i)})
    ds.invalidate_cache("main")


def main() -> int:
    settings.ensure_dirs()
    print("参照：昨天 =", YESTERDAY.isoformat(), "| REFRESH_DAYS =", settings.REFRESH_DAYS,
          "| MAX_BACKFILL_DAYS =", settings.MAX_BACKFILL_DAYS)

    print("")
    print("[1] 库里完全没数据")
    check("最新数据日", scrape_day.newest_day(), "None")
    s, e = scrape_day.catchup_range()
    check("结束日 = 昨天", e, YESTERDAY.isoformat())
    check("起始日 = 昨天往前 REFRESH_DAYS-1", s, d(settings.REFRESH_DAYS - 1))

    print("")
    print("[2] 数据已到昨天 → 只做回刷")
    seed_upto(0)
    check("库内最新", scrape_day.newest_day(), d(0))
    s, e = scrape_day.catchup_range()
    check("起始日", s, d(settings.REFRESH_DAYS - 1))
    check("结束日", e, YESTERDAY.isoformat())

    print("")
    print("[3] 停机 5 天 → 补齐缺口（缺口比回刷窗口更长）")
    ds.invalidate_cache()
    for p in (settings.DAILY_DIR / "main").iterdir():
        for f in p.glob("*.json.gz"):
            f.unlink()
    ds.invalidate_cache("main")
    seed_upto(5)
    check("库内最新", scrape_day.newest_day(), d(5))
    s, e = scrape_day.catchup_range()
    check("起始日 = 缺口第一天", s, d(4))
    check("结束日", e, YESTERDAY.isoformat())
    check("覆盖天数", len(ds.day_span(s, e)), 5)

    print("")
    print("[4] 停机 200 天 → 被 MAX_BACKFILL_DAYS 兜住")
    ds.invalidate_cache()
    for p in (settings.DAILY_DIR / "main").iterdir():
        for f in p.glob("*.json.gz"):
            f.unlink()
    ds.invalidate_cache("main")
    seed_upto(200)
    s, e = scrape_day.catchup_range()
    check("起始日 = 昨天往前 MAX-1", s, d(settings.MAX_BACKFILL_DAYS - 1))
    check("覆盖天数", len(ds.day_span(s, e)), settings.MAX_BACKFILL_DAYS)

    print("")
    print("[5] max_days 显式覆盖")
    s, e = scrape_day.catchup_range(max_days=10)
    check("起始日 = 昨天往前 9", s, d(9))

    print("")
    print("[6] 已经有到昨天的数据 + 回刷窗口 = 只需 3 天")
    ds.invalidate_cache()
    for p in (settings.DAILY_DIR / "main").iterdir():
        for f in p.glob("*.json.gz"):
            f.unlink()
    ds.invalidate_cache("main")
    seed_upto(0)
    s, e = scrape_day.catchup_range()
    check("天数", len(ds.day_span(s, e)), settings.REFRESH_DAYS)

    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
