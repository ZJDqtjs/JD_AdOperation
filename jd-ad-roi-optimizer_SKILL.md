---
name: "jd-ad-roi-optimizer"
description: "Scrape JD Jingzhuntong/Shangzhi ad data with Playwright, backfill Excel, and produce per-product ROI optimization advice (keyword add/negate, target ROI, budget split). Invoke when the user asks to analyze JD ad account ROI, 京准通/商智 data, or optimize 投产比/关键词, or when switching to a new shop/account."
---

# 京东广告 ROI 运营优化分析（可换店/换账号复用）

把「登录 → 抓数 → 回填 Excel → 出每个商品的优化建议」固化为标准流程。换店铺只需改账号参数，
换模型只需重新执行本 SKILL（数据与结论都以接口真实返回为准）。

## 0. 触发场景
- 用户要求分析京东广告账户/店铺的投产比、关键词优化、预算分配
- 用户给出 `计算roi公式.xlsx` 之类的 ROI 模板要求「完成它」
- 用户说「换店/换账号，再跑一遍」
- 关键词：京准通、商智、投产比、ROI、搜索词、否定词、预算分配

## 1. 前置环境（一次性）
- 使用 **uv 虚拟环境**；依赖：`playwright`、`openpyxl`；浏览器 `chromium`
- 运行统一用 `uv run python -m jd_roi.<module>`（或 `<venv>\Scripts\python.exe -m jd_roi.<module>`）
- 登录态：京东现行票据为 `pin / pinId / thor / sdtoken`（**没有 pt_key**，勿用 pt_key 判断）
- 登录方式：**扫码**，必须 headed；使用**持久化 Profile**（保留 cookie + localStorage）：
  `pw.chromium.launch_persistent_context(user_data_dir=auth/browser_profile, headless=False)`
- 抓取用同一 Profile 的 `headless=True`

## 2. 参数化配置（换店核心）
在 `jd_roi/config.py` 修改：
| 变量 | 含义 |
|---|---|
| `LOGIN_URL` | 登录门户（如 `https://jxinquiry.jd.com/detail/bid`） |
| `ACCOUNT_ID` | 当前店铺账号ID（搜索词接口 `pinIds` 用） |
| `DATE_START/END` | 本期日期（格式 `YYYY-MM-DD`） |
| `DATE_CMP_START/END` | 同期对照日期 |
| `EXCEL_PATH` | ROI 模板路径 |
| `USER_DATA_DIR` | 持久化 Profile 目录（换店建议换新目录，避免串号） |
| `LLM_*` | 后续接入其他模型的占位（provider/api_key/base_url/model） |

成本参数（客单价、产品成本、运费、京东扣点/包装）不在 config，而是读 Excel「计算公式」sheet
（示例：H2 客单价、H3 成本、H4 扣点、H5 包装、H6 运费）——用于算保本ROI，见 §4/§6。

换店流程：改 `ACCOUNT_ID/DATE_*/USER_DATA_DIR` → 删掉旧 Profile → 重新扫码登录。
若使用新 Excel 模板，只需同步 §5 的「字段映射表」。

### 2.1 多账号 / 多店铺（跨店铺按 SKU 合并）
一个操作者常同时管多个京东账号，可能是**不同店铺**（各有独立商智），且**同一商品的主投在另一个账号**；
单账号抓取会出现「商智总成交远大于广告成交」的假异常。用 `ACCOUNTS` 同时登录并按 **SKU 合并**：
```python
ACCOUNTS = [
    {"key": "main", "label": "主账号", "account_id": 111, "user_data_dir": "auth/browser_profile"},
    {"key": "b",    "label": "账号B", "account_id": 222, "user_data_dir": "auth/browser_profile_b"},
]
```
（也可用环境变量 `JD_ACCOUNTS='[...]'` 覆盖）
- 每个账号**一个独立持久化 Profile**，逐个扫码：`python -m jd_roi.login --account=b`
- 抓取带 `--account=b`，数据落到 `data/b/`（主账号仍在 `data/`）
- **每个店铺的商智都要抓**（商智是店铺级、口径不同）：`scrape_sz product/flow ... --account=b`
- 合并规则（`analyze.py` 自动完成）：
  - **SKU 是跨店铺主键**：京准通「智能投放-商品」返回 `skuId`，与商智 `sku_id` 完全一致（同店铺命中率100%）
  - 广告数据按 `skuId` **跨账号累加**（同款在不同账号投放会合并）
  - 商智数据**按店铺分别保留**（`shops` 字段），报告标出每店成交
  - 广告渗透率 = 广告成交 ÷ 各店商智成交合计；>100%（归因跨期）标记为不可比
