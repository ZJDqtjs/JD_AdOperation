# 京东广告运营分析服务（JD Ad ROI Service）

把「京准通 + 商智」的抓取、按天入库、任意区间分析、可视化报表封装成一个**可 Docker 部署的常驻服务**：

- **每天 0 点自动**抓取前一天的两个账号数据，**按天**存进 /data
- 容器停机几天再起来会**自动补齐空洞**（缺口 ∪ 最近 N 天回刷，上限 `JD_MAX_BACKFILL_DAYS`）
- 京准通是「点击后 15 天归因」，所以每晚还会**回刷最近 N 天**（默认 15 天）修正数据
- 打开网页即可**自选任意区间**（昨天 / 近 2 天 / 近 3 天 / 近 14 天 / 任意日期）生成**聚合**分析报告
- 报表在原有基础上升级：区间选择器、自动环比（前一等长周期）、操作日志逐条复盘、数据覆盖提示

---

## 1. 快速开始

    git clone <this-repo> jd-ad && cd jd-ad
    cp .env.example .env
    docker compose up -d --build

打开 http://localhost:8000 。

首次启动后需要**扫码登录**（登录态是抓取的前提）：

1. 打开 http://localhost:8000/login
2. 选择账号 → 点「开始登录」
3. 用**手机京东 App** 扫描页面上的二维码
4. 状态变成「已登录」即成功；两个账号要**分别登录**
5. 回到控制台，点「补近3天」或选日期「立即抓取」

登录态保存在 ./docker-auth/<账号key>，容器重启后依然有效；JD 会话通常几天到两周过期，过期后再扫一次即可。

---

## 2. 目录与数据

    docker-data/                                  # = 容器内 /data
      daily/<账号>/<YYYY-MM-DD>/<source>.json.gz  # 按天入库的原始数据
      state.json                                  # 运行状态（最近一次抓取等）
    docker-auth/                                  # = 容器内 /auth  浏览器 Profile / 登录态
    docker-config/                                # = 容器内 /config
      accounts.json                               # 账号配置（可选）
      costs.json                                  # 每个 SKU 的真实成本（可选，强烈建议）
      计算roi公式.xlsx                             # 成本表（Excel「计算公式」sheet）

source 取值：

| source | 含义 |
|---|---|
| jzt_campaign | 京准通概览-计划 |
| jst_campaign | 智能投放-计划 |
| jst_sku | 智能投放-商品（含 skuId，用于和商智对齐） |
| jst_searchword | 智能投放-搜索词 |
| kw | 快车-关键词 |
| sz_product | 商智-商品明细 |
| sz_flow | 商智-流量概况 |
| oplog | 京准通操作日志（调整记录） |

**任意区间分析 = 把这些按天文件聚合**，所以查询很快，也不受接口限流影响。

---

## 3. 配置

全部通过环境变量（见 .env.example / docker-compose.yml）：

| 变量 | 默认 | 说明 |
|---|---|---|
| TZ | Asia/Shanghai | 时区，决定「0 点」是哪里的 0 点 |
| JD_WEB_PORT | 8000 | 宿主机映射端口 |
| JD_SCHEDULE_HOUR / JD_SCHEDULE_MINUTE | 0 / 5 | 每日抓取时间。0:05 是为了等前一天商智结算 |
| JD_REFRESH_DAYS | 15 | 每晚回刷最近多少天（对齐 15 天归因窗口） |
| JD_MAX_BACKFILL_DAYS | 120 | 停机后最多补多少天，防止一次补一年 |
| JD_RUN_ON_START | 1 | 启动时发现昨天没数据就补跑 |
| JD_ENABLE_SCHEDULER | 1 | 关掉就只保留手动抓取 |
| JD_SCOPE | 空 | 只处理指定账号（逗号分隔），被排除的账号既不抓取也不进报表 |
| JD_HEADLESS | 1 | 容器内必须无头 |
| JD_SW_MAX_ROWS | 2000 | 搜索词按花费降序截断行数（覆盖约 99.9% 花费，避免限流） |

### 账号

优先级：环境变量 JD_ACCOUNTS > /config/accounts.json > 内置默认。

    [
      {"key": "main", "label": "主账号", "account_id": 10000000001},
      {"key": "b",    "label": "账号B",  "account_id": 10000000002}
    ]

account_id 是「快车-关键词报表」接口需要的 pinIds。

### 成本（重要）

