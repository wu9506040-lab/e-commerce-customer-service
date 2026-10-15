#!/usr/bin/env python3
"""
闲鱼充电宝话术准确率评测脚本（V1）

任务：串跑 15 题 × POST /api/chat,收集真实答案 + 检索到 contexts
输出：results.json（raw）+ console 打印关键命中

评测题设计要点（参考 docs/eval/xianyu_charge_eval_2026-10-15.md）：
- 5 类场景全覆盖：闲聊/参数/政策/议价/边界
- 包含 ¥99999 不二价核心场景（验收设计点是否落地）
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------
# 配置
# ---------------------------------------------------------------
ENV_FILE = Path(__file__).resolve().parents[2] / "deploy" / ".env.dev"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

CHAT_URL = os.getenv("CHAT_URL", "http://127.0.0.1:8000/api/chat")
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
EMBEDDING_MODEL = "text-embedding-v3"


# ---------------------------------------------------------------
# 15 道测试题
# ---------------------------------------------------------------
QUESTIONS = [
    # --- 闲聊 ---
    {
        "id": 1, "category": "闲聊",
        "query": "在吗?",
        "expected_keywords": ["在", "客服"],
    },
    # --- 参数 ---
    {
        "id": 2, "category": "参数",
        "query": "这个充电宝多大容量?输出多少瓦?",
        "expected_keywords": ["20000mAh", "65W"],
    },
    {
        "id": 3, "category": "参数/三口",
        "query": "能同时充笔记本和手机吗?",
        "expected_keywords": ["三口", "65W", "18W"],
    },
    # --- 政策（民航/质保）---
    {
        "id": 4, "category": "政策/民航",
        "query": "能带上飞机吗?",
        "expected_keywords": ["民航", "100Wh", "74Wh", "登机"],
    },
    {
        "id": 5, "category": "政策/质保",
        "query": "1 年坏了找谁?",
        "expected_keywords": ["1 年", "卖家", "质保"],
    },
    # --- 价格 + 议价（核心亮点）---
    {
        "id": 6, "category": "议价/价格",
        "query": "¥99999 怎么这么贵啊?",
        "expected_keywords": ["容量", "卖家", "质保", "不议价"],
        "must_not_contain": ["便宜不了", "已经是最低价", "亲亲"],
    },
    {
        "id": 7, "category": "议价/砍价",
        "query": "8000 行不行?",
        "expected_keywords": ["不让", "价格", "赠品"],
        "must_not_contain": ["可以", "好,8000"],
    },
    # --- 类比 ---
    {
        "id": 8, "category": "类比",
        "query": "跟罗马仕比有什么区别?",
        "expected_keywords": ["容量", "三口", "卖家", "质保"],
    },
    # --- 边界（卖家名）---
    {
        "id": 9, "category": "边界/卖家名",
        "query": "你是「中华模具店」?怎么卖起充电宝来了?",
        "expected_keywords": ["闲置", "模具", "转让", "朋友"],
    },
    # --- 退款（触发转人工）---
    {
        "id": 10, "category": "退款/转人工",
        "query": "我要退款",
        "expected_keywords": ["转接", "专员", "24", "小时", "客服"],
        "must_not_contain": ["好,马上退款", "已退款"],
    },
    # --- 单位换算 ---
    {
        "id": 11, "category": "参数/单位",
        "query": "20000 毫安时等于多少瓦时?",
        "expected_keywords": ["74Wh"],
    },
    # --- 场景 ---
    {
        "id": 12, "category": "场景/MacBook",
        "query": "充 MacBook 多久能充满?",
        "expected_keywords": ["65W", "MacBook", "小时"],
    },
    # --- 政策（退换）---
    {
        "id": 13, "category": "政策/退换",
        "query": "7 天无理由具体怎么操作?",
        "expected_keywords": ["7 天", "原样", "包装", "私聊"],
    },
    # --- 老客 ---
    {
        "id": 14, "category": "议价/老客",
        "query": "我是老客户,便宜点吧",
        "expected_keywords": ["老客", "不让", "赠品", "数据线"],
    },
    # --- 瑕疵 ---
    {
        "id": 15, "category": "瑕疵",
        "query": "是二手的吗?有什么瑕疵?",
        "expected_keywords": ["闲置", "未拆封", "外包装", "压痕"],
    },
]


# ---------------------------------------------------------------
# 闲鱼渠道 context_block（构造完整买家上下文）
# ---------------------------------------------------------------
CONTEXT_BLOCK = """【当前买家】
- 昵称: 面试官小张
- 渠道: 闲鱼
- 角色: 咨询买家（非老客，新客首次）

【当前商品】
- 卖家账号: 中华模具店（主业模具，副业闲置数码转让）
- 当前商品: 智选 20000mAh 充电宝 PB1（闲鱼闲置转让）
- 价格: ¥99999（不二价：朋友送的近全新，凑个吉利数，卖一台少一台）
- 商品成色: 95 新，未拆封，朋友送用不上闲置转让

