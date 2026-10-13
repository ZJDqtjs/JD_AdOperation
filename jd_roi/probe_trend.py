# -*- coding: utf-8 -*-
"""探测京准通「推广计划」列表里「趋势」元素背后的数据。

思路：用已登录的持久化 Profile 打开 MSA 计划列表页，先记录页面加载期间的全部
XHR/fetch，再定位 clstag 含 reduced_list 的元素、点开它的趋势面板，把「点击前后」
新增的请求与响应落到 data/explore/trend_probe.json，便于离线分析。

用法:
    .venv\\Scripts\\python.exe -m jd_roi.probe_trend --account=main
    .venv\\Scripts\\python.exe -m jd_roi.probe_trend --account=b --no-click
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile, session_ok

OUT = config.DATA_DIR / "explore" / "trend_probe.json"

# 只对这些关键词的响应取 body，避免把整站资源都读一遍（慢且浪费内存）
BODY_KEY = ("trend", "chart", "list", "campaign", "msa", "report", "query",
            "indicator", "metric", "day", "sum")


def _want_body(url: str) -> bool:
    low = url.lower()
    if not any(k in low for k in BODY_KEY):
        return False
    return low.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2")) is False


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    no_click = any(a == "--no-click" for a in sys.argv[1:])

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    records: list[dict] = []
    bodies: dict[str, str] = {}

    def on_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document"):
                return
            url = resp.url
            rec = {
                "url": url,
                "method": req.method,
                "status": resp.status,
                "type": req.resource_type,
                "post": (req.post_data or "")[:4000],
            }
            records.append(rec)
            if _want_body(url) and url not in bodies:
                try:
                    bodies[url] = resp.text()[:6000]
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_response)
    result: dict = {"account": account}

    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(18000)
        result["url"] = page.url
        result["title"] = page.title()
        result["session_ok"] = session_ok(page)
        n_load = len(records)

        # --- 找「趋势」元素 ---
        probe = page.evaluate(
            """() => {
              const byCls = Array.from(document.querySelectorAll('[clstag*="reduced_list"]'));
              const byTxt = Array.from(document.querySelectorAll('*'))
                 .filter(e => e.children.length === 0 && (e.textContent || '').trim() === '趋势');
              const sample = byCls.slice(0, 3).map(e => ({
                 tag: e.tagName, cls: e.className, clstag: e.getAttribute('clstag'),
                 html: e.outerHTML.slice(0, 400),
                 rowText: (e.closest('tr') ? e.closest('tr').innerText : '').slice(0, 400),
              }));
              return {
                 nCls: byCls.length, nTxt: byTxt.length, sample,
                 txtSample: byTxt.slice(0,3).map(e => ({
                     tag: e.tagName, cls: e.className,
                     clstag: e.parentElement ? e.parentElement.getAttribute('clstag') : null,
                     html: (e.parentElement ? e.parentElement.outerHTML : e.outerHTML).slice(0, 400),
                     rowText: (e.closest('tr') ? e.closest('tr').innerText : '').slice(0, 600),
                 })),
              };
            }"""
        )
        result["elem"] = probe
        result["reqAfterLoad"] = list(bodies.keys())

        if not no_click and probe.get("nCls"):
            # --- 点开趋势 ---
            before = len(records)
            try:
                page.eval_on_selector(
                    '[clstag*="reduced_list"]',
                    "e => e.scrollIntoView({block:'center'})")
                page.wait_for_timeout(800)
                page.click('[clstag*="reduced_list"]', timeout=8000)
            except Exception as e:  # noqa: BLE001
                result["clickError"] = str(e)
            page.wait_for_timeout(9000)
            result["newAfterClick"] = records[before:]
            # 点击后可能渲染出弹层/图表，抓一下可见文本
            try:
                result["popup"] = page.evaluate(
                    """() => {
                      const cands = Array.from(document.querySelectorAll(
                        '.ant-modal, .ant-drawer, .ant-popover, .jd-modal, [class*=chart], [class*=trend], [class*=Trend]'));
                      return cands.slice(0, 6).map(e => ({
                        cls: e.className, text: (e.innerText || '').slice(0, 1200)}));
                    }"""
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                page.screenshot(path=str(config.DATA_DIR / "explore" / "trend_popup.png"))
            except Exception:  # noqa: BLE001
                pass

        # 落盘：请求全量 + 关键响应 body
        result["requests"] = records
        result["bodies"] = bodies
    finally:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print("URL:", result.get("url"))
        print("session_ok:", result.get("session_ok"))
        print("req total:", len(result.get("requests", [])))
        print("elem:", json.dumps({k: v for k, v in (result.get("elem") or {}).items()
                                   if k in ("nCls", "nTxt")}, ensure_ascii=False))
        print("-> ", OUT)
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