保本 ROI = 1 ÷ 毛利率。毛利率取不到真数就只能拍全店均值，而**生鲜各品毛利差好几倍**，
共用一条线会把「其实打平」的商品判成「可以放大」。所以成本口径分三层，自动降级：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 供货方 ERP 接口 `JD_COSTS_URL` / `JD_COSTS_TOKEN` | 每个 SKU 的**每件结算价 + 货款 + 运费 + 包材 + 人工**，报表按各自保本线判定 |
| 2 | `/config/costs.json` | 手工维护的同结构文件（接口不通、或想临时覆盖某个 SKU 时使用） |
| 3 | Excel「计算公式」sheet | **全店一套**成本；文件也缺失时退化为内置示例口径（客单价 100 / 成本 70 / 运费 5 → 毛利率 25% → 保本 ROI 4.00） |

第 1 层的口径（供货方接口的 `supply` 是**京东结算给我们的钱 = 收入**，不是成本，别再乘一遍扣点）：

```
单件贡献 = supply − _goodsCost − shipping − package − labor
毛利率   = 单件贡献 ÷ 件单价（件单价 = 商智该 SKU 成交额 ÷ 件数）
保本 ROI = 1 ÷ 毛利率        放量 ROI = 保本 × 1.25
```

`_goodsCost`（我方买货成本）是第 1 层唯一的成本字段；第 2 层等价字段叫 `goodsCost`。
未命中接口/文件的 SKU 会**回落到第 3 层并在报表标 `*`**，成本来源、覆盖率、未接入清单都印在总结页。
接口字段与待补充项见 `SKU成本接口对接说明.md`。Token 只写 `.env`（已在 `.gitignore`），不要提交。

本地手工维护时，`/config/costs.json` 结构与接口响应一致：

    {
      "default": {"shipping": 5.0, "goodsCost": 40.0, "package": 0.0, "labor": 0.0, "returnRate": 0.05},
      "skus": {
        "100000000001": {"supply": 70.0, "goodsCost": 45.0, "shipping": 5.0, "package": 0.6, "labor": 0.4}
      }
    }

入仓品「袋 ↔ 京东件」换算不确定时，写 `/config/costs_units.json`（`{"100000000001": 2}`，见
`config/costs_units.example.json`）；供货方接口补上 `unitsPerSale` 后这个文件就不需要了。

**成本表模板**：仓库提供 `config/roi-template.xlsx` —— 4 个 sheet（预算分配 / 计算公式 / 搜推词分析 /
竞品分析表）的结构、公式与口径说明齐全，数据全部为示例值。复制成 `config/计算roi公式.xlsx` 后
填入自己的客单价 / 成本 / 扣点 / 运费即可。真实成本表不入库。

---

## 4. 网页功能

| 页面 | 说明 |
|---|---|
| / | 控制台：区间选择器、数据覆盖日历、手动抓取/补数、任务与登录状态 |
| /report?start=&end=&accounts= | 任意区间聚合报表（顶部自带区间选择器） |
| /login | 扫码登录 |
| /api/docs | 自动生成的 API 文档 |

报表页签：① 总结 ② 商品ROI交叉表 ③ 调整复盘 ④ 计划明细 ⑤ 搜索词诊断 ⑥ 行动清单。

**区间怎么用**：以所选区间为最新一期，自动向前取两个**等长**区间做环比。
例如选 2026-10-01 ~ 2026-10-03（3 天），会对比 9/28~9/30 和 9/25~9/27。

**看绝对效率**用 7~14 天（受归因延迟影响小）；**看即时反应**用 1~3 天。

---

## 5. 常用 API

    curl localhost:8000/api/status
    curl localhost:8000/api/coverage
    curl "localhost:8000/api/analysis?start=2026-10-01&end=2026-10-03"
    curl -X POST "localhost:8000/api/run?start=2026-10-01&end=2026-10-03&force=1"
    curl -X POST "localhost:8000/api/login/start?account=main"

---

## 6. 不用 Docker 也能跑

    uv venv && uv pip install -r requirements.txt
    python -m playwright install chromium
    export JD_DATA_DIR=./data JD_AUTH_DIR=./auth JD_CONFIG_DIR=./config
    python -m uvicorn jd_roi.webapp:app --host 0.0.0.0 --port 8000

单次抓取（不启服务）：

    python -m jd_roi.scrape_day --days 3
    python -m jd_roi.scrape_day 2026-09-27 2026-10-03
    python -m jd_roi.scrape_day --account=b --force 2026-10-03 2026-10-03
    python -m jd_roi.migrate_daily

