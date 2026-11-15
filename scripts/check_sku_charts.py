# -*- coding: utf-8 -*-
"""无头浏览器验证「ROI vs 保本线 / 广告渗透率」两张图的卡片内筛选（账号 / 前N / 搜索）。"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/report"
ERRS = []


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_context().new_page()
        pg.on("pageerror", lambda e: ERRS.append(str(e)))
        pg.goto(BASE, wait_until="load", timeout=180000)
        # 两张图在「商品 ROI」页签里，先切过去，否则控件不可见
        pg.evaluate("document.querySelector('.tab[onclick*=\"p-sku\"]').click()")
        pg.wait_for_timeout(300)

        print("SKUCHART:", pg.evaluate(
            "({rows:(window.__SKUCHART||{}).rows.length,"
            " accounts:((window.__SKUCHART||{}).accounts||[]).map(a=>a.key).join(','),"
            " wkeys:((window.__SKUCHART||{}).wkeys||[]).join(',')})"))
        print("bars:", pg.evaluate(
            "({roi:(window.__CH.chSkuRoi.getOption().series||[]).map(s=>s.data.length),"
            " share:(window.__CH.chShare.getOption().series||[]).map(s=>s.data.length)})"))
        print("bars/height:", pg.evaluate(
            "[document.getElementById('chSkuRoi').style.height,"
            " document.getElementById('chShare').style.height]"))
        print("count:", pg.eval_on_selector_all(
            ".toolbar[data-skc] .srchcount", "els=>els.map(e=>e.textContent)"))

        # 1) 切到单个账号
        chip = pg.query_selector_all(".card:has(#chSkuRoi) .chip[data-ak]")
        print("acct chips:", [c.text_content() for c in chip])
        if len(chip) > 1:
            chip[1].click()
            pg.wait_for_timeout(300)
            print("after acct:", pg.evaluate(
                "({names:window.__CH.chSkuRoi.getOption().yAxis[0].data.slice(-3),"
                " count:document.querySelectorAll('.toolbar[data-skc] .srchcount')[0].textContent})"))

        # 2) 改条数
        pg.select_option(".card:has(#chSkuRoi) select", "30")
        pg.wait_for_timeout(300)
        print("after n=30:", pg.evaluate(
            "[window.__CH.chSkuRoi.getOption().series[0].data.length,"
            " document.getElementById('chSkuRoi').style.height]"))

        # 3) 搜索
        pg.fill(".card:has(#chSkuRoi) input.srch", "玉米")
        pg.wait_for_timeout(400)
        print("after q:", pg.evaluate(
            "[window.__CH.chSkuRoi.getOption().yAxis[0].data,"
            " document.querySelectorAll('.toolbar[data-skc] .srchcount')[0].textContent]"))

        # 4) 广告渗透率图同样可切
        pg.select_option(".card:has(#chShare) select", "0")
        pg.wait_for_timeout(400)
        print("share all:", pg.evaluate(
            "[window.__CH.chShare.getOption().series[0].data.length,"
            " document.getElementById('chShare').style.height]"))
        print("pageerror:", ERRS)
        b.close()
    return 1 if ERRS else 0


if __name__ == "__main__":
    raise SystemExit(main())
