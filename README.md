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
