# -*- coding: utf-8 -*-
"""诊断：读取持久化 Profile 的登录态，测试目标站点访问。

用法:
    .venv\\Scripts\\python.exe -m jd_roi.diagnose [--headed]
"""
from __future__ import annotations

import sys

from . import config
from .browser import launch_persistent


def dump(context, label: str) -> None:
    cookies = context.cookies()
    print(f"\n===== {label}: 共 {len(cookies)} 个 cookie =====")
    interesting = ("pt_", "pin", "jzt", "sz", "inquiry", "sso", "token", "uuid", "unick")
    for c in sorted(cookies, key=lambda x: (x["domain"], x["name"])):
        nm = c["name"]
        if any(k in nm.lower() for k in interesting):
            v = c["value"] or ""
            print(f"  * {nm} @ {c['domain']} = {v[:12]}...({len(v)})")
    print("  --- 全部 cookie 名 ---")
    print("  " + ", ".join(sorted({c["name"] for c in cookies})))


def main() -> int:
    headed = "--headed" in sys.argv
    pw, context = launch_persistent(headless=not headed)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        dump(context, "初始 profile")

        for label, url in (
            ("登录门户 jxinquiry", config.LOGIN_URL),
            ("京准通 jzt", config.JZT_HOME),
            ("商智 jdsz", config.SZ_HOME),
        ):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(8000)
                print(f"\n[{label}] 最终URL: {page.url}")
                print(f"           title: {page.title()}")
                print(f"           login页? {'passport.jd.com' in page.url}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n[{label}] 异常: {type(exc).__name__}: {exc}")

        dump(context, "访问后 profile")
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
