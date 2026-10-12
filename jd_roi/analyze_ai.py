# -*- coding: utf-8 -*-
"""AI 解读层（独立于规则层）。

定位：规则层（analyze2）继续产出**确定性**的 analysis2.json；
本模块把「**本地当天已抓取**的原始指标 + 方法论」喂给 LLM，让它**套用方法论做判断**，
产出规则层没有的诊断/取舍/顺序，而不是把规则层的档位结论换个格式再念一遍。

铁律：
  - 不碰 analyze2 / report2，不改规则层产物。
  - 算数一律由代码完成；模型只做解释、归因、优先级、话术。
  - 不把规则层结论（`advice.summary` / 计划 `tier`）塞进输入 —— 否则模型只会复读。
  - 模型输出的 campaignId 必须来自输入；否定/加价词只能取自输入里的词库，否则标记为问题。

数据来源：**只读本地按天库**（analyze2.build 不请求京东接口），
区间由 `local_range()` 从实际已抓取的天推算，不自己拍一个区间。

用法：
  python -m jd_roi.analyze_ai                      # 全部账号，区间=本地最近 AI_RANGE_DAYS 天
  python -m jd_roi.analyze_ai --account=b          # 只看账号B
  python -m jd_roi.analyze_ai --start=... --end=... # 指定区间
  python -m jd_roi.analyze_ai --dry-run            # 只打印将发送的摘要与提示词，不调用
  python -m jd_roi.analyze_ai --model=xxx --out=data/ai_advice.json
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

from . import llm, settings

DATA_DIR = settings.DATA_DIR
ANALYSIS = DATA_DIR / "analysis2.json"

# 方法论来源：优先读已安装的 skill（jd-ad-roi-optimizer.zip 解出的 references/）
_SKILL_DIR = Path.home() / ".workbuddy" / "skills" / "jd-ad-roi-optimizer"
METHODOLOGY_CANDIDATES = [
    _SKILL_DIR / "references" / "methodology.md",
    _SKILL_DIR / "references" / "diagnosis-playbook.md",
]


def _arg(name: str, default: str | None = None) -> str | None:
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1] or None
    return default


# ---------------- 本地实抓区间 ----------------
def local_range(accounts=None, days: int | None = None,
                anchor: str = "jzt_campaign") -> tuple[str, str] | None:
    """从本地按天库算出「实际已抓取」的最近一段**连续**区间。

    以 anchor（默认京准通概览-计划）为准：取库内最新一天，向前回溯，遇到断层就停，
    最多回溯 days 天（默认 settings.AI_RANGE_DAYS）。
    这样 AI 分析的区间永远贴着本地真实数据，不会去分析一个没抓过的区间。
    """
    from . import dailystore as ds
    accounts = accounts or settings.ACCOUNTS
    dayset: set[str] = set()
    for a in accounts:
        dayset |= set(ds.days_for(a["key"], anchor))
    if not dayset:
        return None
    end = max(dayset)
    n = max(int(days or settings.AI_RANGE_DAYS), 1)
    cur = dt.date.fromisoformat(end)
    start = end
    for i in range(1, n):
        d = (cur - dt.timedelta(days=i)).isoformat()
        if d in dayset:
            start = d
        else:
            break
    return start, end


# ---------------- 摘要构建 ----------------
def _w(x: dict) -> dict:
    return {"cost": round(x.get("cost", 0), 1), "roi": round(x.get("roi", 0), 2),
            "ctr": round(x.get("ctr", 0), 2), "cvr": round(x.get("cvr", 0), 2),
            "cpc": round(x.get("cpc", 0), 2),
            "ord": x.get("ord", 0), "amt": round(x.get("amt", 0), 1)}


def build_digest(data: dict, account: str | None = None, *, max_skus: int = 30,
                 words_per_kind: int = 8, max_ops: int = 12) -> dict:
    """把 analysis2 结构压成「原始指标摘要」。

    刻意**不含** `advice.summary` 与计划 `tier`：那是规则层的结论，
    喂进去只会让模型复读；这里只给数字与事实，让模型套方法论自己下判断。
    """
    meta = data.get("meta", {})
    accts = data.get("accounts", [])
    if account:
        accts = [a for a in accts if a.get("key") == account]
    keys = {a["key"] for a in accts}

    cov = meta.get("coverage") or {}
    coverage = {k: {wk: {"have": (v.get(wk) or {}).get("got"),
                         "want": (v.get(wk) or {}).get("want")}
                    for wk in ("w0", "w1", "w2")}
                for k, v in cov.items() if not account or k in keys}

    digest: dict = {
        "meta": {
            "range": meta.get("range"),
            "windows": [{"key": w.get("key"), "start": w.get("start"),
                         "end": w.get("end"), "label": w.get("label")}
                        for w in (meta.get("windows") or [])],
            "breakeven_flat": meta.get("breakevenFlat"),
            "breakeven_blend": meta.get("breakevenBlend"),
            "margin_blend": meta.get("marginBlend"),
            "grow_factor": meta.get("growFactor"),
            "cost_coverage_share": (meta.get("costCoverage") or {}).get("costShare"),
            "cost_unit_warn": len((meta.get("costCoverage") or {}).get("unitWarn") or []),
            "caliber": meta.get("caliber"),
            "coverage_days": coverage,
        },
        "accounts": [],
        "type_split": {},
        "plans": [],
        "skus": [],
        "words": {},
        "recent_ops": [],
        "targeting": {},
        "targeting_gaps": [],
    }

    for a in accts:
        t = a.get("totals", {})
        row = {"key": a.get("key"), "label": a.get("label")}
        for wk in ("w0", "w1", "w2"):
            if t.get(wk):
                m = _w(t[wk])
                m["ad_share"] = t[wk].get("adShare")
                m["sz_amt"] = t[wk].get("sz", {}).get("amt") if t[wk].get("sz") else None
                row[wk] = m
        digest["accounts"].append(row)

    # 计划类型拆解（智能化=偏收割 / 快车-关键词=偏拉新）—— 判断结构是否失衡的核心
    ts = data.get("typeSplit") or {}
    for k, per in ts.items():
        if account and k not in keys:
            continue
        digest["type_split"][k] = {}
        for wk in ("w0", "w1", "w2"):
            digest["type_split"][k][wk] = {
                t: {"cost": round(v.get("cost", 0), 1), "roi": round(v.get("roi", 0), 2),
                    "ord": v.get("ord", 0), "n": v.get("n", 0)}
                for t, v in (per.get(wk) or {}).items()}

    plans = [p for p in data.get("plans", []) if not account or p.get("account") in keys]
    for p in plans:
        wins = p.get("wins", {})
        if sum((wins.get(wk) or {}).get("cost", 0) for wk in ("w0", "w1", "w2")) <= 0:
            continue          # 三期都没花费的计划没有决策价值，剔除以压小输入
        digest["plans"].append({
            "campaignId": p.get("campaignId"), "name": p.get("name"),
            "account": p.get("account"), "type": p.get("typeLabel"),
            "breakeven": p.get("breakeven"), "grow_line": p.get("growLine"),
            "cost_mapped": p.get("costMapped"),
            **{wk: _w(wins[wk]) for wk in ("w0", "w1", "w2") if wins.get(wk)},
        })

    skus = [s for s in data.get("skus", [])
            if not account or any(a in keys for a in (s.get("adAccounts") or []))]
    skus.sort(key=lambda s: -((s.get("wins", {}).get("w2") or {}).get("cost") or 0))
    for s in skus[:max_skus]:
        w2 = (s.get("wins", {}).get("w2") or {})
        c = w2.get("cost") or 0
        if c <= 0:
            continue
        digest["skus"].append({
            "skuId": s.get("skuId"), "name": (s.get("name") or "")[:28],
            "breakeven": s.get("breakeven"), "grow_line": s.get("growLine"),
            "roi_w2": round(w2.get("roi", 0), 2), "cost_w2": round(c, 1),
            "cvr_w2": round(w2.get("cvr", 0), 2), "ctr_w2": round(w2.get("ctr", 0), 2),
            "gross_per_order": s.get("grossPerOrder"), "cost_source": s.get("costSource"),
        })

    words = data.get("words", {})
    for a in accts:
        aw = words.get(a["key"], {}).get("w2", {})
        for pname, pv in aw.items():
            sw = pv.get("sw", [])
            if not sw:
                continue
            def pick(kind):
                return [{"w": x["word"], "roi": round(x.get("roi", 0), 2),
                         "cost": round(x.get("cost", 0), 1), "ord": x.get("ord", 0),
                         "clk": x.get("clk", 0)}
                        for x in sorted([y for y in sw if y.get("kind") == kind],
                                        key=lambda z: -z.get("cost", 0))[:words_per_kind]]
            digest["words"][pname] = {"good": pick("good"), "waste": pick("waste"),
                                      "low": pick("low")}

    # 定向维度（人群/地域）—— 判断「有没有在用定向功能」的直接证据。
    # 只给客观行，不下结论；缺口由规则层 targeting_gaps 先算好，AI 只负责解释与给建议。
    tg = data.get("targeting") or {}
    for a in accts:
        key = a["key"]
        block: dict = {}
        for kind, maxn in (("crowd", 12), ("area", 8)):
            items = (tg.get(kind, {}).get(key) or {}).get("w2") or []
            block[kind] = items[:maxn]
        digest["targeting"][key] = block
    digest["targeting_gaps"] = [g for g in (data.get("targetingGaps") or [])
                                if not account or g.get("account") == account]

    # 近期人工操作（只给事实：改了什么、前后 ROI），不下判定，交给模型复盘
    adjs = [x for x in (data.get("adjustments") or [])
            if not account or x.get("account") in keys]
    for op in adjs[-max_ops:]:
        digest["recent_ops"].append({
            "day": op.get("day"), "account": op.get("accountLabel") or op.get("account"),
            "plan": op.get("plan"), "kind": op.get("kind"),
            "before_roi": (op.get("before") or {}).get("roi"),
            "after_roi": (op.get("after") or {}).get("roi"),
        })
    return digest


# ---------------- 方法论 ----------------
_INLINE_RUBRIC = """\
【京东快车底层逻辑·判断准则】
1. 计划分两类：智能计划偏「收割」（优先触达已交互人群），标准/关键词计划偏「拉新」。两者必须搭配，只收割不拉新会枯竭。
2. 保本 ROI = 1/毛利率，是止损线；ROI ≥ 放量线(保本×1.25) 放大，保本≤ROI<放量线 优化，ROI<保本 止损。
3. 词分层：高效词(订单≥2且ROI>保本)提价；废词(花费≥阈值且0单)否定（精确匹配）；低效词(花费≥10且ROI<保本)降价或否定。
4. 否定词必须跨窗口确认，单窗口命中的可能是一时波动，误否会砍掉高效大词。
5. 曝光低→出价/词太小/低出高溢锁死；点击率低→创意+人群匹配（直接影响链接权重）；转化率低→内功（商详/评价/客服/物流/价格）。
6. 高客单、小众精准品应改用标准计划+高溢价人群定向，而不是智能化泛投。
7. 标准计划初期亏损是正常的（拉新成本），盈利靠智能计划收割；不能只看总 ROI 一刀切。
"""


def load_methodology() -> tuple[str, str]:
    parts = []
    for p in METHODOLOGY_CANDIDATES:
        if p.exists():
            try:
                parts.append(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    if parts:
        return "\n\n---\n\n".join(parts), "skill-file"
    return _INLINE_RUBRIC, "inline"


# ---------------- 提示词 ----------------
SCHEMA = {
    "headline": "一句话核心判断：当前投放最关键的矛盾（≤40字）",
    "health": {"verdict": "健康/亚健康/失血", "score": "0-100 的整数",
               "reason": "结合方法论的一段判断（引用关键数字）"},
    "diagnosis": [{
        "aspect": "计划结构/拉新与收割/流量(曝光·CTR)/转化(CVR)/词效率/成本口径 之一",
        "finding": "从输入数据里看到的客观事实，必须引用具体数字",
        "method": "据此对应的快车底层逻辑（讲清原理，不要只写名词）",
        "impact": "这个事实对投产比或长期流量的影响",
    }],
    "account_reads": [{"account": "账号key", "read": "该账号的打法特征与主要问题"}],
    "plan_judgements": [{
        "campaignId": "必须来自输入的 plans（最多 8 条，按 w2 花费降序取最关键的）",
        "name": "计划名", "stage": "拉新/收割/收割+放大",
        "verdict": "放大/优化/收缩/停投/改结构",
        "why": "用方法论解释（引用该计划的 ROI vs 保本/放量线、CTR、CVR、花费占比）",
        "actions": ["2-3 条可执行动作（含出价/预算/否定词/结构改造的具体做法）"],
    }],
    "keyword_plan": [{
        "campaign": "计划名（只给确有可否定/可提价词的计划）", "stage": "",
        "negate": [{"word": "只能取自该计划 waste/low 词", "why": "为什么否"}],
        "boost": [{"word": "只能取自该计划 good 词", "why": "为什么提价"}],
    }],
    "reallocation": {"from": ["计划/品类"], "to": ["计划/品类"],
                     "logic": "为什么把预算从哪挪到哪（结合保本线与放量线）"},
    "targeting_advice": [{
        "feature": "定向人群(DMP)/定向地域/时间折扣/设备定向/测图测词测人群/竞品定向/智能出价 之一",
        "status": "未启用/部分启用/已启用待优化",
        "evidence": "必须引用输入 targeting / targeting_gaps 里的客观事实与数字",
        "method": "对应的快车原理（为什么要做、解决什么问题）",
        "suggest": "具体怎么做（建什么包/怎么设溢价/先小预算测试等，可执行）",
        "priority": "高/中/低",
    }],
    "sequence": [{"step": 1, "do": "先做什么", "because": "为什么这个顺序（哪些能立即止损、哪些要等内功）"}],
    "watchlist": ["上线后要盯的信号/验证点（用哪个指标、期望变成什么）"],
    "risks": ["数据或执行上的风险提示"],
    "confidence": "high/medium/low",
}

SYSTEM = """你是资深京东快车（搜索广告）投放操盘手。你的任务不是复述数据，而是**套用我给你的方法论**，
对这家店当前的投放做出诊断、取舍和动作排序。

