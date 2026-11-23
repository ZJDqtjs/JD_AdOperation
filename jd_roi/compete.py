# -*- coding: utf-8 -*-
"""SKU 竞品与关键词分析（前台价页右侧面板的数据层）。

数据来源
--------
* 京东搜索前台（用现有登录态）：同款在售商品、价格、是否投广告
* 京准通「流量解析」：行业关键词的展现指数 / 点击指数 / CTR / CVR / 平均出价
* 京准通「商品推词」：热搜词 / 潜力词 / 竞品词 / 竞品争夺词
* 本地按天库：jst_sku（SKU 类目）、jst_searchword（本店近 30 天搜索词）

口径
----
* 京东只暴露公开前台数据，**看不到竞品后台的京准通计划/预算/出价**；
  这里给的是「同款在售 SKU + 行业词效率 + 京东模型推词」，用于推断竞品打法。
* 行业侧的展现/点击是**指数**，不是真实次数，只能比 CTR/CVR/出价。
* 结果按 SKU 缓存到 settings.REPORT_DIR/compete_<sku>.json，默认 12 小时新鲜。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import json
import re
import time
import urllib.parse
from pathlib import Path

from . import settings

# 推词类型（京东 adKeywordTypes 位值，实测）
KW_TYPES = {"热搜词": 4, "潜力词": 64, "竞品词": 8192, "竞品争夺词": 16384}

CACHE_TTL = 12 * 3600          # 秒；12 小时后允许重新抓取
CACHE_VERSION = 1

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/msa/','language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""

_JS_CARDS = """els => els.map(e => {
  const t = e.querySelector('[title]');
  return {sku: e.getAttribute('data-sku'),
          title: t ? t.getAttribute('title') : '',
          text: (e.innerText || '').replace(/\\n+/g, '|').slice(0, 320)};
})"""


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


def _account_for(account: str | None) -> str:
    keys = [a["key"] for a in settings.ad_accounts()]
    if account and account in keys:
        return account
    return settings.MAIN_ACCOUNT


def cache_path(sku, account: str | None = None) -> Path:
    suffix = f"_{account}" if account else ""
    return Path(settings.REPORT_DIR) / f"compete_{sku}{suffix}.json"


def load_cache(sku, account: str | None = None,
               max_age: float | None = CACHE_TTL) -> dict | None:
    """读缓存；max_age=None 表示不校验新鲜度。"""
    fp = cache_path(sku, account)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("version") != CACHE_VERSION:
        return None
    if max_age is not None:
        try:
            age = time.time() - fp.stat().st_mtime
        except OSError:
            return None
        if age > max_age:
            return None
    return data


def _save(sku, account: str, data: dict) -> dict:
    fp = cache_path(sku, account)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ---------------------------------------------------------------- 类目
def _resolve_cid(sku, account: str) -> tuple[int, str]:
    """SKU 的三级类目：先查本地 jst_sku，再退回该账号行业大盘缓存。"""
    sku_s = str(sku)
    root = Path(settings.DAILY_DIR) / account
    try:
        days = sorted([p.name for p in root.iterdir() if p.is_dir()], reverse=True)[:10]
    except OSError:
        days = []
    for day in days:
        fp = root / day / "jst_sku.json.gz"
        if not fp.exists():
            continue
        try:
            raw = json.load(gzip.open(fp, "rt", encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows = raw if isinstance(raw, list) else (raw.get("rows") or raw.get("data") or [])
        for row in rows:
            if str(row.get("skuId")) == sku_s and row.get("skuCid3"):
                return int(row["skuCid3"]), day
    # 行业缓存兜底
    try:
        files = sorted((Path(settings.REPORT_DIR)).glob(f"industry_{account}_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for fp in files[:1]:
            ind = json.loads(fp.read_text(encoding="utf-8"))
            cid = (ind.get("category") or {}).get("id")
            if cid:
                return int(cid), "industry-cache"
    except Exception:  # noqa: BLE001
        pass
    return 0, ""


# ---------------------------------------------------------------- 浏览器
@contextlib.contextmanager
def _session(account: str, wait: float = 45.0, log=None):
    from . import scrape_day
    from .browser import launch_persistent

    if log:
        log("等待浏览器登录态空闲…")
    if not scrape_day.acquire(timeout=wait, name="compete"):
        raise RuntimeError("系统正在执行抓取任务（补齐历史数据），等它结束后再点「重新抓取」")
    pw = ctx = None
    try:
        pw, ctx = launch_persistent(headless=True, account=account)
        page = ctx.new_page()
        # 必须落在 jd.com 域名下再发 fetch：about:blank 跨域会被浏览器拦掉
        page.goto("https://jzt.jd.com/msa/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        yield page
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


def _post(page, url: str, payload: dict) -> dict:
    res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
    if res.get("status") != 200:
        raise RuntimeError(f"接口 HTTP {res.get('status')}: {str(res.get('text'))[:160]}")
    data = json.loads(res.get("text") or "{}")
    if data.get("code") not in (1, None) or data.get("success") is False:
        raise RuntimeError(f"接口返回异常：{str(data)[:200]}")
    return data


# ---------------------------------------------------------------- 前台搜索
def _price_of(text: str) -> float | None:
    m = re.search(r"¥\|(\d+)\|\.\|(\d+)", text)
    if m:
        return float(m.group(1) + "." + m.group(2))
    m = re.search(r"¥\s*(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _shop_of(text: str) -> str:
    parts = [x.strip() for x in text.split("|") if x.strip()]
    for seg in reversed(parts):
        if any(k in seg for k in ("旗舰店", "专营店", "专卖店", "自营专区", "官方店", "店")):
            return seg
    return ""


def _sales_of(text: str) -> str:
    m = re.search(r"已售([\d.]+[万千]?\+?)", text)
    return m.group(1) if m else ""


def _search_cards(page, keyword: str, pages: int = 1) -> list[dict]:
    out: dict[str, dict] = {}
    for idx in range(max(1, min(pages, 2))):
        s = 1 + idx * 60
        url = ("https://search.jd.com/Search?keyword=%s&enc=utf-8&s=%d"
               % (urllib.parse.quote(keyword), s))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4800)
        try:
            page.mouse.wheel(0, 2600)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(2200)
        cards = page.eval_on_selector_all("[data-sku]", _JS_CARDS)
        for card in cards:
            sku = str(card.get("sku") or "")
            title = (card.get("title") or "").strip()
            if not sku or not title or sku in out:
                continue
            text = card.get("text") or ""
            out[sku] = {"sku": sku, "title": title, "price": _price_of(text),
                        "shop": _shop_of(text), "sales": _sales_of(text),
                        "ad": "广告" in text}
        time.sleep(1.2)
    return list(out.values())


def _spec_grams(title: str) -> int | None:
    """从标题里取规格重量（克）：2斤→1000、500g→500、1kg→1000。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|千克|g|克|斤)", title or "", re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("kg", "千克"):
        n *= 1000
    elif unit == "斤":
        n *= 500
    return int(round(n))