- 报告「按 SKU 关联」区：广告花费/成交/ROI（跨账号合并）× 商智总成交（分店）/访客/转化 × 广告渗透率

## 3. 数据抓取（接口清单）
京准通为 SPA，数据走 JSON 接口。**在已登录页面的上下文内 `page.evaluate(fetch)`**，
带上 `credentials:'include'` 与业务头即可自动带 Cookie：

```js
headers: {'Content-Type':'application/json','accept':'application/json, text/plain, */*',
          'referer':'https://jzt.jd.com/','language':'zh_CN','siteid':'0','loginmode':'0'}
```

| 数据 | 站点(先访问建立SSO) | 接口 |
|---|---|---|
| 推广计划总览 | `jzt.jd.com/msa/#/list/tab/plan?objective=overview` | `POST atoms-api.jd.com/dspad/msa/promolist/overview/campaign` |
| 智能投放-账户 | `jzt.jd.com/jst/#/report/account` | `POST jzt-api.jd.com/reweb/jst/account/account/list` |
| 智能投放-计划 | 同上 | `POST jzt-api.jd.com/reweb/jst/account/campaign/list` |
| 智能投放-商品 | 同上 | `POST jzt-api.jd.com/reweb/jst/account/sku/list`（**含 skuId，用于与商智精确关联**） |
| **智能投放-搜索词** | 同上 | `POST jzt-api.jd.com/reweb/jst/effect/searchword/list` |
| 智能投放-订单/地域 | 同上 | `POST /reweb/jst/effect/order/list`、`/location/list` |
| 快车-关键词报表 | `jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1` | `POST jzt-api.jd.com/reweb/msa/orientation/keyword/list` |
| 报表中心首页(SSO) | `jzt.jd.com/report/index.html/#/overview/page` | — |
| 商智-商品明细 | `jdsz.jd.com/szweb/view/product/productDetail.html` | `POST szgateway.jd.com/api/lowcode/productDetail/table/productTable.ajax` |
| 商智-流量概况 | `jdsz.jd.com/szweb/view/flow/flow-summary.html` | `POST /api/lowcode/flowSummary/getCoreSummary.ajax`、`/productFlow/getFlowSrcTop.ajax` |
| 商智-广告概况 | `jdsz.jd.com/szweb/view/market/advert-summary.html` | `POST /api/lowcode/flow/payFlow/advertSummary/getSummaryData.ajax` |
| **操作日志（调整记录）** | `jzt.jd.com/logging/#/business?businessType=-16` | `POST jzt-api.jd.com/logplatform/common/list/query` |
| 操作日志-枚举 | 同上 | `POST /logplatform/common/business/info`、`/logplatform/common/list/opt` |

公共请求体（按需覆盖）：
```json
{"isDaily":false,"startDay":"YYYY-MM-DD","endDay":"YYYY-MM-DD","obys":"impressions|desc",
 "filters":[],"clickOrOrderDay":15,"clickOrOrderCaliber":0,"orderStatusCategory":1,
 "page":1,"pageSize":200,"giftFlag":0,"promotionMode":null,"columns":["..."],"requestFrom":0}
```
- **必须分页**：以返回行数 `< pageSize` 作为终止条件（搜索词常有数千行）。
- 智能投放接口用 `referer: https://jzt.jd.com/jst/`；快车关键词用报表页 referer。
- 搜索词接口返回 `searchTerm`（该词维度）；`promotionMode` 目前恒为 0。
- **商智网关(szgateway)要求动态签名头 `user-mnp` / `user-mup` / `uuid`**，无法手工构造。
  做法：先打开对应商智页面，用 Playwright 监听其自身请求**捕获这些头与维度(bsBrand/bsCate3)**，
  再用 `page.evaluate(fetch)` 复用这些头请求目标日期；`scrape_sz.py` 已封装（`SzSession`）。

