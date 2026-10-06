# -*- coding: utf-8 -*-
"""商智指标编码（szgateway 的 indicator key）。"""
SZ_UV = "jdr_sch_traffic_brow_sku__page_cnt_traffic_plat_item_di_sz_bsg"      # 访客数
SZ_PV = "jdr_sch_traffic_brow_sku__page_qtty_traffic_plat_item_di_sz_bsg"     # 浏览量
SZ_AMT = "jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot"              # 成交金额
SZ_ORDN = "jdr_sch_trade_deal_ord_ord_qtty_sz_trade_deal_snapshot"            # 成交单量
SZ_QTY = "jdr_sch_trade_deal_ord_sku_qtty_sz_trade_deal_snapshot"             # 成交件数
SZ_CVR = "fo_jdr_sch_industry_deal_rate"                                      # 成交转化率（比率，不可加）
SZ_AOV = "fo_jdr_sch_trade_deal_ord_amt_user_sz_trade_deal_snapshot"          # 客单价（比率，不可加）
# 流量来源报表用到的两个 key
SRC_UV = "jdr_sch_traffic_brow_sku_cnt_jd_unified_attribution_sz"
SRC_AMT = "jdr_sch_traffic_intr_ord_ord_amt_jd_unified_attribution_trade_deal_snapshot_sz"

# 按天聚合时「可相加」的商品指标；其余（CVR/AOV/compare）一律丢弃或重算
PRODUCT_SUM = [SZ_AMT, SZ_ORDN, SZ_QTY, SZ_UV, SZ_PV]
FLOW_SUM = [SZ_AMT, SZ_ORDN, SZ_UV, SZ_PV]
