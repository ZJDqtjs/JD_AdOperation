# -*- coding: utf-8 -*-
"""从 analysis2.json 的 words.b 生成「否定词」待执行清单。

规则（对齐视频方法论 + 项目 SKILL §6）：
  - waste 词：花费≥阈值 且 订单=0  → 精确否定
  - low   词：花费≥10 且 ROI<保本  → 精确否定（先否，比降价更干脆；与用户"先加否定词"一致）
  - 跨窗口确认：w2 与 w1 都命中才更稳；只有 w2 命中的单独标注 single
阈值：WASTE_MIN = 3.0 元（B 账号客单低，3 元已够 5+ 次点击）
输出：data/negatives_b.json
"""
import json

WASTE_MIN = 3.0
LOW_MIN = 10.0

d = json.load(open("data/analysis2.json", encoding="utf-8"))
words = d["words"]["b"]
plans = {p["name"]: p for p in d["plans"] if p.get("account") == "b"}


def collect(wk):
    out = {}
    for pname, pv in words.get(wk, {}).items():
        for x in pv.get("sw", []):
            if x.get("kind") == "waste" and x.get("cost", 0) >= WASTE_MIN:
                out.setdefault(pname, {})[x["word"]] = {
                    "cost": round(x["cost"], 1), "ord": x.get("ord", 0),
                    "roi": round(x.get("roi", 0), 2), "clk": x.get("clk", 0), "why": "waste"}
            elif x.get("kind") == "low" and x.get("cost", 0) >= LOW_MIN:
                out.setdefault(pname, {})[x["word"]] = {
                    "cost": round(x["cost"], 1), "ord": x.get("ord", 0),
                    "roi": round(x.get("roi", 0), 2), "clk": x.get("clk", 0), "why": "low"}
    return out


w2 = collect("w2")
w1 = collect("w1")

result = []
total_cost = 0.0
for pname, p in plans.items():
    cid = p["campaignId"]
    be = p["breakeven"]
    words2 = w2.get(pname, {})
    words1 = w1.get(pname, {})
    items = []
    for w, meta in sorted(words2.items(), key=lambda kv: -kv[1]["cost"]):
        conf = "confirmed" if w in words1 else "single-w2"
        action = "apply" if conf == "confirmed" else "review"   # 只跨窗口确认的才直接执行
        items.append({"word": w, **meta, "be": be, "confidence": conf, "action": action})
        total_cost += meta["cost"]
    # 只有 w1 命中、w2 没有的：w2 已不再花钱，无需否定；仅记录备查
    for w in words1:
        if w not in words2:
            meta = words1[w]
            items.append({"word": w, **meta, "be": be, "confidence": "only-w1",
                          "action": "skip"})
    if items:
        result.append({"campaignId": cid, "plan": pname, "breakeven": be,
                       "negatives": items})

result.sort(key=lambda r: -sum(i["cost"] for i in r["negatives"] if i.get("action") == "apply"))
n_apply = sum(1 for r in result for i in r["negatives"] if i.get("action") == "apply")
n_review = sum(1 for r in result for i in r["negatives"] if i.get("action") == "review")
cost_apply = sum(i["cost"] for r in result for i in r["negatives"] if i.get("action") == "apply")
out = {"meta": {"source": "analysis2.json words.b", "wasteMin": WASTE_MIN,
                "lowMin": LOW_MIN, "totalWasteCost": round(total_cost, 1),
                "nPlans": len(result), "nWords": sum(len(r["negatives"]) for r in result),
                "nApply": n_apply, "nReview": n_review,
                "costApply": round(cost_apply, 1)},
       "plans": result}
json.dump(out, open("data/negatives_b.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

print("=== 否定词待执行清单（账号B）===")
print(f"直接执行(跨2窗口确认) {n_apply} 词 / 涉及 ¥{cost_apply:.1f}；"
      f"待复核(仅w2) {n_review} 词；9计划合计命中 {out['meta']['nWords']} 词 ¥{out['meta']['totalWasteCost']}\n")
for r in result:
    print(f"● {r['plan']}  ({r['campaignId']})  保本{r['breakeven']}")
    for it in r["negatives"]:
        mark = {"apply": "✔执行", "review": "?复核", "skip": "-跳过"}.get(it.get("action"), it.get("action"))
        print(f"    [{it['confidence']:>10}][{mark}] {it['word']:<26} 花{it['cost']:>5} 单{it['ord']} ROI{it['roi']:>5}  ({it['why']})")
    print()
