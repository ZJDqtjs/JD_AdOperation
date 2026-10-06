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
import sys
import threading
import time

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

_SCRAPE_LOCK = threading.Lock()
_RUNNING: dict = {"active": False, "account": None, "day": None, "kind": None,
                  "startedAt": None, "progress": "", "finishedAt": None, "error": None,
                  "result": None}


def is_running() -> bool:
    return bool(_RUNNING.get("active"))


def runtime_status() -> dict:
    return dict(_RUNNING)


def acquire(timeout: float = 0) -> bool:
    return _SCRAPE_LOCK.acquire(timeout=timeout) if timeout else _SCRAPE_LOCK.acquire(blocking=False)


def release() -> None:
    try:
        _SCRAPE_LOCK.release()
    except RuntimeError:
        pass


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
           acquire_lock: bool = True) -> dict:
    """抓取 [start, end]（含）每天的数据。已存在的天默认跳过。"""
    settings.ensure_dirs()
    accounts = accounts or settings.ACCOUNTS
    end = ds.norm_day(end or (ds.today() - dt.timedelta(days=1)))
    start = ds.norm_day(start or end)
    days = ds.day_span(start, end)
    kinds = kinds or ds.KINDS

    if acquire_lock and not acquire():
        raise RuntimeError("已有抓取任务在运行")
    _RUNNING.update({"active": True, "startedAt": dt.datetime.now().isoformat(timespec="seconds"),
                     "finishedAt": None, "error": None, "result": None, "progress": "starting"})
    summary = {"start": start, "end": end, "accounts": {}, "startedAt": _RUNNING["startedAt"]}
    try:
        for acct in accounts:
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
    finally:
        _RUNNING.update({"active": False, "finishedAt": dt.datetime.now().isoformat(timespec="seconds"),
                         "result": summary})
        if acquire_lock:
            release()
    ds.write_state(lastRun=summary, lastRunAt=_RUNNING["finishedAt"])
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
