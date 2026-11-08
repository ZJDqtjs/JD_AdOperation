# -*- coding: utf-8 -*-
"""抓取京准通「账户中心-财务记录」的资金流水（充值/消费/分配/冻结/余额）。

用法（容器内，逐账号抓）：
    python /tmp/fetch_cashflow.py --account=main --days=30 --out=/data/cashflow
    python /tmp/fetch_cashflow.py --all --days=30 --out=/data/cashflow

产出：<out>/<account>.json
    {
      "account": "main", "label": "主账号", "account_id": 10000000001,
      "range": {"begin": "2026-10-09", "end": "2026-11-08"},
      "balance": {...allbalance 返回...},
      "pins": [...子账户...],
      "recharge": [...充值流水...],
      "consume": [...消费流水...],
      "assign": [...分配流水...],
      "freeze": [...冻结流水...],
      "summary": {...消费汇总...}
    }

设计要点：
    * 参数一律「偷」页面自己发的真实请求体（orderTypes 这种长列表只有前端知道），
      再只改日期/分页，避免瞎猜参数导致返回空。
    * 全程只读 GET/POST 查询接口，不点任何充值/转账/提现按钮。
    * 请求间隔 0.8~1.6s 随机，避免风控。
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, "/app")

from jd_roi import settings  # noqa: E402
from jd_roi.browser import launch_persistent, session_ok  # noqa: E402

URL = "https://jzt.jd.com/account/#/financialRecord"
API = "https://atoms-api.jd.com/financecore/"
TABS = ["消费记录", "分配记录", "冻结记录", "充值记录"]

# 需要偷模板的接口（key 用于匹配 URL 片段）
TPL_KEYS = ["account/cashrecharge/list", "account/cashrecharge/amountall/get",
            "subaccount/cashconsume/list", "subaccount/consumer/sumCost/query",
            "account/cashassign/list", "subaccount/freeze/list",
            "subaccount/subpin/list", "subaccount/allbalance/get"]

JS_CALL = """async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json',
              'accept': 'application/json, text/plain, */*',
              'referer': 'https://jzt.jd.com/account/',
              'language': 'zh_CN', 'siteid': '0', 'loginmode': '0'},
    body: JSON.stringify(body || {})});
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return {code: -1, msg: t.slice(0, 300)}; }
}"""


def _arg(argv: list[str], name: str, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def _sleep():
    time.sleep(0.8 + random.random() * 0.8)


def _rows(payload) -> tuple[list, int]:
    """把各种嵌套的 {data:{data:[...], paginator:{total}}} 摊平成 (rows, total)。"""
    d = (payload or {}).get("data")
    total = 0
    for _ in range(4):
        if isinstance(d, dict):
            total = total or (d.get("paginator") or {}).get("total") or d.get("total") or 0
            if isinstance(d.get("data"), list):
                return d["data"], total or len(d["data"])
            if isinstance(d.get("list"), list):
                return d["list"], total or len(d["list"])
            d = d.get("data")
        elif isinstance(d, list):
            return d, total or len(d)
        else:
            break
    return [], total


def collect_templates(page) -> dict:
    tpl: dict = {}

    def on_resp(resp):
        u = resp.url
        for k in TPL_KEYS:
            if k in u and k not in tpl:
                try:
                    tpl[k] = json.loads(resp.request.post_data or "{}")
                except Exception:  # noqa: BLE001
                    tpl[k] = {}

    page.on("response", on_resp)
    for tab in TABS:
        try:
            page.locator(f"text={tab}").first.click(timeout=8000)
            page.wait_for_timeout(5000)
        except Exception:  # noqa: BLE001
            pass
    return tpl


def fetch_all(page, api: str, base: dict, begin: str, end: str,
              page_size: int = 1000, max_pages: int = 30) -> list:
    out: list = []
    p = 1
    while p <= max_pages:
        body = dict(base or {})
        body.update({"beginDate": begin, "endDate": end, "page": p, "pageSize": page_size})
        got = page.evaluate(JS_CALL, {"url": API + api, "body": body})
        rows, total = _rows(got)
        out.extend(rows)
        # 只按「本页是否拿满」判断翻页：paginator 的层级各接口不一致，不能当唯一依据
        if not rows or len(rows) < page_size:
            break
        p += 1
        _sleep()
    return out


def run_account(key: str, days: int, out_dir: Path) -> dict:
    acc = settings.get_account(key)
    begin_d = settings.today() - timedelta(days=days - 1)
    begin, end = begin_d.isoformat(), settings.today().isoformat()
    pw, context = launch_persistent(headless=True, account=key)
    try:
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)
        if not session_ok(page):
            raise RuntimeError("登录态失效，需要先扫码登录")
        tpl = collect_templates(page)

        # 余额 / 子账户：拿不到模板就用最小参数
        balance = page.evaluate(JS_CALL, {"url": API + "subaccount/allbalance/get", "body": {}})
        _sleep()
        pins_payload = page.evaluate(JS_CALL, {
            "url": API + "subaccount/subpin/list",
            "body": tpl.get("subaccount/subpin/list") or
                    {"page": 1, "pageSize": 65535, "dimension": 1, "sortType": 3}})
        pin_rows, _ = _rows(pins_payload)
        _sleep()

        recharge = fetch_all(page, "account/cashrecharge/list",
                             tpl.get("account/cashrecharge/list") or {}, begin, end)
        consume = fetch_all(page, "subaccount/cashconsume/list",
                            tpl.get("subaccount/cashconsume/list") or {}, begin, end)
        assign = fetch_all(page, "account/cashassign/list",
                           tpl.get("account/cashassign/list") or {}, begin, end)

        # 冻结记录按 pin 查；用第一个 pin 即可覆盖账户级（页面默认就是这个）
        freeze: list = []
        fbase = dict(tpl.get("subaccount/freeze/list") or {})
        freeze_pin = (pin_rows[0].get("pinName") if pin_rows else "") or ""
        fbase["pin"] = freeze_pin
        freeze = fetch_all(page, "subaccount/freeze/list", fbase, begin, end)

        # 消费汇总（按期 + 按资金类型）
        sbase = dict(tpl.get("subaccount/consumer/sumCost/query") or {})
        sbase.pop("page", None)
        sbase.pop("pageSize", None)
        summary = page.evaluate(JS_CALL, {"url": API + "subaccount/consumer/sumCost/query",
                                          "body": dict(sbase, beginDate=begin, endDate=end)})
        _sleep()
        info = page.evaluate(JS_CALL, {"url": API + "subaccount/consumer/info",
                                       "body": {"beginDate": begin, "endDate": end,
                                                "timeFilterType": 0}})

        data = {
            "account": key, "label": acc.get("label") or key,
            "account_id": acc.get("account_id") or 0,
            "range": {"begin": begin, "end": end, "days": days},
            "fetched_at": settings.now_iso(),
            "templates": tpl,
            "balance": (balance or {}).get("data"),
            "pins": pin_rows,
            "summary": (summary or {}).get("data"),
            "consume_info": (info or {}).get("data"),
            "recharge": recharge, "consume": consume,
            "assign": assign, "freeze": freeze,
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        print(f"[{key}] {acc.get('label')} {begin}~{end} 余额="
              f"{(data['balance'] or {}).get('cashBalance')} "
              f"充值{len(recharge)} 消费{len(consume)} 分配{len(assign)} 冻结{len(freeze)}",
              flush=True)
        return data
    finally:
        try:
            context.close()
        finally:
            pw.stop()


def main() -> int:
    argv = sys.argv[1:]
    out_dir = Path(_arg(argv, "out", "/data/cashflow"))
    days = int(_arg(argv, "days", "30") or 30)
    if any(a == "--all" for a in argv):
        keys = [a["key"] for a in settings.ACCOUNTS]
    else:
        keys = [(_arg(argv, "account", settings.MAIN_ACCOUNT) or settings.MAIN_ACCOUNT)]
    print(f"账号: {keys}  区间: 近 {days} 天", flush=True)
    for i, k in enumerate(keys):
        try:
            run_account(k, days, out_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[{k}] 失败：{type(e).__name__}: {e}", flush=True)
        if i < len(keys) - 1:
            time.sleep(8 + random.random() * 5)     # 账号之间喘一口，别连打
    return 0


if __name__ == "__main__":
    sys.exit(main())
