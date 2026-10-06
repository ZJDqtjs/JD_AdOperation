# -*- coding: utf-8 -*-
"""跑全部测试，并做一次 Dockerfile 一致性检查。

    python tests/run_all.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

SUITES = [
    ("区间聚合（按天入库 → 任意区间加总）", "tests/test_range_agg.py"),
    ("端到端管线（冷启动/抓取编排/聚合/报表/HTTP）", "tests/test_pipeline_e2e.py"),
    ("真实浏览器 + 打桩京东接口（分页/限流/isDaily/商智capture/操作日志）", "tests/test_scrape_live_http.py"),
    ("停机补数（缺口 ∪ 回刷窗口 + 上限）", "tests/test_catchup.py"),
    ("调度（0:05 触发 + 回刷窗口）", "tests/test_scheduler.py"),
    ("容器契约（Dockerfile ENV + 目录约定 + 该布局下跑 Web）", "tests/test_container_layout.py"),
    ("扫码登录流程（真实浏览器 + 打桩登录页）", "tests/test_login_flow.py"),
]

FAILS = []


def docker_checks() -> None:
    print("=" * 78)
    print("Docker 制品一致性检查")
    print("=" * 78)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    # 1) COPY 源必须存在
    for m in re.finditer(r"^COPY\s+(\S+)\s+(\S+)\s*$", dockerfile, re.M):
        src = m.group(1)
        exists = (ROOT / src).exists()
        ok = exists or src.endswith("entrypoint.sh") is False and src == "requirements.txt"
        status = "PASS" if (ROOT / src).exists() else "FAIL"
        print(f"  [{status}] COPY 源存在: {src}")
        if not (ROOT / src).exists():
            FAILS.append("COPY " + src)

    # 2) requirements 里的包都能 import
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip().splitlines()
    import importlib.util
    for line in req:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pkg = re.split(r"[=<\[\]]", line)[0].strip()
        mod = {"uvicorn": "uvicorn"}.get(pkg, pkg.replace("-", "_"))
        found = importlib.util.find_spec(mod) is not None
        print(f"  [{'PASS' if found else 'FAIL'}] requirements 可导入: {line}")
        if not found:
            FAILS.append("req " + pkg)

    # 3) EXPOSE 端口与 compose 一致
    expose = re.search(r"^EXPOSE\s+(\d+)", dockerfile, re.M)
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    port_ok = expose is not None and expose.group(1) in compose
    print(f"  [{'PASS' if port_ok else 'FAIL'}] EXPOSE 与 compose 端口一致 ({expose.group(1) if expose else '?'})")
    if not port_ok:
        FAILS.append("EXPOSE")

    # 4) entrypoint 引用的 app 可导入
    ep = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    has_app = "jd_roi.webapp:app" in ep
    print(f"  [{'PASS' if has_app else 'FAIL'}] entrypoint 指向 jd_roi.webapp:app")
    if not has_app:
        FAILS.append("entrypoint app")

    # 5) 挂载目录的默认值要和 settings 用的环境变量对上
    for var in ("JD_DATA_DIR", "JD_AUTH_DIR", "JD_CONFIG_DIR"):
        ok = var in dockerfile and var in (ROOT / ".env.example").read_text(encoding="utf-8") or var in dockerfile
        print(f"  [{'PASS' if var in dockerfile else 'FAIL'}] Dockerfile 声明了 {var}")
        if var not in dockerfile:
            FAILS.append(var)

    # 6) 所有 JD_* 变量都必须真的被 settings 读取（拼错=静默失效）
    settings_src = (ROOT / "jd_roi" / "settings.py").read_text(encoding="utf-8")
    declared = set()
    for path in ("Dockerfile", "docker-compose.yml", ".env.example"):
        declared |= set(re.findall(r"\bJD_[A-Z_]+\b", (ROOT / path).read_text(encoding="utf-8")))
    declared |= set(re.findall(r"\bTZ\b", (ROOT / "Dockerfile").read_text(encoding="utf-8")))
    ignored = {"JD_WEB_PORT"}   # 由 uvicorn 命令行用，settings 也读，下面会命中
    unread = sorted(v for v in declared if v not in ignored and ('"' + v + '"') not in settings_src)
    print(f"  [{'PASS' if not unread else 'FAIL'}] Dockerfile/compose/.env 里的 JD_* 都被 settings 读取"
          + (f"（未读取: {unread}）" if unread else ""))
    if unread:
        FAILS.append("unread env: " + ",".join(unread))

    # 7) echarts 资源会随 jd_roi 一起进镜像
    asset = ROOT / "jd_roi" / "assets" / "echarts.min.js"
    print(f"  [{'PASS' if asset.exists() else 'FAIL'}] echarts 资源存在（随 COPY jd_roi 进镜像）")
    if not asset.exists():
        FAILS.append("echarts asset")
    print("")


def main() -> int:
    docker_checks()
    results = []
    for title, path in SUITES:
        print("=" * 78)
        print("跑测试：" + title)
        print("=" * 78)
        p = subprocess.run([PY, str(ROOT / path)], cwd=str(ROOT))
        results.append((title, p.returncode))
        print("")

    print("=" * 78)
    print("汇总")
    print("=" * 78)
    for title, code in results:
        print(("  [PASS] " if code == 0 else "  [FAIL] ") + title)
    for f in FAILS:
        print("  [FAIL] docker check: " + f)
    bad = [t for t, c in results if c != 0] + FAILS
    print("")
    print("ALL GREEN" if not bad else f"{len(bad)} 项失败")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
