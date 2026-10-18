# 钟维 - AI应用开发实习简历（三个版本）

---

## 基本信息
- 电话：134-1311-7268
- 邮箱：zwyyy2312278361@163.com
- GitHub：github.com/wu9506040-lab/e-commerce-customer-service

---

## 教育背景
**东莞城市学院** | 软件工程 · 本科 | 2025.09 – 2027.06
- 主修课程：数据结构、数据库原理、计算机网络、软件工程、Python程序设计

---

## 项目经历

---

### 版本1：AI应用开发方向

> **电商智能客服Agent系统（大创项目核心模块·独立迭代优化）** | 2026.03 – 至今
>
> 独立设计开发企业级AI客服系统，覆盖售前/售中/售后全链路13类业务工具。针对IM场景LLM回复慢、易幻觉、答非所问等问题，设计了从消息接收到回复发送的完整AI管线，实现端到端延迟从54s优化至10s内。
>
> - **RAG检索优化**：针对初始命中率仅42.7%的问题，搭建BM25+向量双路召回→RRF融合→LLM精排三级链路；基于18篇规范文档构建330题分层测试集，hit@1从0.427提升至0.576（+35%），hit@5从0.733提升至0.979
> - **多格式知识中台**：支持PDF（页眉剔除/章节层级/表格转markdown/脚注独立块）、Markdown、TXT、JSON多格式解析；句子边界语义切分（绝不截断句中）+ 尾句重叠 + 增量幂等入库
> - **渠道隔离架构**：Qdrant payload打channel标签 + 检索过滤，闲鱼/商城知识库互不干扰；快速通道设计——常规问题跳过RAG直出LLM，复杂问题才走检索，平衡速度与质量
> - **回复质量校验**：纯正则校验管道（零延迟）——markdown/占位符剥离、长度保险丝、价格幻觉三层模式检测（对比商品价格白名单），观察模式先攒数据再收紧
> - **LLM工程化**：SSE流式输出、分Agent短prompt（price/tech/default各自独立）、few-shot话术库、打字模拟拟人化
>
> **AI栈：LangGraph + Function Calling + Qwen** | **后端：FastAPI + Redis + MySQL + Qdrant** | **前端：Vue3 + TS** | **部署：Docker Compose**

---

### 版本2：Agent开发方向

> **电商智能客服Agent系统（大创项目核心模块·独立迭代优化）** | 2026.03 – 至今
>
> 独立设计开发多Agent客服系统，覆盖售前/售中/售后全链路13类业务工具。设计三级意图路由+分Agent编排架构，实现意图识别准确率与响应速度的平衡，端到端回复延迟从54s优化至10s内。
>
> - **三级意图路由**：本地关键词/正则优先命中（零LLM开销，80%+IM问题走本地）→ 规则miss才LLM分类；tech/price/default三级优先级，合批消息双意图叠加
> - **分Agent编排**：按意图挂独立短prompt（price/tech/default各自40字硬约束）；议价Agent轮次计数+梯度让步策略动态注入（第1次小幅→第3次最大→守底线）；场景识别（售前/售中/售后）YAML话术库
> - **状态机设计**：议价轮次SQLite计数（1h过期）+ 防回退订单状态机（refunded > shipped > paid > 待付款）；转人工触发条件（议价超N轮/用户说找人工/投诉退款）
> - **人机协同**：AI自动回复↔人工接管无缝切换；转人工自动建工单（带订单证据+对话摘要）；快捷短语模板库
> - **工具调用**：mtop接口调用（商品详情/会话列表/订单状态）、WebSocket消息收发、Cookie四层自愈（HTTP续期+Playwright免密登录）
>
> **AI栈：LangGraph + Function Calling + Qwen** | **后端：FastAPI + Redis + MySQL + Qdrant** | **前端：Vue3 + TS** | **部署：Docker Compose**

---

### 版本3：AI全栈方向

> **电商智能客服中台（大创项目核心模块·独立全栈开发）** | 2026.03 – 至今
>
> 从0到1独立设计开发企业级AI客服中台——包含闲鱼渠道接入、AI应答管线、人工坐席工作台、数据看板的完整全栈系统。自研闲鱼WebSocket协议栈，实现7×24无人值守运行，服务真实电商卖家。
>
> - **后端**：Python FastAPI（SSE流式输出）+ SQLAlchemy + Pydantic；自研闲鱼LWP协议栈（帧注册/心跳/ACK/MessagePack解码/mtop签名）；多账号并行架构（每账号独立Worker隔离运行，单账号故障不影响全局）
> - **前端**：Vue3 + Vite + TypeScript + ECharts；三栏式人工坐席工作台（会话列表+对话窗口+客户信息）；数据看板11图（消息趋势/渠道分布/响应时间/热门问题TOP10）；深色侧边栏管理台布局
> - **数据层**：MySQL 8（消息/订单/工单/用户，多渠道视图隔离）+ Redis（会话热数据/命令事件总线）+ Qdrant（向量库，payload渠道过滤）
> - **部署运维**：Docker Compose五容器编排（api/frontend/mysql/redis/qdrant）；Cookie四层自愈（mtop Set-Cookie + HTTP续期 + Playwright免密登录）；看门狗进程探活自动拉起；97个测试文件/2.3万行
> - **AI能力**：通义千问qwen-plus SSE流式对话、text-embedding-v3向量检索、BM25+向量混合召回、意图识别+多Agent编排
>
> **技术栈：Python 3.11 + FastAPI + Vue3 + TypeScript + MySQL + Redis + Qdrant + Docker**

---

## 实习经历
**宇辰科技有限公司** | 数据质量实习生 | 2024.07 – 2025.01
- 参与自动驾驶数据质检产线：制定错/低质样本识别规则与复核标准，沉淀标准化校验流程，直接作用于下游模型训练数据质量。

---

## 技能清单
- **Agent工程**：FastAPI、状态机编排、护栏与降级处理、Function Calling、LangGraph生产使用、Prompt工程与版本化
- **RAG与评测**：混合检索（BM25+向量+RRF）、精排、切片与知识治理；评测集构建、分层端到端评测
- **后端**：Python / PyDantic / SQLAlchemy；JWT 鉴权；SSE 流式；熔断 / 限流 / 退避 / 幂等
- **数据与部署**：MySQL 8（复杂查询、索引、事务）、Redis 7（缓存、限流、状态回滚）、Qdrant / Chroma、Docker Compose、Nginx、ECS、Git、CI
- **前端与效能**：Vue3 + TypeScript + Vite、ECharts、Playwright；pytest / 覆盖率 / CI 门禁

---

## 自我评价
有 RAG / Agent 全栈项目实战经验（从需求分析、数据清洗、模型优化到部署上线全流程），能独立完成端到端开发。学习能力强，可快速上手新技术，对 AI 应用落地有持续热情和探索意愿。
