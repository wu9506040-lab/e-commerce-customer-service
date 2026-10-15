"""
DebounceQueue V16.4 标点检测 — 启发式断句识别

新设计：
- 收到新消息，看最新消息结尾标点
- 句末标点（?？!！.。?） → _short_window 立即触发（话已说完）
- 其它标点/无标点 → 维持原 _first/_next_window 等待

防 Bug：纯逗号或无标点 → 不能立即 fire（话还没说完）
"""
import os
import time
from unittest.mock import MagicMock

import pytest

from app.services.xianyu.debounce_queue import DebounceQueue


# 强制使用短 window 测试（避免被全局 env 干扰）
@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    monkeypatch.setenv("XIANYU_DEBOUNCE_FIRST", "0.5")
    monkeypatch.setenv("XIANYU_DEBOUNCE_NEXT", "0.3")
    monkeypatch.setenv("XIANYU_DEBOUNCE_SHORT", "0.1")  # 短 window = 100ms
    yield


class TestPunctDetection:
    """标点启发式 — 4 类典型场景"""

    def test_terminal_punct_uses_short_window(self):
        """句末标点结尾 → 用 short_window 100ms（提前触发）"""
        fired = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.5,
        )
        # 买家一句话，结尾 "?"  → 应该 100ms 内触发
        q.on_message({"cid": "c1", "text": "能充 MacBook 吗?"})
        time.sleep(0.2)
        assert len(fired) == 1, f"句末标点未提前触发: fired={fired}"
        assert fired[0][0] == "c1"
        assert fired[0][1][0]["text"] == "能充 MacBook 吗?"

    def test_no_punct_uses_full_window(self):
        """无标点结尾 → 走原 first_window（话可能没说完，不能 100ms 触发）"""
        fired = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.5,
        )
        q.on_message({"cid": "c1", "text": "你好我想咨询"})
        time.sleep(0.2)  # 200ms - 短 window 100ms 已过但应等原 window
        assert fired == [], f"无标点不应提前触发,但 fired={fired}"
        time.sleep(0.6)  # 总 800ms, 远超过 first=0.5s
        assert len(fired) == 1, f"应触发但未 fired={fired}"
        assert len(fired[0][1]) == 1  # 1 条消息

    def test_broken_message_no_punct_then_punct(self):
        """场景：买家分两条发 '你好' + '我想咨询'（无标点+无标点）→ 持续等"""
        fired = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.5,
        )
        q.on_message({"cid": "c1", "text": "你好"})
        time.sleep(0.1)
        q.on_message({"cid": "c1", "text": "我想咨询"})  # 累计，无标点
        time.sleep(0.2)
        assert fired == [], f"断句未完不应触发 fired={fired}"
        time.sleep(0.8)  # 给充足时间（next_window=0.3s, 但有 thread 调度延迟）
        assert len(fired) >= 1, f"应触发但未 fired={fired}"

    def test_broken_then_final_punct(self):
        """场景：'你好'（无）+ '充电宝'（无）+ '能充 MacBook 吗?' → 末句有标点 → 短触发"""
        fired = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.5,
        )
        q.on_message({"cid": "c1", "text": "你好"})
        time.sleep(0.05)
        q.on_message({"cid": "c1", "text": "充电宝"})
        time.sleep(0.05)
        q.on_message({"cid": "c1", "text": "能充 MacBook 吗?"})  # 句末标点
        time.sleep(0.15)  # 短 window = 100ms 应触发
        assert len(fired) == 1, f"末尾带标点应触发但未: fired={fired}"
        assert len(fired[0][1]) == 3  # 三段合并

    def test_middle_punct_does_not_shorten(self):
        """中间有逗号(不是句末) → 不提前触发"""
        fired = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.5,
        )
        q.on_message({"cid": "c1", "text": "你好，我想咨询下，"})
        time.sleep(0.2)  # 中间是逗号 → 不能 100ms 触发
        assert fired == [], "末尾是逗号不应短触发"

    def test_terminal_punct_types(self):
        """6 种句末标点都能识别"""
        for last_char, desc in [
            ("?", "英文问号"),
            ("？", "中文问号"),
            ("!", "英文感叹号"),
            ("！", "中文感叹号"),
            (".", "英文句号"),
            ("。", "中文句号"),
        ]:
            fired = []
            q = DebounceQueue(
                fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
                window_seconds=0.5,
            )
            q.on_message({"cid": "c1", "text": f"测试{last_char}"})
            time.sleep(0.2)
            assert len(fired) == 1, f"{desc} '{last_char}' 未触发"

    def test_short_window_env_override(self):
        """环境变量 XIANYU_DEBOUNCE_SHORT 控制短 window 时长"""
        os.environ["XIANYU_DEBOUNCE_SHORT"] = "0.05"
        fired = []
        # 注意：env 是 init 时读，需要 fresh queue
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.5,
        )
        q.on_message({"cid": "c1", "text": "在吗?"})
        time.sleep(0.1)
        assert len(fired) == 1
