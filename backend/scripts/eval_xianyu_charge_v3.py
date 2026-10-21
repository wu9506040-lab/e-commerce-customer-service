#!/usr/bin/env python3
"""
闲鱼充电宝话术准确率评测 V3 — 60 题结构化评测集（售前 20 / 售中 20 / 售后 20）

与 V2 的区别：
- 题目固定 60 道（不再随机抽样），售前/售中/售后全链路覆盖
- expected 关键词经 2026-10-21 全量探测校准（避开 order_query 路由歧义问法）
- 双口径评分：严格通过率（PASS/总数）+ 加权得分（PASS + PARTIAL×0.5）

用法：
  python eval_xianyu_charge_v3.py
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / "deploy" / ".env.dev"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

CHAT_URL = os.getenv("CHAT_URL", "http://127.0.0.1:8000/api/chat")

# 商品/店铺设定（与 daemon reply_scheduler 注入的 context 一致——防止答案漂移到"官方旗舰店"）
CONTEXT_BLOCK = """【当前商品】
- 卖家账号: 中华模具店（主业模具设计制造，副业清闲置）
- 当前商品: 智选 20000mAh 充电宝 PB1（闲鱼闲置转让）
- 价格: ¥99999（不二价）
- 商品成色: 95 新，未拆封，朋友送闲置
- 售后承诺: 1 年卖家个人质保 + 7 天无理由 + 容量保 80%"""

# =============================================================================
# 60 题评测集（售前 20 / 售中 20 / 售后 20）
# expected 关键词 = 该题最低限度应包含的事实点（经 2026-10-21 探测校准）
# =============================================================================
QUESTIONS = [
    # ================= 售前（参数/成色/议价/店铺）20 题 =================
    {"stage": "售前", "category": "闲聊", "query": "在吗?", "expected": ["在"]},
    {"stage": "售前", "category": "参数", "query": "容量多大?", "expected": ["20000mAh"]},
    {"stage": "售前", "category": "参数", "query": "能带上飞机吗?", "expected": ["74Wh"]},
    {"stage": "售前", "category": "参数", "query": "能给笔记本充电吗?", "expected": ["65W"]},
    {"stage": "售前", "category": "参数", "query": "有几个输出口?", "expected": ["Type-C"]},
    {"stage": "售前", "category": "参数", "query": "自己充满要多久?", "expected": ["3.5"]},
    {"stage": "售前", "category": "参数", "query": "有多重?", "expected": ["420"]},
    {"stage": "售前", "category": "参数", "query": "是多少瓦时的?", "expected": ["74Wh"]},
    {"stage": "售前", "category": "正品", "query": "正品保障有吗?", "expected": ["正品"]},
    {"stage": "售前", "category": "成色", "query": "成色怎么样?", "expected": ["95"]},
    {"stage": "售前", "category": "成色", "query": "二手的介意吗?", "expected": ["闲置", "95"]},
    {"stage": "售前", "category": "成色", "query": "包装完整吗?", "expected": ["压痕"]},
    {"stage": "售前", "category": "容量", "query": "容量虚标吗?", "expected": ["80"]},
    {"stage": "售前", "category": "参数", "query": "能充 iPhone 吗?", "expected": ["快充"]},
    {"stage": "售前", "category": "类比", "query": "跟小米比有什么优势?", "expected": ["65W"]},
    {"stage": "售前", "category": "店铺", "query": "为什么模具店卖充电宝?", "expected": ["模具"]},
    {"stage": "售前", "category": "店铺", "query": "是厂家吗?", "expected": ["闲置"]},
    {"stage": "售前", "category": "议价", "query": "太贵了能少点吗?", "expected": ["赠品"]},
    {"stage": "售前", "category": "议价", "query": "能抹个零头吗?", "expected": ["不二价", "不议价"]},
    {"stage": "售前", "category": "政策", "query": "包邮吗?", "expected": ["包邮"]},

    # ================= 售中（下单/发货/物流/发票）20 题 =================
    {"stage": "售中", "category": "下单", "query": "怎么下单?", "expected": ["下单"]},
    {"stage": "售中", "category": "发货", "query": "今天几点前拍下能当天发?", "expected": ["17:00"]},
    {"stage": "售中", "category": "支付", "query": "能货到付款吗?", "expected": ["货到付款"]},
    {"stage": "售中", "category": "支付", "query": "支持哪些支付方式?", "expected": ["支付"]},
    {"stage": "售中", "category": "支付", "query": "可以分期付款吗?", "expected": ["分期"]},
    {"stage": "售中", "category": "自提", "query": "可以自提吗?", "expected": ["自提"]},
    {"stage": "售中", "category": "催发", "query": "帮我催下仓库", "expected": ["仓储"]},
    {"stage": "售中", "category": "物流", "query": "包裹一直在中转", "expected": ["催促"]},
    {"stage": "售中", "category": "物流", "query": "包裹一直停在同一个网点怎么办?", "expected": ["网点"]},
    {"stage": "售中", "category": "签收", "query": "朋友说包裹没收到怎么办?", "expected": ["签收", "核实"]},
    {"stage": "售中", "category": "改地址", "query": "地址填错了还能改吗?", "expected": ["地址"]},
    {"stage": "售中", "category": "改地址", "query": "地址写错了怎么办?", "expected": ["地址"]},
    {"stage": "售中", "category": "运费", "query": "发新疆西藏能包邮吗?", "expected": ["新疆"]},
    {"stage": "售中", "category": "漏发", "query": "漏发了一件怎么办?", "expected": ["漏发"]},
    {"stage": "售中", "category": "漏发", "query": "少发了赠品怎么办?", "expected": ["补发"]},
    {"stage": "售中", "category": "错发", "query": "发错货了怎么办?", "expected": ["换货"]},
    {"stage": "售中", "category": "发票", "query": "能开电子发票吗?", "expected": ["电子发票"]},
    {"stage": "售中", "category": "发票", "query": "想要纸质发票", "expected": ["纸质发票"]},
    {"stage": "售中", "category": "发票", "query": "发票多久能开出来?", "expected": ["工作日"]},
    {"stage": "售中", "category": "发票", "query": "发票开错了怎么办?", "expected": ["红冲"]},

    # ================= 售后（退款/退货/质保/投诉）20 题 =================
    {"stage": "售后", "category": "退款", "query": "我要退款", "expected": ["退款", "专员"]},
    {"stage": "售后", "category": "退款", "query": "不喜欢能退吗?", "expected": ["退款", "专员"]},
    {"stage": "售后", "category": "退款", "query": "怎么申请退款", "expected": ["退款"]},
    {"stage": "售后", "category": "退款", "query": "退款几天到账?", "expected": ["工作日"]},
    {"stage": "售后", "category": "退货", "query": "7天无理由退货", "expected": ["无理由"]},
    {"stage": "售后", "category": "退货", "query": "怎么申请退货退款?", "expected": ["退款"]},
    {"stage": "售后", "category": "退货", "query": "退货后退款多久?", "expected": ["工作日"]},
    {"stage": "售后", "category": "仅退款", "query": "已签收仅退款怎么操作?", "expected": ["退款"]},
    {"stage": "售后", "category": "退款原因", "query": "退款原因选错了怎么办?", "expected": ["修改"]},
    {"stage": "售后", "category": "退货运费", "query": "退货运费谁出?", "expected": ["运费"]},
    {"stage": "售后", "category": "质保", "query": "保修多久?", "expected": ["保修"]},
    {"stage": "售后", "category": "质保", "query": "电池保修多久?", "expected": ["电池"]},
    {"stage": "售后", "category": "质保", "query": "保修期内坏了", "expected": ["保修"]},
    {"stage": "售后", "category": "质量", "query": "质量有问题怎么办?", "expected": ["协商"]},
    {"stage": "售后", "category": "容量", "query": "电池能用多久衰减吗?", "expected": ["衰减"]},
    {"stage": "售后", "category": "损坏", "query": "收到的东西是坏的", "expected": ["补发"]},
    {"stage": "售后", "category": "换货", "query": "可以换货吗?", "expected": ["换货"]},
    {"stage": "售后", "category": "保价", "query": "降价了能退差价吗?", "expected": ["差价"]},
    {"stage": "售后", "category": "人工", "query": "找人工客服", "expected": ["人工"]},
    {"stage": "售后", "category": "投诉", "query": "投诉你们", "expected": ["人工"]},
]


def call_chat(query: str, timeout: int = 90) -> dict:
    body = {"query": query, "channel": "xianyu", "session_id": "xianyu-68099209693", "context_block": CONTEXT_BLOCK}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CHAT_URL, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", errors="replace")[:300]}
    except Exception as e:
        return {"error": str(e)}
    chunks, contexts, intent = [], [], ""
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            evt = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        t = evt.get("type", "")
        if t == "meta":
            intent = evt.get("intent", "")
            contexts = evt.get("contexts", []) or []
        elif t == "token":
            chunks.append(evt.get("text", ""))
    return {"answer": "".join(chunks), "contexts": contexts, "intent": intent}


def main():
    n = len(QUESTIONS)
    print(f"[v3] 60 题评测集 | 共 {n} 题 | {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}\n", flush=True)

    results = []
    for i, q in enumerate(QUESTIONS, 1):
        t0 = time.time()
        r = call_chat(q["query"])
        dt = time.time() - t0
        answer = r.get("answer", "")
        intent = r.get("intent", "?")

        hit = [k for k in q["expected"] if k in answer]
        miss = [k for k in q["expected"] if k not in answer]

        if "error" in r:
            verdict = "ERROR"
        elif not answer or len(answer) < 2:
            verdict = "EMPTY"
        elif len(miss) == 0:
            verdict = "PASS"
        elif hit:
            verdict = "PARTIAL"
        else:
            verdict = "MISS"

        results.append({
            "id": i, "stage": q["stage"], "category": q["category"], "query": q["query"],
            "expected": q["expected"], "hit": hit, "miss": miss,
            "answer": answer, "intent": intent,
            "latency_sec": round(dt, 1), "verdict": verdict,
        })
        print(f"[{i:02d}/{n}] {q['stage']}·{q['category']} {q['query']} -> {verdict} "
              f"({dt:.1f}s, intent={intent}) 命中{hit} 漏{miss}", flush=True)

    # 汇总（双口径）
    def score(rows):
        p = sum(1 for r in rows if r["verdict"] == "PASS")
        pt = sum(1 for r in rows if r["verdict"] == "PARTIAL")
        bad = sum(1 for r in rows if r["verdict"] in ("MISS", "ERROR", "EMPTY"))
        strict = round(p / len(rows) * 100, 1) if rows else 0
        weighted = round((p + pt * 0.5) / len(rows) * 100, 1) if rows else 0
        return p, pt, bad, strict, weighted

    out = {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
           "total": n, "results": results, "summary": {}}
    for stage in ("售前", "售中", "售后"):
        rows = [r for r in results if r["stage"] == stage]
        p, pt, bad, strict, weighted = score(rows)
        out["summary"][stage] = {"pass": p, "partial": pt, "miss_or_error": bad,
                                 "strict_rate": strict, "weighted_score": weighted}
        print(f"\n== {stage} == PASS {p} / PARTIAL {pt} / MISS+ERR {bad}"
              f" | 严格通过率 {strict}% | 加权 {weighted}%")
    p, pt, bad, strict, weighted = score(results)
    out["summary"]["总计"] = {"pass": p, "partial": pt, "miss_or_error": bad,
                              "strict_rate": strict, "weighted_score": weighted}
    print(f"\n== 总计 == PASS {p} / PARTIAL {pt} / MISS+ERR {bad}"
          f" | 严格通过率 {strict}% | 加权得分 {weighted}%")
    lat = sorted(r["latency_sec"] for r in results)
    print(f"平均延迟 {round(sum(lat)/len(lat),1)}s | P50 {lat[len(lat)//2]}s | 最大 {lat[-1]}s")

    # 失败明细（迭代用）
    fails = [r for r in results if r["verdict"] != "PASS"]
    if fails:
        print(f"\n未通过 {len(fails)} 题：")
        for r in fails:
            print(f"  [{r['verdict']}] {r['stage']}·{r['query']} | 漏 {r['miss']} | intent={r['intent']}")
            print(f"    答: {r['answer'][:100].replace(chr(10),' ')}")

    raw_out = Path(__file__).resolve().parents[2] / "docs" / "eval" / "xianyu_charge_v3_raw.json"
    raw_out.parent.mkdir(parents=True, exist_ok=True)
    raw_out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[v3] raw 保存: {raw_out}")
    print(f"[v3] 达标判定（≥80%）: 严格 {strict}% / 加权 {weighted}%")


if __name__ == "__main__":
    main()
