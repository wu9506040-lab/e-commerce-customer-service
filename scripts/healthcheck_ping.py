#!/usr/bin/env python3
"""
E 演示层快赢 #3：ECS 端定时 ping healthcheck.io 用的脚本

# 用法

1. 注册 healthcheck.io（免费）：
   - 访问 https://healthcheck.io
   - 新建 check，Name = "智能客服 ECS"
   - Period = 5 minutes
   - 得到 UUID（如 `a1b2c3d4-...`）

2. 填入环境变量：

   ```bash
   # ~/.bashrc 或 /etc/profile.d/healthcheck.sh
   export HEALTHCHECK_UUID="a1b2c3d4-5678-90ab-cdef-1234567890ab"
   export HEALTHCHECK_TARGET_URL="http://120.79.27.124:8000/health"
   ```

3. 部署到 ECS：

   ```bash
   scp scripts/healthcheck_ping.py aliyun:/opt/customer-service/scripts/
   ```

4. 添加 cron（每 5 分钟跑一次，避开 :00 / :05 整点避免惊群）：

   ```bash
   crontab -e
   # 智能客服健康上报
   3,8,13,18,23,28,33,38,43,48,53,58 * * * * /usr/bin/python3 /opt/customer-service/scripts/healthcheck_ping.py >> /var/log/healthcheck_ping.log 2>&1
   ```

5. 验证：手动跑一次

   ```bash
   python3 scripts/healthcheck_ping.py
   curl https://healthcheck.io/api/v3/checks/{UUID}/log  # 看最新 ping 时间
   ```

# 工作原理

- GET `$HEALTHCHECK_TARGET_URL`（默认 `http://120.79.27.124:8000/health`）
- 解析 status 字段：
  - `ok` → ping `https://hc-ping.com/{UUID}`（success）
  - `degraded` 或网络不通 → ping `https://hc-ping.com/{UUID}/fail`
- healthcheck.io 在连续 N 次没收到 ping 时发邮件/钉钉/微信告警
- 通过选择 "minutes=2, grace=5" 实现"漏 1 次不报警、漏 2 次报警"的容错

# 为什么不用 docker exec / 容器内 cron？

- 容器重建会丢 cron 配置（除非 mount 进 data volume）
- 容器内 cron 进程死了没人管（supervisord 配置复杂）
- ECS 主机层面 cron 更可靠（重启容器不影响）
- 5min 一次的 curl，CPU 几乎为 0，没必要跑容器
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# =============================================================
# 配置（全部走环境变量，方便不同 ECS 复用）
# =============================================================
DEFAULT_TARGET_URL = "http://120.79.27.124:8000/health"
DEFAULT_HC_BASE = "https://hc-ping.com"

HEALTHCHECK_UUID = os.environ.get("HEALTHCHECK_UUID", "").strip()
HEALTHCHECK_TARGET_URL = os.environ.get("HEALTHCHECK_TARGET_URL", DEFAULT_TARGET_URL)
HEALTHCHECK_BASE = os.environ.get("HEALTHCHECK_BASE", DEFAULT_HC_BASE)

# 请求超时：必须 < cron 周期（5 min），否则 cron 任务堆积
HTTP_TIMEOUT = float(os.environ.get("HEALTHCHECK_TIMEOUT", "10"))


def _log(msg: str) -> None:
    """stdout 一行（cron 会自动重定向到日志文件）"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def check_target(url: str) -> tuple[bool, str]:
    """GET 目标 URL，判断是否 ok

    Returns:
        (ok, detail)
        - ok=True: status="ok"
        - ok=False: 任意其他情况（含超时/4xx/5xx/degraded）
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return False, f"http_status={resp.status}"
            payload = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return False, f"non_json_response body={payload[:100]!r}"

            status = data.get("status")
            if status == "ok":
                return True, f"status=ok components={list(data.get('components', {}).keys())}"
            return False, f"status={status!r} body={json.dumps(data, ensure_ascii=False)[:200]}"

    except urllib.error.URLError as e:
        return False, f"url_error: {type(e).__name__}: {e}"
    except urllib.error.HTTPError as e:
        return False, f"http_error: {e.code} {e.reason}"
    except TimeoutError:
        return False, "timeout"
    except Exception as e:
        return False, f"unexpected: {type(e).__name__}: {e}"


def ping_healthcheck(ok: bool, detail: str) -> bool:
    """向 healthcheck.io 报告结果

    - ok=True  → GET {base}/{uuid}
    - ok=False → GET {base}/{uuid}/fail
    - 未配置 UUID → 仅打印，不调网络（dev 环境友好）

    Returns:
        上报成功 True / 失败 False
    """
    if not HEALTHCHECK_UUID:
        _log(f"[skip-ping] HEALTHCHECK_UUID 未配置，detail={detail}")
        return True  # 不算失败（dev 环境）

    suffix = "" if ok else "/fail"
    url = f"{HEALTHCHECK_BASE}/{HEALTHCHECK_UUID}{suffix}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status == 200:
                _log(f"[ping-ok] {url} ({detail})")
                return True
            _log(f"[ping-fail] {url} http_status={resp.status}")
            return False
    except Exception as e:
        _log(f"[ping-err] {url} {type(e).__name__}: {e}")
        return False


def main() -> int:
    started = time.monotonic()
    _log(f"start target={HEALTHCHECK_TARGET_URL} hc_uuid_set={bool(HEALTHCHECK_UUID)}")

    ok, detail = check_target(HEALTHCHECK_TARGET_URL)
    ping_ok = ping_healthcheck(ok, detail)

    elapsed = time.monotonic() - started
    _log(f"done ok={ok} ping_ok={ping_ok} elapsed={elapsed:.2f}s")

    # 退出码：健康 OK = 0，否则非 0（便于 cron 日志筛选）
    return 0 if (ok and ping_ok) else 1


if __name__ == "__main__":
    sys.exit(main())