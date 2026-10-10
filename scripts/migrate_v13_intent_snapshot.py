#!/usr/bin/env python
"""V13（2.3）列迁移：messages 表加 intent_snapshot JSON 列（幂等）

项目无 alembic（Base.metadata.create_all 只建表不改列），存量表加列走本脚本。
幂等：先查 information_schema，列已存在则跳过——可重复执行。

用法（宿主机，指向 docker 映射端口）：
    DATABASE_URL='mysql+pymysql://cs_user:<pwd>@localhost:3307/customer_service?charset=utf8mb4' \
    PYTHONIOENCODING=utf-8 python scripts/migrate_v13_intent_snapshot.py
"""
import os
import sys

from sqlalchemy import create_engine, text


def main() -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("FAIL: 需要 DATABASE_URL 环境变量")
        return 1
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'messages' "
            "AND COLUMN_NAME = 'intent_snapshot'"
        )).scalar()
        if exists:
            print("SKIP: messages.intent_snapshot 已存在，无需迁移")
            return 0
        conn.execute(text(
            "ALTER TABLE messages ADD COLUMN intent_snapshot JSON NULL "
            "COMMENT 'V13(2.3) 意图决策快照 {primary,method,confidence,intents,upgraded}' "
            "AFTER scores"
        ))
        conn.commit()
        print("PASS: messages.intent_snapshot 列已添加")
        return 0


if __name__ == "__main__":
    sys.exit(main())
