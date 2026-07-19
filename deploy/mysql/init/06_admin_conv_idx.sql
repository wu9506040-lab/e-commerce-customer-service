-- 06_admin_conv_idx.sql - P4-1 admin 全局会话查询索引
-- 创建时间：2026-07-19
-- 触发：P4-1 admin_conversations 路由上线 + list endpoint 时间窗扫描
-- 依赖：01_schema.sql 中已存在 idx_conversations_user_status_time (user_id, status, last_message_at DESC)
--       （该索引 user-scoped 场景够用；本索引补 admin global 场景）
-- 兼容：脚本幂等（CREATE IF NOT EXISTS 检查 + SELECT 输出）
-- 升级：L1（additive，无破坏性，符合 CLAUDE.md §9.4.4 L1 变更门槛）

SET NAMES utf8mb4;

-- 检查索引是否已存在（idempotent）
SET @idx_exists := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'conversations'
      AND INDEX_NAME = 'idx_conversations_status_time'
);

-- 动态 DDL（已存在则跳过）
SET @sql := IF(
    @idx_exists = 0,
    'CREATE INDEX idx_conversations_status_time ON conversations (status, last_message_at DESC) COMMENT ''admin 全局会话查询索引（按状态 + 时间倒序，admin_conversations.py 专用）''',
    'SELECT ''idx_conversations_status_time already exists'' AS info'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 验证索引已生效
SELECT
    INDEX_NAME,
    COLUMN_NAME,
    SEQ_IN_INDEX,
    COLLATION
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'conversations'
  AND INDEX_NAME = 'idx_conversations_status_time'
ORDER BY SEQ_IN_INDEX;

SELECT 'ADMIN_CONV_IDX_OK' AS status;
