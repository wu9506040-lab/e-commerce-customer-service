"""test_xianyu_ws_client.py - V16.8 P3 自实现 WS 客户端单元测试

V16.8 P3 设计: cookie 用 XIANYU_COOKIE_DIR env (默认 ~/.goofish-cli)
测试时用 monkeypatch.setenv("XIANYU_COOKIE_DIR", str(tmp_path)) 覆盖, 不依赖 Path.home
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.xianyu.xianyu_ws_client import (
    _build_ack,
    _generate_mid,
    _load_cookie_header,
    XianyuWsClient,
)


def _set_cookie_dir(monkeypatch, tmp_path):
    """把 XIANYU_COOKIE_DIR 设到 tmp_path（避免 Path.home patch 问题）"""
    monkeypatch.setenv("XIANYU_COOKIE_DIR", str(tmp_path))


# ============================================================
# _load_cookie_header
# ============================================================
class TestLoadCookieHeader:
    def test_env_var_priority(self, monkeypatch, tmp_path):
        """环境变量 XIANYU_COOKIE_HEADER 优先级最高"""
        monkeypatch.setenv(
            "XIANYU_COOKIE_HEADER",
            "unb=2218051380643; _m_h5_tk=abc; cookie2=xyz",
        )
        (tmp_path / "cookies.txt").write_text("fromtxt=1", encoding="utf-8")
        _set_cookie_dir(monkeypatch, tmp_path)
        assert _load_cookie_header() == "unb=2218051380643; _m_h5_tk=abc; cookie2=xyz"

    def test_cookies_txt(self, monkeypatch, tmp_path):
        _set_cookie_dir(monkeypatch, tmp_path)
        (tmp_path / "cookies.txt").write_text("unb=123; _m_h5_tk=tok", encoding="utf-8")
        assert _load_cookie_header() == "unb=123; _m_h5_tk=tok"

    def test_cookies_json_list(self, monkeypatch, tmp_path):
        _set_cookie_dir(monkeypatch, tmp_path)
        (tmp_path / "cookies.json").write_text(
            json.dumps([
                {"name": "unb", "value": "u1", "domain": ".goofish.com", "path": "/"},
                {"name": "_m_h5_tk", "value": "t1", "domain": ".goofish.com", "path": "/"},
            ]),
            encoding="utf-8",
        )
        assert _load_cookie_header() == "unb=u1; _m_h5_tk=t1"

    def test_cookies_json_dict_with_cookies(self, monkeypatch, tmp_path):
        _set_cookie_dir(monkeypatch, tmp_path)
        (tmp_path / "cookies.json").write_text(
            json.dumps({"cookies": [{"name": "unb", "value": "u1"}]}),
            encoding="utf-8",
        )
        assert _load_cookie_header() == "unb=u1"

    def test_not_found_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XIANYU_COOKIE_HEADER", raising=False)
        _set_cookie_dir(monkeypatch, tmp_path)
        # 目录存在但两个文件都不在
        with pytest.raises(FileNotFoundError, match="Cookie 未找到"):
            _load_cookie_header()

    def test_env_blank_falls_through(self, monkeypatch, tmp_path):
        """env 空白 → 走文件路径"""
        monkeypatch.setenv("XIANYU_COOKIE_HEADER", "   ")
        _set_cookie_dir(monkeypatch, tmp_path)
        (tmp_path / "cookies.txt").write_text("unb=t", encoding="utf-8")
        assert _load_cookie_header() == "unb=t"

    def test_cookies_dir_default_is_goofish_cli(self, monkeypatch):
        """不设 XIANYU_COOKIE_DIR → 默认 ~/.goofish-cli"""
        monkeypatch.delenv("XIANYU_COOKIE_DIR", raising=False)
        monkeypatch.delenv("XIANYU_COOKIE_HEADER", raising=False)
        # 默认 path 是 Path.home()/.goofish-cli 但文件不存在 → raise
        with pytest.raises(FileNotFoundError):
            _load_cookie_header()


# ============================================================
# _build_ack
# ============================================================
class TestBuildAck:
    def test_basic_ack(self):
        msg = {"headers": {"mid": "abc123"}}
        ack = _build_ack(msg)
        assert ack["lwp"] == "/ack"
        assert ack["headers"]["code"] == 200
        assert ack["headers"]["mid"] == "abc123"

    def test_no_mid_uses_random(self):
        ack = _build_ack({})
        assert ack["headers"]["mid"]


# ============================================================
# XianyuWsClient 接口
# ============================================================
class TestXianyuWsClientInterface:
    def test_init_loads_cookie(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XIANYU_COOKIE_HEADER", "k=v")
        c = XianyuWsClient(callback=lambda ev: None)
        assert c._cookie_header == "k=v"
        assert c.is_alive() is False
        assert c._start_time > 0

    def test_init_no_cookie_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XIANYU_COOKIE_HEADER", raising=False)
        _set_cookie_dir(monkeypatch, tmp_path)
        with pytest.raises(FileNotFoundError):
            XianyuWsClient(callback=lambda ev: None)

    def test_start_stop_lifecycle(self, monkeypatch):
        monkeypatch.setenv("XIANYU_COOKIE_HEADER", "k=v")
        c = XianyuWsClient(callback=lambda ev: None)
        c.start()
        try:
            assert c._thread is not None
            assert c._thread.is_alive() is True
        finally:
            c.stop()
        assert c._thread is None


# ============================================================
# _parse_one
# ============================================================
class TestParseOne:
    def _client(self):
        os.environ["XIANYU_COOKIE_HEADER"] = "k=v"
        return XianyuWsClient(callback=lambda ev: None)

    def test_basic_msg(self):
        c = self._client()
        ev = c._parse_one({
            "cid": "40703180655",
            "sendUserId": "9",
            "sendUserName": "卖家小助手",
            "message": "在吗",
            "createTime": 1792131360249,
        })
        assert ev["event"] == "message"
        assert ev["cid"] == "40703180655"
        assert ev["send_user_id"] == "9"
        assert ev["send_user_name"] == "卖家小助手"
        assert ev["send_message"] == "在吗"
        assert ev["ts"] == 1792131360249

    def test_nested_data(self):
        c = self._client()
        ev = c._parse_one({
            "data": {
                "cid": "123",
                "sendUserId": "u",
                "sendUserName": "n",
                "message": "msg",
                "createTime": 1,
            }
        })
        assert ev["cid"] == "123"
        assert ev["send_message"] == "msg"

    def test_no_cid_returns_none(self):
        c = self._client()
        assert c._parse_one({"message": "x"}) is None
        assert c._parse_one({}) is None
        assert c._parse_one(None) is None
        assert c._parse_one("not_dict") is None

    def test_alternative_field_names(self):
        c = self._client()
        ev = c._parse_one({
            "cid": "x",
            "fromUserId": "u_alt",
            "fromUserName": "n_alt",
            "content": "msg_alt",
            "ts": 99,
        })
        assert ev["send_user_id"] == "u_alt"
        assert ev["send_user_name"] == "n_alt"
        assert ev["send_message"] == "msg_alt"
        assert ev["ts"] == 99
