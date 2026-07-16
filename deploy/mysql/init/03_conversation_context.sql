-- =============================================================
-- M14: conversation_contexts 会话上下文表
-- 1:1 → conversations.id
-- 存 session 级 KV：last_intent / current_order_no / flow_state / resolved_orders / flow_payload
--
-- L1 级别新增表（CLAUDE.md §9.4.4）：不破坏 conversations schema
-- 灰度：ENABLE_CONTEXT_STORE=False 时不读不写
-- =============================================================

USE `customer_service`;

CREATE TABLE IF NOT EXISTS `conversation_contexts` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT        COMMENT '主键',
  `conversation_id`   BIGINT UNSIGNED NOT NULL                       COMMENT '所属会话 ID（逻辑外键 → conversations.id，唯一）',
  `user_id`           BIGINT UNSIGNED NOT NULL                       COMMENT '所属用户 ID（冗余便于查询）',
  `last_intent`       VARCHAR(32)  DEFAULT NULL                      COMMENT '上一轮意图: order_query/refund_query/...',
  `current_order_no`  VARCHAR(64)  DEFAULT NULL                      COMMENT '当前会话锁定的订单号（OrderCard 跳转 / Resolver 选定）',
  `flow_state`        VARCHAR(64)  DEFAULT NULL                      COMMENT '业务流状态: refund.completed / logistics.tracking',
  `resolved_orders`   JSON         DEFAULT NULL                      COMMENT 'Resolver 推断过的订单列表 [{order_no, status, picked_at}]',
  `flow_payload`      JSON         DEFAULT NULL                      COMMENT '业务流中间态（dict）',
  `create_time`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`           TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_conv_ctx_conv_id` (`conversation_id`),
  KEY `idx_conv_ctx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话上下文表（M14）';