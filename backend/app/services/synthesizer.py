"""
Response Synthesizer - 薄壳 re-export（Sprint 3 拆分后）

Sprint 3 拆分背景：
- 原 928 行单体 → services/chat/{orchestrator, prompt_assembler, stream_dispatcher, refund_handler, citation_formatter}
- 本文件保留为「向后兼容薄壳」：兜住历史 import 路径，让上游零改动升级
- 唯一源真（single source of truth）：app.services.chat.* 子包

何时删除：
- 当前代码已无业务逻辑（L1 重构完成）；仅作 re-export 垫片
- 删除计划 = S4 末（与 core/qwen.py / core/embedding.py / services/rerank.py 一起清退）
- 删除前需扫除：
  1. 全仓 grep `from app.services.synthesizer` 应只剩本文件内
  2. 删 test_anti_hallucination / test_source_attribution / test_synthesizer_refund 中 patch app.services.synthesizer.* 的引用（已 Sprint 3 commit 4 移到 chat.* 命名空间）
  3. 全量测试 150 PASS 不变
"""
# 主类：orchestrator 接管
from app.services.chat.orchestrator import Synthesizer  # noqa: F401

# Prompt 模板：prompt_assembler 提供（最终走 prompt_loader.load）
from app.services.chat.prompt_assembler import (  # noqa: F401
    SYSTEM_PROMPT_BASE,
    NO_LOGIN_PROMPT,
    _build_context_block,
    _build_chat_prompt,
    _format_tool_result,
    _format_policy_docs,
    _format_history,
    _build_meta_contexts,
    _extract_order_no_from_history,
)

# 流式 / 调度：stream_dispatcher 提供
from app.services.chat.stream_dispatcher import (  # noqa: F401
    _LLM_SEMAPHORE,
    stream_llm,
    stream_simple,
    search_by_keyword_window,
)

# 退款双轨制（V2 + V3）：refund_handler 提供
from app.services.chat.refund_handler import (  # noqa: F401
    handle_refund_v2,
    handle_refund_v3,
)

__all__ = [
    "Synthesizer",
    "SYSTEM_PROMPT_BASE",
    "NO_LOGIN_PROMPT",
    "_build_context_block",
    "_build_chat_prompt",
    "_format_tool_result",
    "_format_policy_docs",
    "_format_history",
    "_build_meta_contexts",
    "_extract_order_no_from_history",
    "_LLM_SEMAPHORE",
    "stream_llm",
    "stream_simple",
    "search_by_keyword_window",
    "handle_refund_v2",
    "handle_refund_v3",
]
