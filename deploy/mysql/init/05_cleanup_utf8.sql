-- 清空被双重编码的数据（utf8mb4 重灌）
SET NAMES utf8mb4;
DELETE FROM order_items;
DELETE FROM orders;
DELETE FROM products;
DELETE FROM users WHERE username NOT IN ('admin', 'demotest');
SELECT 'CLEAN_OK' AS status;