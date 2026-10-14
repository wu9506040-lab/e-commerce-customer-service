# XianyuDaemon 部署指南

## Windows（用 NSSM）

### 前置
1. 安装 [NSSM](https://nssm.cc/)（推荐 2.24+），把 `nssm.exe` 路径加到 PATH
3. 装 Python 3.11+，加到 PATH
3. 装依赖：`pip install -r backend/requirements.txt`
4. 导入闲鱼 cookie：`goofish auth login <cookies.json>`

### 安装
管理员权限打开 CMD：
```cmd
cd E:\智能客服
scripts\xianyu\install_service.bat
```

### 常用命令
```cmd
nssm status XianyuDaemon          # 查看状态
nssm stop XianyuDaemon            # 停止
nssm restart XianyuDaemon         # 重启
scripts\xianyu\uninstall_service.bat   # 卸载
type logs\xianyu_daemon.log       # 看日志
curl http://localhost:8000/admin/xianyu/health  # 健康检查
```

### 配置（环境变量）
可在 `install_service.bat` 里改：
- `XIANYU_CHAT_API_URL`：智能客服 /api/chat 地址
- `XIANYU_MIN_DELAY` / `XIANYU_MAX_DELAY`：回复延迟区间（默认 30-180s）
- `XIANYU_DEBOUNCE_WINDOW`：断句静默期（默认 30s）
- `XIANYU_LOG_DIR` / `XIANYU_STATE_FILE`：日志/状态文件路径

## Linux（systemd）

`/etc/systemd/system/xianyu-daemon.service`：
```ini
[Unit]
Description=智能客服 闲鱼 IM 自动化 Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/customer-service
ExecStart=/usr/bin/python3 /opt/customer-service/scripts/run_xianyu_daemon.py
Restart=always
RestartSec=10
User=customer-service
Environment="ENABLE_XIANYU_TOOLS=true"
Environment="PYTHONIOENCODING=utf-8"
Environment="XIANYU_CHAT_API_URL=http://localhost:8000/api/chat"
Environment="XIANYU_MIN_DELAY=30"
Environment="XIANYU_MAX_DELAY=180"
StandardOutput=append:/var/log/xianyu/daemon.log
StandardError=append:/var/log/xianyu/daemon.err.log

[Install]
WantedBy=multi-user.target
```

启用：
```bash
sudo systemctl daemon-reload
sudo systemctl enable xianyu-daemon
sudo systemctl start xianyu-daemon
sudo systemctl status xianyu-daemon
```

## Docker（可选）

```dockerfile
# 复用 backend/Dockerfile，加：
COPY scripts /app/scripts
ENV ENABLE_XIANYU_TOOLS=true
CMD ["python", "/app/scripts/run_xianyu_daemon.py"]
```

⚠️ Docker 内跑 daemon 需要容器 `--restart=always` + cookie 走 volume mount。

## 健康检查端点

后端 FastAPI 需要加（V16.1.6 待做）：
```
GET /admin/xianyu/health
```
返：
```json
{
  "watcher_alive": true,
  "cookie_valid": true,
  "debounce_pending_count": 0,
  "sent_today": 12,
  "daily_quota_remaining": 38,
  "errors_count": 0,
  "should_alert": false,
  "issues": []
}
```

## 告警

`XianyuHealth.should_alert()` 在连续 3 错时返 True。当前默认打 log；后续接飞书 / 邮件 webhook（V16.2）。

## 故障排查

| 现象 | 查 | 解决 |
|------|----|------|
| 服务起不来 | `logs/xianyu_daemon.err.log` | 看 Python 错误栈 |
| 收不到消息 | `logs/xianyu_daemon.log` 看 watcher | 重新 `goofish auth login` |
| 不回复 | `health.errors_count` + `last_error` | 看 `/admin/xianyu/health` |
| 限流 | `daily_quota_exhausted` 状态 | 调高 daily_quota 或等 24h |
