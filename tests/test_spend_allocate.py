# -*- coding: utf-8 -*-
"""京准通花费按 SKU 归集：四路口径 + 未归因 + 与计划级报表对账。

硬性要求：
  - 能定位 SKU 的（jst skuId / 创意落地页 / 计划 spuId / 全站营销 sxuId）记到商品；
  - 定位不到的**单列未归因**，绝不按比例摊到商品；
  - 已归集 + 未归因 = 计划级报表合计（= 京准通页面的快车/全站营销花费）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jd_roi import paycomm  # noqa: E402


def main() -> int:
    # 智能投放：两个 SKU
    jst_rows = [
        {"skuId": 100000001, "cost": "100.00", "campaignName": "商品_常规推广_智能化_A"},
        {"skuId": 100000002, "cost": "50.00", "campaignName": "商品_常规推广_智能化_B"},
    ]
    # 关键词创意：一条带落地页、一条没有落地页
    creative_rows = [
        {"campaignType": 2, "cost": "30.00", "campaignName": "关键词-A",
         "adName": "系统推荐", "landingPageUrl": "https://item.jd.com/100000001.html"},
        {"campaignType": 2, "cost": "20.00", "campaignName": "关键词-无落地页", "adName": "无"},
    ]
    # 快车计划级（含已删）：智能化 150、关键词 50、全站智能推广 3 个计划
    msa_plans = [
        {"campaignId": 1, "cost": "100.00", "campaignName": "智能-A",
         "fullMarketingScenarioTypeName": "商品-常规推广-智能化"},
        {"campaignId": 2, "cost": "50.00", "campaignName": "智能-B",
         "fullMarketingScenarioTypeName": "商品-常规推广-智能化"},
        {"campaignId": 3, "cost": "50.00", "campaignName": "关键词计划",
         "fullMarketingScenarioTypeName": "商品-常规推广-关键词"},
        {"campaignId": 11, "cost": "200.00", "campaignName": "全站-在投",
         "fullMarketingScenarioTypeName": "商品-常规推广-全站智能推广"},
        {"campaignId": 12, "cost": "70.00", "campaignName": "全站-已删-同名",
         "fullMarketingScenarioTypeName": "商品-常规推广-全站智能推广"},
        {"campaignId": 13, "cost": "30.00", "campaignName": "全站-已删-找不到",
         "fullMarketingScenarioTypeName": "商品-常规推广-全站智能推广"},
    ]
    # 快车计划列表：只含在投计划（已删计划没有 spuId）
    promolist_rows = [
        {"campaignId": 11, "campaignName": "全站-在投", "spuId": 200000001, "campaignType": 153},
        {"campaignId": 99, "campaignName": "全站-已删-同名", "spuId": 200000002, "campaignType": 153},
    ]
    # 全站营销：单品推广带 sxuId，全店推广没有
    swa_rows = [
        {"sxuId": 100000003, "cost": "80.00", "adName": "单品-雪莲果", "_swaType": 101},
        {"sxuId": 0, "cost": "20.00", "adName": "全店推广", "_swaType": 118},
    ]

    # 全站智能推广的 SPU 生效 SKU：200000001 这个 SPU 下 3 个 SKU 都在投（黑名单 0 个）
    shop_delivery = {
        200000001: {"skus": [200000001, 200000003, 200000004], "names": {200000003: "佛手柑2个装"},
                    "black": 0},
        200000002: {"skus": [200000002], "names": {}, "black": 1},
    }
    got = paycomm.allocate(jst_rows, creative_rows, msa_plans, promolist_rows, swa_rows,
                           shop_delivery=shop_delivery)
    by, tot = got["bySku"], got["totals"]

    assert by[100000001]["jst"] == 100.0 and by[100000001]["kw"] == 30.0, by[100000001]
    assert by[100000002]["jst"] == 50.0, by[100000002]
    assert by[100000003]["swa"] == 80.0, by[100000003]
    # 全站智能推广：SPU 下「生效 SKU」平分（200/3 → 66.67/66.67/66.66），尾差给最后一个
    assert by[200000001]["shop"] == 66.67, by[200000001]
    assert by[200000003]["shop"] == 66.67, by[200000003]
    assert by[200000004]["shop"] == 66.66, by[200000004]
    # 已删计划同名回找到 spu 200000002，其生效名单里只有它自己 → 整笔 70
    assert by[200000002]["shop"] == 70.0, by[200000002]
    assert tot["shopSplit"] == 1, tot

    # 未归因：关键词 20 + 全站智能推广 30 + 全站营销 20
    unattr = {u["key"]: u["cost"] for u in got["unattrRows"]}
    assert unattr.get("kw") == 20.0, unattr
    assert unattr.get("shop") == 30.0, unattr
    assert unattr.get("swa") == 20.0, unattr
    assert "other" not in unattr, unattr      # 三路齐了就不该有对账差额

    # 对账：快车已归集 + 未归因 = 计划级合计
    msa_covered = (tot["jst"] + tot["shopSmart"] + tot["keywordTotal"]
                   + unattr.get("shop", 0) + unattr.get("other", 0))
    assert round(msa_covered, 2) == tot["msaPlanTotal"] == 500.0, (msa_covered, tot)
    swa_covered = tot["swa"] + unattr.get("swa", 0)
    assert round(swa_covered, 2) == tot["swaPlanTotal"] == 100.0, (swa_covered, tot)
    # 总花费 = 各 SKU 合计 + 未归因
    assert tot["total"] == round(sum(v["total"] for v in by.values()) + tot["unattr"], 2)
    assert tot["total"] == 600.0, tot

    print("PASS 四路花费按 SKU 归集：能定位的到商品、定位不到的单列未归因、总额与计划报表一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
