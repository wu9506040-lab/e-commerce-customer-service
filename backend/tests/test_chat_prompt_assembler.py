"""
Sprint 3: chat.prompt_assembler 纯函数单元测试

覆盖 chat/prompt_assembler.py 的 7 个纯字符串处理函数：
- _build_chat_prompt  - 7 段优先级拼接（context > tool > policy > product > history > 问题）
- _format_tool_result - order_query / refund_query 的 [订单]/[退款] 标签格式化
- _format_policy_docs - policy 加 [1][2]... 编号
- _format_history      - 历史消息 user/assistant 拼接
- _extract_order_no_from_history - 从历史提取最近 ORD... 订单号（M9.5+ / M13 修）

设计原则：
- 纯函数测试，无 I/O / 无 DB / 无 LLM 依赖 → pytest 直接跑
- 与 test_source_attribution.py 不重复（那个测端到端 LLM 调用后的产物；这个测单一函数输出）
- 与 test_prompt_loader.py 解耦（那个测 YAML 加载；这个测加载后的字符串处理）

依据：docs/decisions/2026-07-12-sprint-3-synthesizer-split.md §6 + §10
"""
import os
import sys

# 让模块能找到 app 包（与项目其他测试一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================
# _build_chat_prompt：7 段优先级硬约束
# =============================================================

def test_build_chat_prompt_no_blocks_falls_back_to_empty():
    """所有段都为空 → 保留「无可用资料」兜底段 + 仍拼问题"""
    from app.services.chat.prompt_assembler import _build_chat_prompt

    out = _build_chat_prompt(
        intent="policy_query",
        tool_block="",
        policy_block="",
        product_block="",
        history_block="",
        query="运费多少",
    )
    assert "无可用资料" in out, f"空 blocks 应有兜底段: {out}"
    assert "问题：运费多少" in out
    # 顺序：兜底段在「问题」之前
    assert out.index("无可用资料") < out.index("问题：")


def test_build_chat_prompt_context_block_takes_priority():
    """context_block（M9.5）应当放在最前（高于 tool_block）"""
    from app.services.chat.prompt_assembler import _build_chat_prompt

    out = _build_chat_prompt(
        intent="order_query",
        tool_block="[订单] 一些事实",
        policy_block="",
        product_block="",
        history_block="",
        query="物流",
        context_block="【当前订单】ORD001 已发货",
    )
    # context_block 标记【当前场景】
    ctx_idx = out.index("【当前场景】")
    tool_idx = out.index("[订单]")
    assert ctx_idx < tool_idx, f"context 应在 tool 之前；实际 out={out!r}"


def test_build_chat_prompt_full_priority_ordering():
    """完整 6 段：context > tool > policy > product > history > 问题"""
    from app.services.chat.prompt_assembler import _build_chat_prompt

    out = _build_chat_prompt(
        intent="refund_query",
        tool_block="TOOL",
        policy_block="POLICY",
        product_block="PRODUCT",
        history_block="HISTORY",
        query="Q",
        context_block="CTX",
    )

    # 每个标记段都在前一个之前（或同位置），问题段最末
    indices = [
        out.index("CTX"),
        out.index("TOOL"),
        out.index("POLICY"),
        out.index("PRODUCT"),
        out.index("HISTORY"),
        out.index("问题：Q"),
    ]
    assert indices == sorted(indices), f"段落顺序乱了：indices={indices}\n{out!r}"


def test_build_chat_prompt_sections_separated_by_blank_lines():
    """段落之间用 \\n\\n 分隔，便于 LLM 区分"""
    from app.services.chat.prompt_assembler import _build_chat_prompt

    out = _build_chat_prompt(
        intent="policy_query",
        tool_block="T",
        policy_block="P",
        product_block="",
        history_block="",
        query="Q",
    )
    # 至少有 2 个空行（tool 段、policy 段、问题段共 3 段 → 2 个间隔）
    assert "\n\n" in out


# =============================================================
# _format_policy_docs：政策加 [1][2]... 编号
# =============================================================

def test_format_policy_docs_empty_returns_empty_string():
    """空列表 → 空字符串（不输出「无相关政策」之类兜底，由 _build_chat_prompt 决定）"""
    from app.services.chat.prompt_assembler import _format_policy_docs
    assert _format_policy_docs([]) == ""
    assert _format_policy_docs(None) == ""


def test_format_policy_docs_truncates_long_text():
    """> 500 字符的文本截断 + 加 ... 后缀"""
    from app.services.chat.prompt_assembler import _format_policy_docs

    long_text = "X" * 700
    out = _format_policy_docs([{"text": long_text}])
    assert "[1]" in out
    # 截断后含 500 字符 + "..." 后缀
    assert "..." in out
    assert len(out) < len(long_text) + 50  # 留 buffer 给 [1] / 换行


# =============================================================
# _format_history：复用 pipeline 的 user/assistant 拼接
# =============================================================

def test_format_history_skips_empty_content():
    """空 content 的消息跳过（防止空行污染 prompt）"""
    from app.services.chat.prompt_assembler import _format_history

    history = [
        {"role": "user", "content": "  "},  # 仅空白 → 视为空
        {"role": "user", "content": "真问题"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "回答"},
    ]
    out = _format_history(history)
    assert "真问题" in out
    assert "回答" in out
    # 应只有 2 行（user/assistant 各 1），没有空行
    assert out.count("用户：") == 1
    assert out.count("助手：") == 1


def test_format_history_unknown_role_skipped():
    """非 user/assistant 角色（如 system / tool）不输出"""
    from app.services.chat.prompt_assembler import _format_history

    history = [
        {"role": "system", "content": "系统提示"},  # 跳过
        {"role": "tool", "content": "工具输出"},     # 跳过
        {"role": "user", "content": "ok"},
    ]
    out = _format_history(history)
    assert "系统提示" not in out
    assert "工具输出" not in out
    assert "ok" in out


# =============================================================
# _extract_order_no_from_history：M9.5+ 兜底提取订单号
# =============================================================

def test_extract_order_no_from_history_returns_latest():
    """多轮对话中提取最近一个 ORDER 号（反向遍历）"""
    from app.services.chat.prompt_assembler import _extract_order_no_from_history

    history = [
        {"role": "user", "content": "ORD20260101001 物流"},
        {"role": "assistant", "content": "已发货"},
        {"role": "user", "content": "那能退吗 ORD20260202002"},  # 最新提到
    ]
    assert _extract_order_no_from_history(history) == "ORD20260202002"


def test_extract_order_no_from_history_supports_letter_suffix():
    """M13 修复：订单号 ORD+8 位日期 + 3-6 位字母数字混合（含字母后缀如 899EBA）"""
    from app.services.chat.prompt_assembler import _extract_order_no_from_history

    history = [
        {"role": "user", "content": "ORD20260704899EBA 啥情况"},  # 含字母后缀
    ]
    assert _extract_order_no_from_history(history) == "ORD20260704899EBA"


def test_extract_order_no_from_history_returns_none_when_absent():
    """无 ORDER 串 → None（让上游走「请提供订单号」分支）"""
    from app.services.chat.prompt_assembler import _extract_order_no_from_history

    assert _extract_order_no_from_history(None) is None
    assert _extract_order_no_from_history([]) is None
    assert _extract_order_no_from_history([{"role": "user", "content": "今天天气如何"}]) is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
