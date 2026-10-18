# 参考项目吸收笔记（E:\cankao 两个项目）

> 来源：XianyuAutoAgent-main（多 Agent 分诊）、xianyu-auto-reply-main（工程化完整链路）
> 用途：实现任何协议/回复/编排功能前，先对照本文与对应源码，**逐字段抄，不要凭感觉包装**。
> 2026-10-17 整理。已落地的条目标 ✅。

## 1. 消息接收入口（XianyuAutoAgent main.py / xianyu-auto-reply message_handler.py）

| 机制 | 参考实现 | 我们的状态 |
|------|----------|-----------|
| ACK 帧 | `{"code":200, "headers":{mid,sid,app-key,ua,dt}}`，**code 顶层、无 lwp**；注释："必须发送否则服务器会断开连接" | ✅ 已照抄（此前抄错成 lwp 包装，导致消息延迟 3 分钟） |
| 订单状态消息 | `message['3']['redReminder']`（等待买家付款/交易关闭/等待卖家发货）→ 打日志跳过 | ✅ 已有同款 |
| 输入状态消息 | `message["1"]` 是 list 且含 `@goofish` → "正在输入"，跳过 | ✅ 等价处理 |
| 系统消息 | `message["3"]["needPush"]=="false"` → 跳过 | ✅ 已有同款 |
| 中括号系统消息 | 文本以 `[` 开头 `]` 结尾（[去支付]/[去评价]/[信息卡片]）→ **不回复** | ⬜ 待落地（P1） |
| MsgTips 系统提示 | extJson.msgArg1=="MsgTips"（"打气"任务等营销消息）→ 跳过 | ⬜ 待落地（P1） |
| 5 分钟过期过滤 | `create_time` 超 5 分钟丢弃 | ✅ 已有 |
| 轻量通知（只有 PNM 指针无内容）| 参考项目静默跳过（同样有丢消息隐患）| ✅ 我们额外做了 ackDiff 补拉（增强）|
| 卖家消息 | 记入上下文（role=assistant）——**自动回复能学到卖家人工说的话** | ⬜ 我们直接跳过不记录（待 P2） |
| 人工接管模式 | 卖家发切换关键词（默认"。"）→ 该会话 1h 内不自动回复，超时自动恢复 | ⬜ 待 P2（人工兜底刚需）|

## 2. 分诊与多 Agent（XianyuAgent.py）

| 机制 | 参考实现 | 吸收要点 |
|------|----------|---------|
| 三级路由 | 关键词/正则（tech 优先→price）→ 未命中才 LLM 分类 | ✅ 我们 intent.yaml 同思路；注意他们 tech 优先于 price |
| no_reply 意图 | 分类出 no_reply → 返回 `"-"` 不回复。prompt 明确列出：`[去创建合约]` 等系统消息、"你是谁/什么模型"等无关问题、命令性/角色假设问题 | ⬜ P1：yaml 加规则 + daemon 约定 `[skip]` 不发送 |
| 分 Agent prompt | price/tech/default 各自独立小 prompt；**每个都写死"每句≤10字，总字数≤40字"** | ⬜ P1：按意图短 prompt（我们 yaml 是全场景大 prompt）|
| 议价动态温度 | PriceAgent：`temp = min(0.3 + 轮次×0.15, 0.9)`；议价轮次存 SQLite `chat_bargain_counts`（chat_id 主键）| ⬜ P2 |
| 议价梯度策略 | prompt 写明：第1次小幅/第2次中等/第3次最大优惠，接近上限守底线强调价值 | ⬜ P2 |
| TechAgent | temp 0.4 + `enable_search` 联网兜底 | 可选 |
| 消息链结构 | system = 【商品信息】+【你与客户的对话历史】+ 专属 prompt；买家消息单独 user 条 | 参考我们 /chat 的 session 历史，等价 |
| 安全过滤 | 回复含 微信/QQ/支付宝/银行卡/线下 → 整条替换"[安全提醒]请通过平台沟通" | ⬜ P1（防封号）|
| LLM 参数 | max_tokens=500, top_p=0.8, temp 0.4-0.7 | 我们 max_tokens 未设 → 输出过长，已在 system_base 加 80 字约束 |

