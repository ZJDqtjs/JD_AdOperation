# -*- coding: utf-8 -*-
"""摸清 trendchart 家族接口的能力边界（时间段/颗粒度/多计划）。

已确认：
  - POST /reweb/common/index/trendchart          账号级趋势
  - POST /reweb/common/index/material/trendchart 计划级趋势（campaignIds:[id]）
  - granularity: 2=周 3=月 4=小时（1 推测=天，需验证）
本轮验证：granularity 1 语义、多计划合并、长区间、以及「取数上限」。

用法: .venv\\Scripts\\python.exe -m jd_roi.probe_trend3 --account=main
"""
from __future__ import annotations

import json
import sys

from . import config
from .browser import launch_profile

OUT = config.DATA_DIR / "explore" / "trend_probe3.json"
MAT = "https://atoms-api.jd.com/reweb/common/index/material/trendchart"
ACC = "https://atoms-api.jd.com/reweb/common/index/trendchart"

BASE = {
    "businessType": -4, "mediaResourceType": None, "interactiveType": None,
    "isMsa": 1, "type": 1, "copyWriting": "广告点击15天内累计的成交订单",
    "clickOrOrderDay": 15, "giftFlag": 0, "clickOrOrderCaliber": 0,
    "orderStatusCategory": 1, "deliverySystemType": 0,
}

JS = """async ({url, payload}) => {
  try {
    const r = await fetch(url, {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
        'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'},
      body: JSON.stringify(payload)});
    const t = await r.text();
    return {status: r.status, len: t.length, text: t};
  } catch (e) { return {error: String(e)}; }
}"""


def main() -> int:
    account, _ = config.parse_account(sys.argv[1:])
    pw, ctx = launch_profile(headless=True, account=account)
    page = ctx.new_page()
    out: dict = {"account": account}
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(14000)

        def call(url: str, **extra) -> dict:
            p = dict(BASE)
            p.update(extra)
            r = page.evaluate(JS, {"url": url, "payload": p})
            return r

        def brief(r: dict) -> dict:
            if not r or r.get("error"):
                return {"error": (r or {}).get("error")}
            try:
                j = json.loads(r.get("text") or "{}")
            except Exception:  # noqa: BLE001
                return {"status": r.get("status"), "len": r.get("len")}
            data = j.get("data")
            rows = data if isinstance(data, list) else ([] if data is None else [data])
            dates = [str(x.get("clickDate") or x.get("date")) for x in rows if isinstance(x, dict)]
            return {"status": r.get("status"), "code": j.get("code"),
                    "n": len(rows), "dates": dates[:6] + (["..."] if len(dates) > 6 else [])
                    + dates[-3:] if dates else [], "first": rows[0] if rows else None}

        D12 = {"startDay": "2026-10-12 00:00:00", "endDay": "2026-10-12 23:59:59"}
        LONG = {"startDay": "2026-09-13 00:00:00", "endDay": "2026-10-12 23:59:59"}

        tests = {
            # 账号级：各颗粒度
            "acc.gran1_1d": call(ACC, granularity=1, **D12),
            "acc.gran4_1d": call(ACC, granularity=4, **D12),
            "acc.gran1_30d": call(ACC, granularity=1, **LONG),
            "acc.gran4_30d": call(ACC, granularity=4, **LONG),
            # 计划级
            "mat.gran1_1d_1c": call(MAT, granularity=1, campaignIds=[10007868405], **D12),
            "mat.gran4_1d_1c": call(MAT, granularity=4, campaignIds=[10007868405], **D12),
            "mat.gran1_30d_1c": call(MAT, granularity=1, campaignIds=[10007868405], **LONG),
            "mat.gran1_30d_multi": call(MAT, granularity=1,
                                        campaignIds=[10007868405, 10007417365,
                                                     10007818575, 10007014255], **LONG),
            "mat.gran4_30d_1c": call(MAT, granularity=4, campaignIds=[10007868405], **LONG),
            # 计划级不带 campaignIds
            "mat.gran1_1d_none": call(MAT, granularity=1, **D12),
        }
        out["summary"] = {k: brief(v) for k, v in tests.items()}
        out["raw_len"] = {k: len((v or {}).get("text") or "") for k, v in tests.items()}
        # 保存 2~3 个有代表性的完整响应，供后续字段核对
        out["sample"] = {
            "acc.gran1_30d": (tests["acc.gran1_30d"] or {}).get("text", "")[:12000],
            "mat.gran1_30d_multi": (tests["mat.gran1_30d_multi"] or {}).get("text", "")[:20000],
            "mat.gran4_1d_1c": (tests["mat.gran4_1d_1c"] or {}).get("text", "")[:12000],
        }
    finally:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        for k, v in (out.get("summary") or {}).items():
            print(f"{k:22s} n={v.get('n')} code={v.get('code')} {v.get('dates')}"
                  if not v.get("error") else f"{k:22s} ERR {v}")
        print("->", OUT)
        ctx.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