def _pick_competitors(cards: list[dict], keyword: str, own_sku: str,
                      own_title: str = "") -> list[dict]:
    token = keyword[:2]
    rows = [x for x in cards if x.get("price")]
    keep = [x for x in rows
            if ("拇" in x["title"] or token in x["title"]) and x["sku"] != str(own_sku)]
    if len(keep) < 3:
        keep = [x for x in rows if x["sku"] != str(own_sku)]
    own_g = _spec_grams(own_title)
    for x in keep:
        x["specG"] = _spec_grams(x["title"])
    # 同规格优先、正在投广告的优先，再按价格
    keep.sort(key=lambda x: (-(2 if own_g and x.get("specG") == own_g else 0)
                             - (2 if x.get("ad") else 0), x.get("price") or 0))
    return keep[:24]


def _price_band(items: list[dict], own_price: float | None,
                own_spec: int | None = None) -> dict:
    same = [x for x in items if own_spec and x.get("specG") == own_spec]
    pool = same if len(same) >= 3 else items
    prices = sorted([x["price"] for x in pool if x.get("price")])
    if not prices:
        return {"count": 0}
    mid = prices[len(prices) // 2]
    rank = None
    if own_price:
        rank = 1 + sum(1 for p in prices if p < own_price)
    return {"count": len(prices), "min": prices[0], "median": mid,
            "max": prices[-1], "ownRank": rank,
            "adCount": sum(1 for x in pool if x.get("ad")),
            "basis": "同规格" if pool is same else "全部同款"}


# ---------------------------------------------------------------- 关键词
def _related_words(page, sku) -> list[dict]:
    try:
        d = _post(page, "https://jzt-api.jd.com/dataCenter/industry/v2/keyword/analysis/chart",
                  {"skuId": int(sku)})
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in d.get("data") or []:
        w = str(r.get("name") or "").strip()
        if len(w) < 2 or w.isdigit():
            continue
        out.append({"word": w, "score": _r(r.get("value"), 4)})
    return out[:16]


def _trend(page, word: str, cid: int) -> dict | None:
    today = settings.today()
    start = (today - dt.timedelta(days=6)).isoformat()
    payload = {"cid3": [cid] if cid else [], "clickOrOrderDay": 15,
               "clickOrOrderCaliber": 0, "isGift": 0, "orderStatusCategory": 1,
               "impressionOrClickCaliber": 0, "startDate": start,
               "endDate": today.isoformat(), "analyzeType": 2,
               "campaignTypes": [], "keyword": word, "wordType": "searchWord"}
    try:
        d = _post(page, "https://jzt-api.jd.com/dataCenter/industry/v2/keyword/analysis/trend",
                  payload)
    except Exception:  # noqa: BLE001
        return None
    rows = ((d.get("data") or {}).get("rows") or [])
    if not rows:
        return None
    imp = sum(_f(r.get("impressionsExp")) for r in rows)
    clk = sum(_f(r.get("clicksExp")) for r in rows)
    return {"word": word, "imp": int(imp), "clicks": int(clk),
            "ctr": _r(clk / imp * 100) if imp else None,
            "cvr": _r(sum(_f(r.get("cvs")) for r in rows) / len(rows)),
            "bid": _r(sum(_f(r.get("kwImpressionBid")) for r in rows) / len(rows)),
            "days": len(rows)}


def _recs(page, sku, type_name: str, limit: int = 8) -> list[dict]:
    code = KW_TYPES.get(type_name)
    if not code:
        return []
    try:
        d = _post(page, "https://jzt-api.jd.com/dspad/keyword/sku/recommend",
                  {"adKeywordTypes": [code], "skuId": int(sku), "devType": 2,
                   "adGroupId": 0, "competitorSkus": [int(sku)], "campaignType": 2,
                   "requestFrom": 0})
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in d.get("data") or []:
        w = str(r.get("keyWord") or "").strip()
        if not w:
            continue
        out.append({"word": w, "pv": int(_f(r.get("pv"))),
                    "ctr": _r(r.get("ctr")), "cvr": _r(r.get("cvr")),
                    "bid": _r(r.get("suggestedBidPrice"))})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 本店搜索词
def _own_terms(account: str, token: str, limit: int = 8) -> list[dict]:
    root = Path(settings.DAILY_DIR) / account
    try:
        days = sorted([p.name for p in root.iterdir() if p.is_dir()])[-30:]
    except OSError:
        return []
    agg: dict[str, dict] = {}
    for day in days:
        fp = root / day / "jst_searchword.json.gz"
        if not fp.exists():
            continue
        try:
            raw = json.load(gzip.open(fp, "rt", encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows = raw if isinstance(raw, list) else (raw.get("rows") or raw.get("data") or [])
        for r in rows:
            term = str(r.get("searchTerm") or "").strip()
            camp = str(r.get("campaignName") or "")
            if not term:
                continue
            if token not in term and token not in camp:
                continue
            a = agg.setdefault(term, {"word": term, "cost": 0.0, "clicks": 0,
                                      "orders": 0, "gmv": 0.0})
            a["cost"] += _f(r.get("cost"))
            a["clicks"] += int(_f(r.get("clicks")))
            a["orders"] += int(_f(r.get("totalOrderCnt")))
            a["gmv"] += _f(r.get("totalOrderSum"))
    rows = sorted(agg.values(), key=lambda x: -x["cost"])[:limit]
    for x in rows:
        x["cost"] = _r(x["cost"])
        x["gmv"] = _r(x["gmv"])
        x["roi"] = _r(x["gmv"] / x["cost"]) if x["cost"] else None
    return rows


# ---------------------------------------------------------------- 主流程
def _pick_keyword(title: str, related: list[dict], given: str | None) -> str:
    if given:
        return given
    name = re.sub(r"\s+", "", title or "")
    # 标题里的复合品类：拇指（小）玉米 / 糯玉米 / 甜玉米 等，优先收敛到稳定的搜索词
    if "拇指" in name and "玉米" in name:
        core = "拇指玉米"
    else:
        all_segs = [s for s in re.split(r"[\s/,，、|【】()（）]+", title or "") if s]
        segs = [s for s in all_segs if any(k in s for k in
                ("玉米", "薯", "豆", "菜", "瓜", "果", "肉", "鱼", "虾", "米", "面"))]
        if not segs:
            segs = all_segs
        core = max(segs, key=len) if segs else ""
        core = re.sub(r"(京鲜生|京鲜|精选|甄选|净重|源头直发|京东自营|约|装|袋|盒|包"
                      r"|[0-9.]+(?:g|kg|斤|克|千克)?)", "", core)
    core = core[:12]
    words = [x["word"] for x in related if x.get("word")]
    if core and core in words:
        return core
    # 相关词里优先取「刚好包住 core」的最短词（避免收敛到 拇指玉米婴幼儿 这种长尾）
    covering = [w for w in words if core and core in w]
    if covering:
        return min(covering, key=len)
    inside = [w for w in words if w in core]
    if inside:
        return max(inside, key=len)
    return core or (words[0] if words else "")


def collect(sku, account: str | None = None, keyword: str | None = None,
            name: str | None = None, price=None, log=None) -> dict:
    """抓一次并落缓存。阻塞调用（30~60 秒），必须放后台线程。"""
    sku = str(sku)
    acc = _account_for(account)
    say = log or (lambda _m: None)

    cid, cid_src = _resolve_cid(sku, acc)
    say(f"类目 {'/'.join(filter(None, [str(cid) if cid else '', cid_src]))}，打开浏览器…")

    with _session(acc, log=say) as page:
        say("读取京东相关词…")
        related = _related_words(page, sku)
        kw = _pick_keyword(name or "", related, keyword)
        say(f"按「{kw}」抓取前台同款…")
        cards = _search_cards(page, kw, pages=1)
        competitors = _pick_competitors(cards, kw, sku, own_title=name or "")

        say("读取行业关键词效率…")
        words = [kw] + [x["word"] for x in related
                        if x["word"] != kw and kw[:2] in x["word"]][:3]
        trends = []
        for w in words[:4]:
            t = _trend(page, w, cid)
            if t:
                trends.append(t)
            time.sleep(0.8)

        say("读取京东推词（热搜/潜力/竞品/争夺）…")
        recs = {}
        for tname in KW_TYPES:
            recs[tname] = _recs(page, sku, tname)
            time.sleep(0.5)

    say("读取本店近 30 天搜索词…")
    own = _own_terms(acc, kw[:2])
    band = _price_band(competitors, _f(price) if price not in (None, "") else None,
                       _spec_grams(name or ""))
    # 面板默认展示前 8 个，但缓存保留 24 个，价格带用全量算
    data = {
        "version": CACHE_VERSION,
        "sku": sku,
        "name": name or "",
        "price": _r(price) if price not in (None, "") else None,
        "account": acc,
        "accountLabel": (settings.get_account(acc) or {}).get("label") or acc,
        "cid": cid,
        "keyword": kw,
        "related": related,
        "competitors": competitors,
        "competitorShown": competitors[:8],
        "priceBand": band,
        "trends": trends,
        "recs": recs,
        "ownTerms": own,
        "fetchedAt": settings.now_iso(),
        "caliber": ("同款=京东搜索前台按核心词抓取的在售 SKU；价格/是否广告为前台可见值，"
                    "销量是卡片口径（常为店铺累计），只能横向比。行业展现/点击为指数，"
                    "不可当真实次数；竞品后台计划/预算/出价不可见。"),
    }
    say("完成，写入缓存")
    return _save(sku, acc, data)


# ---------------------------------------------------------------- AI
def ai_messages(data: dict) -> list[dict]:
    band = data.get("priceBand") or {}
    payload = {
        "SKU": data.get("sku"),
        "商品": data.get("name") or "",
        "当前前台价": data.get("price"),
        "核心词": data.get("keyword"),
        "价格带": {"同款数": band.get("count"), "最低价": band.get("min"),
                   "中位价": band.get("median"), "最高价": band.get("max"),
                   "本店价格排第几低": band.get("ownRank"),
                   "在投广告的同款数": band.get("adCount"), "统计口径": band.get("basis")},
        "同款竞品": [{"商品": x.get("title"), "价格": x.get("price"),
                      "是否广告": bool(x.get("ad")), "店铺": x.get("shop"),
                      "销量": x.get("sales")}
                     for x in (data.get("competitors") or [])[:8]],
        "行业关键词": [{"词": x.get("word"), "展现指数": x.get("imp"),
                        "点击指数": x.get("clicks"), "CTR": x.get("ctr"),
                        "CVR": x.get("cvr"), "平均出价": x.get("bid")}
                       for x in (data.get("trends") or [])],
        "京东推词": {k: [{"词": x.get("word"), "搜索量": x.get("pv"),
                         "CTR": x.get("ctr"), "CVR": x.get("cvr"),
                         "建议出价": x.get("bid")}
                        for x in (v or [])[:6]]
                     for k, v in (data.get("recs") or {}).items()},
        "本店近30天搜索词": [{"词": x.get("word"), "花费": x.get("cost"),
                             "点击": x.get("clicks"), "订单": x.get("orders"),
                             "成交额": x.get("gmv"), "ROI": x.get("roi")}
                            for x in (data.get("ownTerms") or [])],
    }
    system = (
        "你是京东生鲜（蔬菜/根茎类）的广告投放操盘手，熟悉京准通快车与商品智能投放。"
        "只能根据给定 JSON 分析：**提到任何数字，都必须是 JSON 里出现过的字段**；"
        "没有给出的指标（如 CPC、客单价、精确销量、竞品出价）一律写「数据不足」，严禁自己推算或编造。"
        "竞品后台的投放计划/预算/出价看不到，不要假装能看到。行业展现/点击是指数，不能当真实次数；"
        "竞品词里 CTR/CVR 为 0 表示平台未给出，不是真的 0。"
        "输出纯中文正文：不要 Markdown 标记（#、**、表格、分隔线），用「短横线」列点；"
        "分四段，每段不超过 5 条，先结论后依据：\n"
        "1. 竞品格局：谁在投广告、价格带、这个 SKU 处在什么位置；\n"
        "2. 关键词动作：加价 / 维持 / 降价 / 否定，逐词说明理由（只引用给出的 ROI、CTR、CVR、出价）；\n"
        "3. 价格与商品建议：结合同款价格带和本店成交词；\n"
        "4. 风险与下一步：本周可执行的动作，按优先级排序。"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def ai_run(data: dict, on_event=None, max_tokens: int = 1800) -> str:
    from . import llm
    return llm.chat(ai_messages(data), temperature=0.35,
                    max_tokens=max_tokens, on_event=on_event, timeout=240.0)
