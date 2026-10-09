# -*- coding: utf-8 -*-
"""浏览器上下文与登录态辅助。"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import config, settings


def _ensure_dirs() -> None:
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    config.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 关键：把 tempfile 指到可写目录，否则 Playwright 建 artifacts 目录会 EPERM
    settings.ensure_dirs()


def launch_persistent(headless: bool | None = None, account: str | None = None):
    """启动持久化上下文（扫码登录用）。

    account: 账号 key（多账号时每个账号一个独立 profile）。
    返回 (playwright, context)。调用方负责 context.close() / playwright.stop()。
    """
    _ensure_dirs()
    headless = config.HEADLESS if headless is None else headless
    acc = config.get_account(account)
    user_data_dir = acc.get("user_data_dir") or str(config.USER_DATA_DIR)
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, context


def launch_with_state(headless: bool | None = None, account: str | None = None):
    """按账号打开**持久化 Profile**（凭据唯一权威来源）。

    历史说明：早期版本用 storage_state(jd_state.json) 起临时上下文，但那份快照
    会独立过期、且与实际在用的 Profile 不同步，是「登录态误判」的来源之一。
    现在抓取/复跑一律走持久化 Profile（含 cookies + localStorage，最稳），
    本函数保留签名以兼容旧调用，内部已改为委托给 launch_persistent()。

    返回 (playwright, context)；不再返回 browser（持久化上下文自带）。
    """
    _ensure_dirs()
    pw, context = launch_persistent(headless=headless, account=account)
    return pw, context


def has_login(context: BrowserContext) -> bool:
    """判断是否已登录京东：以 .jd.com 域的 pin（账号名）cookie 为准。

    注：京东现行登录票据为 pin/pinId/thor/sdtoken，不再使用 pt_key。
    """
    for c in context.cookies():
        if c.get("name") == "pin" and c.get("domain", "").endswith("jd.com") and c.get("value"):
            return True
    return False


_JS_LOGININFO = """
async () => {
  try {
    const r = await fetch('https://jzt-api.jd.com/common/logininfo', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type':'application/json','accept':'application/json, text/plain, */*',
        'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
      body: JSON.stringify({requestFrom: 0})});
    const j = await r.json();
    return j && j.code === 1;
  } catch (e) { return false; }
}
"""


def session_ok(page: Page, retries: int = 2) -> bool:
    """真正问一次服务端：cookie 还在但服务端会话过期时 has_login 会误判。

    必须以 jzt-api /common/logininfo 返回 code==1 为准。
    """
    for _ in range(max(retries, 1)):
        try:
            if page.evaluate(_JS_LOGININFO):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def launch_profile(headless: bool | None = None, account: str | None = None):
    """使用持久化 Profile 抓取（含 cookies + localStorage，最稳）。

    account: 账号 key，多账号时用于选择对应 Profile。
    返回 (playwright, context)。
    """
    return launch_persistent(headless=headless, account=account)


def is_logged_in(context: BrowserContext, page: Page | None = None) -> bool:
    """通过访问京准通判断登录态是否有效。"""
    if not has_login(context):
        return False
    page = page or context.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        url = page.url
        return "passport.jd.com" not in url and "login" not in url.lower()
    except Exception:
        return False

