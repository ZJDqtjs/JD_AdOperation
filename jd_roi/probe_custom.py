# -*- coding: utf-8 -*-
"""探测自定义报表的维度/指标模板（含地域、时段等维度定义）。

用法:
    python -m jd_roi.probe_custom [account]
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile

_JS = """
async ({url, method, payload}) => {
  const opt = {method: method||'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
      'referer':'https://jzt.jd.com/custom-report/','language':'zh_CN','siteid':'0','loginmode':'0'}};
  if ((method||'POST') === 'POST') opt.body = JSON.stringify(payload||{});
  const r = await fetch(url, opt);
  return {status: r.status, text: await r.text()};
}
"""

TARGETS = [
    ("https://jzt-api.jd.com/dataCenter/customreport/v2/template/specific?specificType=0&requestFrom=0&businessFrom=1", "GET", None),
    ("https://jzt-api.jd.com/dataCenter/customreport/v2/template/specific/list?requestFrom=0&businessFrom=1", "POST", {"requestFrom": 0, "businessFrom": 1}),
]


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto("https://jzt.jd.com/custom-report/#/create?specificType=0",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(15000)
        for url, method, payload in TARGETS:
            try:
                res = page.evaluate(_JS, {"url": url, "method": method, "payload": payload or {}})
            except Exception as exc:  # noqa: BLE001
                print(f"[{method}] {url}\n   ✗ {type(exc).__name__}")
                continue
            print(f"\n[{method}] {url[:110]}\n   HTTP {res['status']}")
            try:
                d = json.loads(res["text"])
            except Exception:  # noqa: BLE001
                print("   非JSON:", res["text"][:300])
                continue
            print("   code=", d.get("code"), "msg=", d.get("msg"))
            body = d.get("data")
            txt = json.dumps(body, ensure_ascii=False)
            print("   len=", len(txt))
            # 找维度相关关键词
            for kw in ("地域", "时段", "人群", "area", "hour", "crowd", "region", "time"):
                if kw in txt:
                    print(f"   ✓ 命中「{kw}」")
            print("   head:", txt[:1200])
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
