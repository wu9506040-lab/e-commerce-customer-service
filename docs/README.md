# 电商智能客服 Agent 系统 · 文档总览（V2.1）

> 本目录是项目唯一的"知识仓库"。新成员 / AI 编程工具 / 面试官请按本文档的阅读路径进入。
>
> 最后更新：2026-07-11 · Phase 0 治理优化落地

---

## 0. 30 秒速览

| 维度     | 值                                                             |
|----------|----------------------------------------------------------------|
| 项目     | 电商智能客服 Agent                                              |
| 技术栈   | FastAPI + Vue3 + Qdrant + Redis + MySQL + Qwen                |
| 部署     | Docker Compose（本地 + 阿里云 ECS）                             |
| 工程纪律 | 仓库根 `CLAUDE.md`（V2.1）                                      |
| 业务基线 | `architecture/business.md`（V3.1 冻结）                          |
| 系统形态 | `architecture/system.md`                                         |
| 演进基线 | `development/roadmap.md`（V2 待生成，基于实际代码）              |
| AI 开发规则 | `governance/ai_development_rules.md`                          |

---

## 1. 阅读路径（按角色）

### 1.1 新成员（5 分钟上手）

```
1. README（本文档）
2. architecture/system.md           ← 系统长什么样
3. architecture/business.md         ← 业务全景图
4. CLAUDE.md §7 代码结构           ← 目录怎么分
5. learning_log.md（最新 1-2 个模块） ← 实际怎么开发
```

### 1.2 AI 编程工具（必读）

```
1. CLAUDE.md §2 禁止行为            ← 第一禁线
2. CLAUDE.md §4 AI 6 步任务法      ← 执行模板
3. CLAUDE.md §5 Scope Lock + §6 验证分级 ← 单模块 + 验证
4. CLAUDE.md §9 架构设计要求（永久）  ← 改代码前必检
5. governance/ai_development_rules.md ← 反例 13 条 + Prompt 散落检查
```

### 1.3 模块开发者（实现新模块前）

```
1. CLAUDE.md §9.8 八件套交付规范    ← 必须交付的 8 项
2. CLAUDE.md §9.7 自检 5 问         ← 开发前自检
3. CLAUDE.md §4.4 Stop-Loss 8 问   ← 提交前自检
4. governance/ai_development_rules.md 13 条反例 ← 严禁触犯
5. learning_log.md（最近模块参考样例）
```

### 1.4 面试官 / 外部讲解

```
1. README + architecture/system.md            ← 系统拓扑
2. architecture/business.md V3.1               ← 业务架构全景
3. CLAUDE.md §9 + governance/ai_development_rules.md ← 工程纪律
4. development/roadmap_v1.md                   ← 技术演进路线
5. learning_log.md（里程碑 M1-M13）            ← 实际落地证据
```

---

## 2. 目录索引

```
docs/
├── README.md                              ← 本文件
│
├── architecture/                          ← 架构基线
│   ├── business.md                        ← V3.1 业务架构（冻结）
│   └── system.md                          ← 系统架构 / 技术选型 / 运行时
│
├── governance/                            ← 治理与工程纪律
│   └── ai_development_rules.md            ← AI 开发规则 / 反例清单 / Prompt 管理
│
├── development/                           ← 演进路线
│   └── roadmap_v1.md                      ← V1 演进基线（待生成 V2）
│
├── decisions/                             ← 重大架构决策（ADR · 待填充）
│
├── learning_log.md                        ← 模块学习日志（M1-M13+ · 持续追加）
│
├── OPERATIONS.md                          ← 部署 / 运维
├── HEALTHCHECK.md                         ← healthcheck.io 监控接入
├── demo_walkthrough_report.md             ← 公网演示 walkthrough
├── test_coverage.md                       ← 测试覆盖说明
│
├── PROJECT_DESIGN.md                      ← 项目设计草稿（10 项 TODO）
├── refund_graph_v3.png                    ← 退款流程图（v3）
│
├── ecommerce_kb/                          ← 知识库数据（gitignore）
└── _private/                              ← 简历素材 / 个人笔记（gitignore）
```

---

## 3. 文档类型分级

| 类型         | 位置                | 更新频率       | 谁负责            |
|--------------|---------------------|----------------|-------------------|
| 工程纪律     | `CLAUDE.md`（仓库根）| 极少（架构变更）| Tech Lead         |
| 业务基线     | `architecture/`     | 重大业务变更   | 业务架构师        |
| 系统形态     | `architecture/system.md` | 引入新模块  | Tech Lead         |
| 治理规则     | `governance/`       | 极少          | Tech Lead         |
| 演进路线     | `development/`      | 每次 Roadmap  | Tech Lead         |
| 架构决策     | `decisions/`        | 重大决策时    | 决策者 / Tech Lead |
| 学习日志     | `learning_log.md`   | 每模块完成    | 模块负责人        |
| 运维 / 监控  | `OPERATIONS.md` 等  | 流程变更      | 运维              |

---

## 4. 文档维护规则

### 4.1 什么时候必须更新文档

| 触发场景                             | 必须更新的文档                                |
|--------------------------------------|-----------------------------------------------|
| 新模块 / 新接口落地                  | `learning_log.md` + 八件套                    |
| 业务规则变化                         | `architecture/business.md`                     |
| 引入新依赖 / 新技术                  | `architecture/system.md` §1.2                 |
| 跨模块架构变更                       | `decisions/YYYY-MM-DD-xxx.md`                  |
| Roadmap 推进                         | `development/roadmap.md`                       |
| AI 开发规则调整                      | `CLAUDE.md §4` + `governance/ai_development_rules.md` |
| Prompt 模板新增 / 变更               | `governance/ai_development_rules.md`           |

### 4.2 文档命名规范

- 模块文档：`learning_log.md` 内的章节（以 `## XXX 模块` 形式）
- 架构决策：`decisions/YYYY-MM-DD-<title>.md`（如 `2026-07-11-event-bus.md`）
- 子目录中：`architecture/`、`governance/`、`development/`、`decisions/` 下文件名用 `lower_snake_case.md`

### 4.3 文档提交规范

- 文档改动与代码改动分开 commit（一个 docs: / 一个 feat:/fix:）
- 文档 commit 类型：`docs(scope): 描述`
- 占位 / TODO 文档必须在开头标注 `🚧 草稿` / `状态: 待评审`

---

## 5. 当前冻结 / 待办

| 状态      | 文档                                  | 原因                                |
|-----------|---------------------------------------|-------------------------------------|
| ✅ 冻结   | `architecture/business.md` V3.1       | 业务基线已对齐企业访谈               |
| ✅ 落地   | `CLAUDE.md` V2.1                       | Phase 0 完成                        |
| ✅ 落地   | `governance/ai_development_rules.md`  | 反例 / Prompt 检查已文档化           |
| ✅ 落地   | `architecture/system.md`              | 系统形态已对齐 CLAUDE.md §9          |
| 🚧 待生成 | `development/roadmap.md` V2           | **下一步：扫描实际代码后生成 V2**     |
| 🚧 待填充 | `decisions/`                           | 仅在产生重大决策时新增                |
| 🚧 草稿   | `PROJECT_DESIGN.md`                   | 10 项 TODO 待填充                    |

---

## 6. 一句话总结

> **按 CLAUDE.md 的工程纪律、用 architecture/ 的业务基线、沿 development/roadmap.md 的演进路线、通过 governance/ 的 AI 规则可持续扩展，同时把所有实践沉淀到 learning_log.md。**
