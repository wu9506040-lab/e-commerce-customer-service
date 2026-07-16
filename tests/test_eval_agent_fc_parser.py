"""tests/test_eval_agent_fc_parser.py

C4 收尾：eval_agent_fc.py SSE 解析单测
- 验证 _parse_sse_event 对实际 server SSE schema 的兼容
- 防止回归（之前版本读 event.data.tool_call 双层结构，导致 baseline 全部漏读）

覆盖：
  - meta 事件含 tool_call（顶层）：解析为 ("tool_call", payload)
  - done 事件含 answer（顶层）：解析为 ("done", payload)
  - token 事件含 text：解析为 ("token", payload) — done 未到时 fallback
  - meta 含 tool_result / final：返回 None（不误读为 tool_call）
  - heartbeat：返回 None
  - SSE id 行 / 空行 / 非 data 行：返回 None
  - 坏 JSON / 非 dict payload：返回 None（不抛异常）
  - 旧 schema data 双层嵌套：返回 None（不假装支持，保持版本独立）
  - UTF-8 中文 answer：正常解析
  - CRLF 行尾：strip 后正常解析
"""
import json
import sys
from pathlib import Path

# 让 `from scripts.eval_agent_fc import _parse_sse_event` 能跑（脚本在 scripts/ 下）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_agent_fc import _parse_sse_event  # noqa: E402


# =============================================================
# 1. 主路径：meta/tool_call / done/answer / token/text
# =============================================================
class TestParseMainSchema:
    """C4 live 实测确认的 schema：所有字段在顶层（不在 data 嵌套）。"""

    def test_meta_with_tool_call_top_level(self):
        """meta 事件含 tool_call 顶层字段 → 返回 ('tool_call', payload)。"""
        line = b'data: {"type": "meta", "tool_call": {"name": "lookup_order", "arguments": "{}"}}'
        result = _parse_sse_event(line)
        assert result == ("tool_call", {"name": "lookup_order", "arguments": "{}"}), (
            f"实际: {result}"
        )

    def test_done_with_answer_top_level(self):
        """done 事件含 answer 顶层字段 → 返回 ('done', payload)。"""
        line = 'data: {"type": "done", "answer": "您好"}'.encode("utf-8")
        result = _parse_sse_event(line)
        assert result == ("done", {"answer": "您好"}), f"实际: {result}"

    def test_token_with_text_top_level(self):
        """token 事件含 text 顶层字段 → 返回 ('token', payload)。"""
        line = 'data: {"type": "token", "text": "查"}'.encode("utf-8")
        result = _parse_sse_event(line)
        assert result == ("token", {"text": "查"}), f"实际: {result}"


# =============================================================
# 2. 跳过路径：meta/heartbeat/final/tool_result
# =============================================================
class TestParseSkipEvents:
    """返回 None（不让 eval 误处理）。"""

    def test_meta_final_skipped(self):
        """meta.final 事件 → None（不视作 tool_call）。"""
        line = b'data: {"type": "meta", "turn": 2, "final": true, "tool_used_count": 1}'
        assert _parse_sse_event(line) is None

    def test_meta_tool_result_skipped(self):
        """meta.tool_result 事件 → None（不视作 tool_call，eval 不需要）。"""
        line = b'data: {"type": "meta", "tool_result": {"id": "x", "name": "lookup_order", "result": {}}}'
        assert _parse_sse_event(line) is None

    def test_heartbeat_skipped(self):
        """heartbeat 事件 → None。"""
        line = b'data: {"type": "heartbeat", "ts": 1700000000000}'
        assert _parse_sse_event(line) is None

    def test_unknown_type_skipped(self):
        """未知 type → None。"""
        line = b'data: {"type": "error", "message": "boom"}'
        assert _parse_sse_event(line) is None


