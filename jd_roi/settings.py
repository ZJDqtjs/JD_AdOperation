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
import re
import threading
import time
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


def _load_dotenv() -> None:
    """本地直接跑脚本（不经 docker-compose）时也认仓库根的 .env。

    只填「进程环境里还没有」的键，已 export 的优先；解析失败就整体忽略。

    例外：TZ / JD_TZ 例外处理——业务时区必须由项目 .env 说了算。宿主机（尤其是
    Windows 上用 bash 启动的进程）常带 TZ=UTC，一旦它盖过 .env，整个应用会按 UTC
    算「今天/昨天」，导致每晚抓错日期（少一天）。因此这两个键总是用 .env 覆盖。
    """
    root = Path(_env("JD_APP_DIR", str(Path(__file__).resolve().parent.parent)) or ".")
    f = root / ".env"
    if not f.exists():
        return
    force = {"TZ", "JD_TZ"}          # 这两个键以 .env 为准，不被进程环境污染
    try:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not k or not v:
                continue
            if k in force or k not in os.environ:
                os.environ[k] = v
    except OSError:
        return


_load_dotenv()

BASE_DIR = Path(_env("JD_APP_DIR", str(Path(__file__).resolve().parent.parent)) or ".")
DATA_DIR = Path(_env("JD_DATA_DIR", str(BASE_DIR / "data")) or ".")
AUTH_DIR = Path(_env("JD_AUTH_DIR", str(BASE_DIR / "auth")) or ".")
CONFIG_DIR = Path(_env("JD_CONFIG_DIR", str(BASE_DIR / "config")) or ".")
EXCEL_PATH = Path(_env("JD_EXCEL_PATH", str(CONFIG_DIR / "计算roi公式.xlsx")) or ".")
ACCOUNTS_FILE = Path(_env("JD_ACCOUNTS_FILE", str(CONFIG_DIR / "accounts.json")) or ".")
COSTS_FILE = Path(_env("JD_COSTS_FILE", str(CONFIG_DIR / "costs.json")) or ".")
# 供货方 ERP 的「每个 SKU 真实成本」接口；留空 = 不启用，退回本地 costs.json / Excel 全店口径
COSTS_URL = (_env("JD_COSTS_URL", "") or "").strip()
COSTS_TOKEN = (_env("JD_COSTS_TOKEN", "") or "").strip()
COSTS_TIMEOUT = _env_int("JD_COSTS_TIMEOUT", 20)
COSTS_TTL = _env_int("JD_COSTS_TTL", 6 * 3600)          # 拉到的结果本地缓存 6 小时
COSTS_CACHE_FILE = Path(_env("JD_COSTS_CACHE", str(DATA_DIR / "costs_cache.json")) or ".")
# 成本表**按区间**各存一份（data/costs/costs_<from>__<to>.json）。
# 供货方接口给的是「该区间的均价」，10/1~10/27 与 10/1~10/29 是两张不同的表：
# 以前只有一份全局 costs_cache.json，后查的区间会把先查的覆盖掉，同一 SKU 的
# 货款成本随区间漂移，拼接/断点续抓就会把两种口径混在一张表里（实测月毛利差十几万）。
COSTS_CACHE_DIR = Path(_env("JD_COSTS_CACHE_DIR", str(DATA_DIR / "costs")) or ".")
# ---- 登录态保活 ----
# 京准通的会话凭据 sdtoken 只有约 30 分钟有效期，无人操作的后台进程必须定时「用一下」
# 才能续期（京麦客户端就是这么做的）。这里让 Web 服务常驻时按固定间隔访问京准通首页，
# 由服务端下发新的 sdtoken，从而避免「过一天就要重新登录」。
KEEPALIVE_ENABLED = _env_bool("JD_KEEPALIVE", True)
KEEPALIVE_INTERVAL = _env_int("JD_KEEPALIVE_INTERVAL", 20 * 60)   # 秒；默认 20 分钟（< 30 分钟账期）
KEEPALIVE_JITTER = _env_int("JD_KEEPALIVE_JITTER", 120)           # 每次随机抖动，避免固定节奏被风控

