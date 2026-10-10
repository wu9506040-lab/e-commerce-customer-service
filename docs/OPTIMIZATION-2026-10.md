# Agent 全链路优化方案（2026-10-10 · 四路审计合流版）

> **状态：已批准（2026-10-10 晚圈批）**
> ① 批次一 7 项全量批准，10-11 开工（本地快合直推，不走 PR）；
> ② Multi-Query 按"修伤→hit@K 实测→有正贡献才转正，否则砍并同步删简历/README 口径"执行；
> ③ 批次三选定：3.2 语义切片 A/B + 3.3 请求级超时预算 + 3.4 意图三段补强；
>   **3.1 FC 安全接线本轮不做**→ FC 口径维持"实现完整未上线（安全接线未完成）"，若面试被追问，"知道上线前还差 ownership/确认票据/幂等/per-tool metrics 四件事"本身就是答案；
>   **3.5 checkpointer 不做**，escalate 跨轮恢复挂已知局限。

> 依据：2026-10-10 四份只读摸底报告（意图识别 / 检索与切片 / 生成与会话 / 工具与决策），
> 全部论断带 文件:行号 证据，正文只列结论级摘要，细节可回查报告。
> 时间约束：10 月底投递潮；本方案按"面试叙事价值 × 用户可感知 × 工作量"三轴排期。

---

## 0. 总纲：这个系统真正的问题不是缺功能

四份报告收敛到同一个诊断——**"声明层厚、接线层薄"**，具体三种形态：

| 形态 | 实例（均为实测确认） |
|---|---|
| **只接了一条腿**（读有写无） | `ContextService.update()` 全仓零调用方 → conversation_contexts 表永远空，开了开关也没有会话记忆；UserProfile 的 summary/preferences 零写入方；`message_ratings` 因 done 不发 message_id 全部退化成 message_id=0 会话粒度 |
| **失败分支静默滑向错误路径** | decide 失败无 decide_result → 静默落 synthesize 用默认值硬造答案；LLM 流中途断连被吞 → 截断答案当完整答案落库落缓存；检索全链异常一律 `return []` 无告警分级；rerank 解析失败全 0 分静默保持粗排序 |
| **带伤/落空的已宣称能力** | KB 线上 ~92/202 点陈旧世代（无 delete-before-write）→ 0.806 基线是脏库测的；RAG_TYPE_BOOST 配 3 键 vs 实际 13 种 doc_type → 加权基本落空；Multi-Query A8 融合送 rerank 缺 payload 键 → 一开灰度必 KeyError 静默降级；13 个 FC 工具生产链路零触达且开启即绕过全部防串单护栏 + 1 处越权实锤 |

**优化第一原则：把已宣称的做实（做实 > 新增）。** 失败必须显形、写路径必须接线、快照必须落库。
每一批都以"可复现数字 + 回归测试 + 面试故事"三件套验收。

---

## 1. 六链路现状速览

| 链路 | 现状一句话 | 最大问题 |
|---|---|---|
| 意图识别 | 三级路由（84 pattern 规则→qwen-max LLM→default），V12 多意图，V13 升级 | 双重分类零缓存；method/conf 不落库零回流；84 pattern 双层顺序敏感无冲突工具；无拒识类 |
| 检索召回 | dense(0.4硬阈值)+自研BM25(char 2-gram)→RRF(k=60)→LLM listwise rerank | **库脏**（92/202 陈旧点）；boost 词表错位；BM25 路不受阈值约束；每 query embed 最多 4 次 |
| 知识库切片 | 500 字符硬滑窗+50 overlap，uuid5 内容哈希幂等 | 无语义边界无章节 metadata；无 delete-before-write；README"67 篇"口径过时 |
| 生成 | tagged prompt（来源编号+事实前缀）+agent_v1 5 约束；V3 refund 另有 9 硬约束+后置幻觉校验 | 约束只在默认关闭的 V3 路生效；主路径零程序化校验（citation_formatter 空壳）；历史截断三套标准（20/6/4 条） |
| 工具/决策 | RefundFlow(LangGraph 4 节点)+Resolver+13 工具 FC(关)+四类转人工源 | decide 失败静默生成；retry 无回边形同虚设；escalate 无跨轮恢复（无 checkpointer）；写工具无确认票据/幂等键 |
| 会话/流式 | SSE meta/token/done+heartbeat+resume(Redis checkpoint TTL600s×2 次) | 截断当完整；fallback 二次整流拼接落库；resume 后该轮永不进历史；resolve 人工回复 Redis 热会话看不到 |

