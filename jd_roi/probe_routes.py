# -*- coding: utf-8 -*-
"""从京准通报表 SPA 的 JS 里扒出所有后端接口路径。

SPA 打包后的 JS 里保留了 `msa/orientation/xxx/list` 这类请求路径字面量，
比逐个猜接口名靠谱得多。

用法:
    python -m jd_roi.probe_routes [account]
"""
from __future__ import annotations

import re
import sys

from . import config
from .browser import launch_profile

PATTERNS = [
    r"msa/orientation/[a-zA-Z0-9_]+/list",
    r"msa/promolist/[a-zA-Z0-9_/]+",
    r"reweb/[a-zA-Z0-9_/]+",
    r"/dspad/[a-zA-Z0-9_/]+",
    r"orientation/[a-zA-Z0-9_]+",
]
_JS = """
async (url) => {
  const r = await fetch(url, {credentials:'include'});
  return {status: r.status, text: await r.text()};
}
"""


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.REPORT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        page.goto("https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)

        # 收集页面里所有 script 的绝对地址
        urls = page.evaluate("""() => Array.from(document.scripts)
            .map(s => s.src).filter(Boolean)""")
        js_urls = [u for u in urls if u.startswith("http")]
        print(f"发现 {len(js_urls)} 个 script")
        for u in js_urls:
            print("   -", u[:150])

        found: dict = {}
        for u in js_urls:
            try:
                res = page.evaluate(_JS, u)
            except Exception:  # noqa: BLE001
                continue
            text = res.get("text") or ""
            for pat in PATTERNS:
                for m in re.findall(pat, text):
                    found.setdefault(m if m.startswith(("/", "msa", "reweb")) else m, 0)
                    found[m] = found.get(m, 0) + 1
        print(f"\n=== JS 里出现的接口路径（{len(found)} 条）===")
        for k in sorted(found):
            print("  ", k)

        # 再扫当前页面 document 内联脚本 + 运行时路由
        try:
            inline = page.evaluate("""() => {
                let out = [];
                for (const s of document.querySelectorAll('script:not([src])')) out.push(s.textContent||'');
                return out.join('\\n');
            }""")
        except Exception:  # noqa: BLE001
            inline = ""
        print(f"\n=== 内联脚本命中（len={len(inline)}）===")
        for pat in PATTERNS:
            for m in sorted(set(re.findall(pat, inline))):
                print("  ", m)
        return 0
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
