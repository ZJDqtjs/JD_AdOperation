# -*- coding: utf-8 -*-
"""页面探索：登录后访问目标页面，导出截图/HTML/iframe结构，便于编写选择器。

用法:
    .venv\\Scripts\\python.exe -m jd_roi.explore <name> <url> [wait_ms]
例:
    .venv\\Scripts\\python.exe -m jd_roi.explore jzt "https://jzt.jd.com/msa/#/list/tab/plan?objective=overview" 12000
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config
from .browser import launch_profile


def explore(name: str, url: str, wait_ms: int = 12000) -> None:
    out = config.DATA_DIR / "explore"
    out.mkdir(parents=True, exist_ok=True)

    pw, context = launch_profile(headless=True)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(wait_ms)

        print("URL:", page.url)
        print("TITLE:", page.title())

        # 截图
        shot = out / f"{name}.png"
        page.screenshot(path=str(shot), full_page=False)
        print("SCREENSHOT:", shot)

        # iframe 结构
        frames = []
        for f in page.frames:
            frames.append({"name": f.name, "url": f.url})
        print("FRAMES:")
        for fr in frames:
            print("  -", fr["name"], "|", fr["url"][:160])

        # 主文档 HTML
        html = out / f"{name}.html"
        html.write_text(page.content(), encoding="utf-8")
        print("HTML:", html, "len=", len(page.content()))

        # 文本快照（可见文本前 4000 字）
        try:
            body_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body_text = ""
        txt = out / f"{name}.txt"
        txt.write_text(body_text, encoding="utf-8")
        print("TEXT:", txt, "len=", len(body_text))
        print("----- BODY TEXT (head) -----")
        print(body_text[:3000])
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    args = sys.argv[1:]
    nm = args[0]
    u = args[1]
    w = int(args[2]) if len(args) > 2 else 12000
    explore(nm, u, w)
