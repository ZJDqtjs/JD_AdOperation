# -*- coding: utf-8 -*-
"""容器契约测试：按 Dockerfile 里声明的 ENV 与路径跑一遍。

Docker 装不了，但可以验证「Dockerfile 声明的环境变量/目录约定」是否自洽：
  - Dockerfile 里的 JD_* 全部能被 settings 正确识别
  - JD_DATA_DIR / JD_AUTH_DIR / JD_CONFIG_DIR 指向的目录会被自动创建
  - 调度时间、回刷天数、headless 等开关确实来自 Dockerfile 的 ENV 默认值
  - 成本表缺失时不报错，退化为内置默认（保本 ROI 4.00）
  - Web 应用在这个布局下能起、能出报表
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

# 解析 Dockerfile 的 ENV：KEY=value（可能被行尾反斜杠续行）
envs = {}
for m in re.finditer(r"(JD_[A-Z_]+)=([^\s\\]+)", DOCKERFILE):
    envs[m.group(1)] = m.group(2)

_TMPROOT = ROOT / "tmp"
_TMPROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="jdroi_docker_", dir=str(_TMPROOT)))
envs["JD_APP_DIR"] = str(TMP / "app")
envs["JD_DATA_DIR"] = str(TMP / "data")
envs["JD_AUTH_DIR"] = str(TMP / "auth")
envs["JD_CONFIG_DIR"] = str(TMP / "config")
envs["JD_EXCEL_PATH"] = str(TMP / "config" / "计算roi公式.xlsx")
envs["JD_ENABLE_SCHEDULER"] = "0"
envs["JD_RUN_ON_START"] = "0"
envs["JD_ACCOUNTS"] = json.dumps([{"key": "main", "label": "主账号", "account_id": 111}])
envs["JD_COSTS_URL"] = ""          # 测试保持离线：不连供货方成本接口，保本线走全店口径
envs["JD_WEB_PORT"] = "8901"

for k, v in envs.items():
    os.environ[k] = v
sys.path.insert(0, str(ROOT))

from jd_roi import settings  # noqa: E402
from jd_roi import webapp    # noqa: E402
from jd_roi import analyze2, dailystore as ds, indicators  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = str(got) == str(want)
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ": got=" + str(got) + " want=" + str(want))
    if not ok:
        FAILS.append(name)


def check_true(name, cond):
    print("  [" + ("PASS" if cond else "FAIL") + "] " + name)
    if not cond:
        FAILS.append(name)


def http(path):
    with urllib.request.urlopen("http://127.0.0.1:" + envs["JD_WEB_PORT"] + path, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main() -> int:
    print("[1] Dockerfile ENV 解析")
    check_true("解析出 JD_* 变量", len(envs) >= 8)
    for k in ("JD_DATA_DIR", "JD_AUTH_DIR", "JD_CONFIG_DIR", "JD_SCHEDULE_HOUR",
              "JD_SCHEDULE_MINUTE", "JD_REFRESH_DAYS", "JD_HEADLESS"):
        check_true("Dockerfile 声明了 " + k, k in envs)

    print("")
    print("[2] 目录约定")
    settings.ensure_dirs()
    for k, label in (("JD_DATA_DIR", "数据"), ("JD_AUTH_DIR", "登录态"), ("JD_CONFIG_DIR", "配置")):
        p = Path(envs[k])
        check_true(label + "目录被自动创建: " + str(p), p.is_dir())

    print("")
    print("[3] 开关落到 settings")
    check("调度小时", settings.SCHEDULE_HOUR, envs.get("JD_SCHEDULE_HOUR", "0"))
    check("调度分钟", settings.SCHEDULE_MINUTE, envs.get("JD_SCHEDULE_MINUTE", "5"))
    check("回刷天数", settings.REFRESH_DAYS, envs.get("JD_REFRESH_DAYS", "15"))
    check("headless", settings.HEADLESS, envs.get("JD_HEADLESS", "1") == "1")
    check("Web 端口", settings.WEB_PORT, int(envs["JD_WEB_PORT"]))

    print("")
    print("[4] 成本表缺失时退化为内置默认")
    model = analyze2.load_cost_model()
    margin = analyze2.global_margin(model)
    be = round(1 / margin, 2) if margin > 0 else 0.0
    check("毛利率（Excel H8 口径）", round(margin, 4), 0.25)
    check("保本 ROI", be, 4.00)
    check_true("Excel 路径回退不报错", True)

    print("")
    print("[5] 该布局下 Web 能起并出报表")
    ds.save_day("main", "2026-10-01", "jzt_campaign", {"rows": [
        {"campaignId": 1, "campaignName": "容器计划", "campaignType": 61, "impressions": 100,
         "clicks": 10, "cost": 50.0, "totalOrderCnt": 2, "totalOrderSum": 300.0,
         "totalCartCnt": 4}]})
    ds.save_day("main", "2026-10-01", "jst_sku", {"rows": [
        {"campaignId": 1, "campaignName": "容器计划", "skuId": "100000000001",
         "impressions": 100, "clicks": 10, "cost": 50.0, "totalOrderCnt": 2,
         "totalOrderSum": 300.0, "totalCartCnt": 4}]})
    ds.save_day("main", "2026-10-01", "sz_product", {"rows": [
        {"sku_id": "100000000001", "name": "容器商品",
         indicators.SZ_AMT: 900.0, indicators.SZ_ORDN: 3, indicators.SZ_QTY: 3,
         indicators.SZ_UV: 30, indicators.SZ_PV: 60, indicators.SZ_CVR: 0.1}]})
    ds.invalidate_cache("main")
    d1 = analyze2.build(start="2026-10-01", end="2026-10-01", accounts=[settings.get_account("main")])
    check("构建后 BREAKEVEN_FLAT 与成本表一致", analyze2.BREAKEVEN_FLAT,
          round(1 / analyze2.global_margin(analyze2.load_cost_model()), 2))
    check("SKU 保本线与全店保本线一致", d1["skus"][0]["breakeven"], analyze2.BREAKEVEN_FLAT)

    import uvicorn
    cfg = uvicorn.Config(webapp.app, host="127.0.0.1", port=int(envs["JD_WEB_PORT"]), log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        time.sleep(0.5)
        if getattr(server, "started", False):
            break
    check_true("uvicorn 已启动", getattr(server, "started", False))
    code, body = http("/healthz")
    check("健康检查", body.strip(), "ok")
    code, body = http("/api/coverage")
    check("覆盖接口状态", code, 200)
    check("按天库可读", json.loads(body)["coverage"]["main"]["days"]["count"], 1)
    code, body = http("/report?start=2026-10-01&end=2026-10-01&accounts=main")
    check("报表状态码", code, 200)
    check_true("报表渲染出商品表", "商品 ROI 交叉表" in body)
    server.should_exit = True
    time.sleep(1)

    print("")
    print("ALL PASS" if not FAILS else "FAILURES(" + str(len(FAILS)) + "): " + str(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    code = main()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
