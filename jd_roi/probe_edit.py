# -*- coding: utf-8 -*-
"""打开 MSA「编辑计划」弹窗，抓取读写接口与弹窗结构。

用法:
    python -m jd_roi.probe_edit --account=b --camp=入仓紫拇指
    python -m jd_roi.probe_edit --account=b --camp=入仓紫拇指 --save   # 额外点保存（会真实提交！）
输出:
    data/net/msa_edit_<ts>.json   —— 抓到的 XHR
    data/explore/msa_edit.html    —— 弹窗打开后的页面 HTML
    data/explore/msa_edit.png     —— 截图
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import config
from .browser import launch_profile


def _arg(name: str, default: str | None = None) -> str | None:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    camp = _arg("camp", "入仓紫拇指") or ""
    do_save = "--save" in sys.argv

    net = config.BASE_DIR / "data" / "net"
    net.mkdir(parents=True, exist_ok=True)
    exp = config.BASE_DIR / "data" / "explore"
    exp.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()

    def on_resp(resp) -> None:
        try:
            rt = resp.request.resource_type
            if rt not in ("xhr", "fetch"):
                return
            if any(x in resp.url for x in ("/log", "beacon", "monitor")):
                return
            ctype = resp.headers.get("content-type", "")
            body = ""
            if "json" in ctype:
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    body = ""
            records.append({"url": resp.url, "method": resp.request.method,
                            "postData": (resp.request.post_data or "")[:3000],
                            "status": resp.status, "body": body[:6000]})
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_resp)
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(16000)
        print("列表页:", page.url)
        before = len(records)

        # 找到目标计划所在行，点其中的「编辑」
        row = page.locator(f"tr:has-text('{camp}')").first
        try:
            row.locator("text=编辑").first.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            print("行内编辑按钮定位失败:", type(exc).__name__, "-> 退化为全局按钮")
            page.locator("text=编辑").first.click(timeout=8000)
        page.wait_for_timeout(7000)
        print(f"打开弹窗后新增 XHR {len(records) - before} 条")

        (exp / "msa_edit.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(exp / "msa_edit.png"), full_page=False)
        print("HTML:", exp / "msa_edit.html", " SCREENSHOT:", exp / "msa_edit.png")

        if do_save:
            set_val = _arg("set")
            if set_val:
                inp = page.locator(".jst-price-set input.jad-input").first
                cur = inp.input_value()
                print("当前「目标成交投产比」输入框值:", cur)
                v = cur if set_val == "same" else set_val
                inp.click()
                inp.fill("")
                inp.type(str(v), delay=60)
                page.wait_for_timeout(900)
                print("已填入:", v)
            print("执行保存（真实提交）...")
            for t in ("完成编辑", "保存", "确定", "提交"):
                try:
                    page.locator(f"button:has-text('{t}')").last.click(timeout=5000)
                    print("  已点击:", t)
                    page.wait_for_timeout(6000)
                    break
                except Exception:  # noqa: BLE001
                    continue
        page.wait_for_timeout(3000)
    finally:
        ts = time.strftime("%H%M%S")
        fp = net / f"msa_edit_{account or 'main'}_{ts}.json"
        fp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n共 {len(records)} 条 XHR -> {fp}")
        seen = set()
        for rec in records:
            key = rec["url"].split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            print(f"  {rec['method']} {rec['status']} {key}")
            if rec["postData"]:
                print(f"      post: {rec['postData'][:200]}")
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
