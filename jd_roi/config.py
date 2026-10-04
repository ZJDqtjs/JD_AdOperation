# -*- coding: utf-8 -*-
"""全局配置：路径、站点、抓取参数、多账号、模型API（后续可插拔）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EXCEL_PATH = BASE_DIR / "计算roi公式.xlsx"

# 登录态与浏览器用户目录（持久化，扫码一次后可复用）
AUTH_DIR = BASE_DIR / "auth"
STORAGE_STATE = AUTH_DIR / "jd_state.json"
# 使用独立 user_data_dir，避免与用户日常 Chrome 配置冲突/被占用
USER_DATA_DIR = AUTH_DIR / "browser_profile"

# 登录入口（用户指定的登录门户）
LOGIN_URL = "https://jxinquiry.jd.com/detail/bid"

# 目标站点
JZT_HOME = "https://jzt.jd.com/msa/#/list/tab/plan?objective=overview"
SZ_HOME = "https://jdsz.jd.com/szweb/view/product/product360.html"
# 报表中心（关键词/搜索词报表所在 SPA）
REPORT_HOME = "https://jzt.jd.com/report/index.html/#/overview/page"
REPORT_KEYWORD = "https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1"

# ---- 京准通账户 ID（pinIds）----
# 真实账户 ID 属私有信息，不写入仓库。取值优先级：
#   1) 环境变量 JD_ACCOUNT_ID_MAIN / JD_ACCOUNT_ID_B
#   2) 本地未跟踪文件 jd_roi/local_config.py（见 local_config.example.py，已被 .gitignore 忽略）
#   3) 下方占位符（仅用于跑通流程；抓数前请配置真实值，否则搜索词接口会取错账户）
_PLACEHOLDER_ACCOUNT_ID_MAIN = 10000000001
_PLACEHOLDER_ACCOUNT_ID_B = 10000000002

try:  # 本地私有配置（可选，不入库）
    from .local_config import ACCOUNT_ID_B as _LOCAL_ID_B  # type: ignore
    from .local_config import ACCOUNT_ID_MAIN as _LOCAL_ID_MAIN  # type: ignore
except Exception:  # noqa: BLE001  未提供本地配置时退回占位符
    _LOCAL_ID_MAIN, _LOCAL_ID_B = _PLACEHOLDER_ACCOUNT_ID_MAIN, _PLACEHOLDER_ACCOUNT_ID_B

ACCOUNT_ID = int(os.getenv("JD_ACCOUNT_ID_MAIN") or _LOCAL_ID_MAIN)
ACCOUNT_ID_B = int(os.getenv("JD_ACCOUNT_ID_B") or _LOCAL_ID_B)

# ---- 多账号（可同时登录多个京东/京准通账号，最终按 SKU 合并）----
# 每个账号一个独立持久化 Profile；account_id 用于搜索词/商智等接口
# 用法：python -m jd_roi.login --account=b   （首次逐个扫码）
#       python -m jd_roi.scrape_xxx ... --account=b
# 也支持环境变量覆盖：JD_ACCOUNTS='[{"key":"b","label":"账号B","account_id":123,"user_data_dir":"..."}]'
ACCOUNTS = [
    {"key": "main", "label": "主账号", "account_id": ACCOUNT_ID,
     "user_data_dir": str(AUTH_DIR / "browser_profile")},
    {"key": "b", "label": "账号B", "account_id": ACCOUNT_ID_B,
     "user_data_dir": str(AUTH_DIR / "browser_profile_b")},
]
_env_accounts = os.getenv("JD_ACCOUNTS")
if _env_accounts:
    try:
        ACCOUNTS = json.loads(_env_accounts)
    except Exception:  # noqa: BLE001
        pass
MAIN_ACCOUNT = ACCOUNTS[0]["key"]

# 只分析指定账号（None=全部账号）。默认只分析账号B；可用环境变量 JD_SCOPE=main,b 覆盖
_scope = os.getenv("JD_SCOPE", "b")
SCOPE_ACCOUNTS = [s.strip() for s in _scope.split(",") if s.strip()] or None


def in_scope(key: str) -> bool:
    return SCOPE_ACCOUNTS is None or key in SCOPE_ACCOUNTS


def get_account(key: str | None = None) -> dict:
    """按 key 取账号配置，缺省取第一个（主账号）。"""
    for a in ACCOUNTS:
        if a.get("key") == (key or MAIN_ACCOUNT):
            return a
    return ACCOUNTS[0]


def account_dir(key: str | None = None) -> Path:
    """账号数据目录：主账号用 data/，其他账号用 data/<key>/（向后兼容）。"""
    if key in (None, "", MAIN_ACCOUNT):
        return BASE_DIR / "data"
    return BASE_DIR / "data" / str(key)


def parse_account(argv: list[str]) -> tuple[str | None, list[str]]:
    """从命令行参数中解析 --account=<key>，返回 (account, 其余位置参数)。"""
    account, rest = None, []
    for a in argv:
        if a.startswith("--account="):
            account = a.split("=", 1)[1] or None
        else:
            rest.append(a)
    return account, rest


# 抓取参数（对比区间：调整前 9/14-9/20 vs 调整后 9/21-9/27，9/21 起做调整）
DATE_START = "2026-09-21"
DATE_END = "2026-09-27"
DATE_CMP_START = "2026-09-14"
DATE_CMP_END = "2026-09-20"
DATE_LABEL = "9/21-9/27"

# 无头模式：登录必须 headed（扫码）；抓取可用 headless
HEADLESS = os.getenv("JD_HEADLESS", "0") == "1"

# ---- 模型接入（可插拔） ----
# 现阶段由本 AI 直接充当分析器；后续可设置 API 调用其他模型。
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local")  # local | openai | deepseek | ...
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# 输出
DATA_DIR = BASE_DIR / "data"