---

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 报表显示「数据不完整」 | 该区间有日期没入库。到控制台按日期「立即抓取」补齐 |
| 抓取报「登录态已失效」 | JD 会话过期。打开 /login 重新扫码 |
| 容器内扫码扫不了 | 无头浏览器遇到滑块/短信验证时无法完成。**替代方案**：在本地电脑跑一次 `python -m jd_roi.login --account=main`，然后把生成的 `auth/browser_profile`（或 `auth/<key>`）整个目录 + `auth/jd_state.json` 拷进 `./docker-auth/<key>/`，容器直接复用 |
| 报表出现「口径降级」提示 | 该区间缺 jzt_campaign，账号总额退化为智能投放口径（不含全站智能推广） |
| 商智某些天为空 | 商智**只提供到昨天**，且 endDate >= 今天 会返回空数组（不报错） |
| 抓取报 -3010 限流 | 搜索词接口限流，脚本会自动退避重试；也可调小 JD_SW_MAX_ROWS |
| 想改抓取时间 | 改 JD_SCHEDULE_HOUR / JD_SCHEDULE_MINUTE 后 docker compose up -d |
| `docker build` 在 pip 阶段报 `ReadTimeoutError` | 容器内直连 pypi.org 太慢。Dockerfile 已带 `--retries 5 --timeout 60`；国内建议在 `.env` 里设 `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`（`.env.example` 默认已给），构建会快很多 |

---

## 8. 架构

    scrape_day.py  ──►  dailystore (/data/daily/...)  ──►  analyze2.build(start,end)
         ▲                        ▲                                │
         │ APScheduler 每天 0:05   │ 网页选区间                      ▼
      webapp.py  ─────────────────┘                          report2.render()

- jd_roi/settings.py   全部路径与开关（环境变量驱动）
- jd_roi/dailystore.py 按天读写 + 区间聚合（gzip JSON）
- jd_roi/scrape_day.py 按天抓取，可续跑、限流退避、商智签名头自动重捕
- jd_roi/analyze2.py   任意区间分析（windows_for(start,end) 生成三期）
- jd_roi/report2.py    自包含 HTML（内联 ECharts）
- jd_roi/webapp.py     FastAPI + APScheduler + 扫码登录会话

---

## 9. 验证

一条命令跑全部测试（不需要登录、不需要联网）：

    python tests/run_all.py

包含 4 部分、共 90+ 项断言：

| 套件 | 覆盖 |
|---|---|
| tests/test_range_agg.py | 按天入库 → 任意区间加总：3 天 / 单天 / 含缺失天区间、操作日志按区间切片、覆盖度 |
| tests/test_pipeline_e2e.py | 冷启动空目录不报 500、夜间抓取编排逐天写库、二次运行跳过、区间聚合、报表渲染、**真起 uvicorn 打 HTTP** |
| tests/test_scrape_live_http.py | **真实 Chromium + 把京东域名 route 打桩**，跑真实抓取代码：分页合并、-3010 退避重试、isDaily 按天拆分、商智 capture 动态签名头、操作日志按天落盘 |
| tests/test_scheduler.py | CronTrigger 下一次触发 = 明天 00:05 (Asia/Shanghai)、连续两天同一时刻、任务体抓「到昨天为止的 N 天」窗口 |
| tests/test_container_layout.py | 按 Dockerfile 的 ENV 与目录约定跑一遍：目录自动创建、开关生效、成本表缺失退化、该布局下 Web 能起能出报表 |
| tests/test_login_flow.py | 真实浏览器 + 打桩登录页：自动点 .scan-login 切扫码、二维码可截、**服务端未放行时不会误报已登录**、放行后 storage_state 落盘 |
| tests/run_all.py 内嵌 | Dockerfile COPY 源存在、requirements 可导入、EXPOSE 与 compose 端口一致、entrypoint 指向正确、echarts 资源在镜像内 |

测试用打桩替换掉浏览器与 HTTP（scrape_day.launch_profile / fetch_* / SzFetcher），
所以能在无登录、无网络的环境下把「调度 → 抓取 → 按天入库 → 区间聚合 → 报表 → Web」整条链路跑通。

测试里出现的失败都是真问题，跑测试时别放过。已经这样抓到并修掉 6 个：操作日志读错数据源、
商智汇总行被按天聚合丢掉、没有操作的天不落盘、保本线写死值与公式值打架、
搜索词日抓用错接口 key（会静默漏掉全部搜索词）、操作日志调用签名错误被 except 吞掉。

### 容器层：本机实际执行过的验证

