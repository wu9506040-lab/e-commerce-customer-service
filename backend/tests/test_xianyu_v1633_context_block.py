"""
V16.3.3-4 / V16.3.3-2 收尾：context_block 注入 + BuyerProfile V2 摘要 集成测试

定位（中游+下游都要守）：
1. ChatRequest 显式接收 context_block 字段（V16.3.3 闲鱼 daemon 传）
2. Synthesizer.run_stream 接 context_block 并透传给 dispatcher
3. _xianyu_inject_context 不覆盖 daemon 已拼好的 context_block（防覆盖）
4. XianyuBuyerProfileV2._summarize_older 走纯文本（0 LLM 依赖，1.7 模型快照安全）

按 CLAUDE.md 测试先修原则写，必先跑一次确认 fail 再修实现。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.schemas.chat import ChatRequest
from app.services.chat.orchestrator import Synthesizer


# =============================================================
# Test 1: ChatRequest schema 显式接收 context_block 字段
# =============================================================
class TestChatRequestContextBlockField:
    """V16.3.3：daemon POST /chat 必传 context_block（闲鱼买家画像+商品话术）"""

    def test_context_block_field_parsed(self):
        """context_block 必进 payload 属性（不靠 extra=allow 兜底）"""
        payload = ChatRequest(
            query="在吗",
            channel="xianyu",
            buyer_unb="2218051380643",
            context_block="【闲鱼 IM 渠道上下文】\n买家昵称：zwyyy",
        )
        assert payload.context_block == "【闲鱼 IM 渠道上下文】\n买家昵称：zwyyy"

    def test_context_block_default_none_backward_compat(self):
        """未传则 None（前端/老 daemon 兼容）"""
        payload = ChatRequest(query="在吗")
        assert payload.context_block is None

    def test_context_block_over_max_length_rejected(self):
        """>8k 拒绝（防 prompt 注入过大）"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ChatRequest(query="x", context_block="A" * 9000)


# =============================================================
# Test 2: Synthesizer.run_stream 透传 context_block 到 dispatcher
# =============================================================
class TestRunStreamContextBlockPropagation:
    """run_stream 必须接 context_block 并原样往下传（不丢）"""

    def test_run_stream_has_context_block_param(self):
        import inspect
        sig = inspect.signature(Synthesizer.run_stream)
        assert "context_block" in sig.parameters, (
            "Synthesizer.run_stream 必须有 context_block 参数；"
            "chat.py:486 之所以 LLM 看不到 context_block 就是因为这个签名缺"
        )

    def test_run_stream_passes_context_block_to_policy_dispatcher(self):
        """policy_query 路径：dispatcher(context_block=...) 必须是 daemon 传的原文"""
        ctx = "【闲鱼 IM 渠道上下文】买家：zwyyy | 客户类型：B2B 工程师"
        with patch.object(Synthesizer, "_handle_policy") as mock_policy, \
             patch("app.services.chat.orchestrator.prompt_assembler._build_context_block", return_value=""), \
             patch("app.services.chat.orchestrator.IntentService") as mock_intent, \
             patch("app.services.chat.orchestrator.rewrite_query", return_value=("45 号钢 还是 718", False)):
            mock_intent.classify.return_value = {
                "primary": "policy_query",
                "intent": "policy_query",
                "method": "rule",
                "confidence": 0.95,
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "intents": [],
                "upgraded": False,
            }
            mock_policy.return_value = iter([])
            list(Synthesizer.run_stream(
                query="45 号钢还是 718",
                user_id=0,
                context_block=ctx,
            ))
        assert mock_policy.called, "_handle_policy 未被调"
        kwargs = mock_policy.call_args.kwargs
        assert kwargs.get("context_block") == ctx, (
            f"dispatcher 收到 context_block={kwargs.get('context_block')!r}，"
            f"预期 {ctx!r}（daemon 原文必须原样穿透）"
        )


