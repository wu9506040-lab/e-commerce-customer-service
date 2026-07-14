-- =============================================================
-- 用户长程记忆表 - P2 长程记忆 (跨 session 用户画像)
-- 数据库: customer_service
-- 执行顺序: 02（在 01_schema.sql 之后）
--
-- 设计:
--   - 1:1 → users.id (PK 即 user_id)
--   - summary: 近 7 天对话要点（LLM 摘要，TEXT 容纳 ~2KB）
--   - frequent_skus: 最近 30 天提过的 SKU 列表（JSON 数组，去重）
--   - preferences: 用户偏好 tags（JSON 对象，结构化偏好）
--   - interaction_count: 累计对话轮数（user 消息条数）
--   - last_active_at: 最后活跃时间（用于"7 天前"的过滤）
--
-- §3.3 YAGNI 边界：
--   - 不做事件流（user_profile_events），需要时用 messages JOIN 即可
--   - 不做派生画像（user_personas），summary 字段够用
--   - 不做租户级画像，profile 跟 user_id 1:1
-- =============================================================

USE `customer_service`;

DROP TABLE IF EXISTS `user_profiles`;
CREATE TABLE `user_profiles` (
  `user_id`           BIGINT UNSIGNED NOT NULL                          COMMENT '所属用户 ID（1:1 → users.id）',
  `summary`           TEXT            DEFAULT NULL                       COMMENT '近 7 天对话要点（LLM 摘要，≤500 字）',
  `frequent_skus`     JSON            DEFAULT NULL                       COMMENT '最近 30 天提过的 SKU 列表（去重，最多 20 个）',
  `preferences`       JSON            DEFAULT NULL                       COMMENT '用户偏好 tags（结构化，如 {refund_pref: "fast", cat: "electronics"}）',
  `interaction_count` INT UNSIGNED    NOT NULL DEFAULT 0                COMMENT '累计对话轮数（user 消息数）',
  `last_active_at`    DATETIME        DEFAULT NULL                       COMMENT '最后活跃时间',
  `create_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`           TINYINT         NOT NULL DEFAULT 0                COMMENT '逻辑删除: 0=未删 / 1=已删',
  PRIMARY KEY (`user_id`),
  KEY `idx_user_profiles_last_active` (`last_active_at` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户长程记忆表（1:1 → users.id）';
