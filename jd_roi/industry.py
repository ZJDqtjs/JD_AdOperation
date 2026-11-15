# -*- coding: utf-8 -*-
"""京准通「行业大盘」：地域 / 关键词 / 客户 / 品牌 / 趋势的抓取、缓存与规则层观察。

页面：报表中心 → 行业分析 → 行业大盘（key=data_center, path=/industry/market）
接口：https://jzt-api.jd.com/dataCenter/industry/v2/...

口径（非常重要）：
  * 行业侧「展现/点击/加购」都是**指数**，不是金额、也不是真实件数；
    只能拿行业 CTR / 转化率 / 加购率等**比率**和本店同类目对比，
    绝对量不能相加、不能当花费，更不能和本店成交额直接比大小。
  * 本店侧 self/detail 是真实投放数据（花费/点击/展现/订单），
    与现有按天库的 15 天点击归因口径一致。
  * 地域看板不含直投数据；品牌榜只覆盖 TOP100 品牌。

缓存：一份「账号 + 区间」一个 JSON，落在 settings.REPORT_DIR，
查询先命中本地文件，只有用户显式重抓或自动补齐才请求京东；
取消 / 中途失败 / 接口报错一律不落盘，缓存永不自动删除。
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import threading
import time
from pathlib import Path

from . import settings

API = "https://jzt-api.jd.com/dataCenter/industry/v2/"
HOME = "https://jzt.jd.com/report/index.html"
SCHEMA = 1
PREFIX = "industry"
# 京准通有风控：单请求之间 0.8~1.6 秒随机抖动，绝不并行轰炸
SLEEP_MIN, SLEEP_MAX = 0.8, 1.6
PAGE_SIZE = 50

CALIBER = (
    "行业大盘=京准通数据中心按三级类目汇总的指数化数据（15天/点击/不含赠品/成交订单）；"
    "展现/点击/加购均为指数，不是金额或真实件数，只能对比 CTR/转化率/加购率等比率，"
    "不能相加或当作花费；地域看板不含直投数据。"
)

# 同一账号+区间补齐失败后，30 分钟内不再自动重试（不写负缓存、不删任何文件）；
# 用户点「重新抓取」时 force=True 会绕过冷却。避免登录过期/接口 -301 时每开一次报表就打一轮京东。
FAILURE_COOLDOWN = 30 * 60
_FAILED_LOCK = threading.Lock()
_FAILED: dict[tuple[str, str, str], float] = {}


def _fail_key(account: str, start: str, end: str) -> tuple[str, str, str]:
    return (account, _norm_day(start), _norm_day(end))


def _mark_failed(account: str, start: str, end: str) -> None:
    with _FAILED_LOCK:
        _FAILED[_fail_key(account, start, end)] = time.time()


def _clear_failed(account: str, start: str, end: str) -> None:
    with _FAILED_LOCK:
        _FAILED.pop(_fail_key(account, start, end), None)


def _cooldown_keys(accounts, start: str, end: str) -> list[str]:
    now = time.time()
    with _FAILED_LOCK:
        return [str(k) for k in accounts
                if now - _FAILED.get(_fail_key(str(k), start, end), 0) < FAILURE_COOLDOWN]


_FETCH_JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/report/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


class Cancelled(Exception):
    """抓取被取消：不允许写缓存。"""


# ---------------------------------------------------------------- 小工具
def _f(v, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _r(v, nd: int = 2) -> float:
    return round(_f(v), nd)


def _label(key: str) -> str:
    try:
        return settings.get_account(key).get("label") or key
    except Exception:  # noqa: BLE001
        return key


def _norm_day(s) -> str:
    return str(s or "")[:10]


def _cache_path(account: str, start: str, end: str) -> Path:
    return Path(settings.REPORT_DIR) / f"{PREFIX}_{account}_{_norm_day(start)}_{_norm_day(end)}.json"


def _read_cache(account: str, start: str, end: str) -> dict | None:
    fp = _cache_path(account, start, end)
    if not fp.exists():
        return None
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if d.get("schema") != SCHEMA or d.get("account") != account:
        return None
    if _norm_day(d.get("start")) != _norm_day(start) or _norm_day(d.get("end")) != _norm_day(end):
        return None
    if not isinstance(d.get("datasets"), dict):
        return None
    return d


def _write_cache(payload: dict) -> Path:
    fp = _cache_path(payload["account"], payload["start"], payload["end"])
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(fp)
    return fp


def _rows(data) -> list:
    if isinstance(data, dict):
        return list(data.get("rows") or [])
    if isinstance(data, list):
        return list(data)
    return []


def _summary(data) -> dict:
    if isinstance(data, dict):
        s = data.get("summaries") or []
        if isinstance(s, list) and s and isinstance(s[0], dict):
            return s[0]
    return {}


def _fallback_summary(rows: list[dict]) -> dict:
    """接口偶尔不给 summaries：按行业指数加权还原比率（不换算金额）。"""
    imp = sum(_f(r.get("impressionExponent")) for r in rows)
    clk = sum(_f(r.get("clicksExp")) for r in rows)

    def wavg(field: str, weight: float) -> float:
        if weight <= 0:
            return 0.0
        return round(sum(_f(r.get(field)) * _f(r.get("clicksExp") or
                                              r.get("impressionExponent"))
                         for r in rows) / weight, 2)

    return {
        "ctr": round(clk / imp * 100, 2) if imp else wavg("ctr", clk or imp),
        "cvs": wavg("cvs", clk), "cartCVS": wavg("cartCVS", clk),
        "dealOrderRate": wavg("dealOrderRate", clk),
        "impressionExponent": imp, "clickExponent": clk,
    }


def _leaves(nodes, path: list | None = None) -> list[dict]:
    out: list[dict] = []
    path = path or []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        name = str(n.get("name") or "").strip()
        children = n.get("children") or []
        here = path + ([name] if name else [])
        if children:
            out.extend(_leaves(children, here))
        elif n.get("isLeaf") or n.get("id") is not None:
            out.append({"id": int(_f(n.get("id"))), "name": name,
                        "path": "/".join(here) or name})
    return out


def _choose_category(account: str, leaves: list[dict], override=None) -> dict | None:
    if not leaves:
        return None
    cid = override or settings.INDUSTRY_CID3.get(account) or settings.INDUSTRY_CID3.get("*")
    if cid:
        hit = next((x for x in leaves if x["id"] == int(_f(cid))), None)
        if hit:
            return hit
    return leaves[0]


def _base_payload(start: str, end: str, cat: dict) -> dict:
    return {
        "selfBrandName": "", "businessType": [-4, 256], "cid3": [cat["id"]],
        "clickOrOrderDay": 15, "clickOrOrderCaliber": 0, "isGift": 0,
        "orderStatusCategory": 1, "impressionOrClickCaliber": 0,
        "startDate": _norm_day(start), "endDate": _norm_day(end),
        "analyzeType": 2, "campaignTypes": [],
        "reviewCache": {
            "dateRange": f"{_norm_day(start)}至{_norm_day(end)}",
            "businessType": ", 站外广告", "cid3": cat.get("name") or "",
            "calibers": "15天/点击/不含赠品/成交订单",
        },
    }


def _call(page, path: str, payload: dict, cancel=None) -> dict | list:
    if cancel and cancel.is_set():
        raise Cancelled()
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
    res = page.evaluate(_FETCH_JS, {
        "url": API + path + "?requestFrom=0&businessFrom=1", "payload": payload,
    })
    if not isinstance(res, dict) or res.get("status") != 200:
        raise RuntimeError(f"{path} HTTP {res.get('status') if isinstance(res, dict) else '?'}")
    try:
        d = json.loads(res.get("text") or "")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{path} 返回的不是 JSON：{exc}") from exc
    if d.get("code") != 1:
        raise RuntimeError(f"{path} code={d.get('code')} msg={d.get('msg')}")
    return d.get("data")


# ---------------------------------------------------------------- 抓取
def _collect(page, account: str, start: str, end: str, *, cancel=None,
             progress=None, cid3=None) -> dict:
    """在已登录的京准通页面里串行走完所有看板接口。"""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    say("读取行业类目…")
    leaves = _leaves(_call(page, "industry/getCategory", {}, cancel))
    cat = _choose_category(account, leaves, cid3)
    if not cat:
        raise RuntimeError("京准通没有返回可用的三级类目")
    base = _base_payload(start, end, cat)
    kw_base = {k: v for k, v in base.items() if k != "businessType"}

    def ask(path: str, extra: dict | None = None, payload: dict | None = None,
            tag: str = "") -> tuple[list, dict]:
        say(tag or path)
        p = dict(payload if payload is not None else base)
        p.update(extra or {})
        data = _call(page, path, p, cancel)
        return _rows(data), _summary(data)

    whole, whole_sum = ask("industry/whole", tag="行业整体看板（分日趋势）")
    hourly, _ = ask("industry/whole", {"granularity": 9}, tag="行业时段分布")
    ind_detail, _ = ask("industry/detail", {"dimension": "product"}, tag="分产品线指数")
    self_detail, _ = ask("self/detail", {"dimension": "product"}, tag="本店投放对比")
    ind_cust, ind_cust_sum = ask("industry/user/customer", tag="行业新客/老客")
    self_cust, self_cust_sum = ask("self/user/customer", tag="本店新客/老客")
    area, _ = ask("industry/user/area", tag="行业地域看板")
    brands, _ = ask("industry/brands", tag="行业品牌榜")
    compete, _ = ask("keyword/compete",
                     {"pageNum": 1, "pageSize": PAGE_SIZE, "obys": ""},
                     payload=kw_base, tag="本店 vs 行业词效率")
    billboard, _ = ask("keyword/billboard",
                       {"pageNum": 1, "pageSize": PAGE_SIZE, "obys": ""},
                       payload=kw_base, tag="行业关键词榜")
    hot, _ = ask("industry/searchWord/hotWords", {"searchWordType": 0}, tag="行业热搜词")
    soaring, _ = ask("industry/searchWord/soaringWords", {"searchWordType": 0}, tag="行业飙升词")
    new, _ = ask("industry/searchWord/newWords", {"searchWordType": 0}, tag="行业新词")
    b_hot, _ = ask("industry/searchWord/hotWords", {"searchWordType": 1}, tag="行业品牌热词")
    b_soar, _ = ask("industry/searchWord/soaringWords", {"searchWordType": 1}, tag="行业品牌飙升词")
    b_new, _ = ask("industry/searchWord/newWords", {"searchWordType": 1}, tag="行业品牌新词")

    if cancel and cancel.is_set():
        raise Cancelled()
    datasets = {
        "whole": whole, "hourly": hourly, "industryDetail": ind_detail,
        "selfDetail": self_detail, "industryCustomer": ind_cust,
        "selfCustomer": self_cust, "area": area, "brands": brands,
        "compete": compete, "billboard": billboard,
        "hotWords": hot, "soaringWords": soaring, "newWords": new,
        "brandHotWords": b_hot, "brandSoaringWords": b_soar, "brandNewWords": b_new,
    }
    if not whole and not area and not billboard:
        raise RuntimeError("行业大盘接口全部返回空数据，本区间不落缓存")
    return {
        "schema": SCHEMA, "kind": PREFIX, "account": account,
        "accountLabel": _label(account), "start": _norm_day(start), "end": _norm_day(end),
        "fetchedAt": settings.now_iso(), "category": cat,
        "categoryOptions": leaves, "caliber": CALIBER,
        "datasets": datasets,
        "summaries": {"industry": whole_sum or _fallback_summary(whole),
                      "industryCustomer": ind_cust_sum,
                      "selfCustomer": self_cust_sum},
        "counts": {k: len(v) for k, v in datasets.items()},
    }


def _fetch_account(account: str, start: str, end: str, profile_lock, *,
                   cancel=None, progress=None, force=False, cid3=None) -> tuple[dict | None, bool]:
    """返回 (缓存或新抓的数据, 是否命中缓存)。失败/取消绝不落盘。"""
    if not force:
        cached = _read_cache(account, start, end)
        if cached:
            return cached, True
    if not profile_lock.acquire(timeout=300):
        raise RuntimeError("浏览器 Profile 被占用（登录/保活/抓取中），行业大盘稍后重试")
    pw = ctx = None
    try:
        # 延迟导入：规则层 / 测试环境不需要 playwright 也能 import 本模块
        from .browser import launch_profile, session_ok
        if progress:
            progress("启动浏览器…")
        pw, ctx = launch_profile(headless=True, account=account)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        if not session_ok(page):
            raise RuntimeError("登录态失效：请先到「登录与账号」页扫码，或点「立即续期」")
        payload = _collect(page, account, start, end, cancel=cancel,
                           progress=progress, cid3=cid3)
    finally:
        try:
            if ctx:
                ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if pw:
                pw.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            profile_lock.release()
        except Exception:  # noqa: BLE001
            pass
    if cancel and cancel.is_set():
        raise Cancelled()
    _write_cache(payload)
    return payload, False


# ---------------------------------------------------------------- 后台补齐任务
class IndustryRunner:
    """串行补齐多个账号/区间；与登录/保活/抓取共用同一把 Profile 锁。"""

    def __init__(self, profile_lock: threading.Lock | None = None):
        self._profile_lock = profile_lock or threading.Lock()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._queue: list[dict] = []
        self._thread: threading.Thread | None = None
        self._state: dict = {
            "running": False, "startedAt": None, "finishedAt": None,
            "range": None, "accounts": [], "total": 0, "done": 0,
            "message": "", "error": None, "current": None,
            "lastResult": None, "cancelled": False,
        }

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._state)
            s["queued"] = [dict(q) for q in self._queue]
        return s

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def enqueue(self, accounts, start: str, end: str, force: bool = False,
                note: str = "") -> dict:
        keys = tuple(dict.fromkeys(str(a) for a in accounts if a))
        if not keys:
            return {"ok": False, "error": "没有可补齐的账号"}
        item = {"accounts": keys, "start": _norm_day(start), "end": _norm_day(end),
                "force": bool(force), "note": note}
        with self._lock:
            pending = [self._state.get("current")] + self._queue
            for q in pending:
                if (q and q.get("accounts") == keys and q.get("start") == item["start"]
                        and q.get("end") == item["end"] and q.get("force") == item["force"]):
                    return {"ok": True, "queued": False, "duplicate": True,
                            "message": "该区间的行业大盘补齐已在队列里"}
            self._queue.append(item)
            if self._state.get("running"):
                return {"ok": True, "queued": True, "message": "已加入补齐队列"}
            self._state.update({
                "running": True, "startedAt": settings.now_iso(), "finishedAt": None,
                "range": [item["start"], item["end"]], "accounts": list(keys),
                "total": len(keys), "done": 0, "message": "行业大盘补齐任务已启动",
                "error": None, "cancelled": False,
            })
        self._cancel.clear()
        th = threading.Thread(target=self._work, name="industry-fetch", daemon=True)
        self._thread = th
        th.start()
        return {"ok": True, "queued": False, "message": "行业大盘补齐任务已启动"}

    def run_background(self, accounts, start: str, end: str,
                       force: bool = False, note: str = "") -> dict:
        return self.enqueue(accounts, start, end, force=force, note=note)

    def cancel(self) -> dict:
        self._cancel.set()
        with self._lock:
            self._queue.clear()
            self._state["cancelled"] = True
        return {"ok": True, "message": "已请求取消（未完成的区间不会写入缓存）"}

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._state.update({"running": False, "current": None,
                                        "finishedAt": settings.now_iso()})
                    return
                item = self._queue.pop(0)
                self._state["current"] = item
                self._state.update({
                    "running": True, "range": [item["start"], item["end"]],
                    "accounts": list(item["accounts"]), "total": len(item["accounts"]),
                    "done": 0, "error": None,
                    "message": f"开始补齐 {item['start']} ~ {item['end']} 的行业大盘",
                })
            keys, start, end, force = (item["accounts"], item["start"],
                                       item["end"], item["force"])
            errors: list[str] = []
            for i, key in enumerate(keys, 1):
                if self._cancel.is_set():
                    break
                lab = _label(key)
                self._set(done=i - 1, message=f"[{i}/{len(keys)}] {lab}：正在补齐…")
                try:
                    payload, cached = _fetch_account(
                        key, start, end, self._profile_lock,
                        cancel=self._cancel,
                        progress=lambda m, _lab=lab: self._set(message=f"[{i}/{len(keys)}] {_lab}：{m}"),
                        force=force,
                    )
                    _clear_failed(key, start, end)
                    if cached:
                        self._set(message=f"[{i}/{len(keys)}] {lab}：命中本地缓存")
                    elif payload:
                        n = sum(len(v) for v in payload["datasets"].values())
                        self._set(message=f"[{i}/{len(keys)}] {lab}：已抓 {n} 行并写入缓存")
                except Cancelled:
                    break
                except Exception as exc:  # noqa: BLE001
                    _mark_failed(key, start, end)
                    errors.append(f"{lab}：{type(exc).__name__}: {exc}")
                    self._set(message=f"[{i}/{len(keys)}] {lab}：失败（已跳过，不写缓存）")
                self._set(done=i)
            with self._lock:
                self._state.update({
                    "lastResult": {"range": [start, end], "accounts": list(keys),
                                   "errors": errors,
                                   "cancelled": self._cancel.is_set()},
                    "error": "；".join(errors) if errors else None,
                    "message": ("已取消，未写入缓存" if self._cancel.is_set() else
                                (f"补齐完成（{len(errors)} 个账号失败）" if errors else "行业大盘补齐完成")),
                    "current": None,
                })
            if self._cancel.is_set():
                with self._lock:
                    self._queue.clear()
                    self._state.update({"running": False, "finishedAt": settings.now_iso()})
                return


_runner = IndustryRunner()


def set_profile_lock(lock: threading.Lock) -> None:
    """让行业大盘与扫码登录 / 保活 / 花费抓取共用同一把 Profile 锁。"""
    global _runner
    _runner = IndustryRunner(lock)


def status() -> dict:
    return _runner.snapshot()


def wait_for(accounts, start: str, end: str, timeout: int = 300) -> bool:
    """等后台补齐落盘（AI 生成前调用），不自己发请求；超时返回 False。"""
    keys = [str(a) for a in accounts if a]
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if all(_read_cache(k, start, end) for k in keys):
            return True
        st = status()
        if not st.get("running") and not st.get("queued"):
            return False
        time.sleep(2)
    return False


def _read_blocks(accounts, start: str, end: str) -> tuple[dict, list[str]]:
    blocks, missing = {}, []
    for key in [str(a) for a in accounts if a]:
        d = _read_cache(key, start, end)
        if d:
            blocks[key] = d
        else:
            missing.append(key)
    return blocks, missing


def ensure(accounts, start: str, end: str, *, force: bool = False,
           auto: bool | None = None) -> dict:
    """缓存优先；缺数据自动起后台补齐（不阻塞页面），不提示用户去别的页面。"""
    start, end = _norm_day(start), _norm_day(end)
    blocks, missing = _read_blocks(accounts, start, end)
    if force:
        missing = [str(a) for a in accounts if a]
    retry_later = [] if force else _cooldown_keys(missing, start, end)
    todo = [k for k in missing if k not in retry_later]
    auto = settings.INDUSTRY_AUTO_FETCH if auto is None else auto
    started = False
    if todo and settings.INDUSTRY_ENABLED and auto and start and end:
        started = bool(_runner.enqueue(todo, start, end, force=force).get("ok"))
    return {"blocks": blocks, "missing": missing, "retryLater": retry_later,
            "autoStarted": started, "status": status()}


# ---------------------------------------------------------------- 缓存管理
def cache_entries() -> list[dict]:
    """给 /cache 页用的一份清单（不自动清理，删除由用户点按钮触发）。"""
    base = Path(settings.REPORT_DIR)
    out: list[dict] = []
    for fp in sorted(base.glob(f"{PREFIX}_*.json")):
        m = re.match(rf"^{PREFIX}_(.+)_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})\.json$",
                     fp.name)
        if not m:
            continue
        acc, start, end = m.group(1), m.group(2), m.group(3)
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows = sum(len(v) for v in (d.get("datasets") or {}).values() if isinstance(v, list))
        own_cost = sum(_f(r.get("cost")) for r in (d.get("datasets") or {}).get("selfDetail", []))
        st = fp.stat()
        cat = (d.get("category") or {}).get("path") or ""
        out.append({
            "mode": PREFIX, "account": acc, "accountLabel": _label(acc),
            "start": start, "end": end, "rows": rows,
            "total": round(own_cost, 2), "category": cat,
            "jobId": d.get("fetchedAt"), "savedAt": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "sizeKB": round(st.st_size / 1024, 1), "files": [fp.name],
            "legacy": False, "stale": d.get("schema") != SCHEMA,
        })
    return out


def delete_cache(account: str, start: str, end: str) -> dict:
    fp = _cache_path(account, start, end)
    _clear_failed(account, start, end)   # 用户手动删过缓存，下次查询允许重新自动补齐
    deleted = []
    if fp.exists():
        try:
            fp.unlink()
            deleted.append(fp.name)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "deleted": [], "error": str(exc)}
    return {"ok": True, "deleted": deleted,
            "error": None if deleted else "没有找到对应的缓存文件"}


# ---------------------------------------------------------------- 规则层观察
_REGION_SUFFIX = ("维吾尔自治区", "壮族自治区", "回族自治区", "特别行政区",
                  "自治区", "省", "市", "自治区")


def _region_core(name: str) -> str:
    s = str(name or "").strip()
    for suf in _REGION_SUFFIX:
        if s.endswith(suf):
            return s[:-len(suf)]
    return s


def _region_hit(name: str, own: list[str]) -> bool:
    core = _region_core(name)
    if not core:
        return False
    return any(core and (core == _region_core(x) or core in str(x) or str(x) in core)
               for x in own)


def _own_words(data: dict, account: str) -> set[str]:
    out: set[str] = set()
    per = (data.get("words") or {}).get(account, {}).get("w2") or {}
    for pv in per.values():
        for row in (pv.get("sw") or []) + (pv.get("kw") or []):
            w = str(row.get("word") or "").strip()
            if w:
                out.add(w)
    return out


def _own_word_map(data: dict, account: str) -> dict[str, dict]:
    """把本店 w2 关键词按词聚合（跨计划），用于和行业榜/对比做逐词出价建议。"""
    per = (data.get("words") or {}).get(account, {}).get("w2") or {}
    out: dict[str, dict] = {}
    for plan, pv in per.items():
        for row in pv.get("kw") or []:
            word = str(row.get("word") or "").strip()
            if not word:
                continue
            v = out.setdefault(word, {"word": word, "cost": 0.0, "amt": 0.0,
                                      "ord": 0, "clk": 0, "imp": 0,
                                      "be": 0.0, "plans": []})
            v["cost"] += _f(row.get("cost"))
            v["amt"] += _f(row.get("amt"))
            v["ord"] += int(_f(row.get("ord")))
            v["clk"] += int(_f(row.get("clk")))
            v["imp"] += int(_f(row.get("imp")))
            v["be"] = max(v["be"], _f(row.get("be")))
            if plan and str(plan) not in v["plans"]:
                v["plans"].append(str(plan))
    for v in out.values():
        cost, clk, amt, imp = v["cost"], v["clk"], v["amt"], v["imp"]
        v["roi"] = round(amt / cost, 2) if cost else 0.0
        v["cpc"] = round(cost / clk, 2) if clk else 0.0
        v["ctr"] = round(clk / imp * 100, 2) if imp else 0.0
        v["cvr"] = round(v["ord"] / clk * 100, 2) if clk else 0.0
    return out


def _industry_word_map(ds: dict) -> dict[str, dict]:
    """合并关键词榜 / 热词 / 飙升词 / 新词 / 品牌词 / 本店对比，按词留一份行业信号。"""
    out: dict[str, dict] = {}

    def touch(word, source: str, **kw) -> None:
        word = str(word or "").strip()
        if not word:
            return
        v = out.setdefault(word, {"word": word, "sources": []})
        if source and source not in v["sources"]:
            v["sources"].append(source)
        for k, val in kw.items():
            if val is None or val == "":
                continue
            v[k] = _f(val) if k != "sources" else val

    for r in ds.get("billboard") or []:
        touch(r.get("keyWord"), "关键词榜", ctr=r.get("ctr"), cvr=r.get("cvs"),
              ref_bid=r.get("kwImpressionBid"), imp_exponent=r.get("impressionExponent"),
              click_exponent=r.get("clickExponent"))
    for r in ds.get("hotWords") or []:
        touch(r.get("keyWord"), "热词", rank=r.get("keyWordRank"),
              search_exponent=r.get("searchExponent"), competitiveness=r.get("kwCompetitiveness"))
    for r in ds.get("soaringWords") or []:
        touch(r.get("keyWord"), "飙升词", rank=r.get("keyWordRank"),
              search_exponent=r.get("searchExponent"), rise_rate=r.get("riseRate"),
              competitiveness=r.get("kwCompetitiveness"))
    for r in ds.get("newWords") or []:
        touch(r.get("keyWord"), "新词", rank=r.get("keyWordRank"),
              search_exponent=r.get("searchExponent"), competitiveness=r.get("kwCompetitiveness"))
    for r in ds.get("brandHotWords") or []:
        touch(r.get("keyWord"), "品牌词", rank=r.get("keyWordRank"),
              search_exponent=r.get("searchExponent"), competitiveness=r.get("kwCompetitiveness"))
    for r in ds.get("brandSoaringWords") or []:
        touch(r.get("keyWord"), "品牌飙升词", rank=r.get("keyWordRank"),
              search_exponent=r.get("searchExponent"), rise_rate=r.get("riseRate"),
              competitiveness=r.get("kwCompetitiveness"))
    for r in ds.get("brandNewWords") or []:
        touch(r.get("keyWord"), "品牌新词", rank=r.get("keyWordRank"),
              search_exponent=r.get("searchExponent"), competitiveness=r.get("kwCompetitiveness"))
    for r in ds.get("compete") or []:
        s, i = r.get("self") or {}, r.get("industry") or {}
        touch(r.get("keyWord"), "本店对比", ctr=i.get("ctr"), cvr=i.get("cvs"),
              ref_bid=i.get("kwImpressionBid"), self_roi=s.get("roi"),
              self_ctr=s.get("ctr"), self_cvr=s.get("cvs"), self_cpc=s.get("cpc"),
              self_bid=s.get("kwImpressionBid"),
              imp_exponent=i.get("impressionExponent"), click_exponent=i.get("clickExponent"))
    return out


def _keyword_actions(data: dict, account: str, ds: dict, be: float) -> list[dict]:
    """把「行业词信号」和「本店该词真实 ROI/转化/出价」拼成可执行的出价动作。"""
    own = _own_word_map(data, account)
    imap = _industry_word_map(ds)
    total_cost = sum(v["cost"] for v in own.values())
    total_clk = sum(v["clk"] for v in own.values())
    avg_cpc = round(total_cost / total_clk, 2) if total_clk else 0.0
    actions: list[dict] = []

    def opt(v):
        return None if v is None or v == "" else _r(v)

    def add(word: str, status: str, iw: dict, ov: dict | None, action: str,
            priority: str, target: float | None, reason: str) -> None:
        ov = ov or {}
        actions.append({
            "word": word, "status": status, "sources": list(iw.get("sources") or []),
            "priority": priority, "action": action,
            "suggest_bid": round(target, 2) if target else None,
            "current_bid": opt(iw.get("self_bid")) if iw.get("self_bid") is not None
            else (opt(ov.get("cpc")) if ov else None),
            "industry_bid": opt(iw.get("ref_bid")),
            "own_roi": _r(ov.get("roi")), "own_ctr": _r(ov.get("ctr")),
            "own_cvr": _r(ov.get("cvr")),
            "industry_ctr": _r(iw.get("ctr")), "industry_cvr": _r(iw.get("cvr")),
            "own_cost": round(_f(ov.get("cost")), 2) if ov else 0.0,
            "plans": list(ov.get("plans") or [])[:3],
            "search_exponent": opt(iw.get("search_exponent")),
            "rise_rate": opt(iw.get("rise_rate")),
            "competitiveness": opt(iw.get("competitiveness")),
            "reason": reason,
        })

    # 1) 本店已投、且行业榜/对比里有同词：直接给加价/降价/先修转化
    for word, ov in own.items():
        iw = imap.get(word)
        if not iw:
            continue
        own_roi, own_ctr, own_cvr = _f(ov.get("roi")), _f(ov.get("ctr")), _f(ov.get("cvr"))
        own_be = _f(ov.get("be")) or be
        own_bid = _f(iw.get("self_bid"))
        ref_bid = _f(iw.get("ref_bid"))
        ind_ctr, ind_cvr = _f(iw.get("ctr")), _f(iw.get("cvr"))
        rise = _f(iw.get("rise_rate"))
        src = "/".join(iw.get("sources") or [])
        if own_roi >= own_be and own_cvr >= ind_cvr * 0.9:
            if ref_bid > 0 and (own_bid <= 0 or own_bid < ref_bid * 0.9):
                target = max(own_bid, ref_bid * 0.9, 0.1)
                add(word, "已投", iw, ov,
                    f"提价到 ¥{target:.2f}（行业参考 ¥{ref_bid:.2f} 的 90%）", "高", target,
                    f"{word} 本店 ROI {own_roi:.2f} ≥ 保本 {own_be:.2f}、转化率 {own_cvr:.2f}% "
                    f"不输行业 {ind_cvr:.2f}%；当前出价 ¥{own_bid:.2f} 低于行业参考 ¥{ref_bid:.2f}，"
                    f"可先小幅提价抢量（信号：{src}）。")
            elif rise > 30:
                base = own_bid or _f(ov.get("cpc")) or ref_bid or 0.1
                target = base * 1.05
                add(word, "已投", iw, ov,
                    f"先 +5% 到 ¥{target:.2f} 测 2~3 天", "高", target,
                    f"{word} 行业搜索涨幅 {rise:+.1f}%，本店 ROI {own_roi:.2f} 达标；"
                    "先小幅加价测试，若转化率回落就退回原价。")
            else:
                add(word, "已投", iw, ov,
                    "维持出价，优先放量/复制到相近词", "低", None,
                    f"{word} 本店 ROI {own_roi:.2f} 达标，行业没有明显涨价信号；"
                    "先保持出价，把预算放到同词的高效计划或相近长尾词。")
        elif own_roi < own_be and own_cvr < ind_cvr:
            base = own_bid or _f(ov.get("cpc")) or 0.1
            target = max(0.1, base * 0.9)
            add(word, "已投", iw, ov,
                f"先降价 10% 到 ¥{target:.2f}，并修商详/价格，暂不跟价", "高", target,
                f"{word} 本店 ROI {own_roi:.2f} < 保本 {own_be:.2f}，转化率 {own_cvr:.2f}% "
                f"低于行业 {ind_cvr:.2f}%；加价只会放大亏损，先把转化和内功补上。")
        elif own_roi < own_be and ref_bid > 0 and own_bid > ref_bid * 1.1:
            target = ref_bid
            add(word, "已投", iw, ov,
                f"降价到 ¥{target:.2f} 附近（当前出价高于行业参考）", "中", target,
                f"{word} 本店 ROI {own_roi:.2f} 低于保本 {own_be:.2f}，当前出价 ¥{own_bid:.2f} "
                f"高于行业参考 ¥{ref_bid:.2f}；先降价到参考价附近再观察 2~3 天。")
        elif ind_ctr > 0 and own_ctr < ind_ctr - 1:
            add(word, "已投", iw, ov,
                "先改主图/标题相关性，出价暂不动", "中", None,
                f"{word} 本店 CTR {own_ctr:.2f}% 低于行业 {ind_ctr:.2f}%；"
                "先修创意与词-品匹配，别用加价掩盖点击率问题。")
        else:
            add(word, "已投", iw, ov,
                "观察 2~3 天，暂不调价", "低", None,
                f"{word} 本店 ROI {own_roi:.2f}、转化率 {own_cvr:.2f}%，与行业差异不大；"
                "先保持出价，等下一期趋势再动。")

    # 2) 行业热词/飙升词/新词/品牌词里本店没投的：给一个可执行的起测价
    cand = []
    for word, iw in imap.items():
        if word in own:
            continue
        sources = iw.get("sources") or []
        if not any(s in sources for s in ("热词", "飙升词", "新词", "品牌词", "品牌飙升词", "品牌新词")):
            continue
        rank = _f(iw.get("rank"))
        rise = _f(iw.get("rise_rate"))
        if rank > 30 and rise <= 0:
            continue
        score = (100 if rise > 50 or 0 < rank <= 10 else 0) + min(rise, 200) / 100
        cand.append((score, word, iw, sources, rise))
    cand.sort(key=lambda x: -x[0])
    for _score, word, iw, sources, rise in cand[:6]:
        ref_bid = _f(iw.get("ref_bid"))
        base = ref_bid or avg_cpc or 0.5
        factor = 0.8 if "飙升词" in sources else 0.6
        target = max(0.1, base * factor)
        comp = _f(iw.get("competitiveness"))
        rank = _f(iw.get("rank"))
        if comp > 500000:
            priority, action = "低", f"只做低价长尾，起测 ¥{target:.2f}；竞争度 {comp:,.0f}，不抢头部"
        elif rise > 50 or 0 < rank <= 10:
            priority, action = "高", f"加词测试：精确匹配，起测 ¥{target:.2f}，跑 3 天看 ROI"
        else:
            priority, action = "中", f"可加词测试：起测 ¥{target:.2f}，先小预算跑 3 天"
        add(word, "未投", iw, None, action, priority, target,
            f"{word} 出现在行业{'/'.join(sources)}，本店未投；"
            + (f"搜索指数 {_f(iw.get('search_exponent')):,.0f}" if iw.get("search_exponent") else "")
            + (f"、涨幅 {rise:+.1f}%" if rise else "")
            + (f"、排名 {int(rank)}" if rank else "")
            + f"；行业参考出价 ¥{ref_bid:.2f}，建议先按参考价 {factor * 100:.0f}% 起测，"
              "有成交再逐步提价，别直接抢大词首位。")

    prio = {"高": 0, "中": 1, "低": 2}
    invested = [x for x in actions if x.get("status") == "已投"]
    uncovered = [x for x in actions if x.get("status") == "未投"]
    invested.sort(key=lambda x: (prio.get(x.get("priority"), 9), -_f(x.get("own_cost"))))
    uncovered.sort(key=lambda x: (prio.get(x.get("priority"), 9),
                                  -_f(x.get("search_exponent")), -_f(x.get("rise_rate"))))
    # 已投词是主菜，但必须给未投热词留位置；否则页面会只剩降价建议，看不到加词机会
    picked = invested[:8] + uncovered[:4]
    if len(picked) < 12:
        picked += (invested[8:] + uncovered[4:])[:12 - len(picked)]
    return picked


def _own_regions(data: dict, account: str) -> list[str]:
    rows = ((data.get("targeting") or {}).get("area", {}).get(account, {}).get("w2") or [])
    return [str(r.get("name") or "") for r in rows if r.get("name")]


def _half_change(rows: list[dict], field: str) -> float | None:
    vals = [_f(r.get(field)) for r in rows]
    vals = [v for v in vals if v > 0]
    n = len(vals)
    if n < 4:
        return None
    half = n // 2
    a = sum(vals[:half]) / half
    b = sum(vals[half:]) / (n - half)
    return round((b / a - 1) * 100, 1) if a > 0 else None


def observe(blocks: dict, data: dict) -> dict:
    """把缓存里的客观行压成「观察 / 建议 / 表 / 给 AI 的事实」。

    算数全部在这里完成；LLM 只负责解释、取舍与给动作，不重算。
    """
    meta = data.get("meta") or {}
    be = _f(meta.get("breakevenFlat"), 4.0)
    facts: list[dict] = []
    tables: dict[str, dict] = {}
    observations: list[str] = []
    suggestions: list[dict] = []
    gaps: list[dict] = []
    chart_accounts: list[dict] = []

    for key, blk in (blocks or {}).items():
        ds = blk.get("datasets") or {}
        sums = blk.get("summaries") or {}
        cat = blk.get("category") or {}
        label = blk.get("accountLabel") or _label(key)
        cat_name = cat.get("path") or cat.get("name") or "未指定类目"

        own_rows = [r for r in ds.get("selfDetail", [])
                    if _f(r.get("clicks")) > 0 or _f(r.get("cost")) > 0]
        own_clicks = sum(_f(r.get("clicks")) for r in own_rows)
        own_imps = sum(_f(r.get("impressions")) for r in own_rows)
        own_orders = sum(_f(r.get("totalOrderCnt")) for r in own_rows)
        own_cost = sum(_f(r.get("cost")) for r in own_rows)
        own_ctr = round(own_clicks / own_imps * 100, 2) if own_imps else 0.0
        own_cvr = round(own_orders / own_clicks * 100, 2) if own_clicks else 0.0
        own_cart = (round(sum(_f(r.get("cartCVS")) * _f(r.get("clicks")) for r in own_rows)
                          / own_clicks, 2) if own_clicks else 0.0)
        ind = sums.get("industry") or {}
        ind_ctr, ind_cvr, ind_cart = (_f(ind.get("ctr")), _f(ind.get("cvs")),
                                      _f(ind.get("cartCVS")))
        ctr_gap = round(own_ctr - ind_ctr, 2)
        cvr_gap = round(own_cvr - ind_cvr, 2)
        cart_gap = round(own_cart - ind_cart, 2)

        whole = ds.get("whole") or []
        hourly = ds.get("hourly") or []
        area_rows = ds.get("area") or []
        top_regions = sorted(area_rows,
                             key=lambda x: -_f(x.get("impressionsPercentage")))[:12]
        own_region_names = _own_regions(data, key)
        uncovered = [str(r.get("mappedAreaName") or "") for r in top_regions[:8]
                     if not _region_hit(r.get("mappedAreaName"), own_region_names)]
        uncovered = [x for x in uncovered if x][:6]
        low_cvr_regions = [
            {"name": r.get("mappedAreaName"), "ctr": _r(r.get("ctr")),
             "cvs": _r(r.get("cvs")), "impPct": _r(r.get("impressionsPercentage"))}
            for r in top_regions if ind_cvr > 0 and _f(r.get("cvs")) < ind_cvr * 0.8
        ][:5]

        billboard = ds.get("billboard") or []
        compete = ds.get("compete") or []
        hot = ds.get("hotWords") or []
        soaring = ds.get("soaringWords") or []
        new = ds.get("newWords") or []
        brand_hot = ds.get("brandHotWords") or []
        own_word_set = _own_words(data, key)
        uncovered_words = []
        for w in hot[:30]:
            word = str(w.get("keyWord") or "").strip()
            if word and word not in own_word_set:
                uncovered_words.append({"word": word, "rank": w.get("keyWordRank"),
                                        "searchExponent": w.get("searchExponent"),
                                        "competitiveness": w.get("kwCompetitiveness")})
            if len(uncovered_words) >= 10:
                break
        rising_words = [
            {"word": w.get("keyWord"), "riseRate": _r(w.get("riseRate")),
             "searchExponent": w.get("searchExponent"),
             "competitiveness": w.get("kwCompetitiveness")}
            for w in soaring[:8] if _f(w.get("riseRate")) > 0
        ]
        word_gaps = []
        for row in compete[:20]:
            s, i = row.get("self") or {}, row.get("industry") or {}
            dg_ctr = round(_f(s.get("ctr")) - _f(i.get("ctr")), 2)
            dg_cvr = round(_f(s.get("cvs")) - _f(i.get("cvs")), 2)
            if dg_ctr < -1 or dg_cvr < -1 or (s.get("roi") and _f(s.get("roi")) < be):
                word_gaps.append({
                    "word": row.get("keyWord"), "self_roi": _r(s.get("roi")),
                    "self_ctr": _r(s.get("ctr")), "industry_ctr": _r(i.get("ctr")),
                    "self_cvr": _r(s.get("cvs")), "industry_cvr": _r(i.get("cvs")),
                    "self_cpc": _r(s.get("cpc")), "self_bid": _r(s.get("kwImpressionBid")),
                    "industry_bid": _r(i.get("kwImpressionBid")),
                    "ctr_gap": dg_ctr, "cvr_gap": dg_cvr,
                })

        kw_actions = _keyword_actions(data, key, ds, be)

        ind_cust = sums.get("industryCustomer") or {}
        self_cust = sums.get("selfCustomer") or {}
        new_gap = round(_f(self_cust.get("newCustomerCntPercentage"))
                        - _f(ind_cust.get("newCustomerCntPercentage")), 2)
        brands = ds.get("brands") or []
        self_brand = next((b for b in brands if int(_f(b.get("selfBrandFlag"))) == 1), None)
        imp_chg = _half_change(whole, "impressionExponent")
        clk_chg = _half_change(whole, "clickExponent")
        peaks = sorted(hourly, key=lambda x: -_f(x.get("clickExponent")))[:3]
        valleys = sorted(hourly, key=lambda x: _f(x.get("clickExponent")))[:3]

        obs = [
            f"{label}｜{cat_name}：行业 CTR {ind_ctr:.2f}%、转化率 {ind_cvr:.2f}%、"
            f"加购率 {ind_cart:.2f}%；本店 CTR {own_ctr:.2f}%（{ctr_gap:+.2f}pp）、"
            f"转化率 {own_cvr:.2f}%（{cvr_gap:+.2f}pp）、加购率 {own_cart:.2f}%（{cart_gap:+.2f}pp）。",
            f"地域：行业展现占比前 3 为 "
            + "、".join(f"{r.get('mappedAreaName')}({_f(r.get('impressionsPercentage')):.2f}%)"
                       for r in top_regions[:3]) + "；本店本期地域定向覆盖 "
            + (f"{len(own_region_names)} 个，行业高潜但未覆盖 {len(uncovered)} 个。"
               if own_region_names else "没有地域维度数据，无法核对覆盖。"),
            f"关键词：行业热词榜 {len(billboard)} 行，本店未覆盖热词 {len(uncovered_words)} 个；"
            f"飙升词 {len(soaring)} 个、新词 {len(new)} 个；本店 vs 行业词效率对比 "
            f"{len(compete)} 个词，其中 {len(word_gaps)} 个明显落后。",
            f"客户：本店新客占比 {_f(self_cust.get('newCustomerCntPercentage')):.2f}%，"
            f"行业 {_f(ind_cust.get('newCustomerCntPercentage')):.2f}%（{new_gap:+.2f}pp）；"
            f"品牌榜本店排名 " + (f"第 {self_brand.get('no')} 位（{self_brand.get('name')}）"
                              if self_brand else "未进入 TOP100"),
            ("趋势：区间后半段行业展现指数环比 "
             + (f"{imp_chg:+.1f}%" if imp_chg is not None else "数据不足")
             + "、点击指数 "
             + (f"{clk_chg:+.1f}%" if clk_chg is not None else "数据不足")
             if whole else "趋势：没有分日行业指数，不做环比判断。"),
            "时段：行业点击指数集中在 "
            + "、".join(f"{r.get('date')}点" for r in peaks)
            + "，低谷 " + "、".join(f"{r.get('date')}点" for r in valleys) + "。",
        ]
        if kw_actions:
            n_inv = sum(1 for x in kw_actions if x.get("status") == "已投")
            n_new = sum(1 for x in kw_actions if x.get("status") == "未投")
            obs.append(f"关键词出价：已投词中 {n_inv} 个有明确调价动作，行业未覆盖词 {n_new} 个已给起测出价；"
                       "详见下方「关键词出价建议」，按当前 ROI 与行业基准逐词给，不是只看行业热度。")
        observations.extend(obs)

        def add(topic: str, priority: str, text: str, finding: str = "") -> None:
            suggestions.append({"account": key, "label": label, "topic": topic,
                                "priority": priority, "text": text})
            gaps.append({"account": key, "label": label, "topic": topic,
                         "finding": finding or text, "suggest": text})

        if own_rows:
            if ctr_gap < -0.3:
                add("流量/创意", "高",
                    f"本店 CTR 低于行业 {abs(ctr_gap):.2f}pp，先查主图/标题相关性与词包匹配，"
                    "用小预算测图测词，不要把预算直接加到低 CTR 词上。",
                    f"本店 CTR {own_ctr:.2f}% vs 行业 {ind_ctr:.2f}%")
            elif ctr_gap > 0.5:
                add("流量/创意", "中",
                    f"本店 CTR 高于行业 {ctr_gap:.2f}pp，可优先放大已验证的高点击词/创意，"
                    "但放量后要盯转化率是否回落。",
                    f"本店 CTR {own_ctr:.2f}% vs 行业 {ind_ctr:.2f}%")
            if cvr_gap < -0.5:
                add("转化/内功", "高",
                    f"本店转化率低于行业 {abs(cvr_gap):.2f}pp，优先排查商详、价格、评价、客服，"
                    "这类问题加价放量只会放大亏损。",
                    f"本店转化率 {own_cvr:.2f}% vs 行业 {ind_cvr:.2f}%")
        if uncovered:
            add("地域", "高",
                "行业高潜地域 " + "、".join(uncovered[:4]) +
                " 本店没有地域维度花费：先单开小预算地域计划或设 10%~20% 地域溢价，跑 3~4 天再看 ROI。",
                f"行业展现前 8 的地域中未覆盖 {len(uncovered)} 个")
        if low_cvr_regions:
            add("地域", "中",
                "行业转化率明显低于大盘的 " + "、".join(str(x["name"]) for x in low_cvr_regions[:3]) +
                " 先别加预算，可先降价或屏蔽验证。",
                "行业地域转化率低于大盘 20% 以上")
        if uncovered_words:
            add("关键词", "高",
                "行业热词 " + "、".join(x["word"] for x in uncovered_words[:4]) +
                " 本店未投：先用精确匹配小预算加词测 3~4 天，有成交再逐步提价，别直接抢大词首位。",
                f"本店未覆盖行业热词 {len(uncovered_words)} 个")
        if rising_words:
            add("关键词", "中",
                "飙升词 " + "、".join(str(x["word"]) for x in rising_words[:3]) +
                " 搜索指数在涨，可先做低价长尾测试；竞争度极高的词只观察不追。",
                f"行业飙升词 {len(soaring)} 个")
        if word_gaps:
            add("词效率", "中",
                "本店 vs 行业对比里 " + "、".join(str(x["word"]) for x in word_gaps[:3]) +
                " 的 CTR/转化率或 ROI 落后：先查词-品匹配与落地页，再决定是否降价或否定。",
                f"对比 {len(compete)} 个词，{len(word_gaps)} 个落后")
        if new_gap < -5:
            add("拉新", "中",
                f"本店新客占比低于行业 {abs(new_gap):.2f}pp，检查拉新计划预算是否被收割计划挤占。",
                f"本店新客占比 {_f(self_cust.get('newCustomerCntPercentage')):.2f}% vs 行业 "
                f"{_f(ind_cust.get('newCustomerCntPercentage')):.2f}%")
        if peaks:
            add("时段", "低",
                "行业点击高峰 " + "、".join(f"{r.get('date')}点" for r in peaks) +
                " 可配合分时折扣做小幅度提价；低谷 " + "、".join(f"{r.get('date')}点" for r in valleys) +
                " 先降价验证，注意分时数据按点击小时归因，别只凭一天下结论。",
                "行业时段点击指数分布")
        top_acts = ([x for x in kw_actions if x.get("status") == "已投"][:2]
                    + [x for x in kw_actions if x.get("status") == "未投"][:1])
        for act in top_acts:
            add("关键词出价", act["priority"], act["reason"],
                f"{act['word']}：{act['action']}")

        fact = {
            "account": key, "label": label, "category": cat_name, "cid3": cat.get("id"),
            "cachedAt": blk.get("fetchedAt"),
            "self_vs_industry": {
                "own_cost": round(own_cost, 2), "own_ctr": own_ctr, "industry_ctr": ind_ctr,
                "ctr_gap_pp": ctr_gap, "own_cvr": own_cvr, "industry_cvr": ind_cvr,
                "cvr_gap_pp": cvr_gap, "own_cart_rate": own_cart,
                "industry_cart_rate": ind_cart, "cart_gap_pp": cart_gap,
                "own_roi": _r(sum(_f(r.get("amt")) if "amt" in r else
                                  _f(r.get("roi")) * _f(r.get("cost")) for r in own_rows) / own_cost)
                if own_cost else 0.0,
            },
            "top_regions": [
                {"name": r.get("mappedAreaName"), "imp_pct": _r(r.get("impressionsPercentage")),
                 "order_pct": _r(r.get("orderPercentage")), "ctr": _r(r.get("ctr")),
                 "cvr": _r(r.get("cvs")), "imp_rank": r.get("impressionRank")}
                for r in top_regions[:8]
            ],
            "uncovered_regions": uncovered, "low_cvr_regions": low_cvr_regions,
            "top_keywords": [
                {"word": r.get("keyWord"), "ctr": _r(r.get("ctr")), "cvr": _r(r.get("cvs")),
                 "ref_bid": _r(r.get("kwImpressionBid")),
                 "imp_exponent": _r(r.get("impressionExponent"), 0)}
                for r in billboard[:8]
            ],
            "uncovered_words": [x["word"] for x in uncovered_words[:8]],
            "rising_words": rising_words[:6],
            "new_words": [str(w.get("keyWord") or "") for w in new[:6]],
            "brand_words": [str(w.get("keyWord") or "") for w in brand_hot[:6]],
            "word_gaps": word_gaps[:6],
            "keyword_actions": kw_actions,
            "keyword_summary": {
                "invested": sum(1 for x in kw_actions if x.get("status") == "已投"),
                "uncovered": sum(1 for x in kw_actions if x.get("status") == "未投"),
                "high": sum(1 for x in kw_actions if x.get("priority") == "高"),
            },
            "customer": {"self_new_pct": _r(self_cust.get("newCustomerCntPercentage")),
                         "industry_new_pct": _r(ind_cust.get("newCustomerCntPercentage")),
                         "gap_pp": new_gap,
                         "self_new_cost": _r(self_cust.get("newCustomerCost")),
                         "industry_new_cost": _r(ind_cust.get("newCustomerCost"))},
            "brands": [{"name": b.get("name"), "rank": b.get("no"),
                        "ctr": _r(b.get("ctr")), "cvr": _r(b.get("cvs"))}
                       for b in brands[:5]],
            "self_brand_rank": self_brand.get("no") if self_brand else None,
            "trend": {"impression_change_pct": imp_chg, "click_change_pct": clk_chg,
                      "days": len(whole)},
            "hourly_peaks": [{"hour": r.get("date"), "click_exponent": _r(r.get("clickExponent"), 0),
                              "ctr": _r(r.get("ctr"))} for r in peaks],
            "hourly_valleys": [str(r.get("date") or "") for r in valleys],
            "observations": obs[:6],
        }
        facts.append(fact)
        tables[key] = {
            "regions": top_regions, "keywords": billboard[:20], "compete": compete[:20],
            "hotWords": hot[:12], "soaringWords": soaring[:12], "newWords": new[:12],
            "brandWords": brand_hot[:12], "brands": brands[:10],
            "customer": {"industry": ind_cust, "self": self_cust},
            "keywordActions": kw_actions,
            "trend": whole, "hourly": hourly,
            "industrySummary": ind, "selfDetail": own_rows,
        }
        chart_accounts.append({
            "key": key, "label": label,
            "trend": [{"date": str(r.get("date") or "")[:10],
                       "imp": _f(r.get("impressionExponent")), "clk": _f(r.get("clickExponent")),
                       "ctr": _f(r.get("ctr")), "cvr": _f(r.get("cvs"))} for r in whole],
            "hourly": [{"hour": str(r.get("date") or "").zfill(2),
                        "clk": _f(r.get("clickExponent")), "ctr": _f(r.get("ctr"))}
                       for r in hourly],
        })

    return {
        "available": bool(facts), "caliber": CALIBER, "accounts": facts,
        "tables": tables, "observations": observations[:24],
        "suggestions": suggestions[:24], "gaps": gaps[:24],
        "charts": {"accounts": chart_accounts},
    }


def compact_for_ai(ind: dict | None) -> dict:
    """AI prompt 用：只喂客观事实与规则层算好的缺口，不带大表。"""
    ind = ind or {}
    if not ind.get("available"):
        return {"available": False, "note": "本区间没有行业大盘缓存；不要编造行业数据"}
    # 多账号时 prompt 会线性膨胀：AI 只看前 3 个账号 + 8 条全局缺口，
    # 全量明细仍完整保留在报表⑧页签，模型不需要每个词都拿。
    all_accounts = ind.get("accounts") or []
    accounts = []
    for a in all_accounts[:3]:
        accounts.append({
            "account": a.get("account"), "label": a.get("label"),
            "category": a.get("category"),
            "self_vs_industry": a.get("self_vs_industry"),
            "top_regions": a.get("top_regions", [])[:5],
            "uncovered_regions": a.get("uncovered_regions", [])[:4],
            "low_cvr_regions": a.get("low_cvr_regions", [])[:3],
            "top_keywords": a.get("top_keywords", [])[:5],
            "uncovered_words": a.get("uncovered_words", [])[:6],
            "rising_words": a.get("rising_words", [])[:4],
            "new_words": a.get("new_words", [])[:4],
            "brand_words": a.get("brand_words", [])[:4],
            "word_gaps": a.get("word_gaps", [])[:4],
            "keyword_actions": a.get("keyword_actions", [])[:8],
            "keyword_summary": a.get("keyword_summary"),
            "customer": a.get("customer"),
            "brands": a.get("brands", [])[:3],
            "self_brand_rank": a.get("self_brand_rank"),
            "trend": a.get("trend"),
            "hourly_peaks": a.get("hourly_peaks"),
            "hourly_valleys": a.get("hourly_valleys"),
            "observations": a.get("observations", [])[:3],
        })
    note = ""
    if len(all_accounts) > 3:
        note = f"共 {len(all_accounts)} 个账号，AI 摘要只列前 3 个；完整明细见报表⑧行业大盘页签"
    return {"available": True, "caliber": ind.get("caliber"),
            "accounts": accounts, "gaps": (ind.get("gaps") or [])[:8], "note": note}


def collect(data: dict) -> dict:
    """analyze2.build() 调用：**只读本地缓存**，不请求京东。

    自动补齐由 webapp 在生成报表前调用 ensure() 起后台任务。
    """
    meta = data.get("meta") or {}
    rng = meta.get("range") or {}
    start, end = _norm_day(rng.get("start")), _norm_day(rng.get("end"))
    keys = [a.get("key") for a in (meta.get("accounts") or []) if a.get("key")]
    enabled = bool(settings.INDUSTRY_ENABLED)
    blocks, missing = _read_blocks(keys, start, end) if (enabled and start and end) else ({}, list(keys))
    out = observe(blocks, data)
    out.update({
        "enabled": enabled, "range": {"start": start, "end": end},
        "missing": missing, "retryLater": _cooldown_keys(missing, start, end),
        "status": status(),
        "cachedAt": {k: v.get("fetchedAt") for k, v in blocks.items()},
    })
    return out
