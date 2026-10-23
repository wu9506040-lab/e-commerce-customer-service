"""受控写操作审批门控（V20 亮点 2）：确认票据 + 幂等 + 审计。

与现有 2 步确认的关系（order_modify_tool V2 已有 confirmed 门禁）：
- 用户确认（confirmed=True）解决"用户是否同意"
- 本模块解决确认之后的三个缺口（OPTIMIZATION 文档点名的三件事）：
  ① **票据**：确认不是一次性口头，而是可校验的凭证——confirmed=True 时
     必须存在同 payload 的 pending 票据，否则拒绝执行
  ② **幂等**：同 payload 的重复确认返回同一张票据；已执行的票据不允许
     二次消费（防 LLM 重试导致重复写业务数据）
  ③ **审计**：request / execute / duplicate / reject 全部落 JSONL

语义（与 agent_checkpointer 同一哲学）：
- gate 是安全项，但异常路径宁可放行并告警也不挂主链路？——**不**。
  写操作的门控失败必须拒绝执行（fail-closed），读操作不受影响。
  这是本模块与 trace/checkpointer 唯一的哲学差异：观测可降级，写安全不可降级。

票据生命周期：pending → executed / rejected；TTL 过期 → expired（拒绝）。
存储：v1 进程内 dict + JSONL 审计（单实例部署足够）；多副本时把 dict 换
Redis 即可，接口不变。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_gate: Optional["ApprovalGate"] = None


class ApprovalGate:
    """写操作审批票据门（单例）。"""

    def __init__(self, ttl_seconds: int = 600, audit_dir: Optional[str] = None):
        self._tickets: dict[str, dict[str, Any]] = {}  # idempotent_key -> ticket
        self._ttl = ttl_seconds
        from app.core.config import settings

        self._dir = Path(audit_dir or getattr(settings, "AGENT_TRACE_DIR", "data/agent_traces"))

    # ---------- 内部 ----------

    @staticmethod
    def idempotent_key(tool_name: str, user_id: Any, payload: dict) -> str:
        """幂等键：同工具 + 同用户 + 同参数 = 同一张票。"""
        raw = json.dumps(
            {"tool": tool_name, "user": user_id, "payload": payload},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _audit(self, event: str, ticket: dict) -> None:
        """审计落 JSONL——审计失败只告警（不能因为审计挂掉影响主流程判断）。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / "approvals.jsonl"
            line = json.dumps(
                {"ts": int(time.time() * 1000), "event": event, **ticket},
                ensure_ascii=False, default=str,
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("[approval] 审计写入失败（忽略）: %s", e)

    # ---------- 对外三步 ----------

    def request(self, tool_name: str, user_id: Any, payload: dict) -> dict:
        """第一步（confirmed=False 时调用）：申请票据，返回票据信息。

        幂等：同 key 已有 pending 票据 → 返回同一张（不新建）。
        """
        key = self.idempotent_key(tool_name, user_id, payload)
        with _lock:
            existing = self._tickets.get(key)
            if existing and existing["status"] == "pending" and not self._expired(existing):
                self._audit("request_duplicate", existing)
                return self._public(existing)

            ticket = {
                "ticket_id": uuid.uuid4().hex[:16],
                "key": key,
                "tool": tool_name,
                "user_id": user_id,
                "payload": payload,
                "status": "pending",
                "created_at": int(time.time() * 1000),
                "expires_at": int((time.time() + self._ttl) * 1000),
            }
            self._tickets[key] = ticket
        self._audit("request", ticket)
        return self._public(ticket)

    def verify_and_consume(self, tool_name: str, user_id: Any, payload: dict) -> tuple[bool, str]:
        """第二步（confirmed=True 时调用）：校验并消费票据。

        Returns:
            (True, "")                     —— 票据有效，已消费，可执行
            (False, reason)                —— 拒绝执行；reason ∈
                no_ticket / expired / duplicate / rejected
        """
        key = self.idempotent_key(tool_name, user_id, payload)
        with _lock:
            ticket = self._tickets.get(key)
            if ticket is None:
                reason = "no_ticket"
            elif ticket["status"] == "rejected":
                reason = "rejected"
            elif self._expired(ticket):
                ticket["status"] = "expired"
                reason = "expired"
            elif ticket["status"] == "executed":
                reason = "duplicate"
            else:
                ticket["status"] = "executed"
                ticket["executed_at"] = int(time.time() * 1000)
                self._audit("execute", ticket)
                return True, ""
        self._audit(f"reject_{reason}", ticket or {"key": key, "tool": tool_name})
        return False, reason

    def reject(self, tool_name: str, user_id: Any, payload: dict) -> None:
        """坐席/用户明确拒绝：票据标记 rejected（同 payload 再申请会新建）。"""
        key = self.idempotent_key(tool_name, user_id, payload)
        with _lock:
            t = self._tickets.get(key)
            if t and t["status"] == "pending":
                t["status"] = "rejected"
                self._audit("reject_manual", t)

    # ---------- helpers ----------

    def _expired(self, ticket: dict) -> bool:
        return time.time() * 1000 > ticket.get("expires_at", 0)

    @staticmethod
    def _public(ticket: dict) -> dict:
        """对外暴露的字段（不含内部 key）。"""
        return {
            "ticket_id": ticket["ticket_id"],
            "status": ticket["status"],
            "tool": ticket["tool"],
            "expires_at": ticket["expires_at"],
        }


def get_approval_gate() -> ApprovalGate:
    """单例入口。AGENT_GATING_ENABLED=False 时返回的实例仍工作，
    由调用方决定是否启用（保持模块可测试）。"""
    global _gate
    if _gate is None:
        with _lock:
            if _gate is None:
                from app.core.config import settings

                _gate = ApprovalGate(
                    ttl_seconds=int(getattr(settings, "AGENT_TICKET_TTL_SECONDS", 600)),
                )
    return _gate


def reset_approval_gate() -> None:
    """测试辅助。"""
    global _gate
    with _lock:
        _gate = None