# AI 解读层：夜间抓取完成后自动生成「最近 N 天」的 AI 运营解读，报表页展示。
AI_ENABLED = _env_bool("JD_AI_ENABLED", True)
AI_RANGE_DAYS = _env_int("JD_AI_RANGE_DAYS", 7)                   # 自动生成的区间天数
# 保活时依次访问的页面（逗号分隔）。模仿京麦：打开真实业务页 → 页面 JS 会自发
# 三层心跳（sso/rac 续期、bypass 风控、sgm 埋点），比只访问首页更接近真人使用。
# 顺序访问 + 每页停留数秒，让前端脚本有时间把心跳发出去。
KEEPALIVE_PAGES = [
    p.strip() for p in (_env("JD_KEEPALIVE_PAGES", "") or "").split(",") if p.strip()
]
KEEPALIVE_DWELL_MS = _env_int("JD_KEEPALIVE_DWELL", 8000)         # 每个页停留毫秒（等 JS 发心跳）
# 是否在保活时顺带把 Profile 导出成 storage_state 快照（备份；默认不写，省 IO）
KEEPALIVE_EXPORT_STATE = _env_bool("JD_KEEPALIVE_EXPORT_STATE", False)

# 1 = 按所选区间向接口要均值（会带 date_from/date_to）；0 = 用供货方默认（近 30 天）
COSTS_FOLLOW_WINDOW = _env_bool("JD_COSTS_FOLLOW_WINDOW", True)
# 本次报表实际用的成本口径来源，供报表展示（不入库）
COSTS_SOURCE: dict = {"kind": "全店口径", "detail": "未配置成本来源"}
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
# 抓取的「基准时间」（本地时区）。默认凌晨 1:00 开始（等商智前一日数据结算完）。
SCHEDULE_HOUR = _env_int("JD_SCHEDULE_HOUR", 1)
SCHEDULE_MINUTE = _env_int("JD_SCHEDULE_MINUTE", 0)
# 在基准时间上随机浮动 ±N 分钟（默认 30）。避免每天精确同一秒抓取被风控识别为机器行为。
SCHEDULE_JITTER_MIN = _env_int("JD_SCHEDULE_JITTER", 30)
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

# ---------------- 分时（趋势）数据 ----------------
# 京准通「推广计划」列表里的「趋势」= 分时曲线（granularity=4，only 单日有效）。
# 只在**夜间抓取时**跑一次、**只抓昨天那一天**，避免频繁调用被风控。
HOURLY_ENABLED = _env_bool("JD_HOURLY", True)
# 计划级只取当天花费前 N 个计划（账号级永远是 1 次请求）。N=0 表示只抓账号级。
HOURLY_PLANS = _env_int("JD_HOURLY_PLANS", 20)
# 每晚抓取「最近几天」的分时（默认 1 = 只抓昨天）。调大可以覆盖更早的天，
# 但请求数会按天数翻倍（每天 1 + HOURLY_PLANS 次）。
HOURLY_DAYS = _env_int("JD_HOURLY_DAYS", 1)
# 每个分时请求之间的间隔，别打太密
HOURLY_SLEEP = float(_env("JD_HOURLY_SLEEP", "0.4") or 0.4)

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


# ---------------- 账号管理（网页「登录」页可以增删改名） ----------------
# 只把 key/label/account_id 写回 accounts.json；user_data_dir 用默认值时省略。
def _accounts_payload(accs: list) -> list:
    out = []
    for a in accs:
        key = str(a.get("key") or "").strip()
        if not key:
            continue
        item = {"key": key,
                "label": (a.get("label") or key),
                "account_id": int(a.get("account_id") or 0)}
        custom_dir = a.get("user_data_dir")
        if custom_dir and str(custom_dir) != default_profile_dir(key):
            item["user_data_dir"] = str(custom_dir)
        out.append(item)
    return out


def save_accounts(accs: list | None = None) -> None:
    """写回 accounts.json（先写临时文件再替换，避免半个 JSON）。"""
    payload = _accounts_payload(list(ACCOUNTS if accs is None else accs))
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_name(ACCOUNTS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACCOUNTS_FILE)


