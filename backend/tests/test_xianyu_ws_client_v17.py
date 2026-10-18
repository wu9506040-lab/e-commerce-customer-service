"""
test_xianyu_ws_client_v17.py — V17 纯 Python WebSocket 客户端单元测试

不依赖 live cookie / live WebSocket，纯 mock。

覆盖：
1. 基础工具（trans_cookies / extract_token / generate_sign / generate_mid / generate_uuid / generate_device_id）
2. 异常分类（is_token_or_session_failure / is_network_connection_error）
3. Cookie 多源加载
4. XianyuApis（mock requests.Session）
5. XianyuWsClient（mock websockets.connect + 验证 callback 字段名是 "text" 不是 "send_message"）

V17 关键契约：
- callback event dict 用 "text" 字段（不是 "send_message"）—— 必须对齐 DebounceQueue.on_message
- /reg 用 app-key=444e9908a51d1cb236a27862abc769c9（不是 34839810）
- mtop sign 用 MD5(token&t&appKey&data) — appKey=34839810
"""
import unittest
from unittest.mock import patch, MagicMock

import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 让 import 走 backend 路径
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.xianyu.xianyu_ws_client import (
    XianyuWsClient,
    XianyuApis,
    trans_cookies,
    extract_token,
    generate_sign,
    generate_mid,
    generate_uuid,
    generate_device_id,
    load_cookie_header,
    is_token_or_session_failure,
    is_network_connection_error,
)


# ============================================================
# 基础工具测试
# ============================================================
class TestUtils(unittest.TestCase):
    def test_trans_cookies_basic(self):
        s = "_m_h5_tk=abc_def; cookie2=xyz; unb=12345"
        result = trans_cookies(s)
        self.assertEqual(result, {"_m_h5_tk": "abc_def", "cookie2": "xyz", "unb": "12345"})

    def test_trans_cookies_empty(self):
        self.assertEqual(trans_cookies(""), {})

    def test_trans_cookies_malformed(self):
        # shaxiu 原版行为：没有 = 的字段（"keyonly"）跳过，但空 key（"=empty"）会被接受为 {""}
        self.assertEqual(
            trans_cookies("keyonly; =empty; ok=val"),
            {"": "empty", "ok": "val"},
        )

    def test_extract_token_normal(self):
        self.assertEqual(extract_token("_m_h5_tk=abc_def"), "abc")
        self.assertEqual(extract_token("_m_h5_tk=abc"), "abc")

    def test_extract_token_missing(self):
        self.assertIsNone(extract_token(""))
        self.assertIsNone(extract_token("cookie2=xyz"))

    def test_generate_sign_format(self):
        sign = generate_sign("1741704675971", "abc123", '{"appKey":"a","deviceId":"d"}')
        self.assertEqual(len(sign), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in sign))

    def test_generate_sign_deterministic(self):
        """MD5 必须确定性（同输入同输出）"""
        s1 = generate_sign("t1", "tk1", "data1")
        s2 = generate_sign("t1", "tk1", "data1")
        self.assertEqual(s1, s2)

    def test_generate_sign_different_input(self):
        """不同输入必须不同输出"""
        s1 = generate_sign("t1", "tk1", "data1")
        s2 = generate_sign("t2", "tk1", "data1")
        self.assertNotEqual(s1, s2)

    def test_generate_mid_format(self):
        mid = generate_mid()
        # 格式: "{random_part}{timestamp} 0"
        self.assertIn(" 0", mid)
        parts = mid.split(" 0")[0]
        self.assertGreater(len(parts), 10)

    def test_generate_uuid_format(self):
        uuid_str = generate_uuid()
        # 格式: "-{timestamp}1"
        self.assertTrue(uuid_str.startswith("-"))
        self.assertTrue(uuid_str.endswith("1"))

    def test_generate_device_id_format(self):
        did = generate_device_id("123456789")
        # UUID v4 格式: 8-4-4-4-12 + -user_id
        parts = did.split("-")
        self.assertEqual(len(parts), 6)  # UUID 5 段 + user_id
        self.assertEqual(len(parts[0]), 8)
        self.assertEqual(len(parts[1]), 4)
        self.assertEqual(len(parts[2]), 4)
        self.assertEqual(len(parts[3]), 4)
        self.assertEqual(len(parts[4]), 12)
        self.assertEqual(parts[5], "123456789")

    def test_generate_device_id_deterministic_position14(self):
        """UUID v4 位置 14 必须是 '4'"""
        for _ in range(5):
            did = generate_device_id("u1")
            self.assertEqual(did[14], "4")


