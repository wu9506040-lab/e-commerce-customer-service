# v18.3.0 — 企业级 AI 客服中台

> 首个正式 Release。从闲鱼自动回复脚本演进为企业级多渠道 AI 客服中台。

## ✨ 核心能力

### 多账号管理
- `AccountManager + AccountWorker × N` 隔离架构：每账号独立 WebSocket 连接、回复调度、Cookie 自愈线程，单账号故障不互相影响
- 自家账号互发防死循环（own_unbs 判定，只落库不触发 AI）
- 账号管理页：连接状态灯 / 启停 / 重启 / 注册表热重载

### 人工坐席工作台（三栏）
- 会话列表（渠道标签 · 未读红点 · 三色状态标签）+ 对话窗口 + 客户信息
- AI（浅蓝）/ 人工（深蓝）/ 买家（灰）三色气泡 + 角标区分
- 一键接管 / 恢复 AI、人工直发（daemon WebSocket 通道）、快捷短语芯片

### 转人工工单闭环
- 买家提退款 → 自动扫描会话内交易卡片判定订单状态（有单建 P1 工单 / 无单直接告知）
- 平台直接申请退款（卡片识别）→ P0 工单 + 工作台强通知
- 工单摘要可读化：诉求 + 订单状态 + 最近对话，人工一眼看懂

### 数据看板
- ECharts 11 图：KPI 卡 / 消息量趋势 / 渠道分布 / 峰值时段 / 响应时间 / 热门问题 TOP10 / 意图分类 / 三仪表盘 / 客户统计

### 外发通知
- 5 渠道：钉钉（加签）/ 企业微信 / 飞书 / WxPusher / 通用 webhook
- 配置文件热加载，300s 冷却防轰炸，P0 事件绕过冷却必达

### 向量数据库迁移（Qdrant → Milvus）
- 门面模式双栈并存：`VECTOR_DB=qdrant|milvus` 环境开关，一条命令回滚
- collection / payload 字段 / 增量入库 / 检索管线完全兼容，零停机切换

## 🔧 工程化

- **CI 首绿**：GitHub Actions 从 128 次全红修复到全绿（1139 测试通过 / 覆盖率门禁 / ruff 零告警）
- 修复 48 个失败/悬挂测试、20 处潜在 NameError、测试间环境污染
- 敏感信息隔离：cookie / 账号表 / 通知配置全部 gitignore + example 模板

## 📦 部署

```bash
cd deploy && docker compose --env-file .env.dev up -d
cd frontend && npm run build
python scripts/ingest_kb.py        # 知识库入库（Milvus）
python scripts/watchdog_xianyu_daemon.py
```
