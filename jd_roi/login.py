# -*- coding: utf-8 -*-
"""扫码登录京东账号（SSO，一次登录覆盖京准通/商智），并保存登录态。

登录判定：以 Cookie 中存在 `.jd.com` 域的 `pin` 票据为准，避免页面跳转时序误判。

用法:
    .venv\\Scripts\\python.exe -m jd_roi.login              # 主账号
    .venv\\Scripts\\python.exe -m jd_roi.login --account=b  # 第二个账号
"""
from __future__ import annotations

import sys

from playwright.sync_api import BrowserContext

from . import config
from .browser import has_login, launch_persistent


def _has_login_cookie(context: BrowserContext) -> bool:
    return has_login(context)


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    acc = config.get_account(account)
    print("=" * 70)
    print(f"京东扫码登录  ——  账号 [{acc.get('label')}] ({acc.get('key')})")
    print("=" * 70)
    print("即将打开浏览器窗口，请在【3分钟内】用手机京东App扫码登录。")
    print("登录成功后脚本会自动保存登录态并关闭浏览器。\n", flush=True)

    pw, context = launch_persistent(headless=False, account=account)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(config.LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
        print(f"已打开登录门户: {page.url}", flush=True)
        print("请在此窗口扫码或输入账号密码登录。\n", flush=True)

        deadline_ms = 180_000
        waited = 0
        step = 3000
        logged_in = False
        while waited < deadline_ms:
            try:
                if _has_login_cookie(context):
                    logged_in = True
                    break
                page.wait_for_timeout(step)
                page.bring_to_front()
            except Exception as exc:  # noqa: BLE001
                # 浏览器被用户关闭
                print(f"[中止] 浏览器窗口已关闭: {type(exc).__name__}", flush=True)
                return 3
            waited += step

        if not logged_in:
            print(f"[失败] 等待超时（未检测到 pin），当前URL: {page.url}", flush=True)
            return 2

        print("[成功] 检测到登录票据 pin。", flush=True)

        context.storage_state(path=str(config.STORAGE_STATE))
        print(f"登录态已保存: {config.STORAGE_STATE}", flush=True)

        # 验证京准通
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        print(f"京准通: {page.url}", flush=True)

        # 验证商智
        page.goto(config.SZ_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        sz_ok = "passport.jd.com" not in page.url
        print(f"商智: {page.url}  ->  {'可访问' if sz_ok else '仍跳登录页'}", flush=True)

        context.storage_state(path=str(config.STORAGE_STATE))
        print("[完成] 登录态已更新保存。", flush=True)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
