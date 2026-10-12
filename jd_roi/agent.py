# -*- coding: utf-8 -*-
"""内部数据 Agent：让 AI 能**自己规划步骤、调用内部接口、写并运行脚本**。

定位：这是项目**内部**的自动化助手（本机、单用户），不是对外服务。
用户提出「要哪几天的数据和分析」，Agent 自己决定：
  - 先看本地有哪些数据（`coverage`）；
  - 缺就触发抓取（`fetch_data`）；
  - 拿到区间后取精炼指标（`analyze`）或原始切片（`raw`）；
  - 需要算/画/导出就**写一个新脚本再运行**（`write_file` + `run_python`）；
  - 也可以直接打本机内部 HTTP 接口（`http_api`）。

实现方式是 **ReAct 式提示词工具调用**（不依赖端点是否支持 function calling）：
模型每一轮只输出一个 JSON：
    {"thought": "...", "tool": "<工具名>", "args": {...}}      # 继续干活
    {"thought": "...", "final": "markdown 答案"}               # 收尾
我们执行工具、把结果作为观察喂回去，循环到 `final` 或步数上限。

所有事件（思考/工具/观察/阶段/正文）都写进 `aijobs.Job`，
沿用既有 SSE 流：**页面离开不中断、回来可回放续看**。

安全边界（本机内部工具，仍做最小防护）：
  - 文件读写一律限制在项目目录内；**写文件只允许** `scripts/agent/`、`data/agent/`；
  - `run_python` 用项目自带解释器、有超时与输出上限；
  - `http_api` 只允许打 127.0.0.1:8011 本服务的接口。
"""
from __future__ import annotations

import glob as _glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import analyze2, analyze_ai, dailystore as ds, llm, scrape_day, settings

BASE_DIR = Path(__file__).resolve().parent.parent
AGENT_SCRIPT_DIR = BASE_DIR / "scripts" / "agent"
AGENT_DATA_DIR = BASE_DIR / "data" / "agent"
INTERNAL_BASE = "http://127.0.0.1:8011"

MAX_STEPS = 9                 # 一次任务最多几步（慢模型，别太贪）
OBS_LIMIT = 6000              # 单条观察喂回模型的最大字符
STDOUT_LIMIT = 8000           # 脚本输出回传上限
RUN_TIMEOUT = 180             # 脚本执行超时（秒）
PLAN_MAX_TOKENS = 4000


# ---------------------------------------------------------------- 小工具
def _accounts(spec) -> list[dict]:
    """把 "main" / "main,b" / None 归一成账号字典列表。"""
    if not spec or str(spec).lower() in ("all", "*", "全部"):
        return list(settings.ACCOUNTS)
    if isinstance(spec, (list, tuple)):
        keys = {str(x).strip() for x in spec}
    else:
        keys = {x.strip() for x in str(spec).split(",") if x.strip()}
    got = [a for a in settings.ACCOUNTS if a["key"] in keys]
    return got or list(settings.ACCOUNTS)


def _acc_label(accs) -> str:
    return ",".join(a["key"] for a in accs)


def _clip(s, n: int = OBS_LIMIT) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"\n…（已截断，共 {len(s)} 字）"


def _safe_path(p: str, *, for_write: bool = False) -> Path:
    """把路径限制在项目目录内；写文件只允许 sandbox 目录。"""
    raw = Path(str(p))
    full = (raw if raw.is_absolute() else BASE_DIR / raw).resolve()
    if BASE_DIR not in full.parents and full != BASE_DIR:
        raise ValueError(f"路径越界：只允许项目目录内（{BASE_DIR}）")
    if for_write:
        allowed = (AGENT_SCRIPT_DIR.resolve(), AGENT_DATA_DIR.resolve(), (BASE_DIR / "data").resolve())
        if not any(full == d or d in full.parents for d in allowed):
            raise ValueError("只能写入 scripts/agent/ 或 data/ 目录下")
    return full


def _newest_day() -> str | None:
    try:
        return scrape_day.newest_day()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 工具实现
def t_coverage(args: dict) -> str:
    """本地已抓取哪些天 / 哪些类型。"""
    accs = _accounts(args.get("account"))
    out = {}
    for a in accs:
        per = {}
        for kind in ("jzt_campaign", "jst_campaign", "jst_sku", "jst_searchword",
                     "kw", "sz_product", "sz_flow", "oplog", "crowd", "area"):
            days = ds.days_for(a["key"], kind)
            if days:
                per[kind] = {"days": len(days), "first": min(days), "last": max(days)}
        out[a["key"]] = per
    return _clip({"accounts": out, "newest_day": _newest_day(),
                  "today": settings.today().isoformat(),
                  "ai_default_range": list(analyze_ai.local_range(accs) or [])})


