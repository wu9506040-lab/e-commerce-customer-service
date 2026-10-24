"""全科 Agent（V21 M3-3 · 五专家合成）：开放问题的 FC Function Calling 兜底。

为什么需要第五个专家：
- 4 个意图域专家只负责"确定性"问题——规则/LLM 都能定类的（政策/商品/订单/退款）；
- method=default（LLM 兜底分类也失败）或未注册意图的"开放性"问题，
  此前一律回落 policy_expert——"帮我写段文案"会被送去查退货政策（答非所问）；
- FC 全科 Agent 不受意图域限制，按需自选工具（lookup_order / search_product /
  search_policy），恰好补上"规则和 LLM 都定不了"的口子。

架构意义（合成而非并存）：
  改造前 = 两套引擎灰度切换（ENABLE_AGENT_FC=True 时 FC 接管全部请求，
  False 时全走 4 专家）——两套管线并存互斥，面试叙事说不清"为什么两套"。
  改造后 = 一套多 Agent：专科管确定性，全科管开放性；
  ENABLE_AGENT_FC 语义收敛为"是否启用全科兜底"。

事件协议：直接透传 agent_runner 的 (meta/token/done) 事件流，
与 4 专家同一契约（base.AgentProtocol），orchestrator 无感知。
"""
from __future__ import annotations

import logging
from typing import Generator

from app.services.agents.base import AgentContext, AgentProtocol

logger = logging.getLogger(__name__)


class GeneralistAgent(AgentProtocol):
    """全科 Agent：FC 编排（agent_runner），开放问题兜底。

    dispatch 内部才 import agent_runner（懒加载）：registry 在应用启动期
    构建五专家表，不应为此提前拉起 LLM provider / 工具注册表。
    """

    name = "generalist_agent"
    intents = ("general",)  # 开放问题域（不与 4 个业务意图竞争路由）

    def dispatch(self, ctx: AgentContext) -> Generator:
        from app.services.chat.agent_runner import run_stream_agent

        logger.info(
            f"generalist dispatch: query={ctx.query[:40]!r} user_id={ctx.user_id}",
            extra={"intent": "general", "generalist": True},
        )
        return run_stream_agent(
            ctx.query, user_id=ctx.user_id, history=ctx.history
        )


__all__ = ["GeneralistAgent"]
