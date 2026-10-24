# -*- coding: utf-8 -*-
"""浏览器上下文与登录态辅助。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import config, settings


def _ensure_dirs() -> None:
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    config.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 关键：把 tempfile 指到可写目录，否则 Playwright 建 artifacts 目录会 EPERM
    settings.ensure_dirs()


_HEADLESS_TOKEN = "HeadlessChrome"
# 无头时 Chromium 自报家门的字样会出现在三处，必须一起换掉，少一处都白改：
#   1. navigator.userAgent                → 实测 …HeadlessChrome/153.0.8010.12…
#   2. Sec-CH-UA 请求头                    → 实测 "HeadlessChrome";v="153"（服务端每个请求都能看到）
#   3. navigator.userAgentData.brands     → 实测 [{brand:'HeadlessChrome'…}]（HTTPS 页面 JS 可读）
_CHROME_BRAND = "Google Chrome"
# 进程内缓存探测到的真实指纹（见 _headless_fp）
_FP_CACHE: dict = {}

_JS_PROBE_FP = """() => {
  const d = navigator.userAgentData;
  return {
    ua: navigator.userAgent,
    brands: d ? d.brands.map(b => ({brand: b.brand, version: b.version})) : null,
  };
}"""


def _ua_data_fix(brands: list) -> str:
    """把 navigator.userAgentData 里的无头品牌名改成 Google Chrome。

    JS 读到的 brands 是 C++ 侧独立存的，改 UA 字符串和请求头都不影响它，
    必须在页面脚本执行前用 init script 覆写。
    """
    return """(() => {
  const NAME = {HeadlessChrome: 'Google Chrome'};
  const fix = list => list.map(x => NAME[x.brand] ? {brand: NAME[x.brand], version: x.version} : x);
  const uad = navigator.userAgentData;
  if (!uad) return;
  const proto = Object.getPrototypeOf(uad);
  try { Object.defineProperty(proto, 'brands',
        {configurable: true, get: () => fix(%s)}); } catch (e) {}
  try {
    const orig = uad.getHighEntropyValues.bind(uad);
    Object.defineProperty(proto, 'getHighEntropyValues', {configurable: true, value: async h => {
      const v = await orig(h);
      if (Array.isArray(v.fullVersionList)) v.fullVersionList = fix(v.fullVersionList);
      return v;
    }});
  } catch (e) {}
})();""" % json.dumps(brands, ensure_ascii=False)


def _headless_fp(pw) -> dict:
    """探测一次真实指纹，再只把「无头」这一件事抹掉（进程内只探测一次）。

    **平台段保持真实**（Linux 就写 Linux）：伪造成 Windows 会和 navigator.platform、
    字体列表、WebGL 串打架，反而更容易被比对出来。只否认无头，其余全部说真话。
    """
    if _FP_CACHE:
        return _FP_CACHE
    fp: dict = {}
    browser = None
    try:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        try:
            page = ctx.new_page()
            # userAgentData 只在安全上下文可见：用 http://localhost（本地回环算安全上下文）
            # 并就地伪造响应，不发真实请求、不依赖外网
            page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html",
                                                   body="<html></html>"))
            page.goto("http://localhost/")
            fp = page.evaluate(_JS_PROBE_FP) or {}
        finally:
            ctx.close()
    except Exception:  # noqa: BLE001
        fp = {}
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    ua = (fp.get("ua") or "").replace(_HEADLESS_TOKEN, "Chrome")
    # 真 Chrome 从 101 起 UA 里只报主版本（Chrome/153.0.0.0），Chromium 无头却给完整
    # 构建号（Chrome/153.0.8010.12）——不抹掉就是另一处「我和真 Chrome 不一样」
    m = re.search(r"Chrome/(\d+)\.", ua)
    if m:
        ua = re.sub(r"Chrome/[\d.]+", f"Chrome/{m.group(1)}.0.0.0", ua)

    brands = [{"brand": _CHROME_BRAND if b.get("brand") == _HEADLESS_TOKEN else b.get("brand"),
               "version": b.get("version")}
              for b in (fp.get("brands") or [])]
    _FP_CACHE.update({"ua": ua, "brands": brands})
    return _FP_CACHE


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
    # 只有无头才需要伪装；有头（本地扫码登录）本来就是普通 Chrome 的 UA，不动它
    extra: dict = {}
    if headless:
        fp = _headless_fp(pw)
        if fp.get("ua"):
            extra["user_agent"] = fp["ua"]
        if fp.get("brands"):
            # 服务端从 Sec-CH-UA 直接就能看出无头，必须连请求头一起换
            extra["extra_http_headers"] = {
                "Sec-CH-UA": ", ".join(f'"{b["brand"]}";v="{b["version"]}"'
                                       for b in fp["brands"]),
            }
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        # 容器里 TZ 已是 Asia/Shanghai，但浏览器上下文时区默认跟随系统，
        # 可能变成 Etc/GMT-8 这种异常名字，显式钉死更稳
        timezone_id=settings.TZ,
        args=["--disable-blink-features=AutomationControlled"],
        **extra,
    )
    if headless and _FP_CACHE.get("brands"):
        context.add_init_script(_ua_data_fix(_FP_CACHE["brands"]))
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


_JS_LOGININFO_DATA = """
async () => {
  try {
    const r = await fetch('https://jzt-api.jd.com/common/logininfo', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type':'application/json','accept':'application/json, text/plain, */*',
        'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
      body: JSON.stringify({requestFrom: 0})});
    const j = await r.json();
    if (!j || j.code !== 1) return null;
    const d = j.data || {};
    return {userId: d.userId || 0, pin: d.pin || '',
            name: d.userDisplayName || d.pin || ''};
  } catch (e) { return null; }
}
"""


def login_info(page: Page) -> dict | None:
    """取当前登录账号的 pin / 京准通账户ID（新账号登录后自动回填用）。"""
    try:
        got = page.evaluate(_JS_LOGININFO_DATA)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(got, dict):
        return None
    try:
        got["userId"] = int(got.get("userId") or 0)
    except (TypeError, ValueError):
        got["userId"] = 0
    return got


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