| 检查 | 命令 | 结果 |
|---|---|---|
| 依赖层可解析 | `uv venv tmp/imgvenv && uv pip install -r requirements.txt` | 全绿；干净 venv 里 `import jd_roi.webapp` 成功，17 条路由 |
| 入口脚本 | `bash docker/entrypoint.sh`（把 uvicorn 换成打印桩） | 目录自动创建、示例配置铺到 /config、成本表缺失给出告警、uvicorn 参数正确 |
| 浏览器 | `playwright install --with-deps chromium` | 镜像里执行（本机已有 chromium 可复用） |
| **镜像构建** | WSL2(Arch) 内 `docker build -t jd-roi:latest .` | 构建成功：`jd-roi:latest`（1.89GB，内容 535MB），含 chromium / chromium-headless-shell / ffmpeg |
| **容器运行** | `docker compose up -d --build` | 容器 `jd-roi` healthy；`/healthz` 200、`/login` 正常（含账号密码登录页签）、17 条路由；entrypoint 自动建目录并把示例配置铺到 /config |

> 这台机器没有 Docker Desktop，WSL 的 Arch 又停在 2020 年（glibc 2.31，装不了新包）。
> 解法是用 Docker 官方**静态二进制**（`download.docker.com/linux/static/stable/x86_64/`）
> 解压到 `/opt/docker-static` 并软链到 `/usr/local/bin`，不动任何系统包；
> 再配 `registry-mirrors`（daocloud / 1ms）绕开被墙的 `auth.docker.io`。

### 端到端链路验收（已跑通）

两个账号各自扫码登录后，用真实接口跑完整链路。下列为**示例数值，非真实经营数据**：

| 步骤 | 结果 |
|---|---|
| 抓取 | `python -m jd_roi.scrape_day --account=b 2026-09-13 2026-10-06` → **23 天、0 错误**；每天 8 类数据源（概览/投放计划/投放商品/搜索词/关键词/商智商品/商智流量/操作日志）全部落盘 |
| 主账号 | 已入库 15 天 |
| 区间分析 | 5 天窗口（两账号）：花费 **¥19,000 → ¥18,000 → ¥18,000**、成交 **¥89,000 → ¥69,000 → ¥69,000**、ROI **4.6 → 3.9 → 3.8**；商智总成交 ¥134,000，广告贡献 **51%** |
| 调整复盘 | 该区间 3 条可评估调整：**2 条正确、1 条有问题**（示例商品D ROI 2.4→3.3、示例商品A礼盒 3.6→5.7、示例商品B 2.4→1.9） |
| 容器内复算 | 同一区间在容器里 `/report` 返回 **HTTP 200**，页面里就是同一组数字，容器状态 `healthy` |

> 抓取过程中真实暴露并修掉了 3 个 bug：操作日志用新开空白页发 fetch（origin 为 about:blank → `Failed to fetch`）、
> 搜索词日抓用错接口 key、操作日志调用签名错误被 `except` 吞掉。前两个都是"跑起来正常、数据是空的"这类静默失败。

**全部验收项都已跑通**：Docker 构建 + 容器运行 + 真实抓取 + 任意区间报表 + 8 个测试套件全绿。

---

## 10. 附：原有的本地脚本用法与安全说明

> 以下为升级前（v2）的本地脚本流程，仍然可用；新部署建议直接用上面的服务方式。

# JD_AdOperation

京东广告投放（**京准通 / 商智**）数据抓取与 **ROI 运营分析**工具。

用 Playwright 复用已扫码登录的浏览器 Profile，抓取京准通的推广计划、智能投放、搜索词与操作日志，以及商智的商品/流量/广告数据；
**按 SKU 跨店铺合并**广告与成交，回填 ROI 模板 Excel，并生成自包含的可视化分析报告（内联 ECharts，离线可看）。

