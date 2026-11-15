# -*- coding: utf-8 -*-
"""把抓到的资金流水 JSON 汇总成「账号广告费流水表」（xlsx + html + md）。

核心逻辑在 jd_roi/cashflow.py（网页「广告费流水」页也复用它），这里只负责落文件。

用法（容器内，fetch_cashflow 跑完之后）：
    python /app/scripts/build_cashflow_table.py --src=/data/cashflow --out=/data/cashflow/out

产出：
    账号广告费流水表_<begin>_<end>.xlsx   5 个 sheet（汇总/每日/明细/口径/柱状图）
    账号广告费流水表_<begin>_<end>.html   参考图同款柱状图，浏览器直接打开
    账号广告费流水表_<begin>_<end>.md     人看的摘要
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/app")

from jd_roi import cashflow  # noqa: E402

ORDER = ["main", "b", "acct1", "acct2", "acct3"]


def _arg(argv: list[str], name: str, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def load(src: Path) -> list[dict]:
    items = []
    for p in src.glob("*.json"):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    idx = {k: i for i, k in enumerate(ORDER)}
    items.sort(key=lambda x: (idx.get(x.get("account"), 99), x.get("account") or ""))
    return items


def write_md(accs: list[dict], daily: list[dict], begin: str, end: str, path: Path) -> None:
    L = [f"# 账号广告费流水表（{begin} ~ {end}）", "",
         "> 充值 / 消费柱状图见同名 `.html`（绿上红下、上下独立刻度），"
         "或打开网页 `/cashflow`。", "",
         "## 一、账号汇总", "",
         "| 账号 | 账户ID | 当前余额 | 期初余额 | 充值 | 消费 | 现金/红包 | 日均消费 | 可撑天数 |",
         "|---|---|---:|---:|---:|---:|---|---:|---:|"]
    for a in accs:
        L.append(f"| {a['label']} | {a['account_id']} | {a['cash_balance']:,.2f} | "
                 f"{a['begin_balance']:,.2f} | {a['recharge_sum']:,.2f} | "
                 f"{a['consume_sum']:,.2f} | {a['cash_cost']:,.2f}/{a['award_cost']:,.2f} | "
                 f"{a['day_avg']:,.2f} | {a['cover_days'] if a['cover_days'] is not None else '—'} |")
    L.append(f"| **合计** | | **{sum(a['cash_balance'] for a in accs):,.2f}** | "
             f"**{sum(a['begin_balance'] for a in accs):,.2f}** | "
             f"**{sum(a['recharge_sum'] for a in accs):,.2f}** | "
             f"**{sum(a['consume_sum'] for a in accs):,.2f}** | | | |")
    L += ["", "## 二、每日消费（按账号）", "",
          "| 日期 | " + " | ".join(a["label"] for a in accs) + " | 合计 |",
          "|---|" + "---:|" * (len(accs) + 1)]
    byday: dict[str, dict[str, float]] = defaultdict(dict)
    for r in daily:
        if r["consume"]:
            byday[r["date"]][r["account"]] = r["consume"]
    for day in sorted(byday):
        vals = [byday[day].get(a["account"], 0) for a in accs]
        L.append(f"| {day} | " + " | ".join(f"{v:,.2f}" if v else "—" for v in vals) +
                 f" | {sum(vals):,.2f} |")
    L += ["", "## 三、口径", "",
          "- 交易日期 = 广告消耗日；记账时间 = 次日 08:45 左右批量扣款",
          "- 结余取接口 balanceAmount 原值；接口不给结余的账号按「期初 + 充值 - 消费」滚动推算",
          "- 分配（商家账户 ↔ 投放子账户）不改变账户现金总额，单列不计入余额滚动",
          ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")


def write_html(snap: dict, path: Path) -> None:
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>账号广告费流水图 {snap['begin']} ~ {snap['end']}</title><style>
body{{background:#f5f6f8;margin:0;padding:20px;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#222}}
h1{{font-size:18px;margin:0 0 4px}}
.sub{{color:#888;font-size:12px;margin-bottom:14px}}
.cfchart{{background:#fff;border-radius:8px;padding:10px 14px 6px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
.cfchd{{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;font-size:13px}}
.cfchd span{{color:#888;font-size:12px}}
.cfchd i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 3px 0 10px;vertical-align:-1px}}
.cfchd i.g{{background:#{cashflow.GREEN}}}.cfchd i.r{{background:#{cashflow.RED}}}
text.ax{{font-size:11px;fill:#9aa0a6}}
text.v{{font-size:9.5px}}
text.gv{{fill:#{cashflow.GREEN}}}text.rv{{fill:#{cashflow.RED}}}
text.empty{{fill:#b9bec6;font-size:12px}}
.note{{color:#888;font-size:12px;line-height:1.7}}
</style></head><body>
<h1>账号广告费流水图（{snap['begin']} ~ {snap['end']}）</h1>
<div class="sub">绿色向上 = 当日充值到账，红色向下 = 当日广告消耗；
<b>上下两块各自独立刻度</b>，两向柱高不可直接比（左侧数字是各自的满格值）。鼠标悬停看明细。</div>
{cashflow.charts_html(snap)}
<div class="note">口径：充值按到账日、消费按广告消耗日；次日 08:45 左右批量扣款。
数据来源：京准通 → 账户中心 → 财务记录。</div>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main() -> int:
    argv = sys.argv[1:]
    src = Path(_arg(argv, "src", "/data/cashflow"))
    out = Path(_arg(argv, "out", str(src / "out")))
    raw = load(src)
    if not raw:
        print("没有可用数据，请先跑 fetch_cashflow.py")
        return 1
    accs = [cashflow.build(d) for d in raw]
    ranges = {(a["range"].get("begin"), a["range"].get("end")) for a in accs if a["range"]}
    begin = min((r[0] for r in ranges if r[0]), default="")
    end = max((r[1] for r in ranges if r[1]), default="")
    daily = cashflow.daily_rows(accs, begin, end)
    snap = {"accounts": accs, "daily": daily, "begin": begin, "end": end}

    stem = f"账号广告费流水表_{begin}_{end}"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.xlsx").write_bytes(cashflow.xlsx_bytes(accs, daily, begin, end))
    write_md(accs, daily, begin, end, out / f"{stem}.md")
    write_html(snap, out / f"{stem}.html")

    print(f"[OK] {out / (stem + '.xlsx')}")
    print(f"[OK] {out / (stem + '.md')}")
    print(f"[OK] {out / (stem + '.html')}")
    for a in accs:
        print(f"  {a['label']:<8} 余额 {a['cash_balance']:>10,.2f} 期初 {a['begin_balance']:>10,.2f} "
              f"充值 {a['recharge_sum']:>10,.2f} 消费 {a['consume_sum']:>10,.2f} "
              f"日均 {a['day_avg']:>8,.2f} 可撑 {a['cover_days']} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
