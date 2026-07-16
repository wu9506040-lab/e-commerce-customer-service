"""
eval_agent_fc.py - Agent Function Calling 决策质量评估

C3：评测 Agent FC（C2）在真实/模拟场景下的决策质量。

评测 4 类指标：
- tool_selection_accuracy：LLM 选的工具是否在期望集合里（集合匹配）
- tool_round_efficiency：actual_rounds / optimal_rounds（越接近 1 越高效）
- answer_keyword_match：最终答案中 expected_keywords 命中率
- hallucination_free：最终答案不出现 sensitive_keywords

双模式（CLAUDE.md §9.12 例外条款：一次性脚本不强求接口化）：
- --mock（CI / debug 用）：mock LLM + mock tools/registry.dispatch
- --live（默认 · 手动）：调真实 /api/chat SSE 流式接口

设计取舍：
- 评测集 JSON 是 local artifact（.gitignore data/），与 B1 一致
- 规则路径（80%）+ mini-judge 兜底（20%）—— 跟 B1 faithfulness 经验一致
- 顺序执行（避免 LLM Provider 限流 + 真实 SSE 并发控制）

用法：
    # Mock 模式（CI 友好，验证评测逻辑）
    PYTHONPATH=backend python scripts/eval_agent_fc.py --mode mock

    # Live 模式（灰度前手动跑一次，验证真实决策质量）
    PYTHONPATH=backend python scripts/eval_agent_fc.py --mode live
"""
import argparse
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def _load_env() -> None:
    """加载 .env（仅脚本入口调用，禁止 import 时执行）。

    Why：`.env.dev` 含 USE_LANGGRAPH_REFUND=true 等业务开关；若在 import 期
    load_dotenv，pytest collection 导入本模块（test_eval_agent_fc）时会污染
    os.environ，导致 settings 单例首次加载读到脏值，令无关测试（如 refund V2/V3
    分派）随收集顺序偶发 fail。故只在 main() 里加载。
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    for env_file in [
        BACKEND_DIR / ".env",
        PROJECT_ROOT / "deploy" / ".env.dev",
        PROJECT_ROOT / ".env",
    ]:
        if env_file.exists():
            load_dotenv(env_file)
            break


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_INPUT = PROJECT_ROOT / "data" / "eval_agent_set.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "eval_agent_fc_report.json"


# =============================================================
# 1. 加载评测集
# =============================================================
def load_eval_set(path: Path) -> List[Dict[str, Any]]:
    """加载评测集 JSON，校验必填字段。

    每条 case 必填：query / expected_tools / expected_answer_keywords / category
    可选：sensitive_keywords / expected_rounds / note / user_id / order_no
    """
    if not path.exists():
        raise FileNotFoundError(f"评测集不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"评测集格式错误，期望 list，实际 {type(data)}")
    required = {"query", "expected_tools", "expected_answer_keywords", "category"}
    for i, item in enumerate(data):
        missing = required - set(item.keys())
        if missing:
            raise ValueError(f"评测集第 {i} 条缺字段: {missing}, item={item}")
        if not isinstance(item["expected_tools"], list):
            raise ValueError(
                f"评测集第 {i} 条 expected_tools 必须是 list: item={item}"
            )
    return data


# =============================================================
# 2. 4 类指标计算（rule path）
# =============================================================
def compute_tool_selection_accuracy(actual_tools: List[str], expected: List[Dict]) -> float:
    """tool_selection_accuracy：实际工具调用序列是否覆盖期望。

    评测方法：
    - 提取 expected 中所有 tool name 集合
    - 提取 actual 中所有 tool name 集合（保持顺序）
    - 分数 = |actual ∩ expected| / |expected|
       （期望集合都被调 = 1；只调了一半 = 0.5）
    - 额外的 bonus（不计入）：如果实际顺序与期望顺序一致 +0.1（保留 1 位小数）

    Returns:
        0.0 ~ 1.0
    """
    expected_names = [t["name"] for t in expected]
    expected_set = set(expected_names)
    actual_set = set(actual_tools)
    if not expected_set:
        return 1.0  # 期望为空（direct case）= 满分
    return len(actual_set & expected_set) / len(expected_set)


def compute_tool_round_efficiency(actual_rounds: int, expected_rounds: int) -> float:
    """tool_round_efficiency：actual / expected 的比值，1.0 最优。

    实际轮次 <= 期望：返 1.0（高效）
    实际轮次 > 期望：返 expected / actual（惩罚冗余调用）
    expected_rounds <= 0：返 1.0（无期望值时不评测）
    """
    if expected_rounds <= 0:
        return 1.0
    if actual_rounds <= expected_rounds:
        return 1.0
    return round(expected_rounds / actual_rounds, 3)


def compute_answer_keyword_match(answer: str, expected_keywords: List[str]) -> float:
    """answer_keyword_match：expected_keywords 中被答案覆盖的比例。

    评测方法：substring 匹配（不依赖 LLM）

    Returns:
        0.0 ~ 1.0
    """
    if not expected_keywords:
        return 1.0  # 无期望关键词 = 满分（避免除零）
    if not answer:
        return 0.0
    hits = sum(1 for kw in expected_keywords if kw in answer)
    return round(hits / len(expected_keywords), 3)


def compute_hallucination_free(answer: str, sensitive_keywords: List[str]) -> bool:
    """hallucination_free：sensitive_keywords 都不出现。

    Returns:
        True（无幻觉）/ False（有幻觉）
    """
    if not sensitive_keywords:
        return True
    if not answer:
        return True
    return not any(kw in answer for kw in sensitive_keywords)


# =============================================================
# 3. mini-judge 兜底（B1 faithfulness 同款）
# =============================================================
def mini_judge(answer: str, expected_keywords: List[str], sensitive_keywords: List[str]) -> Dict:
    """当答案 < 10 字 或 expected_keywords 空时触发 mini-judge。

    LLM 调用：max_tokens=5, temperature=0
    输出 1/0 二值（1=pass，0=fail）

    注：mock 模式下不调 LLM，直接返 fallback（避免 CI 依赖 API key）。
    """
    # mock 模式（或 LLM API key 缺失）：fallback 到严格规则
    if os.environ.get("EVAL_AGENT_FC_MOCK") or not os.environ.get("QWEN_API_KEY"):
        # fallback：答案非空 + 不含敏感词 = pass
        if not answer:
            return {"judge": 0, "reason": "empty answer (fallback)"}
        if any(kw in answer for kw in sensitive_keywords):
            return {"judge": 0, "reason": "sensitive keyword found (fallback)"}
        return {"judge": 1, "reason": "fallback rule pass"}

    # 真实 mini-judge（B1 faithfulness 同款实现，简化版）
    try:
        from app.core.providers.llm import get_llm_provider

        sys_prompt = (
            "判断答案是否满足期望。\n"
            f"期望关键词: {expected_keywords}\n"
            f"敏感词（不能出现）: {sensitive_keywords}\n"
            f"答案: {answer}\n"
            "输出 1 (满足) 或 0 (不满足)，不要输出其他内容。"
        )
        resp = get_llm_provider().chat(
            messages=[{"role": "user", "content": sys_prompt}],
            temperature=0.0,
            max_tokens=5,
        )
        judge_text = (resp.get("reply") or "").strip()
        judge = 1 if "1" in judge_text else 0
        return {"judge": judge, "reason": f"mini-judge: {judge_text[:30]}"}
    except Exception as e:
        logger.warning(f"mini-judge 失败，fallback 到规则: {e}")
        return {"judge": 1 if answer and not any(kw in answer for kw in sensitive_keywords) else 0,
                "reason": f"fallback after error: {e}"}


# =============================================================
# 4. Mock 模式评测（CI 友好）
# =============================================================
def evaluate_case_mock(case: Dict[str, Any]) -> Dict[str, Any]:
    """Mock 模式：mock LLM + mock dispatch，验证 agent_runner 逻辑。

    流程：
    1. mock LLM 按 expected_tools 顺序生成 tool_calls（模拟 LLM 决策）
    2. mock dispatch 返 fake_result
    3. 调真实 agent_runner.run_stream_agent（开启 ENABLE_AGENT_FC）
    4. 提取 tool_calls + done.answer
    5. 计算 4 类指标
    """
    # 在 import 之前设置 mock 环境（settings 是单例，agent_runner 内部读）
    os.environ["EVAL_AGENT_FC_MOCK"] = "1"
    # 注意：不要设 os.environ["ENABLE_AGENT_FC"]——agent_runner/orchestrator 读的是
    # settings 对象属性（下方直接覆盖），设 env 会污染 settings 首次加载值，
    # 导致 finally 恢复的"原值"是 True（破坏同进程后续测试）。
    # mock 模式不连真实 DB / 不签发 token，补占位值通过 config 启动校验
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault(
        "DATABASE_URL",
        "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4",
    )

    try:
        from app.core.config import settings  # noqa: F401 触发 settings 加载
        from app.services.chat.agent_runner import run_stream_agent  # noqa: E401
        from app.tools.registry import REGISTRY  # noqa: E401
    except Exception as e:
        logger.warning(f"agent_runner / registry 加载失败: {e}")
        return {"category": case.get("category"), "skipped": True, "reason": str(e)}

    # 直接覆盖 settings（不依赖 reload）——保存原值，finally 恢复，避免污染全局单例
    _orig_enable_fc = settings.ENABLE_AGENT_FC
    _orig_max_turns = settings.MAX_AGENT_TURNS
    settings.ENABLE_AGENT_FC = True
    settings.MAX_AGENT_TURNS = 5

    # 构造 mock LLM：按 expected_tools 顺序生成 tool_calls，最后 1 轮返 answer
    expected = case.get("expected_tools", [])
    mock_llm = MagicMock()

    def mock_chat_side_effect(messages, tools=None, tool_choice=None, **kwargs):
        # 统计已发生几轮 assistant tool_calls 消息
        assistant_tool_calls_count = sum(
            1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
        )
        if assistant_tool_calls_count < len(expected):
            # 还没调完，按期望顺序返下一个 tool_call
            tc = expected[assistant_tool_calls_count]
            tc_dict = {
                "id": f"call_{assistant_tool_calls_count + 1:03d}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc.get("arguments_contains", {})),
                },
            }
            return {
                "reply": None,
                "tool_calls": [tc_dict],
                "model": "mock",
                "usage": {},
            }
        else:
            # 调完所有工具，返最终答案
            fake_answer = "，".join(case.get("expected_answer_keywords", ["默认答案"]))
            return {
                "reply": fake_answer,
                "tool_calls": None,
                "model": "mock",
                "usage": {},
            }

    mock_llm.chat.side_effect = mock_chat_side_effect

    # mock dispatch（不真跑 OrderTool/ProductTool/PolicyService）
    captured_dispatch_calls = []

    def mock_dispatch(name, arguments_json, ctx):
        captured_dispatch_calls.append({
            "name": name,
            "arguments": arguments_json,
            "ctx_user_id": ctx.user_id if ctx else None,
        })
        return {"ok": True, "name": name, "args": arguments_json}

    # 替换模块级 dispatch + get_llm_provider
    with MagicMock() as _:
        # patch 在 agent_runner 命名空间内的引用（不是 registry 命名空间）
        import app.services.chat.agent_runner as ar_mod
        original_dispatch = ar_mod.dispatch
        original_get_llm_provider = ar_mod.get_llm_provider
        ar_mod.dispatch = mock_dispatch
        ar_mod.get_llm_provider = lambda: mock_llm
        try:
            t0 = time.perf_counter()
            events = []
            done_answer = ""
            actual_tools = []
            for event_type, data in run_stream_agent(
                query=case["query"],
                user_id=case.get("user_id", 1),
                history=None,
            ):
                events.append((event_type, data))
                if event_type == "meta" and "tool_call" in data:
                    actual_tools.append(data["tool_call"]["name"])
                elif event_type == "done":
                    done_answer = data.get("answer", "")
            elapsed_ms = (time.perf_counter() - t0) * 1000
        finally:
            ar_mod.dispatch = original_dispatch
            ar_mod.get_llm_provider = original_get_llm_provider
            # 恢复 settings 全局单例，避免污染同进程后续测试（pytest import 复用）
            settings.ENABLE_AGENT_FC = _orig_enable_fc
            settings.MAX_AGENT_TURNS = _orig_max_turns

    # 计算 4 类指标
    actual_rounds = len(actual_tools)
    expected_rounds = case.get("expected_rounds", len(expected)) if expected else 0

    metrics = {
        "tool_selection_accuracy": compute_tool_selection_accuracy(actual_tools, expected),
        "tool_round_efficiency": compute_tool_round_efficiency(actual_rounds, expected_rounds),
        "answer_keyword_match": compute_answer_keyword_match(done_answer, case["expected_answer_keywords"]),
        "hallucination_free": compute_hallucination_free(done_answer, case.get("sensitive_keywords", [])),
    }

    # mini-judge 兜底（答案 < 10 字或 expected_keywords 空时）
    if len(done_answer) < 10 or not case["expected_answer_keywords"]:
        judge = mini_judge(
            done_answer,
            case["expected_answer_keywords"],
            case.get("sensitive_keywords", []),
        )
        metrics["answer_keyword_match"] = float(judge["judge"])
        metrics["mini_judge_reason"] = judge["reason"]

    return {
        "category": case.get("category"),
        "query": case["query"],
        "expected_tools": [t["name"] for t in expected],
        "actual_tools": actual_tools,
        "actual_rounds": actual_rounds,
        "expected_rounds": expected_rounds,
        "done_answer": done_answer,
        "metrics": metrics,
        "elapsed_ms": round(elapsed_ms, 2),
        "mode": "mock",
        "note": case.get("note", ""),
    }


# =============================================================
# 5. Live 模式评测（手动跑）
# =============================================================
def evaluate_case_live(
    case: Dict[str, Any], base_url: str, opener: "urllib.request.OpenerDirector"
) -> Dict[str, Any]:
    """Live 模式：调真实 /api/chat SSE 流式接口，提取 tool_call + answer。

    注：本函数需要 server 在跑（本地或 ECS）；opener 由 main() 用 demotest
    账号登录后构造（httpOnly Cookie 鉴权）。
    """
    import urllib.request
    import urllib.error

    # P0 修复点 1：实际 SSE 端点是 POST /api/chat（不是 /api/chat/stream）
    # P0 修复点 2：鉴权走 httpOnly Cookie（不是 Authorization: Bearer）
    url = f"{base_url}/api/chat"
    payload = {
        "query": case["query"],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    actual_tools = []
    done_answer = ""
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with opener.open(req, timeout=60) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                data = event.get("data", {})
                if event_type == "meta" and "tool_call" in data:
                    actual_tools.append(data["tool_call"]["name"])
                elif event_type == "done":
                    done_answer = data.get("answer", "")
        elapsed_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        logger.warning(f"live 模式请求失败: query={case['query']}, err={e}")
        return {"category": case.get("category"), "query": case["query"], "error": str(e), "mode": "live"}

    expected = case.get("expected_tools", [])
    actual_rounds = len(actual_tools)
    expected_rounds = case.get("expected_rounds", len(expected)) if expected else 0
    metrics = {
        "tool_selection_accuracy": compute_tool_selection_accuracy(actual_tools, expected),
        "tool_round_efficiency": compute_tool_round_efficiency(actual_rounds, expected_rounds),
        "answer_keyword_match": compute_answer_keyword_match(done_answer, case["expected_answer_keywords"]),
        "hallucination_free": compute_hallucination_free(done_answer, case.get("sensitive_keywords", [])),
    }
    return {
        "category": case.get("category"),
        "query": case["query"],
        "expected_tools": [t["name"] for t in expected],
        "actual_tools": actual_tools,
        "actual_rounds": actual_rounds,
        "expected_rounds": expected_rounds,
        "done_answer": done_answer,
        "metrics": metrics,
        "elapsed_ms": round(elapsed_ms, 2),
        "mode": "live",
        "note": case.get("note", ""),
    }


# =============================================================
# 5.5 Live 模式登录：构造带 httpOnly Cookie 的 OpenerDirector
# =============================================================
def _build_session_opener(base_url: str) -> "urllib.request.OpenerDirector":
    """P0 修复：login 改 form-urlencoded（OAuth2PasswordRequestForm）+ Cookie 鉴权。

    后端实际接口契约（见 backend/app/api/auth.py:79）：
    - POST /api/auth/login 用 OAuth2PasswordRequestForm（application/x-www-form-urlencoded）
    - token 通过 Set-Cookie 下发（httpOnly），响应 body 不含 token
    - chat 接口鉴权靠 Cookie（见 backend/app/api/chat.py:119 描述）

    返回的 opener 自带 HTTPCookieProcessor，open() 会自动携带 Cookie。
    """
    import urllib.request
    import urllib.parse
    import http.cookiejar

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    login_url = f"{base_url}/api/auth/login"
    login_data = urllib.parse.urlencode({
        "username": "demotest",
        "password": "demotest123",
    }).encode("utf-8")
    req = urllib.request.Request(
        login_url, data=login_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with opener.open(req, timeout=10) as resp:
        # 验证：响应 body 不含 token，但 Set-Cookie 必须有 cs_token
        body = resp.read().decode("utf-8")
        cookies = [c.name for c in cookie_jar]
        if "cs_token" not in cookies:
            raise RuntimeError(
                f"登录响应未下发 cs_token Cookie（cookies={cookies}, body={body[:200]}）"
            )
        logger.info(f"  自动登录 demotest 成功（cookie: {', '.join(cookies)}）")
    return opener


# =============================================================
# 6. 汇总统计
# =============================================================
def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}

    # 过滤掉 error 的 case
    valid = [r for r in results if "metrics" in r]
    valid_n = len(valid)
    if valid_n == 0:
        return {"total": n, "valid": 0, "errors": n}

    # 4 类指标的均值
    avg_metrics = {
        k: round(sum(r["metrics"][k] for r in valid if isinstance(r["metrics"][k], (int, float))) / valid_n, 3)
        for k in ("tool_selection_accuracy", "tool_round_efficiency", "answer_keyword_match")
    }
    # hallucination_free 是 bool，转 0/1 再求和
    hallucination_free_rate = round(
        sum(1 for r in valid if r["metrics"]["hallucination_free"]) / valid_n, 3
    )
    avg_metrics["hallucination_free_rate"] = hallucination_free_rate

    # 按 category 分组
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for r in valid:
        by_category.setdefault(r.get("category", "unknown"), []).append(r)

    category_stats = {}
    for cat, items in by_category.items():
        m = len(items)
        category_stats[cat] = {
            "count": m,
            "tool_selection_accuracy": round(
                sum(i["metrics"]["tool_selection_accuracy"] for i in items) / m, 3),
            "answer_keyword_match": round(
                sum(i["metrics"]["answer_keyword_match"] for i in items) / m, 3),
            "hallucination_free_rate": round(
                sum(1 for i in items if i["metrics"]["hallucination_free"]) / m, 3),
        }

    # 失败案例（任意指标 < 0.5 或有幻觉）
    failures = [
        {
            "category": r.get("category"),
            "query": r.get("query"),
            "expected_tools": r.get("expected_tools"),
            "actual_tools": r.get("actual_tools"),
            "metrics": r.get("metrics"),
            "done_answer_preview": (r.get("done_answer") or "")[:100],
        }
        for r in valid
        if r["metrics"]["tool_selection_accuracy"] < 0.5
        or r["metrics"]["answer_keyword_match"] < 0.5
        or not r["metrics"]["hallucination_free"]
    ]

    latencies = [r["elapsed_ms"] for r in valid if "elapsed_ms" in r]

    return {
        "total": n,
        "valid": valid_n,
        "errors": n - valid_n,
        "avg_metrics": avg_metrics,
        "by_category": category_stats,
        "failures": failures[:10],  # 最多列 10 个
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else 0,
            "p90": round(sorted(latencies)[int(len(latencies) * 0.9)], 1) if latencies else 0,
        },
    }


# =============================================================
# 7. 打印报告
# =============================================================
def print_report(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("Agent FC 决策质量评估报告")
    print("=" * 70)
    print(f"  评测集大小:   {summary['total']} 条 (有效 {summary['valid']} · 错误 {summary['errors']})")
    if not summary.get("avg_metrics"):
        print("  无有效评测结果（全部 skipped / error），请检查环境配置或评测集。")
        print("=" * 70)
        return
    print()
    print("  全局指标:")
    for k, v in summary["avg_metrics"].items():
        print(f"    {k}: {v}")
    print()
    print("  按 category 分组:")
    print(f"    {'category':<20} {'n':>4}  {'tool_sel':>8}  {'kw_match':>8}  {'hal_free':>8}")
    print("    " + "-" * 55)
    for cat, stat in sorted(summary["by_category"].items(), key=lambda x: -x[1]["count"]):
        print(f"    {cat:<20} {stat['count']:>4}  {stat['tool_selection_accuracy']:>8.3f}  "
              f"{stat['answer_keyword_match']:>8.3f}  {stat['hallucination_free_rate']:>8.3f}")
    print()
    print("  检索时延（ms）:")
    print(f"    p50: {summary['latency_ms']['p50']}")
    print(f"    p90: {summary['latency_ms']['p90']}")
    print()
    if summary["failures"]:
        print(f"  失败案例（前 {len(summary['failures'])} 条）:")
        for f in summary["failures"][:5]:
            print(f"    [{f['category']}] {f['query']}")
            print(f"       expected: {f['expected_tools']}")
            print(f"       actual:   {f['actual_tools']}")
            print(f"       metrics:  {f['metrics']}")
            if f["done_answer_preview"]:
                print(f"       answer:   {f['done_answer_preview']}...")
    print("=" * 70)


# =============================================================
# 8. 主流程
# =============================================================
def main() -> int:
    _load_env()  # 仅入口加载 .env，避免 import 期污染 os.environ（见 _load_env 说明）
    parser = argparse.ArgumentParser(description="Agent FC 决策质量评估")
    parser.add_argument("--mode", choices=["mock", "live"], default="live",
                        help="mock=CI 模式（mock LLM + mock dispatch）; live=手动模式（调真实 API）")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="评测集 JSON 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="详细结果输出")
    parser.add_argument("--base-url", default="http://localhost:8000", help="live 模式 API base URL")
    args = parser.parse_args()

    logger.info(f"加载评测集: {args.input} (mode={args.mode})")
    eval_set = load_eval_set(args.input)
    logger.info(f"  共 {len(eval_set)} 条")

    # live 模式：自动登录拿 cookie（如未提供 cookie 文件）
    opener: Optional["urllib.request.OpenerDirector"] = None
    if args.mode == "live":
        try:
            opener = _build_session_opener(args.base_url)
        except Exception as e:
            logger.error(f"自动登录失败: {e}")
            return 1

    logger.info(f"开始评估（mode={args.mode}）")
    results: List[Dict[str, Any]] = []
    for i, case in enumerate(eval_set, 1):
        try:
            if args.mode == "mock":
                r = evaluate_case_mock(case)
            else:
                r = evaluate_case_live(case, args.base_url, opener)
            results.append(r)
        except Exception as e:
            logger.error(f"[{i}/{len(eval_set)}] query='{case.get('query', '')}' 评估失败: {e}")
            results.append({"category": case.get("category"), "query": case.get("query"),
                            "error": str(e), "mode": args.mode})
        if i % 10 == 0 or i == len(eval_set):
            logger.info(f"  进度 [{i}/{len(eval_set)}]")

    if not results:
        logger.error("无任何结果")
        return 1

    summary = summarize(results)
    print_report(summary)

    # 写详细结果
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {"summary": summary, "details": results, "mode": args.mode},
            f, ensure_ascii=False, indent=2,
        )
    logger.info(f"详细结果已写入: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())