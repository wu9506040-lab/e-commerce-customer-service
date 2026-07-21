"""
run_v12_validation.py - V12 多意图识别验证脚本（ECS 公网环境）

目的：
- 验证 V12 classify() 返 intents[]/primary 后，SSE 流式 chat 接口：
  1. meta.intent 字段 = primary（向后兼容）
  2. 多意图 query 的 token 流是否覆盖 secondary intent（答全率）
  3. 单意图 query 不受影响（向后兼容）
  4. classify 性能 + JSON 解析失败率

不修改业务代码（CLAUDE.md §9 强隔离）；通过 HTTP /api/chat 走真实生产路径。

用法（在 ECS 容器内）：
    docker exec customer-service-api python /tmp/v12_validation/run_v12_validation.py
或本地：
    python scripts/v12_multi_intent_validation/run_v12_validation.py --base-url http://120.79.27.124:8000
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================
# V12 测试用例：20 multi + 5 single（向后兼容 sanity）
# =============================================================
# expected_intents: 期望识别到的意图列表（至少 1 个）
# expected_primary: 期望 primary intent
# coverage_keywords: 期望 LLM 答案覆盖的关键词（multi intent 答全率核心）
MULTI_INTENT_CASES: List[Dict[str, Any]] = [
    # === refund + policy ===
    {
        "id": "m01",
        "query": "我订单 12345 要退款，但运费谁出？",
        "expected_primary": "refund_query",
        "expected_intents": ["refund_query", "policy_query"],
        "coverage_keywords": ["退款", "运费"],
        "category": "refund+policy",
    },
    {
        "id": "m02",
        "query": "7天无理由退货运费谁承担？订单 67890 我想退了",
        "expected_primary": "policy_query",
        "expected_intents": ["policy_query", "refund_query"],
        "coverage_keywords": ["7天", "运费", "退货"],
        "category": "policy+refund",
    },
    {
        "id": "m03",
        "query": "申请退款 12345，理由选质量问题还是描述不符更稳？",
        "expected_primary": "refund_query",
        "expected_intents": ["refund_query", "policy_query"],
        "coverage_keywords": ["退款", "理由"],
        "category": "refund+policy",
    },
    # === product + policy ===
    {
        "id": "m04",
        "query": "这台笔记本续航怎么样？能分期付款吗？",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "policy_query"],
        "coverage_keywords": ["续航", "分期"],
        "category": "product+policy",
    },
    {
        "id": "m05",
        "query": "iPhone 15 拍照效果如何？保修期多久？",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "policy_query"],
        "coverage_keywords": ["拍照", "保修"],
        "category": "product+policy",
    },
    {
        "id": "m06",
        "query": "支持花呗分期吗？最高 24 期免息有没有？",
        "expected_primary": "policy_query",
        "expected_intents": ["policy_query"],
        "coverage_keywords": ["花呗", "分期"],
        "category": "policy",
    },
    # === order + policy ===
    {
        "id": "m07",
        "query": "我的订单 12345 发货了吗？一般几天到货？",
        "expected_primary": "order_query",
        "expected_intents": ["order_query", "policy_query"],
        "coverage_keywords": ["发货", "到货"],
        "category": "order+policy",
    },
    {
        "id": "m08",
        "query": "订单 88888 还没收到，怎么办？超时能赔吗？",
        "expected_primary": "order_query",
        "expected_intents": ["order_query", "policy_query"],
        "coverage_keywords": ["订单", "超时"],
        "category": "order+policy",
    },
    # === order + refund ===
    {
        "id": "m09",
        "query": "订单 12345 我想退了，已经发货了怎么操作？",
        "expected_primary": "refund_query",
        "expected_intents": ["refund_query", "order_query"],
        "coverage_keywords": ["订单", "退货"],
        "category": "refund+order",
    },
    {
        "id": "m10",
        "query": "退款进度怎么查？订单 67890 已申请 3 天",
        "expected_primary": "refund_query",
        "expected_intents": ["refund_query", "order_query"],
        "coverage_keywords": ["退款", "进度"],
        "category": "refund+order",
    },
    # === product + order ===
    {
        "id": "m11",
        "query": "这个键盘我订单 55566 买过，怎么再买一个？",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "order_query"],
        "coverage_keywords": ["键盘", "订单"],
        "category": "product+order",
    },
    {
        "id": "m12",
        "query": "我想看下手机，订单 77777 那台还在卖吗？",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "order_query"],
        "coverage_keywords": ["手机"],
        "category": "product+order",
    },
    # === 三意图（policy 主） ===
    {
        "id": "m13",
        "query": "订单 12345 退款运费怎么算？7天无理由和质量问题有区别吗？",
        "expected_primary": "policy_query",
        "expected_intents": ["policy_query", "refund_query"],
        "coverage_keywords": ["运费", "7天", "质量"],
        "category": "policy+refund",
    },
    {
        "id": "m14",
        "query": "退货和换货政策分别是什么？运费补贴呢？",
        "expected_primary": "policy_query",
        "expected_intents": ["policy_query"],
        "coverage_keywords": ["退货", "换货", "运费"],
        "category": "policy",
    },
    {
        "id": "m15",
        "query": "电脑续航怎样？订单 99999 我想换一台续航好的",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "order_query"],
        "coverage_keywords": ["续航", "换"],
        "category": "product+order",
    },
    # === 三意图边界 ===
    {
        "id": "m16",
        "query": "iPhone 续航够用吗？分期免息怎么申请？订单 11111 还在吗？",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "policy_query"],
        "coverage_keywords": ["续航", "分期"],
        "category": "product+policy+order",
    },
    {
        "id": "m17",
        "query": "笔记本保修多久？我订单 22222 的发票丢了能补吗？",
        "expected_primary": "product_query",
        "expected_intents": ["product_query", "policy_query"],
        "coverage_keywords": ["保修", "发票"],
        "category": "product+policy",
    },
    {
        "id": "m18",
        "query": "订单 33333 怎么取消？取消后钱多久到账？",
        "expected_primary": "order_query",
        "expected_intents": ["order_query"],
        "coverage_keywords": ["取消", "到账"],
        "category": "order+refund",
    },
    {
        "id": "m19",
        "query": "运费险怎么用？订单 44444 没买运费险还能退吗？",
        "expected_primary": "policy_query",
        "expected_intents": ["policy_query", "refund_query"],
        "coverage_keywords": ["运费险"],
        "category": "policy+refund",
    },
    {
        "id": "m20",
        "query": "商品质量有问题怎么办？订单 55555 能换新吗？",
        "expected_primary": "policy_query",
        "expected_intents": ["policy_query", "refund_query"],
        "coverage_keywords": ["质量", "换"],
        "category": "policy+refund",
    },
]

# 单意图 sanity（V12 不应破坏 V11 单意图行为）
SINGLE_INTENT_CASES: List[Dict[str, Any]] = [
    {
        "id": "s01",
        "query": "我的订单 12345 退款进度",
        "expected_primary": "refund_query",
        "coverage_keywords": ["退款"],
    },
    {
        "id": "s02",
        "query": "7天无理由退货政策",
        "expected_primary": "policy_query",
        "coverage_keywords": ["退货"],
    },
    {
        "id": "s03",
        "query": "iPhone 15 拍照效果",
        "expected_primary": "product_query",
        "coverage_keywords": ["拍照"],
    },
    {
        "id": "s04",
        "query": "订单 99999 发货了吗",
        "expected_primary": "order_query",
        "coverage_keywords": ["发货"],
    },
    {
        "id": "s05",
        "query": "运费险怎么退",
        "expected_primary": "policy_query",
        "coverage_keywords": ["运费险"],
    },
]


# =============================================================
# HTTP 客户端（带 Cookie + SSE 流解析）
# =============================================================
class ChatClient:
    """V12 验证 HTTP 客户端：login + SSE chat。"""

    def __init__(self, base_url: str, username: str = "demotest", password: str = "demotest123"):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def login(self) -> None:
        login_url = f"{self.base_url}/api/auth/login"
        login_data = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
        }).encode("utf-8")
        req = urllib.request.Request(
            login_url, data=login_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.opener.open(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            cookies = [c.name for c in self.cookie_jar]
            if "cs_token" not in cookies:
                raise RuntimeError(
                    f"登录响应未下发 cs_token Cookie（cookies={cookies}, body={body[:200]}）"
                )
            logger.info(f"  login 成功（cookie: {', '.join(cookies)}）")

    def chat_sse(self, query: str, session_id: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
        """POST /api/chat (SSE) → 返回 {meta, tokens, full_text, duration_ms, status}"""
        url = f"{self.base_url}/api/chat"
        payload = {"query": query, "stream": True}
        if session_id:
            payload["session_id"] = session_id
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = time.time()
        meta: Optional[Dict[str, Any]] = None
        tokens: List[str] = []
        done_session: Optional[str] = None
        closed = False
        error_msg: Optional[str] = None
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                # 逐行读 SSE
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    try:
                        ev = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    ev_type = ev.get("type")
                    if ev_type == "meta":
                        meta = ev
                    elif ev_type == "token":
                        tokens.append(ev.get("text", ""))
                    elif ev_type == "done":
                        done_session = ev.get("session_id")
                    elif ev_type == "closed":
                        closed = True
                    elif ev_type == "error":
                        error_msg = ev.get("message") or ev.get("error")
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
        duration_ms = round((time.time() - start) * 1000)
        full_text = "".join(tokens)
        return {
            "meta": meta,
            "tokens": tokens,
            "full_text": full_text,
            "session_id": done_session,
            "closed": closed,
            "error": error_msg,
            "duration_ms": duration_ms,
        }


# =============================================================
# 评分函数
# =============================================================
def _coverage(text: str, keywords: List[str]) -> Tuple[float, List[str]]:
    """关键词覆盖率：命中的关键词 / 全部关键词"""
    if not keywords:
        return 1.0, []
    hit = [k for k in keywords if k in text]
    return len(hit) / len(keywords), hit


def _score_multi(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """多意图 case 评分"""
    meta = result.get("meta") or {}
    full_text = result.get("full_text") or ""
    meta_intent = meta.get("intent")
    expected_primary = case["expected_primary"]
    coverage_ratio, hit_keywords = _coverage(full_text, case["coverage_keywords"])

    primary_match = meta_intent == expected_primary
    return {
        "id": case["id"],
        "query": case["query"],
        "category": case.get("category", ""),
        "expected_primary": expected_primary,
        "meta_intent": meta_intent,
        "primary_match": primary_match,
        "coverage_ratio": round(coverage_ratio, 3),
        "coverage_hit": hit_keywords,
        "expected_coverage": case["coverage_keywords"],
        "duration_ms": result["duration_ms"],
        "error": result["error"],
        "intent_method": meta.get("intent_method"),
        "meta_keys": sorted(meta.keys()) if meta else [],
    }


def _score_single(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """单意图 case 评分（向后兼容 sanity）"""
    meta = result.get("meta") or {}
    full_text = result.get("full_text") or ""
    meta_intent = meta.get("intent")
    expected_primary = case["expected_primary"]
    coverage_ratio, hit_keywords = _coverage(full_text, case["coverage_keywords"])

    primary_match = meta_intent == expected_primary
    return {
        "id": case["id"],
        "query": case["query"],
        "expected_primary": expected_primary,
        "meta_intent": meta_intent,
        "primary_match": primary_match,
        "coverage_ratio": round(coverage_ratio, 3),
        "coverage_hit": hit_keywords,
        "duration_ms": result["duration_ms"],
        "error": result["error"],
        "intent_method": meta.get("intent_method"),
    }


# =============================================================
# 汇总统计
# =============================================================
def summarize(scores: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    n = len(scores)
    if n == 0:
        return {
            "label": label,
            "total": 0,
            "primary_correct": 0,
            "primary_accuracy": 0.0,
            "no_error": 0,
            "no_error_rate": 0.0,
            "avg_coverage": 0.0,
            "avg_duration_ms": 0,
            "p95_duration_ms": 0,
        }

    primary_correct = sum(1 for s in scores if s.get("primary_match"))
    no_error = sum(1 for s in scores if not s.get("error"))
    avg_coverage = sum(s.get("coverage_ratio", 0) for s in scores) / n
    avg_duration = sum(s.get("duration_ms", 0) for s in scores) / n
    p95_duration = sorted(s.get("duration_ms", 0) for s in scores)[int(n * 0.95)] if n > 0 else 0
    return {
        "label": label,
        "total": n,
        "primary_correct": primary_correct,
        "primary_accuracy": round(primary_correct / n, 3),
        "no_error": no_error,
        "no_error_rate": round(no_error / n, 3),
        "avg_coverage": round(avg_coverage, 3),
        "avg_duration_ms": round(avg_duration, 1),
        "p95_duration_ms": p95_duration,
    }


# =============================================================
# 主流程
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="V12 多意图识别 · ECS 验证脚本")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--username", default="demotest")
    parser.add_argument("--password", default="demotest123")
    parser.add_argument("--out-dir", default="/tmp/v12_validation",
                        help="输出目录（JSON + Markdown）")
    parser.add_argument("--only", choices=["multi", "single", "all"], default="all")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"V12 验证开始 · base_url={args.base_url} · out={out_dir}")

    client = ChatClient(args.base_url, args.username, args.password)
    client.login()

    multi_scores: List[Dict[str, Any]] = []
    single_scores: List[Dict[str, Any]] = []

    cases_to_run: List[Tuple[str, Dict[str, Any]]] = []
    if args.only in ("multi", "all"):
        cases_to_run.extend([("multi", c) for c in MULTI_INTENT_CASES])
    if args.only in ("single", "all"):
        cases_to_run.extend([("single", c) for c in SINGLE_INTENT_CASES])

    for kind, case in cases_to_run:
        logger.info(f"[{kind}] {case['id']} · {case['query'][:30]}...")
        result = client.chat_sse(case["query"])
        if kind == "multi":
            score = _score_multi(case, result)
            multi_scores.append(score)
            logger.info(
                f"  → meta.intent={score['meta_intent']} (期望 {score['expected_primary']}) "
                f"match={score['primary_match']} coverage={score['coverage_ratio']} "
                f"hit={score['coverage_hit']} dur={score['duration_ms']}ms"
                f"{' ERR='+score['error'] if score['error'] else ''}"
            )
        else:
            score = _score_single(case, result)
            single_scores.append(score)
            logger.info(
                f"  → meta.intent={score['meta_intent']} (期望 {score['expected_primary']}) "
                f"match={score['primary_match']} coverage={score['coverage_ratio']} "
                f"dur={score['duration_ms']}ms"
                f"{' ERR='+score['error'] if score['error'] else ''}"
            )

    multi_summary = summarize(multi_scores, "V12 multi-intent (20 cases)")
    single_summary = summarize(single_scores, "V12 single-intent sanity (5 cases)")

    # 输出 raw + report
    raw = {
        "multi_scores": multi_scores,
        "single_scores": single_scores,
        "multi_summary": multi_summary,
        "single_summary": single_summary,
    }
    (out_dir / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Markdown 报告
    report_lines: List[str] = []
    report_lines.append("# V12 多意图识别 · ECS 验证报告\n")
    report_lines.append(f"base_url: `{args.base_url}`\n")
    report_lines.append(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    report_lines.append("## 1. 多意图 20 case 汇总\n")
    report_lines.append(
        f"| 指标 | 数值 |\n|------|------|\n"
        f"| primary 匹配率 | **{multi_summary['primary_accuracy']}** ({multi_summary['primary_correct']}/{multi_summary['total']}) |\n"
        f"| 无错误率 | {multi_summary['no_error_rate']} ({multi_summary['no_error']}/{multi_summary['total']}) |\n"
        f"| 平均覆盖率 | {multi_summary['avg_coverage']} |\n"
        f"| 平均延迟 | {multi_summary['avg_duration_ms']} ms |\n"
        f"| P95 延迟 | {multi_summary['p95_duration_ms']} ms |\n\n"
    )

    report_lines.append("## 2. 单意图 5 case 向后兼容 sanity\n")
    report_lines.append(
        f"| 指标 | 数值 |\n|------|------|\n"
        f"| primary 匹配率 | **{single_summary['primary_accuracy']}** ({single_summary['primary_correct']}/{single_summary['total']}) |\n"
        f"| 无错误率 | {single_summary['no_error_rate']} ({single_summary['no_error']}/{single_summary['total']}) |\n"
        f"| 平均覆盖率 | {single_summary['avg_coverage']} |\n"
        f"| 平均延迟 | {single_summary['avg_duration_ms']} ms |\n\n"
    )

    report_lines.append("## 3. 多意图 20 case 明细\n")
    report_lines.append(
        "| ID | query | 期望 | 实际 | match | coverage | hit | dur(ms) |\n"
        "|----|-------|------|------|-------|----------|-----|---------|\n"
    )
    for s in multi_scores:
        report_lines.append(
            f"| {s['id']} | {s['query'][:24]}... | {s['expected_primary']} | "
            f"{s['meta_intent']} | {'✅' if s['primary_match'] else '❌'} | "
            f"{s['coverage_ratio']} | {','.join(s['coverage_hit'])} | {s['duration_ms']} |\n"
        )

    failed = [s for s in multi_scores if not s["primary_match"] or s["error"]]
    report_lines.append(f"\n## 4. 失败 case ({len(failed)}/{multi_summary['total']})\n")
    if failed:
        for s in failed:
            report_lines.append(
                f"- **{s['id']}** {s['query']}\n"
                f"  - 期望 primary: {s['expected_primary']}, 实际: {s['meta_intent']}\n"
                f"  - coverage: {s['coverage_ratio']} (hit={s['coverage_hit']})\n"
                f"  - error: {s['error'] or '无'}\n"
            )
    else:
        report_lines.append("无失败 case\n")

    (out_dir / "report.md").write_text("".join(report_lines), encoding="utf-8")

    logger.info(f"\n=== V12 验证汇总 ===")
    logger.info(f"多意图 20 case: primary 准确率 {multi_summary['primary_accuracy']} · "
                f"覆盖率 {multi_summary['avg_coverage']} · P95 延迟 {multi_summary['p95_duration_ms']}ms")
    logger.info(f"单意图 5 case: primary 准确率 {single_summary['primary_accuracy']} · "
                f"覆盖率 {single_summary['avg_coverage']} · P95 延迟 {single_summary['p95_duration_ms']}ms")
    logger.info(f"报告输出: {out_dir}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())