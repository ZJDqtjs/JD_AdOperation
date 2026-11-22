# -*- coding: utf-8 -*-
"""SKU 商品页探寻：降价商品的真实原因（活动标识 + 前台展示价）。

为什么需要这层
--------------
「今日实时」的前台价来自商智成交均价（成交金额 ÷ 成交件数），它能看出「降了」，
但看不出「为什么降」。同一个降价，可能是京东秒杀、百亿补贴、便宜包邮、满减券，
也可能只是商家直接改价——这直接决定广告要不要跟、跟多少。

做法
----
用现有登录态 Profile 打开 ``https://item.jd.com/<sku>.html``，等页面把价格 /
活动渲染出来后，把**页面展示价**和**秒杀标识**带回来（其它活动标识不展示），
按天落到 ``<DATA_DIR>/price/probe_<日期>.json``。价格页「今日实时」一旦取到
商品页价，就用它显示今日前台价，不再显示商智成交均价，并只展示秒杀图标。

只读前台公开页面；每个 SKU 打开一次，结果当天复用（15 分钟内不重复抓），
回看历史也不重复请求。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import dailystore as ds, settings

PROBE_SUBDIR = "price"
# 一次最多探寻多少个 SKU：每个要开一次商品页，太多会把浏览器占太久
PROBE_MAX = 40
# 同一 SKU 在这个时间内重复请求直接复用上次结果（手动「重新探寻」可绕过）
PROBE_FRESH_SECONDS = 15 * 60

_FILE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_RUN: dict = {"active": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
              "current": "", "day": "", "message": "",
              "startedAt": None, "finishedAt": None, "error": None}


# ---------------------------------------------------------------- 页面提取
# 京东 2026 版商品页是 React 壳，价格/活动都由接口回来后再渲染，静态 HTML 里没有。
# 这里在真实渲染后的 DOM 上取三样东西：
#   1. 主价格（多个已知选择器，最后兜底取摘要区第一个 ¥ 数字）
#   2. 右侧商品信息区的文本（活动横幅、促销说明都在这里）
#   3. 促销小标签的短文本（秒杀、百补、券、PLUS 等往往是小标签）
_JS_EXTRACT = r"""
() => {
  const norm = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const txt = el => el ? norm(el.innerText || el.textContent || '') : '';
  const one = sels => {
    for (const s of sels) {
      let el = null;
      try { el = document.querySelector(s); } catch (e) { el = null; }
      const t = txt(el);
      if (t) return {sel: s, text: t};
    }
    return {sel: '', text: ''};
  };
  const priceSel = one([
    '.product-price--main', '.product-price-panel', '.price', '.p-price .price',
    '.summary-price .price', '.price-now', '[class*="price-now"]',
    '[class*="priceValue"]', '#jd-price', '.product-price', '#price'
  ]);
  const nameSel = one(['.sku-name', '.sku-title', '.product-name', '.product-title', 'h1']);
  // 京东 2026 商品页的商品信息在右侧 #page-right-information；老版/兜底再退回常见容器
  const scopeEl = document.querySelector('#page-right-information')
    || document.querySelector('.page-right-information')
    || document.querySelector('.page-right-wrap')
    || document.querySelector('#itemInfo')
    || document.querySelector('.itemInfo-wrap')
    || document.querySelector('.summary')
    || document.querySelector('.product-right')
    || document.querySelector('.product-intro')
    || document.body;
  const scopeText = norm(scopeEl.innerText).slice(0, 6000);

  const labels = [], seen = {};
  let nodes = [];
  try {
    nodes = scopeEl.querySelectorAll(
      '[class*="activity"],[class*="tag"],[class*="label"],[class*="badge"],' +
      '[class*="promo"],[class*="coupon"],[class*="discount"]');
  } catch (e) { nodes = []; }
  for (const el of nodes) {
    const t = txt(el);
    if (!t || t.length > 24 || seen[t]) continue;
    seen[t] = 1;
    labels.push(t);
    if (labels.length >= 40) break;
  }
  // 活动往往只放在图片 alt / title / aria-label 里（例如「百亿补贴」角标）
  const KW = /秒杀|补贴|包邮|满\s*\d*\s*减|券|PLUS|plus|新人|企业|限时|折/;
  try {
    for (const el of scopeEl.querySelectorAll('img[alt],[title],[aria-label]')) {
      const t = norm(el.getAttribute('alt') || el.getAttribute('title')
                     || el.getAttribute('aria-label'));
      if (!t || t.length > 24 || seen[t] || !KW.test(t)) continue;
      seen[t] = 1;
      labels.push(t);
      if (labels.length >= 60) break;
    }
  } catch (e) {}
  return {
    url: location.href,
    title: document.title,
    name: nameSel.text,
    priceText: priceSel.text,
    priceSel: priceSel.sel,
    scopeText: scopeText,
    labels: labels
  };
}
"""


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_price(text: str):
    """从元素文本里取第一个价格：'¥15.8 秒杀 满200减20' → 15.8。"""
    text = text or ""
    for pat in (r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*元"):
        m = re.search(pat, text)
        if m:
            return _f(m.group(1).replace(",", ""))
    m = re.fullmatch(r"\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*", text)
    return _f(m.group(1).replace(",", "")) if m else None


# 只保留「秒杀」图标：页面上的百补 / 包邮 / 券等其它活动不展示
def _badges(hay: str) -> list:
    if "京东秒杀" in hay:
        return [{"key": "seckill", "label": "秒杀", "text": "京东秒杀"}]
    if "秒杀" in hay:
        return [{"key": "seckill", "label": "秒杀", "text": "秒杀"}]
    return []


def _seckill_from_biz(texts: list) -> dict:
    """从 pc_detailpage_wareBusiness 响应里取秒杀标记与价格。"""
    info: dict = {"on": False}
    for raw in texts or []:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        ms = data.get("miaoshaInfo") or {}
        if ms.get("miaosha"):
            info.update({"on": True, "price": _f(ms.get("promoPrice")),
                         "originPrice": _f(ms.get("originPrice")),
                         "endTime": ms.get("endTime"),
                         "remainSeconds": ms.get("miaoshaRemainTime")})
        gather = data.get("warePriceGatherVO") or {}
        for p in gather.get("priceItemList") or []:
            if p.get("priceType") == "finalPrice":
                info["finalPrice"] = _f(p.get("price"))
            elif p.get("priceType") == "seckillPrice":
                info["seckillPrice"] = _f(p.get("price"))
        if info.get("on"):
            break
    return info


def _probe_one(page, sku: str) -> dict:
    """打开一个商品页，取展示价 + 活动标识。失败也返回一条带 error 的记录。"""
    sku = str(sku).strip()
    url = f"https://item.jd.com/{sku}.html"
    rec = {"skuId": sku, "at": settings.now_iso(), "url": url, "finalUrl": "",
           "title": "", "name": "", "price": None, "priceText": "",
           "badges": [], "labels": [], "reason": "", "error": "",
           "seckill": {"on": False}}
    # 价格/活动在 pc_detailpage_wareBusiness 里，其中 miaoshaInfo 才是秒杀权威标记：
    # 2026 版商品页的「京东秒杀」是个 logo 图片，innerText 里根本没有「秒杀」两个字，
    # 只靠页面文案会把秒杀误判成「包邮/满减/限时」（实测 100087667252 天麻）。
    biz: list = []

    def _on_resp(resp):
        if "pc_detailpage_wareBusiness" in resp.url:
            try:
                biz.append(resp.text())
            except Exception:  # noqa: BLE001
                pass

    page.on("response", _on_resp)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # 价格是接口回来后才渲染的：等页面顶部出现 ¥ 数字（不依赖某个 class），
        # 再留一点时间给活动标签渲染
        try:
            page.wait_for_function(
                "() => /[¥￥]\\s*\\d/.test(((document.body && document.body.innerText) "
                "|| '').slice(0, 4000))", timeout=7000)
        except Exception:  # noqa: BLE001  等不到也继续，后面兜底取文本
            pass
        page.wait_for_timeout(1800)
        raw = page.evaluate(_JS_EXTRACT) or {}
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return rec
    finally:
        try:
            page.remove_listener("response", _on_resp)
        except Exception:  # noqa: BLE001
            pass

    rec["finalUrl"] = raw.get("url") or page.url
    rec["title"] = (raw.get("title") or "").strip()
    rec["name"] = (raw.get("name") or "").strip()[:200]
    rec["labels"] = (raw.get("labels") or [])[:60]
    scope = raw.get("scopeText") or ""
    hay = " ".join([raw.get("priceText") or "", scope, " ".join(rec["labels"])])

    # 秒杀：以接口的 miaoshaInfo 为准；接口没回来时用「距离结束」倒计时兜底
    ms = _seckill_from_biz(biz)
    if not ms.get("on") and any("距离结束" in x or "距结束" in x for x in rec["labels"]):
        ms = {"on": True}
    rec["seckill"] = ms
    if ms.get("on"):
        rec["badges"] = [{"key": "seckill", "label": "秒杀", "text": "京东秒杀"}]
        rec["reason"] = "秒杀"
    else:
        rec["badges"] = _badges(hay)
        rec["reason"] = "、".join(b["label"] for b in rec["badges"])

    if "passport.jd.com" in rec["finalUrl"]:
        rec["error"] = "商品页跳转到登录页（登录态可能已失效）"
        return rec

    price = _parse_price(raw.get("priceText") or "")
    if price is None:
        price = _parse_price(scope)
    if price is None:
        rec["error"] = "未取到商品页价格（可能已下柜或触发验证）"
        return rec
    rec["price"] = round(price, 2)
    rec["priceText"] = (raw.get("priceText") or "")[:120]
    if not rec["name"]:
        rec["name"] = rec["title"][:200]
    return rec


def _block_heavy(route):
    """图片/字体/视频不加载：商品页首屏更快，也不影响价格与活动文本。"""
    try:
        if route.request.resource_type in ("image", "media", "font"):
            route.abort()
        else:
            route.continue_()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 落盘
def _dir() -> Path:
    d = settings.DATA_DIR / PROBE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_path(day: str) -> Path:
    return _dir() / f"probe_{ds.norm_day(day)}.json"


def load_day(day: str | None = None) -> dict:
    day = ds.norm_day(day or ds.today())
    fp = probe_path(day)
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"day": day, "rows": {}}
    if not isinstance(data, dict):
        return {"day": day, "rows": {}}
    data.setdefault("day", day)
    data.setdefault("rows", {})
    return data


def _save_record(rec: dict, day: str | None = None) -> None:
    day = ds.norm_day(day or ds.today())
    with _FILE_LOCK:
        data = load_day(day)
        data["rows"][str(rec.get("skuId"))] = rec
        data["updatedAt"] = settings.now_iso()
        tmp = probe_path(day).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(probe_path(day))


def _fresh(rec) -> bool:
    if not rec or rec.get("error"):
        return False
    try:
        ts = datetime.fromisoformat(rec.get("at") or "")
        now = settings.now().replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return (now - ts).total_seconds() <= PROBE_FRESH_SECONDS


def attach(rows: list, day: str | None = None,
           only_day_field: str | None = None) -> dict:
    """把当天的探寻结果补进价格行（商品页价 / 活动标识 / 原因）。

    only_day_field: 主价格表里只给「最新价就是探寻日」的行补，避免把今天的
    商品页价贴到几天前的价格上。
    """
    day = ds.norm_day(day or ds.today())
    data = load_day(day)
    by = data.get("rows") or {}
    n = 0
    for r in rows or []:
        if only_day_field and str(r.get(only_day_field) or "") != day:
            continue
        rec = by.get(str(r.get("skuId") or ""))
        if not rec:
            continue
        r["probeAt"] = rec.get("at") or ""
        r["probePrice"] = rec.get("price")
        r["probePriceText"] = rec.get("priceText") or ""
        # 历史记录里可能存了旧版的多活动标识：这里统一只保留秒杀
        only = [b for b in (rec.get("badges") or []) if b.get("key") == "seckill"]
        r["probeBadges"] = only
        r["probeReason"] = "、".join(b.get("label") or "" for b in only)
        r["probeSeckill"] = rec.get("seckill") or {"on": False}
        r["probeError"] = rec.get("error") or ""
        r["probeName"] = rec.get("name") or ""
        r["probeUrl"] = rec.get("finalUrl") or rec.get("url") or ""
        n += 1
    return {"available": bool(by), "day": day, "count": n,
            "updatedAt": data.get("updatedAt") or ""}


# ---------------------------------------------------------------- 后台任务
def status() -> dict:
    with _RUN_LOCK:
        return dict(_RUN)


def _say(msg: str) -> None:
    with _RUN_LOCK:
        _RUN["message"] = msg
    print(f"[itemprobe] {msg}", flush=True)


def _work(skus: list, day: str, account: str, log=None) -> None:
    from . import scrape_day
    from .browser import launch_persistent

    say = log or _say
    got_lock = False
    pw = ctx = None
    try:
        if not scrape_day.acquire(timeout=2, name="itemprobe"):
            raise RuntimeError("正在执行其它抓取任务，等它结束后再探寻")
        got_lock = True
        say(f"打开浏览器，准备探寻 {len(skus)} 个商品页…")
        pw, ctx = launch_persistent(headless=True, account=account)
        page = ctx.new_page()
        page.route("**/*", _block_heavy)
        for i, sku in enumerate(skus):
            with _RUN_LOCK:
                _RUN.update({"current": sku, "done": i})
            say(f"商品页探寻 {sku}（{i + 1}/{len(skus)}）…")
            rec = _probe_one(page, sku)
            _save_record(rec, day)
            with _RUN_LOCK:
                if rec.get("error"):
                    _RUN["fail"] += 1
                else:
                    _RUN["ok"] += 1
                _RUN["done"] = i + 1
            if rec.get("error"):
                say(f"{sku} 探寻失败：{rec['error']}")
            else:
                say(f"{sku} 商品页 ¥{rec['price']:.2f}"
                    f"（{rec.get('reason') or '无活动标识'}）")
            time.sleep(0.6)
        say(f"探寻完成：成功 {_RUN['ok']} / 失败 {_RUN['fail']}")
    except Exception as exc:  # noqa: BLE001
        with _RUN_LOCK:
            _RUN["error"] = f"{type(exc).__name__}: {exc}"[:300]
        say(f"探寻任务失败：{type(exc).__name__}: {exc}")
    finally:
        try:
            if ctx is not None:
                ctx.close()
        finally:
            try:
                if pw is not None:
                    pw.stop()
            finally:
                if got_lock:
                    scrape_day.release()
                with _RUN_LOCK:
                    _RUN.update({"active": False, "current": "",
                                 "finishedAt": settings.now_iso()})


def start(skus, day: str | None = None, account: str | None = None,
          force: bool = False, log=None) -> dict:
    """起一个后台探寻任务。force=False 时跳过 15 分钟内刚抓过的 SKU。"""
    day = ds.norm_day(day or ds.today())
    ids: list = []
    for s in skus or []:
        s = str(s).strip()
        if s.isdigit() and s not in ids:
            ids.append(s)
    if not ids:
        return {"ok": False, "error": "没有可探寻的 SKU"}
    if not force:
        have = load_day(day).get("rows") or {}
        ids = [s for s in ids if not _fresh(have.get(s))]
    if not ids:
        return {"ok": True, "total": 0, "skipped": True,
                "message": "这些 SKU 刚探寻过（15 分钟内），无需重复"}
    ids = ids[:PROBE_MAX]
    with _RUN_LOCK:
        if _RUN.get("active"):
            return {"ok": False, "error": "已有探寻任务在运行，请等它结束"}
        _RUN.update({"active": True, "total": len(ids), "done": 0, "ok": 0,
                     "fail": 0, "current": "", "day": day, "message": "",
                     "startedAt": settings.now_iso(), "finishedAt": None,
                     "error": None})
    acc = account if account and str(account).lower() != "all" else settings.MAIN_ACCOUNT
    th = threading.Thread(target=_work, args=(ids, day, acc), kwargs={"log": log},
                          name="itemprobe", daemon=True)
    th.start()
    return {"ok": True, "total": len(ids), "day": day, "skus": ids}