## 3. 上下文与商品缓存（context_manager.py）

| 机制 | 参考实现 | 我们的状态 |
|------|----------|-----------|
| 会话历史 | SQLite messages 表（chat_id 索引，max 100 条滚动删除）| ✅ /chat 服务端 session 记忆 |
| 议价计数 | `chat_bargain_counts` 表 UPSERT；注入 context 为 system 消息"议价次数: N" | ⬜ P2 |
| 商品信息缓存 | items 表存 itemDO 全量 JSON，API 只拉一次 | ⬜ P2（我们每次 RAG）|

## 4. 发送与拟人（main.py 尾部）

- 打字模拟（默认关）：`0-1s 基础 + 每字×0.1-0.3s`，上限 10s——回复越长等越久
- 我们用固定 0-3s 随机延迟，够用；打字模拟可作为可选开关（默认关，同参考）

## 5. 工程质量（xianyu-auto-reply ai_reply_engine.py）

| 机制 | 参考实现 | 我们的状态 |
|------|----------|-----------|
| per-chat 锁 | asyncio.Lock + 2h 过期清理 + 上限 1w | ⬜ P1：补过期清理（防泄漏）|
| 消息去重 | is_message_processed（持久化）| ✅ 内存 set + message_id（重启丢失，可接受）|
| provider 容错 | OpenAI/DashScope/Gemini fallback 链 | 单 provider，暂不需要 |
| 回复字数 | 每类 prompt 明确 ≤40 字 | ✅ 已加 ≤80 字约束（可再收紧）|

## 6. 落地清单

- [x] ACK 格式照抄（2026-10-17）
- [x] ackDiff 补拉（轻量通知 → 主动拉取，参考项目没有的增强）
- [x] P1：中括号系统消息 / MsgTips 过滤（daemon 侧）
- [x] P1：安全过滤（微信号等 → 替换安全提醒）
- [x] P1：no_reply 约定（yaml 指令 + daemon `[skip]` 不发送）
- [x] P1：per-cid 锁过期清理
- [x] P2：议价轮次表 + 梯度让步 prompt（动态温度待 /chat 支持透传）
- [x] P2：人工接管模式（切换关键词 + 1h 超时）
- [x] P2：商品信息缓存
- [x] P3：画像 mtop 化完成（MtopProfileClient + session.sync 实测 10 会话）
- [x] L3 Playwright 免密刷新（cookie_browser_refresh.py，实弹成功修复 ILLEGAL）

## 7. Cookie 生命周期四层模型（goofish_cli/core/refresh.py 文档精髓）

| 层 | 机制 | 有效期/触发 | 我们的状态 |
|----|------|------------|-----------|
| L1 | mtop Set-Cookie 自续期（活跃调用时服务端下发新 _m_h5_tk） | _m_h5_tk 仅 **10 分钟**有效，须"活跃访问"续期 | ✅ token 自愈重试 |
| L2 | HTTP passport 续期（hasLogin/silentHasLogin/setLoginSettings） | 每 180s | ✅ 已有 |
| L3 | Playwright 免密登录：goto 首页 → passport 弹窗点「快速进入」→ goto /bought 强鉴权页 → 抓完整 session cookie | L1/L2 层失效（TOKEN_EXOIRED/SESSION_EXPIRED）时自动触发，用户无感 | ⬜ 未引入（需 Playwright 依赖） |
| L4 | 人工：扫码 / F12 重抓 | L3 也失效（免密记忆被清）或风控层 | 现行兜底 |

关键认知（refresh.py 文档原文要点）：
- `_m_h5_tk` 只有 10 分钟有效期，且服务端只在浏览器**活跃访问**时续期——纯 mtop 静默跑会滑向半失效
- 浏览器指纹不对时服务端把注入 cookie 视为半失效 → 首页弹 passport → 点「快速进入」即可免密续期（用户无感）
- **ILLEGAL_ACCESS 属风控层，参考项目明确"刷 cookie 也救不了"**（_is_recoverable_auth_error 不含它）
- 刷新成功判定：必须看到 **cookie2** 被下发（光有 _m_h5_tk/unb 仍会 SESSION_EXPIRED）
- `EXOIRED` 不是拼写错误，阿里就这么拼（已适配）
