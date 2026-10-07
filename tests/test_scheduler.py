# -*- coding: utf-8 -*-
"""调度测试：验证「每天 0 点自动抓取前一天」真的被接上。

不等到 0 点，而是：
  1) 断言 CronTrigger 的下一次触发时间就是「明天 00:05（Asia/Shanghai）」
  2) 直接调用 webapp._nightly_job()，断言它确实把「到昨天为止的回刷窗口」写进了按天库
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(__file__).resolve().parent.parent
_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_sched_", dir=str(_TMPROOT)))

os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ.setdefault("JD_COSTS_URL", "")   # 离线：测试不连供货方成本接口
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_EXCEL_PATH"] = str(TMP / "config" / "missing.xlsx")
os.environ["JD_ENABLE_SCHEDULER"] = "1"
os.environ["JD_RUN_ON_START"] = "0"
os.environ["JD_SCHEDULE_HOUR"] = "0"
os.environ["JD_SCHEDULE_MINUTE"] = "5"
os.environ["JD_REFRESH_DAYS"] = "4"
os.environ["JD_ACCOUNTS"] = json.dumps([{"key": "main", "label": "主账号", "account_id": 111}])
sys.path.insert(0, str(ROOT))

from jd_roi import dailystore as ds, indicators, scrape_day, settings, webapp  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)
    return ok


def check_true(name, cond):
    print("  [" + ("PASS" if cond else "FAIL") + "] " + name)
    if not cond:
        FAILS.append(name)
    return bool(cond)


class FakePage:
    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def inner_text(self, *a, **k):
        return ""

    def close(self):
        return None


class FakeCtx:
    def new_page(self):
        return FakePage()

    def close(self):
        return None


class FakePW:
    def stop(self):
        return None


def stub():
    scrape_day.launch_profile = lambda headless=None, account=None: (FakePW(), FakeCtx())
    scrape_day.has_login = lambda ctx: True
    scrape_day.session_ok = lambda page, retries=2: True
    scrape_day.fetch_jst_multiday = lambda page, kind, s, e: {}
    scrape_day.fetch_oplog_range = lambda page, k, s, e: {"rows": [], "begin": s, "end": e}

    def jzt(page, day):
        return {"rows": [{"campaignId": 1, "campaignName": "P", "campaignType": 61,
                          "impressions": 10, "clicks": 5, "cost": 9.9,
                          "totalOrderCnt": 1, "totalOrderSum": 39.9, "totalCartCnt": 2}],
                "dateStart": day, "dateEnd": day}

    scrape_day.fetch_jzt_day = jzt
    scrape_day.fetch_jst_day = lambda page, kind, day, max_rows=None: {"rows": [],
                                                                      "dateStart": day, "dateEnd": day}
    scrape_day.fetch_kw_day = lambda page, day, aid: {"rows": [], "dateStart": day, "dateEnd": day}

    class Sz:
        def __init__(self, ctx, kind):
            self.kind = kind

        def fetch(self, day):
            if self.kind == "flow":
                return {"core": [{indicators.SZ_UV: 1}], "source": [], "rawCode": 0,
                        "dateStart": day, "dateEnd": day}
            return {"rows": [], "code": 0, "dateStart": day, "dateEnd": day}

        def close(self):
            return None

    scrape_day.SzFetcher = Sz


def main() -> int:
    settings.ensure_dirs()
    stub()
    tz = ZoneInfo(settings.TZ)

    # ---------- 1) CronTrigger 下一次触发时间 ----------
    print("[1] CronTrigger 触发时间")
    webapp._startup()          # 真正走一遍启动逻辑（会创建 APScheduler）
    sch = getattr(webapp.app.state, "scheduler", None)
    check_true("启动后调度器已创建", sch is not None)
    if sch is not None:
        job = sch.get_job("nightly")
        check_true("nightly 任务已注册", job is not None)
        if job is not None:
            nxt = job.trigger.get_next_fire_time(None, dt.datetime.now(tz))
            print("      下一次触发:", nxt.isoformat())
            check("触发时=00:05", nxt.strftime("%H:%M"), "00:05")
            check("触发时区", nxt.strftime("%z"), dt.datetime.now(tz).strftime("%z"))
            nxt2 = job.trigger.get_next_fire_time(nxt, nxt + dt.timedelta(seconds=1))
            check("连续两天同一时刻", nxt2.strftime("%H:%M"), "00:05")
            check("间隔恰好 1 天", (nxt2.date() - nxt.date()).days, 1)

    # ---------- 2) 任务体确实抓「到昨天为止」的窗口 ----------
    print("")
    print("[2] _nightly_job 抓取窗口")
    want_start, want_end = scrape_day.default_range()
    print("      期望窗口:", want_start, "~", want_end, "(REFRESH_DAYS=" + str(settings.REFRESH_DAYS) + ")")
    check("窗口止于昨天", want_end, (ds.today() - dt.timedelta(days=1)).isoformat())

    webapp._nightly_job()
    for _ in range(60):
        time.sleep(0.5)
        if not scrape_day.is_running():
            break
    # _nightly_job 起线程后立刻返回，等状态落盘
    for _ in range(60):
        time.sleep(0.5)
        st = ds.read_state()
        if (st.get("lastRun") or {}).get("end") == want_end:
            break
    st = ds.read_state()
    got = st.get("lastRun") or {}
    check("state 记录了本次任务", got.get("end"), want_end)
    check("state 区间起点", got.get("start"), want_start)
    days = ds.days_for("main", "jzt_campaign")
    check("按天库天数 = REFRESH_DAYS", len(days), settings.REFRESH_DAYS)
    check("最早一天", days[0], want_start)
    check("最后一天", days[-1], want_end)

    if sch is not None:
        sch.shutdown(wait=False)
    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