硬性规则：
- 【只依据输入】所有数字必须来自我给的【本地已抓取数据摘要】，不得自行计算、推测或编造任何数字。
- 【套方法论】每条结论都要讲清「用了哪条快车逻辑、为什么会这样」。不要只贴名词。
- 【拉新/收割一起看】判断计划结构是否失衡：只收割没拉新要预警；拉新计划短期 ROI 低是正常的，
  要区分「结构性正常亏损」与「真正的失血」。
- 【区分流量问题与转化问题】CTR 低 = 图/词/人群；CVR 低 = 内功（商详/评价/客服/价格）。不要混淆。
- 【词只从输入取】keyword_plan 里的词只能来自对应计划的 good/waste/low 列表；不在列表里的词一律不要写。
- 【campaignId 必须存在】plan_judgements[].campaignId 必须来自输入 plans 里出现过的计划。
- 【指出现成可用的功能缺口】结合输入里的 `targeting`（人群/地域客观行）与 `targeting_gaps`
  （代码已算好的定向缺口），在 `targeting_advice` 里点出**店铺还没用起来或没用好**的投放功能，
  例如：定向人群(DMP 人群包)、定向地域、时间折扣、设备定向、测图测词测人群、竞品定向、智能出价等。
  每条都要引用输入里的客观证据（如「本期人群花费全部来自默认人群」「覆盖 29 个地域中 X 个零成交」），
  讲清方法论原理，并给出**可落地的做法**（建什么包、怎么设溢价、先小预算怎么测）。没有证据的功能不要硬凑。