# ============================================================
# 异常分类测试（抄 zhinianboke 模式）
# ============================================================
class TestExceptionClassification(unittest.TestCase):
    def test_token_expired(self):
        e = Exception("FAIL_SYS_TOKEN_EXPIRED: invalid")
        self.assertTrue(is_token_or_session_failure(e))
        # 明确 Token 失效 → 不走网络退避
        self.assertFalse(is_network_connection_error(e))

    def test_session_expired(self):
        e = Exception("FAIL_SYS_SESSION_EXPIRED")
        self.assertTrue(is_token_or_session_failure(e))

    def test_invalid_token(self):
        e = Exception("TOKEN IS INVALID")
        self.assertTrue(is_token_or_session_failure(e))

    def test_network_connection_closed(self):
        e = ConnectionResetError("Connection reset by peer")
        self.assertFalse(is_token_or_session_failure(e))
        self.assertTrue(is_network_connection_error(e))

    def test_network_timeout(self):
        e = TimeoutError("read timeout")
        self.assertFalse(is_token_or_session_failure(e))
        self.assertTrue(is_network_connection_error(e))

    def test_windows_winerror_121(self):
        e = OSError("信号灯超时时间已到")
        e.winerror = 121
        self.assertTrue(is_network_connection_error(e))

    def test_no_close_frame(self):
        e = Exception("no close frame received or sent")
        self.assertTrue(is_network_connection_error(e))

    def test_token_priority_over_network(self):
        """Token 失效错误不应该被当成网络错误走指数退避"""
        e = Exception("FAIL_SYS_TOKEN_EXPIRED")
        self.assertTrue(is_token_or_session_failure(e))
        self.assertFalse(is_network_connection_error(e))


# ============================================================
# Cookie 多源加载测试
# ============================================================
class TestCookieLoad(unittest.TestCase):
    def test_env_var_priority(self):
        """环境变量 XIANYU_COOKIE_HEADER 优先级最高"""
        with patch.dict(os.environ, {"XIANYU_COOKIE_HEADER": "env_cookie=test"}):
            self.assertEqual(load_cookie_header(), "env_cookie=test")

    def test_env_var_with_whitespace_stripped(self):
        """env var 前后空白会被 strip"""
        with patch.dict(os.environ, {"XIANYU_COOKIE_HEADER": "  env_cookie=test  "}):
            self.assertEqual(load_cookie_header(), "env_cookie=test")

    def test_file_not_found_raises(self):
        """env 空 + 文件不存在 → 抛 FileNotFoundError"""
        with patch.dict(os.environ, {"XIANYU_COOKIE_HEADER": ""}):
            with patch("pathlib.Path.exists", return_value=False):
                with self.assertRaises(FileNotFoundError) as cm:
                    load_cookie_header()
                # 错误信息提示三个来源
                msg = str(cm.exception)
                self.assertIn("XIANYU_COOKIE_HEADER", msg)
                self.assertIn("智能客服", msg)


