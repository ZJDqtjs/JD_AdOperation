# -*- coding: utf-8 -*-
"""花费一览：按 SKU 同时抓「CPS 花费（付款佣金）」与「京准通广告花费」。

一、CPS 花费（京准通 → 数据看板 → 商品数据）：
    POST https://atoms-api.jd.com/union/dataBoard/chooseGoods    商品列表（该区间有数据的 SKU）
    POST https://atoms-api.jd.com/union/dataBoard/orderData      单个 SKU 的 CPS 收支（payCommAmount）
    POST https://atoms-api.jd.com/union/dataBoard/targetCompare  账户级分日趋势（合计校验用）

二、京准通广告花费（按 SKU）—— 复用现有的两个抓取模块（同一套接口/口径）：
    jst 商品报表  jzt-api.jd.com/reweb/jst/account/sku/list       智能投放（campaignType 61，按 skuId 直接有 cost）
    快车计划列表  atoms-api.jd.com/dspad/msa/promolist/overview/campaign
                  → campaignType 153（全站智能推广）计划上带 spuId，可按 SKU 归因
                  → campaignType 2（关键词计划）只有计划/单元维度，无法归到 SKU，单列「未归因」

设计要点：
  - 单 SKU 串行请求，每次之间 0.8~1.6 秒随机抖动，避免请求过快触发风控（掉登录态要重扫码）
  - 整段抓取持有浏览器 Profile 锁（与登录 / 保活互斥），同一时刻只允许一个任务
  - **本地缓存优先**：一次抓取落一份 data/reports/<mode>_<account>_<start>_<end>.json，
    同一账号+同一区间再查直接读文件、不发请求；只有显式 force（页面上的「重新抓取」）才重新抓
  - 缓存**不做自动过期、不做自动清理**，删除只能由用户手动触发；取消/中断的抓取不落盘
"""
from __future__ import annotations

import datetime as dt
import io
import json
import random
import re
import threading
import time
from pathlib import Path

from . import catalog, settings
from .browser import launch_persistent, session_ok

BOARD_PAGE = "https://jzt.jd.com/jtk/#/data-board"
JST_PAGE = "https://jzt.jd.com/jst/#/report/account"
BASE = "https://atoms-api.jd.com/union/dataBoard"

# 限速：单次请求之间留随机间隔（风控优先，宁可慢）
DELAY_SKU = (0.8, 1.6)
DELAY_PAGE = (0.6, 1.2)

# (接口字段, Excel 列名, 类型)
FIELDS = [
    ("payCommAmount", "付款佣金金额(元)", "money"),
    ("payCommRate", "付款佣金比例(%)", "num"),
    ("preCommAmount", "预估佣金金额(元)", "money"),
    ("preCommRate", "预估佣金比例(%)", "num"),
    ("clicks", "点击数", "int"),
    ("uv", "引入UV", "int"),
    ("orderCnt", "下单订单量", "int"),
    ("orderCvr", "下单流量转化率(%)", "num"),
    ("orderGmv", "下单订单金额(元)", "money"),
    ("payOrderCnt", "付款订单量", "int"),
    ("fiOrderCnt", "完成订单量", "int"),
    ("fiOrderGmv", "完成订单金额(元)", "money"),
    ("orderUserCnt", "下单用户数", "int"),
    ("roi", "ROI", "num"),
]

# 京准通广告花费列（按 SKU）
SPEND_FIELDS = [
    ("jztCost", "京准通花费合计(元)", "money"),
    ("jztJstCost", "智能投放花费(元)", "money"),
    ("jztShopCost", "全站智能推广花费(元)", "money"),
    ("jztKwCost", "关键词计划花费(元)", "money"),
    ("jztSwaCost", "全站营销花费(元)", "money"),
]

# 花费缓存结构版本：+1 表示口径变了，旧 json 当缓存未命中（文件保留，不删）
#   1 = 智能投放 + 关键词 + 全站智能推广（漏已删计划、没有全站营销）
#   2 = 快车金额以报表中心计划级为准（含已删），新增全站营销(swa)，分不到的单独列「未归因」
#   3 = 关键词改为推广管理创意的 skuId；全站营销按「实际投放 SKU」(deliverySkuIds) 平分
#   4 = 全站智能推广也按 SPU 下「生效 SKU」（全部 SKU − 黑名单）平分，不再整笔记 spuId
SPEND_SCHEMA = 4

# 「未归因」行的固定 SKU 编号（负数是给跨区间拼接用的稳定 key，不是真实商品）
UNATTR_IDS = {"shop": -9101, "kw": -9102, "swa": -9103, "other": -9104}

_JS_FETCH = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json',
             'accept':'application/json, text/plain, */*',
             'referer':'https://jzt.jd.com/jtk/',
             'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _sleep(rng: tuple) -> None:
    time.sleep(random.uniform(*rng))


_NAME_PREFIXES = ("商品_常规推广_智能化_", "商品_常规推广_", "商品_",
                  "AI_快车-关键词_", "快车-关键词-", "快车-关键词_",
                  "全站智能推广_", "快车-投产比-", "快车-关键词")


def _clean_name(name) -> str:
    """从计划名里剥出商品名（如「商品_常规推广_智能化_牛奶芋头/智能」→「牛奶芋头」）。"""
    s = str(name or "").strip()
    if not s:
        return ""
    for p in _NAME_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    return s.replace("/智能", "").strip(" _-/")[:60]


def norm_range(start: str, end: str) -> tuple[str, str]:
    """校验并归一化日期区间（ISO 字符串）。"""
    try:
        s = dt.date.fromisoformat(str(start).strip())
        e = dt.date.fromisoformat(str(end).strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"日期格式不对（要 YYYY-MM-DD）：{exc}") from exc
    if s > e:
        s, e = e, s
    if (e - s).days > 366:
        raise ValueError("区间最长 366 天")
    return s.isoformat(), e.isoformat()


def _post(page, path: str, payload: dict, retries: int = 2) -> dict:
    url = f"{BASE}/{path}?requestFrom=0"
    last = ""
    for _ in range(retries + 1):
        try:
            res = page.evaluate(_JS_FETCH, {"url": url, "payload": payload})
            if res["status"] != 200:
                last = f"HTTP {res['status']}"
            else:
                data = json.loads(res["text"])
                if str(data.get("code")) == "1":
                    return data
                last = json.dumps(data, ensure_ascii=False)[:200]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
    raise RuntimeError(f"{path} 失败：{last}")


# ---------------------------------------------------------------- 报表中心（创意报表）
# 关键词计划的花费落在「创意」维度：每条创意绑定一个商品，落地页里就是 SKU。
# 该报表还包含「已删除/已下线」的计划，比计划列表更全（8 月多出一个已删计划 6,012 元）。
REPORT_BASE = "https://jzt-api.jd.com/reweb/msa"
AD_LIST_API = f"{REPORT_BASE}/base/ad/list"
# 快车计划级花费（含已删除计划），合计 = 京准通「快车花费」
MSA_PLAN_API = f"{REPORT_BASE}/base/campaign/list"
# 全站营销（101 单品推广 / 118 全店推广 / 117 新品推广），单品推广行上带 sxuId
SWA_PLAN_API = "https://jzt-api.jd.com/reweb/swa/account/campaign/list"
SWA_TYPES = {101: "单品推广", 118: "全店推广", 117: "新品推广"}
# 全站营销推广管理：计划列表 + 计划下的「实际投放 SKU」（deliverySkuIds）
SWA_AD_API = "https://jzt-api.jd.com/swa/ad/list"
SWA_FORBID_API = "https://jzt-api.jd.com/swa/forbidden/list"
# 快车推广管理：创意列表直接带 skuId（关键词计划推的是哪个商品）
MSA_AD_API = "https://atoms-api.jd.com/dspad/msa/promolist/item/keyword/ad"
MSA_PAGE = "https://jzt.jd.com/msa/index.html#/list/tab/plan?objective=overview"
MSA_SMART_PAGE = ("https://jzt.jd.com/msa/#/list/shopSmart?objective=item"
                  "&scenario=normal&targetingType=shopSmart")
# 全站智能推广：SPU 下「生效 SKU」列表（拉黑的 SKU 不参与投放）
MSA_FORBID_API = "https://atoms-api.jd.com/dspad/smartmax/forbidden/list"
MSA_MAX_BUSINESS = 600000013

_JS_REPORT = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json',
             'accept':'application/json, text/plain, */*',
             'referer':'https://jzt.jd.com/report/index.html/',
             'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _post_report(page, url: str, payload: dict, retries: int = 2) -> dict:
    last = ""
    for _ in range(retries + 1):
        try:
            res = page.evaluate(_JS_REPORT, {"url": url, "payload": payload})
            if res["status"] != 200:
                last = f"HTTP {res['status']}"
            else:
                data = json.loads(res["text"])
                if str(data.get("code")) == "1":
                    return data
                last = json.dumps(data, ensure_ascii=False)[:200]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
    raise RuntimeError(f"报表中心 {url.rsplit('/', 1)[-1]} 失败：{last}")