def apply_accounts(accs: list) -> list:
    """把账号列表应用到全局。

    必须**原地**改 ACCOUNTS（config.ACCOUNTS 等模块持有的是同一个 list 引用），
    否则新增/删除账号后其他模块看到的还是旧列表。
    """
    global MAIN_ACCOUNT
    norm = _normalize(list(accs))
    ACCOUNTS.clear()
    ACCOUNTS.extend(norm)
    MAIN_ACCOUNT = ACCOUNTS[0]["key"] if ACCOUNTS else "main"
    # config.py 里有一份 import 时拷贝的值，CLI 脚本会用到，这里同步一下
    try:
        from . import config as _config
        _config.MAIN_ACCOUNT = MAIN_ACCOUNT
        _config.ACCOUNT_ID = (ACCOUNTS[0].get("account_id") if ACCOUNTS else 0) or 0
    except Exception:  # noqa: BLE001
        pass
    return ACCOUNTS


def _next_key() -> str:
    used = {a["key"] for a in ACCOUNTS}
    i = 1
    while f"acct{i}" in used:
        i += 1
    return f"acct{i}"


def add_account(label: str, account_id: int = 0, key: str | None = None) -> dict:
    """新增账号（还没登录，登录后 profile 落到 AUTH_DIR/<key>）。"""
    key = (key or "").strip() or _next_key()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", key):
        raise ValueError("账号 key 只能用字母/数字/下划线/短横线")
    if any(a["key"] == key for a in ACCOUNTS):
        raise ValueError(f"账号 key 已存在：{key}")
    label = (label or "").strip() or key
    accs = _accounts_payload(ACCOUNTS) + [
        {"key": key, "label": label, "account_id": int(account_id or 0)}]
    save_accounts(accs)
    apply_accounts(accs)
    return get_account(key)


def rename_account(key: str, label: str) -> dict:
    label = (label or "").strip()
    if not label:
        raise ValueError("名称不能为空")
    accs = _accounts_payload(ACCOUNTS)
    hit = next((a for a in accs if a["key"] == key), None)
    if hit is None:
        raise ValueError("账号不存在")
    hit["label"] = label
    save_accounts(accs)
    apply_accounts(accs)
    return get_account(key)


def delete_account(key: str) -> dict:
    if len(ACCOUNTS) <= 1:
        raise ValueError("至少要保留一个账号")
    key = str(key or "")
    accs = [a for a in _accounts_payload(ACCOUNTS) if a["key"] != key]
    if len(accs) == len(ACCOUNTS):
        raise ValueError("账号不存在")
    save_accounts(accs)
    apply_accounts(accs)
    return {"ok": True, "key": key}


def set_account_id(key: str, account_id: int) -> bool:
    """登录成功后回填「京准通账户ID」（花费/关键词等接口要用它做 pinIds）。"""
    account_id = int(account_id or 0)
    if account_id <= 0:
        return False
    acc = next((a for a in ACCOUNTS if a["key"] == key), None)
    if acc is None or int(acc.get("account_id") or 0) == account_id:
        return False
    accs = _accounts_payload(ACCOUNTS)
    for a in accs:
        if a["key"] == key:
            a["account_id"] = account_id
    save_accounts(accs)
    apply_accounts(accs)
    return True


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


def _costs_key(date_from: str, date_to: str) -> str:
    """区间 → 缓存文件名主体。没给区间就是接口默认窗口（近 30 天），单独一份。"""
    if not date_from and not date_to:
        return "default"
    return f"{date_from or 'any'}__{date_to or 'any'}"


def _costs_range_file(date_from: str, date_to: str) -> Path:
    return COSTS_CACHE_DIR / f"costs_{_costs_key(date_from, date_to)}.json"


