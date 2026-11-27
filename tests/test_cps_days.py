# -*- coding: utf-8 -*-
"""CPS 按天缓存：订单明细（完成日）→ 任意区间 CPS 花费，不再逐个 SKU 抓。

用户 2026-11-25 反馈：已经算过 11/1~11/24，再选 11/11~11/24 又从头抓 CPS。
原因是 CPS 缓存按「整段区间」存，没有覆盖/裁剪复用；而团长活动页的订单明细
本身就能按完成日拿到 totalCosFee（与数据看板付款佣金逐日核对完全一致）。
这里验证：按天落盘后任意子区间都能直接拼出来。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import activity, paycomm, settings  # noqa: E402

D1, D2, D3, D4 = ("2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14")


def _day(key: str, day: str, skus: dict, orders: int, complete: bool = True) -> None:
    total = round(sum(float(v.get("fee") or 0) for v in skus.values()), 2)
    activity._write(activity.cps_day_path(key, day), {
        "date": day, "basis": activity.CPS_BASIS, "source": "order-detail",
        "fetchedAt": settings.now_iso(), "orders": orders, "expected": orders,
        "complete": complete, "total": total, "skus": skus})


def main() -> int:
    fails: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    old_dir, old_get = settings.DATA_DIR, settings.get_account
    tmp = Path(tempfile.mkdtemp(prefix="jdroi_cps_days_"))
    settings.DATA_DIR = tmp
    settings.get_account = lambda a: {"key": a, "label": f"账号-{a}"}
    try:
        print("[1] 按天缓存拼区间：金额/日合计/SKU 合并")
        _day("main", D1, {"1001": {"skuId": 1001, "name": "A", "fee": 10.0, "orders": 2}},
             2)
        _day("main", D2, {"1001": {"skuId": 1001, "name": "A", "fee": 2.0, "orders": 1},
                          "1002": {"skuId": 1002, "name": "B", "fee": 5.5, "orders": 1}},
             2)
        _day("main", D3, {}, 0)          # 这一天确实没有完成订单
        got = activity.cps_payload("main", D1, D3)
        check("三天齐全时能拼出", bool(got))
        check("金额合计正确", abs(float(got["total"]) - 17.5) < 0.01)
        check("按 SKU 合并", {r["skuId"]: r["payCommAmount"] for r in got["rows"]}
              == {1001: 12.0, 1002: 5.5})
        check("分日合计 3 天", len(got.get("daily") or []) == 3)
        check("来源标记 activity_orders", got.get("source") == "activity_orders")

        print("[2] 缺一天 / 不完整：不拿估算顶替")
        check("缺一天返回 None", activity.cps_payload("main", D1, D4) is None)
        _day("main", D4, {"1001": {"skuId": 1001, "fee": 1.0, "orders": 1}},
             1, complete=False)
        check("不完整的一天不算好", not activity.cps_day_good("main", D4))
        check("含不完整日仍返回 None", activity.cps_payload("main", D1, D4) is None)

        print("[3] 花费一览/月度利润读缓存自动复用日缓存")
        data = paycomm.read_cached("cps", "main", D1, D2)
        check("cps 命中日缓存", bool(data))
        check("金额正确", abs(sum(float(r["payCommAmount"]) for r in data["rows"]) - 17.5)
              < 0.01)
        check("京准通花费不误用 CPS 日缓存",
              paycomm.PayCommRunner("spend").cached_result("main", D1, D2) is None)

        print("[4] 日缓存 + 整段文件混合拼接（1/1~1/24 后查 1/11~1/24 的关键路径）")
        M1, M2, M3, M4 = "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"
        _day("mix", M1, {"1001": {"skuId": 1001, "name": "A", "fee": 1.0, "orders": 1}}, 1)
        _day("mix", M2, {"1001": {"skuId": 1001, "name": "A", "fee": 2.0, "orders": 1}}, 1)
        rep = tmp / "reports"
        rep.mkdir(parents=True, exist_ok=True)
        (rep / f"paycomm_mix_{M3}_{M4}.json").write_text(json.dumps({
            "jobId": "f", "mode": "cps", "account": "mix", "schema": 1,
            "accountLabel": "账号-mix", "start": M3, "end": M4,
            "rows": [{"skuId": 1001, "itemName": "A", "payCommAmount": 7.0}],
        }, ensure_ascii=False), encoding="utf-8")
        runner = paycomm.PayCommRunner("cps")
        out = runner.use_stitched("mix", M1, M4)
        merged = runner._results.get((out or {}).get("jobId") or "") or {}
        check("日缓存 + 文件能铺满区间", bool(out) and out.get("stitched"))
        check("两段拼起来", len(out.get("pieces") or []) == 2)
        check("混合来源金额正确",
              abs(sum(float(r["payCommAmount"]) for r in merged.get("rows") or []) - 10.0)
              < 0.01)
        check("来源标 mixed", merged.get("source") == "mixed")

        print("[5] 接口聚合：totalCosFee 优先、isValid=1、分页与 0 单日")
        calls: list[dict] = []

        def fake_post(page, url, payload):
            calls.append(dict(payload))
            if payload.get("startCptDay") == "2026-11-20":
                return {"content": {"total": 3, "data": [
                    {"orderId": "o1", "skuId": 1001, "skuName": "A",
                     "totalCosFee": 1.25, "isValid": 1},
                    {"orderId": "o2", "skuId": 1001, "skuName": "A",
                     "cosFee": 1.0, "cpFee": 0.1, "platformServiceFee": 0.05, "isValid": 1},
                    {"orderId": "o3", "skuId": 1002, "totalCosFee": 9.0, "isValid": 2},
                ]}}
            return {"content": {"total": 0, "data": []}}

        old_post, old_sleep = activity._post, activity.time.sleep
        activity._post = fake_post
        activity.time.sleep = lambda *_a, **_k: None
        try:
            got = activity.fetch_cps_day(object(), "2026-11-20")
            check("完整抓完", bool(got.get("complete")))
            check("只用有效单并优先 totalCosFee",
                  abs(float(got["total"]) - 2.4) < 0.01)
            check("查询条件是完成日",
                  calls and calls[0].get("cptTimeFlag") == 1
                  and calls[0].get("orderTimeFlag") == 0
                  and calls[0].get("startCptDay") == "2026-11-20")
            zero = activity.fetch_cps_day(object(), "2026-11-19")
            check("0 单日也标记完整（可缓存）", bool(zero.get("complete"))
                  and float(zero["total"]) == 0)
        finally:
            activity._post, activity.time.sleep = old_post, old_sleep
    finally:
        settings.DATA_DIR, settings.get_account = old_dir, old_get
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("失败", len(fails), "项：" + "；".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
