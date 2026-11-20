# -*- coding: utf-8 -*-
"""超级补贴消息汇总：京麦工作台「消息 → 店铺 → 超级补贴」。

入口：https://shop.jd.com/jdm/home  → 右上角「消息」→ 顶部「店铺」页签
      → 左侧分类「超级补贴」（msgCat=marketing / msgCode=262）。

消息接口 `sff.jd.com/api?...MessageListProxy.getMsgListForPage` 带 h5st 签名，
无法自己构造，所以流程是：先让页面真实发一次请求、截获它的请求头（含 h5st），
再用同一组请求头把 pageSize 放大重放，一次拿全历史消息。

产出两种视图：
  - skus：按 SKU 汇总的最新补贴状态（竞价成功 / 替补 / 已下线 / 已失效 …）
  - messages：逐条原始消息（含报名ID、补贴价、促销数量、详情链接）

缓存：data/reports/subsidy_<账号>.json（一份账号一份，永不自动过期；
用户点「重新抓取」才覆盖）。页面读缓存秒开。
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

from . import settings

API = ("https://sff.jd.com/api?v=1.0&appId=ZSCUMIUH2ZNF8Z2PVU1J"
       "&api=dsm.pop.jm.msg.center.jsf.message.MessageListProxy.getMsgListForPage")
MSG_CAT = "marketing"
MSG_CODE = 262
HOME = "https://shop.jd.com/jdm/home"

# 有业务含义的状态（决定 SKU 当前状态）；报名邀请 / 其他不参与状态判定
STATUS_ORDER = ["win", "substitute", "offline", "expired", "deleted",
                "quit", "lose", "onfail"]


def _cache_path(account: str) -> Path:
    return Path(settings.REPORT_DIR) / f"subsidy_{account}.json"


def load_cache(account: str) -> dict | None:
    fp = _cache_path(account)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(account: str, payload: dict) -> None:
    fp = _cache_path(account)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(fp)


# ---------------------------------------------------------------- 解析
def _clean(s) -> str:
    return html.unescape(str(s or "")).strip()


def _detail_url(item: dict) -> str:
    for key in ("pcProtocolid", "macProtocolid", "protocolId"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            continue
        url = (d or {}).get("replaceField") or ""
        if url:
            return url
    return ""


def _classify(content: str) -> tuple[str, str]:
    if content.startswith("【商品汰换下线】"):
        return "offline", "已下线"
    if "【超级补贴商品在线转替补】" in content:
        return "substitute", "替补"
    if content.startswith("【超级补贴促销删除】"):
        return "deleted", "已删除"
    if content.startswith("【超级补贴活动上场失败】"):
        return "onfail", "上场失败"
    if content.startswith("【超级补贴竞价成功】"):
        if "成为替补" in content:
            return "substitute", "替补"
        return "win", "竞价成功"
    if "已经失效" in content:
        return "expired", "已失效"
    if "退出活动申请已审核通过" in content:
        return "quit", "已退出"
    if "竞价失败" in content:
        return "lose", "竞价失败"
    if "采销邀请您报名" in content:
        return "invite", "报名邀请"
    return "other", "其他"


def _first(patterns, text: str) -> str:
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_message(item: dict) -> dict:
    content = _clean(item.get("content"))
    kind, label = _classify(content)
    sku = _first([r"报名SKU[：:]\s*【?(\d{5,20})】?",
                  r"SKU[：:]\s*(\d{5,20})",
                  r"商品：\s*（(\d{5,20})，"], content)
    name = _first([r"商品名称：(.+?)（SKU", r"商品名称：(.+?)（报名id",
                   r"商品名称：(.+?)（报名ID",
                   r"商品：\s*（\d{5,20}，([^）]+)）"], content)
    enroll = _first([r"报名ID[：:]\s*【?(\d{4,20})】?", r"报名id[：:]\s*(\d{4,20})"],
                    content)
    price = _float(_first([r"补贴后价格\s*([\d.]+)\s*元",
                           r"超级补贴价格[：:]\s*([\d.]+)"], content))
    promo_qty = _float(_first([r"促销数量\s*(\d+)"], content))
    subsidy_amt = _float(_first([r"补贴金额\s*([\d.]+)\s*元"], content))
    subsidy_qty = _float(_first([r"补贴数量\s*(\d+)"], content))
    return {
        "msgId": str(item.get("msgId") or ""),
        "time": _clean(item.get("time")),
        "unread": str(item.get("read")) == "0",
        "kind": kind,
        "statusLabel": label,
        "skuId": sku,
        "itemName": name.replace("&amp;", "&").strip(),
        "enrollId": enroll,
        "price": price,
        "promoQty": promo_qty,
        "subsidyAmount": subsidy_amt,
        "subsidyQty": subsidy_qty,
        "detailUrl": _detail_url(item),
        "content": content,
    }


# ---------------------------------------------------------------- 与前台价页结合
_END_STATUS = {"offline", "expired", "deleted", "quit", "lose", "onfail"}


def _state_at(history: list, day_end: str) -> tuple[str, float | None]:
    """某天 23:59:59 时该 SKU 的超级补贴状态与当时报名价。

    状态：online=在活动中；paused=替补/暂停；ended=已结束；''=当天还没开始。
    历史按时间升序，字符串时间（YYYY-MM-DD HH:MM:SS）可直接比较。
    """
    state, price = "", None
    for h in history:
        if (h.get("time") or "") > day_end:
            break
        k = h.get("kind") or ""
        if k == "win":
            state = "online"
            price = h.get("price") if h.get("price") is not None else price
        elif k == "substitute":
            state = "paused"
            price = h.get("price") if h.get("price") is not None else price
        elif k in _END_STATUS:
            state = "ended"
    return state, price


def attach_price(rows: list, days: list, account: str) -> dict:
    """把超级补贴信息补到 /api/price 的每一行：当前状态 + 逐日超补价。

    account='all' 时按 SKU 取最近一条消息的店铺（价格页是跨店合并的）。
    返回给页面用的摘要（是否有缓存、抓取时间、在线 SKU 数）。
    """
    keys = ([a["key"] for a in settings.ACCOUNTS]
            if str(account).lower() == "all" else [str(account)])
    merged: dict = {}
    fetched = ""
    for key in keys:
        payload = load_cache(key)
        if not payload:
            continue
        fetched = max(fetched, payload.get("fetchedAt") or "")
        label = payload.get("label") or key
        for rec in payload.get("skus") or []:
            sid = rec.get("skuId") or ""
            if not sid:
                continue
            cur = merged.get(sid)
            if cur is None or (rec.get("lastTime") or "") > (cur[0].get("lastTime") or ""):
                merged[sid] = (rec, label)

    online = sum(1 for rec, _ in merged.values() if rec.get("status") == "win")
    lower = 0
    row_by_sku = {str(r.get("skuId") or ""): r for r in (rows or [])}
    for row in rows or []:
        rec_label = merged.get(str(row.get("skuId") or ""))
        if not rec_label:
            row["subsidyByDay"] = [None] * len(days or [])
            continue
        rec, label = rec_label
        by_day, online_days = [], 0
        for day in days or []:
            state, price = _state_at(rec.get("history") or [], f"{day} 23:59:59")
            val = price if (state == "online" and price is not None) else None
            by_day.append(val)
            if val is not None:
                online_days += 1
        row.update({
            "subsidyStatus": rec.get("status") or "",
            "subsidyStatusLabel": rec.get("statusLabel") or "",
            "subsidyPrice": rec.get("price"),
            "subsidyEnrollId": rec.get("enrollId") or "",
            "subsidyLastTime": rec.get("lastTime") or "",
            "subsidyOnlineDays": online_days,
            "subsidyByDay": by_day,
            "subsidyShop": label,
        })
        lp, sp = row.get("priceLatest"), rec.get("price")
        try:
            if lp is not None and sp:
                if float(lp) < float(sp):
                    lower += 1
        except (TypeError, ValueError):
            pass
    # 报名 SKU 清单：即使商智区间内没有成交（没进价格表）也列出来，
    # 否则这些新报名品在价格页完全不可见，用户没法判断「是没卖还是没抓」。
    order = {"win": 0, "substitute": 1}
    listing = []
    for sid, (rec, label) in merged.items():
        pr = row_by_sku.get(sid) or {}
        listing.append({
            "skuId": sid,
            "itemName": rec.get("itemName") or "",
            "status": rec.get("status") or "",
            "statusLabel": rec.get("statusLabel") or "",
            "price": rec.get("price"),
            "enrollId": rec.get("enrollId") or "",
            "lastTime": rec.get("lastTime") or "",
            "shop": label,
            "inPrice": sid in row_by_sku,
            "priceLatest": pr.get("priceLatest"),
            "priceAvg": pr.get("priceAvg"),
            "qty": pr.get("qty"),
        })
    listing.sort(key=lambda x: x.get("lastTime") or "", reverse=True)
    listing.sort(key=lambda x: order.get(x["status"], 9))
    return {"available": bool(merged), "fetchedAt": fetched,
            "onlineCount": online, "belowCount": lower,
            "skuCount": len(listing),
            "inPriceCount": sum(1 for x in listing if x["inPrice"]),
            "list": listing,
            "accounts": len(keys) if str(account).lower() == "all" else 1}


def summarize(messages: list) -> dict:
    msgs = sorted(messages, key=lambda m: m.get("time") or "")
    skus: dict = {}
    for m in msgs:
        sid = m.get("skuId") or ""
        if not sid:
            continue
        e = skus.get(sid)
        if e is None:
            e = {"skuId": sid, "itemName": "", "status": "", "statusLabel": "",
                 "price": None, "promoQty": None, "enrollId": "",
                 "events": 0, "firstTime": m["time"], "lastTime": m["time"],
                 "lastContent": "", "detailUrl": "", "history": []}
            skus[sid] = e
        e["events"] += 1
        if m.get("itemName"):
            e["itemName"] = m["itemName"]
        if m["kind"] in STATUS_ORDER:
            e["status"] = m["kind"]
            e["statusLabel"] = m["statusLabel"]
            for src, dst in (("price", "price"), ("promoQty", "promoQty"),
                             ("enrollId", "enrollId")):
                if m.get(src) not in (None, ""):
                    e[dst] = m[src]
            e["lastContent"] = m.get("content") or ""
            e["detailUrl"] = m.get("detailUrl") or e["detailUrl"]
        e["lastTime"] = m["time"] or e["lastTime"]
        e["history"].append({k: m.get(k) for k in
                             ("time", "kind", "statusLabel", "price", "promoQty",
                              "enrollId", "content", "detailUrl", "unread")})

    rows = sorted(skus.values(),
                  key=lambda x: (x.get("lastTime") or "", x["skuId"]), reverse=True)
    counts = {k: 0 for k in STATUS_ORDER}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    return {
        "skus": rows,
        "counts": counts,
        "skuCount": len(rows),
        "messageCount": len(msgs),
        "unread": sum(1 for m in msgs if m.get("unread")),
        "messages": sorted(msgs, key=lambda m: m.get("time") or "", reverse=True),
    }


# ---------------------------------------------------------------- 抓取
_JS_REPLAY = """
async ({url, body, headers}) => {
  try {
    const r = await fetch(url, {method: 'POST', credentials: 'include',
      headers: headers, body: body});
    return {status: r.status, text: await r.text()};
  } catch (e) { return {status: -1, text: String(e)}; }
}
"""

_HDR_SKIP = ("content-length", "host", "connection", "accept-encoding")


def _wait_capture(captured: list, timeout: float = 12.0) -> dict | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for c in captured:
            if str(MSG_CODE) in (c.get("post") or ""):
                return c
        time.sleep(0.3)
    return None


def _fetch_items(page) -> tuple[list, int]:
    """打开消息抽屉，截获 262 分类真实请求后放大 pageSize 重放。"""
    captured: list = []

    def on_request(req):
        if "getMsgListForPage" in req.url and req.post_data:
            captured.append({"url": req.url, "headers": dict(req.headers),
                             "post": req.post_data})

    page.on("request", on_request)
    page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(6000)
    page.locator("text=消息").first.click()
    page.wait_for_timeout(2500)
    try:
        page.locator('.drawer__tabs-item-name:has-text("店铺")').first.click(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(2500)
    if _wait_capture(captured, 3) is None:
        try:
            page.locator('.category__second-item[title="超级补贴"]').first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
    seen = _wait_capture(captured, 12)
    if not seen:
        raise RuntimeError("没有捕获到超级补贴消息接口请求（登录态或页面结构可能变了）")

    headers = {k: v for k, v in seen["headers"].items() if k.lower() not in _HDR_SKIP}
    base = json.loads(seen["post"])
    base.setdefault("param", {}).update({
        "belongType": "32", "categoryNew": "shop", "msgId": "-1",
        "previous": False, "noRead": "0",
        "msgTypeInfoList": [{"msgCat": MSG_CAT, "msgCode": str(MSG_CODE)}],
        "appId": "shop", "platform": "web",
    })

    items: list = []
    total = 0
    page_no = 1
    while page_no <= 10:
        body = json.loads(json.dumps(base))
        body["param"].update({"page": page_no, "pageSize": 100})
        res = page.evaluate(_JS_REPLAY, {"url": seen["url"],
                                         "body": json.dumps(body),
                                         "headers": headers})
        if res.get("status") != 200:
            raise RuntimeError(f"消息接口 HTTP {res.get('status')}: "
                               f"{str(res.get('text'))[:160]}")
        got = json.loads(res.get("text") or "{}")
        if got.get("code") != 200:
            raise RuntimeError(f"消息接口返回异常：{str(got)[:200]}")
        data = got.get("data") or {}
        total = int(data.get("total") or 0)
        chunk = data.get("messageInfoDataList") or []
        items.extend(chunk)
        if not chunk or len(items) >= total:
            break
        page_no += 1
    return items, total


def run(account: str = "", log=None, wait: float = 90.0) -> dict:
    """抓一次超级补贴消息并落缓存（内部自己占抓取锁）。"""
    from . import scrape_day
    from .browser import launch_persistent

    acc = account or settings.MAIN_ACCOUNT
    if acc not in [a["key"] for a in settings.ACCOUNTS]:
        raise ValueError(f"未知账号：{acc}")
    if log:
        log("等待浏览器登录态空闲…")
    if not scrape_day.acquire(timeout=wait, name="subsidy"):
        raise RuntimeError("系统正在执行其它抓取任务，等它结束后再点「重新抓取」")
    pw = ctx = None
    try:
        if log:
            log("打开京麦消息中心…")
        pw, ctx = launch_persistent(headless=True, account=acc)
        page = ctx.new_page()
        items, total = _fetch_items(page)
        messages = [parse_message(it) for it in items]
        payload = {
            "account": acc,
            "label": settings.get_account(acc).get("label") or acc,
            "fetchedAt": settings.now_iso(),
            "total": total,
            **summarize(messages),
        }
        if log:
            log(f"解析完成：{payload['messageCount']} 条消息 / "
                f"{payload['skuCount']} 个 SKU")
        _save(acc, payload)
        return payload
    finally:
        try:
            if ctx is not None:
                ctx.close()
        finally:
            try:
                if pw is not None:
                    pw.stop()
            finally:
                scrape_day.release()