- 【只给待审建议】不要发号施令，只给**待人工审阅**的建议。
- 【控制篇幅，务必输出完整 JSON】diagnosis ≤5 条；plan_judgements 最多 8 条（按 w2 花费从高到低，
  只判断最关键的，不要逐个计划都写）；keyword_plan 只对「确有可否定/可提价词」的计划给；
  targeting_advice ≤5 条（按 priority 高到低，优先未启用的）；
  account_reads ≤2 条；sequence ≤5 步。宁可少而精，也不要写半截 —— 输出被截断会导致整份解读不可用。
- 输出**只含一个 JSON 对象**，符合给定 schema，不要任何额外解释文字。
"""


def build_messages(digest: dict, methodology: str) -> list[dict]:
    schema_hint = json.dumps(SCHEMA, ensure_ascii=False, indent=1)
    # 摘要用紧凑 JSON（缩进会把输入撑大一倍，慢模型吃不消）
    digest_json = json.dumps(digest, ensure_ascii=False, separators=(",", ":"))
    user = (
        f"【方法论（必须据此判断）】\n{methodology}\n\n"
        f"【本地已抓取数据摘要（唯一数字来源）】\n{digest_json}\n\n"
        f"【输出 schema】\n{schema_hint}\n\n"
        "请严格按 schema 输出 JSON。记住：结论要体现方法论，而不是把数据换个说法念一遍。"
    )
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


# ---------------- 归一化（容忍模型字段名漂移） ----------------
def normalize(advice: dict) -> dict:
    """把模型可能写歪的字段名收敛到约定 schema。"""
    if not isinstance(advice, dict):
        return {"headline": str(advice)}
    # 计划判断
    if "plan_judgements" not in advice:
        for alt in ("plan_actions", "plans", "planActions", "judgements"):
            if isinstance(advice.get(alt), list):
                advice["plan_judgements"] = advice.pop(alt)
                break
    for p in advice.get("plan_judgements", []) or []:
        if not isinstance(p, dict):
            continue
        if not p.get("why"):
            for alt in ("reason", "rationale", "basis", "analysis"):
                if p.get(alt):
                    p["why"] = p[alt]
                    break
        if not p.get("actions"):
            acts = []
            for alt in ("action", "actions", "suggestion", "todo"):
                v = p.get(alt)
                if isinstance(v, str) and v:
                    acts.append(v)
                elif isinstance(v, list):
                    acts += [str(x) for x in v if x]
            if acts:
                p["actions"] = acts
        if not p.get("verdict"):
            for alt in ("tier", "decision", "judgement"):
                if p.get(alt):
                    p["verdict"] = p[alt]
                    break
    # 词计划
    if "keyword_plan" not in advice:
        for alt in ("keyword_candidates", "keywords", "keyword_actions"):
            if isinstance(advice.get(alt), list):
                advice["keyword_plan"] = advice.pop(alt)
                break
    for k in advice.get("keyword_plan", []) or []:
        if not isinstance(k, dict):
            continue
        if "negate" not in k and k.get("keywords_negate"):
            k["negate"] = [{"word": w} for w in k["keywords_negate"]]
        if "boost" not in k and k.get("keywords_boost"):
            k["boost"] = [{"word": w} for w in k["keywords_boost"]]
    # 诊断
    if "diagnosis" not in advice and isinstance(advice.get("findings"), list):
        advice["diagnosis"] = advice.pop("findings")
    # 未使用/待优化功能建议
    if "targeting_advice" not in advice:
        for alt in ("unused_features", "feature_advice", "targeting", "targetingAdvice"):
            if isinstance(advice.get(alt), list):
                advice["targeting_advice"] = advice.pop(alt)
                break
    for t in advice.get("targeting_advice", []) or []:
        if not isinstance(t, dict):
            continue
        if not t.get("feature"):
            for alt in ("name", "item", "func"):
                if t.get(alt):
                    t["feature"] = t[alt]
                    break
        if not t.get("suggest"):
            for alt in ("action", "advice", "recommend", "howto"):
                if t.get(alt):
                    t["suggest"] = t[alt]
                    break
    return advice


# ---------------- 校验 ----------------
def _num(v):
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def validate(advice: dict, digest: dict) -> dict:
    """防幻觉校验：ID 存在性 + 词来源 + 关键数值区间。"""
    plan_ids = {str(p["campaignId"]) for p in digest["plans"]}
    name_to_words = digest["words"]
    issues, warnings = [], []

    for pj in advice.get("plan_judgements", []) or []:
        if not isinstance(pj, dict):
            continue
        cid = str(pj.get("campaignId", ""))
        if cid and cid not in plan_ids:
            issues.append(f"plan_judgements 引用了不存在的 campaignId={cid}（{pj.get('name')}）")

    for kp in advice.get("keyword_plan", []) or []:
        if not isinstance(kp, dict):
            continue
        camp = kp.get("campaign") or kp.get("name") or ""
        bank = name_to_words.get(camp)
        if bank is None:
            # 允许用 campaignId 指代
            hit = next((p for p in digest["plans"] if str(p["campaignId"]) == str(camp)), None)
            if hit:
                bank = name_to_words.get(hit.get("name"))
        if bank is None:
            if camp:
                warnings.append(f"keyword_plan 的计划「{camp}」不在词库里，无法核对词来源")
            continue
        allow_neg = {x["w"] for x in bank.get("waste", [])} | {x["w"] for x in bank.get("low", [])}
        allow_boost = {x["w"] for x in bank.get("good", [])}
        for item in (kp.get("negate") or []):
            w = item.get("word") if isinstance(item, dict) else str(item)
            if w and w not in allow_neg:
                warnings.append(f"否定词「{w}」不在 {camp} 的 waste/low 列表（可能编造）")
        for item in (kp.get("boost") or []):
            w = item.get("word") if isinstance(item, dict) else str(item)
            if w and w not in allow_boost:
                warnings.append(f"加价词「{w}」不在 {camp} 的 good 列表（可能编造）")

    sc = _num((advice.get("health") or {}).get("score"))
    if sc is not None and not (0 <= sc <= 100):
        warnings.append(f"health.score={sc} 超出 0-100")

    n_pj = len([x for x in (advice.get("plan_judgements") or []) if isinstance(x, dict)])
    n_kp = len([x for x in (advice.get("keyword_plan") or []) if isinstance(x, dict)])
    n_ta = len([x for x in (advice.get("targeting_advice") or []) if isinstance(x, dict)])
    return {"ok": not issues, "issues": issues, "warnings": warnings,
            "n_plan_judgements": n_pj, "n_keyword_plan": n_kp,
            "n_targeting_advice": n_ta,
            "n_plan_actions": n_pj}   # 兼容旧前端字段


# ---------------- 主流程 ----------------
def analyze_data(data: dict, account: str | None = None, *, model: str | None = None,
                 dry_run: bool = False, on_event=None) -> dict:
    """对已在内存里的 analysis2 结构做 AI 解读（网页/夜间任务复用）。

    on_event(kind, text)：可选。给了就把模型流式分片（reasoning/content/reset）
    实时回调出去 —— 网页据此展示「思考过程」，不必干等。
    """
    digest = build_digest(data, account)
    methodology, src = load_methodology()
    messages = build_messages(digest, methodology)

    if dry_run:
        est = sum(len(m["content"]) for m in messages) // 2
        return {"dry_run": True, "methodology_source": src,
                "digest": digest, "messages": messages, "est_tokens": est}

    advice, raw = llm.chat_json(messages, model=model, on_event=on_event)
    advice = normalize(advice)
    report = validate(advice, digest)
    dmeta = digest.get("meta", {})
    out = {"generatedAt": settings.now_str(), "account": account or "all",
           "range": dmeta.get("range"),
           "accounts": [a.get("key") for a in digest.get("accounts", [])],
           "windows": dmeta.get("windows"),
           "model": model or llm.resolve_config().get("model"),
           "methodology_source": src, "validation": report,
           "advice": advice, "raw": raw}
    return out


def analyze(account: str | None = None, *, model: str | None = None,
            dry_run: bool = False, analysis_path: Path | None = None) -> dict:
    """从磁盘的 analysis2.json 生成 AI 解读（命令行用）。"""
    path = analysis_path or ANALYSIS
    if not path.exists():
        raise FileNotFoundError(f"缺少 {path}，请先运行 python -m jd_roi.analyze2 <start> <end>")
    data = json.loads(path.read_text(encoding="utf-8"))
    return analyze_data(data, account, model=model, dry_run=dry_run)


def run_for(start: str, end: str, accounts=None, *, model: str | None = None) -> dict:
    """按区间现算 analysis2（纯本地）并生成 AI 解读。"""
    from . import analyze2
    data = analyze2.build(start=start, end=end, accounts=accounts)
    return analyze_data(data, None, model=model)


def run_for_local(accounts=None, *, model: str | None = None,
                  days: int | None = None) -> dict | None:
    """用「本地实际已抓取」的最近连续区间生成解读（夜间任务/手动缺省用）。"""
    rng = local_range(accounts, days)
    if not rng:
        return None
    return run_for(rng[0], rng[1], accounts, model=model)


# ---------------- 对话框（会话式追问） ----------------
CHAT_SYSTEM = """你是资深京东快车（搜索广告）投放操盘手，正在店铺运营的一对一对话里答疑。
目标：结合我给的方法论、以及【本地已抓取数据摘要】，把运营的问题讲清楚、给出可执行建议。

