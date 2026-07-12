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
COPY requirements.txt .
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt

# 应用代码
COPY app/ ./app/

# 业务配置（Prompt YAML 等；Sprint 2 起）
COPY config/ ./config/

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 启动
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
