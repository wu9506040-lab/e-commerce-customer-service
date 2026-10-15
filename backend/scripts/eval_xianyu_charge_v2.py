#!/usr/bin/env python3
"""
闲鱼充电宝话术准确率评测 V2 — 随机生成测试题（不复用 V1 题目）

特点：
- 题库按品类分池，每次随机抽 15 道
- 避免固定题目集导致对话路径记忆
- 同一品类下 variant 题目测同一意图的多角度表达

用法：
  python eval_xianyu_charge_v2.py [--seed=2026]
"""
import json
import os
import random
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

# =============================================================================
# 题目池（每条带 expected_keywords 评分依据）
# =============================================================================
QUESTION_POOL = [
    # ---- 闲聊 (5) ----
    {"category": "闲聊", "query": "在吗?", "expected": ["在", "客服", "您好"]},
    {"category": "闲聊", "query": "在不在?", "expected": ["在"]},
    {"category": "闲聊", "query": "你好 想咨询下", "expected": ["您好", "客服"]},
    {"category": "闲聊", "query": "hello 麻烦看下商品", "expected": ["您好", "在"]},
    {"category": "闲聊", "query": "掌柜的", "expected": ["在", "客服"]},

    # ---- 参数 (8) ----
    {"category": "参数", "query": "多大容量?", "expected": ["20000mAh", "74Wh"]},
    {"category": "参数", "query": "输出功率多少?", "expected": ["65W"]},
    {"category": "参数", "query": "能给笔记本充电吗?", "expected": ["笔记本", "65W"]},
    {"category": "参数", "query": "能充 iPhone 吗?", "expected": ["iPhone", "Type-C"]},
    {"category": "参数", "query": "几个输出口?", "expected": ["Type-C", "USB-A"]},
    {"category": "参数", "query": "自己充满要多久?", "expected": ["30W", "Type-C"]},
    {"category": "参数", "query": "有多重?", "expected": ["420g"]},
    {"category": "参数", "query": "这个是多少瓦时的?", "expected": ["74Wh"]},

    # ---- 政策 (7) ----
    {"category": "政策/民航", "query": "飞机能带吗?", "expected": ["100Wh", "74Wh", "民航"]},
    {"category": "政策/民航", "query": "国际航班能带吗?", "expected": ["74Wh", "民航"]},
    {"category": "政策/质保", "query": "保修多久?", "expected": ["1 年", "卖家"]},
    {"category": "政策/质保", "query": "电池保修多久?", "expected": ["1 年", "电池"]},
    {"category": "政策/退换", "query": "收到不喜欢能退吗?", "expected": ["7 天", "原样"]},
    {"category": "政策/退换", "query": "运费谁出?", "expected": ["运费", "承担"]},
    {"category": "政策", "query": "包邮吗?", "expected": ["包邮"]},

    # ---- 议价 (8) ----
    {"category": "议价", "query": "太贵了能少点吗?", "expected": ["不让", "赠品"]},
    {"category": "议价", "query": "5000 行不行?", "expected": ["不二价", "价格"]},
    {"category": "议价", "query": "8000 卖不卖?", "expected": ["不二价", "价格"]},
    {"category": "议价", "query": "便宜 100 块也行", "expected": ["价格", "赠品"]},
    {"category": "议价", "query": "新人没优惠吗?", "expected": ["新客", "价格", "赠品"]},
    {"category": "议价", "query": "老客户打折不?", "expected": ["老客", "价格"]},
    {"category": "议价", "query": "这个价有什么优惠?", "expected": ["赠品", "数据线", "充电头"]},
    {"category": "议价", "query": "能抹个零头吗?", "expected": ["价格", "赠品"]},

    # ---- 类比/边界 (6) ----
    {"category": "类比", "query": "跟小米比有什么优势?", "expected": ["容量", "卖家"]},
    {"category": "类比", "query": "和华为比呢?", "expected": ["容量", "卖家"]},
    {"category": "类比", "query": "比京东京造的如何?", "expected": ["74Wh", "65W"]},
    {"category": "边界", "query": "你们店是干嘛的?", "expected": ["模具", "闲置"]},
    {"category": "边界", "query": "为啥叫模具店?", "expected": ["模具", "主业"]},
    {"category": "边界", "query": "是厂家吗?", "expected": ["闲置", "朋友"]},

    # ---- 场景/瑕疵 (5) ----
    {"category": "场景", "query": "出差能用多久?", "expected": ["出差", "三口"]},
    {"category": "场景", "query": "露营能用吗?", "expected": ["420g"]},
    {"category": "瑕疵", "query": "二手的介意吗?", "expected": ["闲置", "95新", "未拆封"]},
    {"category": "瑕疵", "query": "包装完整吗?", "expected": ["包装", "压痕"]},
    {"category": "瑕疵", "query": "电池衰减吗?", "expected": ["保 80%", "1 年"]},

    # ---- 退款/转人工 (3) ----
    {"category": "退款", "query": "我要退款", "expected": ["转接", "专员", "24"]},
    {"category": "退款", "query": "不好用想退", "expected": ["退", "原样"]},
    {"category": "退款", "query": "能退货吗", "expected": ["退"]},
]