def t_fetch_data(args: dict) -> str:
    """触发内部抓取（后台跑，立即返回）。"""
    accs = _accounts(args.get("account"))
    start, end = args.get("start"), args.get("end")
    if not (start and end):
        return "需要 start 与 end（YYYY-MM-DD）"
    force = bool(args.get("force"))
    res = scrape_day.run_background(accs, str(start), str(end), force=force,
                                    log=lambda m: None, name="agent-scrape")
    return _clip({"accounts": _acc_label(accs), "start": start, "end": end,
                  "force": force, "result": res,
                  "hint": "抓取在后台进行，用 scrape_status 查进度；登录态失效会失败。"})


def t_scrape_status(args: dict) -> str:
    return _clip({"runtime": scrape_day.runtime_status(),
                  "catchup": list(scrape_day.catchup_range() or [])})


def t_analyze(args: dict) -> str:
    """区间聚合后的**精炼指标摘要**（判断用，体积可控）。"""
    start, end = args.get("start"), args.get("end")
    if not (start and end):
        rng = analyze_ai.local_range(_accounts(args.get("account")))
        if not rng:
            return "本地没有已抓取数据，且未给 start/end"
        start, end = rng
    accs = _accounts(args.get("account"))
    data = analyze2.build(start=str(start), end=str(end), accounts=accs)
    digest = analyze_ai.build_digest(data, args.get("one_account"), max_skus=15,
                                     words_per_kind=6, max_ops=8)
    return _clip(digest)


def t_raw(args: dict) -> str:
    """原始分析结构的某个切片（比 analyze 更细，自己控制取哪块）。"""
    start, end = args.get("start"), args.get("end")
    if not (start and end):
        return "需要 start 与 end（YYYY-MM-DD）"
    accs = _accounts(args.get("account"))
    data = analyze2.build(start=str(start), end=str(end), accounts=accs)
    section = str(args.get("section") or "meta")
    limit = int(args.get("limit") or 40)
    if section == "meta":
        val = data.get("meta")
    elif section == "totals":
        val = [{"key": a.get("key"), "totals": a.get("totals")}
               for a in (data.get("accounts") or [])]
    elif section in ("plans", "skus", "adjustments"):
        val = (data.get(section) or [])[:limit]
    elif section == "words":
        val = data.get("words")
    elif section == "typeSplit":
        val = data.get("typeSplit")
    elif section == "targeting":
        val = data.get("targeting")
    elif section == "targetingGaps":
        val = data.get("targetingGaps")
    elif section == "advice":
        val = data.get("advice")
    else:
        return (f"未知 section={section}；可用：meta/totals/plans/skus/adjustments/"
                "words/typeSplit/targeting/targetingGaps/advice")
    return _clip(val)


def t_get_report(args: dict) -> str:
    """读取本地缓存的整份 AI 报告要点（不重新生成）。"""
    out = analyze_ai.load_latest()
    if not out:
        return "本地还没有 AI 报告缓存，可用 start_report 触发一次生成。"
    a = out.get("advice") or {}
    return _clip({"range": out.get("range"), "generatedAt": out.get("generatedAt"),
                  "model": out.get("model"), "validation": out.get("validation"),
                  "headline": a.get("headline"), "health": a.get("health"),
                  "advice_keys": list(a.keys())})


def t_start_report(args: dict) -> str:
    """触发一次整份 AI 报告生成（后台，几分钟）。"""
    start, end = args.get("start"), args.get("end")
    accs = _accounts(args.get("account"))
    if not (start and end):
        rng = analyze_ai.local_range(accs)
        if not rng:
            return "本地没有已抓取数据，且未给 start/end"
        start, end = rng
    from . import webapp  # 延迟导入，避免循环依赖
    return _clip(webapp._ai.run_background(str(start), str(end), accs))


def t_list_files(args: dict) -> str:
    pat = str(args.get("glob") or "**/*.py")
    import fnmatch
    hits = []
    for p in sorted(BASE_DIR.glob(pat)):
        if p.is_file() and ".venv" not in p.parts:
            hits.append(str(p.relative_to(BASE_DIR)).replace("\\", "/"))
        if len(hits) >= 200:
            break
    return _clip({"pattern": pat, "count": len(hits), "files": hits})