另有一条横切病根：**开关真值三源漂移**（代码默认 False / .env.dev true / compose 未注入清单）——
`SSE_CARD_V2`、`ENABLE_CONTEXT_STORE` 压根不在 compose env 注入清单里，改环境行为要靠人肉记忆。

---

## 2. 批次一 · 可靠性修复包（P0，10-11 ~ 10-13，约 2.5 天）

> 共同点：都是"现在就在给用户错误答案/丢答案"的活缺陷，全部小切口。

| # | 修复 | 现状证据 | 改法 | 工作量 | 验收 |
|---|---|---|---|---|---|
| 1.1 | **decide 失败静默滑向 synthesize** | refund_graph.py:521,547 返回无 decide_result 的 state→`_decide_route` 落 synthesize 硬造 | retry 未达上限→图内条件回边 decide→decide（让 MAX_LLM_RETRIES=3 真正生效）；达上限/异常→强制 escalate P2"决策异常"，**永不无决策生成** | 半天 | 新增 3 条图测试（mock LLM 失败/坏 JSON/超限）；eval 22/22 无回归 |
| 1.2 | **截断答案当完整** | qwen.py:311-318 流中途断连吞掉→done 照发→落库落缓存删 checkpoint | 捕获断连后置 `truncated=True`：done 事件带标、禁入 response_cache、落库 messages 打标、前端提示"回答可能不完整" | 半天 | 单测模拟断连流；live 复现一次 |
| 1.3 | **fallback 二次整流拼接** | orchestrator.py:242-250 已发 token 后重跑 V1.2 → full_answer=半截+全文拼接落库 | 已发出任何 token 后 fallback 改为**追加固定话术**（"抱歉刚才中断，请重试或转人工"）不再整流重答；未发 token 才允许重跑 | 半天 | 单测断言 full_answer 无拼接 |
| 1.4 | **人工回复回写断链** | handoff_ticket_service.py:190-198 只写 MySQL；session_service.py:56 Redis 热优先→用户看不到 | resolve 后失效该 session Redis 历史缓存（DEL 一行）+回归测试 | 2 小时 | live：resolve→用户拉历史可见 |
| 1.5 | **P0 词表通胀** | "转人工/机器人/起诉"全在 P0 user_requested 词表→坐席队列 P0 稀释 | 分级：12315/媒体/投诉/赔偿类保 P0；"转人工/机器人/人工客服"降 P1 走普通 handoff；词表挪进 guard.yaml 可配置 | 2 小时 | escalation 单测改断言 |
| 1.6 | **V13 词序误升级边角**（今天引入，诚实修） | "ORDxxx 怎么退款还没到账"命中 `怎么.*退款` 后被升级正则误伤，与"退款怎么还没到账"路由分裂 | 升级判定加两道闸：①`_rule_classify` 返回值带 matched_pattern，命中时效类 pattern（到账/工作日/多久）不升级；②query 含 `到账\|工作日\|还没.*到` 负向前瞻不升级 | 半天 | test_intent_multi 补 2 条词序对偶用例 |
| 1.7 | **开关真值自报** | 代码默认/环境/compose 三源漂移，SSE_CARD_V2 等不在注入清单 | 启动期 logging 一行输出全部 feature 开关生效值+来源；compose env 清单补全 2 个漏项；新增 test 断言清单完整 | 半天 | 容器起一次看日志 |

**批次一故事线**："我对着四份链路审计把 4 条**静默失败路径**修成了显式降级——决策失败转人工、截断打标、拼接修复、热缓存同步，用户拿不到错答案比拿到新答案更重要。"

---

## 3. 批次二 · 做实 + 数据飞轮（P1，10-14 ~ 10-20，约 5 天，含 WP2）

> 共同点：把"虚的做实"，并且给 WP2 飞轮提供地基（intent 快照落库是回流的前置）。

