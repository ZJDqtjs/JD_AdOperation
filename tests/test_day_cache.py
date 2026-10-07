# -*- coding: utf-8 -*-
"""「已入库日期」索引的跨进程可见性测试。

抓取进程和 Web 进程各有一份缓存；如果没有 TTL，
Web 进程启动时扫一次就再也不更新，新抓的天在覆盖度里永远不出现。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_cache_", dir=str(_TMPROOT)))

os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_ACCOUNTS"] = json.dumps([{"key": "main", "label": "主账号", "account_id": 111}])
os.environ["JD_DAYS_CACHE_TTL"] = "0"          # 每次都重扫，便于确定性断言
os.environ["JD_ENABLE_SCHEDULER"] = "0"
sys.path.insert(0, str(ROOT))

from jd_roi import dailystore as ds, settings  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)


def main() -> int:
    settings.ensure_dirs()
    print("[1] TTL=0：外部写入立刻可见（模拟「抓取进程写了、Web 进程去读」）")
    check("初始为空", len(ds.days_for("main", "jzt_campaign")), 0)
    ds.save_day("main", "2026-10-01", "jzt_campaign", {"rows": []})
    check("写入后可见", len(ds.days_for("main", "jzt_campaign")), 1)
    # 绕过 save_day 的缓存失效，直接落文件，模拟「另一个进程」写入
    p = ds.day_path("main", "2026-10-02", "jzt_campaign")
    p.parent.mkdir(parents=True, exist_ok=True)
    import gzip
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump({"rows": []}, fh)
    check("外部进程落盘后也可见", len(ds.days_for("main", "jzt_campaign")), 2)

    print("")
    print("[2] 大 TTL 下靠显式失效（scrape_day 在同一进程内就是这么做的）")
    settings.DAYS_CACHE_TTL = 3600
    p3 = ds.day_path("main", "2026-10-03", "jzt_campaign")
    p3.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p3, "wt", encoding="utf-8") as fh:
        json.dump({"rows": []}, fh)
    check("TTL 内看不到（走缓存）", len(ds.days_for("main", "jzt_campaign")), 2)
    ds.invalidate_cache("main")
    check("invalidate 后可见", len(ds.days_for("main", "jzt_campaign")), 3)

    print("")
    print("[3] 结果按日期升序、按 kind 分开")
    ds.save_day("main", "2026-09-30", "jzt_campaign", {"rows": []})
    ds.invalidate_cache("main")
    days = ds.days_for("main", "jzt_campaign")
    check("升序", days, ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"])
    check("另一个 kind 为空", len(ds.days_for("main", "sz_product")), 0)

    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
