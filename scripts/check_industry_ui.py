# -*- coding: utf-8 -*-
"""无头浏览器验收：报表⑧行业大盘页签能渲染、图表能初始化、没有 JS 报错。

开发容器内运行（报表页需要本地按天数据 + 行业大盘缓存）：
    docker cp scripts/check_industry_ui.py jd-roi-dev:/tmp/check_industry_ui.py
    docker exec -w /app -e PYTHONPATH=/app jd-roi-dev python /tmp/check_industry_ui.py
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

URL = ("http://127.0.0.1:8000/report?start=2026-10-01&end=2026-10-07&accounts=main")
ERRS: list[str] = []


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("pageerror", lambda e: ERRS.append(str(e)))
        page.goto(URL, wait_until="load", timeout=90000)
        page.wait_for_timeout(2500)
        charts = page.evaluate("(window.__CHARTS||[]).map(c=>c.id)")
        print("tabs:", page.eval_on_selector_all(".tab", "els=>els.map(e=>e.textContent)"))
        print("charts:", charts)
        print("__CH:", page.evaluate("Object.keys(window.__CH||{})"))
        print("industry_chart_dom:", page.evaluate(
            "!!document.getElementById('chIndustryTrend') && !!document.getElementById('chIndustryHour')"))
        print("__CHARTS type:", page.evaluate("typeof window.__CHARTS"))
        page.get_by_text("⑧ 行业大盘", exact=True).click()
        page.wait_for_timeout(600)
        active = page.evaluate(
            "document.querySelector('section.page.active') && document.querySelector('section.page.active').id")
        print("active tab:", active)
        ind = page.eval_on_selector("#p-ind", "e=>e.innerText")
        for key in ("本店 vs 行业", "地域分析", "行业关键词榜", "规则层建议", "AI 行业总结与建议"):
            print(f"contains {key}:", key in ind)
        print("meta refresh:", page.evaluate(
            "!!document.querySelector('meta[http-equiv=refresh]')"))
        print("pageerror:", ERRS)
        browser.close()
    ok = (not ERRS and "chIndustryTrend" in charts and
          "chIndustryHour" in charts and active == "p-ind")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
