<script setup lang="ts">
// 全局顶栏 - logo + 导航 + 用户菜单
import { ref, onMounted, computed } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { getMe, logout } from '../api';
import type { User } from '../types';

const router = useRouter();
const route = useRoute();
const user = ref<User | null>(null);
const menuOpen = ref(false);

async function refreshUser() {
  try {
    user.value = await getMe();
  } catch {
    user.value = null;
  }
}

onMounted(refreshUser);

// 监听路由变化时刷新 user（处理刚登录/登出的情况）
router.afterEach(() => {
  refreshUser();
});

const navLinks = [
  { name: 'demo', label: '首页', show: 'always' as const },
  { name: 'shop', label: '商品', show: 'always' as const },
  { name: 'chat', label: '客服', show: 'loggedIn' as const },
  { name: 'profile', label: '我的', show: 'loggedIn' as const },
];

const visibleLinks = computed(() =>
  navLinks.filter((l) => l.show === 'always' || user.value),
);

async function onLogout() {
  await logout();
  user.value = null;
  menuOpen.value = false;
  router.push('/demo');
}

function goLogin() {
  router.push({ name: 'login', query: { redirect: route.fullPath } });
}

function goRegister() {
  router.push({ name: 'login', query: { redirect: route.fullPath, tab: 'register' } });
}

// 用户首字母（用于头像占位）
const avatarLetter = computed(() => {
  const name = user.value?.display_name || user.value?.username || '?';
  return name.charAt(0).toUpperCase();
});
</script>

<template>
  <header class="app-nav">
    <div class="nav-inner">
      <!-- Logo -->
      <router-link to="/" class="logo">
        <span class="logo-icon">🤖</span>
        <span class="logo-text">智选客服</span>
      </router-link>

      <!-- 主导航 -->
      <nav class="nav-links">
        <router-link
          v-for="link in visibleLinks"
          :key="link.name"
          :to="{ name: link.name }"
          class="nav-link"
          active-class="active"
        >
          {{ link.label }}
        </router-link>
      </nav>

      <!-- 用户区 -->
      <div class="user-area">
        <template v-if="user">
          <div class="user-menu" @click.stop="menuOpen = !menuOpen">
            <div class="avatar">{{ avatarLetter }}</div>
            <span class="username">{{ user.display_name || user.username }}</span>
            <span class="caret">▾</span>
            <div v-if="menuOpen" class="dropdown" @click.stop>
              <router-link to="/profile" class="dropdown-item" @click="menuOpen = false">
                个人中心
              </router-link>
              <button class="dropdown-item" @click="onLogout">退出登录</button>
            </div>
          </div>
        </template>
        <template v-else>
          <button class="btn-ghost" @click="goLogin">登录</button>
          <button class="btn-primary" @click="goRegister">注册</button>
        </template>
      </div>
    </div>
  </header>
</template>

<style scoped>
.app-nav {
  background: white;
  border-bottom: 1px solid #e5e7eb;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  position: sticky;
  top: 0;
  z-index: 100;
  flex-shrink: 0;
}
.nav-inner {
  max-width: 1280px;
  margin: 0 auto;
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 32px;
}
.logo {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 700;
  color: #1f2937;
}
.logo-icon {
  font-size: 22px;
}
.logo-text {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
.nav-links {
  display: flex;
  gap: 4px;
  flex: 1;
}
.nav-link {
  padding: 8px 14px;
  border-radius: 6px;
  font-size: 14px;
  color: #6b7280;
  transition: all 0.15s;
}
.nav-link:hover {
  background: #f3f4f6;
  color: #1f2937;
}
.nav-link.active {
  background: #eef2ff;
  color: #4f46e5;
  font-weight: 500;
}
.user-area {
  display: flex;
  align-items: center;
  gap: 10px;
}
.user-menu {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px 4px 4px;
  border-radius: 24px;
  cursor: pointer;
  transition: background 0.15s;
}
.user-menu:hover {
  background: #f3f4f6;
}
.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
}
.username {
  font-size: 14px;
  color: #1f2937;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.caret {
  font-size: 10px;
  color: #9ca3af;
}
.dropdown {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  min-width: 160px;
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  padding: 4px;
}
.dropdown-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 12px;
  font-size: 14px;
  color: #1f2937;
  background: none;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.dropdown-item:hover {
  background: #f3f4f6;
}
.btn-ghost {
  padding: 6px 14px;
  background: white;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 14px;
  color: #1f2937;
  cursor: pointer;
}
.btn-ghost:hover {
  background: #f9fafb;
  border-color: #9ca3af;
}
.btn-primary {
  padding: 6px 14px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.btn-primary:hover {
  opacity: 0.92;
}
</style>
