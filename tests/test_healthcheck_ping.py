"""
E - healthcheck_ping.py 单测

覆盖：
1. 目标 URL ok → ping success URL（无 /fail 后缀）
2. 目标 URL degraded → ping /fail URL
3. 目标 URL 网络错 → ping /fail URL
4. HEALTHCHECK_UUID 未配置 → 仅 log，不发请求
5. 退出码：ok=0, fail=1
6. ping URL 格式正确（success 路径不带 /fail，failure 路径带 /fail）
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

# 让脚本能被 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import healthcheck_ping

# 测试用 UUID（fake，但满足 url 拼接需要）
TEST_UUID = "a1b2c3d4-5678-90ab-cdef-1234567890ab"


def _setup_uuid():
    """每个测试前设置 module 内的 UUID（因为模块 import 时已读取 env）"""
    original = healthcheck_ping.HEALTHCHECK_UUID
    healthcheck_ping.HEALTHCHECK_UUID = TEST_UUID
    return original


def _restore_uuid(original):
    healthcheck_ping.HEALTHCHECK_UUID = original


def _mock_response(status: int = 200, payload: dict | None = None):
    """构造 mock urllib response"""
    resp = MagicMock()
    resp.status = status
    body = json.dumps(payload or {}).encode("utf-8")
    resp.read = lambda: body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def test_ok_target_pings_success_url():
    """场景 1：目标 ok → 拼出无 /fail 后缀的 URL"""
    original = _setup_uuid()
    try:
        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                _mock_response(200, {"status": "ok"}),  # check_target
                _mock_response(200),                     # ping success
            ]

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = healthcheck_ping.main()

        assert rc == 0, f"ok 路径应返 0，实际 {rc}"
        assert mock_urlopen.call_count == 2, f"应调 2 次 urlopen，实际 {mock_urlopen.call_count}"

        # 第二次调用是 ping
        ping_call = mock_urlopen.call_args_list[1]
        req = ping_call.args[0]
        assert "/fail" not in req.full_url, f"success 不应带 /fail: {req.full_url}"
        assert TEST_UUID in req.full_url
        print(f"PASS: ok 路径 → ping success URL ({req.full_url})")
    finally:
        _restore_uuid(original)


def test_degraded_target_pings_fail_url():
    """场景 2：目标 degraded → 拼出带 /fail 后缀的 URL"""
    original = _setup_uuid()
    try:
        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                _mock_response(200, {"status": "degraded", "components": {"mysql": {"status": "down"}}}),
                _mock_response(200),
            ]

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = healthcheck_ping.main()

        assert rc == 1, f"degraded 应返 1，实际 {rc}"
        ping_call = mock_urlopen.call_args_list[1]
        req = ping_call.args[0]
        assert req.full_url.endswith("/fail"), f"degraded 应带 /fail: {req.full_url}"
        assert TEST_UUID in req.full_url
        print(f"PASS: degraded 路径 → ping /fail URL ({req.full_url})")
    finally:
        _restore_uuid(original)


def test_network_error_pings_fail_url():
    """场景 3：网络错（URLError） → 不调 ping（因为 ok=False 且 UUID 也没用上）/或 ping /fail 后失败"""
    import urllib.error

    original = _setup_uuid()
    try:
        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            # 全部 URLError
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = healthcheck_ping.main()

        assert rc == 1, f"网络错应返 1，实际 {rc}"
        output = buf.getvalue()
        # check_target 内部 catch URLError 不打印，只 log detail 返回 (False, ...)
        # ping_healthcheck 调 urlopen 又 URLError → log "ping-err"
        assert "ping-err" in output, f"应记录 ping-err，实际: {output}"
        # 应拼出 /fail URL（即使最终 ping 也失败，URL 拼接应发生过）
        assert "/fail" in output, f"应拼 /fail URL，实际: {output}"
        print(f"PASS: 网络错 → 退出码 1 + log ping-err + /fail URL")
    finally:
        _restore_uuid(original)


def test_no_uuid_skips_ping():
    """场景 4：HEALTHCHECK_UUID 未配置 → 仅 log，不发 ping 请求"""
    original = healthcheck_ping.HEALTHCHECK_UUID
    healthcheck_ping.HEALTHCHECK_UUID = ""
    try:
        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response(200, {"status": "ok"})

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = healthcheck_ping.main()

        assert rc == 0, f"skip-ping 仍应 0，实际 {rc}"
        # urlopen 只被调 1 次（check_target），ping 跳过
        assert mock_urlopen.call_count == 1, f"应只调 1 次 urlopen，实际 {mock_urlopen.call_count}"
        assert "skip-ping" in buf.getvalue()
        print(f"PASS: 无 UUID → skip-ping，仅 log")
    finally:
        _restore_uuid(original)


def test_http_500_marks_degraded():
    """场景 5：目标返 500 → 视为 degraded → 拼 /fail"""
    original = _setup_uuid()
    try:
        # mock_resp status=500（read 空）
        mock_resp_500 = MagicMock()
        mock_resp_500.status = 500
        mock_resp_500.read = lambda: b""
        mock_resp_500.__enter__ = lambda s: s
        mock_resp_500.__exit__ = lambda s, *a: False

        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                mock_resp_500,                    # check_target 收到 500
                _mock_response(200),              # ping /fail
            ]
            rc = healthcheck_ping.main()

        assert rc == 1
        # 第二次调用 URL 应带 /fail
        second_url = mock_urlopen.call_args_list[1].args[0].full_url
        assert second_url.endswith("/fail"), f"应带 /fail: {second_url}"
        print(f"PASS: HTTP 500 → /fail")
    finally:
        _restore_uuid(original)


def test_check_target_returns_correct_status():
    """场景 6：check_target 工具函数直接验证"""
    with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, {"status": "ok", "components": {"mysql": {"status": "ok"}}})
        ok, detail = healthcheck_ping.check_target("http://example.com/health")

    assert ok is True
    assert "status=ok" in detail

    with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, {"status": "degraded"})
        ok, detail = healthcheck_ping.check_target("http://example.com/health")

    assert ok is False
    assert "degraded" in detail
    print(f"PASS: check_target 分类正确")


def test_exit_code_mapping():
    """场景 7：退出码映射（业务约定）"""
    original = _setup_uuid()
    try:
        # 场景 A: ok=True, ping ok → 0
        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_mock_response(200, {"status": "ok"}), _mock_response(200)]
            assert healthcheck_ping.main() == 0

        # 场景 B: ok=False（degraded）, ping ok → 1
        with patch.object(healthcheck_ping.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [_mock_response(200, {"status": "degraded"}), _mock_response(200)]
            assert healthcheck_ping.main() == 1

        print(f"PASS: 退出码映射正确（ok=0, fail=1）")
    finally:
        _restore_uuid(original)


if __name__ == "__main__":
    test_ok_target_pings_success_url()
    test_degraded_target_pings_fail_url()
    test_network_error_pings_fail_url()
    test_no_uuid_skips_ping()
    test_http_500_marks_degraded()
    test_check_target_returns_correct_status()
    test_exit_code_mapping()
    print("\nALL 7 SCENARIOS PASSED")