| # | 项 | 改法要点 | 工作量 | 验收（全是可写简历的数字） |
|---|---|---|---|---|
| 2.1 | **KB 大洗库** | ingest 改 **source 级 delete-then-write**（重灌幂等）；重灌全部 114 items（补 33 个未入库、清 17 个遗留含 admin_test、回收 92 个陈旧点）；doc_type 词表归一（13 种→枚举）+ 回填 104 个缺 doc_type 旧点；`RAG_TYPE_BOOST` 键对齐真词表 | 1 天 | 线上点数=重灌模拟数；README 67/202 旧口径刷新；**330 题 hit@K 新基线重测（新旧口径双记录，数据诚实声明追加一条"发现库脏后洗库重测"——这本身就是最好的面试故事）** |
| 2.2 | **去重复调用（成本战役）** | ①`chat.py:314` pre_intent 透传进 `run_stream`（改写后 query 变才重分类）；②请求级 embedding 复用（同 query 的 embed 结果在 guard/semcache/检索/写缓存四处共享，contextvar 实现）；③缓存命中路径跳过 LLM rerank（meta.contexts 用 dense 粗排即可）| 1 天 | KPI 大盘成本账前后对比：**单请求 LLM 调用 5→≤3、embedding 4→1，每会话成本预估 -30~40%**（成本本来就是 WP1 大盘指标，闭环） |
| 2.3 | **意图/评价快照落库 = WP2 地基** | messages 增 `intent_snapshot` JSON 列（primary/method/conf/intents/upgrade 标记）；done 事件带 assistant message_id、前端按消息评价（消灭 message_id=0）；metrics 补 method 分布 + LLM 兜底率 + rule_upgrade 计数 | 1 天 | 👎→message→intent_snapshot→contexts 全链路反查 demo 跑通一次 |
| 2.4 | **badcase 自动池 + 评测集增量** | 低置信（llm 且 conf<0.6）+ 👎 + guard 误杀三类信号自动落 badcase 表；半自动并入 eval 集（人工审核后合入，脚本 `merge_badcase_to_eval.py`，吸取 WP2 调研结论：同存稳定键 source 防 doc_id 漂移） | 1 天 | 飞轮闭环演示：造 3 个差评→入池→审核→合入→重跑 eval 出新数字（简历"驱动补标/回归复验"从措辞变成机制） |
| 2.5 | **意图黄金评测集离线化** | 每类 ≥30 条（规则易错区加密：词序对偶/边界/域外）+ 混淆矩阵 + 分类别 P/R；纯离线跑 classify 不打 LLM（LLM 层用固定 seed/mock）；进 CI（pytest 或 scripts，5 分钟预算） | 1 天 | 新简历行："意图路由黄金集 XX 条，规则层命中 YY%，分类别 P/R 报表"；verify_intent_classify 的死 ECS BASE 一并废弃 |
| 2.6 | **会话记忆接线或诚实砍** | `ContextService.update` 在 done 收尾接线（写 last_intent/current_order_no/flow_state）；refund_flow.py:263 改传真实 ctx 而非空对象；`ENABLE_CONTEXT_STORE` 转正进 compose 清单 | 1 天 | 多轮锁单实测：轮 1 报订单号→轮 2"那能退吗"不再重新解析/不串单 |
| 2.7 | **Multi-Query：修伤再决定开** | 修 A8 融合候选缺 payload 的 KeyError（_format_hits 保留原文或 rerank 端兼容 text 键）+ 融合去重键恢复 chunk 级；打开后 hit@5 重测，**有正贡献才转正，无贡献就砍掉并从简历口径删除** | 半天 | 消融表新增一行（Multi-Query 开/关），二选一结局都可讲 |

**批次二故事线**："我把'数据飞轮'从人肉环节做成机制：差评和低置信样本自动进池、审核合入评测集、CI 重跑；同期发现知识库 45% 是陈旧点，洗库重测刷新全部检索基线——顺便把脏库时代'看起来在优化其实地基是歪的'这个坑写进了公开更正。"

---

## 4. 批次三 · 能力进阶（弹性，10-21 ~ 10-26，选 2~3 项）

> 按面试价值排，允许全砍（如果投递/面试占了时间）。