硬性规则：
- 【只依据输入】摘要里有的数字才能引用；摘要里没有的（如某个功能的具体后台开关、操作路径）如不确定
  就明说不确定，并说明还需要哪些数据才能判断。绝不编造数字。
- 【套方法论】判断要讲清「用了哪条快车逻辑、为什么」，不要只贴名词。
- 【落到这家店】尽量引用摘要里出现过的具体计划名 / 词 / 地域 / 账号，而不是泛泛而谈。
- 【未启用的功能主动提示】用户问到某个功能（定向人群 DMP、定向地域、时间折扣、设备定向、
  测图测词测人群、竞品定向、智能出价等）时，先看摘要 `targeting` / `targeting_gaps` 里是否在用，
  再给「要不要做、怎么做、先怎么测」的建议。
- 【篇幅】一般 300~600 字，条理清晰（可用小标题 / 列表）；用户明确要求详细时才展开。
- 一律用简体中文回答。
"""


def prepare_chat_context(data: dict, account: str | None = None) -> tuple[dict, str]:
    """给一次对话会话准备（可缓存的）上下文：摘要 + 方法论。"""
    return build_digest(data, account), load_methodology()[0]


def build_chat_messages(digest: dict, methodology: str, history, question: str) -> list[dict]:
    context = (f"【方法论（判断依据）】\n{methodology}\n\n"
               f"【本地已抓取数据摘要（唯一数字来源）】\n"
               f"{json.dumps(digest, ensure_ascii=False, separators=(',', ':'))}")
    msgs = [{"role": "system", "content": CHAT_SYSTEM + "\n\n" + context}]
    for h in (history or [])[-8:]:
        role = h.get("role")
        if role in ("user", "assistant") and h.get("content"):
            msgs.append({"role": role, "content": str(h["content"])[:4000]})
    msgs.append({"role": "user", "content": question})
    return msgs


def chat_stream_ctx(digest: dict, methodology: str, history, question: str, *,
                    model: str | None = None, temperature: float = 0.4):
    """用已备好的上下文流式回答一个追问（yield 文本分片，含思考过程）。"""
    msgs = build_chat_messages(digest, methodology, history, question)
    return llm.chat_stream_iter(msgs, model=model, temperature=temperature)


def chat_stream_events_ctx(digest: dict, methodology: str, history, question: str, *,
                           model: str | None = None, temperature: float = 0.4):
    """同 chat_stream_ctx，但 yield `(kind, text)`（reasoning/content），供前端分流展示。"""
    msgs = build_chat_messages(digest, methodology, history, question)
    return llm.chat_stream_events(msgs, model=model, temperature=temperature)


# ---------------- 缓存读写（供网页读取） ----------------
def default_path() -> Path:
    return DATA_DIR / "ai_advice.json"


def load_latest(path: Path | None = None) -> dict | None:
    p = path or default_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def matches(out: dict | None, start: str | None, end: str | None,
            keys: list[str] | None) -> bool:
    """判断缓存解读是否对应当前所选区间/账号。"""
    if not out:
        return False
    r = out.get("range") or {}
    if start and end and (r.get("start"), r.get("end")) != (start, end):
        return False
    if keys:
        if set(out.get("accounts") or []) != set(keys):
            return False
    return True


def save(out: dict, path: Path | None = None) -> tuple[Path, Path]:
    p = path or default_path()
    settings.ensure_dirs()
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    md = p.with_suffix(".md")
    md.write_text(_to_markdown(out), encoding="utf-8")
    return p, md


def _to_markdown(out: dict) -> str:
    a = out.get("advice", {})
    v = out.get("validation", {})
    h = a.get("health") or {}
    L = [f"# AI 运营解读（{out.get('account')}）", "",
         f"- 生成：{out.get('generatedAt')}　模型：{out.get('model')}　"
         f"方法论来源：{out.get('methodology_source')}",
         f"- 区间：{(out.get('range') or {}).get('start')} ~ {(out.get('range') or {}).get('end')}",
         f"- 校验：{'通过' if v.get('ok') else '有问题'}　"
         f"计划判断 {v.get('n_plan_judgements', 0)} 条　告警 {len(v.get('warnings', []))} 条", ""]
    if a.get("headline"):
        L += [f"## 核心判断", a["headline"], ""]
    if h:
        L += [f"## 健康度：{h.get('verdict', '—')}（{h.get('score', '—')}/100）", h.get("reason", ""), ""]
    if a.get("diagnosis"):
        L += ["## 诊断（套方法论）", ""]
        for x in a["diagnosis"]:
            L += [f"### {x.get('aspect', '')}",
                  f"- 现象：{x.get('finding', '')}",
                  f"- 方法论：{x.get('method', '')}",
                  f"- 影响：{x.get('impact', '')}", ""]
    if a.get("account_reads"):
        L += ["## 账号特征", ""]
        for x in a["account_reads"]:
            L.append(f"- **{x.get('account')}**：{x.get('read')}")
        L.append("")
    if a.get("targeting_advice"):
        L += ["## 未用起来 / 待优化的投放功能", ""]
        for t in a["targeting_advice"]:
            L += [f"### [{t.get('priority', '')}] {t.get('feature', '')} · {t.get('status', '')}",
                  f"- 证据：{t.get('evidence', '')}",
                  f"- 方法论：{t.get('method', '')}",
                  f"- 建议：{t.get('suggest', '')}", ""]
    if a.get("plan_judgements"):
        L += ["## 逐计划判断", ""]
        for p in a["plan_judgements"]:
            L.append(f"### {p.get('name')}（{p.get('campaignId')}） · {p.get('stage', '')} → {p.get('verdict', '')}")
            L.append(f"- 依据：{p.get('why')}")
            for act in (p.get("actions") or []):
                L.append(f"- 动作：{act}")
            L.append("")
    if a.get("keyword_plan"):
        L += ["## 词级建议（候选，需人工确认）", ""]
        for k in a["keyword_plan"]:
            L.append(f"### {k.get('campaign')}")
            for it in (k.get("negate") or []):
                w = it.get("word") if isinstance(it, dict) else it
                why = it.get("why") if isinstance(it, dict) else ""
                L.append(f"- 否定：{w}（{why}）")
            for it in (k.get("boost") or []):
                w = it.get("word") if isinstance(it, dict) else it
                why = it.get("why") if isinstance(it, dict) else ""
                L.append(f"- 提价：{w}（{why}）")
            L.append("")
    ra = a.get("reallocation") or {}
    if ra.get("logic"):
        L += ["## 预算再分配", f"- 从：{'、'.join(ra.get('from') or [])}",
              f"- 到：{'、'.join(ra.get('to') or [])}", f"- 逻辑：{ra['logic']}", ""]
    if a.get("sequence"):
        L += ["## 执行顺序", ""]
        for s in a["sequence"]:
            L.append(f"{s.get('step', '')}. {s.get('do', '')} —— {s.get('because', '')}")
        L.append("")
    if a.get("watchlist"):
        L += ["## 观察清单", ""] + [f"- {x}" for x in a["watchlist"]] + [""]
    if a.get("risks"):
        L += ["## 风险提示", ""] + [f"- {r}" for r in a["risks"]] + [""]
    if v.get("warnings") or v.get("issues"):
        L += ["## ⚠️ 校验告警（需人工核对）", ""]
        L += [f"- {x}" for x in v.get("issues", []) + v.get("warnings", [])]
    L += ["", "---",
          "> 本建议由 AI 套用《京东快车底层逻辑》方法论生成，**仅供参考**；"
          "算数与口径由规则层代码保证，执行前请人工复核。"]
    return "\n".join(L)


def main() -> int:
    account = _arg("account")
    model = _arg("model")
    start = _arg("start")
    end = _arg("end")
    dry = "--dry-run" in sys.argv
    out_path = Path(_arg("out", str(DATA_DIR / "ai_advice.json")))

    try:
        # 决定分析区间：显式给了就用；否则贴本地实际已抓取的天
        if not (start and end):
            accs = [a for a in settings.ACCOUNTS if a["key"] == account] if account else None
            rng = local_range(accs)
            if not rng:
                print("[缺少数据] 本地没有已抓取的数据，请先运行 python -m jd_roi.scrape_day")
                return 1
            start, end = rng
        from . import analyze2
        data = analyze2.build(start=start, end=end, accounts=None)
        out = analyze_data(data, account, model=model, dry_run=dry)
    except llm.LlmConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    except llm.LlmCallError as exc:
        print(f"[调用失败] {exc}")
        return 3
    except FileNotFoundError as exc:
        print(f"[缺少数据] {exc}")
        return 1

    if dry:
        print(f"methodology_source = {out['methodology_source']}")
        print(f"est_tokens ≈ {out['est_tokens']}")
        print("\n=== 将发送给模型的 user 消息（前 3000 字）===")
        print(out["messages"][1]["content"][:3000])
        return 0

    settings.ensure_dirs()
    out_path, md_path = save(out, out_path)
    v = out["validation"]
    print(f"模型：{out['model']}　区间：{(out.get('range') or {}).get('start')} ~ "
          f"{(out.get('range') or {}).get('end')}　校验：{'通过' if v['ok'] else '有问题'}")
    if v["issues"]:
        for x in v["issues"]:
            print("  [问题]", x)
    if v["warnings"]:
        for x in v["warnings"][:10]:
            print("  [告警]", x)
    print(f"已写出：{out_path}\n        {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
