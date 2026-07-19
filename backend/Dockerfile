# =============================================================
# 智能客服后端 - Dockerfile
# 基于 python:3.11-slim（最小化）
# =============================================================

FROM python:3.11-slim AS base

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 工作目录
WORKDIR /app

# 系统依赖（curl 用于 healthcheck，tzdata 用于时区）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# Python 依赖（先复制 requirements 利用 Docker 缓存）
COPY backend/requirements.txt .
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt

# 应用代码
COPY backend/app/ ./app/

# 业务配置（Prompt YAML 等；Sprint 2 起）
COPY backend/config/ ./config/

# 业务脚本（M3 治本：KB ingest 入口；m14_validation 等评测脚本）
COPY scripts/ ./scripts/

# 启动入口（M3 治本：KB 自动检测 + ingest）
COPY backend/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 暴露端口
EXPOSE 8000

# 健康检查（start_period 60s 给 entrypoint KB 检测 + 可选 ingest 留时间）
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 启动入口（M3 治本：自动 KB 检测 + ingest + uvicorn）
ENTRYPOINT ["/app/entrypoint.sh"]
