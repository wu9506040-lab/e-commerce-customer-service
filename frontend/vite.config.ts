import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Vite 配置：开发代理把 /api/* 转发到 FastAPI 后端
// 企业级 /api/ 前缀分层：API 路径空间与 SPA 路由空间隔离
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});