def t_read_file(args: dict) -> str:
    p = _safe_path(args.get("path"))
    if not p.exists():
        return f"文件不存在：{p.relative_to(BASE_DIR)}"
    if p.stat().st_size > 2_000_000:
        return "文件过大（>2MB），请换 read_file 的目标或改用脚本处理。"
    text = p.read_text(encoding="utf-8", errors="replace")
    n = int(args.get("max_chars") or 20000)
    return _clip(f"# {p.relative_to(BASE_DIR)}\n{text}", n + 200)


def t_write_file(args: dict) -> str:
    raw = str(args.get("path") or "")
    if not raw:
        return "需要 path"
    # 允许只给文件名 → 落到 scripts/agent/
    if "/" not in raw and "\\" not in raw:
        raw = f"scripts/agent/{raw}"
    p = _safe_path(raw, for_write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(args.get("content") or ""), encoding="utf-8")
    rel = str(p.relative_to(BASE_DIR)).replace("\\", "/")
    return f"已写入 {rel}（{len(str(args.get('content') or ''))} 字）。可用 run_python 运行。"


def t_run_python(args: dict) -> str:
    """在项目环境里运行一个脚本（或一段代码）。"""
    path = args.get("path")
    code = args.get("code")
    if not path and not code:
        return "需要 path 或 code"
    if code and not path:
        AGENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = str(AGENT_DATA_DIR / f"inline_{int(time.time())}.py")
        Path(path).write_text(str(code), encoding="utf-8")
    p = _safe_path(path)
    if not p.exists():
        return f"脚本不存在：{p.relative_to(BASE_DIR)}（先用 write_file 写它）"
    env = dict(os.environ)
    env.setdefault("TZ", settings.TZ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 关键：把项目根加进 PYTHONPATH —— 否则脚本 `from jd_roi import ...` 会 ImportError
    # （python 只把脚本自身目录放进 sys.path[0]，而脚本在 scripts/agent/ 下）。
    prev = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = str(BASE_DIR) + (os.pathsep + prev if prev else "")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, str(p)], cwd=str(BASE_DIR), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=RUN_TIMEOUT)
        ok, rc = True, r.returncode
        out, err = r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        ok, rc, out, err = False, None, "", f"超时（>{RUN_TIMEOUT}s）被终止"
    body = f"$ {p.relative_to(BASE_DIR)}\nexit={rc} ({time.time()-t0:.1f}s)\n"
    if out.strip():
        body += "\n[stdout]\n" + out
    if err.strip():
        body += "\n[stderr]\n" + err
    if not out.strip() and not err.strip():
        body += "（无输出）"
    return _clip(body, STDOUT_LIMIT)


def t_http_api(args: dict) -> str:
    """调用本机 8011 自己的内部接口。"""
    method = str(args.get("method") or "GET").upper()
    path = str(args.get("path") or "")
    if not path.startswith("/"):
        return "path 必须以 / 开头，例如 /api/status"
    url = INTERNAL_BASE + path
    try:
        with httpx.Client(timeout=120.0) as c:
            if method == "GET":
                r = c.get(url, params=args.get("params") or None)
            else:
                r = c.request(method, url, params=args.get("params") or None,
                              json=args.get("body") or None)
        return _clip(f"HTTP {r.status_code}\n{r.text}", 5000)
    except Exception as exc:  # noqa: BLE001
        return f"调用失败：{type(exc).__name__}: {exc}"


