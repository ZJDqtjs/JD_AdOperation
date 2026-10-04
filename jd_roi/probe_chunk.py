# -*- coding: utf-8 -*-
"""从京准通前端所有 JS chunk 中按关键词定位接口与字段定义。

用法:
    python -m jd_roi.probe_chunk --account=b --url="https://jzt.jd.com/msa/#/list/tab/plan" --kw=期望成交投产比
输出:
    data/chunks/<chunk>.txt  —— 命中的 chunk 源码（便于离线 grep 接口路径/字段名）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import config
from .browser import launch_profile

CHUNK_RE = re.compile(r"[\w~\-]{1,64}-[0-9a-f]{16,}\.js")


def _arg(name: str) -> str | None:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return None


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    url = _arg("url") or config.JZT_HOME
    kws = [k for k in (_arg("kw") or "期望成交投产比").split(",") if k]
    out = config.BASE_DIR / "data" / "chunks"
    out.mkdir(parents=True, exist_ok=True)

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
            page.wait_for_timeout(2000)
    finally:
        base = None
        for u in texts:
            if re.search(r"/(index|main)[^/]*\.js$", u):
                base = u.rsplit("/", 1)[0]
                break
        if not base:
            print("未找到主 bundle"); context.close(); pw.stop(); return 1
        names: set[str] = set()
        for body in texts.values():
            names |= set(CHUNK_RE.findall(body))
        print(f"已加载 JS {len(texts)} 个，发现 chunk 名 {len(names)} 个，base={base}")

        hits: list[str] = []
        scanned = 0
        for n in sorted(names):
            u = f"{base}/{n}"
            try:
                r = context.request.get(u, timeout=30000)
                t = r.text()
            except Exception:  # noqa: BLE001
                continue
            scanned += 1
            if any(k in t for k in kws):
                (out / n.replace(".js", ".txt")).write_text(t, encoding="utf-8")
                hits.append(n)
                print(f"  [HIT] {n} -> data/chunks/{n.replace('.js', '.txt')}")
        print(f"扫描 {scanned} 个 chunk，命中 {len(hits)} 个: {hits}")
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
