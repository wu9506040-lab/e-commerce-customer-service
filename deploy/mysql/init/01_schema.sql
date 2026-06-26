-- =============================================================
-- 智能客服 Agent 系统 - MySQL 数据库 Schema
-- 数据库: customer_service（由 MYSQL_DATABASE 环境变量自动创建）
-- 字符集: utf8mb4 / utf8mb4_unicode_ci（继承自 mysqld 命令行）
-- 设计: 5 张表
--   1. users                - 用户主体
--   2. conversations        - 会话索引（与 Redis session_id 对齐）
--   3. messages             - 消息明细
--   4. knowledge_documents  - 知识库文档元数据（与 Qdrant source 对齐）
--   5. operation_logs       - 操作审计日志
--
-- 命名约定:
--   - 表名小写 + 下划线
--   - id BIGINT UNSIGNED PK AUTO_INCREMENT
--   - create_time / update_time 由 MySQL 自动维护
--   - deleted TINYINT DEFAULT 0 配合业务层逻辑删除
--   - 外键一律逻辑关联（不建 DB 级 FK 约束）
-- =============================================================

USE `customer_service`;

-- =============================================================
-- 1. users - 用户主体
-- =============================================================
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `username`        VARCHAR(64)  NOT NULL                              COMMENT '登录名（唯一）',
  `password_hash`   VARCHAR(255) NOT NULL                              COMMENT 'bcrypt 哈希后的密码',
  `display_name`    VARCHAR(200) DEFAULT NULL                          COMMENT '显示名',
  `email`           VARCHAR(200) DEFAULT NULL                          COMMENT '邮箱（唯一，可空）',
  `phone`           VARCHAR(32)  DEFAULT NULL                          COMMENT '手机号（唯一，可空）',
  `role`            VARCHAR(32)  NOT NULL DEFAULT 'user'               COMMENT '角色: user / admin',
  `status`          TINYINT      NOT NULL DEFAULT 1                    COMMENT '状态: 1=启用 / 0=禁用',
  `last_login_at`   DATETIME     DEFAULT NULL                          COMMENT '最后登录时间',
  `last_login_ip`   VARCHAR(64)  DEFAULT NULL                          COMMENT '最后登录 IP',
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`         TINYINT      NOT NULL DEFAULT 0                    COMMENT '逻辑删除: 0=未删 / 1=已删',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_users_username` (`username`),
  UNIQUE KEY `uk_users_email`    (`email`),
  UNIQUE KEY `uk_users_phone`    (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- =============================================================
-- 2. conversations - 会话索引
-- 与 Redis chat:session:{session_id} 共用同一个 session_id
-- Redis 是热路径（24h TTL），MySQL 是冷路径真源（永不过期）
-- =============================================================
DROP TABLE IF EXISTS `conversations`;
CREATE TABLE `conversations` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT        COMMENT '主键',
  `session_id`        VARCHAR(64)  NOT NULL                          COMMENT '会话 ID（与 Redis 对齐）',
  `user_id`           BIGINT UNSIGNED NOT NULL                       COMMENT '所属用户 ID（逻辑外键 → users.id）',
  `title`             VARCHAR(200) DEFAULT NULL                      COMMENT '会话标题（首问摘要或用户改）',
  `status`            TINYINT      NOT NULL DEFAULT 1                COMMENT '状态: 1=进行中 / 0=已结束',
  `message_count`     INT          NOT NULL DEFAULT 0                COMMENT '消息数（冗余，便于列表查询）',
  `first_query`       VARCHAR(500) DEFAULT NULL                      COMMENT '首条问题（用于列表展示）',
  `last_message_at`   DATETIME     DEFAULT NULL                      COMMENT '最后一条消息时间',
  `create_time`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`           TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_conversations_session_id` (`session_id`),
  KEY `idx_conversations_user_status_time` (`user_id`, `status`, `last_message_at` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话表';

-- =============================================================
-- 3. messages - 消息明细
-- 存所有轮次问答，Redis miss 时从 MySQL 按 session_id+create_time 回填最近 20 条
-- =============================================================
DROP TABLE IF EXISTS `messages`;
CREATE TABLE `messages` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT              COMMENT '主键',
  `session_id`    VARCHAR(64)  NOT NULL                               COMMENT '所属会话 ID（逻辑外键 → conversations.session_id）',
  `user_id`       BIGINT UNSIGNED NOT NULL                            COMMENT '所属用户 ID（冗余便于查询）',
  `role`          VARCHAR(16)  NOT NULL                               COMMENT '角色: user / assistant / system',
  `content`       TEXT         NOT NULL                               COMMENT '消息正文',
  `contexts`      JSON         DEFAULT NULL                           COMMENT 'RAG 检索的 context 列表（仅 assistant）',
  `scores`        JSON         DEFAULT NULL                           COMMENT 'context 对应分数（仅 assistant）',
  `token_count`   INT          DEFAULT NULL                           COMMENT 'LLM token 数（仅 assistant）',
  `latency_ms`    INT          DEFAULT NULL                           COMMENT '响应耗时 ms（仅 assistant）',
  `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`       TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_messages_session_time` (`session_id`, `create_time`),
  KEY `idx_messages_user_time`    (`user_id`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='消息明细表';

-- =============================================================
-- 4. knowledge_documents - 知识库文档元数据
-- 与 Qdrant payload.source 字段对齐（同样作为幂等键）
-- 实际向量存在 Qdrant，本表只存「文档是什么」的元数据
-- =============================================================
DROP TABLE IF EXISTS `knowledge_documents`;
CREATE TABLE `knowledge_documents` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `source`          VARCHAR(200) NOT NULL                              COMMENT '来源标识（与 Qdrant payload.source 对齐，唯一）',
  `title`           VARCHAR(500) DEFAULT NULL                          COMMENT '文档标题',
  `description`     TEXT         DEFAULT NULL                          COMMENT '描述',
  `doc_type`        VARCHAR(32)  NOT NULL DEFAULT 'manual'             COMMENT '类型: manual / faq / policy / product',
  `total_chunks`    INT          NOT NULL DEFAULT 0                    COMMENT 'chunk 数（冗余）',
  `total_chars`     INT          NOT NULL DEFAULT 0                    COMMENT '字符数（冗余）',
  `uploader_id`     BIGINT UNSIGNED DEFAULT NULL                       COMMENT '上传者 ID（逻辑外键 → users.id）',
  `status`          TINYINT      NOT NULL DEFAULT 1                    COMMENT '状态: 1=上线 / 0=下线',
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`         TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_knowledge_source` (`source`),
  KEY `idx_knowledge_status_type` (`status`, `doc_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库文档元数据';

-- =============================================================
-- 5. operation_logs - 操作审计日志
-- 记录关键行为：登录 / 灌库 / 删库 / 删会话 等
-- 注意: 不记录闲聊内容（消息正文存 messages 表），只记元动作
-- =============================================================
DROP TABLE IF EXISTS `operation_logs`;
CREATE TABLE `operation_logs` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT            COMMENT '主键',
  `user_id`         BIGINT UNSIGNED DEFAULT NULL                       COMMENT '操作用户 ID（可空：未登录的请求）',
  `username`        VARCHAR(64)  DEFAULT NULL                          COMMENT '用户名（冗余防改名/删除）',
  `action`          VARCHAR(64)  NOT NULL                              COMMENT '动作: login / ingest / delete_knowledge / ...',
  `target_type`     VARCHAR(32)  DEFAULT NULL                          COMMENT '对象类型: knowledge / user / session',
  `target_id`       VARCHAR(64)  DEFAULT NULL                          COMMENT '对象 ID',
  `ip`              VARCHAR(64)  DEFAULT NULL                          COMMENT '来源 IP',
  `user_agent`      VARCHAR(500) DEFAULT NULL                          COMMENT 'UA',
  `detail`          JSON         DEFAULT NULL                          COMMENT '额外参数（JSON）',
  `result`          VARCHAR(16)  NOT NULL DEFAULT 'success'            COMMENT '结果: success / fail',
  `error_msg`       VARCHAR(500) DEFAULT NULL                          COMMENT '失败原因（result=fail 时填写）',
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`         TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_logs_user_time`  (`user_id`, `create_time`),
  KEY `idx_logs_action_time` (`action`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='操作审计日志';
