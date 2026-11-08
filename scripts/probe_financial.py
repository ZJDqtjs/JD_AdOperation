# -*- coding: utf-8 -*-
"""探测京准通「财务记录 / 首页」页面的接口与数据，用于梳理「账号广告费流水表」。

用法（容器内）：
    python /tmp/probe_financial.py --account=main --out=/data/financial_probe

产出：
    <out>/api_<page>_<n>_<slug>.json   每个被捕获的接口响应体
    <out>/report.json                  接口清单 + 页面文本快照
设计：
    * 只读：不点任何「充值/转账/提现」按钮，纯浏览 + 抓响应
    * 每页等 15s 让它自己发完请求，再滚动一次触发懒加载
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from jd_roi.browser import launch_persistent  # noqa: E402

PAGES = {
    "home": "https://jzt.jd.com/home/#/index",
    "fin": "https://jzt.jd.com/account/#/financialRecord",
}
# 只关心业务接口：静态资源、埋点、上报一律跳过
SKIP = re.compile(r"\.(js|css|png|jpe?g|gif|svg|woff2?|ico)(\?|$)", re.I)
SKIP_HINT = ("/log", "monitor", "beacon", "sgm", "unionclick", "misc/")
KEEP_HINT = ("jzt-api", "api.jd.com", "/account", "financial", "balance", "wallet",
             "recharge", "cost", "bill", "fund", "trade", "consume")


def _arg(argv: list[str], name: str, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def _slug(url: str, n: int) -> str:
    p = re.sub(r"^https?://[^/]+", "", url.split("?")[0])
    p = re.sub(r"[^A-Za-z0-9]+", "_", p).strip("_")[:80]
    return f"{n:03d}_{p or 'root'}"


def main() -> int:
    account = _arg(sys.argv[1:], "account", "main")
    out = Path(_arg(sys.argv[1:], "out", "/data/financial_probe"))
    out.mkdir(parents=True, exist_ok=True)

    pw, context = launch_persistent(headless=True, account=account)
    report: dict = {"account": account, "pages": {}}
    try:
        for name, url in PAGES.items():
            items: list[dict] = []
            page = context.new_page()

            def on_resp(resp, items=items, page_key=name):
                try:
                    u = resp.url
                    if SKIP.search(u):
                        return
                    if not any(k in u.lower() for k in KEEP_HINT):
                        return
                    if any(k in u.lower() for k in SKIP_HINT):
                        return
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" not in ctype and "text" not in ctype:
                        return
                    body = resp.text()
                    n = len(items) + 1
                    fp = out / f"api_{page_key}_{_slug(u, n)}.json"
                    try:
                        data = json.loads(body)
                        fp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                    except Exception:  # noqa: BLE001
                        data = None
                        fp.write_text(body[:200000], encoding="utf-8")
                    items.append({"url": u, "status": resp.status,
                                  "method": resp.request.method,
                                  "req": (resp.request.post_data or "")[:4000],
                                  "file": fp.name, "keys": sorted(data.keys())[:12] if isinstance(data, dict) else None})
                except Exception as e:  # noqa: BLE001
                    items.append({"url": resp.url, "error": f"{type(e).__name__}: {e}"})

            page.on("response", on_resp)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(15000)
                for _ in range(2):
                    page.mouse.wheel(0, 900)
                    page.wait_for_timeout(3000)
                text = page.inner_text("body")
            except Exception as e:  # noqa: BLE001
                text = f"__ERR__ {type(e).__name__}: {e}"
            report["pages"][name] = {"url": url, "final_url": page.url,
                                     "text": text[:20000], "apis": items}
            print(f"[{name}] final={page.url} apis={len(items)}")
            for it in items:
                print("   ", it.get("status"), it.get("method"), it.get("url", "")[:150])
            page.close()
    finally:
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        context.close()
        pw.stop()
    print(f"[OK] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