> 本仓库**只包含代码与文档**。抓取数据、截图、Excel、登录态一律不入库，详见 [隐私与安全](#隐私与安全)。

---

## 目录结构

```
.
├── README.md
├── jd-ad-roi-optimizer_SKILL.md   # 完整流程 SKILL（口径、规则、踩坑记录，最详细的参考）
├── .gitignore
└── jd_roi/
    ├── config.py                  # 全局配置：路径、站点、日期窗口、多账号
    ├── local_config.example.py    # 本地私有配置模板（真实账户 ID 放 local_config.py，不入库）
    ├── browser.py                 # 持久化 Profile 启动 / 登录态判定
    ├── login.py                   # 扫码登录，保存登录态
    ├── capture.py                 # 抓包辅助
    ├── store.py                   # 抓取结果落盘（data/*.json）
    ├── scrape_all.py              # 一键抓取多窗口（w0/w1/w2）报表
    ├── scrape_daily.py            # 日粒度抓取（调整前后对比用）
    ├── scrape_jzt.py / scrape_jst_report.py / scrape_keyword.py   # 单接口抓取
    ├── scrape_sz.py               # 商智（含动态签名头捕获）
    ├── scrape_oplog.py            # 操作日志（调整记录）
    ├── analyze.py / analyze2.py   # 分析：保本 ROI、词分层、跨账号按 SKU 合并
    ├── report_html.py / report2.py# 生成自包含 HTML 报告
    ├── fill_excel.py              # 回填 Excel（只写数值，保留公式）
    ├── adjust_bid.py              # 批量调价
    ├── probe_*.py                 # 接口探针（排查用）
    ├── explore*.py                # 页面探索（排查用）
    ├── diagnose.py                # 登录态 / Cookie 诊断
    └── assets/echarts.min.js      # 内联到报告，离线可看
```

## 环境准备

- Python 3.12
- 依赖：`playwright`、`openpyxl`

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install playwright openpyxl
playwright install chromium
```

### 配置账户 ID

真实账户 ID 属于私有信息，不入库。取值优先级：

1. 环境变量 `JD_ACCOUNT_ID_MAIN` / `JD_ACCOUNT_ID_B`
2. 本地未跟踪文件 `jd_roi/local_config.py`
3. `config.py` 中的占位符（仅用于跑通流程）

```bash
cp jd_roi/local_config.example.py jd_roi/local_config.py
# 然后填入真实账户 ID
```

## 使用流程

```bash
# 1) 扫码登录（必须 headed，一次即可长期复用）
python -m jd_roi.login                     # 主账号
python -m jd_roi.login --account=b         # 其他账号（各自独立 Profile）

# 2) 抓取：多个对比窗口（格式 w<key>=<起>:<止>）
python -m jd_roi.scrape_all --account=main w0=2026-09-13:2026-09-19 w1=2026-09-20:2026-09-26 w2=2026-09-27:2026-10-03
python -m jd_roi.scrape_daily 2026-09-01 2026-10-05 --account=main   # 日粒度

# 3) 操作日志（调整记录），接口跨度大时要分段抓取
python -m jd_roi.scrape_oplog sweep 2026-08-15 2026-10-05 --account=main

# 4) 分析 + 出报告
python -m jd_roi.analyze2                  # 跨账号按 SKU 交叉 + 调整复盘 -> data/analysis2.json
python -m jd_roi.report2                   # -> ad_roi_report_v2.html

# 5) 回填 ROI 模板
python -m jd_roi.fill_excel
```

抓取结果统一落在 `data/[<account>/]*.json`（本地生成，不入库）。
时间窗口注意：**商智只提供到「昨天」**，窗口含今天会返回空数据且不报错。

核心口径（保本 ROI = 1 / 毛利率、点击 15 天成交口径、搜索/推荐拆分、词分层与定档规则等）见
[`jd-ad-roi-optimizer_SKILL.md`](./jd-ad-roi-optimizer_SKILL.md)。

## 隐私与安全

仓库通过 `.gitignore` 强制排除以下内容，**请勿用 `-f` 强行提交**：

| 排除项 | 原因 |
|---|---|
| `auth/`、`browser_profile*/` | 登录态 Cookie / localStorage，可被用于操作账号 |
| `*.har` | 抓包文件，含完整请求头与 Cookie |
| `data/` | 抓取数据与分析产物，含账号、店铺、商品与经营数据 |
| `*.png` / `*.jpg` 等图片 | 页面截图会露出账号昵称与账户 ID |
| `ad_roi_report*.html` | 报告内联了完整经营数据 |
| `*.xlsx` | Excel 含客单价/成本/扣点等经营参数 |
| `jd_roi/local_config.py`、`.env*` | 真实账户 ID 等本地私有配置 |
| `tmp/`、`*.log`、`__pycache__/`、`.venv/`、IDE 目录 | 临时产物与本地环境 |

在本仓库中，示例与文档一律使用**占位符**（如 `ACCOUNT_ID = 10000000001`），不写入真实账号、店铺或金额。
提交前建议自检：确认待提交文件中不含账号名、手机号、店铺昵称与真实账户 ID。

## 免责声明

本工具仅用于**自有店铺**的广告投放数据分析。请遵守京东开放平台及各站点服务条款，
合理控制抓取频率，避免对线上服务造成压力。
