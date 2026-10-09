# -*- coding: utf-8 -*-
"""抓取锁回归验证（不联网、不碰真实数据目录）。

覆盖这次修的 bug：
  1) 后台抓取**成功后**必须释放锁（以前锁被永久占住，页面显示「空闲」却一直报
     「已有抓取任务在运行」，而且无法查看/取消）
  2) 后台抓取**抛异常**后也必须释放锁
  3) 运行中的任务可被取消，取消后锁释放、状态可见
  4) 卡死的锁（持有线程已死）能被识别为 stale 并强制解锁
  5) webapp.api_run / _nightly_job 走的是同一个安全入口
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="jdlock_")
os.environ.update({
    "JD_DATA_DIR": str(Path(TMP) / "data"),
    "JD_AUTH_DIR": str(Path(TMP) / "auth"),
    "JD_RUN_ON_START": "0",
    "JD_ENABLE_SCHEDULER": "0",
    "JD_ACCOUNTS": json.dumps([{"key": "main", "label": "主账号", "account_id": 111}]),
})
sys.path.insert(0, str(ROOT))

from jd_roi import scrape_day, webapp  # noqa: E402

FAILS: list = []
ACCS = [{"key": "main", "label": "主账号", "account_id": 111}]


def check(name, got, want):
    ok = got == want
    print(("      OK  " if ok else "  FAIL  ") + f"{name}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        FAILS.append(name)


def wait(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def make_stub(body):
    """模拟 scrape：占/放锁的语义与真实实现一致。"""
    def _f(accounts=None, start=None, end=None, force=False, kinds=None,
           log=None, acquire_lock=True, trigger="cli", name=None):
        if acquire_lock and not scrape_day.acquire(name="stub"):
            raise RuntimeError("已有抓取任务在运行")
        scrape_day._RUNNING.update({"active": True, "progress": "starting",
                                    "cancelled": False, "error": None})
        cancelled = False
        try:
            cancelled = bool(body())
        finally:
            scrape_day._RUNNING.update({"active": False, "cancelled": cancelled,
                                        "finishedAt": "2026-10-08 00:00:00"})
            scrape_day._CANCEL.clear()
            if acquire_lock:
                scrape_day.release()
        return {"start": start, "end": end, "accounts": {}, "cancelled": cancelled}
    return _f


def lean_body(seconds: float):
    """会响应取消的慢任务（真实 scrape 在每个检查点看 _CANCEL）。"""
    def _b():
        for _ in range(int(seconds / 0.05)):
            if scrape_day._CANCEL.is_set():
                return True
            time.sleep(0.05)
        return False
    return _b


def main() -> int:
    real = scrape_day.scrape

    print("[1] 后台抓取成功后释放锁（本次 bug 根因）")
    scrape_day.scrape = make_stub(lean_body(0.3))
    r1 = scrape_day.run_background(ACCS, "2026-10-01", "2026-10-01", name="scrape")
    check("首次触发成功", r1.get("ok"), True)
    r2 = scrape_day.run_background(ACCS, "2026-10-01", "2026-10-01", name="scrape")
    check("运行中重复触发被拒", r2.get("error"), "已有抓取任务在运行")
    check("任务结束后锁已释放", wait(lambda: not scrape_day.is_locked()), True)
    r3 = scrape_day.run_background(ACCS, "2026-10-01", "2026-10-01", name="scrape")
    check("可以再次触发（不再被死锁挡住）", r3.get("ok"), True)
    wait(lambda: not scrape_day.is_locked())

    print("")
    print("[2] 后台抓取抛异常也释放锁")
    def boom():
        raise RuntimeError("模拟抓取异常")
    scrape_day.scrape = make_stub(boom)
    res = scrape_day.run_background(ACCS, "2026-10-01", "2026-10-01", name="scrape")
    check("触发成功", res.get("ok"), True)
    check("异常后锁仍释放", wait(lambda: not scrape_day.is_locked()), True)
    check("异常被记进状态", bool(scrape_day.runtime_status().get("error")), True)

    print("")
    print("[3] 取消运行中的任务")
    scrape_day.scrape = make_stub(lean_body(30))
    scrape_day.run_background(ACCS, "2026-10-01", "2026-10-01", name="scrape")
    check("任务已起来", wait(lambda: scrape_day.is_running()), True)
    info = scrape_day.cancel_running()
    check("识别到运行中", info["running"], True)
    check("已请求取消", info["cancelRequested"], True)
    check("活任务的锁不被强解", info["lockReleased"], False)
    check("任务已取消", wait(lambda: scrape_day.runtime_status().get("cancelled")), True)
    check("取消后锁被正常释放", wait(lambda: not scrape_day.is_locked()), True)
    check("最终回到空闲", wait(lambda: not scrape_day.is_running()), True)
    check("取消标记已复位", scrape_day.runtime_status().get("cancelRequested"), False)

    print("")
    print("[4] 卡死的锁可识别并强制解锁")
    # 复现历史 bug 的终态：active=False，但锁还占着，且持有线程早已不在
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    with scrape_day._GUARD:
        scrape_day._LOCKED = True
        scrape_day._LOCK_THREAD = dead
        scrape_day._LOCK_OWNER.update({"name": "scrape", "acquiredAt": "2026-10-08 00:00:00",
                                       "thread": dead.name, "pid": 1234})
    scrape_day._RUNNING.update({"active": False})
    st = scrape_day.runtime_status()
    check("状态里带锁信息", st["lock"]["locked"], True)
    check("持有者线程已死 alive=False", st["lock"]["alive"], False)
    check("判定为 stale", st["lock"]["stale"], True)
    check("stale 锁可自动解锁", scrape_day.reset_lock(force=False), True)
    check("解锁后 is_locked 为假", scrape_day.is_locked(), False)
    check("force 未开时无锁可解", scrape_day.reset_lock(force=False), False)

    print("")
    print("[5] webapp 接口层")
    scrape_day.scrape = make_stub(lean_body(0.3))
    resp = webapp.api_run(start="2026-10-01", end="2026-10-01")
    check("/api/run 受理", resp.get("ok"), True)
    check("受理后锁被占用", wait(lambda: scrape_day.is_locked()), True)
    dup = webapp.api_run(start="2026-10-01", end="2026-10-01")
    check("并发 /api/run 被拒", dup.get("error"), "已有抓取任务在运行")
    check("抓完自动解锁", wait(lambda: not scrape_day.is_locked()), True)
    st = webapp.api_run_status()
    check("状态接口含 lock 字段", "lock" in st, True)
    check("状态接口含 cancelRequested", "cancelRequested" in st, True)
    again = webapp.api_run(start="2026-10-01", end="2026-10-01")
    check("再次触发成功", again.get("ok"), True)
    wait(lambda: not scrape_day.is_locked())
    cancel = webapp.api_run_cancel()
    check("空闲时取消不报错", cancel.get("ok"), True)
    check("空闲时无取消标记", cancel["cancelRequested"], False)

    print("")
    print("[6] 定时任务与页面渲染")
    scrape_day.scrape = make_stub(lean_body(0.1))
    webapp._nightly_job()
    check("_nightly_job 结束后锁已释放", wait(lambda: not scrape_day.is_locked()), True)
    html = webapp.dashboard(type("R", (), {"query_params": {}})()).body.decode("utf-8")
    check("页面含取消按钮", "取消 / 解锁" in html, True)
    check("页面含 doCancel", "function doCancel" in html, True)

    scrape_day.scrape = real
    print("")
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过：抓取锁不再泄漏，可查看、可取消、可解锁")
    return 0


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