_JS_SWA = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json',
             'accept':'application/json, text/plain, */*',
             'referer':'https://jzt.jd.com/swa/index.html/',
             'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""

_JS_ATOMS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json',
             'accept':'application/json, text/plain, */*',
             'referer':'https://jzt.jd.com/msa/',
             'language':'zh_CN','siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""


def _post_swa(page, url: str, payload: dict, retries: int = 2) -> dict:
    """全站营销接口（jzt-api /swa/...）。"""
    last = ""
    for _ in range(retries + 1):
        try:
            res = page.evaluate(_JS_SWA, {"url": url, "payload": payload})
            if res["status"] != 200:
                last = f"HTTP {res['status']}"
            else:
                data = json.loads(res["text"])
                if str(data.get("code")) == "1":
                    return data
                last = json.dumps(data, ensure_ascii=False)[:200]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
    raise RuntimeError(f"全站营销 {url.rsplit('/', 2)[-2]}/{url.rsplit('/', 1)[-1]} 失败：{last}")


def _post_atoms(page, url: str, payload: dict, retries: int = 2) -> dict:
    """推广管理（atoms-api /dspad/msa/...）。"""
    last = ""
    for _ in range(retries + 1):
        try:
            res = page.evaluate(_JS_ATOMS, {"url": url, "payload": payload})
            if res["status"] != 200:
                last = f"HTTP {res['status']}"
            else:
                data = json.loads(res["text"])
                if str(data.get("code")) == "1":
                    return data
                last = json.dumps(data, ensure_ascii=False)[:200]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
    raise RuntimeError(f"推广管理 {url.rsplit('/', 1)[-1]} 失败：{last}")


def sku_from_creative(row: dict) -> int:
    """从创意行解析 SKU：优先落地页 item.jd.com/<sku>.html，其次创意名里的数字。"""
    for src in (row.get("landingPageUrl"), row.get("adName")):
        s = str(src or "")
        m = re.search(r"item\.jd\.com/(\d{6,})", s) or re.search(r"(\d{9,})", s)
        if m:
            return int(m.group(1))
    return 0


