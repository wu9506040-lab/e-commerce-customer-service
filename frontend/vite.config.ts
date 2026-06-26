import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Vite 配置：开发代理把 /chat /auth /conversations /admin 转发到 FastAPI 后端
// 同源策略：浏览器只看到 5173，所有后端请求通过代理，避免 CORS + cookie 跨域问题
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/chat': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/auth': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/conversations': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});