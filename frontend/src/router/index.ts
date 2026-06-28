// =============================================================
// Vue Router 4 - 多页路由（M9 重构：从单 ChatPage 升级到产品级 SPA）
//
// 路由表：
//   /login       登录 + 注册（双 tab）
//   /demo        演示模式首页（能力展示，未登录可访问）
//   /shop        商品橱窗（公开）
//   /shop/:sku   商品详情（公开）
//   /chat        对话页（需登录）
//   /chat/:sessionId 恢复指定会话（需登录）
//   /profile     个人中心（需登录）
//
// 路由守卫：
//   - meta.requiresAuth 路由未登录跳 /login
//   - meta.guestOnly 路由已登录跳 /shop（如 /login）
// =============================================================
import {
  createRouter,
  createWebHistory,
  type RouteLocationNormalized,
  type RouteRecordRaw,
} from 'vue-router';

import { getMe, isAuthed } from '../api';

const routes: RouteRecordRaw[] = [
  // 默认重定向
  {
    path: '/',
    redirect: () => {
      // 未登录去 /demo，已登录去 /shop
      // 注：不能用 document.cookie 判断，cookie 是 httpOnly JS 读不到
      return isAuthed.value === true ? '/shop' : '/demo';
    },
  },

  // 演示模式首页（公开）
  {
    path: '/demo',
    name: 'demo',
    component: () => import('../views/DemoLanding.vue'),
    meta: { title: '智能客服演示' },
  },

  // 登录 + 注册（guest only）
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginPage.vue'),
    meta: { title: '登录 / 注册', guestOnly: true },
  },

  // 商品橱窗（公开，但需登录才能问客服）
  {
    path: '/shop',
    name: 'shop',
    component: () => import('../views/ShopPage.vue'),
    meta: { title: '商品橱窗' },
  },
  {
    path: '/shop/:sku',
    name: 'product-detail',
    component: () => import('../views/ProductDetail.vue'),
    meta: { title: '商品详情' },
  },

  // 对话（需登录）
  {
    path: '/chat',
    name: 'chat',
    component: () => import('../views/ChatPage.vue'),
    meta: { title: '智能客服', requiresAuth: true },
  },
  {
    path: '/chat/:sessionId',
    name: 'chat-session',
    component: () => import('../views/ChatPage.vue'),
    meta: { title: '智能客服', requiresAuth: true },
  },

  // 个人中心（需登录）
  {
    path: '/profile',
    name: 'profile',
    component: () => import('../views/ProfilePage.vue'),
    meta: { title: '个人中心', requiresAuth: true },
  },

  // 404 fallback
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

// =============================================================
// 路由守卫：未登录拦截 + 已登录重定向
// =============================================================
router.beforeEach(async (to: RouteLocationNormalized) => {
  // 首次加载时探测登录态（httpOnly Cookie 不可被 document.cookie 读取，必须调 API）
  if (isAuthed.value === null) {
    await getMe();
  }

  if (to.meta.requiresAuth && !isAuthed.value) {
    return { name: 'login', query: { redirect: to.fullPath } };
  }
  if (to.meta.guestOnly && isAuthed.value) {
    return { name: 'shop' };
  }

  // 标题
  if (to.meta.title) {
    document.title = `${to.meta.title} - 智选电商客服`;
  }
  return true;
});

export default router;
