#!/usr/bin/env python
"""V14（2.4）badcase 池迁移：列名规避 MySQL 保留字（幂等）

背景：badcase_candidates 首版列名 signal 撞上 MySQL 8 保留字（SIGNAL 语句），
ORM 自动加引号能跑，但手写 SQL/BI 直查即炸（2026-10-11 实测）。列改名
signal_type（ORM 属性/API 键不变）。

行为：
- 表不存在 → SKIP（新环境由 Base.metadata.create_all 直接建正确结构）
- 表存在且列名为 signal → CHANGE 为 signal_type
- 已是 signal_type → SKIP
"""
import os
import sys

from sqlalchemy import create_engine, text


def main() -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("FAIL: 需要 DATABASE_URL 环境变量")
        return 1
    if "@mysql:" in url:
        print("FAIL: DATABASE_URL 是容器内主机名 mysql，宿主机请显式传 localhost:3307")
        return 1
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as conn:
        tbl = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'badcase_candidates'"
        )).scalar()
        if not tbl:
            print("SKIP: badcase_candidates 表不存在（启动建表会直接建 signal_type 版）")
            return 0
        old = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'badcase_candidates' "
            "AND COLUMN_NAME = 'signal'"
        )).scalar()
        if old:
            conn.execute(text(
                "ALTER TABLE badcase_candidates "
                "CHANGE COLUMN `signal` signal_type VARCHAR(16) NOT NULL "
                "COMMENT '信号类型 thumbs_down|low_confidence|guard_blocked'"
            ))
            conn.commit()
            print("PASS: badcase_candidates.signal -> signal_type 已改名")
        else:
            print("SKIP: 已是 signal_type，无需迁移")
        # 索引跟随列重命名（MySQL 按列维护、索引名保持旧 ix 名），校验没丢即可
        idx = conn.execute(text(
            "SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'badcase_candidates' "
            "AND COLUMN_NAME = 'signal_type'"
        )).scalar()
        print(f"INFO: signal_type 上索引数={idx}（ix 旧名保留属正常）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
