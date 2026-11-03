# -*- coding: utf-8 -*-
"""缓存管理页（清单/删除）与「全部账号」部分命中（先出已有部分，再后台补抓）。

用户 2026-11-03 反馈：月度利润整月有缓存能秒出，CPS 花费选「全部账号 + 上月」
却直接开始抓取 —— 因为 all_payload 要求每个账号都有精确整月文件，缺一个就整批抓。
现在改成：先用已有缓存出总览，缺的账号挂到后台任务上补抓，抓完前端自动刷新。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import paycomm, profit, settings, webapp  # noqa: E402

START, END = "2026-09-01", "2026-09-30"


def _cps_file(rep: Path, account: str) -> None:
    (rep / f"paycomm_{account}_{START}_{END}.json").write_text(json.dumps({
        "jobId": "t", "mode": "cps", "account": account, "schema": 1,
        "accountLabel": account, "start": START, "end": END,
        "rows": [{"skuId": 1001, "itemName": "测试品", "payCommAmount": 12.0}],
    }, ensure_ascii=False), encoding="utf-8")


def _profit_file(rep: Path, name: str, payload: dict) -> None:
    (rep / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    fails: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    old_dir = settings.DATA_DIR
    old_get = settings.get_account
    old_accs = list(settings.ACCOUNTS)
    tmp = Path(tempfile.mkdtemp(prefix="jdroi_cache_manage_"))
    settings.DATA_DIR = tmp
    settings.get_account = lambda a: {"key": a, "label": f"账号-{a}"}
    try:
        rep = tmp / "reports"
        rep.mkdir(parents=True, exist_ok=True)

        print("[1] 利润缓存清单：新格式 / 旧格式 / 口径过期")
        _profit_file(rep, f"profit_{START}_{END}.json", {
            "logicVersion": profit.LOGIC_VERSION, "accounts": ["main", "b"],
            "rows": [{"skuId": 1}, {"skuId": 2}], "totals": {"net": 12.5},
            "byAccount": {"all": {"rows": [{"skuId": 1}, {"skuId": 2}],
                                  "totals": {"net": 12.5}}}})
        _profit_file(rep, f"profit_all_{START}_{END}.json",
                     {"logicVersion": 5, "accounts": ["main"], "rows": [{"skuId": 1}]})
        aged = rep / "profit_2026-08-01_2026-08-31.json"
        _profit_file(rep, aged.name, {"logicVersion": profit.LOGIC_VERSION - 1,
                                      "accounts": ["main"], "rows": [{"skuId": 9}]})
        entries = profit.cache_entries()
        check("识别出新格式", any(e["start"] == START and not e["legacy"] for e in entries))
        check("旧命名标 legacy", any(e["legacy"] for e in entries))
        check("口径不符标 stale",
              any(e["start"] == "2026-08-01" and e["stale"] for e in entries))
        check("净利取 totals", any(abs(e["total"] - 12.5) < 0.01 for e in entries))

        print("[2] 删除：新格式按区间、legacy 按文件名、拒绝越界路径")
        got = profit.delete_cache(start=START, end=END)
        check("新格式按区间删掉", f"profit_{START}_{END}.json" in (got["deleted"] or []))
        check("legacy 不会被区间删除误删", (rep / f"profit_all_{START}_{END}.json").exists())
        got = profit.delete_cache(file=f"profit_all_{START}_{END}.json")
        check("按文件名删 legacy", f"profit_all_{START}_{END}.json" in (got["deleted"] or []))
        check("拒绝越界文件名", profit.delete_cache(file="../../etc/passwd")["ok"] is False)

        print("[3] 「全部账号」部分命中：先出已有部分 + 后台补抓")
        _cps_file(rep, "main")
        settings.ACCOUNTS[:] = [{"key": "main", "label": "账号-main"},
                                {"key": "b", "label": "账号-b"}]
        runner = paycomm.PayCommRunner("cps")
        started: dict = {}

        def fake_backfill(start: str, end: str, note: str = "") -> dict:
            started["note"] = note
            return {"ok": True, "jobId": "job-backfill", "account": "all",
                    "start": start, "end": end}

        runner.run_all_background = fake_backfill      # 不真的开浏览器
        out = webapp._paycomm_start(runner, "all", START, END)
        check("返回成功且标 partial", bool(out.get("ok")) and bool(out.get("partial")))
        check("缺的账号被点名", out.get("missing") == ["账号-b"])
        check("启动了后台补抓", started.get("note") and out.get("backfill") == "job-backfill")
        part = runner._results.get("job-backfill") or {}
        check("部分结果挂到补抓任务上", bool(part.get("partial")))
        check("部分结果就是已命中的那些行", len(part.get("rows") or []) == 1)
        check("warnings 说明正在补抓",
              any("后台补抓" in w for w in (part.get("warnings") or [])))

        runner._set(running=True, jobId="job-backfill", account="all", range=[START, END])
        snap = runner.snapshot(with_rows=True)
        check("后台跑的时候也能拿到部分行", len(snap.get("rows") or []) == 1)
        check("快照透传 partial", bool(snap.get("partial")))

        print("[4] 全部账号都有缓存：直接命中，不再补抓")
        _cps_file(rep, "b")
        started.clear()
        out2 = webapp._paycomm_start(runner, "all", START, END)
        check("完全命中 cached", bool(out2.get("cached")) and not out2.get("partial"))
        check("不再启动补抓", not started)

        print("[5] 花费缓存清单按账号改名显示最新名字")
        cps_entries = [e for e in paycomm.PayCommRunner("cps").cache_entries()
                       if e["account"] == "main"]
        check("账号名来自当前配置", bool(cps_entries) and
              cps_entries[0]["accountLabel"] == "账号-main")
        check("行数与合计正确", cps_entries[0]["rows"] == 1
              and abs(cps_entries[0]["total"] - 12.0) < 0.01)
    finally:
        settings.DATA_DIR = old_dir
        settings.get_account = old_get
        settings.ACCOUNTS[:] = old_accs
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("失败", len(fails), "项：" + "；".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
