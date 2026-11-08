# -*- coding: utf-8 -*-
"""逐个点击「财务记录」页的 tab，摸清每个 tab 背后的接口与表格列。

用法（容器内）：
    python /tmp/probe_fin_tabs.py --account=main --out=/data/financial_probe/tabs
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from jd_roi.browser import launch_persistent  # noqa: E402

URL = "https://jzt.jd.com/account/#/financialRecord"
TABS = ["收支汇总", "充值记录", "分配记录", "冻结记录", "消费记录",
        "货款账扣", "其他支出记录"]
KEEP = ("financecore", "jzt-api.jd.com")


def _arg(argv: list[str], name: str, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def main() -> int:
    account = _arg(sys.argv[1:], "account", "main")
    out = Path(_arg(sys.argv[1:], "out", "/data/financial_probe/tabs"))
    out.mkdir(parents=True, exist_ok=True)
    pw, context = launch_persistent(headless=True, account=account)
    report: dict = {}
    try:
        page = context.new_page()
        bucket: list[dict] = []

        def on_resp(resp):
            try:
                u = resp.url
                if not any(k in u for k in KEEP):
                    return
                if "/cashrecharge/" not in u and "consume" not in u and "bill" not in u \
                        and "freeze" not in u and "assign" not in u and "deduct" not in u:
                    return
                body = resp.text()
                try:
                    data = json.loads(body)
                except Exception:  # noqa: BLE001
                    return
                d = data.get("data")
                n = 0
                if isinstance(d, dict):
                    n = len(d.get("data") or d.get("list") or d.get("records") or []) \
                        if isinstance(d, dict) else 0
                bucket.append({"url": u.split("?")[0], "req": (resp.request.post_data or "")[:1500],
                               "total": (d or {}).get("total") if isinstance(d, dict) else None,
                               "rows": n, "body": data})
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_resp)
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)

        for tab in TABS:
            before = len(bucket)
            try:
                loc = page.locator(f"text={tab}").first
                loc.click(timeout=8000)
            except Exception as e:  # noqa: BLE001
                report[tab] = {"click_error": f"{type(e).__name__}: {e}"}
                continue
            page.wait_for_timeout(7000)
            new = bucket[before:]
            tables = []
            for sel in ("table", ".el-table", ".jad-table"):
                try:
                    els = page.locator(sel)
                    for i in range(min(els.count(), 3)):
                        t = els.nth(i).inner_text()
                        if t.strip():
                            tables.append(t[:6000])
                except Exception:  # noqa: BLE001
                    pass
            report[tab] = {"apis": [{k: v for k, v in it.items() if k != "body"} for it in new],
                           "tables": tables[:3],
                           "text": page.inner_text("body")[:4000]}
            tag = re.sub(r"\W+", "_", tab)
            for i, it in enumerate(new, 1):
                (out / f"{tag}_{i:02d}.json").write_text(
                    json.dumps(it["body"], ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{tab}] +{len(new)} api, tables={len(tables)}")
            for it in new:
                print("    ", it["url"], "rows=", it["rows"], "total=", it["total"], "req=", it["req"][:120])
    finally:
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        context.close()
        pw.stop()
    print(f"[OK] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
