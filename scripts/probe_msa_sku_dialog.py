# -*- coding: utf-8 -*-
"""只读：抓「全站智能推广 → 更多 → 设置SKU黑名单」弹窗背后的接口（SPU 下的 SKU 列表/黑名单）。"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")

from jd_roi import browser  # noqa: E402

acc = sys.argv[1] if len(sys.argv) > 1 else "main"
seen: list[tuple] = []

pw, ctx = browser.launch_persistent(headless=True, account=acc)
try:
    page = ctx.new_page()

    def on_req(req):
        try:
            if req.resource_type in ("xhr", "fetch") and "jd.com" in req.url:
                seen.append((req.method, req.url, (req.post_data or "")[:300]))
        except Exception:  # noqa: BLE001
            pass

    page.on("request", on_req)
    page.goto("https://jzt.jd.com/msa/#/list/shopSmart?objective=item&scenario=normal&targetingType=shopSmart",
              wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(14000)
    print("URL:", page.url, flush=True)
    print("=== 页面加载的 list 类接口 ===")
    for m, u, b in dict.fromkeys(seen):
        if any(k in u for k in ("list", "detail", "sku", "spu", "forbid", "black")):
            print(m, u, flush=True)
            if b:
                print("   ", b, flush=True)

    seen.clear()
    # 找第一行的「更多」并点开，再点「设置SKU黑名单」
    steps = []
    try:
        steps.append(("更多", page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('tr'));
          for (const tr of rows) {
            const el = Array.from(tr.querySelectorAll('a,span,div'))
              .find(e => e.children.length === 0 && (e.innerText||'').trim() === '更多');
            if (el && tr.innerText.indexOf('SPU') >= 0) { el.click(); return 'ok'; }
          }
          return 'no-more';
        }""")))
        page.wait_for_timeout(1500)
        steps.append(("设置SKU黑名单", page.evaluate("""() => {
          const el = Array.from(document.querySelectorAll('a,span,li,div'))
            .find(e => e.children.length === 0 && (e.innerText||'').indexOf('设置SKU黑名单') >= 0);
          if (!el) return 'no-item';
          el.click();
          return 'ok';
        }""")))
        page.wait_for_timeout(7000)
        print("steps:", steps, flush=True)
        print("dialog text:", page.inner_text("body")[:0] or "", flush=True)
        txt = page.inner_text("body")
        i = txt.find("SKU黑名单")
        print("弹窗片段:", (txt[i:i+400] if i >= 0 else "未找到弹窗").replace("\n", " | "), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("click ERR", type(exc).__name__, str(exc)[:120], flush=True)

    print("=== 打开弹窗后的接口 ===")
    for m, u, b in dict.fromkeys(seen):
        print(m, u, flush=True)
        if b:
            print("   ", b, flush=True)
finally:
    for closer in (lambda: ctx and ctx.close(), lambda: pw and pw.stop()):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
