"""BusinessFlow Protocol（M14 §10 阶段 3）

按 plan §10 阶段 3 风险：第 2 个 Flow 出现时再抽 Base 类。
本文件只定义 Protocol（结构类型），不写 Base 抽象类。

使用 Protocol 而非 ABC 的理由：
- 不强制继承（YAGNI：避免空壳类）
- typing.Protocol 是 duck typing 的形式化（CLAUDE.md §9.3.1）
- 未来第 2 个 Flow 出现时，可从 Protocol 推导 Base 抽象
"""
from __future__ import annotations

from typing import Any, Generator, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class Flow(Protocol):
    """BusinessFlow 接口（结构类型）

    每个 Flow 代表一个业务场景的显式状态机：
    - 接收业务输入（query/user_id/intent_result/...）
    - 按节点的固定顺序执行
    - 边执行边 yield SSE 事件（meta / token / done）
    - meta 事件携带 flow_stage 字段（前端可视化阶段指示器）
    """

    # Flow 名称（用于日志 + 审计 + 前端展示）
    name: str

    def run(self) -> Generator[Tuple[str, Any], None, None]:
        """执行 Flow，按节点顺序 yield SSE 事件

        Returns:
            生成器，yield ("meta", {...}) / ("token", "...") / ("done", {...}) 元组
        """
        ...