def _costs_cache_read(date_from: str = "", date_to: str = "") -> dict:
    """读成本缓存：给了区间就读**该区间**那份，不给就读「最近一次拉到的」。

    区间文件还没有、而老的全局文件正好是同一区间（升级到按区间存放之前的缓存）时，
    把它搬过去，免得升级后白跑一次接口。
    """
    if not date_from and not date_to:
        try:
            return json.loads(COSTS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    try:
        return json.loads(_costs_range_file(date_from, date_to).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    legacy = _costs_cache_read()
    q = (legacy or {}).get("_query") or {}
    if (legacy or {}).get("skus") and q.get("date_from", "") == date_from \
            and q.get("date_to", "") == date_to:
        _costs_cache_write(legacy, date_from, date_to)
        return legacy
    return {}


def costs_cached(date_from: str = "", date_to: str = "") -> dict | None:
    """本区间的成本表在不在本地。**只读文件**：不发请求、不起线程，给 HTTP 请求线程用。"""
    data = _costs_cache_read(date_from, date_to)
    return data if data.get("skus") else None


def _costs_cache_write(data: dict, date_from: str = "", date_to: str = "") -> None:
    """区间文件 + 「最近一次」两份都写（后者给状态页和成本护栏对比用）。"""
    targets = [COSTS_CACHE_FILE]
    if date_from or date_to:
        targets.insert(0, _costs_range_file(date_from, date_to))
    for p in targets:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass


def _guard_costs(data: dict, prev: dict) -> dict:
    """拦掉「单件货款成本突然暴涨」的 SKU：沿用上一版，避免上游一天脏数据毁掉整月利润。

    判据（三条同时满足才拦）：新值 > 旧值 × 3、新值 > 到手结算价 × 1.5、旧值本身有效。
    实测 2026-10-26 供货方接口把两个西兰苔 SKU 的 _goodsCost 从 ~16 元改成 829 元
    （整月均值被拉到 116.8），利润表因此算出 -101 元/件的假亏损。
    命中的 SKU 会留在 data["_guard"] 里，报表备注会列出来。
    """
    new_skus = data.get("skus") or {}
    prev_skus = (prev or {}).get("skus") or {}
    if not new_skus or not prev_skus:
        return data
    warn: list = []
    for sid, rec in list(new_skus.items()):
        old = prev_skus.get(sid) or {}
        try:
            n = float(rec.get("_goodsCost") or 0)
            o = float(old.get("_goodsCost") or 0)
            sup = float(rec.get("supply") or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0 or o <= 0 or sup <= 0:
            continue
        if n > o * 3 and n > sup * 1.5:
            new_skus[sid] = dict(rec, _goodsCost=o, _guardedGoodsCost=n)
            warn.append({"skuId": sid, "name": rec.get("_name") or "",
                         "used": round(o, 2), "rejected": round(n, 2)})
    if warn:
        data["_guard"] = warn
    return data


def _costs_fetch(date_from: str = "", date_to: str = "") -> dict:
    """拉供货方 ERP 的 SKU 成本接口；任何异常都抛给调用方处理。"""
    import time
    import urllib.parse
    import urllib.request

    headers = {"Accept": "application/json", "User-Agent": "jd-roi/1.0"}
    if COSTS_TOKEN:
        headers["X-Api-Token"] = COSTS_TOKEN

    def _get(query: dict) -> dict:
        url = COSTS_URL
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=COSTS_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    qs = {}
    if date_from:
        qs["date_from"] = date_from
    if date_to:
        qs["date_to"] = date_to
    unranged = False
    try:
        data = _get(qs)
    except Exception:  # noqa: BLE001
        # 供货方接口对「同时带 date_from + date_to」的请求会 500（2026-10-25 实测），
        # 退一步不带区间再取一次（该接口本来就返回整张成本表）
        if not qs:
            raise
        data = _get({})
        unranged = True
    if not isinstance(data, dict) or not isinstance(data.get("skus"), dict):
        raise ValueError(f"成本接口返回结构异常：{str(data)[:160]}")
    # 护栏要和「同一区间的上一版」比：不同区间的均价本来就不同，跨区间比会误报
    data = _guard_costs(data, _costs_cache_read(date_from, date_to) or _costs_cache_read())
    data["_fetched_at"] = time.time()
    # _query 必须如实记录「这份表到底是按什么区间算的」：退步取整表时区间没生效，
    # 就不能冒充是本区间的表 —— 报表要靠它判断成本口径是否对得上。
    data["_query"] = ({"date_from": "", "date_to": ""} if unranged
                      else {"date_from": date_from, "date_to": date_to})
    if unranged:
        data["_unranged"] = True
    return data


def costs_unit_map() -> dict:
    """手工换算表 config/costs_units.json：{"<skuId>": 每 1 个京东售卖件 = 几个 ERP 计量单位}。

    默认全部按 1:1，因为实测只有 1:1 成立：按 ERP出库件数÷商智京东件数（1.5~4.2 倍）换算后，
    到手结算价会**高于**消费者实付价，显然不对 —— 那个比值差的是 ERP 里混入了其他渠道销量。
    供货方若确认某个 SKU 真是「1 件 = N 袋」，在这里（或接口的 unitsPerSale）填 N 即可。
    """
    p = CONFIG_DIR / "costs_units.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for k, v in (raw or {}).items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            pass
    return out


_costs_lock = threading.Lock()
_costs_pending: set = set()


def _costs_refresh_async(date_from: str, date_to: str) -> None:
    """后台按新区间重拉成本表，不阻塞当前这一次请求。同一个区间只排一次。"""
    key = (date_from, date_to)
    with _costs_lock:
        if key in _costs_pending:
            return
        _costs_pending.add(key)

    def run() -> None:
        t0 = time.time()
        try:
            data = _costs_fetch(date_from, date_to)
            _costs_cache_write(data, date_from, date_to)
            print(f"[成本表] 后台刷新完成 {date_from} ~ {date_to}："
                  f"{len(data.get('skus') or {})} 个 SKU，耗时 {time.time() - t0:.1f}s", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[成本表] 后台刷新失败 {date_from} ~ {date_to}（保留旧表）："
                  f"{type(e).__name__}: {e}", flush=True)
        finally:
            with _costs_lock:
                _costs_pending.discard(key)

    threading.Thread(target=run, daemon=True, name="costs-refresh").start()


def load_costs(date_from: str = "", date_to: str = "", force: bool = False) -> dict:
    """每个 SKU 的真实成本：远端接口优先 → 本地缓存 → config/costs.json → 空（全店口径）。

    返回结构：{"default": {...}, "skus": {"<skuId>": {"supply":…, "_goodsCost":…}},
    外加 "_source"/"updatedAt" 等说明字段，供报表标注成本口径来源。
    接口口径：**到手结算价 supply = 京东结算给我们的每件金额（收入，已扣点）**，
    _goodsCost 才是我方货款成本 —— 见《SKU成本接口对接说明.md》第 2 节。

    性能约定：接口按区间算均值，实测 1 天 ~0.1s、10 天 ~1s、整月 ~4-7s。
    报表页开一次就同步等接口会白屏好几秒，所以除了「本地无表」和 force（用户点
    「只更新 ERP 成本」）之外，一律**先用本地已有的表秒出**，新区间丢给后台线程刷新，
    下一次请求自然就用上新数据了。

    区间隔离：每个区间一份缓存（见 COSTS_CACHE_DIR），本区间没拉过时宁可明说
    「借用了另一个区间的均价」，也不再悄悄覆盖别人的表 —— 否则拼接/断点续抓出来的
    毛利会是两种区间均价的混合值，看着像数据，其实对不上任何一天。
    """
    global COSTS_SOURCE
    if COSTS_URL:
        rng = f"{date_from or '—'} ~ {date_to or '—'}"
        own = _costs_cache_read(date_from, date_to)      # 本区间的表
        own_has = bool(own.get("skus"))
        own_age = time_ago(own.get("_fetched_at"))
        latest = _costs_cache_read()                     # 最近一次拉到的（可能是别的区间）
        latest_has = bool(latest.get("skus"))
        failed = None
        if force or not (own_has or latest_has):
            # 用户显式要求刷新，或本地一份都没有：只能同步等接口
            try:
                t0 = time.time()
                fresh = _costs_fetch(date_from, date_to)
                _costs_cache_write(fresh, date_from, date_to)
                fq = (fresh.get("_query") or {})
                COSTS_SOURCE = {"kind": "供货方接口",
                                "detail": f"{COSTS_URL}｜请求 {rng}｜实取 "
                                          f"{fq.get('date_from') or '默认窗口'} ~ "
                                          f"{fq.get('date_to') or '默认窗口'}｜"
                                          f"updatedAt {fresh.get('updatedAt', '—')}"
                                          f"｜耗时 {time.time() - t0:.1f}s"}
                return fresh
            except Exception as e:  # noqa: BLE001  断网/Token 失效/5xx 都走缓存，不阻断报表
                print(f"[成本表] 接口拉取失败，改用本地缓存/costs.json：{type(e).__name__}: {e}")
                failed = e
        if own_has:
            if failed:
                COSTS_SOURCE = {"kind": "本地缓存",
                                "detail": f"接口失败（{type(failed).__name__}: {failed}），"
                                          f"沿用 {rng} 的成本表（{int(own_age / 60)} 分钟前）"}
            elif own_age < COSTS_TTL:
                COSTS_SOURCE = {"kind": "本地缓存",
                                "detail": f"{rng} 的成本表，{int(own_age / 60)} 分钟前拉取"}
            else:
                COSTS_SOURCE = {"kind": "本地缓存",
                                "detail": f"{rng} 的成本表已 {int(own_age / 3600)} 小时，"
                                          f"先用旧值出报表，已在后台按本区间刷新"}
                _costs_refresh_async(date_from, date_to)
            return own
        if latest_has:
            # 本区间还没拉过：先借最近一版立即出报表（不白屏），并在后台补本区间的表。
            # 「借」必须写在明面上，报表与成本面板都能看到这个口径差。
            lq = latest.get("_query") or {}
            l_age = time_ago(latest.get("_fetched_at"))
            if failed:
                COSTS_SOURCE = {"kind": "本地缓存",
                                "detail": f"接口失败（{type(failed).__name__}: {failed}），"
                                          f"借用 {lq.get('date_from') or '—'} ~ "
                                          f"{lq.get('date_to') or '—'} 的成本表"
                                          f"（{int(l_age / 60)} 分钟前）"}
            else:
                COSTS_SOURCE = {"kind": "本地缓存（借用区间）",
                                "detail": f"{rng} 的成本表本地还没拉过，先用 "
                                          f"{lq.get('date_from') or '—'} ~ "
                                          f"{lq.get('date_to') or '—'} 的均价"
                                          f"（{int(l_age / 60)} 分钟前）立即出报表，"
                                          f"已在后台按 {rng} 拉取，拉完再查即以本区间为准"}
                _costs_refresh_async(date_from, date_to)
            return latest
    if COSTS_FILE.exists():
        try:
            data = json.loads(COSTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("skus"):
                COSTS_SOURCE = {"kind": "config/costs.json", "detail": str(COSTS_FILE)}
                return data
        except Exception as e:  # noqa: BLE001
            COSTS_SOURCE = {"kind": "全店口径", "detail": f"costs.json 解析失败：{e}"}
    return {}


def time_ago(ts) -> float:
    """时间戳距今秒数；无效值返回无穷大（视为过期）。"""
    import time
    try:
        return max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return float("inf")


# ---------------- 统一时间源（必须走 TZ，绝不能用裸 datetime.now()） ----------------
# 血泪教训：Windows 上 Python 的 datetime.now() 由「进程本地时区」决定，而进程时区
# 可能被环境变量 TZ（如 UTC）污染；Windows CRT 又未必认 TZ=Asia/Shanghai 这种 IANA 名。
# 结果：容器/后台进程跑到 UTC 时区 → today() 比真实日期少一天 → 每晚抓的是「前天」。
# 因此所有「业务日期」计算一律走本模块的 now()/today()，显式用 ZoneInfo(TZ)。
def _tzinfo():
    """解析 settings.TZ 得到 tzinfo；失败退回固定 +08:00。"""
    from datetime import timezone, timedelta
    name = TZ or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        # 常见别名兜底，最后退回北京时间
        if name in ("Asia/Shanghai", "PRC", "CTT", "Asia/Chongqing", "Asia/Chungking"):
            return timezone(timedelta(hours=8))
        return timezone(timedelta(hours=8))


_TZINFO = None


def now():
    """带目标时区的当前时间（aware）。业务上一切「现在」都用它。"""
    global _TZINFO
    from datetime import datetime
    if _TZINFO is None:
        _TZINFO = _tzinfo()
    return datetime.now(_TZINFO)


def today():
    """目标时区下的当前日期（date）。"""
    return now().date()


def now_iso(timespec: str = "seconds") -> str:
    """目标时区当前时间 ISO 字符串（不带 offset，保持与既有落盘格式兼容）。"""
    return now().replace(tzinfo=None).isoformat(timespec=timespec)


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return now().strftime(fmt)