# =============================================================
# Test 3: _xianyu_inject_context 不覆盖 daemon 已拼好的
# =============================================================
class TestXianyuInjectDoesNotOverwrite:
    """daemon 已拼 context_block 时, _xianyu_inject_context 必须保留"""

    def _make_payload(self, *, unb, cid, query, order_no, existing_context):
        """模拟 ChatRequest 行为：context_block 字段已存在且非空"""
        payload = MagicMock(spec=ChatRequest)
        payload.buyer_unb = unb
        payload.session_id = cid
        payload.query = query
        payload.order_no = order_no
        payload.context_block = existing_context
        return payload

    def test_preserves_daemon_context_block(self):
        """daemon 拼的 context_block 必保留 — 不能被 _xianyu_inject_context 覆盖"""
        from app.api.chat import _xianyu_inject_context
        daemon_ctx = "【daemon 已拼好的 context】买家：zwyyy"
        payload = self._make_payload(
            unb="2218051380643",
            cid="cid-test-1",
            query="在吗",
            order_no="945237003804",
            existing_context=daemon_ctx,
        )
        # stub 掉所有 DB 依赖（test 重点：inject 不覆盖）
        with patch("sqlalchemy.create_engine"), \
             patch("sqlalchemy.orm.sessionmaker"), \
             patch("app.clients.mysql_client.get_db"), \
             patch(
                 "app.services.xianyu.xianyu_buyer_profile_v2.XianyuBuyerProfileV2"
             ), \
             patch(
                 "app.services.xianyu.xianyu_item_repository.XianyuItemRepository"
             ):
            _xianyu_inject_context(payload)
        # 断言：daemon 已拼的内容必须保留
        assert daemon_ctx in (payload.context_block or ""), (
            f"_xianyu_inject_context 覆盖了 daemon context_block: "
            f"{payload.context_block!r}"
        )

    def test_fills_when_empty(self):
        """payload.context_block 为空时, _xianyu_inject_context 兜底自动拼"""
        from app.api.chat import _xianyu_inject_context
        payload = self._make_payload(
            unb="2218051380643",
            cid="cid-test-2",
            query="在吗",
            order_no="945237003804",
            existing_context="",
        )
        # mock 假 buyer profile（只有 buyer_unb）
        with patch("sqlalchemy.create_engine"), \
             patch("sqlalchemy.orm.sessionmaker"), \
             patch("app.clients.mysql_client.get_db"):
            with patch(
                "app.services.xianyu.xianyu_buyer_profile_v2.XianyuBuyerProfileV2"
            ) as ProfileMock, \
            patch(
                "app.services.xianyu.xianyu_item_repository.XianyuItemRepository"
            ) as ItemMock:
                prof_instance = ProfileMock.return_value
                prof_instance.build_context.return_value = {
                    "nick": "zwyyy",
                    "customer_type": "engineer",
                    "project_category": "注塑模具",
                    "communication_pref": "tech_first",
                    "has_order": False,
                }
                item_instance = ItemMock.return_value
                item_instance.get_item_prompt.return_value = "商品话术示例"
                item_instance.get_item_title.return_value = "注塑模具 ¥10"

                _xianyu_inject_context(payload)

        # 兜底后非空
        assert payload.context_block and payload.context_block.strip(), (
            "_xianyu_inject_context 没在 context_block 为空时兜底填充"
        )
        assert "zwyyy" in payload.context_block
        assert "engineer" in payload.context_block


# =============================================================
# Test 4: XianyuBuyerProfileV2._summarize_older 纯文本（0 LLM 依赖）
# =============================================================
class TestXianyuBuyerProfileV2SummarizeNoLLM:
    """V16.3.3-2 收尾：_summarize_older 纯文本，0 LLM（1.7 模型快照安全）"""

    def test_summarize_older_does_not_call_llm(self):
        """V16.3.3-2 收尾：_summarize_older 不调任何 LLM provider（0 LLM 依赖，1.7 模型快照安全）"""
        from app.services.xianyu.xianyu_buyer_profile_v2 import XianyuBuyerProfileV2
        db = MagicMock()
        repo = XianyuBuyerProfileV2(db=db)
        msgs = [MagicMock(message_text=f"消息{i}", tip_type=None) for i in range(15)]
        # V16.3.3-2 设计：本仓库主 LLM 入口是 app.core.providers.llm.get_llm_provider
        # 未来引入别的 provider 时补 patch 路径
        with patch("app.core.providers.llm.get_llm_provider") as m_llm:
            summary = repo._summarize_older(msgs)
        assert isinstance(summary, str) and summary.strip(), "摘要必返回字符串"
        assert not m_llm.called, "get_llm_provider 被意外调用（违反 0 LLM 依赖约束）"

    def test_summarize_older_truncates_input(self):
        """_summarize_older 不爆（消息很多也不报 RecursionError）"""
        from app.services.xianyu.xianyu_buyer_profile_v2 import XianyuBuyerProfileV2
        db = MagicMock()
        repo = XianyuBuyerProfileV2(db=db)
        msgs = [MagicMock(message_text=f"m{i}", tip_type=None) for i in range(500)]
        summary = repo._summarize_older(msgs)
        assert isinstance(summary, str)
        assert len(summary) < 10_000, "摘要过长应截断"
