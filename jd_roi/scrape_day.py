# -*- coding: utf-8 -*-
"""按天抓取京准通 + 商智，写入 dailystore。

特点：
* **按天存储**，任意区间聚合纯本地计算
* **可续跑**：已存在的天默认跳过（--force 覆盖）
* **限流友好**：搜索词按花费降序截断、分页 sleep、命中 -3010 自动退避重试
* **商智签名头失效自动重捕**

用法：
    python -m jd_roi.scrape_day 2026-09-27 2026-10-03                 # 指定区间（两个账号）
    python -m jd_roi.scrape_day --days 3                              # 最近3天（到昨天）
    python -m jd_roi.scrape_day --account=b --force 2026-10-03 2026-10-03
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
import traceback

from . import dailystore as ds
from . import settings
from .browser import has_login, launch_profile, session_ok
from .scrape_jst_report import API as JST_API, COLS_ACCOUNT, COLS_SW
from .scrape_jzt import CAMPAIGN_API, CUSTOM_COLUMNS
from .scrape_keyword import COLUMNS as KW_COLS, KEYWORD_API
from .scrape_sz import INDICATORS, SzSession, URL_CORE, URL_FLOWSRC, URL_PRODUCT, _base

_JST_KEY = {"jst_campaign": "campaign", "jst_sku": "sku", "jst_searchword": "searchword"}
# 允许直接传接口短名（campaign/sku/searchword），避免调用方传错 key 时静默失败
_JST_KEY.update({v: v for v in ("campaign", "sku", "searchword")})

_GUARD = threading.Lock()
_LOCKED = False
_LOCK_THREAD: "threading.Thread | None" = None
_LOCK_OWNER: dict = {"name": None, "acquiredAt": None, "thread": None, "pid": None}
_CANCEL = threading.Event()
_RUNNING: dict = {"active": False, "account": None, "day": None, "kind": None,
                  "startedAt": None, "progress": "", "finishedAt": None, "error": None,
                  "cancelled": False, "result": None}


def is_running() -> bool:
    return bool(_RUNNING.get("active"))


def runtime_status() -> dict:
    d = dict(_RUNNING)
    d["lock"] = lock_info()
    d["cancelRequested"] = _CANCEL.is_set()
    d["taskId"] = (_CURRENT or {}).get("id")
    return d


def acquire(timeout: float = 0, name: str = "") -> bool:
    """占用抓取锁；已被占用时返回 False。"""
    global _LOCKED, _LOCK_THREAD
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while True:
        with _GUARD:
            if not _LOCKED:
                _LOCKED = True
                _LOCK_THREAD = threading.current_thread()
                _LOCK_OWNER.update({"name": name or _LOCK_THREAD.name,
                                    "acquiredAt": dt.datetime.now().isoformat(timespec="seconds"),
                                    "thread": _LOCK_THREAD.name, "pid": os.getpid()})
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def release() -> None:
    """释放抓取锁（幂等，未持有时静默返回）。"""
    global _LOCKED, _LOCK_THREAD
    with _GUARD:
        if not _LOCKED:
            return
        _LOCKED = False
        _LOCK_THREAD = None
        _LOCK_OWNER.update({"name": None, "acquiredAt": None, "thread": None, "pid": None})


def is_locked() -> bool:
    with _GUARD:
        return _LOCKED


def lock_info() -> dict:
    """锁的持有者信息。

    stale=True 表示「锁被占着，但持有它的线程已经不在了」——这正是以前
    网页触发抓取成功后忘记 release 造成的死锁状态，可以安全强制解锁。
    """
    with _GUARD:
        locked = _LOCKED
        info = dict(_LOCK_OWNER)
        th = _LOCK_THREAD
    alive = bool(th is not None and th.is_alive())
    if locked and not alive and is_running():
        alive = True  # 同步执行（CLI / 测试）时以 active 为准
    info["locked"] = locked
    info["alive"] = alive
    info["stale"] = bool(locked and not alive)
    return info


def reset_lock(force: bool = False) -> bool:
    """解锁卡死的锁。默认只在持有者已死（stale）时解；force=True 无条件解。"""
    info = lock_info()
    if not info["locked"] or (not force and not info["stale"]):
        return False
    release()
    _RUNNING.update({"active": False, "cancelled": False,
                     "progress": "已强制解锁卡死的抓取锁", "finishedAt":
                         dt.datetime.now().isoformat(timespec="seconds")})
    return True


def request_cancel() -> None:
    """请求当前抓取在最近一个检查点停下。"""
    _CANCEL.set()
    _RUNNING["cancelRequested"] = True


def cancel_running(force: bool = False) -> dict:
    """网页「取消/解锁」入口：有任务就请求取消，卡死的锁顺手解开。"""
    running = is_running()
    if running:
        request_cancel()
    else:
        _CANCEL.clear()
    released = reset_lock(force=bool(force))
    return {"running": running, "cancelRequested": _CANCEL.is_set(),
            "lockReleased": released, "status": runtime_status()}


def run_background(accounts=None, start: str | None = None, end: str | None = None,
                   force: bool = False, kinds=None, log=None, name: str = "scrape",
                   trigger: str = "manual") -> dict:
    """占锁 + 起后台线程抓取，**无论成功、失败、线程起不来，都保证释放锁**。

    以前网页/定时两条路径都是「在 except 里 release」，抓取成功就漏解，
    导致第一次抓完之后锁被永久占住，之后点抓取永远提示「已有抓取任务在运行」。
    """
    if not acquire(name=name):
        return {"ok": False, "error": "已有抓取任务在运行"}
    gen = _TASK_GEN

    def _work():
        try:
            scrape(accounts, start, end, force=force, kinds=kinds,
                   log=log or (lambda m: None), acquire_lock=False,
                   trigger=trigger, name=name)
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()[-500:]
            _RUNNING.update({"active": False, "error": tb})
            if _TASK_GEN == gen:
                # scrape 还没走到登记就崩了（例如调用签名不对）：也要在任务中心留下记录
                d_end = ds.norm_day(end or (ds.today() - dt.timedelta(days=1)))
                d_start = ds.norm_day(start or d_end)
                start_task(trigger, name, d_start, d_end,
                           [a.get("key") for a in (accounts or [])], force, kinds)
                finish_task("failed", None, tb)
        finally:
            release()  # 关键：成功路径也必须解锁

    th = threading.Thread(target=_work, name=name, daemon=True)
    try:
        th.start()
    except Exception:  # noqa: BLE001
        release()
        raise
    return {"ok": True, "start": start, "end": end, "force": bool(force),
            "accounts": [a.get("key") for a in (accounts or [])], "name": name,
            "trigger": trigger}


# ---------------------------------------------------------------- 任务中心
# 「当前任务 + 历史任务」注册表：供控制台的任务面板查看与操作。
# 历史落盘到 <DATA_DIR>/tasks.json（Web 进程与 CLI 进程都可能写，写前会与磁盘合并）。
_TASK_LOCK = threading.RLock()
_CURRENT: "dict | None" = None
_HISTORY: list = []
_HISTORY_LOADED = False
_TASK_GEN = 0          # 任务登记次数，用于判断「异常发生在登记之前」
_HISTORY_MAX = 200
_LOG_MAX = 400
_TASKS_FILE = settings.DATA_DIR / "tasks.json"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _read_tasks_file() -> list:
    try:
        data = json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    items = data.get("tasks") if isinstance(data, dict) else data
    return [t for t in (items or []) if isinstance(t, dict) and t.get("id")]


def _flush_history() -> None:
    """把内存历史与磁盘历史合并后落盘（CLI 与 Web 各自的写入互不覆盖）。

    顺序按 seq（毫秒时间戳）倒序：同一秒内起的多个任务不能靠 id 里的随机后缀比大小，
    否则「最新一条」会随机跳动。
    """
    merged: dict = {}
    for t in _read_tasks_file():
        merged[t["id"]] = t
    for t in _HISTORY:
        merged[t["id"]] = t
    items = sorted(merged.values(),
                   key=lambda t: (t.get("seq") or 0, t.get("startedAt") or ""),
                   reverse=True)[:_HISTORY_MAX]
    _HISTORY[:] = items
    try:
        _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TASKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tasks": items}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_TASKS_FILE)
    except Exception:  # noqa: BLE001
        pass


def _ensure_history() -> None:
    global _HISTORY_LOADED
    if _HISTORY_LOADED:
        return
    _HISTORY[:] = _read_tasks_file()[:_HISTORY_MAX]
    _HISTORY_LOADED = True


def _new_task_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def start_task(trigger: str, name: str | None, start: str, end: str,
               accounts: list, force: bool, kinds) -> dict:
    """登记一个正在运行的任务。"""
    global _CURRENT, _TASK_GEN
    _TASK_GEN += 1
    rec = {"id": _new_task_id(), "name": name or "scrape", "trigger": trigger,
           "seq": int(time.time() * 1000), "status": "running", "start": start, "end": end,
           "days": len(ds.day_span(start, end)), "accounts": list(accounts or []),
           "force": bool(force), "kinds": list(kinds or []),
           "startedAt": _now(), "finishedAt": None, "elapsedSec": None,
           "progress": "starting", "error": None, "summary": None, "log": []}
    with _TASK_LOCK:
        _ensure_history()
        _CURRENT = rec
    return rec


def append_log(msg: str) -> None:
    """任务日志：既进环形缓冲，也作为当前进度展示。"""
    _RUNNING["progress"] = msg
    with _TASK_LOCK:
        if _CURRENT is None:
            return
        _CURRENT["progress"] = msg
        log = _CURRENT["log"]
        log.append(f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}")
        if len(log) > _LOG_MAX:
            del log[:len(log) - _LOG_MAX]


def finish_task(status: str, summary=None, error: str | None = None) -> dict | None:
    """结束当前任务并落到历史。"""
    global _CURRENT
    with _TASK_LOCK:
        rec = _CURRENT
        if rec is None:
            return None
        rec.update({"status": status, "finishedAt": _now(), "error": error,
                    "summary": summary})
        started = rec.get("startedAt")
        try:
            rec["elapsedSec"] = int((dt.datetime.now()
                                     - dt.datetime.fromisoformat(started)).total_seconds())
        except Exception:  # noqa: BLE001
            rec["elapsedSec"] = None
        _ensure_history()
        _HISTORY.insert(0, rec)
        del _HISTORY[_HISTORY_MAX:]
        _CURRENT = None
        _flush_history()
    return rec


def _trim(rec: dict, log_n: int) -> dict:
    out = dict(rec)
    log = out.get("log") or []
    out["logCount"] = len(log)
    out["log"] = log[-log_n:]
    # 运行中的任务由服务端算耗时：浏览器和服务器可能不在同一时区，
    # 让前端拿时间字符串自己相减会算出 8 小时的误差。
    if out.get("status") == "running":
        try:
            out["elapsedSec"] = int((dt.datetime.now()
                                     - dt.datetime.fromisoformat(out["startedAt"])).total_seconds())
        except Exception:  # noqa: BLE001
            pass
    return out


def task_list(limit: int = 50) -> dict:
    """任务中心数据源：运行中的任务 + 历史任务（最新在前）。"""
    limit = max(1, min(int(limit or 50), _HISTORY_MAX))
    with _TASK_LOCK:
        _ensure_history()
        cur = _trim(_CURRENT, 12) if _CURRENT else None
        items = [_trim(t, 6) for t in _HISTORY[:limit]]
    return {"running": cur, "items": items, "count": len(items),
            "lock": lock_info(), "cancelRequested": _CANCEL.is_set()}


def get_task(task_id: str) -> dict | None:
    with _TASK_LOCK:
        if _CURRENT and _CURRENT.get("id") == task_id:
            return json.loads(json.dumps(_trim(_CURRENT, _LOG_MAX)))
        _ensure_history()
        for t in _HISTORY:
            if t.get("id") == task_id:
                return json.loads(json.dumps(t))
    return None


def cancel_task(task_id: str) -> dict:
    """取消指定任务（只对正在运行的那个生效）。"""
    with _TASK_LOCK:
        cur = dict(_CURRENT) if _CURRENT else None
    if not cur or cur.get("id") != task_id:
        return {"ok": False, "error": "该任务不在运行中，无需取消"}
    request_cancel()
    return {"ok": True, "taskId": task_id, "cancelRequested": True,
            "message": "已请求取消，任务会在当前这一天结束后停止"}


def rerun_task(task_id: str, force: bool | None = None) -> dict:
    """按历史任务的原参数重新发起一次抓取。"""
    rec = get_task(task_id)
    if rec is None:
        return {"ok": False, "error": "任务不存在"}
    if is_running():
        return {"ok": False, "error": "已有抓取任务在运行"}
    keys = rec.get("accounts") or []
    accs = [a for a in settings.ACCOUNTS if a["key"] in keys] or settings.ACCOUNTS
    return run_background(accs, rec.get("start"), rec.get("end"),
                          force=bool(rec.get("force") if force is None else force),
                          log=None, name="scrape", trigger="rerun")


def clear_history() -> int:
    """清掉已结束任务的历史（正在运行的任务保留）。"""
    global _HISTORY
    with _TASK_LOCK:
        _ensure_history()
        n = len(_HISTORY)
        _HISTORY.clear()
        try:
            _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TASKS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tasks": []}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_TASKS_FILE)
        except Exception:  # noqa: BLE001
            pass
    return n


_JZT_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""
_JST_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/jst/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""
_KW_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/index.html/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _post(page, js, url, payload, retries: int = 5):
    last = None
    for attempt in range(retries):
        res = page.evaluate(js, {"url": url, "payload": payload})
        if res["status"] == 200:
            try:
                d = json.loads(res["text"])
            except Exception:  # noqa: BLE001
                d = {"code": -1, "msg": res["text"][:200]}
            code = d.get("code")
            if code == -3010 or (isinstance(code, int) and code < 0 and "上限" in str(d.get("msg"))):
                time.sleep(settings.RETRY_SLEEP)
                last = d
                continue
            return d
        last = {"code": res["status"], "msg": res["text"][:200]}
        time.sleep(3)
    raise RuntimeError(f"请求失败 {url}: {json.dumps(last, ensure_ascii=False)[:200]}")


def _parse_day(s):
    """'20260921' -> '2026-09-21'；区间串 '20260921~20260927' -> None。"""
    t = str(s or "")
    if "~" in t or "-" in t and "~" in t:
        return None
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return None


# ---------------------------------------------------------------- 京准通
def fetch_jzt_day(page, day: str) -> dict:
    rows, no = [], 1
    while True:
        d = _post(page, _JZT_FETCH, CAMPAIGN_API, {
            "page": no, "pageSize": 50, "status": "", "filters": [], "obys": "",
            "startDay": day, "endDay": day, "clickOrOrderCaliber": 0, "clickOrOrderDay": 15,
            "giftFlag": 0, "orderStatusCategory": 1, "customColumns": CUSTOM_COLUMNS, "requestFrom": 0})
        if d.get("code") != 1:
            raise RuntimeError(f"jzt: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        b = body.get("data") or []
        rows += b
        if len(b) < 50 or no > 20:
            break
        no += 1
        time.sleep(settings.PAGE_SLEEP)
    return {"rows": rows, "dateStart": day, "dateEnd": day}


def fetch_jst_multiday(page, kind: str, start: str, end: str) -> dict:
    """智能投放计划/商品：一次 isDaily 请求拿到区间内每天，再按天拆分。"""
    cols = COLS_SW if kind == "searchword" else COLS_ACCOUNT
    rows, no = [], 1
    while True:
        d = _post(page, _JST_FETCH, JST_API[_JST_KEY.get(kind, kind)], {
            "isDaily": True, "startDay": start, "endDay": end, "obys": "impressions|desc",
            "filters": [], "clickOrOrderDay": 15, "clickOrOrderCaliber": 0,
            "orderStatusCategory": None, "page": no, "pageSize": 200,
            "giftFlag": 0, "promotionMode": None, "columns": cols, "requestFrom": 0})
        if str(d.get("code")) != "1":
            raise RuntimeError(f"jst {kind}: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        b = body.get("datas") or []
        rows += b
        if len(b) < 200 or no > 60:
            break
        no += 1
        time.sleep(settings.PAGE_SLEEP)
    by_day: dict = {}
    for r in rows:
        day = _parse_day(r.get("date"))
        if not day:
            continue
        by_day.setdefault(day, []).append(r)
    return by_day


def fetch_jst_day(page, kind: str, day: str, max_rows: int | None = None) -> dict:
    cols = COLS_SW if kind == "searchword" else COLS_ACCOUNT
    obys = "cost|desc" if kind == "searchword" else "impressions|desc"
    cap = max_rows or (settings.SW_MAX_ROWS if kind == "searchword" else 10 ** 9)
    rows, no = [], 1
    while True:
        d = _post(page, _JST_FETCH, JST_API[_JST_KEY.get(kind, kind)], {
            "isDaily": False, "startDay": day, "endDay": day, "obys": obys, "filters": [],
            "clickOrOrderDay": 15, "clickOrOrderCaliber": 0, "orderStatusCategory": None,
            "page": no, "pageSize": 200, "giftFlag": 0, "promotionMode": None,
            "columns": cols, "requestFrom": 0})
        if str(d.get("code")) != "1":
            raise RuntimeError(f"jst {kind}: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        b = body.get("datas") or []
        rows += b
        if len(b) < 200 or len(rows) >= cap or no > 60:
            break
        no += 1
        time.sleep(settings.PAGE_SLEEP)
    return {"rows": rows, "dateStart": day, "dateEnd": day}


def fetch_kw_day(page, day: str, account_id: int) -> dict:
    rows, no = [], 1
    while True:
        d = _post(page, _KW_FETCH, KEYWORD_API, {
            "isDaily": False, "startDay": day, "endDay": day, "clickOrOrderCaliber": 0,
            "clickOrOrderDay": 15, "giftFlag": 0, "orderStatusCategory": 1, "filters": [],
            "pinIds": [account_id], "targetingType": "", "columns": KW_COLS,
            "obys": "impressions|desc", "page": no, "pageSize": 200, "needGdEffectOrder": False})
        if d.get("code") != 1:
            raise RuntimeError(f"kw: {json.dumps(d, ensure_ascii=False)[:200]}")
        body = d.get("data") or {}
        b = body.get("datas") or []
        rows += b
        if len(b) < 200 or no > 20:
            break
        no += 1
        time.sleep(settings.PAGE_SLEEP)
    return {"rows": rows, "dateStart": day, "dateEnd": day}


# ---------------------------------------------------------------- 商智
class SzFetcher:
    """商智动态签名头存活很短：失败就重开页面重新捕获。"""

    def __init__(self, context, kind: str):
        self.context = context
        self.kind = kind
        self.page = None
        self.sess = None

    def _new(self):
        if self.page is not None:
            try:
                self.page.close()
            except Exception:  # noqa: BLE001
                pass
        page = self.context.new_page()
        s = SzSession(page, self.kind)
        s.capture(wait_ms=settings.SZ_CAPTURE_WAIT)
        self.page, self.sess = page, s
        return s

    def fetch(self, day: str):
        for attempt in range(3):
            s = self.sess or self._new()
            cstart = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
            base = _base(day, day, cstart, cstart, s.brands, s.cates)
            try:
                if self.kind == "flow":
                    core = s.post(URL_CORE, base)
                    src = s.post(URL_FLOWSRC, dict(
                        base, referIndicator=SRC_UV_REF, sort="top", attentionType="",
                        groups=["jdr_sch_traffic_cha_last_field_src_rmad_sz_2"],
                        attributes=["jdr_sch_traffic_cha_last_field_src_rmad_sz_2"]))
                    code = (core.get("header") or {}).get("code")
                    data = (core.get("body") or {}).get("data") or []
                    if code == 0 and data:
                        return {"core": data, "source": (src.get("body") or {}).get("data") or [],
                                "rawCode": 0, "dateStart": day, "dateEnd": day}
                else:
                    d = s.post(URL_PRODUCT, dict(base, proType="sku", indicators=INDICATORS,
                                                 onlyAttention=False))
                    code = (d.get("header") or {}).get("code")
                    rows = (d.get("body") or {}).get("data") or []
                    if code == 0 and rows:
                        return {"rows": rows, "code": code, "dateStart": day, "dateEnd": day}
            except Exception:  # noqa: BLE001
                pass
            self.sess = None  # 强制重新捕获
            time.sleep(2)
        # 三天都拿不到：返回空，但仍落盘（避免每次重抓）
        if self.kind == "flow":
            return {"core": [], "source": [], "rawCode": -1, "dateStart": day, "dateEnd": day}
        return {"rows": [], "code": -1, "dateStart": day, "dateEnd": day}

    def close(self):
        if self.page is not None:
            try:
                self.page.close()
            except Exception:  # noqa: BLE001
                pass


SRC_UV_REF = "jdr_sch_traffic_brow_sku_cnt_jd_unified_attribution_sz"


# ---------------------------------------------------------------- 操作日志
def fetch_oplog_range(page, acct_key: str, start: str, end: str) -> dict:
    # 注意：scrape_oplog._post(page, url, payload) 只接 3 个参数
    from .scrape_oplog import LEVELS, URL_QUERY, _post as _op_post
    # 必须先在这个页面里打开操作日志页：新开的空白页 origin 是 about:blank，
    # 在它上面 fetch jzt-api 会直接 "TypeError: Failed to fetch"（没有 origin/referer/cookie 上下文）
    try:
        page.goto("https://jzt.jd.com/logging/#/business?businessType=-16",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
    except Exception:  # noqa: BLE001
        pass
    win = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    rows, seen, errors = [], set(), []
    while win <= end_d:
        wend = min(win + dt.timedelta(days=6), end_d)
        for lvl in LEVELS:
            try:
                page_no = 1
                while page_no <= 20:
                    d = _op_post(page, URL_QUERY, {
                        "btype": -16, "pageSize": 100, "pageIndex": page_no, "type": lvl,
                        "beginTime": win.isoformat(), "endTime": wend.isoformat(),
                        "keywordId": "", "searchType": 1, "operator": "", "optContent": ""})
                    body = d.get("data") or {}
                    batch = body.get("data") or []
                    for r in batch:
                        k = "|".join(str(r.get(x)) for x in
                                     ("optTime", "actionObjectId", "operationContent", "operationDetails"))
                        if k in seen:
                            continue
                        seen.add(k)
                        r["_level"] = lvl
                        r["_btype"] = -16
                        rows.append(r)
                    if len(batch) < 100 or page_no * 100 >= (body.get("count") or 0):
                        break
                    page_no += 1
                    time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                # 不要静默吞掉：以前这里 pass 掉了一个参数签名错误，导致操作日志整块为空
                errors.append(f"{lvl}@{win}~{wend}: {exc}")
        win = wend + dt.timedelta(days=1)
    rows.sort(key=lambda r: str(r.get("optTime") or ""))
    if errors and not rows:
        raise RuntimeError("操作日志抓取失败：" + "；".join(errors[:3]))
    return {"rows": rows, "begin": start, "end": end, "errors": errors}


# ---------------------------------------------------------------- 主流程
def scrape(accounts=None, start: str | None = None, end: str | None = None,
           force: bool = False, kinds=None, log=lambda m: None,
           acquire_lock: bool = True, trigger: str = "cli",
           name: str | None = None) -> dict:
    """抓取 [start, end]（含）每天的数据。已存在的天默认跳过。"""
    settings.ensure_dirs()
    accounts = accounts or settings.ACCOUNTS
    end = ds.norm_day(end or (ds.today() - dt.timedelta(days=1)))
    start = ds.norm_day(start or end)
    days = ds.day_span(start, end)
    kinds = kinds or ds.KINDS

    if acquire_lock and not acquire(name="scrape"):
        raise RuntimeError("已有抓取任务在运行")
    _CANCEL.clear()
    _RUNNING.update({"active": True, "startedAt": dt.datetime.now().isoformat(timespec="seconds"),
                     "finishedAt": None, "error": None, "result": None,
                     "cancelled": False, "progress": "starting"})
    start_task(trigger, name, start, end, [a.get("key") for a in accounts], force, kinds)
    summary = {"start": start, "end": end, "accounts": {}, "startedAt": _RUNNING["startedAt"],
               "cancelled": False}

    raw_log = log

    def log(msg):  # noqa: F811  统一走「任务日志 + 调用方回调」两条路
        append_log(msg)
        raw_log(msg)

    def _stopped() -> bool:
        """是否收到了取消请求（在账号之间、每天之间检查）。"""
        if _CANCEL.is_set():
            summary["cancelled"] = True
            log("收到取消请求，已停止抓取")
            return True
        return False

    err: str | None = None
    try:
        for acct in accounts:
            if _stopped():
                break
            key = acct["key"]
            label = acct.get("label") or key
            acc_sum = {"label": label, "days": {}, "errors": []}
            summary["accounts"][key] = acc_sum
            todo = [d for d in days if force or not _day_complete(key, d, kinds)]
            if not todo:
                log(f"[{label}] {start}~{end} 全部已存在，跳过")
                continue
            log(f"[{label}] 需要抓取 {len(todo)} 天：{todo[0]} ~ {todo[-1]}")
            _RUNNING["account"] = label
            pw, context = launch_profile(headless=settings.HEADLESS, account=key)
            try:
                p_jzt = context.new_page()
                p_kw = context.new_page()
                # 必须先访问京准通首页建立 SSO，否则 atoms-api/jzt-api 直接返回 -100 登录失败
                p_jzt.goto("https://jzt.jd.com/msa/#/list/tab/plan?objective=overview",
                           wait_until="domcontentloaded", timeout=90000)
                p_jzt.wait_for_timeout(9000)
                if not (has_login(context) and session_ok(p_jzt)):
                    raise RuntimeError("登录态已失效（服务端判定未登录），请先在控制台 /login 重新扫码")
                p_jzt.goto("https://jzt.jd.com/jst/#/report/account",
                           wait_until="domcontentloaded", timeout=90000)
                p_jzt.wait_for_timeout(9000)
                need_kw = [k for k in ("kw",) if k in kinds]
                if need_kw:
                    p_kw.goto("https://jzt.jd.com/report/index.html/#/overview/page", wait_until="domcontentloaded", timeout=90000)
                    p_kw.wait_for_timeout(7000)
                    p_kw.goto("https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1", wait_until="domcontentloaded", timeout=90000)
                    p_kw.wait_for_timeout(8000)

                # --- 智能投放 计划/商品：一次请求覆盖整个区间 ---
                if "jst_campaign" in kinds or "jst_sku" in kinds:
                    for kind in ("jst_campaign", "jst_sku"):
                        if kind not in kinds:
                            continue
                        try:
                            by_day = fetch_jst_multiday(p_jzt, kind, start, end)
                            for day in todo:
                                rows = by_day.get(day)
                                if rows is None and not force and ds.has_day(key, day, kind):
                                    continue
                                ds.save_day(key, day, kind, {"rows": rows or [], "dateStart": day,
                                                             "dateEnd": day})
                                acc_sum["days"].setdefault(day, []).append(kind)
                            log(f"[{label}] {kind} 按天拆分完成，覆盖 {len(by_day)} 天")
                        except Exception as exc:  # noqa: BLE001
                            acc_sum["errors"].append(f"{kind}: {exc}")
                            log(f"[{label}] {kind} 失败：{exc}")

                sz = {}
                for day in todo:
                    if _stopped():
                        break
                    _RUNNING["day"] = day
                    got = []
                    for kind, fn in (("jzt_campaign", lambda: fetch_jzt_day(p_jzt, day)),
                                     ("jst_searchword", lambda: fetch_jst_day(p_jzt, "searchword", day)),
                                     ("kw", lambda: fetch_kw_day(p_kw, day, acct.get("account_id") or 0))):
                        if kind not in kinds:
                            continue
                        if not force and ds.has_day(key, day, kind):
                            continue
                        try:
                            ds.save_day(key, day, kind, fn())
                            got.append(kind)
                        except Exception as exc:  # noqa: BLE001
                            acc_sum["errors"].append(f"{kind}@{day}: {exc}")
                    for kind in ("sz_product", "sz_flow"):
                        if kind not in kinds:
                            continue
                        if not force and ds.has_day(key, day, kind):
                            continue
                        if kind not in sz:
                            sz[kind] = SzFetcher(context, "flow" if kind == "sz_flow" else "product")
                        try:
                            ds.save_day(key, day, kind, sz[kind].fetch(day))
                            got.append(kind)
                        except Exception as exc:  # noqa: BLE001
                            acc_sum["errors"].append(f"{kind}@{day}: {exc}")
                    for f in sz.values():
                        f.close()
                    sz = {}
                    acc_sum["days"][day] = acc_sum["days"].get(day, []) + got
                    log(f"[{label}] {day} 完成：{','.join(got) if got else '全部已存在'}")

                if "oplog" in kinds:
                    try:
                        pl = fetch_oplog_range(context.new_page(), key, start, end)
                        by_day: dict = {}
                        for r in pl["rows"]:
                            by_day.setdefault(str(r.get("optTime") or "")[:10], []).append(r)
                        # 没有操作的那天也要落盘（rows=[]），否则「这天抓过」无从判断，
                        # 会导致 _day_complete 永远为 False、覆盖度看起来缺天
                        for day in days:
                            ds.save_day(key, day, "oplog",
                                        {"rows": by_day.get(day, []),
                                         "dateStart": day, "dateEnd": day})
                        log(f"[{label}] 操作日志 {len(pl['rows'])} 条，覆盖 {len(by_day)} 天"
                            f"（{len(days)} 天已标记为已抓取）")
                    except Exception as exc:  # noqa: BLE001
                        acc_sum["errors"].append(f"oplog: {exc}")
                        log(f"[{label}] 操作日志失败：{exc}")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            ds.invalidate_cache(key)
    except BaseException as exc:
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        finished = dt.datetime.now().isoformat(timespec="seconds")
        cancelled = bool(summary.get("cancelled"))
        status = "failed" if err else ("cancelled" if cancelled else "done")
        _RUNNING.update({"active": False, "finishedAt": finished, "cancelled": cancelled,
                         "error": err, "result": summary})
        _CANCEL.clear()
        finish_task(status, summary, err)
        if acquire_lock:
            release()
    ds.write_state(lastRun=summary, lastRunAt=finished)
    return summary


def _day_complete(key: str, day: str, kinds) -> bool:
    want = [k for k in kinds if k in ds.KINDS]
    return all(ds.has_day(key, day, k) for k in want)


def default_range(days: int | None = None):
    """默认抓「昨天」往前 N 天（含昨天）。"""
    n = days or settings.REFRESH_DAYS
    end = ds.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=max(n - 1, 0))
    return start.isoformat(), end.isoformat()


def newest_day(accounts=None) -> str | None:
    """所有账号里最新一天的数据日期。"""
    accounts = accounts or settings.ACCOUNTS
    newest = None
    for a in accounts:
        for kind in ("jzt_campaign", "jst_campaign", "sz_product", "kw"):
            days = ds.days_for(a["key"], kind)
            if days:
                newest = days[-1] if newest is None else max(newest, days[-1])
    return newest


def catchup_range(accounts=None, max_days: int | None = None):
    """真正需要抓的区间 = 「补齐缺口」 ∪ 「回刷最近 N 天」。

    容器停几天、甚至停一个月再起来时，只抓最近 N 天会留下空洞；
    这里从库里最新一天的下一天开始补，并用 max_days 兜住上限。
    返回 (start, end)；已经是最新且无需回刷时返回 None。
    """
    accounts = accounts or settings.ACCOUNTS
    end = ds.today() - dt.timedelta(days=1)
    refresh_start = end - dt.timedelta(days=max(settings.REFRESH_DAYS - 1, 0))
    cap = max_days if max_days is not None else settings.MAX_BACKFILL_DAYS
    floor = end - dt.timedelta(days=max(cap - 1, 0))

    newest = newest_day(accounts)
    if not newest:
        start = max(refresh_start, floor)
    else:
        start = min(dt.date.fromisoformat(newest) + dt.timedelta(days=1), refresh_start)
        start = max(start, floor)
    if start > end:
        return None
    return start.isoformat(), end.isoformat()


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    account = None
    force = False
    days = None
    rest = []
    for a in argv:
        if a.startswith("--account="):
            account = a.split("=", 1)[1]
        elif a == "--force":
            force = True
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1])
        else:
            rest.append(a)
    accs = [settings.get_account(account)] if account else settings.ACCOUNTS
    if rest and len(rest) >= 2:
        start, end = rest[0], rest[1]
    else:
        start, end = default_range(days)
    print(f"[scrape_day] {start} ~ {end} accounts={[a['key'] for a in accs]} force={force}", flush=True)
    res = scrape(accs, start, end, force=force, log=lambda m: print(m, flush=True))
    for k, v in res["accounts"].items():
        print(f"  {v['label']}: 天数 {len(v['days'])} 错误 {len(v['errors'])}")
        for e in v["errors"][:8]:
            print("    !", e)
    print("[scrape_day] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
