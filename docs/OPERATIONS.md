# 运维指南

> 端口、数据卷、生产部署、故障排查、日常命令。

---

## 端口清单

| 服务 | 端口 | 用途 |
|------|------|------|
| frontend | 5173 | Web UI（nginx 80 映射）|
| api | 8000 | FastAPI REST + Swagger `/docs` |
| qdrant | 6333 / 6334 | REST API / gRPC |
| redis | 6379 | 缓存 |
| mysql | 3307 → 3306 | 数据库（宿主 3307 避开本机）|

---

## 数据卷位置

所有数据持久化到 **E 盘**（避免 C 盘紧张）：

| 卷 | 路径 |
|----|------|
| Qdrant | `E:\DockerData\volumes\qdrant` |
| Redis | `E:\DockerData\volumes\redis` |
| MySQL | `E:\DockerData\volumes\mysql` |
| 上传文件 | `E:\DockerData\volumes\uploads` |
| 应用日志 | `E:\DockerData\volumes\logs` |

---

## 常用命令

```powershell
# 启动
docker compose --env-file .env.dev up -d

# 实时日志
docker compose --env-file .env.dev logs -f api

# 进入容器调试
docker compose --env-file .env.dev exec api bash

# 停止（保留数据）
docker compose --env-file .env.dev down

# 重置（⚠️ 删数据）
docker compose --env-file .env.dev down -v

# 改代码后重建 API
docker compose --env-file .env.dev build api
docker compose --env-file .env.dev up -d api
```

---

## 生产部署

### 1. 准备 `.env.prod`

```bash
cp .env.example .env.prod
```

### 2. 关键差异

| 项 | 开发 | 生产 |
|----|------|------|
| `JWT_SECRET` | 可用占位符 | 必须 `openssl rand -hex 32` 生成 |
| `COOKIE_SECURE` | `false` | `true`（要求 HTTPS）|
| `APP_ENV` | `dev` | `prod` |
| `LOG_LEVEL` | `INFO` | `WARNING` |
| `FRONTEND_PORT` | 默认 | 暴露（默认 5173）|

### 3. 启动

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod up -d --build
```

### 4. 升级单个服务

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod build api
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod up -d api
```

### 5. HTTPS 终止

生产环境需在 nginx / caddy / Cloudflare 终止 HTTPS，配置示例：

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://localhost:5173;
    }

    location /api/ {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_http_version 1.1;
    }
}
```

---

## 故障排查

| 症状 | 排查 |
|------|------|
| `/chat` 返回 500 "QWEN_API_KEY 未配置" | `.env.dev` 中 `QWEN_API_KEY` 未填或为占位符；重启 `docker compose up -d api` |
| `/health` 提示 `redis: down` | `docker logs customer-service-redis` 看启动错；常见是 WSL2 重启后 DNS 缓存失效，等 5s 重试 |
| `curl localhost:8000` 连不上 | `docker ps` 看 api 容器是否 Running；`docker logs customer-service-api` 看启动日志 |
| 端口 3306/6379/6333 占用 | MySQL 宿主端口已改 3307；Redis/Qdrant 端口如冲突，编辑 `.env.dev` 加 `ports:` 映射或停本地服务 |
| MySQL 容器 OOM 反复重启 | 数据量大时调高 `deploy.resources.limits.memory`（compose.yml 默认 512M）|
| 前端 SSE 流式断流 | Nginx 反代需加 `proxy_buffering off` + `proxy_http_version 1.1`；浏览器 devtools 看 network 是否 `text/event-stream` |
| `npm run build` TS 报错 | `cd frontend && rm -rf node_modules package-lock.json && npm install` |
| `python-bcrypt` 安装失败（Windows 本地）| Docker 容器内无此问题；本地调试用 `pip install bcrypt` 而非 `pip install bcrypt-binary` |

---

## 监控与日志

### 应用日志

```bash
# 实时跟踪
docker compose --env-file .env.dev logs -f api

# 最近 100 行
docker compose --env-file .env.dev logs --tail=100 api

# 按时间过滤
docker compose --env-file .env.dev logs --since="2026-06-26T10:00:00" api
```

### 健康检查

```bash
# 单次检查
curl http://localhost:8000/health

# 持续监控（每 5 秒）
watch -n 5 "curl -s http://localhost:8000/health | jq ."
```

### 数据库备份

```bash
# 导出 MySQL
docker exec customer-service-mysql mysqldump \
  -u root -p"$MYSQL_ROOT_PASSWORD" \
  --all-databases > backup_$(date +%Y%m%d).sql

# 恢复
cat backup_20260626.sql | docker exec -i customer-service-mysql mysql \
  -u root -p"$MYSQL_ROOT_PASSWORD"
```

---

## 性能调优

### Qdrant 索引

```python
# 如数据量大，调高 HNSW 参数
HNSW_CONFIG = {
    "m": 16,             # 默认 16，每节点连接数
    "ef_construct": 100, # 默认 100，构建时搜索深度
}
```

### MySQL 连接池

`backend/app/core/config.py`：

```python
DB_POOL_SIZE: int = 10      # 默认连接数
DB_MAX_OVERFLOW: int = 20   # 高峰溢出
DB_POOL_TIMEOUT: int = 30   # 获取连接超时
```

### Redis 内存

```yaml
# docker-compose.yml
redis:
  command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
```

---

## 安全建议

| 项 | 配置 |
|----|------|
| JWT Secret | ≥ 32 字符随机（`openssl rand -hex 32`）|
| 密码 | bcrypt 哈希（项目已实现，禁止明文）|
| CORS | 白名单配置，不用 `*` |
| Cookie | 生产环境 `Secure` + `HttpOnly` + `SameSite=Lax` |
| API Key | 仅存 `.env`，**禁止**硬编码到代码 |
| 数据库 | 关闭公网访问，仅 Docker 网络互通 |
| 容器 | 定期 `docker compose pull` 更新基础镜像 |