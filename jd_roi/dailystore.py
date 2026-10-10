# -*- coding: utf-8 -*-
"""按天入库的数据层。

设计目标：让「任意区间聚合」变成纯本地计算，不再依赖接口。

目录结构：
    <DATA_DIR>/daily/<account>/<YYYY-MM-DD>/<kind>.json.gz
    <DATA_DIR>/state.json                     运行状态（最近一次抓取等）

kind 取值：
    jzt_campaign 京准通概览-计划    jst_campaign 智能投放-计划
    jst_sku      智能投放-商品      jst_searchword 智能投放-搜索词
    kw           快车-关键词        sz_product  商智-商品明细
    sz_flow      商智-流量概况      oplog       京准通操作日志
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import threading
import time
from pathlib import Path

from . import settings
from .indicators import FLOW_SUM, PRODUCT_SUM, SRC_AMT, SRC_UV, SZ_AMT, SZ_ORDN, SZ_UV

KINDS = ["jzt_campaign", "jst_campaign", "jst_sku", "jst_searchword", "kw",
         "sz_product", "sz_flow", "oplog"]

# 广告类报表可相加的字段
AD_SUM = ["impressions", "clicks", "cost", "totalOrderCnt", "totalOrderSum", "totalCartCnt"]

AGG = {
    "jzt_campaign": {"key": ["campaignId"], "sum": AD_SUM,
                     "keep": ["campaignName", "campaignType", "spuId", "childProNo", "status"]},
    "jst_campaign": {"key": ["campaignId"], "sum": AD_SUM, "keep": ["campaignName"]},
    "jst_sku": {"key": ["skuId", "campaignId"], "sum": AD_SUM, "keep": ["campaignName"]},
    "jst_searchword": {"key": ["campaignName", "searchTerm"], "sum": AD_SUM,
                       "keep": ["campaignName", "searchTerm"]},
    "kw": {"key": ["campaignId", "groupId", "keywordName", "targetingType"], "sum": AD_SUM,
           "keep": ["campaignName", "groupName", "keywordName", "targetingType"]},
}

_lock = threading.RLock()
_days_cache: dict = {}


def _f(v, d=0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def today() -> dt.date:
    """当前日期。**必须**走 settings 的目标时区，不能用裸 datetime.now()。

    裸 now() 在进程时区被污染成 UTC 时会少一天，导致每晚抓「前天」而非「昨天」。
    """
    return settings.today()


def norm_day(day) -> str:
    """把 date / 'YYYY-MM-DD' / 'YYYYMMDD' 统一成 'YYYY-MM-DD'。"""
    if isinstance(day, dt.date):
        return day.isoformat()
    s = str(day).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def day_span(start, end) -> list:
    d0, d1 = dt.date.fromisoformat(norm_day(start)), dt.date.fromisoformat(norm_day(end))
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 读写
def day_dir(account: str, day: str) -> Path:
    return settings.DAILY_DIR / account / norm_day(day)


def day_path(account: str, day: str, kind: str) -> Path:
    return day_dir(account, day) / f"{kind}.json.gz"


def save_day(account: str, day: str, kind: str, payload) -> Path:
    p = day_path(account, day, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    tmp.replace(p)
    with _lock:
        _days_cache.pop(account, None)
    return p


def load_day(account: str, day: str, kind: str):
    p = day_path(account, day, kind)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def has_day(account: str, day: str, kind: str) -> bool:
    return day_path(account, day, kind).exists()


def days_for(account: str, kind: str) -> list:
    """列出某账号某数据源已入库的日期。

    带 TTL 缓存：抓取进程和 Web 进程是**两个进程**，各有一份缓存。
    如果没有 TTL，Web 进程启动时扫一次就永远不再更新，
    新抓的天在控制台/覆盖度里永远不出现（报告本身走文件系统所以没问题）。
    """
    now = time.time()
    with _lock:
        ent = _days_cache.get(account)
    if ent is None or (now - ent[0]) > settings.DAYS_CACHE_TTL:
        root = settings.DAILY_DIR / account
        cache = {}   # kind -> [day, ...]
        if root.exists():
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                for f in d.glob("*.json.gz"):
                    cache.setdefault(f.name[:-len(".json.gz")], []).append(d.name)
        for v in cache.values():
            v.sort()
        with _lock:
            _days_cache[account] = (time.time(), cache)
        ent = _days_cache[account]
    return list(ent[1].get(kind, []))


def invalidate_cache(account: str | None = None) -> None:
    with _lock:
        if account:
            _days_cache.pop(account, None)
        else:
            _days_cache.clear()


def coverage(accounts=None) -> dict:
    accounts = accounts or settings.ACCOUNTS
    out = {}
    for a in accounts:
        k = a["key"]
        per = {}
        for kind in KINDS:
            days = days_for(k, kind)
            per[kind] = {"count": len(days),
                         "first": days[0] if days else None,
                         "last": days[-1] if days else None,
                         "days": days}
        all_days = sorted({d for kind in KINDS for d in per[kind]["days"]})
        out[k] = {"label": a.get("label") or k, "kinds": per,
                  "days": {"count": len(all_days),
                           "first": all_days[0] if all_days else None,
                           "last": all_days[-1] if all_days else None}}
    return out


# ---------------------------------------------------------------- 聚合
def _split_sums(rows: list, spec: dict) -> list:
    keys, sumf, keep = spec["key"], spec["sum"], spec["keep"]
    groups: dict = {}
    order: list = []
    for r in rows:
        kk = tuple(str(r.get(x)) for x in keys)
        g = groups.get(kk)
        if g is None:
            g = {x: r.get(x) for x in keep}
            for x in keys:
                g[x] = r.get(x)
            for x in sumf:
                g[x] = 0.0
            if r.get("$summary"):
                g["$summary"] = True
            groups[kk] = g
            order.append(kk)
        for x in sumf:
            g[x] += _f(r.get(x))
    out = []
    for kk in order:
        g = groups[kk]
        for x in sumf:
            g[x] = round(g[x], 2)
        if "impressions" in g:
            g["impressions"] = int(g["impressions"])
            g["clicks"] = int(g["clicks"])
            g["totalOrderCnt"] = int(g["totalOrderCnt"])
            g["totalCartCnt"] = int(g["totalCartCnt"])
        out.append(g)
    return out


def _agg_sz_product(payloads: list) -> dict:
    groups: dict = {}
    for pl in payloads:
        for r in (pl or {}).get("rows", []):
            sid = r.get("sku_id")
            if sid is None:
                continue
            g = groups.get(sid)
            if g is None:
                g = {"sku_id": sid, "name": r.get("name"), "img_src": r.get("img_src"),
                     "pro_url": r.get("pro_url")}
                if r.get("$summary"):
                    g["$summary"] = True
                for x in PRODUCT_SUM:
                    g[x] = 0.0
                groups[sid] = g
            for x in PRODUCT_SUM:
                g[x] += _f(r.get(x))
    for g in groups.values():
        for x in PRODUCT_SUM:
            g[x] = int(g[x]) if x != SZ_AMT else round(g[x], 2)
    return {"rows": list(groups.values())}


def _agg_sz_flow(payloads: list) -> dict:
    core: dict = {}
    src: dict = {}
    for pl in payloads or []:
        c = ((pl or {}).get("core") or [None])[0]
        if c:
            for x in FLOW_SUM:
                core[x] = core.get(x, 0.0) + _f(c.get(x))
        for s in (pl or {}).get("source") or []:
            nm = s.get("name")
            e = src.setdefault(nm, {"name": nm, SRC_UV: 0.0, SRC_AMT: 0.0})
            e[SRC_UV] += _f(s.get(SRC_UV))
            e[SRC_AMT] += _f(s.get(SRC_AMT))
    for x in FLOW_SUM:
        if x in core:
            core[x] = int(core[x]) if x != SZ_AMT else round(core[x], 2)
    for e in src.values():
        e[SRC_UV] = int(e[SRC_UV])
        e[SRC_AMT] = round(e[SRC_AMT], 2)
    return {"core": [core] if core else [], "source": list(src.values()), "rawCode": 0}


def _agg_oplog(payloads: list) -> dict:
    seen, rows = set(), []
    for pl in payloads or []:
        for r in (pl or {}).get("rows", []):
            k = "|".join(str(r.get(x)) for x in
                         ("optTime", "actionObjectId", "operationContent", "operationDetails"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(r)
    rows.sort(key=lambda r: str(r.get("optTime") or ""))
    return {"rows": rows}


def load_window(account: str, kind: str, start: str, end: str):
    """把区间内每天的原始数据聚合成一份「等同单次区间抓取」的结果。"""
    days = [d for d in day_span(start, end) if has_day(account, d, kind)]
    if not days:
        return None
    payloads = [load_day(account, d, kind) for d in days]
    payloads = [p for p in payloads if p]
    if kind in AGG:
        spec = AGG[kind]
        rows = _split_sums([r for p in payloads for r in (p.get("rows") or [])], spec)
        out = {"rows": rows, "days": days, "dateStart": days[0], "dateEnd": days[-1]}
        if kind == "jzt_campaign":
            tot = {k: sum(_f(r.get(k)) for r in rows)
                   for k in ("impressions", "clicks", "cost", "totalOrderCnt", "totalOrderSum")}
            out["ext"] = {"impressions": int(tot["impressions"]), "clicks": int(tot["clicks"]),
                          "cost": f"{tot['cost']:.2f}", "totalOrderCnt": int(tot["totalOrderCnt"]),
                          "totalOrderSum": f"{tot['totalOrderSum']:.2f}",
                          "totalOrderROI": f"{tot['totalOrderSum'] / tot['cost']:.2f}" if tot["cost"] else "0.00"}
        return out
    if kind == "sz_product":
        out = _agg_sz_product(payloads)
    elif kind == "sz_flow":
        out = _agg_sz_flow(payloads)
    elif kind == "oplog":
        out = _agg_oplog(payloads)
    else:
        out = {"rows": []}
    out.update({"days": days, "dateStart": days[0], "dateEnd": days[-1]})
    return out


def load_range_window(account: str, kind: str, start: str, end: str) -> dict:
    """同 load_window，但保证返回 dict（缺失时给空结构）。"""
    got = load_window(account, kind, start, end)
    if got is not None:
        return got
    if kind == "sz_product":
        return {"rows": [], "days": [], "dateStart": start, "dateEnd": end}
    if kind == "sz_flow":
        return {"core": [], "source": [], "rawCode": 0, "days": [], "dateStart": start, "dateEnd": end}
    return {"rows": [], "days": [], "dateStart": start, "dateEnd": end}


# ---------------------------------------------------------------- 运行状态
def read_state() -> dict:
    if not settings.STATE_FILE.exists():
        return {}
    try:
        return json.loads(settings.STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_state(**kv) -> dict:
    with _lock:
        cur = read_state()
        cur.update(kv)
        settings.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings.STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(settings.STATE_FILE)
        return cur