TOOLS = {
    "coverage": {"fn": t_coverage, "desc": "查看本地已抓取的数据覆盖（各账号各类型的天数/首末日期）与库内最新一天",
                 "args": {"account": "可选，'main'|'b'|'main,b'，缺省=全部"}},
    "fetch_data": {"fn": t_fetch_data, "desc": "触发内部抓取指定区间的数据（后台跑，立即返回；缺数据时用）",
                   "args": {"account": "可选", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "force": "可选 true 强制重抓"}},
    "scrape_status": {"fn": t_scrape_status, "desc": "查看当前抓取进度/下一次补跑区间", "args": {}},
    "analyze": {"fn": t_analyze, "desc": "取某区间的【精炼指标摘要】（保本线/放量线/账号合计/计划三窗口/类型拆解/词分层/SKU/定向缺口），分析判断首选",
                "args": {"start": "YYYY-MM-DD 可选", "end": "YYYY-MM-DD 可选", "account": "可选", "one_account": "可选，只看某账号"}},
    "raw": {"fn": t_raw, "desc": "取原始分析结构的一个切片，比 analyze 更细",
            "args": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "account": "可选",
                     "section": "meta|totals|plans|skus|adjustments|words|typeSplit|targeting|targetingGaps|advice",
                     "limit": "可选，返回条数上限"}},
    "get_report": {"fn": t_get_report, "desc": "读取本地缓存的整份 AI 报告要点（不重新生成）", "args": {}},
    "start_report": {"fn": t_start_report, "desc": "触发一次整份 AI 报告生成（后台，需数分钟）",
                     "args": {"start": "可选", "end": "可选", "account": "可选"}},
    "list_files": {"fn": t_list_files, "desc": "列出项目内文件（glob）", "args": {"glob": "如 jd_roi/*.py"}},
    "read_file": {"fn": t_read_file, "desc": "读取项目内文本文件", "args": {"path": "相对项目根的路径", "max_chars": "可选"}},
    "write_file": {"fn": t_write_file, "desc": "写一个新文件（沙箱：scripts/agent/ 或 data/ 下；只给文件名则落到 scripts/agent/）",
                   "args": {"path": "如 analyze_x.py 或 scripts/agent/x.py", "content": "文件内容"}},
    "run_python": {"fn": t_run_python, "desc": "用项目解释器运行脚本或一段代码，返回 stdout/stderr（超时 180s）",
                   "args": {"path": "可选，脚本路径", "code": "可选，直接给代码"}},
    "http_api": {"fn": t_http_api, "desc": "调用本机 8011 的内部接口（GET/POST 等）",
                 "args": {"method": "GET/POST", "path": "以 / 开头", "params": "可选", "body": "可选"}},
}


def tools_doc() -> str:
    lines = []
    for name, spec in TOOLS.items():
        args = spec.get("args") or {}
        arg_s = "、".join(f"{k}({v})" for k, v in args.items()) if args else "无参数"
        lines.append(f"- `{name}`：{spec['desc']}\n    参数：{arg_s}")
    return "\n".join(lines)


SYSTEM_AGENT = """你是这家京东店铺的**内部数据 Agent**，可以直接操作本项目的内部接口、文件与脚本。
用户会提出「要哪几天的数据 / 分析 / 导出 / 画图」这类任务，你要**自己规划并一步步执行**，最后给结论。

【每一轮只输出一个 JSON 对象，不要输出别的文字】
继续干活：
{"thought": "一句话说明这一步要干嘛", "tool": "工具名", "args": {...}}
任务完成：
{"thought": "一句话", "final": "给用户的最终 markdown 答案"}

【可用工具】
%s

【铁律】
- 只依据工具返回的真实数据下结论；**绝不编造数字**。工具没给的数据，要么再取一次，要么说明拿不到。
- 判断要**套用下面的方法论**（讲原理，不只贴名词），并引用具体计划名/词/地域。
- 缺数据就先 `coverage` 看有没有，再 `fetch_data` 抓；抓取是后台任务，别死等，可先分析已有区间。
- 要算新指标 / 导出 CSV / 画图，就 `write_file` 写个脚本再用 `run_python` 跑（脚本放 scripts/agent/）。
  脚本里可以直接 `from jd_roi import analyze2, dailystore, settings` 复用本项目能力。
- `thought` 必须一句话，别啰嗦（模型慢，省 token）。
- **效率**：`run_python` 报了错最多修一次；一旦工具结果够回答问题，**立刻用 `final` 收尾**，
  别再为了"多查一点"继续调工具 —— 步数很宝贵。
- 最多 %d 步；信息够了就尽快给 `final`。
- `final` 用简体中文 markdown，**结论 + 依据（带数字）+ 可执行动作**，一般 400~900 字。

【方法论（判断依据）】
%%s
""" % (tools_doc(), MAX_STEPS)


# ---------------------------------------------------------------- 执行环
def _memo_analyze():
    """同一区间/账号只算一次 analysis2（agent 一轮内复用）。"""
    cache: dict = {}

    def get(start, end, accs):
        key = (start, end, _acc_label(accs))
        if key not in cache:
            cache[key] = analyze2.build(start=start, end=end, accounts=accs)
        return cache[key]
    return get


