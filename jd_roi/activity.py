# -*- coding: utf-8 -*-
"""团长招商活动：报名记录 + 效果数据的抓取与读取。

两个来源：
  1) 操作日志「活动」页 https://jzt.jd.com/logging/#/business?businessType=32
     接口 POST jzt-api.jd.com/logplatform/common/list/query（btype=32, type=activity）
     「报名团长招商活动」的 operationDetails 里逐商品带出 SKU、佣金、活动起止、团长服务费；
     「报名cps官方活动」是另一套格式（只有商品id + 佣金比例）。
  2) 团长招商活动页 https://jzt.jd.com/jtk/#/activity-cp
     已报名活动 / 团长信息 / 出单概览 / 出单团长 / 商品效果。

落盘（按账号，容器内 /data/activity/<account>/）：
  signups.json            报名明细（按 optTime+活动+SKU 去重，增量合并）
  activities.json         已报名活动快照 + 团长名称/能力分（只增不删，历史报名能反查团长）
  perf/<YYYY-MM-DD>.json  当天出单概览 / 团长 / 商品效果（按天可加总）
  state.json              最近一次抓取信息
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path

from . import catalog, settings

JTK_URL = "https://jzt.jd.com/jtk/#/activity-cp"
LOG_URL = "https://jzt.jd.com/logging/#/business?businessType=32"
JZT_HOME = "https://jzt.jd.com/msa/#/list/tab/plan?objective=overview"
BTYPE = 32

# 报名团长招商：添加商品:100284897922,商品佣金:12%,活动开始时间:2026-11-18,活动结束时间:2026-11-18,团长服务费:3%;
_RE_CP = re.compile(
    r"添加商品[:：]\s*(\d+)\s*,\s*商品佣金[:：]\s*([\d.]+)\s*%"
    r"\s*,\s*活动开始时间[:：]\s*(\d{4}-\d{2}-\d{2})"
    r"\s*,\s*活动结束时间[:：]\s*(\d{4}-\d{2}-\d{2})"
    r"\s*,\s*团长服务费[:：]\s*([\d.]+)\s*%")
# 报名cps官方活动：添加商品:[商品id:100076175178,佣金比例:15%;]
_RE_OFFICIAL = re.compile(r"商品id[:：]\s*(\d+)\s*,\s*佣金比例[:：]\s*([\d.]+)\s*%")

_JS_POST = """
async ({url, payload}) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 20000);
  try {
    const r = await fetch(url, {
      method: 'POST', credentials: 'include', signal: ctl.signal,
      headers: {'Content-Type':'application/json',
        'accept':'application/json, text/plain, */*',
        'referer':'https://jzt.jd.com/jtk/','language':'zh_CN','siteid':'0','loginmode':'0'},
      body: JSON.stringify(payload)
    });
    return {status: r.status, text: await r.text()};
  } finally { clearTimeout(timer); }
}
"""

_write_lock = threading.Lock()
_RUN_LOCK = threading.Lock()
_RUNNING: dict = {"active": False, "startedAt": None, "finishedAt": None,
                  "progress": "", "error": None, "result": None}


# ---------------------------------------------------------------- 路径 / 落盘
def data_root() -> Path:
    return Path(settings.DATA_DIR) / "activity"


def acct_dir(key: str) -> Path:
    d = data_root() / key
    (d / "perf").mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 接口
def _post(page, url: str, payload):
    res = page.evaluate(_JS_POST, {"url": url, "payload": payload})
    if not isinstance(res, dict):
        raise RuntimeError(f"{url} -> 无效响应 {str(res)[:120]}")
    if res.get("status") != 200:
        raise RuntimeError(f"{url} -> HTTP {res.get('status')} {str(res.get('text'))[:160]}")
    d = json.loads(res.get("text") or "{}")
    if d.get("code") not in (1, 0):
        raise RuntimeError(f"{url} -> {json.dumps(d, ensure_ascii=False)[:200]}")
    return d


def _atoms(page, path: str, payload):
    return _post(page, "https://atoms-api.jd.com" + path, payload)


def _days(start: str, end: str):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += dt.timedelta(days=1)


def query_signup_log(page, start: str, end: str, level: str = "activity",
                     window_days: int = 7, verbose=None):
    """按 7 天窗口拉操作日志（接口跨度大会静默截断）。"""
    url = "https://jzt-api.jd.com/logplatform/common/list/query"
    rows, seen, errors = [], set(), []
    win = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    while win <= end_d:
        wend = min(win + dt.timedelta(days=window_days - 1), end_d)
        page_no = 1
        while page_no <= 50:
            payload = {"btype": BTYPE, "pageSize": 100, "pageIndex": page_no, "type": level,
                       "beginTime": win.isoformat(), "endTime": wend.isoformat(),
                       "keywordId": "", "searchType": 1, "operator": "", "optContent": ""}
            try:
                d = _post(page, url, payload)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{win}~{wend} p{page_no}: {exc}")
                break
            body = d.get("data") or {}
            batch = body.get("data") or []
            for r in batch:
                k = "|".join(str(r.get(x)) for x in
                             ("optTime", "actionObjectId", "operationContent", "operationDetails"))
                if k in seen:
                    continue
                seen.add(k)
                rows.append(r)
            if len(batch) < 100 or len(rows) >= (body.get("count") or 0):
                break
            page_no += 1
            time.sleep(0.2)
        if verbose and rows:
            verbose(f"    操作日志 {win}~{wend}：累计 {len(rows)} 条")
        win = wend + dt.timedelta(days=1)
    return rows, errors


def parse_signups(rows: list, account: str, label: str) -> list:
    """把操作日志行拆成「一条报名 = 一行 SKU」。"""
    out = []
    for r in rows:
        det = str(r.get("operationDetails") or "")
        action = str(r.get("operationContent") or "")
        act_id = str(r.get("actionObjectId") or "").strip()
        act_name = str(r.get("actionObject") or "").strip()
        if not act_id:
            m = re.search(r"活动id[\[：:]\s*(\d+)", det)
            act_id = m.group(1) if m else ""
        official = "cps官方" in action or "官方活动" in action
        hits = []
        if official:
            hits = [(sku, {"commission": float(rate), "startDate": "", "endDate": "",
                           "serviceFee": None}) for sku, rate in _RE_OFFICIAL.findall(det)]
        if not hits:
            hits = [(sku, {"commission": float(comm), "startDate": s, "endDate": e,
                           "serviceFee": float(fee)})
                    for sku, comm, s, e, fee in _RE_CP.findall(det)]
        for sku, extra in hits:
            out.append({
                "key": f"{account}|{r.get('optTime')}|{act_id}|{sku}|{action}",
                "account": account,
                "accountLabel": label,
                "optTime": r.get("optTime") or "",
                "action": action,
                "official": official,
                "activityId": act_id,
                "activityName": act_name,
                "skuId": str(sku),
                "commission": extra["commission"],
                "startDate": extra["startDate"],
                "endDate": extra["endDate"],
                "serviceFee": extra["serviceFee"],
            })
    return out


def fetch_applied(page, verbose=None) -> list:
    items, page_no = [], 1
    while page_no <= 40:
        d = _atoms(page, "/union/cp/appliedActivities",
                   {"cpActId": "", "actStatus": 0, "pageIndex": page_no, "pageSize": 50,
                    "hasContact": False, "optSource": 1})
        body = d.get("data") or {}
        batch = body.get("data") or []
        items.extend(batch)
        total = int(body.get("total") or 0)
        if not batch or len(items) >= total:
            break
        page_no += 1
        time.sleep(0.2)
    if verbose:
        verbose(f"    已报名活动 {len(items)} 个")
    return items


def fetch_unions(page, ids: list) -> dict:
    ids = [int(x) for x in dict.fromkeys(ids) if x]
    out: dict = {}
    for i in range(0, len(ids), 20):
        batch = ids[i:i + 20]
        try:
            d = _atoms(page, "/union/cp/getUnionBaseInfo", batch)
            for u in d.get("data") or []:
                uid = str(u.get("unionId"))
                out.setdefault(uid, {}).update(
                    {k: u.get(k) for k in ("name", "pin", "imageUrl", "contact", "serviceTime")})
        except Exception:  # noqa: BLE001
            pass
        try:
            d = _atoms(page, "/union/cp/getUnionTalentBatch", batch)
            for u in d.get("data") or []:
                uid = str(u.get("unionId"))
                out.setdefault(uid, {}).update(
                    {k: u.get(k) for k in ("compositeScore", "unionGrade", "channelScore",
                                           "dealScore", "goodsServiceScore",
                                           "orderCountStar", "gmvStar", "buyRate")})
        except Exception:  # noqa: BLE001
            pass
    return out


def _paged(page, path: str, base: dict, page_no: int = 1, page_size: int = 100,
           max_pages: int = 30) -> list:
    rows = []
    for i in range(max_pages):
        p = dict(base)
        p["pageNo"] = page_no + i
        p["pageSize"] = page_size
        d = _atoms(page, path, p)
        body = d.get("data") or {}
        batch = body.get("data") or []
        rows.extend(batch)
        total = int(body.get("total") or 0)
        if not batch or len(rows) >= total:
            break
        time.sleep(0.15)
    return rows


def fetch_perf_day(page, day: str) -> dict:
    base = {"startDate": day, "endDate": day}
    summ = (_atoms(page, "/union/cp/actReport/summary", base).get("data") or {})
    cp = _paged(page, "/union/cp/actReport/cpUnionAnalysis", base)
    sku = _paged(page, "/union/cp/actReport/skuAnalysis", base)
    return {"date": day, "fetchedAt": settings.now_iso(), "summary": summ, "cp": cp, "sku": sku}


# ---------------------------------------------------------------- 订单明细（活动维度）
# 团长诊断只按团长/商品汇总，没有活动维度；订单明细里每单带 cpActId，
# 按它汇总才能拿到「这个活动自己的订单/GMV/佣金」，这是活动视图的数据来源。
ORDER_URL = "https://atoms-api.jd.com/union/pop/report/detail/orders?requestFrom=0"
ORDER_PAGE = "https://jzt.jd.com/jtk/#/order-detail"
# CPS 花费（数据看板 → 付款佣金）按「订单完成时间」统计；订单明细用完成日筛选后，
# 逐单 totalCosFee = 佣金 + 团长服务费 + 京东平台技术服务费，与 payCommAmount 完全一致。
CPS_BASIS = "finishTime"
_ORDER_BASE = {
    "startDate": "", "endDate": "", "startCptDay": None, "endCptDay": None,
    "sourceEmt": None, "isValid": 1, "pageIndex": 1, "pageSize": 100,
    "orderTimeFlag": 1, "cptTimeFlag": 0, "taskId": None, "contentId": None,
    "selectType": 0, "actType": 0, "orderId": "", "orderState": None,
    "deliveryModeType": 1,
}


def fetch_order_day(page, day: str, max_pages: int = 30) -> dict:
    """拉某天的订单明细，只保留有效订单且属于团长活动的，按活动 → 商品汇总。

    这个接口的几个坑（实测）：
      * 第 1 页偶发空包（会话没挂稳），要重试，且空包时 total 可能是 0 而不是 None；
      * 单次查询超过约 20 页会被限流（后续页一直空），所以按天拉、页间隔 1 秒；
      * total 有时返回 0，这时以“最后一页不足 100 条”判定抓完。
    """
    rows, page_no, total, retry = [], 1, None, 0
    seen = set()
    complete = False
    while page_no <= max_pages:
        payload = dict(_ORDER_BASE, startDate=day, endDate=day, pageIndex=page_no)
        d = _post(page, ORDER_URL, payload)
        body = d.get("content") or d.get("data") or {}
        batch = body.get("data") or []
        t = body.get("total")
        if t:
            total = int(t)
        if not batch:
            # 空包多半是「刚拉完一个几百单的大日子，被限流了」，2 秒重试救不回来；
            # 按 3/8/18 秒退避，等限流窗口过去再试同一页。
            if retry < 3:
                time.sleep((3, 8, 18)[retry])
                retry += 1
                continue
            break
        retry = 0
        for r in batch:
            oid = r.get("orderId")
            if oid in seen:
                continue
            seen.add(oid)
            rows.append(r)
        if total and len(rows) >= total:
            complete = True
            break
        if len(batch) < 100:
            complete = True
            break
        page_no += 1
        time.sleep(0.8)
    acts: dict = {}
    for r in rows:
        try:
            if int(r.get("isValid") or 0) != 1:
                continue
            aid = str(int(r.get("cpActId") or 0))
            if aid == "0":
                continue
            sid = str(r.get("skuId") or "")
            gmv = float(r.get("skuPrice") or 0)
            cosfee = float(r.get("cosFee") or 0)
            cpfee = float(r.get("cpFee") or 0)
            qty = int(r.get("skuCount") or 0)
        except (TypeError, ValueError):
            continue
        name = str(r.get("cpActName") or r.get("actName") or r.get("planName") or aid)
        e = acts.setdefault(aid, {"actName": name, "orders": 0, "qty": 0, "gmv": 0.0,
                                  "cosFee": 0.0, "cpFee": 0.0, "skus": {}})
        e["orders"] += 1
        e["qty"] += qty
        e["gmv"] += gmv
        e["cosFee"] += cosfee
        e["cpFee"] += cpfee
        sk = e["skus"].setdefault(sid, {"name": str(r.get("skuName") or sid), "orders": 0,
                                        "qty": 0, "gmv": 0.0, "cosFee": 0.0, "cpFee": 0.0})
        sk["orders"] += 1
        sk["qty"] += qty
        sk["gmv"] += gmv
        sk["cosFee"] += cosfee
        sk["cpFee"] += cpfee
    return {"date": day, "fetchedAt": settings.now_iso(), "total": len(rows),
            "expected": total or None, "complete": complete, "acts": acts}


# ---------------------------------------------------------------- CPS 按天（完成日）
def cps_day_path(key: str, day: str) -> Path:
    return acct_dir(key) / "cpsdays" / f"{day}.json"


def _fee_of(row: dict) -> float:
    """一单的 CPS 费用：优先 totalCosFee，缺失时用三项相加兜底。"""
    if row.get("totalCosFee") is not None:
        try:
            return float(row.get("totalCosFee") or 0)
        except (TypeError, ValueError):
            pass
    total = 0.0
    for k in ("cosFee", "cpFee", "platformServiceFee"):
        try:
            total += float(row.get(k) or 0)
        except (TypeError, ValueError):
            pass
    return total


def fetch_cps_day(page, day: str, max_pages: int = 60) -> dict:
    """拉某天「订单完成」的全部有效订单，按 SKU 汇总 CPS 费用。

    口径与京准通「数据看板 → 商品数据」的付款佣金完全一致：
      * 订单明细按**完成日**（cptTimeFlag=1 + startCptDay/endCptDay）查询；
      * 取每单 totalCosFee（佣金 + 团长服务费 + 平台技术服务费）；
      * 只统计有效订单（isValid=1）。
    这样一次分页抓取即可覆盖当天所有 SKU，不必再逐个 SKU 请求 orderData。
    """
    rows, page_no, total, retry = [], 1, None, 0
    seen: set = set()
    complete = False
    while page_no <= max_pages:
        payload = dict(_ORDER_BASE, startDate=None, endDate=None,
                       startCptDay=day, endCptDay=day, orderTimeFlag=0,
                       cptTimeFlag=1, isValid=1, pageIndex=page_no)
        d = _post(page, ORDER_URL, payload)
        body = d.get("content") or d.get("data") or {}
        batch = body.get("data") or []
        if body.get("total") is not None:
            total = int(body.get("total") or 0)
        if not batch:
            # 与订单明细页相同的坑：首页偶发空包 / 单次超过约 20 页被限流
            if retry < 3:
                retry += 1
                time.sleep(2)
                continue
            if total == 0 and page_no == 1:
                complete = True          # 这一天确实没有完成订单
            break
        retry = 0
        for r in batch:
            k = (r.get("orderId"), r.get("skuId"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(r)
        if total and len(rows) >= total:
            complete = True
            break
        if len(batch) < 100:
            complete = True
            break
        page_no += 1
        time.sleep(0.7)

    skus: dict = {}
    for r in rows:
        try:
            if int(r.get("isValid") or 1) != 1:
                continue
        except (TypeError, ValueError):
            continue
        sid = str(r.get("skuId") or "").strip()
        if not sid:
            continue
        fee = _fee_of(r)
        e = skus.setdefault(sid, {"skuId": int(sid) if sid.isdigit() else sid,
                                  "name": str(r.get("skuName") or ""),
                                  "fee": 0.0, "cosFee": 0.0, "cpFee": 0.0,
                                  "platformFee": 0.0, "orders": 0})
        e["fee"] += fee
        for src, dst in (("cosFee", "cosFee"), ("cpFee", "cpFee"),
                         ("platformServiceFee", "platformFee")):
            try:
                e[dst] += float(r.get(src) or 0)
            except (TypeError, ValueError):
                pass
        e["orders"] += 1
        if not e["name"] and r.get("skuName"):
            e["name"] = str(r.get("skuName"))

    for e in skus.values():
        for k in ("fee", "cosFee", "cpFee", "platformFee"):
            e[k] = round(float(e[k]), 2)
    return {"date": day, "basis": CPS_BASIS, "source": "order-detail",
            "fetchedAt": settings.now_iso(), "orders": len(rows),
            "expected": total, "complete": complete,
            "total": round(sum(float(e["fee"]) for e in skus.values()), 2),
            "skus": skus}


def cps_day_good(key: str, day: str) -> bool:
    """该账号这一天的 CPS 日缓存是否可用（完整抓到过，且当天已经结束）。

    当天还在结算，抓到的「完成订单」只会越来越多；把当天当完整缓存会把
    半天的数据冻住。所以只承认 < 今天的日期。
    """
    if str(day) >= settings.today().isoformat():
        return False
    p = cps_day_path(key, day)
    if not p.exists():
        return False
    d = _read(p, {}) or {}
    return bool(d.get("complete")) and str(d.get("date") or "") == day


def cps_payload(key: str, start: str, end: str) -> dict | None:
    """用按天缓存拼出任意区间的 CPS 花费；缺任意一天都返回 None（不拿估算顶替）。"""
    days = list(_days(start, end))
    if not days:
        return None
    today = settings.today().isoformat()
    skus: dict = {}
    daily: list = []
    latest = 0.0
    for day in days:
        p = cps_day_path(key, day)
        d = _read(p, None)
        if (day >= today or not d or not d.get("complete")
                or str(d.get("date") or "") != day):
            return None
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
        daily.append({"date": day,
                      "payCommAmount": round(float(d.get("total") or 0), 2),
                      "fiOrderCnt": int(d.get("orders") or 0),
                      "orderCnt": int(d.get("orders") or 0)})
        for sid, s in (d.get("skus") or {}).items():
            sid = str(sid)
            e = skus.setdefault(sid, {
                "skuId": int(sid) if sid.isdigit() else sid, "itemName": "",
                "fee": 0.0, "orders": 0})
            e["fee"] += float(s.get("fee") or 0)
            e["orders"] += int(s.get("orders") or 0)
            if not e["itemName"] and s.get("name"):
                e["itemName"] = str(s.get("name"))
    rows = []
    for e in skus.values():
        name = e["itemName"]
        rows.append({"skuId": e["skuId"], "itemName": name,
                     "category": catalog.category_of(name),
                     "payCommAmount": round(e["fee"], 2),
                     "payCommRate": None, "preCommAmount": None, "preCommRate": None,
                     "clicks": None, "uv": None, "orderCnt": None, "orderCvr": None,
                     "orderGmv": None, "payOrderCnt": None,
                     "fiOrderCnt": e["orders"], "fiOrderGmv": None,
                     "orderUserCnt": None, "roi": None})
    rows.sort(key=lambda r: -(r.get("payCommAmount") or 0))
    total = round(sum(float(r["payCommAmount"]) for r in rows), 2)
    return {"jobId": f"cpsdays-{key}-{start}_{end}", "mode": "cps", "account": key,
            "accountLabel": settings.get_account(key).get("label") or key,
            "start": start, "end": end, "rows": rows, "daily": daily,
            "source": "activity_orders",
            "sourceLabel": "团长活动 · 订单明细（完成日口径）",
            "days": len(days), "target": f"cps:{key}:{start}_{end}",
            "noSpend": total == 0, "total": total,
            "finishedAt": settings.now_iso(),
            "savedAt": (time.strftime("%Y-%m-%d %H:%M", time.localtime(latest))
                        if latest else None)}


def fetch_cps_into_cache(page, key: str, start: str, end: str,
                         on_progress=None) -> dict | None:
    """借一个已打开的京准通页面，把区间内缺失的 CPS 按天缓存补齐。

    只补缺的天；已经完整抓过的日期直接跳过。全部齐了才返回区间结果，
    否则返回 None（已抓到的天仍然落盘，下次只补剩下的）。
    """
    if not settings.CPS_DAYS_ENABLED:
        return None
    today = settings.today().isoformat()
    need = [d for d in _days(start, end) if d < today and not cps_day_good(key, d)]
    if not need:
        return cps_payload(key, start, end)
    try:
        if "order-detail" not in (page.url or ""):
            page.goto(ORDER_PAGE, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(12000)
    except Exception:  # noqa: BLE001
        return None

    # 连续翻十几天的订单，中途常被限流（某天开始一直返回空页）。先整段跑一遍，
    # 失败的日期收集起来，重载页面后再补一轮；还不行的留到下次，不阻塞其它天。
    pending = list(need)
    for _pass in range(2):
        if not pending:
            break
        still: list = []
        for day in pending:
            got = None
            for _attempt in range(2):
                try:
                    got = fetch_cps_day(page, day)
                except Exception as exc:  # noqa: BLE001
                    got = None
                    if on_progress:
                        on_progress(f"{day} 取数失败（{type(exc).__name__}），重试…")
                if got and got.get("complete"):
                    break
                try:
                    page.reload()
                    page.wait_for_timeout(9000)
                except Exception:  # noqa: BLE001
                    pass
            if got and got.get("complete"):
                _write(cps_day_path(key, day), got)
                if on_progress:
                    on_progress(f"{day} 完成 {got['total']:,.2f} 元（{got['orders']} 单）")
            else:
                still.append(day)
            time.sleep(2)
        pending = still
        if pending:
            try:
                page.reload()
                page.wait_for_timeout(9000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
    return cps_payload(key, start, end) if not pending else None


# ---------------------------------------------------------------- 合并落盘
def _merge_signups(key: str, fresh: list) -> dict:
    path = acct_dir(key) / "signups.json"
    with _write_lock:
        old = _read(path, {"rows": []})
        merged = {r.get("key"): r for r in (old.get("rows") or []) if r.get("key")}
        for r in fresh:
            prev = merged.get(r["key"]) or {}
            # 用新抓的字段覆盖，但不用空值抹掉之前已经解析出的团长/活动期
            row = dict(prev)
            row.update({k: v for k, v in r.items() if v not in (None, "")})
            merged[r["key"]] = row
        rows = sorted(merged.values(), key=lambda r: r.get("optTime") or "", reverse=True)
        _write(path, {"updatedAt": settings.now_iso(), "rows": rows})
    return {"rows": rows, "added": max(len(rows) - len(old.get("rows") or []), 0)}


def _merge_activities(key: str, applied: list, unions: dict) -> dict:
    path = acct_dir(key) / "activities.json"
    with _write_lock:
        old = _read(path, {"items": {}, "unions": {}})
        items = dict(old.get("items") or {})
        seen = set()
        for a in applied:
            aid = str(a.get("cpActId") or "")
            if not aid:
                continue
            seen.add(aid)
            prev = items.get(aid) or {}
            item = {
                "activityId": aid,
                "title": a.get("title"),
                "cpUnionId": str(a.get("cpUnionId") or ""),
                "startTime": a.get("startTime"),
                "endTime": a.get("endTime"),
                "actStatus": a.get("actStatus"),
                "status": a.get("status"),
                "appliedGoodsCnt": a.get("appliedGoodsCnt"),
                "passGoodsCnt": a.get("passGoodsCnt"),
                "commissionRateMin": a.get("commissionRateMin"),
                "commissionRateMax": a.get("commissionRateMax"),
                "categories": [c.get("categoryName") for c in (a.get("categoryVo") or [])][:6],
                "updatedAt": settings.now_iso(),
                "firstSeen": prev.get("firstSeen") or settings.now_iso(),
            }
            items[aid] = item
        for aid, it in items.items():
            it["active"] = aid in seen
        uu = dict(old.get("unions") or {})
        for uid, info in (unions or {}).items():
            uu[uid] = {**(uu.get(uid) or {}), **{k: v for k, v in info.items() if v is not None}}
        _write(path, {"updatedAt": settings.now_iso(), "items": items, "unions": uu})
    return {"items": items, "unions": uu}


def _perf_empty(perf: dict) -> bool:
    """summary 全是 null 且团长/商品都为空，视为「这天没抓到」。"""
    s = perf.get("summary") or {}
    if any(isinstance(v, (int, float)) for v in s.values()):
        return False
    return not (perf.get("cp") or perf.get("sku"))


def _save_perf(key: str, perf: dict) -> bool:
    """空结果不覆盖已有数据；返回是否真正写入。"""
    path = acct_dir(key) / "perf" / f"{perf['date']}.json"
    if _perf_empty(perf):
        old = _read(path, None)
        if old and not _perf_empty(old):
            return False
    _write(path, perf)
    return True


def _save_state(key: str, info: dict) -> None:
    _write(acct_dir(key) / "state.json", info)


# ---------------------------------------------------------------- 抓取主流程
def _jtk_ok(page) -> bool:
    try:
        return "/jtk" in page.url and "gw/index" not in page.url
    except Exception:  # noqa: BLE001
        return False


def run(accounts=None, start: str | None = None, end: str | None = None,
        perf_days: int | None = None, log=lambda m: None, acquire_lock: bool = True) -> dict:
    """抓取报名日志 [start,end] + 最近 perf_days 天效果。"""
    from .browser import launch_profile, has_login, session_ok

    accs = accounts or settings.ad_accounts()
    today = settings.today()
    end = end or today.isoformat()
    start = start or (today - dt.timedelta(days=30)).isoformat()
    perf_days = int(perf_days or settings.REFRESH_DAYS or 15)
    # 团长诊断（actReport）D-1 的数据要到次日早上才更新，凌晨抓只能拿到空包；
    # 所以窗口包含今天，由白天的补抓把 D-1 / 今天补上（见 refresh_recent_perf）。
    perf_end = min(dt.date.fromisoformat(end), today).isoformat()
    perf_start = (dt.date.fromisoformat(perf_end) - dt.timedelta(days=perf_days - 1)).isoformat()
    # 订单明细 / CPS 仍只抓到昨天：当天订单还没跑完，抓了也是半截
    data_end = min(dt.date.fromisoformat(end), today - dt.timedelta(days=1)).isoformat()
    data_start = (dt.date.fromisoformat(data_end) - dt.timedelta(days=perf_days - 1)).isoformat()

    from . import scrape_day
    if acquire_lock and not scrape_day.acquire(timeout=3600, name="activity"):
        raise RuntimeError("抓取锁被占用，稍后再试")
    _RUNNING.update({"active": True, "startedAt": settings.now_iso(), "finishedAt": None,
                     "progress": f"报名日志 {start}~{end}", "error": None, "result": None})
    summary = {"start": start, "end": end, "perfStart": perf_start, "perfEnd": perf_end,
               "accounts": {}}
    err = None
    try:
        for acc in accs:
            key = acc["key"]
            label = acc.get("label") or key
            rec = {"label": label, "signups": 0, "activities": 0, "perfDays": 0,
                   "cpsDays": 0, "errors": []}
            summary["accounts"][key] = rec
            _RUNNING["progress"] = f"[{label}] 团长活动抓取中…"
            log(f"[{label}] 团长活动抓取：报名日志 {start}~{end}，效果 {perf_start}~{perf_end}")
            pw = ctx = None
            try:
                pw, ctx = launch_profile(headless=settings.HEADLESS, account=key)
                page = ctx.new_page()
                page.goto(JZT_HOME, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(5000)
                if not (has_login(ctx) and session_ok(page)):
                    rec["errors"].append("登录态失效")
                    log(f"[{label}] 登录态失效，跳过")
                    continue

                # --- 团长招商活动页 ---
                page.goto(JTK_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(6000)
                unions = {}
                if _jtk_ok(page):
                    applied = fetch_applied(page, verbose=log)
                    union_ids = [a.get("cpUnionId") for a in applied]
                    unions = fetch_unions(page, union_ids)
                    merged = _merge_activities(key, applied, unions)
                    rec["activities"] = len(applied)
                    for day in _days(perf_start, perf_end):
                        try:
                            perf = fetch_perf_day(page, day)
                            # 团长名补进当天数据，页面不用再查一次
                            for row in perf.get("cp") or []:
                                u = unions.get(str(row.get("cpUnionId"))) or {}
                                row["cpUnionName"] = u.get("name")
                            _save_perf(key, perf)
                            rec["perfDays"] += 1
                        except Exception as exc:  # noqa: BLE001
                            rec["errors"].append(f"perf {day}: {exc}")
                    log(f"[{label}] 已报名活动 {rec['activities']} 个，效果 {rec['perfDays']} 天")
                    # 订单明细 → 活动维度。必须先进「订单明细」页等它初始化，
                    # 否则接口会偶发返回空包（会话/身份还没挂上）。
                    try:
                        page.goto("https://jzt.jd.com/jtk/#/order-detail",
                                  wait_until="domcontentloaded", timeout=90000)
                        page.wait_for_timeout(12000)
                        for day in _days(data_start, data_end):
                            path = acct_dir(key) / "orders" / f"{day}.json"
                            try:
                                od = fetch_order_day(page, day)
                            except Exception as exc:  # noqa: BLE001
                                rec["errors"].append(f"orders {day}: {exc}")
                                try:
                                    page.reload()
                                    page.wait_for_timeout(9000)
                                    od = fetch_order_day(page, day)
                                except Exception as exc2:  # noqa: BLE001
                                    rec["errors"].append(f"orders {day} retry: {exc2}")
                                    time.sleep(5)
                                    continue
                            old = _read(path, {}) or {}
                            new_total = int(od.get("total") or 0)
                            old_total = int(old.get("total") or 0)
                            # 0 行且不完整基本是限流空包，绝不能覆盖缓存（否则活动页会显示 0 单）
                            if od.get("complete") or new_total > old_total:
                                _write(path, od)
                                rec["orderDays"] = rec.get("orderDays", 0) + 1
                            else:
                                rec["errors"].append(
                                    f"orders {day}: 本次不完整 {new_total}/{od.get('expected')}，保留旧数据")
                            time.sleep(2)
                        log(f"[{label}] 订单明细 {rec.get('orderDays', 0)} 天")
                        # 补漏：被限流/会话断开时可能连续多天没写成，整段跑完再重载页面补一遍
                        _odir = acct_dir(key) / "orders"

                        def _stale(d: str) -> bool:
                            p = _odir / f"{d}.json"
                            if not p.exists():
                                return True
                            cached = _read(p, {}) or {}
                            # 只补「抓了一半」的天；0 行天可能是真的没单，不用每晚重试
                            return not cached.get("complete") and int(cached.get("total") or 0) > 0

                        _missing = [d for d in _days(data_start, data_end) if _stale(d)]
                        if _missing:
                            log(f"[{label}] 补漏 {len(_missing)} 天：{_missing[0]} ~ {_missing[-1]}")
                            try:
                                page.reload()
                                page.wait_for_timeout(9000)
                            except Exception:  # noqa: BLE001
                                pass
                            for day in _missing:
                                try:
                                    od = fetch_order_day(page, day)
                                    if od.get("total") or od.get("complete"):
                                        _write(_odir / f"{day}.json", od)
                                        rec["orderDays"] = rec.get("orderDays", 0) + 1
                                except Exception as exc:  # noqa: BLE001
                                    rec["errors"].append(f"orders repair {day}: {exc}")
                                time.sleep(2)
                        # CPS 按天：同一页面按「订单完成日」再查一遍，逐单 totalCosFee
                        # 与数据看板付款佣金同口径。抓过就跳过，月度利润任意子区间可直接复用。
                        if settings.CPS_DAYS_ENABLED:
                            for day in _days(data_start, data_end):
                                if cps_day_good(key, day):
                                    continue
                                got = None
                                for _try in range(2):
                                    try:
                                        got = fetch_cps_day(page, day)
                                    except Exception as exc:  # noqa: BLE001
                                        got = None
                                        rec["errors"].append(f"cps {day}: {exc}")
                                    if got and got.get("complete"):
                                        break
                                    try:
                                        page.reload()
                                        page.wait_for_timeout(9000)
                                    except Exception:  # noqa: BLE001
                                        pass
                                if got and got.get("complete"):
                                    _write(cps_day_path(key, day), got)
                                    rec["cpsDays"] = rec.get("cpsDays", 0) + 1
                                else:
                                    rec["errors"].append(f"cps {day}: 本次不完整，保留旧数据")
                                time.sleep(1.5)
                            log(f"[{label}] CPS 按天缓存 +{rec.get('cpsDays', 0)} 天")
                    except Exception as exc:  # noqa: BLE001
                        rec["errors"].append(f"orders page: {exc}")
                        log(f"[{label}] 订单明细页打开失败：{exc}")
                else:
                    rec["errors"].append("团长招商页不可访问")
                    log(f"[{label}] 团长招商页不可访问（可能无权限），跳过效果数据")
                    merged = _read(acct_dir(key) / "activities.json",
                                   {"items": {}, "unions": {}})
                    unions = merged.get("unions") or {}

                # --- 操作日志-活动 ---
                page.goto(LOG_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(6000)
                rows, errors = query_signup_log(page, start, end, verbose=log)
                fresh = parse_signups(rows, key, label)
                unparsed = 0
                for r in rows:
                    det = str(r.get("operationDetails") or "")
                    if ("报名" in str(r.get("operationContent") or "")
                            and not (_RE_CP.search(det) or _RE_OFFICIAL.search(det))):
                        unparsed += 1
                if unparsed:
                    rec["errors"].append(f"{unparsed} 条报名日志格式未识别")
                # 报名时就把活动→团长落进去，历史记录也能显示团长
                amap = merged.get("items") or {}
                for r in fresh:
                    info = amap.get(str(r["activityId"])) or {}
                    uid = str(info.get("cpUnionId") or "")
                    r["cpUnionId"] = uid
                    r["cpUnionName"] = (unions.get(uid) or {}).get("name") or ""
                    r["actStart"] = info.get("startTime") or ""
                    r["actEnd"] = info.get("endTime") or ""
                res = _merge_signups(key, fresh)
                rec["signups"] = len(fresh)
                rec["errors"].extend(errors[:3])
                _save_state(key, {"lastRun": settings.now_iso(), "start": start, "end": end,
                                  "perfStart": perf_start, "perfEnd": perf_end,
                                  "signupRows": len(res["rows"]), "added": res["added"],
                                  "cpsDays": rec.get("cpsDays", 0)})
                log(f"[{label}] 报名记录 {len(fresh)} 条（库存 {len(res['rows'])} 条，新增 {res['added']}）")
            except Exception as exc:  # noqa: BLE001
                rec["errors"].append(f"{type(exc).__name__}: {exc}")
                log(f"[{label}] 抓取失败：{type(exc).__name__}: {exc}")
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
    except BaseException as exc:
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _RUNNING.update({"active": False, "finishedAt": settings.now_iso(),
                         "progress": "", "error": err, "result": summary})
        if acquire_lock:
            scrape_day.release()
    return summary


def refresh_recent_perf(days: int = 2, accounts=None, log=lambda m: None) -> dict:
    """只补抓最近 N 天的团长诊断（出单概览 / 出单团长 / 商品效果）。

    京东「团长诊断」的 D-1 数据要次日早上才更新，凌晨 01:00 主抓取拿到的是空包，
    所以白天再补一次，避免页面「出单团长」表一直缺最新 1~2 天。
    """
    from . import scrape_day
    from .browser import launch_profile, has_login, session_ok

    accs = accounts or settings.ad_accounts()
    today = settings.today()
    start = today - dt.timedelta(days=max(int(days), 1) - 1)
    out = {"start": start.isoformat(), "end": today.isoformat(), "accounts": {}}
    if not scrape_day.acquire(timeout=1800, name="activity-perf"):
        raise RuntimeError("抓取锁被占用，稍后再试")
    try:
        for acc in accs:
            key = acc["key"]
            label = acc.get("label") or key
            rec = {"label": label, "filled": 0, "empty": [], "errors": []}
            out["accounts"][key] = rec
            log(f"[{label}] 团长诊断补抓：{start}~{today}")
            pw = ctx = None
            try:
                pw, ctx = launch_profile(headless=settings.HEADLESS, account=key)
                page = ctx.new_page()
                page.goto(JZT_HOME, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(5000)
                if not (has_login(ctx) and session_ok(page)):
                    rec["errors"].append("登录态失效")
                    log(f"[{label}] 登录态失效，跳过")
                    continue
                page.goto(JTK_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(6000)
                if not _jtk_ok(page):
                    rec["errors"].append("团长招商页不可访问")
                    continue
                unions = (_read(acct_dir(key) / "activities.json", {}) or {}).get("unions") or {}
                for day in _days(start.isoformat(), today.isoformat()):
                    try:
                        perf = fetch_perf_day(page, day)
                        for row in perf.get("cp") or []:
                            u = unions.get(str(row.get("cpUnionId"))) or {}
                            row["cpUnionName"] = row.get("cpUnionName") or u.get("name")
                        if _perf_empty(perf):
                            rec["empty"].append(day)
                        if _save_perf(key, perf):
                            rec["filled"] += 1
                    except Exception as exc:  # noqa: BLE001
                        rec["errors"].append(f"perf {day}: {exc}")
                    time.sleep(1)
                log(f"[{label}] 团长诊断补抓完成：写入 {rec['filled']} 天"
                    + (f"，仍为空 {','.join(rec['empty'])}" if rec["empty"] else ""))
            except Exception as exc:  # noqa: BLE001
                rec["errors"].append(f"{type(exc).__name__}: {exc}")
                log(f"[{label}] 团长诊断补抓失败：{type(exc).__name__}: {exc}")
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
    finally:
        scrape_day.release()
    return out


def run_background(accounts=None, start: str | None = None, end: str | None = None,
                   perf_days: int | None = None, log=None) -> dict:
    """后台线程抓取（网页按钮 / 夜间任务共用）。已有任务在跑则不重复启动。"""
    if _RUNNING.get("active"):
        return {"ok": False, "error": "团长活动抓取已在运行"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "团长活动抓取已在运行"}
    _RUNNING.update({"active": True, "startedAt": settings.now_iso(), "finishedAt": None,
                     "progress": "排队等待抓取锁…", "error": None, "result": None})

    def _work():
        try:
            run(accounts=accounts, start=start, end=end, perf_days=perf_days,
                log=log or (lambda m: None))
        except Exception as exc:  # noqa: BLE001
            _RUNNING.update({"active": False, "error": f"{type(exc).__name__}: {exc}",
                             "finishedAt": settings.now_iso()})
        finally:
            _RUN_LOCK.release()

    th = threading.Thread(target=_work, name="activity-scrape", daemon=True)
    th.start()
    return {"ok": True, "thread": th.name}


def runtime_status() -> dict:
    return dict(_RUNNING)


# ---------------------------------------------------------------- 读取 / 汇总
def load_signups(keys: list, since: str | None = None) -> list:
    rows = []
    for key in keys:
        data = _read(acct_dir(key) / "signups.json", {"rows": []})
        for r in data.get("rows") or []:
            if since and str(r.get("optTime") or "")[:10] < since:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r.get("optTime") or "", reverse=True)
    return rows


def load_activities(keys: list) -> dict:
    items, unions = {}, {}
    for key in keys:
        data = _read(acct_dir(key) / "activities.json", {})
        items.update(data.get("items") or {})
        unions.update(data.get("unions") or {})
    return {"items": items, "unions": unions}


def load_perf(keys: list, start: str, end: str) -> dict:
    days = []
    for key in keys:
        for day in _days(start, end):
            p = acct_dir(key) / "perf" / f"{day}.json"
            if p.exists():
                d = _read(p, None)
                if d:
                    d["account"] = key
                    days.append(d)
    return {"days": days}


def load_orders(keys: list, start: str, end: str) -> dict:
    days = []
    for key in keys:
        for day in _days(start, end):
            p = acct_dir(key) / "orders" / f"{day}.json"
            if p.exists():
                d = _read(p, None)
                # 0 行且不完整的文件是限流时的坏缓存，当作没抓到，别让页面显示成「没出单」
                if d and (d.get("complete") or int(d.get("total") or 0) > 0):
                    d["account"] = key
                    days.append(d)
    return {"days": days}
