# -*- coding: utf-8 -*-
"""扫码登录流程测试：真实浏览器 + 打桩登录页。

验证（不需要真的扫码）：
  - 打开登录页后能自动点 .scan-login 切到二维码模式
  - 给出可用的二维码 PNG（状态 hasQr=True）
  - 服务端会话未解除时**不会**误判为已登录（这正是之前踩过的坑：
    cookie 还在但服务端已失效，只看 cookie 会误报）
  - 服务端返回 code=1 后状态变 logged_in，并把 storage_state 落盘
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_login_", dir=str(_TMPROOT)))

os.environ["JD_DATA_DIR"] = str(TMP / "data")
os.environ.setdefault("JD_COSTS_URL", "")   # 离线：测试不连供货方成本接口
os.environ["JD_AUTH_DIR"] = str(TMP / "auth")
os.environ["JD_CONFIG_DIR"] = str(TMP / "config")
os.environ["JD_EXCEL_PATH"] = str(TMP / "config" / "missing.xlsx")
os.environ["JD_ACCOUNTS"] = json.dumps([{"key": "main", "label": "主账号", "account_id": 111}])
os.environ["JD_HEADLESS"] = "1"
os.environ["JD_LOGIN_TIMEOUT"] = "90"
os.environ["JD_ENABLE_SCHEDULER"] = "0"
os.environ["JD_RUN_ON_START"] = "0"
sys.path.insert(0, str(ROOT))

from jd_roi import browser, settings, webapp  # noqa: E402

FAILS = []
STATE = {"server_ok": False, "qr_clicked": 0, "logininfo_calls": 0}

QR_SVG = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'"
          "%3E%3Crect width='160' height='160' fill='%23eeeeee'/%3E%3Crect x='20' y='20' "
          "width='40' height='40' fill='%23000'/%3E%3Crect x='100' y='100' width='40' "
          "height='40' fill='%23000'/%3E%3C/svg%3E")

LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>登录</title>
<style>.scan-login{display:block;width:36px;height:32px;background:#ddd}
.qrcode{margin:40px auto;width:200px;height:200px;text-align:center}</style></head>
<body>
<div id="card">
  <div class="tab">密码登录</div>
  <div class="scan-login" onclick="document.getElementById('qrbox').style.display='block';window.__clicked=1">扫码</div>
  <div id="qrbox" style="display:none"><div class="qrcode"><img src="QR_SVG_HERE" width="160" height="160"></div></div>
</div>
</body></html>""".replace("QR_SVG_HERE", QR_SVG)


def check(name, got, want):
    ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)


def check_true(name, cond):
    print("  [" + ("PASS" if cond else "FAIL") + "] " + name)
    if not cond:
        FAILS.append(name)


def handler(route):
    req = route.request
    url = req.url
    origin = req.headers.get("origin") or "*"
    cors = {"access-control-allow-origin": origin,
            "access-control-allow-credentials": "true",
            "access-control-allow-headers": "*",
            "access-control-allow-methods": "*"}
    # 跨域 POST + Content-Type: application/json 会先发 OPTIONS 预检，必须应答
    if req.method == "OPTIONS":
        route.fulfill(status=204, headers=cors, body="")
        return
    if "common/logininfo" in url:
        STATE["logininfo_calls"] += 1
        body = {"code": 1, "data": {"pin": "tester"}} if STATE["server_ok"] \
            else {"code": -100, "msg": "登录失败"}
        route.fulfill(status=200, headers=dict(cors, **{"content-type": "application/json"}),
                      body=json.dumps(body))
        return
    route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8"},
                  body=LOGIN_PAGE)


def fake_launch(headless=None, account=None):
    """注意：Playwright 同步 API 有线程亲和性。

    LoginSession 在自己的线程里调用本函数，所以这里必须**在这个线程里**
    启动 playwright 并创建 context，否则会报 "Cannot switch to a different thread"。
    生产代码就是这么做的（浏览器在 _loop 内创建），测试也必须照做。
    """
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(TMP / "profile"), headless=True,
        viewport={"width": 1200, "height": 800})
    # 有 pin cookie，但服务端会话失效 —— 正是真实的过期场景
    ctx.add_cookies([{"name": "pin", "value": "stale", "domain": ".jd.com", "path": "/"}])
    ctx.route("**/*", handler)
    return (pw, ctx)


def main() -> int:
    settings.ensure_dirs()

    print("[0] 临时目录必须落在可写目录内（Playwright artifacts 依赖它）")
    import tempfile
    td = tempfile.gettempdir()
    inside = str(td).startswith(str(settings.DATA_DIR)) or str(td).startswith(str(settings.TMP_DIR))
    print("      tempfile.gettempdir() =", td)
    check_true("tempfile 被钉到 DATA_DIR/TMP_DIR 内", inside)
    check_true("TEMP/TMP 环境变量一致", os.environ.get("TEMP") == str(settings.TMP_DIR))
    check_true("临时目录可写", os.access(td, os.W_OK))

    print("")
    print("[1] 启动登录流程（真实浏览器 + 打桩登录页）")
    if True:
        original = browser.launch_persistent
        browser.launch_persistent = fake_launch
        try:
            login = webapp.LoginSession()
            check_true("start() 返回 True", login.start("main") is True)
            check_true("重复 start() 被拒绝", login.start("main") is False)

            snap = {}
            for _ in range(60):
                time.sleep(0.5)
                snap = login.snapshot()
                if snap["hasQr"]:
                    break
            if snap["state"] == "error":
                print("      !! LoginSession 异常:", snap.get("error"))
            check("状态进入等待扫码", snap["state"], "waiting")
            check_true("已生成二维码", snap["hasQr"])
            check_true("二维码是 PNG 且非空", bool(login.qr_png) and login.qr_png[:4] == b"\x89PNG")

            print("")
            print("[2] 服务端仍判未登录时不能误报成功")
            time.sleep(3)
            check("状态仍是 waiting", login.snapshot()["state"], "waiting")
            check_true("确实问过服务端", STATE["logininfo_calls"] >= 1)

            print("")
            print("[3] 服务端返回已登录 → 落盘")
            STATE["server_ok"] = True
            for _ in range(60):
                time.sleep(0.5)
                snap = login.snapshot()
                if snap["state"] == "logged_in":
                    break
            check("状态变为 logged_in", snap["state"], "logged_in")
            state_file = Path(os.environ["JD_AUTH_DIR"]) / "jd_state.json"
            check_true("storage_state 已落盘: " + str(state_file), state_file.exists())
            st = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            check_true("登录态里有 cookies", len(st.get("cookies") or []) > 0)
        finally:
            browser.launch_persistent = original

    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    _code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(_code)
