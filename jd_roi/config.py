# -*- coding: utf-8 -*-
"""兼容层：老脚本用的 config，现在统一从 settings（环境变量驱动）取。

新代码请直接用 jd_roi.settings / jd_roi.dailystore。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import settings

BASE_DIR = settings.BASE_DIR
DATA_DIR = settings.DATA_DIR
AUTH_DIR = settings.AUTH_DIR
CONFIG_DIR = settings.CONFIG_DIR

# Excel 成本表
EXCEL_PATH = settings.EXCEL_PATH

# 登录态与浏览器用户目录（持久化，扫码一次后可复用）
STORAGE_STATE = AUTH_DIR / "jd_state.json"
USER_DATA_DIR = AUTH_DIR / "browser_profile"

# 登录入口
LOGIN_URL = os.getenv("JD_LOGIN_URL", "https://jxinquiry.jd.com/detail/bid")

# 目标站点
JZT_HOME = "https://jzt.jd.com/msa/#/list/tab/plan?objective=overview"
SZ_HOME = "https://jdsz.jd.com/szweb/view/product/product360.html"
REPORT_HOME = "https://jzt.jd.com/report/index.html/#/overview/page"
REPORT_KEYWORD = "https://jzt.jd.com/report/index.html/#/rtb/basic/keyword?hideNav=1"

# ---- 多账号 ----
ACCOUNTS = settings.ACCOUNTS
MAIN_ACCOUNT = settings.MAIN_ACCOUNT
ACCOUNT_ID = (ACCOUNTS[0].get("account_id") if ACCOUNTS else 0) or 0

# 只分析指定账号（None=全部）。v2 起两个账号都进广告口径
_scope = os.getenv("JD_SCOPE", "")
SCOPE_ACCOUNTS = [s.strip() for s in _scope.split(",") if s.strip()] or None


def in_scope(key: str) -> bool:
    return SCOPE_ACCOUNTS is None or key in SCOPE_ACCOUNTS


def get_account(key: str | None = None) -> dict:
    return settings.get_account(key)


def account_dir(key: str | None = None) -> Path:
    """老版抓取脚本的落盘目录：主账号 data/，其他 data/<key>/。"""
    if key in (None, "", MAIN_ACCOUNT):
        return Path(DATA_DIR)
    return Path(DATA_DIR) / str(key)


def parse_account(argv: list[str]) -> tuple:
    account, rest = None, []
    for a in argv:
        if a.startswith("--account="):
            account = a.split("=", 1)[1] or None
        else:
            rest.append(a)
    return account, rest


# 抓取参数（仅老脚本使用；v2 由网页选择区间）
DATE_START = os.getenv("JD_DATE_START", "2026-09-27")
DATE_END = os.getenv("JD_DATE_END", "2026-10-03")
DATE_CMP_START = os.getenv("JD_DATE_CMP_START", "2026-09-20")
DATE_CMP_END = os.getenv("JD_DATE_CMP_END", "2026-09-26")
DATE_LABEL = f"{DATE_START}~{DATE_END}"

HEADLESS = settings.HEADLESS

# ---- 模型接入（占位）----
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
