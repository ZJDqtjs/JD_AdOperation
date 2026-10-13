# -*- coding: utf-8 -*-
"""精准探测「推广计划」行内「趋势」元素背后调用的是哪个接口、返回什么。

上一轮 probe_trend 点击命中了错误的元素（列表里 clstag 含 reduced_list 的有很多，
第一个是「更多筛选」按钮）。本轮改为按可见文本「趋势」定位，并：
  1) 记录点击前后全部 atoms-api/jzt-api 请求（含响应体）；
  2) 把行内趋势弹层（trendPic-container）的完整 DOM 与图表数据落盘；
  3) 追加实验：直接用 campaignId 调 trendchart，验证可否拿到「单个计划的分时趋势」。

用法:
    .venv\\Scripts\\python.exe -m jd_roi.probe_trend2 --account=main
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile, session_ok

OUT = config.DATA_DIR / "explore" / "trend_probe2.json"
API = "https://atoms-api.jd.com/reweb/common/index/trendchart"


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    caps: list[dict] = []

    def on_response(resp) -> None:
        try:
            u = resp.url
            if "atoms-api.jd.com" not in u and "jzt-api.jd.com" not in u:
                return
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            body = ""
            try:
                body = resp.text()[:20000]
            except Exception:  # noqa: BLE001
                pass
            caps.append({"url": u, "method": resp.request.method,
                         "post": (resp.request.post_data or "")[:4000],
                         "status": resp.status, "body": body})
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_response)
    result: dict = {"account": account}
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(18000)
        result["url"] = page.url
        result["session_ok"] = session_ok(page)

        # 找「趋势」元素 + 所在行的 campaignId
        info = page.evaluate(
            """() => {
              const sp = Array.from(document.querySelectorAll('span.reduced-blue'))
                  .filter(e => (e.textContent || '').trim() === '趋势');
              if (!sp.length) return {n: 0};
              const e = sp[0];
              const host = e.closest('[clstag*="reduced_list"]') || e.parentElement;
              // 行容器：向上找到含有计划名的区块
              let row = e, up = 0;
              while (row && up < 8) {
                 if (/\\(ID|ID：|ID:/.test(row.innerText || '') || (row.innerText||'').includes('营销目标')) break;
                 row = row.parentElement; up++;
              }
              const m = (row && row.innerText || '').match(/ID[：:]\s*(\d{6,})/);
              return {n: sp.length, clstag: host ? host.getAttribute('clstag') : null,
                      campId: m ? m[1] : null,
                      rowText: (row ? row.innerText : '').slice(0, 500)};
            }"""
        )
        result["trendElem"] = info

        # 点击前记录
        n0 = len(caps)
        clicked = page.evaluate(
            """() => {
              const el = Array.from(document.querySelectorAll('span.reduced-blue'))
                  .find(e => (e.textContent || '').trim() === '趋势');
              if (!el) return false;
              el.scrollIntoView({block: 'center'});
              const ev = el.parentElement || el;
              ['mouseover','mouseenter','mousemove'].forEach(t =>
                  ev.dispatchEvent(new MouseEvent(t, {bubbles:true})));
              ev.click();
              return true;
            }"""
        )
        result["clicked"] = clicked
        page.wait_for_timeout(9000)
        result["afterClick"] = caps[n0:]

        # 弹层 DOM（含 canvas/svg 与 tooltip 数据）
        result["popup"] = page.evaluate(
            """() => {
              const out = [];
              document.querySelectorAll('.trendPic-container, [class*=trendPic]').forEach(el => {
                 out.push({cls: el.className,
                           visible: !!(el.offsetWidth || el.offsetHeight),
                           html: el.outerHTML.slice(0, 8000)});
              });
              return out.slice(0, 4);
            }"""
        )
        try:
            page.screenshot(path=str(config.DATA_DIR / "explore" / "trend2.png"))
        except Exception:  # noqa: BLE001
            pass
        context.close()

        # 独立会话内直接用接口试 campaignId 过滤
        probe_url = page.url  # noqa: F841 (already closed)
    finally:
        pw.stop()

    # ---- 第二轮：用新会话直接打接口，验证 campaignId / granularity 语义 ----
    pw2, ctx2 = launch_profile(headless=True, account=account)
    p2 = ctx2.new_page()
    try:
        p2.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        p2.wait_for_timeout(12000)
        base = {
            "startDay": "2026-10-11 00:00:00", "endDay": "2026-10-11 23:59:59",
            "businessType": -4, "granularity": 4, "mediaResourceType": None,
            "interactiveType": None, "isMsa": 1, "hide": 1, "type": 1,
            "copyWriting": "广告点击15天内累计的成交订单", "clickOrOrderDay": 15,
            "giftFlag": 0, "clickOrOrderCaliber": 0, "orderStatusCategory": 1,
            "deliverySystemType": 0,
        }
        # 逐个测试候选参数：campaignId / dimension / levelType
        tests = {
            "plain": {},
            "gran2": {"granularity": 2},
            "gran3": {"granularity": 3},
            "gran5": {"granularity": 5},
            "campaignId": {"campaignId": 10007868405},
            "campaignIds": {"campaignIds": [10007868405]},
            "levelType1": {"levelType": 1},
            "campaignId+type2": {"campaignId": 10007868405, "type": 2},
        }
        api_out: dict = {}
        for name, extra in tests.items():
            payload = dict(base)
            payload.update(extra)
            js = """async ({url, payload}) => {
              try {
                const r = await fetch(url, {method:'POST', credentials:'include',
                  headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
                    'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
                  body: JSON.stringify(payload)});
                const t = await r.text();
                return {status: r.status, len: t.length, head: t.slice(0, 900)};
              } catch (e) { return {error: String(e)}; }
            }"""
            api_out[name] = p2.evaluate(js, {"url": API, "payload": payload})
            p2.wait_for_timeout(1200)
        result["apiTests"] = api_out
    finally:
        ctx2.close()
        pw2.stop()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("trendElem:", json.dumps(result.get("trendElem"), ensure_ascii=False)[:500])
    print("clicked:", result.get("clicked"), "afterClick n:", len(result.get("afterClick") or []))
    print("popup n:", len(result.get("popup") or []))
    for k, v in (result.get("apiTests") or {}).items():
        print(f"  {k:18s} status={v.get('status')} len={v.get('len')} err={v.get('error')}")
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