抓取命令（本仓库 `jd_roi/` 已实现）：
```bash
# ---- 推荐流程（v2）----
python -m jd_roi.login                                   # 扫码登录（headed，一次）
python -m jd_roi.scrape_oplog sweep 2026-08-15 2026-10-05 --account=main   # 操作日志（按周分段）
python -m jd_roi.scrape_oplog sweep 2026-08-15 2026-10-05 --account=b
python -m jd_roi.scrape_all --account=main w0=2026-09-13:2026-09-19 w1=2026-09-20:2026-09-26 w2=2026-09-27:2026-10-03
python -m jd_roi.scrape_all --account=b    w0=2026-09-13:2026-09-19 w1=2026-09-20:2026-09-26 w2=2026-09-27:2026-10-03
python -m jd_roi.scrape_daily 2026-09-01 2026-10-05 --account=main   # 日粒度（用于调整前后对比）
python -m jd_roi.scrape_daily 2026-09-01 2026-10-05 --account=b
python -m jd_roi.analyze2        # 跨账号按SKU交叉 + 操作日志复盘 -> data/analysis2.json
python -m jd_roi.report2         # 生成自包含 HTML -> ad_roi_report_v2.html

# ---- 单点抓取（排查/补数用）----
python -m jd_roi.scrape_jzt / scrape_jst_report / scrape_keyword / scrape_sz   # 旧版单接口脚本仍可用
```
结果统一落地 `data/*.json`（`store.save`），便于分步复跑与换模型后重新分析。
`jd_roi/analyze.py` 把 §6 的分析规则（保本ROI、三档定档、词分层、建议动作）固化为代码，
并**按商品名模糊匹配商智 SKU**（2-gram 重叠，忽略产地/单位等通用词），合并「广告成交 vs 商智总成交」
得到**广告渗透率**；输出结构化 `analysis.json`。
`jd_roi/report_html.py` 渲染为自包含 HTML（**内联 ECharts**，离线可看）：
KPI、可视化图表（花费/成交对比、投产比排名带保本线、搜索/推荐结构、广告渗透率）、
每个商品卡片（KPI、环比、商智 SKU 对照、搜索/推荐拆分、高效/低效/废词、建议动作与目标ROI）。
ECharts 库缓存于 `jd_roi/assets/echarts.min.js`（首次需联网下载，之后离线内联）。

## 4. 关键口径
- **保本ROI = 1 / 毛利率**。全店 Excel 口径：毛利率 =（客单价 − 产品成本 − 运费 − 京东扣点 − 包装）/ 客单价。
  接入供货方 ERP 后按 SKU 算，术语统一为「**前台件单价**」（消费者实付 = 商智成交额 ÷ 件数）与「**到手结算价**」（供货方字段 `supply`）：
  毛利率 =（到手结算价 − 货款 − 运费 − 包材 − 人工）÷ 前台件单价，详见 `SKU成本接口对接说明.md` 与 README 第 3 节。
- 报价对比统一用口径：**点击15天内累计成交订单**（`clickOrOrderDay=15`、`clickOrOrderCaliber=0`）
- 智能投放计划 = 搜索流量 + 推荐流量：
  **搜索词下数据 = Σ 该计划所有 searchTerm；推荐数据 = 计划总 − 搜索词合计**
- 注意「概览」与「智能投放报表」的订单/金额口径可能不同（成交订单 vs 下单订单），
  同一次分析内**必须保持口径一致**，并在结论里注明。

## 5. Excel 回填
用 `openpyxl` 以 `data_only=False` 打开，**只写数值、保留原有公式**（ROI=成交额/花费 等）。
标准 4-sheet 模板与字段映射：
| Sheet | 写入内容 |
|---|---|
| 预算分配 | 总访客量（商智）；付费/免费访客若「无权限/未订购商享版」则留空并注明，不推算 |
| 计算公式 | 客单价/成本/运费等参数，及保本ROI、保本PPC 公式 |
| 搜推词分析 | 每个计划三段：`计划总数据 / 搜索词下数据 / 推荐数据（公式=差值）`；M 列写「效果不好的词」 |
| 竞品分析表 | 自身按场景（智能化/关键词/全站智能推广…）真实展现；竞品列无接口时留空并注明 |

换模板时改 `jd_roi/fill_excel.py` 里的单元格坐标映射即可。

## 6. 分析与建议规则（换店后自动套用）
对每个计划/商品计算本期与同期：
1. **定档**
   - `ROI ≥ 4` → 🟢 放大：提预算 20~30% / 提高核心词出价
   - `保本ROI ≤ ROI < 4`（保本线附近）→ 🟡 优化：抠词、砍推荐、调出价
   - `ROI < 保本ROI` → 🔴 止损：降预算、否定无效词、必要时暂停
2. **词分层**（按 plan × searchTerm 聚合，去重相加）
   - 高效词：`订单≥2 且 ROI>保本` → 提价/加词
   - 废词：`花费≥阈值(默认5~8元) 且 订单=0` → 否定
   - 低效词：`花费≥10 且 ROI<保本` → 降价或精准否定
