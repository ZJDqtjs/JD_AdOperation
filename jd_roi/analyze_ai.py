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

from . import industry, llm, settings

DATA_DIR = settings.DATA_DIR
ANALYSIS = DATA_DIR / "analysis2.json"

# 方法论来源：优先读本项目随仓库维护的知识库（官方课程提炼），
# 再读本机已安装的 skill（jd-ad-roi-optimizer.zip 解出的 references/）。
# 顺序即优先级；同名文件只取先命中的那份，避免两处副本重复喂给模型。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_SKILL = _PROJECT_ROOT / ".trae" / "skills" / "jd-ad-roi-optimizer"
_SKILL_DIR = Path.home() / ".workbuddy" / "skills" / "jd-ad-roi-optimizer"
METHODOLOGY_CANDIDATES = [
    _PROJECT_SKILL / "references" / "kuaiche-playbook.md",
    _SKILL_DIR / "references" / "kuaiche-playbook.md",
    _PROJECT_SKILL / "references" / "methodology.md",
    _SKILL_DIR / "references" / "methodology.md",
    _PROJECT_SKILL / "references" / "diagnosis-playbook.md",
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
def _f(v, d=0.0) -> float:
    """摘要层统一的宽松数值转换（接口可能给字符串、None 或空串）。"""
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _w(x: dict) -> dict:
    return {"cost": round(x.get("cost", 0), 1), "roi": round(x.get("roi", 0), 2),
            "ctr": round(x.get("ctr", 0), 2), "cvr": round(x.get("cvr", 0), 2),
            "cpc": round(x.get("cpc", 0), 2), "aov": round(x.get("aov", 0), 2),
            "imp": x.get("imp", 0), "clk": x.get("clk", 0),
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
        "hourly": {},
        "hourly_gaps": [],
        "industry": {},
        "industry_gaps": [],
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

    # 观察期/日点击要用「所选区间」的实际天数，不能默认 7 天
    _rng = meta.get("range") or {}
    try:
        _days = (_rng["end"] and _rng["start"] and
                 (dt.date.fromisoformat(str(_rng["end"])) -
                  dt.date.fromisoformat(str(_rng["start"]))).days + 1) or 7
    except Exception:  # noqa: BLE001
        _days = 7

    plans = [p for p in data.get("plans", []) if not account or p.get("account") in keys]
    for p in plans:
        wins = p.get("wins", {})
        if sum((wins.get(wk) or {}).get("cost", 0) for wk in ("w0", "w1", "w2")) <= 0:
            continue          # 三期都没花费的计划没有决策价值，剔除以压小输入
        m = wins.get("w2") if (wins.get("w2") or {}).get("cost", 0) > 0 else wins.get("w1") or {}
        be = _f(p.get("breakeven"))
        margin = _f(p.get("planMargin")) or ((1 / be) if be > 0 else 0.0)
        aov = _f(m.get("aov")); cvr = _f(m.get("cvr")) / 100.0; cpc = _f(m.get("cpc"))
        be_cpc = round(aov * cvr * margin, 2) if (aov > 0 and cvr > 0 and margin > 0) else None
        digest["plans"].append({
            "campaignId": p.get("campaignId"), "name": p.get("name"),
            "account": p.get("account"), "type": p.get("typeLabel"),
            "breakeven": p.get("breakeven"), "grow_line": p.get("growLine"),
            "cost_mapped": p.get("costMapped"),
            "margin": round(margin, 4),
            # 代码先算好保本 CPC 与提价空间，模型只解释、不重算（避免算数幻觉）
            "breakeven_cpc": be_cpc,
            "cpc_headroom_pct": (round((be_cpc - cpc) / be_cpc * 100, 1)
                                 if (be_cpc and cpc) else None),
            "clicks_per_day": round(_f(m.get("clk")) / _days, 1),
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
    type_of = {p.get("name"): p.get("typeLabel") for p in plans}
    for a in accts:
        aw = words.get(a["key"], {}).get("w2", {})
        for pname, pv in aw.items():
            # 快车关键词计划没有搜索词明细，改用关键词报表；两者都空才跳过
            sw, kw = pv.get("sw") or [], pv.get("kw") or []
            bank = sw or kw
            if not bank:
                continue
            src = "searchTerm" if sw else "keywordName"

            def pick(kind):
                return [{"w": x["word"], "roi": round(x.get("roi", 0), 2),
                         "cost": round(x.get("cost", 0), 1), "ord": x.get("ord", 0),
                         "clk": x.get("clk", 0)}
                        for x in sorted([y for y in bank if y.get("kind") == kind],
                                        key=lambda z: -z.get("cost", 0))[:words_per_kind]]
            n_kind = {k: sum(1 for y in bank if y.get("kind") == k)
                      for k in ("good", "waste", "low")}
            digest["words"][pname] = {
                "plan_type": type_of.get(pname) or "未知",
                "source": src,
                "sample_size": len(bank),
                "counts": n_kind,
                "good": pick("good"), "waste": pick("waste"), "low": pick("low"),
            }

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

    # 分时（京准通列表行「趋势」的背后数据）—— 分时折扣 / 时段出价的唯一依据。
    # 注意口径：夜间抓取只抓「昨天」那一天，所以这是「最近一天的小时分布」，不是区间分布，
    # 摘要里显式带 `days` 与 `scope`，避免模型把它当成整段区间的结论。
    hy = data.get("hourly") or {}
    for a in accts:
        key = a["key"]
        b = hy.get(key) or {}
        if not b.get("available"):
            digest["hourly"][key] = {"available": False,
                                     "note": "本轮未抓到分时数据（该数据只在夜间抓取时抓昨天一天）"}
            continue
        tot = b.get("totals") or {}
        plans_out = []
        for p in (b.get("plans") or [])[:12]:
            hs = [h for h in (p.get("hours") or []) if _f(h.get("cost")) > 0]
            # 「最差时段」必须先花过钱：不然一整排「花 0.5 元 / ROI 0」的噪声小时会挤掉真正在烧钱的时段
            pcost = _f(p.get("cost"))
            floor = max(pcost / 24 * 0.5, 5.0)
            heavy = [h for h in hs if _f(h.get("cost")) >= floor] or hs
            worst = sorted(heavy, key=lambda x: _f(x.get("roi")))[:4]
            best = sorted(hs, key=lambda x: -_f(x.get("roi")))[:3]
            plans_out.append({
                "campaignId": p.get("campaignId"), "name": str(p.get("name") or "")[:24],
                "cost": round(pcost, 1), "roi": round(_f(p.get("roi")), 2),
                "worst_hours": [{"h": h.get("hour"), "cost": round(_f(h.get("cost")), 1),
                                 "roi": round(_f(h.get("roi")), 2),
                                 "cpc": round(_f(h.get("cpc")), 2)} for h in worst],
                "best_hours": [{"h": h.get("hour"), "cost": round(_f(h.get("cost")), 1),
                                "roi": round(_f(h.get("roi")), 2)} for h in best],
            })
        digest["hourly"][key] = {
            "available": True, "days": b.get("days"),
            "scope": "仅「最近一天」的 24 小时分布（非区间分布）",
            "total": {"cost": round(_f(tot.get("cost")), 1), "amt": round(_f(tot.get("amt")), 1),
                      "roi": round(_f(tot.get("roi")), 2)},
            "hours": [{"h": h.get("hour"), "cost": round(_f(h.get("cost")), 1),
                       "amt": round(_f(h.get("amt")), 1), "roi": round(_f(h.get("roi")), 2),
                       "cpc": round(_f(h.get("cpc")), 2), "share": h.get("share")}
                      for h in (b.get("hours") or [])],
            "plans": plans_out,
        }
    digest["hourly_gaps"] = [g for g in (data.get("hourlyGaps") or [])
                             if not account or g.get("account") == account]

    # 行业大盘：客观事实 + 规则层算好的缺口；指数只用于比率对比，模型不许换算成金额
    ind = industry.compact_for_ai(data.get("industry"))
    if account:
        ind["accounts"] = [a for a in (ind.get("accounts") or [])
                           if a.get("account") == account]
        ind["gaps"] = [g for g in (ind.get("gaps") or [])
                       if g.get("account") == account]
    digest["industry"] = ind
    digest["industry_gaps"] = ind.get("gaps") or []

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

【官方课程《1小时吃透快车》硬规则（选词/出价/调整必用）】
8. 排名 = 出价 × 质量分；扣费 = 下一名出价 × 下一名质量分 ÷ 你的质量分 + 0.01。
   出价高 ≠ 排名高，出价 ≠ 扣费；质量分低时硬抢 TOP1 只会烧钱。
9. 质量分四构成：基础（销量/评价/动销/库存）、相关性（词与三级类目/标题）、
   创意（点击率）、转化（转化率，权重最高）。质量分没有后台数值，看「平均点击成本是否持续下降」。
10. 选词：三级类目下选；精准长尾 70% + 品类核心大词 30%；建车期 20~50 个词；
    长尾初始出价 = 行业均值 ×0.8~1.0，核心大词 ×1.10~1.20。
11. 最终出价 = 基础出价 × 时间折扣 × 人群溢价 × 抢排名溢价（乘法链）；新品期不圈人群、不溢价。
12. 日预算 ≈ 日均点击成本 × 50~100 点击；至少跑 3~4 天再调词/调价，单日数据无代表性。
13. 高消耗 0 产出的「吸血词」要否定；优质词抢排名或单独建计划给足预算。
    否定词入口只在「关键词计划 → 计划 → 单元 → 关键词 → 否定词设置」；
    智能化 / 全站智能推广**没有否定词入口**，只能改目标投产比/预算/商品黑名单。
"""


def load_methodology() -> tuple[str, str]:
    """加载方法论：本项目课程知识库优先，已安装 skill 兜底，最后退回内置要点。

    返回 (正文, 来源标签)。来源标签会展示在报表/AI 台上，用来确认本次分析
    到底吃到了哪份知识库（例如 `skill-file:kuaiche-playbook+methodology`）。
    """
    parts, names, seen = [], [], set()
    for p in METHODOLOGY_CANDIDATES:
        if p.name in seen or not p.exists():
            continue
        try:
            parts.append(p.read_text(encoding="utf-8"))
            names.append(p.stem)
            seen.add(p.name)
        except Exception:  # noqa: BLE001
            continue
    if parts:
        return "\n\n---\n\n".join(parts), "skill-file:" + "+".join(names)
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
    "quality_diagnosis": [{
        "campaignId": "必须来自输入的 plans（最多 4 条，按 w2 花费降序）",
        "name": "计划名",
        "items": [{
            "part": "基础质量分/相关性/创意质量分/转化质量分 之一",
            "level": "高/中/低/无法判断",
            "evidence": "引用输入里的销量评价线索、CTR、CVR、CPC 趋势、词与类目匹配等客观事实",
            "action": "针对这一构成项的可执行动作（如换主图测点击率、补评价、改标题相关性）",
        }],
        "summary": "该计划质量分的整体判断与最优先补的一项",
    }],
    "bid_plan": [{
        "campaignId": "必须来自输入的 plans（最多 5 条）",
        "name": "计划名",
        "current_cpc": "输入的 w2 cpc",
        "breakeven_cpc": "输入里代码算好的 breakeven_cpc（不要自己重算）",
        "headroom": "有提价空间/接近保本/已超保本",
        "suggest": "结合最终出价=基础×时间×人群×抢排名，给出具体动作：抬价幅度、抢排名、还是降价/关溢价",
    }],
    "playbook_audit": [{
        "campaignId": "必须来自输入的 plans（最多 6 条，按 w2 花费降序）",
        "name": "计划名",
        "stage": "拉新/收割/收割+放大",
        "checks": [{
            "item": "词量/词配比/出价空间/日点击预算/观察周期/否定词入口 之一",
            "status": "达标/不足/超限/不适用/需人工确认",
            "evidence": "必须引用输入里的数字（词量、CPC vs 保本CPC、点击/天、最近调整距今天数、计划类型）",
            "action": "对应课程规则的可执行动作（可复制的后台路径/参数）",
        }],
        "conclusion": "先观察/可放大/需改造/建议收缩 之一，并写一句话理由",
    }],
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
    "dayparting_advice": [{
        "scope": "account（账号级时段策略）或 plan（某计划的时段策略）",
        "campaignId": "scope=plan 时必填，必须来自输入的 hourly[].plans / plans；account 时留空",
        "target": "作用对象：账号 key 或 plan 名",
        "hours": "要调整的小时，如「04:00-05:00、23:00」或「18:00-22:00」",
        "direction": "降价/关停/提价/放宽预算 之一",
        "evidence": "必须引用输入 hourly / hourly_gaps 里的小时花费与 ROI 数字（含该时段在全天的花费占比）",
        "method": "为什么这个时段该这样调（结合保本线、CPC 异常、拉新/收割节奏讲原理）",
        "suggest": "可执行做法：折扣幅度/预算调整量/先测几天；以及要盯的验证指标",
        "priority": "高/中/低",
    }],
    "industry_advice": [{
        "topic": "行业趋势/本店 vs 行业/地域/关键词/热词与飙升词/新客/品牌/时段 之一",
        "finding": "只引用 industry 里的客观事实，必须带具体数字（含单位/pp）",
        "benchmark": "对应的行业基准数字，或「无行业基准」",
        "evidence": "引用 industry / industry_gaps 里的事实，不得改写数字",
        "suggest": "可落地的动作：加什么词/地域、怎么测、先盯哪个指标、观察几天",
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
- 【分时只当「最近一天」看】输入里的 `hourly` 是**最近一天 24 小时**的分布（`days` 字段写了具体日期），
  **不是**所选区间的分布，严禁当成区间结论；引用时必须带上那个日期。
  若某账号 `hourly.available=false`，说明本轮没抓分时，不要编造时段数据。
  有数据时，在 `dayparting_advice` 里给出时段策略：优先处理「花费占比高但 ROI 明显低于全天均值」的时段
  （`hourly_gaps` 已算好，直接引用其中的小时、花费、ROI）；同时指出高 ROI 时段该怎么加。
  注意：分时 ROI 按**点击所在小时**归因，越晚的时段归因窗口越短、ROI 天然偏低，
  所以对 23:00 这类时段的判断要留余地，别只凭一天的数据下死结论。
- 【行业大盘必须单独总结并给建议】输入 `industry.available=true` 时，在 `industry_advice` 里
  按「行业趋势 → 本店 vs 行业 → 地域 → 关键词/热词 → 新客/品牌 → 时段」挑最关键的 3~5 条写，
  每条必须有 `finding`（引用具体数字）、`benchmark`（行业基准）、`evidence`、`suggest`（可执行动作）、`priority`；
  `industry_gaps` 是代码算好的缺口，优先引用。**指数不是金额**：展现/点击/加购指数不能相加、不能当花费，
  也不能和本店成交额比大小；只对比 CTR/转化率/加购率等比率，且必须写清「pp」或「%」。
  若 `industry.available=false` 或某账号没有行业缓存，就明说本轮没有行业数据，不要编造行业数字。
- 【只给待审建议】不要发号施令，只给**待人工审阅**的建议。
- 【课程硬规则必须落地】方法论里的《1小时吃透快车》规则不是背景知识，必须在下列字段里被引用：
  * `quality_diagnosis`：按「基础质量分 / 相关性 / 创意质量分 / 转化质量分」四项拆，
    每项给 level + 证据 + 动作；没有数据的项写「无法判断」，不要编。
  * `bid_plan`：直接采用输入里代码算好的 `breakeven_cpc`，对比 `cpc` 判断「有提价空间/接近保本/已超保本」；
    建议必须体现「最终出价 = 基础出价 × 时间折扣 × 人群溢价 × 抢排名溢价」。
  * `playbook_audit`：逐计划检查「词量（建车 20~50）、词配比（长尾70/核心大词30，无法判定就写需人工确认）、
    日点击是否 50~100、观察周期是否满 3~4 天、否定词入口是否可用」；status 必须用给定枚举。
- 【否定词入口硬约束】`keyword_plan` 只能给**关键词计划**（type 含「关键词/快车」）的否定词；
  智能化/全站智能推广**没有否定词入口**，其 waste/low 词只能在 `playbook_audit` 里写成
  「无否定入口 → 改目标投产比/预算/商品黑名单」，绝不能写进 `keyword_plan`。
- 【控制篇幅，务必输出完整 JSON】diagnosis ≤5 条；quality_diagnosis ≤4 条；bid_plan ≤5 条；
  playbook_audit ≤6 条；plan_judgements 最多 8 条（按 w2 花费从高到低，
  只判断最关键的，不要逐个计划都写）；keyword_plan 只对「确有可否定/可提价词」的计划给；
  targeting_advice ≤5 条（按 priority 高到低，优先未启用的）；
  dayparting_advice ≤4 条（账号级时段策略优先，其次只挑时段差异最大的 1-2 个计划）；
  industry_advice ≤5 条（按 priority 高到低，优先有明确行业基准与缺口的）；
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
    # 课程 SOP 三段：容忍不同模型返回的字段名漂移
    for std, alts in (
        ("playbook_audit", ("sop_audit", "sop_checks", "playbook", "plan_sop")),
        ("quality_diagnosis", ("quality_score", "quality_breakdown", "quality_audit")),
        ("bid_plan", ("bidding_advice", "bid_advice", "bid_space", "cpc_plan")),
    ):
        if std not in advice:
            for alt in alts:
                if isinstance(advice.get(alt), list):
                    advice[std] = advice.pop(alt)
                    break
    for x in advice.get("playbook_audit", []) or []:
        if isinstance(x, dict) and not x.get("checks") and isinstance(x.get("items"), list):
            x["checks"] = x.pop("items")
        if isinstance(x, dict) and not x.get("conclusion"):
            for alt in ("verdict", "summary", "result"):
                if x.get(alt):
                    x["conclusion"] = x[alt]
                    break
    for x in advice.get("quality_diagnosis", []) or []:
        if isinstance(x, dict) and not x.get("items") and isinstance(x.get("parts"), list):
            x["items"] = x.pop("parts")
    for x in advice.get("bid_plan", []) or []:
        if not isinstance(x, dict):
            continue
        if not x.get("current_cpc"):
            for alt in ("cpc", "now_cpc", "actual_cpc"):
                if x.get(alt):
                    x["current_cpc"] = x[alt]
                    break
        if not x.get("breakeven_cpc"):
            for alt in ("be_cpc", "breakeven_cpc", "break_even_cpc"):
                if x.get(alt):
                    x["breakeven_cpc"] = x[alt]
                    break
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
    # 分时（时段）建议
    if "dayparting_advice" not in advice:
        for alt in ("hour_advice", "dayparting", "time_advice", "hourly_advice",
                    "daypartingAdvice", "schedule_advice"):
            if isinstance(advice.get(alt), list):
                advice["dayparting_advice"] = advice.pop(alt)
                break
    for d in advice.get("dayparting_advice", []) or []:
        if not isinstance(d, dict):
            continue
        if not d.get("hours"):
            for alt in ("hour", "time", "period", "hours_range"):
                if d.get(alt):
                    d["hours"] = d[alt]
                    break
        if not d.get("scope"):
            d["scope"] = "plan" if d.get("campaignId") else "account"
        if not d.get("suggest"):
            for alt in ("action", "advice", "recommend", "howto"):
                if d.get(alt):
                    d["suggest"] = d[alt]
                    break
    # 行业大盘观察/建议：容忍模型字段名漂移
    if "industry_advice" not in advice:
        for alt in ("market_advice", "industry", "industryAdvice", "benchmark_advice"):
            if isinstance(advice.get(alt), list):
                advice["industry_advice"] = advice.pop(alt)
                break
    for x in advice.get("industry_advice", []) or []:
        if not isinstance(x, dict):
            continue
        if not x.get("suggest"):
            for alt in ("action", "advice", "recommend", "howto"):
                if x.get(alt):
                    x["suggest"] = x[alt]
                    break
        if not x.get("finding") and x.get("observation"):
            x["finding"] = x["observation"]
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

    # 分时建议：scope=plan 时必须指向真实存在的计划；且不能引用没有分时数据的账号
    hy = digest.get("hourly") or {}
    no_hour = {k for k, v in hy.items() if not v.get("available")}
    for d in advice.get("dayparting_advice", []) or []:
        if not isinstance(d, dict):
            continue
        if str(d.get("scope") or "").lower() == "plan":
            cid = str(d.get("campaignId") or "")
            if cid and cid not in plan_ids:
                issues.append(f"dayparting_advice 引用了不存在的 campaignId={cid}")
        claim = (str(d.get("evidence") or "") + str(d.get("finding") or ""))
        for k in no_hour:
            if k and k in claim:
                warnings.append(f"dayparting_advice 引用了本轮没有分时数据的账号「{k}」")

    # 新增的课程 SOP 段落同样校验 campaignId，防止模型编计划
    for sec in ("quality_diagnosis", "bid_plan", "playbook_audit"):
        for x in advice.get(sec, []) or []:
            if not isinstance(x, dict):
                continue
            cid = str(x.get("campaignId", ""))
            if cid and cid not in plan_ids:
                issues.append(f"{sec} 引用了不存在的 campaignId={cid}（{x.get('name')}）")

    n_pj = len([x for x in (advice.get("plan_judgements") or []) if isinstance(x, dict)])
    n_kp = len([x for x in (advice.get("keyword_plan") or []) if isinstance(x, dict)])
    n_ta = len([x for x in (advice.get("targeting_advice") or []) if isinstance(x, dict)])
    n_dp = len([x for x in (advice.get("dayparting_advice") or []) if isinstance(x, dict)])
    n_q = len([x for x in (advice.get("quality_diagnosis") or []) if isinstance(x, dict)])
    n_bid = len([x for x in (advice.get("bid_plan") or []) if isinstance(x, dict)])
    n_sop = len([x for x in (advice.get("playbook_audit") or []) if isinstance(x, dict)])
    n_ind = len([x for x in (advice.get("industry_advice") or []) if isinstance(x, dict)])
    if n_ind and not (digest.get("industry") or {}).get("available"):
        warnings.append("本轮没有行业大盘缓存，但模型输出了 industry_advice（可能编造行业数据）")
    return {"ok": not issues, "issues": issues, "warnings": warnings,
            "n_plan_judgements": n_pj, "n_keyword_plan": n_kp,
            "n_targeting_advice": n_ta, "n_dayparting_advice": n_dp,
            "n_quality_diagnosis": n_q, "n_bid_plan": n_bid, "n_playbook_audit": n_sop,
            "n_industry_advice": n_ind,
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
- 【分时数据只代表最近一天】摘要里 `hourly` / `hourly_gaps` 是**最近一天 24 小时**的分布
  （`days` 字段写明是哪天），**不是**所选区间的分布；引用时必须带上那个日期，严禁当成区间结论。
  某账号 `hourly.available=false` 表示本轮没抓分时，不要编造时段数字。
  分时 ROI 按点击所在小时归因，越晚的时段归因窗口越短、ROI 天然偏低，别只凭一天对深夜时段下死结论。
- 【行业大盘只对比率】摘要里 `industry` / `industry_gaps` 是京准通按三级类目汇总的**指数化**数据：
  展现/点击/加购指数不是金额、不能相加、不能和成交额比大小；只能引用 CTR/转化率/加购率等比率及
  规则层算好的地域/关键词/新客缺口。用户问行业、地域大盘、热词、竞品时，优先引用 `industry` 里的
  客观数字与 `industry_gaps` 的建议；`industry.available=false` 就说明本轮没有行业数据，不要编造。
- 【课程规则优先】选词/出价/质量分/否定词/预算/调整节奏的问题，按方法论里的
  《1小时吃透快车》硬规则回答（排名=出价×质量分、最终出价=基础×时间×人群×抢排名、
  长尾70/核心大词30、日预算≈CPC×50~100点击、至少跑3~4天再调）。
- 【否定词入口】只有关键词计划能设否定词；智能化/全站智能推广没有否定词入口，
  只能改目标投产比/预算/商品黑名单，回答时必须区分，不能给错入口。
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
    if a.get("playbook_audit"):
        L += ["## 快车 SOP 体检（官方课程硬规则）", ""]
        for x in a["playbook_audit"]:
            L.append(f"### {x.get('name')}（{x.get('campaignId')}） → {x.get('conclusion', '')}")
            for c in (x.get("checks") or []):
                L.append(f"- [{c.get('status', '')}] {c.get('item', '')}：{c.get('evidence', '')}"
                         f" → {c.get('action', '')}")
            L.append("")
    if a.get("quality_diagnosis"):
        L += ["## 质量分四构成诊断", ""]
        for x in a["quality_diagnosis"]:
            L.append(f"### {x.get('name')}（{x.get('campaignId')}）")
            for it in (x.get("items") or []):
                L.append(f"- {it.get('part', '')}：{it.get('level', '')} —— "
                         f"{it.get('evidence', '')} → {it.get('action', '')}")
            if x.get("summary"):
                L.append(f"- 小结：{x['summary']}")
            L.append("")
    if a.get("bid_plan"):
        L += ["## 出价与保本 CPC 空间", ""]
        for x in a["bid_plan"]:
            L.append(f"### {x.get('name')}（{x.get('campaignId')}）")
            L.append(f"- 实际 CPC {x.get('current_cpc')} ｜ 保本 CPC {x.get('breakeven_cpc')} ｜ {x.get('headroom', '')}")
            L.append(f"- 建议：{x.get('suggest', '')}")
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
