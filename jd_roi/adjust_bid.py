# -*- coding: utf-8 -*-
"""回滚/修改京准通「智能投放-爆款场景」计划的期望成交投产比。

接口（已实测）:
  读: POST https://jzt-api.jd.com/growth/scenario/get     {"campaignId":"<id>"}
  写: POST https://jzt-api.jd.com/growth/scenario/update  ← 把 get 返回的 data 原样回传，只改 biddingPrice
      （biddingPrice = 期望成交投产比；dayBudget = 日预算；campaignType=61 / scenarioType=2 为爆款场景）

用法:
  # 1) 只读：从操作日志里找出「判定为有问题」的调价，给出回滚目标
  python -m jd_roi.adjust_bid plan  --account=b
  # 2) 预览（读线上当前值，不写）
  python -m jd_roi.adjust_bid apply --account=b --dry-run
  # 3) 真正写回
  python -m jd_roi.adjust_bid apply --account=b --yes
  # 4) 手工指定目标值（覆盖自动推导）
  python -m jd_roi.adjust_bid apply --account=b --set=9015332674=4.7,7305991706=3.6,8943938765=2.6 --yes
"""
from __future__ import annotations

import json
import re
import sys

from . import analyze2, config
from .browser import launch_profile

API = "https://jzt-api.jd.com"

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


def _arg(name: str, default: str | None = None) -> str | None:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


def _post(page, path: str, payload: dict) -> dict:
    res = page.evaluate(_JS, {"url": API + path, "payload": payload})
    if res["status"] != 200:
        raise RuntimeError(f"HTTP {res['status']}: {res['text'][:200]}")
    return json.loads(res["text"])


def get_scenario(page, campaign_id: str | int) -> dict:
    """读取计划当前配置（爆款场景）。"""
    resp = _post(page, "/growth/scenario/get", {"campaignId": str(campaign_id), "requestFrom": 0})
    if resp.get("code") != 1 or not resp.get("data"):
        raise RuntimeError(f"读取失败: {json.dumps(resp, ensure_ascii=False)[:200]}")
    return resp["data"]


def update_scenario(page, data: dict, campaign_id: str | int) -> dict:
    """按 get 的返回结构写回（只改需要变的字段）。

    注意：get 返回的顶层 campaignId 为 null，直接回传会被后端当成「新建计划」并报
    「计划名称已存在」，必须补上顶层 campaignId。
    """
    payload = dict(data)
    payload["campaignId"] = int(campaign_id)
    resp = _post(page, "/growth/scenario/update", payload)
    if resp.get("code") != 1:
        raise RuntimeError(f"写回失败: {json.dumps(resp, ensure_ascii=False)[:200]}")
    return resp


# ---------------- 从操作日志推导回滚方案 ----------------
_ROI_RE = re.compile(r"期望成交投产比[：:]\s*([\d.]+)")


def rollback_plan(account: str) -> list[dict]:
    """扫描操作日志，返回 {campaignId,name,target,current,events}。

    只取「调价」类且被 analyze2 判定为「有问题」的操作；同一计划多次调整时，
    回滚到**最早那次**调整之前的值（即撤销全部错误下调）。
    """
    accts = analyze2._load_accounts()
    model = analyze2.load_cost_model()
    ops = analyze2.op_rows(accts)
    plans = analyze2.plan_rows(accts, model)
    adj = analyze2.adjustments(accts, ops, plans)

    by_cid: dict[str, dict] = {}
    for a in adj:
        if a.get("account") != account:
            continue
        if a.get("verdict") != "有问题":
            continue
        det = analyze2._decode(a.get("detail") or "")
        nums = _ROI_RE.findall(det)
        if len(nums) < 2:
            continue
        cid = str(a.get("campaignId") or a.get("targetId"))
        ev = {"time": a.get("time"), "from": float(nums[0]), "to": float(nums[1]), "note": a.get("note")}
        item = by_cid.setdefault(cid, {"campaignId": cid, "name": a.get("plan") or a.get("target"),
                                       "events": []})
        item["events"].append(ev)
    out = []
    for item in by_cid.values():
        item["events"].sort(key=lambda e: e["time"] or "")
        item["target"] = item["events"][0]["from"]   # 撤销全部错误调整 -> 回到最早之前
        item["current"] = item["events"][-1]["to"]   # 日志里的最新值
        out.append(item)
    out.sort(key=lambda x: -len(x["events"]))
    return out


def main() -> int:
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "plan")
    account, _ = config.parse_account(sys.argv[1:])
    account = account or config.MAIN_ACCOUNT
    use_oplog = "--set" not in sys.argv

    if cmd == "plan":
        print(f"账号 {account} · 判定为「有问题」的调价（只读）\n" + "=" * 96)
        for it in rollback_plan(account):
            print(f"\n● {it['name']}  (campaignId={it['campaignId']})")
            for e in it["events"]:
                print(f"    {e['time']}  投产比 {e['from']} → {e['to']}   {e['note']}")
            print(f"    ⇒ 回滚目标: {it['target']}")
        if not use_oplog:
            print("\n(--set 已指定，plan 忽略自动推导)")
        return 0

    # ---- apply ----
    dry = "--dry-run" in sys.argv or "--yes" not in sys.argv
    set_arg = _arg("set")
    if set_arg:
        items = []
        for part in set_arg.split(","):
            cid, val = part.split("=")
            items.append({"campaignId": cid.strip(), "target": float(val), "name": "", "events": []})
    else:
        items = rollback_plan(account)
    if not items:
        print("没有需要回滚的计划。")
        return 1

    pw, context = launch_profile(headless=True, account=account)
    page = context.new_page()
    try:
        page.goto(config.JZT_HOME, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        print(f"账号 {account} · {'预览（未写入）' if dry else '开始写回'}\n" + "=" * 96)
        for it in items:
            cid = it["campaignId"]
            data = get_scenario(page, cid)
            cur = data.get("biddingPrice")
            target = float(it["target"])
            name = it.get("name") or data.get("campaignName")
            tags = f"campaignType={data.get('campaignType')} scenarioType={data.get('scenarioType')} 日预算={data.get('dayBudget')}"
            if cur is not None and abs(float(cur) - target) < 1e-9:
                print(f"  = {name} ({cid}) 已是 {target}，跳过  [{tags}]")
                continue
            print(f"  → {name} ({cid})  投产比 {cur} → {target}  [{tags}]")
            if not dry:
                data["biddingPrice"] = target
                resp = update_scenario(page, data, cid)
                chk = get_scenario(page, cid)
                ok = "OK" if abs(float(chk.get("biddingPrice") or 0) - target) < 1e-9 else "未生效!"
                print(f"      写回返回: {json.dumps(resp.get('data'), ensure_ascii=False)}  校验当前值={chk.get('biddingPrice')} {ok}")
                page.wait_for_timeout(1500)
        if dry:
            print("\n[预览] 未做任何修改；确认后加 --yes 执行。")
    finally:
        context.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
