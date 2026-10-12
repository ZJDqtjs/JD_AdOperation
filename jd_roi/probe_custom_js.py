# -*- coding: utf-8 -*-
"""从自定义报表 SPA JS 扒接口路径。"""
from __future__ import annotations

import re

from . import config
from .browser import launch_profile


def main() -> int:
    pw, ctx = launch_profile(headless=True, account="b")
    page = ctx.new_page()
    try:
        page.goto("https://jzt.jd.com/custom-report/#/create?specificType=0",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)
        urls = page.evaluate("()=>Array.from(document.scripts).map(s=>s.src).filter(Boolean)")
        print("scripts:", len(urls))
        paths = set()
        for u in urls:
            if not u.startswith("http"):
                continue
            try:
                r = page.evaluate(
                    "async (u)=>{const r=await fetch(u,{credentials:'include'});return await r.text();}", u)
            except Exception:  # noqa: BLE001
                continue
            for m in re.findall(r"/dataCenter/[a-zA-Z0-9_/]+", r):
                paths.add(m)
            for m in re.findall(r"customreport/[a-zA-Z0-9_/]+", r):
                paths.add(m)
        print("=== customreport 相关路径 ===")
        for p in sorted(paths):
            print("  ", p)
        # 打印 operateData / template/specific 附近的 JS 片段，还原请求体结构
        for u in urls:
            if not u.startswith("http"):
                continue
            try:
                r = page.evaluate(
                    "async (u)=>{const r=await fetch(u,{credentials:'include'});return await r.text();}", u)
            except Exception:  # noqa: BLE001
                continue
            for kw in ("operateData", "template/specific"):
                for m in re.finditer(re.escape(kw), r):
                    s = max(0, m.start() - 700)
                    print(f"\n--- {kw} @ {u.rsplit('/', 1)[-1]} ---")
                    print(r[s:m.start() + 400])
                    break
        return 0
    finally:
        ctx.close()
        pw.stop()


if __name__ == "__main__":
    raise SystemExit(main())
