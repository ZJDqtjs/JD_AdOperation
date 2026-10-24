# -*- coding: utf-8 -*-
"""命令行导出：CPS 花费 / 京准通花费（两个独立文件），与网页同一套逻辑。

实现全部复用 jd_roi/paycomm.py，避免两处口径漂移。

用法（容器内）：
    docker cp scripts/export_paycomm_by_sku.py jd-roi-dev:/tmp/ && \
    docker exec -w /app -e PYTHONPATH=/app jd-roi-dev \
      python /tmp/export_paycomm_by_sku.py 2026-09-01 2026-09-30 main [cps|spend|both]

产物：data/reports/paycomm_<acc>_<start>_<end>.xlsx      （CPS）
      data/reports/paycomm_spend_<acc>_<start>_<end>.xlsx（京准通花费）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jd_roi import paycomm, settings
from jd_roi.browser import launch_persistent, session_ok


def _cps(page, start: str, end: str, account: str) -> dict:
    goods = paycomm.list_goods(page, start, end)
    print(f"[cps] {len(goods)} 个 SKU（{start}~{end}）")
    rows: list[dict] = []
    for i, g in enumerate(goods, 1):
        sku = int(g["itemId"])
        try:
            od = paycomm.sku_metrics(page, sku, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"  [fail] {sku}: {exc}")
            od = {}
        rec = {"skuId": sku, "itemName": g.get("itemName")}
        for k, _, _ in paycomm.FIELDS:
            rec[k] = od.get(k)
        rows.append(rec)
        print(f"  [{i}/{len(goods)}] {sku} 佣金={rec.get('payCommAmount')}")
        paycomm._sleep(paycomm.DELAY_SKU)
    try:
        daily = paycomm.daily_total(page, start, end)
    except Exception:  # noqa: BLE001
        daily = []
    return {"rows": rows, "daily": daily}


def _spend(page, start: str, end: str, account: str) -> dict:
    print("[spend] 读取京准通广告花费…")
    spend = paycomm.fetch_spend(page, start, end,
                                settings.get_account(account).get("account_id"))
    names: dict = {}
    try:
        names = {int(g["itemId"]): (g.get("itemName") or "")
                 for g in paycomm.list_goods(page, start, end)}
    except Exception:  # noqa: BLE001
        names = {}
    hints = spend.get("hints") or {}
    rows = [{"skuId": sku, "itemName": names.get(sku) or hints.get(sku, ""),
             "jztJstCost": v["jst"], "jztShopCost": v["shop"], "jztKwCost": v["kw"],
             "jztCost": v["total"]}
            for sku, v in (spend.get("bySku") or {}).items()]
    rows.sort(key=lambda r: (r.get("jztCost") or -1), reverse=True)
    return {"rows": rows, "spend": spend}


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    start, end = paycomm.norm_range(args[0] if args else "2026-08-01",
                                    args[1] if len(args) > 1 else "2026-08-31")
    account = args[2] if len(args) > 2 else "main"
    which = (args[3] if len(args) > 3 else "both").lower()

    pw, ctx = launch_persistent(headless=True, account=account)
    page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
    try:
        page.goto(paycomm.BOARD_PAGE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)
        if not session_ok(page):
            print("[fail] 登录态失效，先跑 ./scripts/dev_sync_auth.sh main")
            return 2

        label = settings.get_account(account).get("label") or account
        base = Path(settings.DATA_DIR) / "reports"
        base.mkdir(parents=True, exist_ok=True)

        if which in ("cps", "both"):
            d = _cps(page, start, end, account)
            stem = f"paycomm_{account}_{start}_{end}"
            (base / f"{stem}.json").write_text(json.dumps(
                dict(d, account=account, start=start, end=end, mode="cps"),
                ensure_ascii=False), encoding="utf-8")
            (base / f"{stem}.xlsx").write_bytes(
                paycomm.build_cps_xlsx(d["rows"], d["daily"], start, end, label))
            print(f"[OK] CPS {len(d['rows'])} 个 SKU -> {base / (stem + '.xlsx')}")

        if which in ("spend", "both"):
            d = _spend(page, start, end, account)
            stem = f"paycomm_spend_{account}_{start}_{end}"
            (base / f"{stem}.json").write_text(json.dumps(
                dict(d, account=account, start=start, end=end, mode="spend"),
                ensure_ascii=False), encoding="utf-8")
            (base / f"{stem}.xlsx").write_bytes(
                paycomm.build_spend_xlsx(d["rows"], d["spend"], start, end, label))
            t = (d["spend"].get("totals") or {})
            print(f"[OK] 花费 {len(d['rows'])} 个 SKU | 合计 {t.get('total', 0):,.2f}"
                  f"（未归因关键词 {t.get('keyword', 0):,.2f}） -> {base / (stem + '.xlsx')}")
        return 0
    finally:
        ctx.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