def fetch_creative_rows(page, start: str, end: str, account_id: int,
                        page_size: int = 200) -> list[dict]:
    """快车「创意」报表（含已删除计划），每行带 cost 与落地页 SKU。"""
    rows: list[dict] = []
    pno = 1
    while pno <= 30:
        payload = {
            "isDaily": False, "startDay": start, "endDay": end,
            "clickOrOrderCaliber": 0, "clickOrOrderDay": 30, "giftFlag": 0,
            "orderStatusCategory": 1, "filters": [], "pinIds": [int(account_id)],
            "creativeTypes": [], "deliveryType": 0,
            "columns": ["cost", "clicks", "impressions", "totalOrderCnt", "totalOrderSum"],
            "obys": "cost|desc", "page": pno, "pageSize": page_size,
        }
        d = _post_report(page, AD_LIST_API, payload)
        batch = ((d.get("data") or {}).get("datas") or [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        pno += 1
        _sleep(DELAY_PAGE)
    return rows


def list_goods(page, start: str, end: str, page_size: int = 100) -> list[dict]:
    rows: list[dict] = []
    idx = 1
    while True:
        d = _post(page, "chooseGoods", {"displayType": 1, "itemId": "", "pageIndex": idx,
                                       "pageSize": page_size, "startDate": start, "endDate": end})
        data = d.get("data") or {}
        batch = data.get("data") or []
        rows.extend(batch)
        total = int(data.get("total") or 0)
        if not batch or len(rows) >= total or idx > 50:
            break
        idx += 1
        _sleep(DELAY_PAGE)
    return rows


def sku_metrics(page, sku: int, start: str, end: str) -> dict:
    d = _post(page, "orderData", {"startDate": start, "endDate": end, "realTime": False,
                                  "skuId": sku, "spuId": None})
    return d.get("data") or {}


def daily_total(page, start: str, end: str) -> list[dict]:
    d = _post(page, "targetCompare", {"startDate": start, "endDate": end,
                                      "realTime": False, "skuId": None, "spuId": None})
    return d.get("data") or []


def fetch_msa_plans(page, start: str, end: str, account_id: int,
                    page_size: int = 200) -> list[dict]:
    """快车计划级花费（报表中心 base/campaign/list，**含已删除计划**）。

    合计与京准通「快车花费」完全一致，用来兜住 promolist 里看不到的已删计划。
    """
    rows: list[dict] = []
    pno = 1
    while pno <= 20:
        payload = {
            "isDaily": False, "startDay": start, "endDay": end,
            "clickOrOrderCaliber": 0, "clickOrOrderDay": 30, "giftFlag": 0,
            "orderStatusCategory": 1, "filters": [], "pinIds": [int(account_id)],
            "creativeTypes": [], "deliveryType": 0,
            "columns": ["cost", "clicks", "impressions"],
            "obys": "cost|desc", "page": pno, "pageSize": page_size,
        }
        d = _post_report(page, MSA_PLAN_API, payload)
        batch = ((d.get("data") or {}).get("datas") or [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        pno += 1
        _sleep(DELAY_PAGE)
    return rows


def fetch_swa_plans(page, start: str, end: str, page_size: int = 200) -> list[dict]:
    """全站营销花费（单品推广 101 / 全店推广 118 / 新品推广 117）。

    单品推广报表一行只给到 SPU（sxuId），真正投放的 SKU 见 fetch_swa_delivery()。
    """
    rows: list[dict] = []
    for ctype in SWA_TYPES:
        pno = 1
        while pno <= 20:
            payload = {
                "platform": "", "campaignTypes": [ctype], "province": "",
                "startDay": start, "endDay": end, "orderStatus": "",
                "giftFlag": "", "clickOrOrderDay": 15, "clickOrOrderCaliber": 0,
                "sxuId": "", "obys": "", "isDaily": False, "orderStatusCategory": 1,
                "dateValues": [{"startDay": start, "endDay": end}],
                "pageNum": pno, "pageSize": page_size, "requestFrom": 0,
            }
            d = _post_report(page, SWA_PLAN_API, payload)
            batch = ((d.get("data") or {}).get("rows") or [])
            for r in batch:
                r["_swaType"] = ctype
            rows.extend(batch)
            if len(batch) < page_size:
                break
            pno += 1
            _sleep(DELAY_PAGE)
        _sleep(DELAY_PAGE)
    return rows


def fetch_swa_delivery(page, start: str, end: str) -> dict:
    """全站营销单品推广：SPU → 实际投放的 SKU 列表。

    单品推广是「按 SPU 投」，计划里可以把 SPU 下部分 SKU 拉黑，报表只给 SPU 汇总，
    必须再查 swa/forbidden/list 的 deliverySkuIds 才知道真正在投哪几个 SKU。
    返回 {spuId: {"skus": [skuId...], "names": {skuId: 商品名}}}。
    """
    out: dict[int, dict] = {}
    try:
        d = _post_swa(page, SWA_AD_API, {
            "page": 1, "pageSize": 200, "platform": "", "status": "", "sxuType": "",
            "sxuId": "", "filters": [], "obys": None, "product": "swa_dsp",
            "startDay": start, "endDay": end, "conversionCategory": 15,
            "campaignType": 101, "sureWinStatus": "", "requestFrom": 0,
        })
        rows = ((d.get("data") or {}).get("data") or [])
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        try:
            spu = int(r.get("spuId") or r.get("sxuId") or 0)
            cid = int(r.get("campaignId") or 0)
            gid = int(r.get("groupId") or r.get("adGroupId") or 0)
        except (TypeError, ValueError):
            continue
        if not spu:
            continue
        payload = {"spuIds": [spu], "businessType": 600000004, "campaignType": 101,
                   "searchType": 2,
                   "campaignInfoList": [{"campaignId": cid, "adGroupId": gid, "spuId": spu,
                                         "jointActivityId": 0, "activityType": 0}],
                   "requestFrom": 0}
        try:
            data = _post_swa(page, SWA_FORBID_API, payload).get("data") or {}
        except Exception:  # noqa: BLE001
            continue
        skus: list[int] = []
        for x in data.get("deliverySkuIds") or []:
            try:
                skus.append(int(x))
            except (TypeError, ValueError):
                pass
        names: dict[int, str] = {}
        for sid, v in (data.get("skuDetailMap") or {}).items():
            try:
                names[int(sid)] = str((v or {}).get("skuName") or (v or {}).get("name") or "")
            except (TypeError, ValueError):
                pass
        if skus:
            slot = out.setdefault(spu, {"skus": [], "names": {}})
            for sku in skus:
                if sku not in slot["skus"]:
                    slot["skus"].append(sku)
            for sid, nm in names.items():
                if nm:
                    slot["names"].setdefault(sid, nm)
        _sleep(DELAY_PAGE)
    return out


def fetch_msa_spu_skus(page, spu_ids: list[int]) -> dict:
    """全站智能推广（campaignType=153）：SPU → 该 SPU 下**真正在投**的 SKU。

    计划按 SPU 投放，SPU 下的 SKU 可以拉黑（推广管理 → 更多 → 设置SKU黑名单），
    接口一次就能给全部 SPU 的生效 SKU（`spuEffectiveSkuMap`）+ 拉黑数量（`spuSkuNumMap`）。
    返回 {spuId: {"skus": [...], "names": {...}, "black": n}}。
    """
    spus = sorted({int(x) for x in (spu_ids or []) if int(x or 0) > 0})
    if not spus:
        return {}
    out: dict[int, dict] = {}
    for i in range(0, len(spus), 40):          # 一次别塞太多，分批
        batch = spus[i:i + 40]
        payload = {
            "spuIds": batch, "businessType": MSA_MAX_BUSINESS, "campaignType": 153,
            "searchType": 2,
            "campaignInfoList": [{"campaignId": 0, "adGroupId": 0, "spuId": spu,
                                  "jointActivityId": None, "activityType": None}
                                 for spu in batch],
            "requestFrom": 0,
        }
        try:
            data = _post_atoms(page, MSA_FORBID_API, payload).get("data") or {}
        except Exception:  # noqa: BLE001
            continue
        eff = data.get("spuEffectiveSkuMap") or {}
        nums = data.get("spuSkuNumMap") or {}
        details = data.get("skuDetailMap") or {}
        for spu in batch:
            skus = []
            for x in eff.get(str(spu)) or eff.get(spu) or []:
                try:
                    skus.append(int(x))
                except (TypeError, ValueError):
                    pass
            if not skus:
                continue
            names: dict[int, str] = {}
            for sid in skus:
                v = details.get(str(sid)) or details.get(sid) or {}
                nm = str(v.get("skuName") or v.get("name") or "")
                if nm:
                    names[sid] = nm
            black = int((nums.get(str(spu)) or {}).get("blackSkuNum") or 0)
            out[int(spu)] = {"skus": skus, "names": names, "black": black}
        _sleep(DELAY_PAGE)
    return out


def fetch_kw_creative_skus(page, page_size: int = 200) -> dict:
    """快车关键词创意 → 它推的是哪个 SKU（管理页接口直接返回 skuId）。

    报表中心的创意报表只有落地页 URL，遇到店铺页/活动页就取不到 SKU；
    这里按计划取准：返回 {campaignId: {skuId, ...}}。
    """
    out: dict[int, set] = {}
    pno = 1
    while pno <= 10:
        payload = {"page": pno, "pageSize": page_size, "status": "", "filters": [],
                   "creativeType": [], "obys": "", "startDay": settings.today().isoformat(),
                   "endDay": settings.today().isoformat(), "clickOrOrderCaliber": 0,
                   "clickOrOrderDay": 15, "giftFlag": 0, "orderStatusCategory": 1,
                   "customColumns": ["campaignName", "skuId", "url"],
                   "campaignId": "", "groupId": "", "putType": "", "requestFrom": 0}
        try:
            d = _post_atoms(page, MSA_AD_API, payload)
        except Exception:  # noqa: BLE001
            break
        data = d.get("data") or {}
        rows: list[dict] = []
        for key in ("msaNormalAdReport", "msaSmartAdReport"):
            rows.extend(((data.get(key) or {}).get("data") or []))
        for r in rows:
            try:
                cid = int(r.get("campaignId") or 0)
                sku = int(r.get("skuId") or 0)
            except (TypeError, ValueError):
                continue
            if cid and sku > 0:
                out.setdefault(cid, set()).add(sku)
        if len(rows) < page_size:
            break
        pno += 1
        _sleep(DELAY_PAGE)
    return out


def _norm_name(name) -> str:
    return re.sub(r"\s+", "", str(name or ""))[:40]


def _spu_maps(promolist_rows: list[dict]) -> tuple[dict, dict]:
    """从快车计划列表建 campaignId→spuId、计划名归一化→spuId 两张映射表。"""
    by_id: dict[int, int] = {}
    by_name: dict[str, set] = {}
    for c in promolist_rows or []:
        spu = int(c.get("spuId") or 0)
        if not spu:
            continue
        try:
            cid = int(c.get("campaignId") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid:
            by_id[cid] = spu
        key = _norm_name(c.get("campaignName"))
        if key:
            by_name.setdefault(key, set()).add(spu)
    return by_id, by_name


def _match_spu(name, by_name: dict) -> int:
    """已删/已下线计划拿不到 spuId 时，按计划名回找同一个商品（要求唯一命中）。"""
    key = _norm_name(name)
    if not key:
        return 0
    hit = by_name.get(key)
    if hit and len(hit) == 1:
        return next(iter(hit))
    if len(key) >= 20:
        cands = {spu for k, spus in by_name.items()
                 if k[:20] == key[:20] for spu in spus}
        if len(cands) == 1:
            return next(iter(cands))
    return 0


def allocate(jst_rows: list[dict], creative_rows: list[dict], msa_plans: list[dict],
             promolist_rows: list[dict], swa_rows: list[dict],
             kw_skus: dict | None = None, swa_delivery: dict | None = None,
             shop_delivery: dict | None = None) -> dict:
    """把四路口径按 SKU 归集；归不到的单独列出，**绝不按比例分摊**。

    1) 智能投放  jst 商品报表按 skuId（含已下线计划）
    2) 关键词    推广管理创意接口的 skuId（最准）；取不到退回创意落地页 URL（含已删计划）
    3) 全站智能推广  报表中心计划级金额（含已删计划）+ 计划 spuId → SPU 下**生效 SKU 平分**
       （生效 SKU = SPU 的全部 SKU − SKU 黑名单；拿不到名单时退回整笔记 spuId）
    4) 全站营销  单品推广按「实际投放 SKU」(deliverySkuIds) 平分；全店/新品推广没有 SKU → 未归因
    总额校验：快车已归集 + 未归因 必须等于计划级合计（= 京准通「快车花费」）。
    """
    by_sku: dict[int, dict] = {}
    hints: dict[int, str] = {}
    unattr = {"shop": 0.0, "kw": 0.0, "swa": 0.0, "other": 0.0}

    def bucket(sku: int) -> dict:
        return by_sku.setdefault(int(sku), {"jst": 0.0, "shop": 0.0, "kw": 0.0, "swa": 0.0})

    for r in jst_rows or []:
        sku = int(r.get("skuId") or 0)
        cost = float(r.get("cost") or 0)
        if not sku or not cost:
            continue
        bucket(sku)["jst"] += cost
        hints.setdefault(sku, _clean_name(r.get("campaignName")))

    kw_total = 0.0
    kw_rows: list[dict] = []
    for r in creative_rows or []:
        if r.get("campaignType") not in (2, None):
            continue
        cost = float(r.get("cost") or 0)
        if cost <= 0:
            continue
        kw_total += cost
        sku = 0
        try:
            cid = int(r.get("campaignId") or 0)
        except (TypeError, ValueError):
            cid = 0
        cands = (kw_skus or {}).get(cid) if cid else None
        if cands and len(cands) == 1:
            sku = next(iter(cands))     # 管理页直接给了 SKU，最准
        if not sku:
            sku = sku_from_creative(r)
        if sku:
            bucket(sku)["kw"] += cost
            hints.setdefault(sku, _clean_name(r.get("adName") or r.get("campaignName")))
        else:
            unattr["kw"] += cost
            kw_rows.append({"campaignId": r.get("campaignId"),
                            "campaignName": r.get("campaignName") or r.get("adName") or "",
                            "cost": round(cost, 2)})

    by_id, by_name = _spu_maps(promolist_rows)
    msa_total = 0.0
    shop_split = 0
    for p in msa_plans or []:
        cost = float(p.get("cost") or 0)
        if not cost:
            continue
        msa_total += cost
        scen = str(p.get("fullMarketingScenarioTypeName") or "")
        if "全站智能推广" not in scen:
            continue    # 关键词/智能投放分别由创意报表、jst 商品报表覆盖
        try:
            cid = int(p.get("campaignId") or 0)
        except (TypeError, ValueError):
            cid = 0
        spu = by_id.get(cid) or _match_spu(p.get("campaignName"), by_name)
        if not spu:
            unattr["shop"] += cost
            continue
        # 计划是按 SPU 投的：SPU 下没被拉黑的 SKU 才真正在投，花费在它们之间平分
        info = (shop_delivery or {}).get(spu) or {}
        skus = list(info.get("skus") or [spu])
        share = round(cost / len(skus), 2)
        last = round(cost - share * (len(skus) - 1), 2)
        if len(skus) > 1:
            shop_split += 1
        for i, sku in enumerate(skus):
            bucket(sku)["shop"] += last if i == len(skus) - 1 else share
            nm = (info.get("names") or {}).get(sku)
            if not nm:
                nm = _clean_name(p.get("campaignName")) if sku == spu else ""
            if nm:
                hints.setdefault(sku, nm)

    swa_total = 0.0
    swa_split = 0
    for r in swa_rows or []:
        cost = float(r.get("cost") or 0)
        if not cost:
            continue
        swa_total += cost
        try:
            spu = int(r.get("sxuId") or 0)
        except (TypeError, ValueError):
            spu = 0
        info = (swa_delivery or {}).get(spu) or {}
        skus = list(info.get("skus") or ([] if not spu else [spu]))
        if not skus:
            unattr["swa"] += cost
            continue
        # 一个计划投多个 SKU（SPU 下没被拉黑的那些）时平分，尾差给最后一个，合计不变
        share = round(cost / len(skus), 2)
        last = round(cost - share * (len(skus) - 1), 2)
        if len(skus) > 1:
            swa_split += 1
        for i, sku in enumerate(skus):
            bucket(sku)["swa"] += last if i == len(skus) - 1 else share
            nm = (info.get("names") or {}).get(sku)
            if not nm:
                nm = _clean_name(r.get("adName")) if sku == spu else ""
            if nm:
                hints.setdefault(sku, nm)

    jst_total = round(sum(v["jst"] for v in by_sku.values()), 2)
    shop_total = round(sum(v["shop"] for v in by_sku.values()), 2)
    kw_mapped = round(sum(v["kw"] for v in by_sku.values()), 2)
    swa_mapped = round(sum(v["swa"] for v in by_sku.values()), 2)

    # 对账：快车三路（含未归因）必须等于计划级合计，差额也进未归因，绝不丢钱
    # 关键词用 kw_total（已含无落地页那部分），否则会把未归因的关键词重复算一遍
    covered_msa = round(jst_total + shop_total + kw_total + unattr["shop"] + unattr["other"], 2)
    gap_msa = round(msa_total - covered_msa, 2)
    if abs(gap_msa) >= 0.01:
        unattr["other"] += gap_msa
    covered_swa = round(swa_mapped + unattr["swa"], 2)
    gap_swa = round(swa_total - covered_swa, 2)
    if abs(gap_swa) >= 0.01:
        unattr["swa"] += gap_swa

    out: dict[int, dict] = {}
    for sku, v in by_sku.items():
        jst_c, shop_c = round(v["jst"], 2), round(v["shop"], 2)
        kw_c, swa_c = round(v["kw"], 2), round(v["swa"], 2)
        out[sku] = {"jst": jst_c, "shop": shop_c, "kw": kw_c, "swa": swa_c,
                    "total": round(jst_c + shop_c + kw_c + swa_c, 2)}

    unattr_rows: list[dict] = []
    for key, label in (("shop", "快车-已删/找不到对应商品的全站智能推广"),
                       ("kw", "关键词-无落地页，归不到商品"),
                       ("swa", "全站营销-全店/新品推广或无 SKU"),
                       ("other", "快车-其他类型计划（与计划报表对账差额）")):
        val = round(unattr[key], 2)
        if abs(val) >= 0.01:
            unattr_rows.append({"key": key, "name": label, "cost": val})

    totals = {
        "jst": jst_total,
        "shopSmart": shop_total,
        "keywordTotal": round(kw_total, 2),
        "swa": swa_mapped,
        "keyword": round(unattr["kw"], 2),
        "other": round(unattr["other"], 2),
        "msaPlanTotal": round(msa_total, 2),
        "swaPlanTotal": round(swa_total, 2),
        "swaSplit": swa_split,
        "shopSplit": shop_split,
        "unattr": round(sum(r["cost"] for r in unattr_rows), 2),
        "skuCount": len(out),
    }
    totals["total"] = round(sum(v["total"] for v in out.values()) + totals["unattr"], 2)
    kw_rows.sort(key=lambda r: r["cost"], reverse=True)
    return {"bySku": out, "totals": totals, "keywordRows": kw_rows,
            "unattrRows": unattr_rows, "hints": {k: v for k, v in hints.items() if v}}


def fetch_spend(page, start: str, end: str, account_id: int | None = None) -> dict:
    """按 SKU 抓京准通花费：快车（智能投放/关键词/全站智能推广）+ 全站营销。

    四路数据源：
      jst 商品报表          /reweb/jst/account/sku/list      智能投放，按 SKU，含已下线
      报表中心创意报表      /reweb/msa/base/ad/list          关键词，落地页 SKU（含已删计划）
      快车推广管理创意      /dspad/msa/promolist/item/keyword/ad  关键词创意直接给 skuId（更准）
      报表中心计划报表      /reweb/msa/base/campaign/list    快车计划级金额（含已删），对账基准
      快车计划列表          /dspad/msa/promolist/...         计划上的 spuId（归因用）
      全站营销报表          /reweb/swa/account/campaign/list 单品推广只到 SPU（sxuId）
      全站营销推广管理      /swa/ad/list + /swa/forbidden/list  计划实际投放的 SKU（deliverySkuIds）
    实测 2026-08 主账号：33,750.20 + 8,325.87 + 11,432.90 + 9,558.13 = 63,067.10
    = 京准通「效果广告花费」（快车 53,508.97 + 全站营销 9,558.13）。
    """
    from .scrape_jst_report import fetch as jst_fetch
    from .scrape_jzt import fetch_campaigns

    if not account_id:
        account_id = int(settings.get_account(None).get("account_id") or 0)

    # 快车计划列表：当前页面就在 msa 快车，直接取（spuId 映射）
    camps = fetch_campaigns(page, start, end)
    # 全站智能推广计划涉及的 SPU：要去查「SPU 下生效 SKU」（黑名单除外）
    shop_spus: list[int] = []
    for r in camps.get("rows") or []:
        try:
            if int(r.get("campaignType") or 0) == 153:
                spu = int(r.get("spuId") or 0)
                if spu and spu not in shop_spus:
                    shop_spus.append(spu)
        except (TypeError, ValueError):
            continue
    _sleep(DELAY_PAGE)
    # 智能投放商品报表：先切到 jst 报表页，保证接口上下文与现网一致
    try:
        page.goto(JST_PAGE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
    except Exception:  # noqa: BLE001
        pass
    jst = jst_fetch(page, "sku", start, end)
    _sleep(DELAY_PAGE)

    creatives: list[dict] = []
    try:
        creatives = fetch_creative_rows(page, start, end, account_id)
    except Exception:  # noqa: BLE001
        creatives = []
    _sleep(DELAY_PAGE)

    plans: list[dict] = []
    try:
        plans = fetch_msa_plans(page, start, end, account_id)
    except Exception:  # noqa: BLE001
        plans = []
    _sleep(DELAY_PAGE)

    swa: list[dict] = []
    try:
        swa = fetch_swa_plans(page, start, end)
    except Exception:  # noqa: BLE001
        swa = []
    _sleep(DELAY_PAGE)

    # 推广管理页：关键词创意直接给 skuId；全站营销计划给「实际投放 SKU」
    try:
        page.goto(MSA_PAGE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
    except Exception:  # noqa: BLE001
        pass
    kw_skus: dict = {}
    try:
        kw_skus = fetch_kw_creative_skus(page)
    except Exception:  # noqa: BLE001
        kw_skus = {}
    _sleep(DELAY_PAGE)
    swa_delivery: dict = {}
    try:
        swa_delivery = fetch_swa_delivery(page, start, end)
    except Exception:  # noqa: BLE001
        swa_delivery = {}
    _sleep(DELAY_PAGE)
    # 全站智能推广：SPU 下「生效 SKU」（SPU 全部 SKU − 黑名单），花费要平分
    shop_delivery: dict = {}
    try:
        shop_delivery = fetch_msa_spu_skus(page, shop_spus)
    except Exception:  # noqa: BLE001
        shop_delivery = {}

    out = allocate(jst.get("rows") or [], creatives, plans, camps.get("rows") or [], swa,
                   kw_skus=kw_skus, swa_delivery=swa_delivery, shop_delivery=shop_delivery)
    ext = camps.get("ext") or {}
    try:
        out["totals"]["campaignTotal"] = round(float(ext.get("cost") or 0), 2)
    except (TypeError, ValueError):
        out["totals"]["campaignTotal"] = None
    return out


# ================================================================ Excel
def _bold():
    from openpyxl.styles import Font
    return Font(bold=True)


def _center():
    from openpyxl.styles import Alignment
    return Alignment(horizontal="center", vertical="center")


def _fill():
    from openpyxl.styles import PatternFill
    return PatternFill("solid", fgColor="DDEBF7")


def build_cps_xlsx(rows: list[dict], daily: list[dict], start: str, end: str,
                   account_label: str = "", dim: str = "sku") -> bytes:
    """CPS 花费（付款佣金）导出。"""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "SKU付款佣金"
    fill = _fill()
    cols = FIELDS
    ws.append(["SKU", "商品名称"] + [name for _, name, _ in cols])
    for c in ws[1]:
        c.font = _bold()
        c.fill = fill
        c.alignment = _center()

    def _num(v, k):
        t = dict((f[0], f[2]) for f in cols)[k]
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v
        return f if t in ("money", "num") else int(f)

    for r in rows:
        ws.append([r.get("skuId"), r.get("itemName")]
                  + [_num(r.get(k), k) for k, _, _ in cols])
    total_pay = round(sum((float(r.get("payCommAmount") or 0)) for r in rows), 2)
    ws.append([])
    ws.append(["合计", f"{len(rows)} 个 SKU", total_pay])
    ws.freeze_panes = "C2"
    for i, w in enumerate([15, 46] + [17] * len(cols), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row[2:]:
            if isinstance(c.value, float):
                c.number_format = "#,##0.00"
            elif isinstance(c.value, int):
                c.number_format = "#,##0"

    # 品类汇总（忽略斤数/g数/个数等规格，按大类汇总）
    cps_keys = ["payCommAmount", "preCommAmount", "clicks", "uv", "orderCnt", "orderGmv",
                "payOrderCnt", "fiOrderCnt", "fiOrderGmv", "orderUserCnt"]
    labels = {k: lab for k, lab, _ in FIELDS}
    cats = sorted(catalog.aggregate(rows, cps_keys), key=lambda x: -x["payCommAmount"])
    ws_c = wb.create_sheet("品类汇总", 1)
    ws_c.append(["品类", "SKU数"] + [labels[k] for k in cps_keys])
    for c in ws_c[1]:
        c.font = _bold()
        c.fill = fill
        c.alignment = _center()
    for it in cats:
        ws_c.append([it["category"], it["skuCount"]] + [it[k] for k in cps_keys])
    ws_c.append([])
    ws_c.append(["合计", f"{len(rows)} 个 SKU"] + [
        round(sum(it[k] for it in cats), 2) if k == "payCommAmount" else None for k in cps_keys])
    ws_c.freeze_panes = "C2"
    for i, w in enumerate([20, 10] + [17] * len(cps_keys), 1):
        ws_c.column_dimensions[get_column_letter(i)].width = w
    for row in ws_c.iter_rows(min_row=2):
        for c in row[2:]:
            if isinstance(c.value, float):
                c.number_format = "#,##0.00"
            elif isinstance(c.value, int):
                c.number_format = "#,##0"

    ws2 = wb.create_sheet("分日合计")
    ws2.append(["日期", "付款佣金金额(元)", "预估佣金金额(元)", "点击数", "下单订单量",
                "下单订单金额(元)", "完成订单量", "完成订单金额(元)", "ROI"])
    for c in ws2[1]:
        c.font = _bold()
        c.fill = fill
    for d in daily:
        ws2.append([d.get("date"), d.get("payCommAmount"), d.get("preCommAmount"), d.get("clicks"),
                    d.get("orderCnt"), d.get("orderGmv"), d.get("fiOrderCnt"),
                    d.get("fiOrderGmv"), d.get("roi")])
    if daily:
        ws2.append([])
        ws2.append(["合计", round(sum((d.get("payCommAmount") or 0) for d in daily), 2),
                    round(sum((d.get("preCommAmount") or 0) for d in daily), 2),
                    sum((d.get("clicks") or 0) for d in daily),
                    sum((d.get("orderCnt") or 0) for d in daily),
                    round(sum((d.get("orderGmv") or 0) for d in daily), 2),
                    sum((d.get("fiOrderCnt") or 0) for d in daily),
                    round(sum((d.get("fiOrderGmv") or 0) for d in daily), 2), None])
    for i, w in enumerate([13, 18, 18, 12, 13, 18, 13, 18, 10], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "B2"

    ws3 = wb.create_sheet("说明")
    ws3.append(["统计区间", f"{start} ~ {end}"])
    ws3.append(["账号", account_label or "-"])
    ws3.append(["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    ws3.append(["数据来源", "京准通 数据看板 → 商品数据（atoms-api /union/dataBoard）"])
    ws3.append(["口径", "每个 SKU 单独取 orderData；付款佣金金额=payCommAmount（与页面「商品数据」一致）"])
    ws3.column_dimensions["A"].width = 14
    ws3.column_dimensions["B"].width = 92

    if dim == "cat":
        # 「按品类」导出：去掉 SKU 明细，品类汇总成为首屏
        try:
            wb.remove(wb["SKU付款佣金"])
        except KeyError:
            pass

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_spend_xlsx(rows: list[dict], spend: dict, start: str, end: str,
                     account_label: str = "", dim: str = "sku") -> bytes:
    """京准通广告花费（按 SKU）导出。"""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    totals = (spend or {}).get("totals") or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "SKU广告花费"
    fill = _fill()
    ws.append(["SKU", "商品名称"] + [name for _, name, _ in SPEND_FIELDS])
    for c in ws[1]:
        c.font = _bold()
        c.fill = fill
        c.alignment = _center()
    for r in rows:
        ws.append([r.get("skuId"), r.get("itemName")]
                  + [float(r.get(k) or 0) for k, _, _ in SPEND_FIELDS])
    ws.append([])
    ws.append(["合计", f"{len(rows)} 个 SKU"] + [
        round(sum((float(r.get(k) or 0)) for r in rows), 2) if k == "jztCost" else None
        for k, _, _ in SPEND_FIELDS])
    ws.freeze_panes = "C2"
    for i, w in enumerate([15, 46] + [17] * len(SPEND_FIELDS), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row[2:]:
            if isinstance(c.value, float):
                c.number_format = "#,##0.00"
            elif isinstance(c.value, int):
                c.number_format = "#,##0"

    sp_keys = [k for k, _, _ in SPEND_FIELDS]
    sp_labels = {k: lab for k, lab, _ in SPEND_FIELDS}
    cats = sorted(catalog.aggregate(rows, sp_keys), key=lambda x: -x.get("jztCost", 0))
    ws_c = wb.create_sheet("品类汇总", 1)
    ws_c.append(["品类", "SKU数"] + [sp_labels[k] for k in sp_keys])
    for c in ws_c[1]:
        c.font = _bold()
        c.fill = fill
        c.alignment = _center()
    for it in cats:
        ws_c.append([it["category"], it["skuCount"]] + [it[k] for k in sp_keys])
    ws_c.append([])
    ws_c.append(["合计", f"{len(rows)} 个 SKU"] + [
        round(sum(it[k] for it in cats), 2) if k == "jztCost" else None for k in sp_keys])
    ws_c.freeze_panes = "C2"
    for i, w in enumerate([20, 10] + [17] * len(sp_keys), 1):
        ws_c.column_dimensions[get_column_letter(i)].width = w
    for row in ws_c.iter_rows(min_row=2):
        for c in row[2:]:
            if isinstance(c.value, float):
                c.number_format = "#,##0.00"
            elif isinstance(c.value, int):
                c.number_format = "#,##0"

    unattr_rows = (spend or {}).get("unattrRows") or []
    if unattr_rows:
        ws_u = wb.create_sheet("未归因花费")
        ws_u.append(["项目", "花费(元)"])
        for c in ws_u[1]:
            c.font = _bold()
            c.fill = fill
        for r in unattr_rows:
            ws_u.append([r.get("name"), r.get("cost")])
        ws_u.append([])
        ws_u.append(["合计", round(sum(float(r.get("cost") or 0) for r in unattr_rows), 2)])
        ws_u.column_dimensions["A"].width = 46
        ws_u.column_dimensions["B"].width = 14

    kw_rows = (spend or {}).get("keywordRows") or []
    if kw_rows:
        ws4 = wb.create_sheet("未归因关键词计划")
        ws4.append(["计划ID", "计划名称", "花费(元)"])
        for c in ws4[1]:
            c.font = _bold()
            c.fill = fill
        for r in kw_rows:
            ws4.append([r.get("campaignId"), r.get("campaignName"), r.get("cost")])
        ws4.append([])
        ws4.append(["合计", f"{len(kw_rows)} 个计划",
                    round(sum(r.get("cost") or 0 for r in kw_rows), 2)])
        ws4.column_dimensions["A"].width = 16
        ws4.column_dimensions["B"].width = 60
        ws4.column_dimensions["C"].width = 14

    ws3 = wb.create_sheet("说明")
    ws3.append(["统计区间", f"{start} ~ {end}"])
    ws3.append(["账号", account_label or "-"])
    ws3.append(["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
    ws3.append(["数据来源", "智能投放 = jst 商品报表（按 skuId）；"
                          "关键词 = 报表中心创意报表（按创意落地页 SKU，含已删除计划）；"
                          "全站智能推广 = 报表中心计划级金额（含已删除计划）+ 计划 spuId；"
                          "全站营销 = 全站营销报表（单品推广的 sxuId）"])
    ws3.append(["花费合计", totals.get("total")])
    ws3.append(["  其中 智能投放", totals.get("jst")])
    ws3.append(["  其中 关键词计划", totals.get("keywordTotal")])
    ws3.append(["  其中 全站智能推广", totals.get("shopSmart")])
    ws3.append(["  其中 全站营销", totals.get("swa")])
    ws3.append(["快车计划报表合计", totals.get("msaPlanTotal")])
    ws3.append(["全站营销报表合计", totals.get("swaPlanTotal")])
    ws3.append(["未归因合计", totals.get("unattr")])
    ws3.append(["未归因说明", "只按能定位到的 SKU 归因（计划 spuId / 创意落地页 / sxuId），"
                              "定位不到的花费单列在「未归因花费」sheet，绝不按比例摊到商品上。"
                              "「快车计划报表合计」应与京准通快车花费一致。"])
    ws3.column_dimensions["A"].width = 22
    ws3.column_dimensions["B"].width = 95

    if dim == "cat":
        try:
            wb.remove(wb["SKU广告花费"])
        except KeyError:
            pass

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_cached(mode: str, account: str, start: str, end: str) -> dict | None:
    """只读本地缓存（精确命中 → 多段拼接），不启动浏览器、不发请求。

    给「月度利润分析」这类要复用花费数据的页面调用。
    """
    runner = PayCommRunner(mode)          # 仅用于读文件，不涉及 Profile 锁
    try:
        data = runner.cached_result(account, start, end)
        if data:
            return data
        got = runner.use_stitched(account, start, end)
        if got and got.get("jobId"):
            return runner._results.get(got["jobId"])
    except Exception:  # noqa: BLE001
        return None
    return None


# ================================================================ 任务
MODES = {
    # mode: (磁盘文件名前缀, 页面标题, 按钮文案)
    "cps": ("paycomm", "CPS 花费", "「CPS 花费」"),
    "spend": ("paycomm_spend", "京准通花费", "「京准通花费」"),
}


class PayCommRunner:
    """花费一览的后台任务（单线程串行 + 进度快照）。

    mode="cps"   → 只抓数据看板的 CPS 付款佣金（按 SKU 逐个 orderData，慢）
    mode="spend" → 只抓京准通广告花费（智能投放商品报表 + 快车计划，快）

    profile_lock 由调用方注入（webapp 的 LoginSession._profile_lock），
    与扫码登录 / 登录态检查 / 保活互斥，避免同一 Profile 被两个上下文打开。
    """

    def __init__(self, mode: str = "cps", profile_lock: threading.Lock | None = None):
        if mode not in MODES:
            raise ValueError(f"未知 mode: {mode}")
        self.mode = mode
        self._lock = threading.Lock()
        self._profile_lock = profile_lock or threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._results: dict[str, dict] = {}
        self._state = {
            "running": False, "jobId": None, "account": None, "range": None,
            "total": 0, "done": 0, "message": "", "error": None,
            "startedAt": None, "finishedAt": None, "cancelled": False, "cached": False,
        }

    # -------------------------------------------------- 对外
    def snapshot(self, with_rows: bool = False) -> dict:
        with self._lock:
            state = dict(self._state)
        job_id = state.get("jobId")
        data = self._results.get(job_id) if job_id else None
        if with_rows and data is None and not state.get("running"):
            # 进程重启 / 换了浏览器标签：把最近一次落盘结果恢复出来
            data = self._latest_from_disk()
            if data:
                state.update(jobId=data.get("jobId"), account=data.get("account"),
                             range=[data.get("start"), data.get("end")],
                             message=state.get("message") or "以上是最近一次保存的结果")
        if with_rows and data:
            state["rows"] = data.get("rows") or []
            state["mode"] = self.mode
            if self.mode == "cps":
                state["daily"] = data.get("daily") or []
                state["totalPay"] = round(sum((float(r.get("payCommAmount") or 0))
                                              for r in state["rows"]), 2)
            else:
                state["spend"] = data.get("spend") or {}
        return state

    def run_background(self, account: str, start: str, end: str, note: str = "") -> dict:
        start, end = norm_range(start, end)
        key = settings.get_account(account)["key"]
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": f"已有{MODES[self.mode][2]}任务在运行，请等它结束"}
            job_id = time.strftime("%Y%m%d%H%M%S") + "-" + self.mode + "-" + key
            self._cancel.clear()
            self._state.update({
                "running": True, "jobId": job_id, "account": key, "range": [start, end],
                "total": 0, "done": 0, "message": note or "正在启动浏览器…", "error": None,
                "startedAt": settings.now_iso(), "finishedAt": None, "cancelled": False,
                "cached": False,
            })
        t = threading.Thread(target=self._work, args=(job_id, key, start, end),
                             name=f"paycomm-{self.mode}-{key}", daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "jobId": job_id, "account": key, "start": start, "end": end}

    def cancel(self) -> dict:
        with self._lock:
            running = bool(self._state.get("running"))
        if running:
            self._cancel.set()
        return {"ok": True, "cancelRequested": running}

    def export(self, job_id: str | None = None, account: str | None = None,
               start: str | None = None, end: str | None = None,
               dim: str = "sku") -> tuple[bytes, str] | None:
        """取结果的 Excel：优先按 (账号, 区间) 找缓存文件，其次内存 / jobId。"""
        data = None
        if account and start and end:
            try:
                data = self.cached_result(account, start, end)
            except Exception:  # noqa: BLE001
                data = None
        job_id = job_id or self.snapshot().get("jobId")
        if not data:
            data = self._results.get(job_id) if job_id else None
        if not data and job_id:
            data = self._load_from_disk(job_id)
        if not data and account and start and end:
            got = self.use_stitched(account, start, end)   # 拼接结果只在内存，重启后这里重建
            if got and got.get("jobId"):
                data = self._results.get(got["jobId"])
        if not data:
            data = self._latest_from_disk()   # 兜底：最近一次落盘结果
        if not data:
            return None
        label = data.get("accountLabel") or data.get("account")
        suffix = "_品类" if dim == "cat" else ""
        if self.mode == "spend":
            xlsx = build_spend_xlsx(data["rows"], data.get("spend") or {},
                                    data["start"], data["end"], label, dim=dim)
            fname = (f"京准通花费{suffix}_{data.get('account')}"
                     f"_{data['start']}_{data['end']}.xlsx")
        else:
            xlsx = build_cps_xlsx(data["rows"], data.get("daily") or [],
                                  data["start"], data["end"], label, dim=dim)
            fname = (f"CPS花费{suffix}_{data.get('account')}"
                     f"_{data['start']}_{data['end']}.xlsx")
        return xlsx, fname

    # -------------------------------------------------- 本地缓存
    # 规则：一次抓取 = 一个 (账号 + 区间) 的落地文件，之后再看同一区间直接读文件，
    #       只有用户显式点「重新抓取」才会重新请求京准通。
    #       不做任何自动过期 / 自动清理，删除只能由用户手动触发。
    def cached_result(self, account: str, start: str, end: str) -> dict | None:
        key = settings.get_account(account)["key"]
        fp, _ = self._disk_paths(key, start, end)
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not data.get("rows"):
            return None
        if self.mode == "spend" and int(data.get("schema") or 1) < SPEND_SCHEMA:
            # 旧口径（无全站营销、漏已删计划）当缓存未命中：文件保留不删，重抓一次即可
            return None
        for r in data.get("rows") or []:
            if not r.get("category"):
                r["category"] = catalog.category_of(r.get("itemName") or "")
        st = fp.stat()
        data.setdefault("start", start)
        data.setdefault("end", end)
        data.setdefault("account", key)
        if not data.get("accountLabel"):
            data["accountLabel"] = settings.get_account(key).get("label") or key
        data["file"] = fp.name
        data["savedAt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        data["sizeKB"] = round(st.st_size / 1024, 1)
        return data

    def use_cache(self, account: str, start: str, end: str) -> dict | None:
        """命中缓存时把结果直接摆到状态里（不启动浏览器、不请求接口）。"""
        start, end = norm_range(start, end)
        key = settings.get_account(account)["key"]
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": f"已有{MODES[self.mode][2]}任务在运行，请等它结束"}
        data = self.cached_result(key, start, end)
        if not data:
            return None
        job_id = data.get("jobId") or f"cache-{self.mode}-{key}-{start}_{end}"
        data["jobId"] = job_id
        self._results[job_id] = data
        rows = data.get("rows") or []
        self._set(running=False, cancelled=False, cached=True, jobId=job_id,
                  account=key, range=[start, end], total=len(rows), done=len(rows),
                  error=None, finishedAt=settings.now_iso(),
                  message=(f"命中本地缓存（{data.get('savedAt')} 抓取，本次未请求京准通）；"
                           f"需要最新数据请点「重新抓取」"))
        return {"ok": True, "cached": True, "jobId": job_id, "account": key,
                "start": start, "end": end, "savedAt": data.get("savedAt"),
                "rows": len(rows)}

    def _parse_name(self, fp: Path) -> tuple[str, str, str] | None:
        """从文件名解析 (账号, 起, 止)；不符合 `<prefix>_<account>_<start>_<end>.json` 就返回 None。"""
        stem = fp.stem
        prefix = self._prefix() + "_"
        if not stem.startswith(prefix):
            return None
        parts = stem[len(prefix):].split("_")
        if len(parts) != 3:
            return None
        acc, s, e = parts
        try:
            dt.date.fromisoformat(s)
            dt.date.fromisoformat(e)
        except Exception:  # noqa: BLE001
            return None
        return acc, s, e

    def _spans(self, key: str) -> list[tuple[str, str]]:
        return sorted({(e["start"], e["end"]) for e in self.cache_entries()
                       if e.get("account") == key and e.get("start") and e.get("end")})

    def use_stitched(self, account: str, start: str, end: str) -> dict | None:
        """用多段相邻缓存拼出完整区间（如 8 月 + 9 月 → 8/1~9/30），完全不请求京准通。

        只接受「首尾正好相接、完整覆盖且不外溢」的组合，避免把区间外的数据算进来。
        """
        start, end = norm_range(start, end)
        key = settings.get_account(account)["key"]
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": f"已有{MODES[self.mode][2]}任务在运行，请等它结束"}
        spans = self._spans(key)
        cur, picked = start, []
        while cur <= end:
            # 同一开始日可能有多段（如 9/1~9/3 与 9/1~9/30）：取「不越过 end 的最长一段」，
            # 既保证覆盖，又不会把区间外的数据带进来
            cands = [(s, e) for s, e in spans if s == cur and e <= end]
            if not cands:
                return None
            hit = max(cands, key=lambda x: x[1])
            picked.append(hit)
            cur = (dt.date.fromisoformat(hit[1]) + dt.timedelta(days=1)).isoformat()
        if len(picked) < 2:
            return None                      # 单段就是精确命中，交给 use_cache
        parts = []
        for s, e in picked:
            data = self.cached_result(key, s, e)
            if not data:
                return None
            parts.append(data)

        rows: dict[int, dict] = {}
        daily: list[dict] = []
        sums: dict[str, float] = {}
        kw_map: dict[str, dict] = {}
        unattr_map: dict[str, dict] = {}
        hints: dict[int, str] = {}
        num_keys = [k for k, _, _ in (FIELDS if self.mode == "cps" else SPEND_FIELDS)]
        ratio_keys = {"payCommRate", "preCommRate", "orderCvr", "roi"}
        for d in parts:
            for r in d.get("rows") or []:
                try:
                    sku = int(r.get("skuId") or 0)
                except (TypeError, ValueError):
                    sku = 0
                if not sku:
                    continue
                tgt = rows.get(sku)
                if tgt is None:
                    tgt = {"skuId": sku, "itemName": r.get("itemName") or "",
                           "category": r.get("category") or ""}
                    for k in num_keys:
                        tgt[k] = 0.0
                    rows[sku] = tgt
                if not tgt["itemName"] and r.get("itemName"):
                    tgt["itemName"] = r["itemName"]
                for k in num_keys:
                    if k in ratio_keys:
                        continue
                    v = r.get(k)
                    if v is None:
                        continue
                    try:
                        tgt[k] += float(v)
                    except (TypeError, ValueError):
                        pass
            daily.extend(d.get("daily") or [])
            sp = d.get("spend") or {}
            for k, v in (sp.get("totals") or {}).items():
                if isinstance(v, (int, float)):
                    sums[k] = sums.get(k, 0.0) + float(v)
            for c in sp.get("keywordRows") or []:
                ck = f"{c.get('campaignId')}|{c.get('campaignName')}"
                item = kw_map.setdefault(ck, {"campaignId": c.get("campaignId"),
                                              "campaignName": c.get("campaignName") or "",
                                              "cost": 0.0})
                item["cost"] += float(c.get("cost") or 0)
            for u in sp.get("unattrRows") or []:
                kk = str(u.get("key") or "other")
                item = unattr_map.setdefault(kk, {"key": kk,
                                                  "name": u.get("name") or "未归因",
                                                  "cost": 0.0})
                item["cost"] += float(u.get("cost") or 0)
            for k2, v2 in (sp.get("hints") or {}).items():
                hints.setdefault(int(k2), v2)

        for r in rows.values():
            for k in num_keys:
                r[k] = None if k in ratio_keys else round(float(r[k]), 2)
        out_rows = sorted(rows.values(),
                          key=lambda x: (x.get("jztCost") if self.mode == "spend"
                                         else x.get("payCommAmount")) or -1, reverse=True)
        daily.sort(key=lambda x: str(x.get("date") or ""))
        kw_rows = sorted(kw_map.values(), key=lambda x: -x["cost"])

        job_id = f"stitch-{self.mode}-{key}-{start}_{end}"
        label = settings.get_account(key).get("label") or key
        payload = {"jobId": job_id, "mode": self.mode, "account": key, "accountLabel": label,
                   "start": start, "end": end, "rows": out_rows,
                   "stitched": [f"{a}~{b}" for a, b in picked],
                   "finishedAt": settings.now_iso()}
        if self.mode == "cps":
            payload["daily"] = daily
        else:
            unattr_rows = sorted((u for u in unattr_map.values() if abs(u["cost"]) >= 0.01),
                                 key=lambda x: -x["cost"])
            for u in unattr_rows:
                u["cost"] = round(u["cost"], 2)
            sums["unattr"] = round(sum(u["cost"] for u in unattr_rows), 2)
            sums["skuCount"] = len(out_rows)
            # 合计直接按行加总（含「未归因」行），避免漏掉新增的列
            sums["total"] = round(sum(float(r.get("jztCost") or 0) for r in out_rows), 2)
            payload["spend"] = {"totals": sums, "keywordRows": kw_rows,
                                "unattrRows": unattr_rows, "hints": hints}
        self._results[job_id] = payload
        pieces = " + ".join(f"{a}~{b}" for a, b in picked)
        self._set(running=False, cancelled=False, cached=True, jobId=job_id,
                  account=key, range=[start, end], total=len(out_rows), done=len(out_rows),
                  error=None, finishedAt=settings.now_iso(),
                  message=f"命中本地缓存拼接（{pieces}），未请求京准通；需要最新数据请点「重新抓取」")
        return {"ok": True, "cached": True, "stitched": True, "jobId": job_id,
                "account": key, "start": start, "end": end,
                "pieces": [f"{a}~{b}" for a, b in picked], "rows": len(out_rows)}

    def cache_entries(self) -> list[dict]:
        """已落地的缓存清单（按同账号+区间去重，按抓取时间倒序）。"""
        out: dict[tuple, dict] = {}
        for fp in self._disk_files():
            parsed = self._parse_name(fp)
            if not parsed:
                continue          # 旧命名 / 不认识的文件不参与（避免一个区间两条记录）
            acc, start, end = parsed
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            rows = data.get("rows") or []
            if not rows:
                continue
            if self.mode == "cps":
                total = sum(float(r.get("payCommAmount") or 0) for r in rows)
            else:
                total = sum(float(r.get("jztCost") or 0) for r in rows)
            st = fp.stat()
            out[(acc, start, end)] = {
                "mode": self.mode,
                "account": acc,
                # 以当前配置的名称为准：账号改名后缓存清单也显示新名字（key 不变，缓存照旧命中）
                "accountLabel": (settings.get_account(acc).get("label")
                                 or data.get("accountLabel") or acc),
                "start": start, "end": end,
                "rows": len(rows), "total": round(total, 2),
                "jobId": data.get("jobId"),
                "savedAt": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "sizeKB": round(st.st_size / 1024, 1),
                "files": [fp.name],
            }
        items = list(out.values())
        items.sort(key=lambda x: x["savedAt"], reverse=True)
        return items

    def delete_cache(self, account: str, start: str, end: str) -> dict:
        """手动删除某账号某区间的缓存（json + 同名 xlsx；兼容旧命名）。"""
        key = settings.get_account(account)["key"]
        jf, xf = self._disk_paths(key, start, end)
        base = jf.parent
        legacy = [base / f"{self._prefix()}_{start}_{end}_{key}.json",
                  base / f"{self._prefix()}_{start}_{end}_{key}.xlsx"]
        deleted: list[str] = []
        for fp in (jf, xf, *legacy):
            if fp.exists():
                try:
                    fp.unlink()
                    deleted.append(fp.name)
                except Exception:  # noqa: BLE001
                    pass
        # 内存里同一份结果也丢掉
        for jid, data in list(self._results.items()):
            if data.get("account") == key and data.get("start") == start and data.get("end") == end:
                self._results.pop(jid, None)
        return {"ok": True, "deleted": deleted,
                "error": None if deleted else "没有找到对应的缓存文件"}

    # -------------------------------------------------- 内部
    def _prefix(self) -> str:
        return MODES[self.mode][0]

    def _disk_paths(self, account: str, start: str, end: str) -> tuple[Path, Path]:
        base = Path(settings.DATA_DIR) / "reports"
        base.mkdir(parents=True, exist_ok=True)
        stem = f"{self._prefix()}_{account}_{start}_{end}"
        return base / f"{stem}.json", base / f"{stem}.xlsx"

    def _disk_files(self) -> list[Path]:
        base = Path(settings.DATA_DIR) / "reports"
        files = list(base.glob(f"{self._prefix()}_*.json"))
        if self.mode == "cps":
            # paycomm_*.json 会把 paycomm_spend_*.json 也匹配进来，排除掉
            files = [p for p in files if not p.name.startswith("paycomm_spend_")]
        return files

    def _load_from_disk(self, job_id: str) -> dict | None:
        for fp in self._disk_files():
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if data.get("jobId") == job_id:
                return data
        return None

    def _latest_from_disk(self) -> dict | None:
        """最近一次落盘结果（进程重启后仍能把上次的表恢复出来）。"""
        files = sorted(self._disk_files(), key=lambda p: p.stat().st_mtime, reverse=True)
        for fp in files[:3]:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if data.get("rows"):
                return data
        return None

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _collect(self, page, account: str, start: str, end: str,
                 on_progress=None) -> tuple[list[dict], dict]:
        """在已打开的京准通页面上抓一个 mode 的数据，返回 (rows, extra)。

        extra = {"spend": {...}}（spend 模式）或 {"daily": [...]}（cps 模式）。
        不写盘、不动运行态；「花费一览」的手动任务与「月度利润」自动补抓共用这一份逻辑。
        """
        def say(msg: str, **kw) -> None:
            if on_progress:
                on_progress(msg)
            else:
                self._set(message=msg, **kw)

        rows: list[dict] = []
        extra: dict = {}
        if self.mode == "spend":
            say("正在读取京准通广告花费（快车：智能投放 / 关键词 / 全站智能推广 + 全站营销）…")
            spend = fetch_spend(page, start, end,
                                settings.get_account(account).get("account_id"))
            names: dict = {}
            try:
                # 只借商品列表补商品名（取不到不影响花费数据）
                goods = list_goods(page, start, end)
                names = {int(g["itemId"]): (g.get("itemName") or "") for g in goods}
            except Exception:  # noqa: BLE001
                names = {}
            hints = spend.get("hints") or {}
            rows = [{"skuId": sku, "itemName": (nm := names.get(sku) or hints.get(sku, "")),
                     "category": catalog.category_of(nm),
                     "jztJstCost": v["jst"], "jztShopCost": v["shop"],
                     "jztKwCost": v["kw"], "jztSwaCost": v["swa"], "jztCost": v["total"]}
                    for sku, v in (spend.get("bySku") or {}).items()]
            # 归不到商品的花费：单独成行（固定的负 SKU 编号，跨区间拼接也能对上），
            # 绝不按比例摊到具体商品上
            for u in spend.get("unattrRows") or []:
                key2 = str(u.get("key") or "other")
                col = {"kw": "jztKwCost", "swa": "jztSwaCost"}.get(key2, "jztShopCost")
                rows.append({"skuId": UNATTR_IDS.get(key2, -9199),
                             "itemName": u.get("name") or "未归因",
                             "category": "未归因",
                             "jztJstCost": 0.0, "jztShopCost": 0.0,
                             "jztKwCost": 0.0, "jztSwaCost": 0.0,
                             col: u.get("cost") or 0.0,
                             "jztCost": u.get("cost") or 0.0})
            rows.sort(key=lambda r: (r.get("jztCost") or -1), reverse=True)
            say(f"京准通花费已取到 {len(rows)} 个 SKU", total=len(rows), done=len(rows))
            extra["spend"] = spend
        else:
            say("正在读取商品列表…")
            goods = list_goods(page, start, end)
            if goods:
                say(f"共 {len(goods)} 个 SKU，正在逐个取数…", total=len(goods))

            for i, g in enumerate(goods, 1):
                if self._cancel.is_set():
                    break
                sku = int(g["itemId"])
                try:
                    od = sku_metrics(page, sku, start, end)
                except Exception as exc:  # noqa: BLE001
                    od = {}
                    say(f"{sku} 取数失败（{type(exc).__name__}），已跳过")
                rec = {"skuId": sku, "itemName": g.get("itemName"),
                       "category": catalog.category_of(g.get("itemName") or "")}
                for k, _, _ in FIELDS:
                    rec[k] = od.get(k)
                rows.append(rec)
                say(f"已完成 {i}/{len(goods)}：{sku} 付款佣金 "
                    f"{float(rec.get('payCommAmount') or 0):,.2f} 元", done=i)
                _sleep(DELAY_SKU)

            if not self._cancel.is_set():
                say("正在取账户分日合计…")
                try:
                    extra["daily"] = daily_total(page, start, end)
                except Exception:  # noqa: BLE001
                    extra["daily"] = []
        return rows, extra

    def _work(self, job_id: str, account: str, start: str, end: str) -> None:
        pw = ctx = None
        rows: list[dict] = []
        extra: dict = {}
        try:
            lock = self._profile_lock
            self._set(message="等待浏览器 Profile 空闲…")
            if not lock.acquire(timeout=600):
                raise RuntimeError("浏览器 Profile 被占用（登录 / 保活进行中），请稍后重试")
            try:
                self._set(message="正在启动浏览器…")
                pw, ctx = launch_persistent(headless=True, account=account)
                page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                page.goto(BOARD_PAGE, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(10000)
                if not session_ok(page):
                    raise RuntimeError("登录态失效：请先到「登录」页扫码，或点控制台的「立即续期」")
                rows, extra = self._collect(page, account, start, end)
            finally:
                try:
                    ctx and ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pw and pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                lock.release()

            spend = extra.get("spend") or {}
            daily = extra.get("daily") or []
            cancelled = self._cancel.is_set()
            if cancelled:
                # 取消 / 进程被重启打断：**绝不落盘**，否则会污染缓存（留下不完整的区间数据）
                self._set(running=False, cancelled=True, finishedAt=settings.now_iso(),
                          message=f"已取消，未写入缓存（本次已取 {len(rows)} 个 SKU 作废）",
                          error=None)
                return
            if not rows:
                raise RuntimeError("没有抓到任何 SKU 数据")

            sort_key = "jztCost" if self.mode == "spend" else "payCommAmount"
            rows.sort(key=lambda r: (r.get(sort_key) if r.get(sort_key) is not None else -1),
                      reverse=True)
            label = settings.get_account(account).get("label") or account

            payload = {"jobId": job_id, "mode": self.mode, "account": account,
                       "schema": SPEND_SCHEMA,
                       "accountLabel": label, "start": start, "end": end, "rows": rows,
                       "finishedAt": settings.now_iso()}
            if self.mode == "spend":
                payload["spend"] = spend
            else:
                payload["daily"] = daily
            self._results[job_id] = payload
            try:
                jf, _ = self._disk_paths(account, start, end)
                jf.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

            if self.mode == "spend":
                total_jzt = round(sum((float(r.get("jztCost") or 0)) for r in rows), 2)
                kw = float(((spend.get("totals") or {}).get("keyword")) or 0)
                detail = (f"京准通花费合计 {total_jzt:,.2f} 元"
                          + (f"（另有未归因关键词 {kw:,.2f} 元）" if kw else ""))
            else:
                total_pay = round(sum((float(r.get("payCommAmount") or 0)) for r in rows), 2)
                detail = f"付款佣金合计 {total_pay:,.2f} 元"
            self._set(running=False, cancelled=False, cached=False,
                      finishedAt=settings.now_iso(), total=len(rows), done=len(rows),
                      message=f"完成：{len(rows)} 个 SKU，{detail}（已写入本地缓存）",
                      error=None)
        except Exception as exc:  # noqa: BLE001
            self._set(running=False, error=f"{type(exc).__name__}: {exc}",
                      finishedAt=settings.now_iso(), message=f"失败：{exc}")


def fetch_into_cache(page, mode: str, account: str, start: str, end: str,
                     on_progress=None) -> dict | None:
    """借一个已打开的京准通页面，同步抓某区间花费并写成与「花费一览」相同的缓存文件。

    「月度利润分析」发现花费缓存缺失时直接调用它补齐，用户不必再去别的页面手动抓一次。
    返回落盘的 payload；一个 SKU 都没抓到（该区间确实没有花费）则返回 None、不写盘。
    """
    start, end = norm_range(start, end)
    key = settings.get_account(account)["key"]
    runner = PayCommRunner(mode)
    rows, extra = runner._collect(page, key, start, end, on_progress)
    if not rows:
        return None
    job_id = time.strftime("%Y%m%d%H%M%S") + "-" + mode + "-" + key
    payload = {"jobId": job_id, "mode": mode, "account": key, "schema": SPEND_SCHEMA,
               "accountLabel": settings.get_account(key).get("label") or key,
               "start": start, "end": end, "rows": rows,
               "finishedAt": settings.now_iso(), "autoFilled": True}
    payload.update(extra)
    try:
        fp, _ = runner._disk_paths(key, start, end)
        fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return payload
