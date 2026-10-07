# -*- coding: utf-8 -*-
"""集中式配置（环境变量驱动，容器友好）。

所有路径都可用环境变量覆盖，默认相对应用根目录：
    JD_APP_DIR      应用根目录（默认仓库根）
    JD_DATA_DIR     数据目录（按天入库的 json.gz + 状态文件）
    JD_AUTH_DIR     浏览器持久化 Profile / 登录态
    JD_CONFIG_DIR   账号配置与成本表
    JD_EXCEL_PATH   成本表 xlsx（Excel「计算公式」sheet）
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return default if v is None or v == "" else v


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(_env("JD_APP_DIR", str(Path(__file__).resolve().parent.parent)) or ".")
DATA_DIR = Path(_env("JD_DATA_DIR", str(BASE_DIR / "data")) or ".")
AUTH_DIR = Path(_env("JD_AUTH_DIR", str(BASE_DIR / "auth")) or ".")
CONFIG_DIR = Path(_env("JD_CONFIG_DIR", str(BASE_DIR / "config")) or ".")
EXCEL_PATH = Path(_env("JD_EXCEL_PATH", str(CONFIG_DIR / "计算roi公式.xlsx")) or ".")
ACCOUNTS_FILE = Path(_env("JD_ACCOUNTS_FILE", str(CONFIG_DIR / "accounts.json")) or ".")
COSTS_FILE = Path(_env("JD_COSTS_FILE", str(CONFIG_DIR / "costs.json")) or ".")
# 进程临时目录（Playwright 的 artifacts 也落这里）
TMP_DIR = Path(_env("JD_TMP_DIR", str(DATA_DIR / "tmp")) or ".")
# 「已入库日期」缓存的存活秒数（Web 与抓取是两个进程，靠 TTL 感知对方写入）
DAYS_CACHE_TTL = float(_env("JD_DAYS_CACHE_TTL", "30") or 30)

# 历史 Profile 目录名（老版本用的是 auth/browser_profile、auth/browser_profile_b）
_LEGACY_PROFILE = {"main": "browser_profile", "b": "browser_profile_b"}


def default_profile_dir(key: str) -> str:
    """容器里用 <AUTH_DIR>/<key>；本地若已存在老 Profile 则复用，免去重新扫码。"""
    new = AUTH_DIR / key
    legacy = AUTH_DIR / _LEGACY_PROFILE.get(key, "__none__")
    if not new.exists() and legacy.exists():
        return str(legacy)
    return str(new)


def excel_path() -> Path:
    """成本表：优先 JD_EXCEL_PATH / config/，回退到仓库根目录的同名文件。"""
    p = Path(_env("JD_EXCEL_PATH", str(CONFIG_DIR / "计算roi公式.xlsx")) or ".")
    if p.exists():
        return p
    for cand in (BASE_DIR / "计算roi公式.xlsx", BASE_DIR / "cr.xlsx",
                 CONFIG_DIR / "costs.xlsx", CONFIG_DIR / "计算roi公式.xlsx"):
        if cand.exists():
            return cand
    return p

DAILY_DIR = DATA_DIR / "daily"
REPORT_DIR = DATA_DIR / "reports"
STATE_FILE = DATA_DIR / "state.json"

# ---------------- 调度 ----------------
TZ = _env("JD_TZ", _env("TZ", "Asia/Shanghai")) or "Asia/Shanghai"
SCHEDULE_HOUR = _env_int("JD_SCHEDULE_HOUR", 0)
SCHEDULE_MINUTE = _env_int("JD_SCHEDULE_MINUTE", 5)
# 每晚回刷最近 N 天：京准通是「点击后15天归因」，昨天的数据后面还会长，必须反复刷新
REFRESH_DAYS = _env_int("JD_REFRESH_DAYS", 15)
RUN_ON_START = _env_bool("JD_RUN_ON_START", True)
# 停机后补数据的上限天数（防止一次补一年）
MAX_BACKFILL_DAYS = _env_int("JD_MAX_BACKFILL_DAYS", 120)

# ---------------- 抓取 ----------------
HEADLESS = _env_bool("JD_HEADLESS", True)
SW_MAX_ROWS = _env_int("JD_SW_MAX_ROWS", 2000)
# 商智页面要等它自己发请求才能捕获动态签名头，等多久
SZ_CAPTURE_WAIT = _env_int("JD_SZ_CAPTURE_WAIT", 13000)
PAGE_SLEEP = float(_env("JD_PAGE_SLEEP", "0.35") or 0.35)
RETRY_SLEEP = float(_env("JD_RETRY_SLEEP", "22") or 22)

# ---------------- Web ----------------
WEB_HOST = _env("JD_WEB_HOST", "0.0.0.0") or "0.0.0.0"
WEB_PORT = _env_int("JD_WEB_PORT", 8000)
ENABLE_SCHEDULER = _env_bool("JD_ENABLE_SCHEDULER", True)

SYNC_ENABLED = _env_bool("JD_SYNC_ENABLED", False)
SYNC_BASE_URL = _env("JD_SYNC_BASE_URL", "") or ""

# ---------------- 账号 ----------------
# 默认账号一律用占位符：真实账户 ID 属私有信息，不入库。
# 覆盖方式（优先级从高到低）：
#   1) JD_ACCOUNTS 环境变量（JSON 数组）
#   2) config/accounts.json（本地私有文件，已列入 .gitignore / .dockerignore）
DEFAULT_ACCOUNTS = [
    {"key": "main", "label": "主账号", "account_id": 10000000001},
    {"key": "b", "label": "账号B", "account_id": 10000000002},
]


def _normalize(accs: list) -> list:
    out = []
    for a in accs:
        key = str(a.get("key") or "").strip()
        if not key:
            continue
        acc = dict(a)
        acc["key"] = key
        acc["label"] = a.get("label") or key
        acc["account_id"] = a.get("account_id") or 0
        acc["user_data_dir"] = a.get("user_data_dir") or default_profile_dir(key)
        out.append(acc)
    return out


def load_accounts() -> list:
    """账号来源优先级：JD_ACCOUNTS 环境变量 > accounts.json > 内置默认。"""
    raw = os.getenv("JD_ACCOUNTS")
    if raw:
        try:
            return _normalize(json.loads(raw))
        except Exception:  # noqa: BLE001
            pass
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("accounts") or []
            if data:
                return _normalize(data)
        except Exception:  # noqa: BLE001
            pass
    return _normalize(DEFAULT_ACCOUNTS)


ACCOUNTS = load_accounts()

# JD_SCOPE：只处理其中几个账号（其余账号既不抓取也不出现在报表里）
_scope = [s.strip() for s in (_env("JD_SCOPE", "") or "").split(",") if s.strip()]
if _scope:
    _kept = [a for a in ACCOUNTS if a["key"] in _scope]
    if _kept:
        ACCOUNTS = _kept
    else:
        print(f"[settings][warn] JD_SCOPE={_scope} 未匹配到任何账号，已忽略", flush=True)

MAIN_ACCOUNT = ACCOUNTS[0]["key"] if ACCOUNTS else "main"


def get_account(key: str | None = None) -> dict:
    for a in ACCOUNTS:
        if a["key"] == (key or MAIN_ACCOUNT):
            return a
    return ACCOUNTS[0] if ACCOUNTS else {"key": "main", "label": "主账号", "account_id": 0,
                                         "user_data_dir": str(AUTH_DIR / "main")}


EXCEL_PATH = excel_path()


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUTH_DIR, CONFIG_DIR, DAILY_DIR, REPORT_DIR, TMP_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
    use_tmp_dir()


def use_tmp_dir() -> str:
    """把进程的临时目录钉到工作区内。

    Playwright 启动浏览器时会用 tempfile 建 playwright-artifacts-XXXX 目录；
    如果系统的 TEMP 不可写（受限沙箱、只读根文件系统等），登录/抓取会直接 EPERM 失败。
    这里统一改到 <DATA_DIR>/tmp，容器内外都稳。
    """
    p = str(TMP_DIR)
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return p
    os.environ["TMPDIR"] = p
    os.environ["TEMP"] = p
    os.environ["TMP"] = p
    try:
        import tempfile
        tempfile.tempdir = p
    except Exception:  # noqa: BLE001
        pass
    return p


def load_costs() -> dict:
    """可选：每个 SKU 的真实供货价/运费，用于把「保本ROI」算准。

    config/costs.json 形如：
    {"default": {"shipping": 5.0, "platformRate": 0.0, "returnRate": 0.05},
     "skus": {"100000000001": {"supply": 70.0, "price": 100.0, "shipping": 5.0}}}
    """
    if not COSTS_FILE.exists():
        return {}
    try:
        return json.loads(COSTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
