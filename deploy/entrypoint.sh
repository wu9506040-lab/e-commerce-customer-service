#!/bin/bash
# 入口脚本: 容器首次启动时引导 QR 登录, 之后直接跑 daemon
set -e

COOKIE_PATH="/root/.goofish-cli/cookies.json"
LOG_FILE="/app/logs/xianyu_daemon_qr.log"

mkdir -p /root/.goofish-cli /app/logs

# 1. cookie 不存在 → 跑 do_qr_login.py (用户 docker exec 扫码)
if [ ! -f "$COOKIE_PATH" ]; then
    echo "===== 首次启动, 需要扫码登录 =====" | tee -a "$LOG_FILE"
    echo "请执行:" | tee -a "$LOG_FILE"
    echo "  docker exec -it $(cat /etc/hostname) python /app/do_qr_login.py" | tee -a "$LOG_FILE"
    echo "扫码完成后 cookie 会写到 $COOKIE_PATH" | tee -a "$LOG_FILE"
    echo "然后重启容器: docker restart \$(cat /etc/hostname)" | tee -a "$LOG_FILE"
    # 阻塞等 cookie 出现 (后台每 10s 检查)
    while [ ! -f "$COOKIE_PATH" ]; do
        sleep 10
    done
    echo "===== cookie 已就位, 启动 daemon =====" | tee -a "$LOG_FILE"
fi

# 2. 启动 daemon
echo "Starting xianyu daemon..." | tee -a "$LOG_FILE"
exec python -u /app/run_xianyu_daemon.py
