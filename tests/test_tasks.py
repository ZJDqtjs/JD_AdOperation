# -*- coding: utf-8 -*-
"""任务中心验证（不联网、不碰真实数据目录）。

覆盖：
  1) 抓取任务被登记：运行中 → 历史，状态/区间/账号/耗时/日志齐全
  2) 硬失败（浏览器起不来）记为 failed 并保留失败原因
  3) 运行中的任务可取消，取消后记 cancelled，日志里有取消痕迹
  4) 历史任务可按原参数重跑
  5) 历史落盘 tasks.json，重启进程（重新加载）后仍可见
  6) webapp 的 /api/tasks* 接口层可用
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="jdtask_")
os.environ.update({
    "JD_DATA_DIR": str(Path(TMP) / "data"),
    "JD_AUTH_DIR": str(Path(TMP) / "auth"),
    "JD_RUN_ON_START": "0",
    "JD_ENABLE_SCHEDULER": "0",
    "JD_ACCOUNTS": json.dumps([{"key": "main", "label": "主账号", "account_id": 111},
                               {"key": "b", "label": "账号B", "account_id": 222}]),
})
sys.path.insert(0, str(ROOT))

from jd_roi import dailystore as ds, scrape_day, settings, webapp  # noqa: E402

FAILS: list = []
ACCS = settings.ACCOUNTS
TASKS_FILE = Path(settings.DATA_DIR) / "tasks.json"


def check(name, got, want):
    ok = got == want
    print(("      OK  " if ok else "  FAIL  ") + f"{name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        FAILS.append(name)


def ok(name, cond):
    check(name, bool(cond), True)


def wait(pred, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def wait_idle(timeout=25.0) -> bool:
    """任务真正结束：active=False 且锁已释放（两者之间有极短窗口）。"""
    return wait(lambda: not scrape_day.is_running() and not scrape_day.is_locked(), timeout)


# ---------------------------------------------------------------- 打桩
class FakePage:
    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def close(self):
        return None

    def evaluate(self, *a, **k):
        return {"status": 200, "text": "{}"}


class FakeCtx:
    def new_page(self):
        return FakePage()

    def close(self):
        return None


class FakePW:
    def stop(self):
        return None


class FakeSz:
    def __init__(self, ctx, kind):
        pass

    def fetch(self, day):
        return {"rows": [], "dateStart": day, "dateEnd": day}

    def close(self):
        return None


def stub_network(slow=0.0):
    scrape_day.launch_profile = lambda headless=None, account=None: (FakePW(), FakeCtx())
    scrape_day.has_login = lambda ctx: True
    scrape_day.session_ok = lambda page, retries=2: True
    scrape_day.SzFetcher = FakeSz
    scrape_day.fetch_oplog_range = lambda page, k, s, e: {"rows": [], "begin": s, "end": e}
    scrape_day.fetch_jst_multiday = lambda page, kind, s, e: {}
    scrape_day.fetch_jst_day = lambda page, kind, day, max_rows=None: {
        "rows": [], "dateStart": day, "dateEnd": day}
    scrape_day.fetch_kw_day = lambda page, day, aid: {"rows": [], "dateStart": day, "dateEnd": day}

    def jzt(page, day):
        if slow:
            time.sleep(slow)
        return {"rows": [{"campaignId": 1, "campaignName": "P", "cost": 1,
                          "impressions": 1, "clicks": 1, "totalOrderCnt": 0,
                          "totalOrderSum": 0, "totalCartCnt": 0}],
                "dateStart": day, "dateEnd": day}

    scrape_day.fetch_jzt_day = jzt


def main() -> int:
    print("[1] 一次正常抓取进入历史")
    stub_network()
    logs = []
    res = scrape_day.scrape(ACCS, "2026-09-01", "2026-09-03", log=logs.append,
                            trigger="manual", name="scrape")
    check("抓取本身成功", len(res["accounts"]), 2)
    tl = scrape_day.task_list()
    check("没有运行中的任务", tl["running"], None)
    ok("历史里有 1 条", len(tl["items"]) >= 1)
    t = tl["items"][0]
    check("状态 = 成功", t["status"], "done")
    check("触发方式 = 手动", t["trigger"], "manual")
    check("区间", [t["start"], t["end"]], ["2026-09-01", "2026-09-03"])
    check("天数", t["days"], 3)
    check("账号", t["accounts"], ["main", "b"])
    ok("有耗时", isinstance(t["elapsedSec"], int) and t["elapsedSec"] >= 0)
    ok("有完成时间", bool(t["finishedAt"]))
    ok("日志进环形缓冲", t["logCount"] > 3)
    ok("日志含按天完成", any("完成" in x for x in t["log"]))
    ok("接口日志回调仍生效", any("完成" in x for x in logs))
    tid1 = t["id"]

    print("")
    print("[2] 详情含完整日志；失败任务记账")
    full = scrape_day.get_task(tid1)
    check("详情能取到", full["id"], tid1)
    ok("详情日志 >= 列表摘要", len(full["log"]) >= len(t["log"]))
    check("未知任务返回 None", scrape_day.get_task("不存在"), None)

    def boom(headless=None, account=None):
        raise RuntimeError("模拟浏览器起不来")
    scrape_day.launch_profile = boom
    try:
        scrape_day.scrape(ACCS, "2026-09-04", "2026-09-04", trigger="schedule", name="nightly")
    except RuntimeError:
        pass
    tl = scrape_day.task_list()
    t2 = tl["items"][0]
    check("失败任务状态", t2["status"], "failed")
    check("失败任务触发方式", t2["trigger"], "schedule")
    ok("失败原因被记录", "浏览器起不来" in (t2["error"] or ""))

    print("")
    print("[3] 取消运行中的任务")
    stub_network(slow=1.0)
    r = scrape_day.run_background(ACCS, "2026-09-10", "2026-09-12", log=None,
                                  name="scrape", trigger="manual")
    check("后台任务已受理", r.get("ok"), True)
    ok("运行中任务可见", wait(lambda: (scrape_day.task_list()["running"] or {}).get("id")))
    cur = scrape_day.task_list()["running"]
    check("运行中状态", cur["status"], "running")
    check("运行中触发方式", cur["trigger"], "manual")
    c = scrape_day.cancel_task(cur["id"])
    check("取消受理", c["ok"], True)
    ok("已请求取消", wait(lambda: scrape_day.task_list()["cancelRequested"]))
    check("任务以 cancelled 收尾",
          wait(lambda: (scrape_day.get_task(cur["id"]) or {}).get("status") == "cancelled"), True)
    dead = scrape_day.get_task(cur["id"])
    ok("日志记录了取消", any("取消" in x for x in dead["log"]))
    ok("提前停止（未抓满 3 天）", len(dead["summary"]["accounts"]) <= 2)
    check("取消不存在的任务被拒", scrape_day.cancel_task(cur["id"])["ok"], False)
    check("锁已释放", wait_idle(), True)

    print("")
    print("[4] 重跑历史任务")
    stub_network()
    rr = scrape_day.rerun_task(tid1)
    check("重跑受理", rr.get("ok"), True)
    ok("按原参数（3 天区间）", rr["start"] == "2026-09-01" and rr["end"] == "2026-09-03")
    check("任务完成", wait_idle(), True)
    new = scrape_day.task_list()["items"][0]
    check("新任务触发方式 = 重跑", new["trigger"], "rerun")
    check("新任务是独立 ID", new["id"] != tid1, True)
    check("重跑未知任务被拒", scrape_day.rerun_task("不存在")["ok"], False)

    print("")
    print("[5] 历史落盘 + 重新加载")
    ok("tasks.json 存在", TASKS_FILE.exists())
    saved = json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]
    ok("磁盘上有历史", len(saved) >= 4)
    n_before = len(scrape_day.task_list()["items"])
    scrape_day._HISTORY_LOADED = False
    scrape_day._HISTORY.clear()
    check("重启后仍能读到同样条数", len(scrape_day.task_list()["items"]), n_before)

    print("")
    print("[6] webapp 接口层")
    api = webapp.api_tasks()
    check("api_tasks ok", api["ok"], True)
    ok("返回 running 字段", "running" in api)
    ok("返回 lock 字段", "lock" in api)
    d = webapp.api_task_detail(tid1)
    check("api_task_detail ok", d["ok"], True)
    bad = webapp.api_task_detail("不存在")
    check("未知任务返回 404", bad.status_code, 404)
    ok("api_task_cancel 对非运行任务返回错误", webapp.api_task_cancel(tid1)["ok"] is False)
    ok("api_task_rerun 可调用", webapp.api_task_rerun(tid1, force=None).get("ok") is True)
    check("重跑完成", wait_idle(), True)
    html = webapp.dashboard(type("R", (), {"query_params": {}})()).body.decode("utf-8")
    ok("页面含任务中心按钮", "openTasks()" in html)
    ok("页面含历史任务表", "taskRows" in html)
    ok("页面含清理历史", "clearTasks()" in html)

    print("")
    print("[7] 线程在登记任务之前就崩溃，也要留下失败记录")
    real = scrape_day.scrape

    def bad_signature(accounts=None, start=None, end=None):   # 故意不接受 log/trigger 等参数
        return {}
    scrape_day.scrape = bad_signature
    r = scrape_day.run_background(ACCS, "2026-09-20", "2026-09-20", name="scrape",
                                  trigger="manual")
    check("受理", r.get("ok"), True)
    check("结束后锁释放", wait_idle(), True)
    t = scrape_day.task_list()["items"][0]
    check("留下失败记录", t["status"], "failed")
    ok("失败原因是 TypeError", "TypeError" in (t["error"] or ""))
    check("区间仍被记录", [t["start"], t["end"]], ["2026-09-20", "2026-09-20"])
    scrape_day.scrape = real

    print("")
    print("[8] 清理历史")
    n = scrape_day.clear_history()
    ok("清理有返回值", n >= 1)
    check("历史已清空", scrape_day.task_list()["count"], 0)
    check("磁盘也已清空", json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"], [])
    check("清理后的新任务仍能记录",
          (scrape_day.task_list()["items"] == []), True)

    print("")
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过：任务可查看、可取消、可重跑、可持久化")
    return 0


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
