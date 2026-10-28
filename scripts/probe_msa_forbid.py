# -*- coding: utf-8 -*-
"""只读：全站智能推广 forbidden/list —— SPU 下有哪些 SKU、哪些被拉黑（生效 SKU）。"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")

from jd_roi import browser  # noqa: E402

acc = sys.argv[1] if len(sys.argv) > 1 else "main"
spu = int(sys.argv[2]) if len(sys.argv) > 2 else 100229760231
cid = int(sys.argv[3]) if len(sys.argv) > 3 else 9024290294
gid = int(sys.argv[4]) if len(sys.argv) > 4 else 0

JS = """
async ({url, payload}) => {
  const r = await fetch(url, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json','accept':'application/json, text/plain, */*',
             'referer':'https://jzt.jd.com/msa/','language':'zh_CN',
             'siteid':'0','loginmode':'0'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}
"""

payload = {"spuIds": [spu], "businessType": 600000013, "campaignType": 153,
           "searchType": 2,
           "campaignInfoList": [{"campaignId": cid, "adGroupId": gid, "spuId": spu,
                                 "jointActivityId": None, "activityType": None}],
           "requestFrom": 0}

pw, ctx = browser.launch_persistent(headless=True, account=acc)
try:
    page = ctx.new_page()
    page.goto("https://jzt.jd.com/msa/#/list/shopSmart?objective=item&scenario=normal&targetingType=shopSmart",
              wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(9000)
    res = page.evaluate(JS, {"url": "https://atoms-api.jd.com/dspad/smartmax/forbidden/list",
                             "payload": payload})
    j = json.loads(res["text"])
    data = j.get("data") or {}
    print("code", j.get("code"), "keys", list(data.keys()), flush=True)
    for k in ("deliverySkuIds", "spuEffectiveSkuMap", "spuSkuNumMap", "spuSkuErrorMap",
              "spuForbidSkuMap", "forbiddenSkuIds", "skuDetailMap"):
        v = data.get(k)
        if v is None:
            continue
        s = json.dumps(v, ensure_ascii=False)
        print(f"--- {k}: {s[:900]}", flush=True)
    skm = data.get("skuDetailMap") or {}
    print("skuDetailMap:", len(skm), flush=True)
    for sid, v in list(skm.items())[:12]:
        print("   ", sid, {kk: (v or {}).get(kk) for kk in
                           ("skuId", "name", "skuName", "forbidden", "isForbidden", "status")
                           if isinstance(v, dict) and kk in v}, flush=True)
finally:
    for closer in (lambda: ctx and ctx.close(), lambda: pw and pw.stop()):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