3. **搜索 vs 推荐**：推荐流量 ROI 明显偏低（尤指 <保本甚至为负）时，
   按模板备注启用方案：在 **快车-关键词-成交-投产比出价** 新建计划承接。
4. **目标设定**：ROI<保本的品目标先做到 **保本×1.05~1.1**；ROI>4 的品目标 **+0.5** 并同步加预算。
5. **输出结构**：先给全局总览表（花费/ROI/上期ROI/档位），再逐品给
   「现状 → 关键词增删（可直接复制） → 出价/预算动作 → 目标ROI」。

## 7. 换店 / 换模型 操作清单
1. 改 `config.py`（`ACCOUNTS` 账号列表、日期、成本参数、Excel 路径）
2. 逐账号扫码：`python -m jd_roi.login [--account=<key>]`（每个账号一个独立 Profile）
3. 逐账号抓取京准通 + **该店铺的商智**（都带 `--account=<key>`），落 `data/[<key>/]`
4. `python -m jd_roi.fill_excel` 回填
5. `python -m jd_roi.analyze` + `python -m jd_roi.report_html --open` 出分析结果与可视化报告
   （analyze 自动跨账号按 skuId 合并；接入其他模型时把 `data/analysis.json` 作为 prompt 输入）

## 7.5 v2 分析口径（analyze2 + report2）

v2 相对 v1 的四个修正，都是踩过坑之后改的：

1. **两个账号都必须进广告口径**。v1 默认 `JD_SCOPE=b`，只把账号B的广告算进去，
   却把两个店铺的商智成交都算进渗透率分母，导致渗透率虚高/超过 100%。
   v2 统计「广告」时**两个账号全部相加**，商智仍按店铺分别保留。
2. **三窗口对比**：w0 调整前 / w1 第一轮调整 / w2 最近一周，全部 7 天，互不重叠。
3. **成本口径**：Excel 只给全店一套成本（模板示例：毛利率 25%、保本 ROI=4.00；真实值以你的成本表为准）。
   曾试图「按客单价等比缩放产品成本 + 固定运费」算每个商品自己的保本线，
   结果低价商品的保本 ROI 算出 20~63 这种荒谬值 —— **该做法已弃用**。
   现在统一用 Excel 毛利率，输出「毛利/单 = 真实客单价 × 毛利率」与
   「预估净利 = 商智成交 × 毛利率 − 广告花费」，并附**毛利率敏感性**（20%→保本5.00、25%→4.00、30%→3.33、35%→2.86）。
   **要真正判定单品盈亏，必须拿到每个 SKU 的真实供货价/运费。**
4. **计划类型拆分**：概览里 `campaignType` 61=智能化、2=快车-关键词、153=全站智能推广。
   - 61/2 的明细来自智能投放报表（有 skuId）；
   - **153 不在智能投放报表里**，但它自带 `spuId` = 商智 sku_id，v2 用「一计划=一商品」把它补进 SKU 表，
     否则会漏掉主账号约 40% 的花费。

## 7.6 调整复盘（把操作日志变成结论）

`analyze2.adjustments()` 把每条**人工**操作与该计划的**日粒度**效果对齐：

- 有日数据：`调整前7天` vs `调整后7天`（用 `jst_campaign_daily`，`isDaily=true`）
- 无日数据（如全站智能推广）：退化为**周窗口** w0→w1 或 w1→w2
- 判定规则：`正确 / 有问题 / 效果不明显 / 存疑 / 已止损 / 观察中 / 无基线 / 无法评估`
- **没有基线就不判定**：新建计划、长期暂停、已删除的计划，"0 → X" 不是提效，标为「无基线」
- 状态变更要看方向：「暂停→有效」是**重启**，「有效/启动→暂停」才是止损（正则解析操作详情）

## 8. 注意事项
- 无头抓取前先访问对应站点首页建立 SSO，否则接口可能返回 `code:-401 非法请求`
- 所有接口都要分页，只看第一页会严重低估花费与废词
- Cookie 失效表现为接口 401 或跳 `passport.jd.com`，需重新扫码
- 不写入任何账号/密钥到本 SKILL；账号信息只放在 `config.py`（本地）
- 结论必须基于接口真实返回，禁止用模板里的示例数字当结果
- **操作日志接口会静默截断**：`beginTime~endTime` 跨度太大（实测 8/15~10/5）只返回 8 月记录。
  必须**按 7 天分段抓取再合并**（`scrape_oplog sweep` 已实现，按 optTime+目标ID+内容去重）
- **商智只提供到「昨天」**：`endDate >= 今天` 时接口返回 `code:0` 但 `data:[]`（不报错！）。
  分析窗口必须落在昨天之前，否则商智全是空的
