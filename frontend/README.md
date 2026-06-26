# Frontend (Vue3)

> 占位 — 待开发。

## 技术栈

| 类别 | 选型 |
|---|---|
| 框架 | Vue3 + Vite + TypeScript |
| UI 库 | Element Plus |
| 状态管理 | Pinia |
| HTTP | Axios |
| 路由 | Vue Router 4 |
| 样式 | SCSS |

## 计划目录结构（待定）

```
frontend/
├── src/
│   ├── main.ts             # 入口
│   ├── App.vue
│   ├── api/                # API 客户端
│   ├── views/              # 页面
│   ├── components/         # 组件
│   ├── stores/             # Pinia stores
│   ├── router/             # 路由
│   ├── composables/        # 组合式函数
│   ├── types/              # TypeScript 类型
│   └── utils/              # 工具
├── public/
├── index.html
├── package.json
├── vite.config.ts
├── Dockerfile              # 多阶段：node build → nginx serve
├── nginx.conf
└── .dockerignore
```

## 开发

代码就绪后，本地开发：

```bash
cd frontend
npm install
npm run dev  # 默认 http://localhost:5173
```

容器内（生产模式）：

```bash
docker compose --env-file ../deploy/.env.dev up -d frontend
```
