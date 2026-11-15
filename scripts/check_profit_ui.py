# -*- coding: utf-8 -*-
"""无头浏览器验证利润页快捷区间 + 断点续抓提示（不发真实请求，接口全部打桩）。"""
import json

from playwright.sync_api import sync_playwright

ERRS = []
RUN_BODY = {"ok": True, "cached": False, "jobId": "stub-job",
            "resumed": {"pieces": ["2026-10-01 ~ 2026-10-25"],
                        "tail": ["2026-10-26", "2026-10-29"]}}


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_context().new_page()
        pg.on("pageerror", lambda e: ERRS.append(str(e)))
        pg.route("**/api/profit/**", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "active": False})))
        pg.route("**/api/profit/run*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(RUN_BODY)))
        pg.goto("http://127.0.0.1:8000/profit", wait_until="load")

        print("PF:", pg.evaluate("typeof window.PF"),
              "| QRANGE:", pg.evaluate("typeof window.QRANGE"))
        print("chips:", pg.eval_on_selector_all(
            "#pfQuick .chip[data-k]",
            "els=>els.map(e=>e.getAttribute('data-k')+'|'+e.textContent+'|'+(e.title||''))"))

        # 「本月」按下去：区间应填 1 号 ~ 昨天，并提示只补抓尾段
        pg.evaluate("window.PF.quick('month')")
        pg.wait_for_timeout(400)
        print("inputs:", pg.evaluate("[pfStart.value, pfEnd.value]"))
        print("marked:", pg.eval_on_selector_all(
            "#pfQuick .chip.on", "els=>els.map(e=>e.textContent)"))
        print("msg:", pg.evaluate("(document.getElementById('pfMsg')||{}).textContent"))

        # 边界：每月 1 号（用真实 span 源码 + 假 today）
        edge = pg.evaluate("""(s) => {
            var iso = d => d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')
                              +'-'+String(d.getDate()).padStart(2,'0');
            var mk = new Function('today','iso', 'return ' + s);
            var f = mk(() => new Date(2026, 10, 1), iso);
            var g = mk(() => new Date(2026, 10, 30), iso);
            return {first: f('month'), mid: g('month'), last: g('lastmonth')};
        }""", pg.evaluate("window.QRANGE.span.toString()"))
        print("edge:", edge)

        # 服务端日历按钮（控制台/报表页的「本月」也必须止于昨天）
        pg.unroute("**/api/profit/**")
        pg.goto("http://127.0.0.1:8000/", wait_until="load")
        print("range_bar:", pg.evaluate("""
            Array.from(document.querySelectorAll('button.pre'))
                 .map(b => b.textContent + ':' + (b.getAttribute('onclick')||''))
                 .filter(t => t.indexOf('setSpan') >= 0)"""))
        print("pageerror:", ERRS)
        b.close()
    return 1 if ERRS else 0


if __name__ == "__main__":
    raise SystemExit(main())
