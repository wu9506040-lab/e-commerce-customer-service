-- =============================================================
-- 智能客服 Agent 系统 - MySQL Seed 数据
-- 仅首次初始化时执行（docker-entrypoint-initdb.d 只在 data 目录为空时跑）
--
-- ⚠️ 出于安全考虑，本文件不预置默认密码 hash
--    原因: SQL 文件是明文落盘的，如果预置 admin123 的 bcrypt hash，
--          任何拿到仓库的人都能用同一密码登录（除非重置）
--    正确做法: 首次部署后用 auth 模块 / Python 脚本设置密码
--
-- 占位符说明:
--   password_hash = '__SET_VIA_AUTH_MODULE__'
--   ↑ 业务层看到此值应拒绝登录并提示「请通过管理后台重置密码」
-- =============================================================

USE `customer_service`;

-- =============================================================
-- 默认管理员账号（待设置密码）
-- =============================================================
INSERT INTO `users` (
  `username`,
  `password_hash`,
  `display_name`,
  `email`,
  `role`,
  `status`
) VALUES (
  'admin',
  '__SET_VIA_AUTH_MODULE__',
  '系统管理员',
  'admin@customer-service.local',
  'admin',
  1
);

-- =============================================================
-- 首次部署后设置密码的方法（任选其一）
-- =============================================================
-- 方式 A: Docker exec 进 API 容器用 Python bcrypt 脚本（推荐）
--
--   docker exec -it customer-service-api python -c "
--   import bcrypt
--   h = bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt(rounds=12)).decode()
--   print(h)
--   "
--   # 拿到 hash 后 SQL 更新：
--   UPDATE users SET password_hash = '<上面输出的hash>' WHERE username = 'admin';
--
-- 方式 B: 等后续 auth 模块提供 CLI 命令（计划中）
-- =============================================================