# ============================================================
# XianyuApis 测试（mock requests.Session）
# ============================================================
class TestXianyuApis(unittest.TestCase):
    def _make_apis(self) -> XianyuApis:
        return XianyuApis("_m_h5_tk=abc_def_token; unb=12345; cookie2=xyz")

    def test_init_loads_cookies(self):
        apis = self._make_apis()
        self.assertEqual(apis.cookies_str, "_m_h5_tk=abc_def_token; unb=12345; cookie2=xyz")
        # session.cookies 应该有 _m_h5_tk
        self.assertEqual(apis.session.cookies.get("_m_h5_tk"), "abc_def_token")

    def test_get_token_success(self):
        apis = self._make_apis()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"accessToken": "test_access_token_xyz"},
        }
        apis.session.post = MagicMock(return_value=mock_resp)

        result = apis.get_token("device-id-123")
        self.assertIsNotNone(result)
        self.assertEqual(result["data"]["accessToken"], "test_access_token_xyz")
        # post 必须带 sign 参数
        call_kwargs = apis.session.post.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        self.assertIn("sign", params)
        self.assertEqual(len(params["sign"]), 32)

    def test_get_token_risk_control(self):
        """RGV587_ERROR / 被挤爆啦 → 返 None"""
        apis = self._make_apis()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ret": ["FAIL::RGV587_ERROR::被挤爆啦"]}
        apis.session.post = MagicMock(return_value=mock_resp)

        result = apis.get_token("device-id-123")
        self.assertIsNone(result)

    def test_get_token_business_failure(self):
        """业务失败（非风控）→ 返 None"""
        apis = self._make_apis()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ret": ["FAIL::OTHER_ERROR"]}
        apis.session.post = MagicMock(return_value=mock_resp)

        result = apis.get_token("device-id-123")
        self.assertIsNone(result)

    def test_get_token_exception_returns_none(self):
        """网络异常 → 返 None（不抛）"""
        apis = self._make_apis()
        apis.session.post = MagicMock(side_effect=Exception("network error"))
        result = apis.get_token("device-id-123")
        self.assertIsNone(result)

    def test_get_token_missing_token(self):
        """Cookie 中没有 _m_h5_tk → 返 None"""
        apis = XianyuApis("unb=12345")  # 没有 _m_h5_tk
        result = apis.get_token("device-id-123")
        self.assertIsNone(result)

    def test_refresh_token_success(self):
        apis = self._make_apis()
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.json.return_value = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"accessToken": "refreshed_token"},
        }
        apis.session.post = MagicMock(return_value=mock_resp)

        result = apis.refresh_token()
        self.assertIsNotNone(result)
        self.assertEqual(result["data"]["accessToken"], "refreshed_token")

    def test_refresh_token_data_val_empty_brace(self):
        """refresh_token data_val 必须是 '{}'"""
        apis = self._make_apis()
        mock_resp = MagicMock()
        mock_resp.headers = {}
        mock_resp.json.return_value = {"ret": ["SUCCESS::调用成功"], "data": {}}
        apis.session.post = MagicMock(return_value=mock_resp)
        apis.refresh_token()
        call_data = apis.session.post.call_args.kwargs.get("data")
        self.assertEqual(call_data, {"data": "{}"})

    def test_extract_access_token(self):
        # V17.1 严格判空：必须 ret 成功 + accessToken 非空
        # 正常情况
        self.assertEqual(
            XianyuApis.extract_access_token(
                {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "xyz"}}
            ),
            "xyz",
        )
        # None 输入
        self.assertIsNone(XianyuApis.extract_access_token(None))
        # data 缺 accessToken（即便 ret 成功）
        self.assertIsNone(
            XianyuApis.extract_access_token(
                {"ret": ["SUCCESS::调用成功"], "data": {}}
            )
        )
        # data 非 dict
        self.assertIsNone(
            XianyuApis.extract_access_token(
                {"ret": ["SUCCESS::调用成功"], "data": "not_dict"}
            )
        )
        # V17.1 新增：accessToken 是空字符串（防御新版风控假 token）
        self.assertIsNone(
            XianyuApis.extract_access_token(
                {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": ""}}
            )
        )
        # V17.1 新增：accessToken 是 None
        self.assertIsNone(
            XianyuApis.extract_access_token(
                {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": None}}
            )
        )
        # V17.1 新增：ret 失败（即便有 token 字段也不算成功）
        self.assertIsNone(
            XianyuApis.extract_access_token(
                {"ret": ["FAIL::xxx"], "data": {"accessToken": "xyz"}}
            )
        )
        # V17.1 新增：ret 是字符串而非列表
        self.assertEqual(
            XianyuApis.extract_access_token(
                {"ret": "SUCCESS::调用成功", "data": {"accessToken": "abc"}}
            ),
            "abc",
        )

    def test_refresh_cookies(self):
        apis = self._make_apis()
        apis.refresh_cookies("_m_h5_tk=new_token_part; unb=99999")
        self.assertEqual(apis.session.cookies.get("_m_h5_tk"), "new_token_part")
        self.assertEqual(apis.session.cookies.get("unb"), "99999")


# ============================================================
# XianyuWsClient 测试（核心：验证 callback 字段名）
# ============================================================
class TestXianyuWsClient(unittest.TestCase):
    COOKIE_STR = "_m_h5_tk=abc_def; unb=123456789; cookie2=test"

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        self._tmp.write(self.COOKIE_STR)
        self._tmp.close()

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _make_client(self) -> XianyuWsClient:
        return XianyuWsClient(callback=lambda e: None, cookie_path=self._tmp.name)

    # ---------- 初始化 ----------
    def test_init_parses_cookie(self):
        client = self._make_client()
        self.assertEqual(client._unb, "123456789")
        self.assertIsNotNone(client._device_id)
        self.assertIsNone(client._access_token)
        self.assertFalse(client.is_alive())
        self.assertFalse(client._stop_event.is_set())

    def test_init_missing_unb_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("_m_h5_tk=abc")  # 没 unb
            bad = f.name
        try:
            with self.assertRaises(ValueError) as cm:
                XianyuWsClient(callback=lambda e: None, cookie_path=bad)
            self.assertIn("unb", str(cm.exception))
        finally:
            os.unlink(bad)

    # ---------- ⚠️ 关键：callback event dict 字段名是 'text' ----------
    def test_build_chat_event_uses_text_field(self):
        """⚠️ 关键契约：V17 双字段输出（text + send_message 同时存在，值一致）

        text 给 DebounceQueue.on_message 新契约；send_message 给旧 reply_scheduler 兼容。
        两个字段必须同时输出且值相等，否则下游消费者会断链。
        """
        client = self._make_client()
        inner_msg = {
            "1": {
                "2": "chat_abc@goofish",
                "5": int(time.time() * 1000),
                "10": {
                    "senderUserId": "buyer_999",
                    "reminderTitle": "买家昵称",
                    "reminderContent": "在吗",
                    "reminderUrl": "https://www.goofish.com/item.htm?itemId=ITEM_001&",
                },
            }
        }
        ev = client._build_chat_event(inner_msg)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["text"], "在吗")
        self.assertEqual(ev["cid"], "chat_abc")
        self.assertEqual(ev["send_user_id"], "buyer_999")
        self.assertEqual(ev["item_id"], "ITEM_001")
        self.assertEqual(ev["event"], "message")
        # V17 双字段兼容契约（参考 xianyu_ws_client.py:1257 注释）
        self.assertIn("send_message", ev)  # 别名必须存在（兼容旧 reply_scheduler）
        self.assertEqual(ev["send_message"], ev["text"])  # 双字段值必须一致

    # ---------- 消息分发：跳过卖家本人消息 ----------
    def test_handle_message_skips_self(self):
        events = []
        client = XianyuWsClient(
            callback=lambda e: events.append(e), cookie_path=self._tmp.name
        )
        msg = self._make_sync_msg(
            sender_user_id="123456789",  # = client._unb
            text="。",
            chat_id="chat_123@goofish",
            create_time_ms=int(time.time() * 1000),
        )
        client._handle_message(msg)
        self.assertEqual(len(events), 0)  # 卖家本人消息被跳过

    # ---------- 消息分发：买家消息 → callback ----------
    def test_handle_message_routes_buyer_chat(self):
        events = []
        client = XianyuWsClient(
            callback=lambda e: events.append(e), cookie_path=self._tmp.name
        )
        msg = self._make_sync_msg(
            sender_user_id="buyer_555",
            text="在吗",
            chat_id="chat_456@goofish",
            create_time_ms=int(time.time() * 1000),
            item_id="ITM_X",
        )
        client._handle_message(msg)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["text"], "在吗")
        self.assertEqual(ev["cid"], "chat_456")
        self.assertEqual(ev["send_user_id"], "buyer_555")
        self.assertEqual(ev["item_id"], "ITM_X")

    # ---------- 过期消息过滤 ----------
    def test_handle_message_skips_expired(self):
        events = []
        client = XianyuWsClient(
            callback=lambda e: events.append(e), cookie_path=self._tmp.name
        )
        old_time = int((time.time() - 600) * 1000)  # 10 分钟前
        msg = self._make_sync_msg(
            sender_user_id="buyer_777",
            text="过期消息",
            chat_id="chat_789@goofish",
            create_time_ms=old_time,
        )
        client._handle_message(msg)
        self.assertEqual(len(events), 0)

    # ---------- 系统订单消息 → 跳过 ----------
    def test_handle_message_skips_order_status(self):
        events = []
        client = XianyuWsClient(
            callback=lambda e: events.append(e), cookie_path=self._tmp.name
        )
        # 直接构造订单状态消息（不走 chat message 判定）
        order_msg = {
            "lwp": "/p/syncPushPackage/ackPush",
            "headers": {"mid": "123"},
            "body": [{
                "syncPushPackage": {
                    "data": [{
                        "data": base64.b64encode(json.dumps({
                            "3": {"redReminder": "等待买家付款"},
                            "1": "user_id@goofish",
                        }).encode()).decode()
                    }]
                }
            }]
        }
        client._handle_message(order_msg)
        self.assertEqual(len(events), 0)

    # ---------- 心跳响应识别 ----------
    def test_is_heartbeat_response(self):
        client = self._make_client()
        # 标准心跳响应
        hb = {"headers": {"mid": "123"}, "code": 200}
        self.assertTrue(client._is_heartbeat_response(hb))
        # 没 code
        self.assertFalse(client._is_heartbeat_response({"headers": {"mid": "123"}}))
        # 有 body → 不是心跳
        self.assertFalse(
            client._is_heartbeat_response({"headers": {"mid": "123"}, "code": 200, "body": [1]})
        )
        # 非 dict
        self.assertFalse(client._is_heartbeat_response("not dict"))

    # ---------- ACK 构造 ----------
    def test_build_ack_echoes_headers(self):
        client = self._make_client()
        msg = {
            "headers": {
                "mid": "abc",
                "sid": "sid_xyz",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "ua": "DingTalk UA",
                "dt": "j",
            },
            "code": 200,
        }
        ack = client._build_ack(msg)
        self.assertEqual(ack["lwp"], "/ack")
        self.assertEqual(ack["headers"]["code"], 200)
        self.assertEqual(ack["headers"]["mid"], "abc")
        self.assertEqual(ack["headers"]["sid"], "sid_xyz")
        self.assertEqual(ack["headers"]["app-key"], "444e9908a51d1cb236a27862abc769c9")
        self.assertEqual(ack["headers"]["ua"], "DingTalk UA")
        self.assertEqual(ack["headers"]["dt"], "j")

    # ---------- 重连延迟（指数退避 + 抖动） ----------
    def test_calculate_retry_delay_grows(self):
        client = self._make_client()
        d1 = client._calculate_retry_delay(1, "")
        d3 = client._calculate_retry_delay(3, "")
        d5 = client._calculate_retry_delay(5, "")
        # 至少不递减（容忍抖动）
        self.assertGreaterEqual(d3, d1 * 0.7)
        self.assertGreaterEqual(d5, d3 * 0.7)

    def test_calculate_retry_delay_cap(self):
        """no close frame 封顶 30s × 1.3 抖动"""
        client = self._make_client()
        d = client._calculate_retry_delay(20, "no close frame received")
        self.assertLessEqual(d, 30 * 1.3)

    # ---------- 智能判断 websockets 版本 ----------
    def test_resolve_header_kwarg(self):
        client = self._make_client()
        # 新版（≥14）— 有 additional_headers
        new_ws = MagicMock()
        new_ws.connect = lambda *a, **kw: None  # placeholder
        # 用 inspect 直接 mock：构造一个带 additional_headers 的函数签名
        def new_connect(additional_headers=None, **kwargs):
            pass
        new_ws.connect = new_connect
        self.assertEqual(client._resolve_header_kwarg(new_ws), "additional_headers")
        # 旧版（≤13）— 只有 extra_headers
        def old_connect(extra_headers=None, **kwargs):
            pass
        new_ws.connect = old_connect
        self.assertEqual(client._resolve_header_kwarg(new_ws), "extra_headers")

    # ---------- is_sync_package 判定 ----------
    def test_is_sync_package(self):
        client = self._make_client()
        self.assertTrue(client._is_sync_package({
            "body": [{"syncPushPackage": {"data": []}}]
        }))
        self.assertFalse(client._is_sync_package({"body": [{"other": "x"}]}))
        self.assertFalse(client._is_sync_package({"body": "not_list"}))
        self.assertFalse(client._is_sync_package({}))

    # ---------- is_chat_message 判定 ----------
    def test_is_chat_message(self):
        client = self._make_client()
        valid = {"1": {"10": {"reminderContent": "hi"}}}
        self.assertTrue(client._is_chat_message(valid))
        self.assertFalse(client._is_chat_message({}))
        self.assertFalse(client._is_chat_message({"1": {}}))
        self.assertFalse(client._is_chat_message({"1": {"10": {}}}))

    # ---------- stop() 是幂等的 ----------
    def test_stop_idempotent(self):
        client = self._make_client()
        # 没 start 就 stop 不崩
        client.stop()
        client.stop()  # 多次 stop 不崩

    # ---------- start() 后 is_alive() 返回 True ----------
    def test_start_stop_lifecycle(self):
        client = XianyuWsClient(
            callback=lambda e: None, cookie_path=self._tmp.name
        )
        self.assertFalse(client.is_alive())
        # 不能真连 ws（会爆），但能验证线程启动了
        # 用 patch mock websockets
        import unittest.mock as um
        with um.patch("websockets.connect", side_effect=Exception("mock")):
            client.start()
            time.sleep(0.5)  # 让线程启动
            # 线程跑 connect 失败 → 退到指数退避 → 仍然 alive
            self.assertTrue(client.is_alive())
            client.stop()
            self.assertFalse(client.is_alive())

    # ---------- 工具方法 ----------
    def _make_sync_msg(
        self,
        sender_user_id: str,
        text: str,
        chat_id: str,
        create_time_ms: int,
        item_id: str = "",
    ) -> dict:
        """构造一条 syncPushPackage chat message"""
        inner_payload = {
            "1": {
                "2": chat_id,
                "5": create_time_ms,
                "10": {
                    "senderUserId": sender_user_id,
                    "reminderTitle": "买家昵称",
                    "reminderContent": text,
                    "reminderUrl": (
                        f"https://www.goofish.com/item.htm?itemId={item_id}&"
                        if item_id else ""
                    ),
                },
            }
        }
        return {
            "lwp": "/p/syncPushPackage/ackPush",
            "headers": {"mid": "123"},
            "body": [{
                "syncPushPackage": {
                    "data": [{
                        "data": base64.b64encode(
                            json.dumps(inner_payload).encode()
                        ).decode()
                    }]
                }
            }],
        }


if __name__ == "__main__":
    unittest.main()