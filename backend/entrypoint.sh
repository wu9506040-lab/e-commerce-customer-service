#!/usr/bin/env bash
# =============================================================
# M3 部署治本（2026-07-19）：API 容器启动入口
#
# 职责：
#   1. 启动时检测 KB 目录是否挂载（volume mount 治本 V4 regression 根因）
#   2. 检测 Qdrant collection 是否为空
#   3. 如果 KB 目录非空 且 Qdrant 为空 → 自动跑 ingest_ecommerce_kb.py
#   4. 如果两者都非空 → 跳过 ingest（idempotent 设计，无重复入库风险）
#   5. 启动 uvicorn（业务主进程）
#
# 退出策略：ingest 失败不阻塞启动（外层 try/except + os.system）
# =============================================================

set -e

KB_DIR="/app/docs/ecommerce_kb"
QDRANT_URL="${QDRANT_URL:-http://customer-service-qdrant:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-faq_v1}"

echo "[entrypoint] ===== M3 启动检测 ====="
echo "[entrypoint] KB_DIR=$KB_DIR"
echo "[entrypoint] QDRANT_URL=$QDRANT_URL"
echo "[entrypoint] QDRANT_COLLECTION=$QDRANT_COLLECTION"

# 1. KB 目录存在性 + 非空检测
if [ ! -d "$KB_DIR" ] || [ -z "$(ls -A "$KB_DIR" 2>/dev/null)" ]; then
  echo "[entrypoint] ⚠️  KB 目录为空或缺失（$KB_DIR），跳过 ingest"
else
  KB_FILE_COUNT=$(ls -1 "$KB_DIR"/*.json 2>/dev/null | wc -l)
  echo "[entrypoint] KB 目录含 $KB_FILE_COUNT 个 JSON 文件"

  # 2. Qdrant collection 检测 + 自动 ingest
  python <<PYEOF
import os
import sys

try:
    from qdrant_client import QdrantClient
except ImportError:
    print("[entrypoint] ⚠️  qdrant_client 未安装，跳过 ingest 检测")
    sys.exit(0)

try:
    client = QdrantClient(url="$QDRANT_URL", timeout=10)
    points_count = 0
    try:
        info = client.get_collection("$QDRANT_COLLECTION")
        points_count = info.points_count
    except Exception as inner_e:
        # 404（collection 不存在）→ 视为空集合（需要 ingest）
        err_msg = str(inner_e)
        if "404" in err_msg or "Not found" in err_msg:
            print(f"[entrypoint] ℹ️  collection '{os.environ.get('QDRANT_COLLECTION', '$QDRANT_COLLECTION')}' 不存在，按空集合处理")
            points_count = 0
        else:
            raise inner_e
    print(f"[entrypoint] Qdrant collection 当前 vectors: {points_count}")

    if points_count == 0:
        print("[entrypoint] 🔄 Qdrant 为空，触发自动 ingest")
        ret = os.system("PYTHONPATH=/app python /app/scripts/ingest_ecommerce_kb.py")
        if ret == 0:
            print("[entrypoint] ✅ 自动 ingest 完成")
        else:
            print(f"[entrypoint] ⚠️  自动 ingest 退出码={ret}，不阻塞启动")
    else:
        print(f"[entrypoint] ✅ Qdrant 已有 {points_count} vectors，跳过 ingest")
except Exception as e:
    print(f"[entrypoint] ⚠️  Qdrant 检测异常: {e}")
    print("[entrypoint] 跳过 ingest（不阻塞启动）")
PYEOF
fi

echo "[entrypoint] ===== 启动 uvicorn ====="
exec uvicorn app.main:app --host 0.0.0.0 --port 8000