【当前场景】
售前咨询（售前话术要点）
- 价值塑造四件套：稀缺性 + 品质值 + 服务值 + 诚意值
- 议价原则：¥99999 不二价，但赠品补偿（数据线 + 充电头 + 包邮）
- 卖家故事："中华模具店"是模具店主家，朋友送的近全新闲置转让，不是厂家零售

【闲鱼渠道特别提醒】
- 不主动退款、不主动确认收货、不主动转账
- 退款 / 投诉 / 二次议价 转接专员
- 7 天无理由 + 1 年卖家质保（个人承诺，非厂家）
- 民航标准: 74Wh < 100Wh，可携带登机
"""


def call_chat(query: str, timeout: int = 60, buyer_unb: str | None = None) -> dict:
    """POST /api/chat (sse 流式,合并成一段)"""
    body = {
        "query": query,
        "channel": "xianyu",
        "context_block": CONTEXT_BLOCK,
    }
    if buyer_unb is not None:
        body["buyer_unb"] = buyer_unb
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        CHAT_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", errors="replace")[:300]}
    except Exception as e:
        return {"error": str(e)}

    # SSE 解析: `id:` + `data: {...}` 多行
    chunks_text = []
    contexts = []  # 从 meta 提取
    scores = []
    intent = ""
    for line in raw.splitlines():
        line = line.strip()
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
            scores = evt.get("scores", []) or []
        elif t == "token":
            chunks_text.append(evt.get("text", ""))
        elif t == "done":
            pass
        elif t == "context":
            ctx = evt.get("context", {})
            if isinstance(ctx, dict):
                contexts.append(ctx)
        elif t == "contexts":
            contexts.extend(evt.get("contexts", []) or [])
        elif t == "scores":
            scores = evt.get("scores", []) or scores
        elif t == "sources":
            contexts.extend(evt.get("sources", []) or [])
        elif t == "answer":
            return {"answer": evt.get("text", ""), "contexts": contexts, "intent": intent, "raw": raw}

    answer = "".join(chunks_text)
    return {
        "answer": answer,
        "contexts": contexts,
        "intent": intent,
        "scores": scores,
        "raw_len": len(raw),
    }


def main():
    print(f"[eval] CHAT_URL={CHAT_URL}")
    print(f"[eval] QWEN_API_KEY={'SET' if QWEN_API_KEY else 'MISSING'}")
    print(f"[eval] 共 {len(QUESTIONS)} 题\n")

    results = []
    for q in QUESTIONS:
        print(f"--- 题目 {q['id']:02d}: {q['query'][:40]} ---")
        t0 = time.time()
        r = call_chat(q["query"], buyer_unb=None)
        dt = time.time() - t0

        answer = r.get("answer", "")
        contexts = r.get("contexts", [])

        # 关键词命中
        hit_kw = []
        for k in q.get("expected_keywords", []):
            if k in answer:
                hit_kw.append(k)
        miss_kw = [k for k in q.get("expected_keywords", []) if k not in answer]

        # 不应包含的关键词
        forbidden_hit = [k for k in q.get("must_not_contain", []) if k in answer]

        # context 中是否包含 sku011_*
        ctx_has_sku011 = any(
            "sku011" in str(c.get("source", "")) or "sku011" in str(c.get("title", ""))
            for c in contexts
            if isinstance(c, dict)
        )

        verdict = "?"
        if "error" in r:
            verdict = "ERROR"
        elif "answer" in r and len(answer) < 2:
            verdict = "EMPTY"
        elif forbidden_hit:
            verdict = "FAIL"
        elif len(miss_kw) == 0 and len(hit_kw) >= max(1, len(q.get("expected_keywords", [])) // 2):
            verdict = "PASS"
        elif hit_kw:
            verdict = "PARTIAL"
        else:
            verdict = "MISS"

        results.append({
            "id": q["id"],
            "category": q["category"],
            "query": q["query"],
            "expected": q.get("expected_keywords", []),
            "must_not_contain": q.get("must_not_contain", []),
            "hit_keywords": hit_kw,
            "miss_keywords": miss_kw,
            "forbidden_hits": forbidden_hit,
            "answer": answer,
            "contexts": [{"source": c.get("source",""), "title": c.get("title",""), "score": c.get("score","?")}
                         for c in contexts if isinstance(c, dict)][:5],
            "ctx_has_sku011": ctx_has_sku011,
            "latency_sec": round(dt, 1),
            "verdict": verdict,
        })
        print(f"  [{verdict}] {dt:.1f}s | ctx.sku011={ctx_has_sku011} | "
              f"hit={len(hit_kw)}/{len(q.get('expected_keywords',[]))} | "
              f"forbidden={forbidden_hit}")
        print(f"  答: {answer[:200]}")
        print()

    # 保存
    out = Path(__file__).resolve().parents[2] / "docs" / "eval" / "xianyu_charge_raw_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] 结果保存: {out}")

    # 汇总
    by_verdict = {}
    for r in results:
        by_verdict.setdefault(r["verdict"], []).append(r["id"])
    print("\n[eval] 汇总:")
    for v, ids in sorted(by_verdict.items()):
        print(f"  {v}: {len(ids)} 题 ({ids})")


if __name__ == "__main__":
    main()
