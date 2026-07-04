# ECS 健康监控接入指南（healthcheck.io）

> 让面试官/HR 看到 README 上有一颗绿色"在线"徽章 = 整个系统真的在 7×24 小时活着。
> 比纯截图证明"我跑过 demo"更可信。

---

## 1. 5 分钟接入

### 步骤 1：注册 healthcheck.io

1. 访问 https://healthcheck.io
2. 用邮箱注册（**免费版** 20 个 check 足够）
3. 右上角 **+ Add check**
   - **Name**: `智能客服 ECS`
   - **Period**: `5 minutes`
   - **Grace**: `5 minutes`（漏 1 次不报警，漏 2 次才报警，避免单次抖动误报）
   - **Channels**: 勾选 Email（推荐） + 其他你想要的
4. 创建后得到一个 UUID，形如 `a1b2c3d4-5678-90ab-cdef-1234567890ab`

### 步骤 2：在 ECS 配置环境变量

```bash
ssh aliyun

sudo nano /etc/profile.d/healthcheck.sh
# 写入（替换为真实 UUID）：
export HEALTHCHECK_UUID="a1b2c3d4-5678-90ab-cdef-1234567890ab"
export HEALTHCHECK_TARGET_URL="http://120.79.27.124:8000/health"

source /etc/profile.d/healthcheck.sh
```

### 步骤 3：上传脚本到 ECS

```bash
# 本机执行
scp scripts/healthcheck_ping.py aliyun:/tmp/

# ECS 上
ssh aliyun
sudo mkdir -p /opt/customer-service/scripts
sudo mv /tmp/healthcheck_ping.py /opt/customer-service/scripts/
sudo chmod +x /opt/customer-service/scripts/healthcheck_ping.py
```

### 步骤 4：配置 cron（ECS 主机层，不在容器内）

```bash
ssh aliyun
crontab -e

# 添加（避开 :00 / :05 整点避免多实例惊群）：
3,8,13,18,23,28,33,38,43,48,53,58 * * * * /usr/bin/python3 /opt/customer-service/scripts/healthcheck_ping.py >> /var/log/healthcheck_ping.log 2>&1
```

### 步骤 5：手动验证

```bash
ssh aliyun
# 跑一次
python3 /opt/customer-service/scripts/healthcheck_ping.py
# 期望输出：
# [2026-XX-XXTXX:XX:XXZ] start target=http://120.79.27.124:8000/health hc_uuid_set=True
# [2026-XX-XXTXX:XX:XXZ] [ping-ok] https://hc-ping.com/a1b2c3d4-... (...status=ok...)
# [2026-XX-XXTXX:XX:XXZ] done ok=True ping_ok=True elapsed=0.0Xs
```

去 healthcheck.io 后台 → 你的 check → **Log** tab，应看到刚跑的 ping 记录。

---

## 2. README 状态徽章（可选但推荐）

拿到 UUID 后，替换 README.md 中的占位符：

```diff
- ![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fhealthchecks.io%2Fapi%2Fv3%2Fchecks%2FYOUR-UUID-HERE%2Fbadge&...)
+ ![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fhealthchecks.io%2Fapi%2Fv3%2Fchecks%2Fa1b2c3d4-...%2Fbadge&...)
```

healthcheck.io 提供官方 JSON endpoint，shields.io 直接代理渲染：

- `ok` → 绿色 "✓ up"
- 漏 ping → 红色 "✗ down"
- grace 期 → 灰色 "✝ pending"

---

## 3. 工作原理

```
ECS (120.79.27.124)                    healthcheck.io                    README 访客
─────────────                          ────────────────                  ────────────
cron 每 5min ─→ healthcheck_ping.py
                  ├─ GET 120.79.27.124:8000/health  ─→ FastAPI /health 探测 mysql/redis/qdrant
                  │   ↓ status="ok"
                  ├─ GET https://hc-ping.com/{UUID}  ──→ healthcheck.io 收到 "alive"
                  │                                                              ↓
                  │                                                       shields.io 拉状态
                  │                                                              ↓
                  └─ 失败：GET https://hc-ping.com/{UUID}/fail  ──→ 触发告警      ← 访客看到 🟢/🔴
```

---

## 4. 故障演练（验证告警链路）

### 4.1 模拟 API 挂掉

```bash
ssh aliyun
docker stop customer-service-api
# 5 min 后 healthcheck_ping 探测 /health 失败 → ping /fail
# 5+5=10 min 后 healthcheck.io 发邮件告警
docker start customer-service-api
```

### 4.2 模拟 MySQL 挂掉（MySQL 挂则 /health 返回 degraded）

```bash
ssh aliyun
docker stop customer-service-mysql
# 下次 cron：探测到 status="degraded" → ping /fail → 触发告警
docker start customer-service-mysql
```

### 4.3 手动 ping 一次（debug 用）

```bash
curl https://hc-ping.com/YOUR-UUID-HERE          # 标记 alive
curl https://hc-ping.com/YOUR-UUID-HERE/fail     # 标记 failed
```

---

## 5. 为什么不用其他方案？

| 方案 | 缺点 | healthcheck.io 优势 |
|------|------|-------------------|
| UptimeRobot | 免费只 50 个 check，需要绑域名 | **免域名**，纯 IP 即可 |
| Prometheus + Grafana | 重型，本项目只有 1 个 ECS | **零基础设施**，直接调 HTTPS |
| 自写监控脚本 | 缺通知渠道，cron 失败无感知 | **现成 Email/Slack/钉钉/微信** 集成 |
| Docker 容器内 cron | 容器重建丢 cron、cron 死了没人管 | **ECS 主机层 cron**，与容器解耦 |
| 容器 healthcheck（已有） | 只重启容器，不发外网告警 | **+ 现有 healthcheck 互补**：内层重启，外层告警 |

---

## 6. 脚本说明

- 入口：`scripts/healthcheck_ping.py`（纯标准库 urllib，零外部依赖）
- 测试：`tests/test_healthcheck_ping.py`（7 场景：ok/degraded/network/无 UUID/HTTP 500/工具函数/退出码）
- 复用：
  - ECS 监控（本文）
  - 多 ECS（每个 ECS 一个独立 UUID + 同样的 cron）
  - 其他服务（改 `HEALTHCHECK_TARGET_URL` 即可复用脚本）

---

## 7. 简历/面试场景话术

> "这个 README 上的绿点是真实的——我在阿里云 ECS 上跑了一个 5 分钟周期的健康上报脚本（`scripts/healthcheck_ping.py`），探活整个 FastAPI 服务的 MySQL/Redis/Qdrant 三件套。一旦 ECS 挂了，5 分钟内我邮箱会收到告警。"

这是简历项目里 **"持续运营"** 的最直接证据，比任何 mock 数据都有说服力。