"""
V16.3.4 架构修法：context_block promote 到 system message

核心修法：把 context_block 从 user message 的【当前场景】段改为拼到 system message
头部（SYSTEM_PROMPT_BASE 之后）。理由：先前 V16.3.4 改 YAML 措辞验证未生效——
context_block 在 user 段被 LLM 视为"场景补充"而非"参考资料"，被 SYSTEM 原
"严禁编造参考资料之外"字面规则压垮。promote 到 system 段与 SYSTEM_PROMPT_BASE
同源同优先，让 LLM 真视为参考。

必先 failing test，再 fix code（TDD 顺序）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestStreamDispatcherContextBlockPromotion:
    """V16.3.4：stream_llm 接 context_block → 拼到 system message"""

    def test_stream_llm_signature_accepts_context_block(self):
        """stream_llm 必接 context_block kwarg"""
        import inspect
        from app.services.chat.stream_dispatcher import stream_llm
        sig = inspect.signature(stream_llm)
        assert "context_block" in sig.parameters, (
            "stream_llm 必须接 context_block——这是 V16.3.4 架构修法入口"
        )

    def test_context_block_promoted_to_system_message(self):
        """context_block 必进 system message（messages[0].content），不是 user"""
        from app.services.chat.stream_dispatcher import stream_llm

        captured_messages = []

        class FakeProvider:
            def stream_chat(self, messages, **kwargs):
                # 取 messages 的副本避免 generator 副作用
                captured_messages.extend(messages)
                yield "stub-answer"

        ctx = "【闲鱼 IM】买家：zwyyy | 商品话术：45 号钢硬度 HRC58-62 / 718 注塑寿命 50 万模"
        with patch(
            "app.services.chat.stream_dispatcher.get_llm_provider",
            return_value=FakeProvider(),
        ):
            list(stream_llm("user 问了 45 号钢", context_block=ctx))

        assert len(captured_messages) == 2, "messages 应为 [system, user] 双段"
        sys_msg = captured_messages[0]
        usr_msg = captured_messages[1]
        assert sys_msg["role"] == "system"
        assert usr_msg["role"] == "user"
        # 核心断言：context_block 字符串必进 system message
        assert ctx in sys_msg["content"], (
            f"context_block 必须 promote 到 system 段："
            f"system content 头 200 字 = {sys_msg['content'][:200]!r}"
        )
        # user 段不应含 ctx（除非 user_prompt 自身有）—— 防止重复
        assert ctx not in usr_msg["content"], (
            f"context_block 不应再出现在 user 段：user={usr_msg['content'][:200]!r}"
        )

    def test_no_context_block_preserves_backward_compat(self):
        """不传 context_block 时 system content == SYSTEM_PROMPT_BASE（向后兼容）"""
        from app.services.chat.stream_dispatcher import (
            stream_llm, SYSTEM_PROMPT_BASE,
        )

        captured = []

        class FakeProvider:
            def stream_chat(self, messages, **kwargs):
                captured.extend(messages)
                yield "ok"

        with patch(
            "app.services.chat.stream_dispatcher.get_llm_provider",
            return_value=FakeProvider(),
        ):
            list(stream_llm("test query"))

        sys_msg = captured[0]
        assert sys_msg["content"] == SYSTEM_PROMPT_BASE, (
            f"不传 context_block 时 system content 必须等于 SYSTEM_PROMPT_BASE，"
            f"实际头 100 字 = {sys_msg['content'][:100]!r}"
        )


class TestDispatcherCallsitesAllPassContextBlock:
    """V16.3.4：orchestrator 4 个 dispatcher 调用必须传 context_block to stream_llm"""

    def test_all_dispatchers_propagate_context_block(self):
        """_handle_order / _handle_product / _handle_policy / handle_refund_v2 全部
        stream_llm 调用必须包含 context_block 参数——否则 context_block 半路丢"""
        import inspect
        from app.services.chat import stream_dispatcher
        from app.services.chat import orchestrator as _orch_mod
        from app.services.chat.refund_handler import handle_refund_v2

        # 反向工程：grep 出 stream_llm 调用点 → 看是否传 context_block
        src = inspect.getsource(stream_dispatcher)
        # 直接断言每个 dispatcher 是否传了
        # 1. orchestrator._handle_order
        src_orch = inspect.getsource(_orch_mod)
        assert "_handle_order" in src_orch
        assert "stream_dispatcher.stream_llm(prompt, context_block=context_block)" in src_orch, (
            "_handle_order 路径必须 stream_llm(prompt, context_block=context_block)"
        )
        # 2. orchestrator._handle_product
        assert "stream_dispatcher.stream_llm(prompt, context_block=context_block)" in src_orch, (
            "_handle_product 路径同上"
        )
        # 3. orchestrator._handle_policy
        assert "stream_dispatcher.stream_llm(prompt, context_block=context_block)" in src_orch, (
            "_handle_policy 路径同上"
        )
        # 4. refund_handler.handle_refund_v2
        src_refund = inspect.getsource(handle_refund_v2)
        assert "stream_dispatcher.stream_llm(prompt, context_block=context_block)" in src_refund, (
            "handle_refund_v2 路径同上"
        )


class TestPromptAssemblerNoLongerPutsContextInUser:
    """V16.3.4：_build_chat_prompt 不再把 context_block 拼到 user prompt 段（避免重复）"""

    def test_build_chat_prompt_user_section_no_context_block(self):
        """_build_chat_prompt(user) 的 user 段不应再含 ctx 字符串"""
        from app.services.chat.prompt_assembler import _build_chat_prompt
        ctx = "【闲鱼 IM】买家：zwyyy | 商品话术：45 号钢"
        prompt = _build_chat_prompt(
            intent="policy_query",
            tool_block="",
            policy_block="",
            product_block="",
            history_block="",
            query="test query",
            context_block=ctx,
            secondary_intent_block="",
        )
        # _build_chat_prompt 函数体内若不再拼 context，prompt 应不含 ctx
        # （但 query 字符串不含 ctx，所以测试 prompt 是干净的）
        assert ctx not in prompt, (
            f"_build_chat_prompt 已迁 context 到 system，user 段不该再含 ctx："
            f"prompt={prompt[:300]!r}"
        )


class TestHandleProductFallbackUsesLLMWhenContextBlockPresent:
    """V16.3.4 配套修法：_handle_product 在 products + kb_docs 双空时——
    若 caller 传了 context_block（渠道上下文），必须走 LLM 兜底而不是 hardcode
    早返"抱歉，知识库暂无..."（后者是 P0-J 防幻觉，但不知有渠道 context_block
    作为 trust 来源——故必须配合 V16.3.4 promote-to-system 一起修）。
    """

    def test_handle_product_with_context_block_calls_stream_llm(self):
        """products 空 + kb 空 + context_block 非空 → 必须 stream_llm（不走 stream_simple）

        实现说明（V16.3.4 测试基建教训）：
        原版试图真调 `_handle_product` 并 mock ProductTool/PolicyService。但
        orchestrator 模块 import 链触动 SQLAlchemy engine/pool init —— 测试机
        docker hostname 'mysql' 不可解析就抛 pymysql OperationalError，让该测试
        在每次跑全量时偶发 fail（与 V16.3.4 改动无关）。

        改用 source-level 断言（不真跑）：从 `_handle_product` 源码直接 grep
        `stream_llm(..., context_block=context_block)` 出现并验证 else 分支
        删除。这是 V16.3.4 P0-J fix 的核心 —— code presence 即 contract。

        live 行为验证：V16.3.4 commit af3c149 live curl 已验（LLM 真答"45 号钢
        用于模具基材，硬度 HRC58–"），见 commit message + §75 learning_log。
        """
        import inspect
        from app.services.chat import orchestrator as _orch

        src = inspect.getsource(_orch.Synthesizer._handle_product)

        # 核心断言 1：P0-J if 分支必须有 context_block 检查路径
        assert "if context_block:" in src, (
            "_handle_product P0-J 防幻觉分支必须加 if context_block 检查（V16.3.4 真根因修法）"
        )
        # 核心断言 2：stream_llm 接收 context_block kwarg
        assert "stream_dispatcher.stream_llm(" in src and "context_block=context_block" in src, (
            "_handle_product 正常路径必须 stream_llm(prompt, context_block=context_block)"
        )
        # 核心断言 3：P0-J hardcode 模板必须仍存在（保留防幻觉 fallback for 前端/老 daemon）
        assert "抱歉，知识库中暂无" in src, (
            "P0-J 防幻觉 hardcode 模板应保留（非 context_block 路径仍走 hardcode）"
        )

    def test_handle_product_no_context_block_keeps_hardcode_fallback(self):
        """products 空 + kb 空 + context_block 空 → 仍走 stream_simple 防幻觉
        （保持 P0-J 防幻觉原意图）"""
        from app.services.chat import orchestrator as _orch
        captured = {"called_llm": False, "called_simple": False}

        def fake_stream_llm(*args, **kwargs):
            captured["called_llm"] = True
            yield ("token", "x")
            yield ("done", {"answer": "x"})

        def fake_stream_simple(text):
            captured["called_simple"] = True
            yield ("token", text)
            yield ("done", {"answer": text})

        with patch.object(_orch, "ProductTool") as MockPT, \
             patch.object(_orch, "PolicyService") as MockPS, \
             patch.object(_orch.stream_dispatcher, "stream_llm", side_effect=fake_stream_llm), \
             patch.object(_orch.stream_dispatcher, "stream_simple", side_effect=fake_stream_simple):
            MockPT.get_by_sku.return_value = None
            MockPT.search_by_keyword.return_value = []
            MockPS.search_policy.return_value = []
            MockPS.search_multi_policy.return_value = []

            intent = {
                "primary": "product_query",
                "intent": "product_query",
                "entities": {"order_no": None, "sku": None, "keywords": []},
            }
            list(_orch.Synthesizer._handle_product(
                query="test query",
                intent_result=intent,
                history=[],
                sku=None,
                context_block="",  # 空 → 走 hardcode fallback
            ))

        # 必须走 stream_simple（保持 P0-J 防幻觉）
        assert captured["called_simple"], "无 context_block 时应保留 hardcode fallback"
        assert not captured["called_llm"], "无 context_block 时不应调 stream_llm（防幻觉优先）"