| # | 项 | 内容 | 价值 | 工作量 |
|---|---|---|---|---|
| 3.1 | **Agent FC 安全接线+灰度**（最有故事、最重） | 前置四件套：①dispatch 统一注入 ownership 中间层（修 `get_shipping_insurance_info` 越权实锤）；②写操作服务端确认票据（confirmed 不再由 LLM 自填）+ 幂等键；③per-tool metrics（成功率/耗时/错误分类）；④FC 路径与防串单体系对齐（不能先于一切短路，改为意图路由后、按工具白名单进入）。完成后 `ENABLE_AGENT_FC` 灰度 10%→全量，eval_agent_fc.py live 出真实 tool_selection_accuracy 数字 | "简历上 14→13 个工具的 Agent 编排从 demo 变成可上线"，且体现**安全前置**思维（实习生里稀缺） | 3 天 |
| 3.2 | **语义切片 A/B** | 句边界+标题拼接进 embedding 文本的 recursive 切片 vs 现 500 硬切；330 题 hit@5 对照（必须在 2.1 洗库后做） | RAG 深度叙事：切片策略消融有真数字 | 1 天 |
| 3.3 | **请求级超时预算** | /chat 总 deadline 90s + 分段（rewrite/intent/decide/synthesize 各配 max_tokens 与独立超时）；非流式 LLM 调用过 semaphore；fallback V1.2 在断路器 OPEN 时直接快速失败不再二次调 LLM | 韧性账："最坏 10 分钟→90 秒"，一句话能量型指标 | 1 天 |
| 3.4 | **意图层三段补强** | ①分类 prompt 外置进 prompt_loader + 类别描述扩写（边界+反例+拒识指令）；②qwen-max→qwen-turbo 降级实验（黄金集对照精度损失）；③规则层 84 pattern 冲突静态扫描脚本（启动期全 pattern 两两交集报告，进 CI） | "小模型换成本不掉精度" + 规则表工程化治理 | 1~1.5 天 |
| 3.5 | **checkpointer 跨轮恢复** | LangGraph Redis Saver；escalate/need_more_info 后下一轮续接 decide 状态而非从零开始 | 深度叙事（图状态机持久化），但独立价值中等 | 1.5 天 |

---

## 5. 明确不做 / 后置清单（防过度工程，写进 README 已知局限）

| 项 | 为什么不做 |
|---|---|
| BM25 换 jieba/倒排表 | 202 点规模线性扫无感 + 项目"不乱装依赖"规则；扩库 >1 万点再做（scroll 分页先行，顺手 30 分钟） |
| HNSW/payload 索引调参 | 202 点 < indexing_threshold=20000，HNSW 根本没建，调了也没对象；README 已诚实标注 |
| RateLimitMiddleware Redis 化 | 单实例部署无竞态暴露；已知局限挂 backlog |
| prompt 灰度 traffic_ratio | 单人项目没有分流对象；死 flag `ENABLE_PROMPT_VERSIONING` 删除即可 |
| 语义缓存索引化/真 LRU | 50 条/用户规模暴扫可接受，随机淘汰影响面≈0 |
| profile summary LLM 离线任务 | 画像价值未验证前不做摘要生成；先接线 2.6 真实数据，一个迭代后无消费就砍列 |
| 历史 token 预算制截断 | 200 字输出约束 + 20 条历史上限双保险，当前风险低；投递期不碰 |
| V2 路径删除 | V3 保险丝仍是 V2（双异常兜底），现在删=削韧性；FC 灰度稳定后（批次三 3.1 完成）再收敛 |

---

## 6. 总时间线（倒排到投递）

```
10-10  方案批准（今天）
10-11~13  批次一（可靠性 7 项，每天 2~3 项小步提交，本地快合直推）
10-14~17  批次二上（2.1/2.2/2.3）→ ★10-17 左右：材料已新，投出第一批（C 线并行启动，不等开发完）
10-18~20  批次二下（2.4~2.7）
10-21~26  批次三选 2~3 项（若面试约进则降级为只做 3.3 半天项）
10-27~31  投递冲刺 + B 线模拟面试（用批次一/二产出的新数字更新宝典弹药文档）
```

**每批完成即更新**：README 战绩卡（新 hit@K/成本/意图精度数字）、简历弹药（数据与评测流水线.md）、learning_log。
**口径纪律**：洗库前后两套 hit@K 分开标注日期；Multi-Query 若砍，简历同步删词；任何"转正"必须活体+评测双验。

## 7. 风险登记

| 风险 | 对策 |
|---|---|
| 洗库后 hit@K 新基线比 0.806 低 | 数字难看但诚实且更可信——更正声明机制 10-09 已建过一次，复用该叙事；最坏情况是"发现旧数字虚高"，又是真实性故事素材 |
| decide 回边改动引入死循环 | 回边带 `decide_retry_count` 上限 3 + 超限强制 escalate；图测试锁三条路径 |
| ContextService.update 接线时序坑（首轮 conversation 未落库） | context_service.py:137-142 已返 False，接线时补一行告警计数；失败不影响主链路 |
| FC 灰度后幻觉工具调用 | 白名单+ownership 双闸；eval_agent_fc live 先跑 22 例门禁再开 |
| 投递期开发贪多 | 批次三允许整批砍；红线：每周留 ≥2 天给 B/C 线（面试+投递+材料） |
