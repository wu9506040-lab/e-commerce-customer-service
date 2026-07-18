-- =============================================================
-- 智能客服 Agent 系统 - 商品 seed（M1 电商升级补 seed）
-- 原因：01_schema.sql 只 CREATE TABLE products，02_seed.sql 没 INSERT 数据
--       → 每次 ECS 部署 /shop 都是空
-- 修复：补 10 个商品（手机/耳机/手表/平板/笔记本/键盘/鼠标 全类目）
-- 命名规范：SKU001-SKU010（M1 升级约定）
-- 重要：执行必须 `mysql --default-character-set=utf8mb4`，否则 UTF-8 字节被当 latin1 双重编码
--       容器自动 init 走 docker-entrypoint-initdb.d 也已 SET NAMES utf8mb4 兼容
-- =============================================================
SET NAMES utf8mb4;
USE `customer_service`;

-- 清空旧数据（幂等）
DELETE FROM `products`;

-- 10 个商品 seed
INSERT INTO `products`
  (`sku`, `name`, `description`, `price`, `attributes`, `review_text`, `stock`, `status`, `deleted`)
VALUES
  ('SKU001', '智选 Z1 旗舰手机', '6.7 寸 OLED 屏 · 骁龙 8 Gen3 · 5000mAh 大电池 · 2 亿像素主摄', 3999.00,
   '{"category":"手机","brand":"智选","ram":"12GB","storage":"256GB","color":"星空黑","screen":"6.7 inch","battery":"5000mAh"}',
   '手感不错，续航很强，2 亿像素拍照清晰，骁龙 8 Gen3 打游戏不卡。', 100, 1, 0),
  ('SKU002', '智选 Z2 拍照手机', '6.5 寸曲面屏 · 三星 5000 万主摄 · 轻薄机身 7.5mm', 2999.00,
   '{"category":"手机","brand":"智选","ram":"8GB","storage":"128GB","color":"云雾白","screen":"6.5 inch"}',
   '曲面屏手感好，拍照色彩讨喜，轻薄适合女生。', 80, 1, 0),
  ('SKU003', '智选 X1 千元机', '6.4 寸高刷屏 · 6000mAh 超大电池 · 高性价比学生机', 999.00,
   '{"category":"手机","brand":"智选","ram":"6GB","storage":"128GB","color":"冰川蓝","battery":"6000mAh"}',
   '千元档位王者，续航无敌，给爸妈买的最合适。', 200, 1, 0),

  ('SKU004', '智选 E1 蓝牙耳机', '主动降噪 · 30h 超长续航 · Hi-Fi 级音质 · 入耳式', 499.00,
   '{"category":"耳机","brand":"智选","type":"入耳式","anc":true,"battery":"30h"}',
   '降噪效果惊艳，地铁通勤神器，戴久了耳朵也不疼。', 150, 1, 0),
  ('SKU005', '智选 E2 头戴耳机', '40mm 大动圈 · 主动降噪 · 50h 续航 · 折叠便携', 799.00,
   '{"category":"耳机","brand":"智选","type":"头戴式","anc":true,"battery":"50h"}',
   '在家听歌神器，降噪一流，戴着很舒服。', 60, 1, 0),

  ('SKU006', '智选 W1 智能手表', '1.43 寸 AMOLED · 100+ 运动模式 · 心率血氧监测 · 14 天续航', 899.00,
   '{"category":"手表","brand":"智选","screen":"1.43 inch","battery":"14d","gps":true}',
   '续航 14 天不是梦，运动模式齐全，外观也很时尚。', 80, 1, 0),

  ('SKU007', '智选 P1 平板电脑', '11 寸 2.5K 全面屏 · 骁龙 8+ · 10000mAh · 配套手写笔', 2799.00,
   '{"category":"平板","brand":"智选","screen":"11 inch","battery":"10000mAh","stylus":true}',
   '看剧学习两不误，2.5K 屏幕清晰，手写笔做笔记很方便。', 50, 1, 0),

  ('SKU008', '智选 L1 轻薄笔记本', '14 寸 2.8K OLED · i7-13700H · 16G+1T · 1.2kg 轻薄', 5999.00,
   '{"category":"笔记本","brand":"智选","cpu":"i7-13700H","ram":"16GB","storage":"1TB","screen":"14 inch","weight":"1.2kg"}',
   '出差党的福音，1.2kg 背着不累，OLED 屏幕做设计很爽。', 30, 1, 0),

  ('SKU009', '智选 K1 机械键盘', '75% 配列 · 客制化轴体 · RGB 背光 · 三模无线', 399.00,
   '{"category":"键盘","brand":"智选","layout":"75%","wireless":true,"rgb":true}',
   '客制化键盘入门首选，敲击手感清脆，码字很爽。', 120, 1, 0),

  ('SKU010', '智选 M1 无线鼠标', '人体工学 · 4000DPI · 静音微动 · 三模无线 · 长续航', 199.00,
   '{"category":"鼠标","brand":"智选","wireless":true,"dpi":4000,"silent":true}',
   '静音办公神器，手感贴合手掌，长时间用不累。', 200, 1, 0);