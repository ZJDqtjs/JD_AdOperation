# -*- coding: utf-8 -*-
"""探测京准通前端 JS 中的接口路径，用于定位「编辑计划」的读/写接口。

用法:
    python -m jd_roi.probe_msa_api --account=b
    python -m jd_roi.probe_msa_api --account=b --url=https://jzt.jd.com/jst/#/report/account --grep=jst
输出:
    data/<acct>/msa_api_paths_<acct>.json  —— 去重后的接口路径，按关键词分类
"""
from __future__ import annotations

import json
import re
import sys

from . import config, store
from .browser import launch_profile

PAT = re.compile(r"""["'`]((?:/[A-Za-z0-9_\-]+){2,}(?:/[A-Za-z0-9_\-]+)*)["'`]""")
KEY = ["promolist", "campaign", "plan", "budget", "roi", "price", "save", "update",
       "edit", "modify", "submit", "detail", "info", "query", "list"]


def _arg(argv: list[str], name: str) -> str | None:
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return None


def main() -> int:
    account, rest = config.parse_account(sys.argv[1:])
    url = _arg(sys.argv[1:], "url") or config.JZT_HOME
    grep = _arg(sys.argv[1:], "grep")
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    texts: dict[str, str] = {}

    def grab(resp) -> None:
        try:
            if resp.request.resource_type == "script" and resp.url.endswith(".js") and "jd.com" in resp.url:
                texts[resp.url] = resp.text()
        except Exception:  # noqa: BLE001
            pass

    page.on("response", grab)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)
        for _ in range(3):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(2500)
        print(f"URL: {page.url}")
        print(f"捕获 JS: {len(texts)} 个")
        try:
            hrefs = page.eval_on_selector_all(
                "a[href]", "els=>els.map(e=>e.getAttribute('href'))")
            links = sorted({h for h in hrefs if h and ("#/" in h or "jst" in h or "campaign" in h or "plan" in h)})
            if links:
                print("页面内路由链接:")
                for h in links[:40]:
                    print("   ", h)
        except Exception:  # noqa: BLE001
            pass
        # 把所有已加载 chunk 的路径落地，便于离线 grep
        try:
            all_paths = []
            for m in PAT.finditer("".join(texts.values())):
                all_paths.append(m.group(1))
            store.save(f"api_all_paths_{account or 'main'}",
                       sorted({p for p in all_paths if not p.lower().endswith((".js", ".css", ".png", ".svg"))})[:8000])
        except Exception:  # noqa: BLE001
            pass
    finally:
        found: dict[str, list[str]] = {}
        for u, body in texts.items():
            for m in PAT.finditer(body or ""):
                p = m.group(1)
                low = p.lower()
                if grep and grep.lower() not in low:
                    continue
                if not any(k in low for k in KEY):
                    continue
                if low.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff")):
                    continue
                if any(x in low for x in ("/log", "/monitor", "/beacon")):
                    continue
                found.setdefault(p, []).append(u.split("/")[-1][:40])
        tag = (grep or "all") + "_" + (account or "main")
        store.save(f"api_paths_{tag}", {p: sorted(set(v))[:5] for p, v in sorted(found.items())})
        print(f"[OK] {len(found)} 条候选路径 -> data/api_paths_{tag}.json\n")
        for p, src in found.items():
            if any(k in p.lower() for k in ("update", "save", "edit", "modify", "add", "batch")):
                print(f"  {p}\n      <- {sorted(set(src))[:3]}")
        context.close()
        pw.stop()
    return 0



if __name__ == "__main__":
    sys.exit(main())