# =============================================================
# 3. SSE 协议层：非 data 行 / 空行 / 坏 JSON
# =============================================================
class TestParseSseProtocol:
    def test_id_line_skipped(self):
        """SSE id: 行（不带 data:） → None。"""
        assert _parse_sse_event(b"id: 1") is None

    def test_event_line_skipped(self):
        """SSE event: 行 → None。"""
        assert _parse_sse_event(b"event: meta") is None

    def test_empty_line_skipped(self):
        """空行 → None。"""
        assert _parse_sse_event(b"") is None
        assert _parse_sse_event(b"\n") is None
        assert _parse_sse_event(b"\r\n") is None

    def test_comment_line_skipped(self):
        """SSE 注释行（:comment） → None。"""
        assert _parse_sse_event(b":keepalive") is None

    def test_bad_json_skipped(self):
        """data: 后 JSON 损坏 → None（不抛）。"""
        assert _parse_sse_event(b"data: not json{") is None
        assert _parse_sse_event(b"data: {incomplete") is None

    def test_non_dict_payload_skipped(self):
        """data: 后是字符串/列表而非 dict → None。"""
        assert _parse_sse_event(b'data: "string"') is None
        assert _parse_sse_event(b"data: [1,2,3]") is None
        assert _parse_sse_event(b"data: 42") is None
        assert _parse_sse_event(b"data: null") is None

    def test_empty_data_prefix_skipped(self):
        """data: 后空 → None。"""
        assert _parse_sse_event(b"data:") is None
        assert _parse_sse_event(b"data: ") is None


# =============================================================
# 4. 编码与行尾
# =============================================================
class TestParseEncoding:
    def test_crlf_line_ending(self):
        """\\r\\n 行尾 strip 后正常解析。"""
        line = b'data: {"type": "done", "answer": "x"}\r\n'
        assert _parse_sse_event(line) == ("done", {"answer": "x"})

    def test_utf8_chinese(self):
        """UTF-8 中文 answer 正常解析（不抛 UnicodeDecodeError）。"""
        line = 'data: {"type": "done", "answer": "您好"}'.encode("utf-8")
        assert _parse_sse_event(line) == ("done", {"answer": "您好"})

    def test_invalid_utf8_replaced_not_raised(self):
        """非法 UTF-8 bytes 不抛异常（errors='replace' 策略）。"""
        # 0xff 单字节是非法 UTF-8 起始
        line = b'data: {"type": "done", "answer": "\xff\xfe"}'
        # 不抛异常；解析可能成功也可能返回 None（取决于替换后的 JSON 是否合法）
        result = _parse_sse_event(line)
        assert result is None or (isinstance(result, tuple) and result[0] == "done")


# =============================================================
# 5. 边界：旧 schema 不假装支持
# =============================================================
class TestParseOldSchemaRejected:
    """旧版 eval 读 event.data.tool_call 双层结构；当前 server 不发此结构。
    当前 parser 不应假装支持（避免掩盖未来 schema 漂移）。"""

    def test_old_double_layered_meta_rejected(self):
        """data 双层嵌套的 meta.tool_call → None（不发则忽略）。"""
        line = b'data: {"type": "meta", "data": {"tool_call": {"name": "old"}}}'
        assert _parse_sse_event(line) is None

    def test_old_double_layered_done_rejected(self):
        line = b'data: {"type": "done", "data": {"answer": "old"}}'
        assert _parse_sse_event(line) is None


# =============================================================
# 6. 字段缺失防御
# =============================================================
class TestParseMissingFields:
    def test_meta_without_tool_call_returns_none(self):
        """meta 事件但无 tool_call → None（不要返回 'meta' 这种无意义类型）。"""
        line = b'data: {"type": "meta", "turn": 1}'
        assert _parse_sse_event(line) is None

    def test_done_without_answer_returns_none(self):
        """done 事件但无 answer → None。"""
        line = b'data: {"type": "done"}'
        assert _parse_sse_event(line) is None

    def test_token_without_text_returns_none(self):
        """token 事件但无 text → None。"""
        line = b'data: {"type": "token"}'
        assert _parse_sse_event(line) is None