# 取 15 题（每品类至少 1 题，余下随机补齐）
def sample_questions(n=15, seed=None):
    rng = random.Random(seed)
    by_cat = {}
    for q in QUESTION_POOL:
        by_cat.setdefault(q["category"], []).append(q)
    cats = list(by_cat.keys())
    selected = []
    # 先每品类至少 1 题
    for c in cats:
        if by_cat[c]:
            selected.append(rng.choice(by_cat[c]))
    # 余下题随机补
    pool = [q for q in QUESTION_POOL if q not in selected]
    rng.shuffle(pool)
    while len(selected) < n and pool:
        selected.append(pool.pop())
    # 打乱
    rng.shuffle(selected)
    # 重新编号
    for i, q in enumerate(selected, 1):
        q["id"] = i
    return selected


CONTEXT_BLOCK = """【当前买家】
- 昵称: 测试买家
- 渠道: 闲鱼
- 角色: 咨询买家（非老客，新客首次）

【当前商品】
- 卖家账号: 中华模具店（主业模具，副业闲置数码转让）
- 当前商品: 智选 20000mAh 充电宝 PB1（闲鱼闲置转让）
- 价格: ¥99999（不二价）
- 商品成色: 95 新，未拆封，朋友送闲置

【当前场景】
售前咨询（售前话术要点）
- 价值塑造：稀缺性 + 品质值 + 服务值 + 诚意值
- 议价原则：¥99999 不二价，赠品补偿（数据线 + 充电头 + 包邮）
- 卖家故事：中华模具店是模具店主家，朋友送的近全新闲置转让，不是厂家零售
"""


def call_chat(query: str, timeout: int = 60) -> dict:
    body = {
        "query": query,
        "channel": "xianyu",
        "context_block": CONTEXT_BLOCK,
    }
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
    seed = int(sys.argv[1].split("=")[1]) if len(sys.argv) > 1 and "=" in sys.argv[1] else 2026
    questions = sample_questions(n=15, seed=seed)
    print(f"[v2] seed={seed} | 共 {len(questions)} 题\n", flush=True)

    results = []
    for q in questions:
        t0 = time.time()
        r = call_chat(q["query"])
        dt = time.time() - t0
        answer = r.get("answer", "")
        contexts = r.get("contexts", [])
        intent = r.get("intent", "?")

        # 关键词命中（同义也算：用包含判定）
        hit = [k for k in q["expected"] if k in answer]
        miss = [k for k in q["expected"] if k not in answer]
        ctx_has_sku011 = any("sku011" in str(c.get("source", "")) for c in contexts if isinstance(c, dict))

        # 判定
        if "error" in r:
            verdict = "ERROR"
        elif not answer or len(answer) < 2:
            verdict = "EMPTY"
        elif intent in ("order_query", "refund_query") and "未登录" in answer:
            verdict = "MISS_FIXTURE"
        elif len(miss) == 0:
            verdict = "PASS"
        elif hit:
            verdict = "PARTIAL"
        else:
            verdict = "MISS"

        results.append({
            "id": q["id"], "category": q["category"], "query": q["query"],
            "expected": q["expected"], "hit": hit, "miss": miss,
            "answer": answer, "intent": intent, "ctx_has_sku011": ctx_has_sku011,
            "latency_sec": round(dt, 1), "verdict": verdict,
        })

        # console 输出
        print(f"--- 题目 {q['id']:02d} [{q['category']}] : {q['query']} ---", flush=True)
        print(f"  intent={intent} | {verdict} | {dt:.1f}s | sku011={ctx_has_sku011}")
        print(f"  命中: {hit}/{len(q['expected'])} 漏: {miss}")
        print(f"  答: {answer[:140]}...")
        print()

    # 保存 raw
    raw_out = Path(__file__).resolve().parents[2] / "docs" / "eval" / "xianyu_charge_v2_raw.json"
    raw_out.parent.mkdir(parents=True, exist_ok=True)
    raw_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[v2] raw 保存: {raw_out}")

    # 汇总
    by_v = {}
    for r in results:
        by_v.setdefault(r["verdict"], []).append(r["id"])
    n_total = len(results)
    n_pass = len(by_v.get("PASS", []))
    n_part = len(by_v.get("PARTIAL", []))
    n_miss = n_total - n_pass - n_part - len(by_v.get("ERROR", [])) - len(by_v.get("MISS_FIXTURE", []))
    n_fix = len(by_v.get("MISS_FIXTURE", []))
    n_err = len(by_v.get("ERROR", [])) + len(by_v.get("EMPTY", []))
    score = round((n_pass + n_part * 0.5) / n_total * 100, 1)
    rag_hit = sum(1 for r in results if r["ctx_has_sku011"])
    print(f"\n[v2] 汇总：")
    for v, ids in sorted(by_v.items()):
        print(f"  {v}: {len(ids)} 题 {ids}")
    print(f"  加权得分: {score}% (PASS={n_pass} PARTIAL={n_part})")
    print(f"  RAG 召回 sku011: {rag_hit}/{n_total}")


if __name__ == "__main__":
    main()