def run(job, task: str, *, accounts=None, start=None, end=None,
        model: str | None = None) -> None:
    """把用户任务跑成一次 Agent 会话，事件全部写进 job。"""
    methodology, _src = analyze_ai.load_methodology()
    rng = analyze_ai.local_range(_accounts(accounts))
    ctx = {
        "today": settings.today().isoformat(),
        "accounts": [a["key"] for a in settings.ACCOUNTS],
        "local_days_latest": _newest_day(),
        "ai_default_range": list(rng or []),
        "ui_selected_range": [start, end] if start and end else None,
        "sandbox": {"scripts": "scripts/agent/", "data": "data/agent/"},
    }
    msgs = [
        {"role": "system", "content": SYSTEM_AGENT % methodology},
        {"role": "user", "content": f"【任务】{task}\n\n【运行环境】"
                                    f"{json.dumps(ctx, ensure_ascii=False)}"},
    ]
    job.emit("phase", "Agent 已启动，正在规划…")

    step = 0
    parse_fails = 0
    while step < MAX_STEPS:
        step += 1
        buf: list[str] = []

        def _cb(kind, text, _buf=buf):
            if kind == "reset":
                _buf.clear()
                job.emit("reset", "")
            elif kind == "reasoning":
                job.emit("reasoning", text)
            elif kind == "content":
                _buf.append(text)

        job.emit("phase", f"第 {step}/{MAX_STEPS} 步：模型规划中…")
        job.emit("reasoning", f"\n\n──── 第 {step} 步 ────\n")
        try:
            raw = llm.chat(msgs, model=model, temperature=0.2,
                           max_tokens=PLAN_MAX_TOKENS, on_event=_cb)
        except Exception as exc:  # noqa: BLE001
            job.finish(error=f"模型调用失败：{type(exc).__name__}: {exc}")
            return

        try:
            plan = llm.extract_json(raw)
        except Exception:  # noqa: BLE001
            parse_fails += 1
            job.emit("obs", json.dumps({"name": "(解析)", "ok": False,
                                        "summary": "模型没给出合法 JSON，要求重试"},
                                       ensure_ascii=False))
            msgs.append({"role": "assistant", "content": raw[:2000]})
            msgs.append({"role": "user",
                         "content": '你上一轮不是合法 JSON。请只输出一个 JSON 对象，'
                                    '形如 {"thought":"...","tool":"...","args":{...}} '
                                    '或 {"thought":"...","final":"..."}。'})
            if parse_fails <= 3:
                step -= 1          # 格式错不算一步（最多白送 3 次），别浪费步数预算
            continue

        thought = str(plan.get("thought") or "")
        if thought:
            job.emit("plan", json.dumps({"thought": thought}, ensure_ascii=False))

        # ---- 收尾 ----
        if plan.get("final"):
            final = str(plan["final"])
            job.emit("phase", "已生成结论")
            for i in range(0, len(final), 80):
                job.emit("content", final[i:i + 80])
            job.finish(result={"answer": job.content, "steps": step})
            return

        # ---- 执行工具 ----
        name = str(plan.get("tool") or "")
        args = plan.get("args") or {}
        if name not in TOOLS:
            obs = f"未知工具 {name}。可用工具：{', '.join(TOOLS)}"
            ok = False
        else:
            job.emit("tool", json.dumps({"name": name, "args": args}, ensure_ascii=False))
            job.emit("phase", f"执行 {name} …")
            t0 = time.time()
            try:
                obs = TOOLS[name]["fn"](args if isinstance(args, dict) else {})
                ok = True
            except Exception as exc:  # noqa: BLE001
                obs, ok = f"{type(exc).__name__}: {exc}", False
            job.emit("obs", json.dumps({"name": name, "ok": ok,
                                        "ms": int((time.time() - t0) * 1000),
                                        "summary": obs[:1200]}, ensure_ascii=False))

        msgs.append({"role": "assistant",
                     "content": json.dumps({"thought": thought, "tool": name, "args": args},
                                           ensure_ascii=False)})
        msgs.append({"role": "user",
                     "content": f"[观察] {name}（{'成功' if ok else '失败'}）返回：\n{_clip(obs)}"})

    # 步数用尽
    job.emit("phase", f"已达最大步数（{MAX_STEPS}），输出阶段结论")
    tail = ("已达最大执行步数，先给阶段结论：请把上文工具观察里**已经拿到**的数据整理成"
            "结论 + 依据 + 下一步建议；没有拿到的数据要明确说明。")
    try:
        raw = llm.chat(msgs + [{"role": "user", "content": tail}], model=model,
                       temperature=0.2, max_tokens=PLAN_MAX_TOKENS, on_event=None)
        try:
            final = str(llm.extract_json(raw).get("final") or raw)
        except Exception:  # noqa: BLE001
            final = raw
    except Exception as exc:  # noqa: BLE001
        job.finish(error=f"收尾失败：{type(exc).__name__}: {exc}")
        return
    for i in range(0, len(final), 80):
        job.emit("content", final[i:i + 80])
    job.finish(result={"answer": job.content, "steps": MAX_STEPS})
