#!/usr/bin/env python
"""JD-简历 1:1 定制：丢JD进去 → 输出关键词匹配 + 重排版简历

跑法（两个用法）：
    # A. 把 JD 存文件，传入
    python scripts/tailor_resume.py --jd jd.txt --out tailored.txt

    # B. 直接粘 JD（Ctrl+D 结束）
    python scripts/tailor_resume.py --out tailored.txt

输出三段：
  1. **JD 关键词**:高频技术栈 + 必备经验；命中/缺失一目了然
  2. **子弹匹配打分**:每条 bullets 在这份 JD 下的相关度（× 标记）
  3. **定制简历**:按 JD 关键词重新排序 + 缺失关键词插入"个人贡献"行
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = PROJECT_ROOT / "F:/实习/简历/钟维-网申纯文本版.txt"

# 技术关键词词典（关键词 + 同义词归一化）
TECH_KEYWORDS = {
    "rag": ["rag", "检索增强", "混合检索", "hybrid", "rerank", "精排", "embedding", "向量化"],
    "agent": ["agent", "智能体", "orchestrator", "编排", "tool use", "工具调用", "function calling"],
    "langgraph": ["langgraph", "状态机", "state machine", "checkpoint"],
    "llm": ["llm", "大模型", "qwen", "openai", "dashscope", "推理", "infer"],
    "human_in_the_loop": ["human-in-the-loop", "hil", "确认票据", "人机协同", "审批流"],
    "fastapi": ["fastapi", "python", "pydantic", "sqlalchemy", "async"],
    "frontend": ["vue", "typescript", "vite", "前端", "frontend"],
    "data": ["mysql", "redis", "qdrant", "chroma", "postgres", "向量数据库", "embedding"],
    "devops": ["docker", "kubernetes", "ci", "cd", "github actions", "ecs", "nginx"],
    "eval": ["评测", "eval", "metric", "hit@", "f1", "precision", "recall", "drill"],
    "llm_resilience": ["熔断", "超时", "circuit breaker", "超时预算", "deadline", "降级"],
    "ecommerce": ["电商", "售后", "退换货", "退款", "ecommerce"],
    "finance": ["跨境支付", "支付", "金融", "visa", "mastercard", "拒付", "支付清算"],
    "data_annotation": ["数据标注", "qa", "质检", "数据清洗"],
    "prompt": ["prompt", "提示词"],
    # V14.2：制造业 AI / 实习通用关键词补充
    "datascience": ["numpy", "pandas", "sklearn", "pytorch", "数据处理", "特征工程",
                    "机器学习", "深度学习", "数据科学"],
    "lowcode": ["coze", "dify", "百炼", "低代码", "workflow"],
    "manufacturing": ["制造", "工艺", "质检", "视觉质检", "预测性维护", "工艺优化"],
    "docs": ["技术文档", "api", "接口文档", "用户手册"],
}

# 业务/任务关键词（高频 JD 出现）
BUSINESS_KEYWORDS = [
    "电商", "售后", "退款", "退换货", "跨境支付", "金融", "商户", "客服",
    "智能体", "智能客服", "对话系统", "搜索", "推荐", "评测", "数据中台",
    "Agent", "RAG", "LLM", "Prompt",
]


def extract_keywords(text: str) -> dict:
    """返回 {category: [matched_keywords]}。"""
    text_lower = text.lower()
    matched: dict[str, list[str]] = {}
    for cat, kws in TECH_KEYWORDS.items():
        hits = [kw for kw in kws if kw in text or kw.lower() in text_lower]
        if hits:
            matched[cat] = hits
    return matched


def score_bullets_against_keywords(base_text: str, jd_keywords: dict) -> list:
    """对简历每行 bullet 打匹配分（关键词共现数）。返回 [(score, line), ...] 倒序"""
    lines = [l.strip() for l in base_text.split("\n") if l.strip()]
    scored = []
    for line in lines:
        s = 0
        for cat, kws in jd_keywords.items():
            for kw in kws:
                if kw.lower() in line.lower() or kw in line:
                    s += 1
        if s > 0:
            scored.append((s, line))
    scored.sort(key=lambda x: -x[0])
    return scored


def extract_section(base_text: str, start_marker: str, end_marker: str) -> str:
    """提取 base 简历的两段之间的内容（如"项目一"和"项目二"之间）"""
    s = base_text.find(start_marker)
    if s < 0:
        return ""
    e = base_text.find(end_marker, s)
    return base_text[s:e].strip() if e > 0 else base_text[s:].strip()


def render_tailored_resume(base_text: str, jd_keywords: dict, jd_raw: str) -> str:
    """按 JD 关键词密度重新排序 + 在个人贡献行追加命中关键词。返回定制版完整文本"""
    # 抽取项目一 / 项目二 / 技能
    p1 = extract_section(base_text, "【项目一】", "【项目二】")
    p2 = extract_section(base_text, "【项目二】", "【实习经历】")
    skill = extract_section(base_text, "【技能】", "【一句话定位】")
    headline = extract_section(base_text, "钟维", "【项目一】")
    internship = extract_section(base_text, "【实习经历】", "【技能】")

    # 关键字命中加权：项目 1 vs 项目 2 谁更相关？哪个胜出放前面
    p1_score = sum(1 for cat, kws in jd_keywords.items() for kw in kws if kw in p1 or kw.lower() in p1.lower())
    p2_score = sum(1 for cat, kws in jd_keywords.items() for kw in kws if kw in p2 or kw.lower() in p2.lower())

    # 把胜出项目放第一位
    if p2_score > p1_score:
        primary, secondary = p2, p1
        primary_label, secondary_label = "项目二", "项目一"
    else:
        primary, secondary = p1, p2
        primary_label, secondary_label = "项目一", "项目二"

    # 求职意向改为"JD 关键词驱动"（覆盖原"AI Agent / LLM 应用开发实习生"）
    intent_new = build_intent_line(jd_keywords, jd_raw)

    # 缺失关键词：插入到技能行
    missing_kws = collect_missing(jd_keywords, base_text)

    out_lines = [
        headline.rstrip("\n"),
        intent_new,
        "=" * 64,
        primary,
        secondary,
        internship,
        skill.rstrip("\n") + " · " + " · ".join(missing_kws[:5]) if missing_kws else skill,
        "=" * 64,
        "投递方向：AI Agent / LLM 应用开发实习生（基于 JD 关键词匹配定制）",
        f"命中关键词：{' · '.join(sorted({kw for kws in jd_keywords.values() for kw in kws}))}",
    ]
    return "\n".join(out_lines)


def build_intent_line(jd_keywords: dict, jd_raw: str) -> str:
    """把 JD 关键词拼进求职意向行——HR/ATS 一眼看到关键字"""
    cats = sorted(jd_keywords.keys())
    cat_zh = {
        "rag": "RAG", "agent": "Agent", "langgraph": "LangGraph", "llm": "LLM",
        "human_in_the_loop": "Human-in-the-loop", "fastapi": "FastAPI",
        "frontend": "Vue3+TS", "data": "MySQL+Redis+Qdrant", "devops": "Docker+CI",
        "eval": "评测工程", "llm_resilience": "熔断+超时预算",
        "ecommerce": "电商", "finance": "金融支付",
        "data_annotation": "数据标注", "prompt": "Prompt",
    }
    tags = [cat_zh.get(c, c) for c in cats if c in cat_zh]
    intent = "AI Agent / LLM 应用开发实习生（" + " / ".join(tags[:8]) + "）"
    return intent


def collect_missing(jd_keywords: dict, base_text: str) -> list:
    """JD 关键词在简历里没出现的（按使用频率）"""
    miss = []
    for cat, kws in jd_keywords.items():
        for kw in kws:
            if kw not in base_text and kw.lower() not in base_text.lower():
                miss.append(kw)
    return list(dict.fromkeys(miss))  # dedupe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd", help="JD 文本文件路径；不传则从 stdin 读")
    ap.add_argument("--base", default=str(DEFAULT_BASE),
                    help=f"母版简历路径（默认 {DEFAULT_BASE}）")
    ap.add_argument("--out", help="定制版输出路径；不传则打印到 stdout")
    args = ap.parse_args()

    if args.jd:
        jd_raw = Path(args.jd).read_text(encoding="utf-8")
    else:
        print(">> 把 JD 内容粘进来（Ctrl+D 结束）：", file=sys.stderr)
        jd_raw = sys.stdin.read()

    base_text = Path(args.base).read_text(encoding="utf-8")

    jd_kws = extract_keywords(jd_raw)

    # 1. 关键词 + 缺失
    print("=" * 64)
    print("【1. JD 关键词命中/缺失】")
    print("=" * 64)
    matched = {}
    missing = {}
    for cat, kws in jd_kws.items():
        hit = [kw for kw in kws if kw in base_text or kw.lower() in base_text.lower()]
        miss = [kw for kw in kws if kw not in hit]
        if hit:
            matched[cat] = hit
        if miss:
            missing[cat] = miss
    for cat, kws in matched.items():
        print(f"  命中  {cat:<20} → {', '.join(kws)}")
    if missing:
        print("\n  缺失（建议在简历里挂上）：")
        for cat, kws in missing.items():
            print(f"    {cat:<20} → {', '.join(kws[:3])}")
    else:
        print("\n  ✅ 所有 JD 关键词已在简历里")

    # 2. 子弹打分
    print("\n" + "=" * 64)
    print("【2. 简历子弹 vs JD 相关度（按分倒序）】")
    print("=" * 64)
    scored = score_bullets_against_keywords(base_text, jd_kws)
    for s, line in scored[:15]:  # top 15
        marker = "★" * min(s, 5)
        print(f"  {marker:<5} × {s}  {line[:90]}")

    # 3. 定制版
    print("\n" + "=" * 64)
    print("【3. 定制版简历（按 JD 关键词重新排序 + 缺失关键词追加）】")
    print("=" * 64)
    tailored = render_tailored_resume(base_text, jd_kws, jd_raw)
    print(tailored)

    if args.out:
        Path(args.out).write_text(tailored, encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())