- **商智动态签名头 `user-mnp/user-mup/uuid` 存活时间很短**：一次会话里先抓别的报表再抓商智会返回
  `rawCode:-407` 或空数组。`scrape_all` 已改为**每次请求前重新开页面捕获头**
- **智能投放搜索词接口限流 `code:-3010`**（"操作次数已超过上限"）：单窗口近万行、几十页必然触发。
  解决：搜索词按 `obys:"cost|desc"` 排序后**只取前 2000 行**（覆盖 99.9% 花费），
  并按页 sleep 0.35s + 命中限流后退避 22s 重试（`scrape_all`）
- **搜索词接口不返回 `campaignId`**，只有 `campaignName`，聚合必须以计划名为键
- 接口返回的商品名里含 `&amp;` 等 HTML 实体，渲染前要 `html.unescape` 再转义，否则页面出现 `&amp;amp;`
- 沙箱下 `tempfile` 默认目录不可写会让 Playwright 报 `EPERM ... playwright-artifacts`，
  跑脚本前把 `TEMP/TMP` 指到工作区目录内

## 9. v3：容器化常驻服务（按天入库 + 任意区间）

v2 是「选定固定区间、跑一次出一次报表」；v3 把它变成**常驻服务**：

| 能力 | 实现 |
|---|---|
| 每天 0 点自动抓前一天 | `jd_roi/webapp.py` 里的 APScheduler（`JD_SCHEDULE_HOUR/MINUTE`，时区 `TZ`） |
| 按天入库 | `jd_roi/dailystore.py` → `<DATA_DIR>/daily/<账号>/<YYYY-MM-DD>/<source>.json.gz` |
| 任意区间分析 | `analyze2.windows_for(start,end)` 生成「前两期/前一期/所选区间」三期 |
| 网页选区间 | `/report?start=&end=&accounts=`，顶栏自带区间选择器与预设（昨天/近3天/近7天…） |
| 扫码登录 | `/login`：无头浏览器切到 `.scan-login` 扫码模式，把二维码截图给前端轮询 |
| 手动补数 | `POST /api/run?start=&end=&force=`（后台线程 + 全局锁） |

### 关键设计取舍

1. **按天存、区间算**：接口支持区间查询，但如果每次都去拉接口，任意区间就会频繁触发限流。
   按天落盘后，任意区间都是本地聚合，秒级返回，且不受限流影响。
2. **回刷窗口 `JD_REFRESH_DAYS`（默认 15）**：京准通是「点击后 15 天归因」，昨天的数据后面还会长。
   所以每晚不是只抓昨天，而是重抓最近 15 天覆盖写入。商智当天结算后不再变化。
3. **商智只能抓到「昨天」**：`endDate >= 今天` 时接口返回 `code:0` + `data:[]`（**不报错**）。
   所以默认区间一律「到昨天为止」。
4. **登录判定必须问服务端**：`pin` cookie 还在、但服务端会话已过期时，只看 cookie 会误判为已登录。
   统一用 `browser.session_ok(page)`（`/common/logininfo` 返回 `code==1`）。
5. **无头浏览器必须先访问 jzt 首页建立 SSO**，否则 jzt-api 直接返回 `-100 登录失败`。
6. **京麦登录页（passport.shop.jd.com）默认是密码登录**，二维码要先点右上角 `.scan-login` 图标才会出现。

### 命令

```bash
# 服务
uvicorn jd_roi.webapp:app --host 0.0.0.0 --port 8000

# 按天抓取（可续跑）
python -m jd_roi.scrape_day --days 3
python -m jd_roi.scrape_day 2026-09-27 2026-10-03 --force

# 任意区间分析（等价于网页做的事）
python -m jd_roi.analyze2 2026-10-01 2026-10-03
python -m jd_roi.report2        # 生成 ad_roi_report_v2.html

# 把历史结果导入按天库
python -m jd_roi.migrate_daily
```

### 相关环境变量

`JD_DATA_DIR` / `JD_AUTH_DIR` / `JD_CONFIG_DIR` / `JD_EXCEL_PATH` / `TZ` /
`JD_SCHEDULE_HOUR` / `JD_SCHEDULE_MINUTE` / `JD_REFRESH_DAYS` / `JD_RUN_ON_START` /
`JD_ENABLE_SCHEDULER` / `JD_SW_MAX_ROWS` / `JD_WEB_PORT`。
账号：`JD_ACCOUNTS`（JSON 字符串）> `<CONFIG_DIR>/accounts.json` > 内置默认。
