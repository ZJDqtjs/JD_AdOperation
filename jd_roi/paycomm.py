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

# 京准通广告花费列（按 SKU，来自 jst 商品报表 + 全站智能推广计划）
SPEND_FIELDS = [
    ("jztCost", "京准通花费合计(元)", "money"),
    ("jztJstCost", "智能投放花费(元)", "money"),
    ("jztShopCost", "全站智能推广花费(元)", "money"),
    ("jztKwCost", "关键词计划花费(元)", "money"),
]

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


def fetch_spend(page, start: str, end: str, account_id: int | None = None) -> dict:
    """按 SKU 抓京准通广告花费（智能投放 + 全站智能推广），并给出无法归因的部分。

    复用 jd_roi.scrape_jst_report（jst 商品报表，按 skuId 有 cost）与
    jd_roi.scrape_jzt（快车计划列表，全站智能推广计划上带 spuId）。
    实测：同一个智能投放计划的花费在两个接口里完全一致（差额 0.00），口径可直接相加。
    """
    from .scrape_jst_report import fetch as jst_fetch
    from .scrape_jzt import fetch_campaigns

    if not account_id:
        account_id = int(settings.get_account(None).get("account_id") or 0)

    # 快车计划列表：当前页面就在 msa 快车，直接取
    camps = fetch_campaigns(page, start, end)
    _sleep(DELAY_PAGE)
    # 智能投放商品报表：先切到 jst 报表页，保证接口上下文与现网一致
    try:
        page.goto(JST_PAGE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
    except Exception:  # noqa: BLE001
        pass
    jst = jst_fetch(page, "sku", start, end)

    by_sku: dict[int, dict] = {}
    # SKU → 商品线索（从计划名里剥出来的商品名，仅用于没有 CPS 商品名时兜底显示）
    hints: dict[int, str] = {}

    def bucket(sku: int) -> dict:
        return by_sku.setdefault(int(sku), {"jst": 0.0, "shop": 0.0, "kw": 0.0})

    for r in jst.get("rows") or []:
        sku = int(r.get("skuId") or 0)
        if sku:
            bucket(sku)["jst"] += float(r.get("cost") or 0)
            if sku not in hints:
                hints[sku] = _clean_name(r.get("campaignName"))

    unattr = {"keyword": 0.0, "other": 0.0}
    kw_rows: list[dict] = []
    kw_list_total = 0.0
    for c in camps.get("rows") or []:
        cost = float(c.get("cost") or 0)
        ctype = c.get("campaignType")
        spu = int(c.get("spuId") or 0)
        if ctype == 61:
            # 智能投放：已由上面的 jst 商品报表按 SKU 覆盖（同类计划金额一致），不重复计
            continue
        if ctype == 153 and spu:
            bucket(spu)["shop"] += cost
            if spu not in hints:
                hints[spu] = _clean_name(c.get("campaignName"))
        elif ctype == 2:
            # 关键词计划：这里只累计总额（对账用）；真正的 SKU 归因走下面的创意报表
            kw_list_total += cost
        else:
            unattr["other"] += cost

    # 关键词：报表中心「创意」报表 —— 每条创意绑定一个商品（落地页带 SKU），
    # 且包含已删除计划（比计划列表更全，8 月多出 6,012 元已删计划花费）
    kw_total = 0.0
    creatives: list[dict] = []
    try:
        creatives = fetch_creative_rows(page, start, end, account_id)
    except Exception:  # noqa: BLE001
        creatives = []
    for r in creatives:
        if r.get("campaignType") not in (2, None):
            continue
        cost = float(r.get("cost") or 0)
        if cost <= 0:
            continue
        kw_total += cost
        sku = sku_from_creative(r)
        if sku:
            bucket(sku)["kw"] += cost
            if sku not in hints:
                hints[sku] = _clean_name(r.get("adName") or r.get("campaignName"))
        else:
            unattr["keyword"] += cost
            kw_rows.append({"campaignId": r.get("campaignId"),
                            "campaignName": r.get("campaignName") or r.get("adName") or "",
                            "cost": round(cost, 2)})
    if not creatives:
        # 创意报表取不到时退回计划列表口径：关键词花费如实记为「未归因」
        kw_total = kw_list_total
        unattr["keyword"] += kw_list_total
        for c in camps.get("rows") or []:
            cost = float(c.get("cost") or 0)
            if c.get("campaignType") == 2 and cost:
                kw_rows.append({"campaignId": c.get("campaignId"),
                                "campaignName": c.get("campaignName") or "",
                                "cost": round(cost, 2)})

    out: dict[int, dict] = {}
    for sku, v in by_sku.items():
        jst_cost, shop_cost = round(v["jst"], 2), round(v["shop"], 2)
        kw_cost = round(v["kw"], 2)
        out[sku] = {"jst": jst_cost, "shop": shop_cost, "kw": kw_cost,
                    "total": round(jst_cost + shop_cost + kw_cost, 2)}

    totals = {
        "jst": round(sum(v["jst"] for v in out.values()), 2),
        "shopSmart": round(sum(v["shop"] for v in out.values()), 2),
        "keywordTotal": round(kw_total, 2),
        "keywordListTotal": round(kw_list_total, 2),
        "keyword": round(unattr["keyword"], 2),
        "other": round(unattr["other"], 2),
        "skuCount": len(out),
    }
    totals["total"] = round(totals["jst"] + totals["shopSmart"] + totals["keywordTotal"], 2)
    ext = camps.get("ext") or {}
    try:
        totals["campaignTotal"] = round(float(ext.get("cost") or 0), 2)
    except (TypeError, ValueError):
        totals["campaignTotal"] = None
    kw_rows.sort(key=lambda r: r["cost"], reverse=True)
    return {"bySku": out, "totals": totals, "keywordRows": kw_rows,
            "hints": {k: v for k, v in hints.items() if v}}


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
                          "全站智能推广 = 快车计划列表（计划上的 spuId）"])
    ws3.append(["花费合计", totals.get("total")])
    ws3.append(["  其中 智能投放", totals.get("jst")])
    ws3.append(["  其中 关键词计划", totals.get("keywordTotal")])
    ws3.append(["  其中 全站智能推广", totals.get("shopSmart")])
    ws3.append(["关键词-计划列表口径", totals.get("keywordListTotal")])
    ws3.append(["未归因 关键词", totals.get("keyword")])
    ws3.append(["未归因 其他类型", totals.get("other")])
    ws3.append(["快车计划总花费", totals.get("campaignTotal")])
    ws3.append(["未归因说明", "关键词花费按创意落地页的 SKU 归因；只有创意缺落地页时才无法归因，"
                              "这类花费单列在「未归因关键词计划」sheet",
                ])
    ws3.column_dimensions["A"].width = 20
    ws3.column_dimensions["B"].width = 95

    if dim == "cat":
        try:
            wb.remove(wb["SKU广告花费"])
        except KeyError:
            pass

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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

    def run_background(self, account: str, start: str, end: str) -> dict:
        start, end = norm_range(start, end)
        key = settings.get_account(account)["key"]
        with self._lock:
            if self._state.get("running"):
                return {"ok": False, "error": f"已有{MODES[self.mode][2]}任务在运行，请等它结束"}
            job_id = time.strftime("%Y%m%d%H%M%S") + "-" + self.mode + "-" + key
            self._cancel.clear()
            self._state.update({
                "running": True, "jobId": job_id, "account": key, "range": [start, end],
                "total": 0, "done": 0, "message": "正在启动浏览器…", "error": None,
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
                "accountLabel": (data.get("accountLabel")
                                 or settings.get_account(acc).get("label") or acc),
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

    def _work(self, job_id: str, account: str, start: str, end: str) -> None:
        pw = ctx = None
        rows: list[dict] = []
        daily: list[dict] = []
        spend: dict = {}
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

                if self.mode == "spend":
                    self._set(message="正在读取京准通广告花费（智能投放 + 全站智能推广）…")
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
                             "jztKwCost": v["kw"], "jztCost": v["total"]}
                            for sku, v in (spend.get("bySku") or {}).items()]
                    rows.sort(key=lambda r: (r.get("jztCost") or -1), reverse=True)
                    self._set(total=len(rows), done=len(rows))
                else:
                    self._set(message="正在读取商品列表…")
                    goods = list_goods(page, start, end)
                    if goods:
                        self._set(total=len(goods), message=f"共 {len(goods)} 个 SKU，正在逐个取数…")

                    for i, g in enumerate(goods, 1):
                        if self._cancel.is_set():
                            break
                        sku = int(g["itemId"])
                        try:
                            od = sku_metrics(page, sku, start, end)
                        except Exception as exc:  # noqa: BLE001
                            od = {}
                            self._set(message=f"{sku} 取数失败（{type(exc).__name__}），已跳过")
                        rec = {"skuId": sku, "itemName": g.get("itemName"),
                               "category": catalog.category_of(g.get("itemName") or "")}
                        for k, _, _ in FIELDS:
                            rec[k] = od.get(k)
                        rows.append(rec)
                        self._set(done=i, message=f"已完成 {i}/{len(goods)}：{sku} "
                                                  f"付款佣金 {float(rec.get('payCommAmount') or 0):,.2f} 元")
                        _sleep(DELAY_SKU)

                    if not self._cancel.is_set():
                        self._set(message="正在取账户分日合计…")
                        try:
                            daily = daily_total(page, start, end)
                        except Exception:  # noqa: BLE001
                            daily = []
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
