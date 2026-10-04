# -*- coding: utf-8 -*-
"""只读探测：京准通 MSA「计划详情」接口结构（用于确认出价/投产比字段）。

用法:
    python -m jd_roi.probe_campaign --account=b 9015332674 7305991706
输出:
    data/<acct>/campaign_detail_<id>.json
"""
from __future__ import annotations

import json
import sys

from . import config, store
from .browser import launch_profile

API_BASE = "https://atoms-api.jd.com"

CANDIDATES = [
    ("/dspad/msa/campaign/get", [{"campaignId": None}, {"campaignIdList": None}, {"id": None}]),
    ("/dspad/msa/campaign/select", [{"campaignId": None}, {"campaignIdList": None}]),
    ("/dspad/msa/campaign/item/get", [{"campaignId": None}, {"campaignIdList": None}]),
]

_JS = """
async ({url, payload}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json',
      'accept': 'application/json, text/plain, */*',
      'referer': 'https://jzt.jd.com/', 'language': 'zh_CN',
      'siteid': '0', 'loginmode': '0'},
    body: JSON.stringify(payload)
  });
  return {status: r.status, text: await r.text()};
}
"""


def main() -> int:
    account, rest = config.parse_account(sys.argv[1:])
    cids = [int(x) for x in rest if x.isdigit()]
    if not cids:
        print("用法: python -m jd_roi.probe_campaign --account=b <campaignId> ...")
        return 2

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        for cid in cids:
            print("=" * 90)
            print("campaignId:", cid)
            for path, payloads in CANDIDATES:
                for tpl in payloads:
                    payload = {k: (cid if v is None else v) for k, v in tpl.items()}
                    res = page.evaluate(_JS, {"url": API_BASE + path, "payload": payload})
                    body = res["text"] or ""
                    ok = '"code":1' in body.replace(" ", "") or '"success":true' in body.lower()
                    print(f"  {path} {list(payload)} -> HTTP {res['status']} | {body[:220]}")
                    if ok and len(body) > 200:
                        store.save(f"campaign_detail_{cid}", json.loads(body))
                        print(f"     [OK] 已保存 data/campaign_detail_{cid}.json")
                        break
                else:
                    continue
                break
    finally:
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