# =============================================================
# 7. 集成验证：模拟 SSE 流的逐行解析（端到端）
# =============================================================
class TestParseRealisticSseStream:
    """模拟 server 真实 SSE 输出（来自 ECS 实测抓包）。"""

    def test_full_lookup_order_flow(self):
        """订单查询完整流程：meta(tool_call) + meta(tool_result) + meta(final) + tokens + done"""
        lines = [
            b"id: 1",
            b'data: {"type": "meta", "turn": 1, "tool_call": {"id": "call_1", "name": "lookup_order", "arguments": "{\\"order_no\\": \\"SO001\\"}"}, "stream_id": "abc"}',
            b"id: 2",
            b'data: {"type": "meta", "turn": 1, "tool_result": {"id": "call_1", "name": "lookup_order", "result": {"status": "shipped"}}, "stream_id": "abc"}',
            b"id: 3",
            b'data: {"type": "meta", "turn": 2, "final": true, "tool_used_count": 1, "stream_id": "abc"}',
            b"id: 4",
            b'data: {"type": "token", "text": "\\u60a8"}',  # 您
            b"id: 5",
            b'data: {"type": "token", "text": "\\u7684"}',  # 的
            b"id: 6",
            b'data: {"type": "token", "text": "\\u8ba2\\u5355\\u5df2\\u53d1\\u8d27"}',  # 订单已发货
            b"id: 7",
            b'data: {"type": "done", "answer": "\\u60a8\\u7684\\u8ba2\\u5355\\u5df2\\u53d1\\u8d27"}',
            b"",
            b"",
        ]

        tool_calls = []
        done_answer = ""
        token_acc = []
        for line in lines:
            parsed = _parse_sse_event(line)
            if parsed is None:
                continue
            event_type, payload = parsed
            if event_type == "tool_call":
                if payload.get("name"):
                    tool_calls.append(payload["name"])
            elif event_type == "done":
                done_answer = payload.get("answer") or ""
            elif event_type == "token":
                if payload.get("text"):
                    token_acc.append(payload["text"])

        # 验证：解析到 1 个 tool_call（lookup_order），done_answer 正确
        assert tool_calls == ["lookup_order"], f"tool_calls: {tool_calls}"
        assert done_answer == "\u60a8\u7684\u8ba2\u5355\u5df2\u53d1\u8d27", (
            f"done_answer: {done_answer!r}"
        )
        # 验证 token_acc 也累积（fallback 路径可用）
        assert len(token_acc) == 3

    def test_no_tool_direct_reply_flow(self):
        """direct 类（不调工具）流程：meta(final) + tokens + done"""
        lines = [
            b'data: {"type": "meta", "turn": 1, "final": true, "tool_used_count": 0, "stream_id": "xyz"}',
            b'data: {"type": "token", "text": "\\u4f60\\u597d"}',  # 你好
            b'data: {"type": "done", "answer": "\\u4f60\\u597d\\uff0c\\u6211\\u662f\\u667a\\u80fd\\u5ba2\\u670d"}',
        ]

        tool_calls = []
        done_answer = ""
        token_acc = []
        for line in lines:
            parsed = _parse_sse_event(line)
            if parsed is None:
                continue
            event_type, payload = parsed
            if event_type == "tool_call":
                if payload.get("name"):
                    tool_calls.append(payload["name"])
            elif event_type == "done":
                done_answer = payload.get("answer") or ""
            elif event_type == "token":
                if payload.get("text"):
                    token_acc.append(payload["text"])

        # direct 类：不调工具（tool_calls 为空），done_answer 有内容
        assert tool_calls == []
        assert done_answer == "\u4f60\u597d\uff0c\u6211\u662f\u667a\u80fd\u5ba2\u670d"
        # token 累积做 fallback
        assert "".join(token_acc) == "\u4f60\u597d"