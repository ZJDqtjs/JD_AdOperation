# -*- coding: utf-8 -*-
"""行业大盘：缓存命中、规则层观察、AI 摘要与报表页签的离线回归。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["JD_ENABLE_SCHEDULER"] = "0"
os.environ["JD_INDUSTRY"] = "1"
os.environ["JD_INDUSTRY_AUTO_FETCH"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import analyze_ai, industry, report2, settings  # noqa: E402

START, END, KEY = "2026-10-01", "2026-10-07", "main"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def payload() -> dict:
    whole = [{"date": f"2026-10-0{i}", "impressionExponent": 100000 + i * 1000,
              "clickExponent": 3000 + i * 100, "ctr": "3.00", "cvs": "9.71",
              "cartCVS": "51.50"} for i in range(1, 8)]
    hourly = [{"date": f"{h:02d}", "clickExponent": 1000 + h * 10, "ctr": "3.00"}
              for h in range(24)]
    return {
        "schema": industry.SCHEMA, "kind": "industry", "account": KEY,
        "accountLabel": "主账号", "start": START, "end": END,
        "fetchedAt": "2026-10-08T01:00:00",
        "category": {"id": 13575, "name": "根茎类", "path": "生鲜/蔬菜/根茎类"},
        "categoryOptions": [{"id": 13575, "name": "根茎类", "path": "生鲜/蔬菜/根茎类"}],
        "caliber": industry.CALIBER,
        "datasets": {
            "whole": whole, "hourly": hourly,
            "industryDetail": [{"businessType": -4, "ctr": "3.00", "cvs": "9.71"}],
            "selfDetail": [{"businessType": -4, "clicks": 1000, "impressions": 20000,
                            "totalOrderCnt": 80, "cost": 1000.0, "roi": "4.00",
                            "cartCVS": "40.00", "ctr": "5.00", "cvs": "8.00"}],
            "industryCustomer": [], "selfCustomer": [],
            "area": [
                {"mappedAreaName": "广东省", "impressionsPercentage": "12.83",
                 "orderPercentage": "16.79", "ctr": "3.44", "cvs": "11.08",
                 "impressionRank": 1, "orderRank": 1},
                {"mappedAreaName": "北京市", "impressionsPercentage": "8.01",
                 "orderPercentage": "10.81", "ctr": "3.20", "cvs": "12.27",
                 "impressionRank": 2, "orderRank": 2},
            ],
            "brands": [{"no": 1, "name": "京鲜生", "ctr": "4.47", "cvs": "15.92",
                        "selfBrandFlag": 1}],
            "compete": [{"keyWord": "红薯",
                         "self": {"roi": "2.00", "ctr": "4.00", "cvs": "5.00",
                                  "cpc": "1.20", "kwImpressionBid": "0.80"},
                         "industry": {"ctr": "7.79", "cvs": "17.22",
                                      "kwImpressionBid": "0.80"}}],
            "billboard": [{"keyWord": "红薯", "impressionExponent": "266676",
                           "clickExponent": "20784", "ctr": "7.79", "cvs": "17.22",
                           "kwImpressionBid": "0.80"}],
            "hotWords": [{"keyWord": "红薯", "keyWordRank": "1",
                          "searchExponent": "73126", "kwCompetitiveness": "74966"},
                         {"keyWord": "木薯", "keyWordRank": "2",
                          "searchExponent": "8000", "kwCompetitiveness": "1000"}],
            "soaringWords": [{"keyWord": "红薯新鲜", "keyWordRank": "1",
                              "riseRate": "191.60", "searchExponent": "3690",
                              "kwCompetitiveness": "0"}],
            "newWords": [{"keyWord": "新鲜芋头", "keyWordRank": "1",
                          "searchExponent": "2019", "kwCompetitiveness": "0"}],
            "brandHotWords": [{"keyWord": "榴莲蜜薯", "keyWordRank": "1",
                               "searchExponent": "19586", "kwCompetitiveness": "2969"}],
            "brandSoaringWords": [], "brandNewWords": [],
        },
        "summaries": {
            "industry": {"ctr": "3.00", "cvs": "9.71", "cartCVS": "51.50",
                         "impressionExponent": "22686580", "clickExponent": "680435"},
            "industryCustomer": {"newCustomerCntPercentage": "68.48",
                                 "newCustomerCost": "34.81",
                                 "oldCustomerCntPercentage": "31.52"},
            "selfCustomer": {"newCustomerCntPercentage": "61.96",
                             "newCustomerCost": "35.81",
                             "oldCustomerCntPercentage": "38.04"},
        },
        "counts": {},
    }


def fake_data(ind: dict) -> dict:
    return {
        "meta": {"range": {"start": START, "end": END},
                 "windows": [{"key": "w0", "start": START, "end": END, "label": "前两期"},
                             {"key": "w1", "start": START, "end": END, "label": "前一期"},
                             {"key": "w2", "start": START, "end": END, "label": "本期"}],
                 "accounts": [{"key": KEY, "label": "主账号", "accountId": 1}],
                 "generatedAt": "2026-10-08 01:00", "caliber": "测试口径",
                 "breakevenFlat": 4.0},
        "accounts": [{"key": KEY, "label": "主账号", "totals": {}}],
        "plans": [], "skus": [], "noAd": [], "typeSplit": {},
        "words": {KEY: {"w2": {"测试计划": {"sw": [], "kw": [
            {"word": "玉米", "cost": 10, "amt": 50, "ord": 1, "clk": 10, "imp": 200,
             "roi": 5, "kind": "good", "be": 4.0},
            {"word": "红薯", "cost": 100, "amt": 200, "ord": 10, "clk": 100, "imp": 1000,
             "roi": 2, "kind": "low", "be": 4.0}]}}}},
        "targeting": {"area": {KEY: {"w2": [{"name": "广东", "cost": 100}]}},
                      "crowd": {KEY: {"w2": []}}},
        "targetingGaps": [], "hourly": {}, "hourlyGaps": [],
        "ops": [], "adjustments": [], "runtime": {},
        "advice": {"summary": [], "combined": {}},
        "industry": ind,
    }


def main() -> int:
    print("=" * 78)
    print("行业大盘（缓存 / 观察 / AI 摘要 / 报表页签）")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as td:
        old = settings.REPORT_DIR
        settings.REPORT_DIR = Path(td)
        try:
            industry._write_cache(payload())
            cached = industry._read_cache(KEY, START, END)
            check("缓存按账号+区间命中", cached is not None and
                  cached["category"]["path"] == "生鲜/蔬菜/根茎类")

            entries = industry.cache_entries()
            check("缓存清单列出行业大盘", len(entries) == 1 and
                  entries[0]["rows"] > 0 and entries[0]["total"] == 1000.0,
                  f"rows={entries[0]['rows'] if entries else '-'}")

            ens = industry.ensure([KEY], START, END, auto=False)
            check("查询先命中缓存、不发请求", not ens["missing"] and KEY in ens["blocks"])

            ind = industry.collect(fake_data({}))
            check("规则层产出行业观察", ind["available"] and
                  len(ind["accounts"]) == 1 and len(ind["observations"]) >= 4)
            a = ind["accounts"][0]
            check("本店 vs 行业比率差已算好",
                  a["self_vs_industry"]["ctr_gap_pp"] == 2.0 and
                  a["self_vs_industry"]["cvr_gap_pp"] == -1.71,
                  f"ctr_gap={a['self_vs_industry']['ctr_gap_pp']} "
                  f"cvr_gap={a['self_vs_industry']['cvr_gap_pp']}")
            check("地域/热词缺口进入建议", bool(ind["suggestions"]) and bool(ind["gaps"]) and
                  any("木薯" in x for x in a["uncovered_words"]),
                  f"uncovered={a['uncovered_words']}")
            acts = a.get("keyword_actions") or []
            check("关键词出价建议结合本店表现",
                  any(x["word"] == "红薯" and x["status"] == "已投" and
                      x.get("suggest_bid") for x in acts) and
                  any(x["word"] == "木薯" and x["status"] == "未投" and
                      x.get("suggest_bid") for x in acts),
                  f"actions={[(x['word'], x['status'], x.get('suggest_bid')) for x in acts[:5]]}")
            check("趋势与时段图数据齐全",
                  len(ind["charts"]["accounts"][0]["trend"]) == 7 and
                  len(ind["charts"]["accounts"][0]["hourly"]) == 24)

            d = fake_data(ind)
            digest = analyze_ai.build_digest(d)
            check("AI 摘要含 industry 与缺口",
                  digest["industry"]["available"] and bool(digest["industry_gaps"]) and
                  "指数" in str(digest["industry"].get("caliber") or ""))
            compact = industry.compact_for_ai(ind)
            check("AI 摘要不塞大表",
                  compact["available"] and len(compact["accounts"][0]["top_regions"]) <= 6)

            other = dict(ind, missing=["other"], status={"running": True, "accounts": [KEY], "queued": []})
            mine = dict(ind, missing=[KEY], status={"running": True, "accounts": [KEY], "queued": []})
            check("自动刷新只针对本页缺失账号",
                  not report2._industry_pending(other) and report2._industry_pending(mine))

            html = report2.page_industry(d, ai=None)
            check("报表页签渲染地域/关键词/建议",
                  "行业大盘观察" in html and "广东省" in html and "红薯" in html and
                  "关键词出价建议" in html and "规则层建议" in html and
                  "AI 行业总结与建议" in html)
            check("行业页使用账号切换而不是纵向罗列",
                  "indtabs" in html and "indacct" in html and "indwrap" in html)
            check("页签列表包含 p-ind", any(cid == "p-ind" for cid, _ in report2.TABS))

            res = industry.delete_cache(KEY, START, END)
            check("缓存只由用户显式删除", res["ok"] and not industry.cache_entries() and
                  industry._read_cache(KEY, START, END) is None)

            industry._mark_failed(KEY, START, END)
            cool = industry.ensure([KEY], START, END, auto=True)
            check("补齐失败冷却期内不自动重试",
                  cool["retryLater"] == [KEY] and not cool["autoStarted"])
            forced = industry.ensure([KEY], START, END, force=True, auto=False)
            check("显式重抓绕过冷却",
                  forced["retryLater"] == [] and forced["missing"] == [KEY])
            industry._clear_failed(KEY, START, END)
        finally:
            settings.REPORT_DIR = old

    print("")
    if FAILS:
        print("FAILED:", "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
