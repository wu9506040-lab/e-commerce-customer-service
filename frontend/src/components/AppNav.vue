<script setup lang="ts">
/**
 * 顶栏（京东红电商风）
 * - 左：logo「智选客服」+ 红色装饰
 * - 中：搜索框（可输入回车跳转 /shop）
 * - 右：商品/客服/关于 + 我的订单/头像菜单
 */
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getMe, logout, isAuthed } from '../api';
import type { User } from '../types';

const route = useRoute();
const router = useRouter();

const user = ref<User | null>(null);
const menuOpen = ref(false);
const searchText = ref('');

const showSearch = computed(() => route.name === 'shop' || route.name === 'home');

async function loadUser() {
  try {
    user.value = await getMe();
  } catch {
    user.value = null;
  }
}

// 监听登录态变化：登录/登出后 AppNav 立即刷新（M13 修复：原版只在 onMounted 调一次）
watch(isAuthed, (val) => {
  if (val === true) loadUser();
  else user.value = null;
});

function toggleMenu() {
  menuOpen.value = !menuOpen.value;
}

function closeMenu(e: MouseEvent) {
  const target = e.target as HTMLElement;
  if (!target.closest('.user-menu')) menuOpen.value = false;
}

function go(path: string) {
  router.push(path);
  menuOpen.value = false;
}

function onSearch() {
  const q = searchText.value.trim();
  router.push({ name: 'shop', query: q ? { q } : {} });
}

async function onLogout() {
  await logout();
  user.value = null;
  menuOpen.value = false;
  router.push('/demo');
}

onMounted(() => {
  loadUser();
  document.addEventListener('click', closeMenu);
});
onUnmounted(() => document.removeEventListener('click', closeMenu));
</script>

<template>
  <header class="topbar">
    <div class="topbar-inner">
      <!-- Logo -->
      <div class="logo" @click="router.push(user ? '/chat' : '/demo')">
        <div class="logo-mark">智</div>
        <div class="logo-text">
          <span class="logo-name">智选客服</span>
          <span class="logo-en">ZHIXUAN</span>
        </div>
      </div>

      <!-- 搜索框（仅在 /shop 等商品页显示） -->
      <div v-if="showSearch" class="search">
        <input
          v-model="searchText"
          type="text"
          placeholder="搜索 商品 / 订单 / 政策"
          @keyup.enter="onSearch"
        />
        <button class="search-btn" @click="onSearch">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <circle cx="7" cy="7" r="5" stroke="#fff" stroke-width="1.5"/>
            <line x1="11" y1="11" x2="14" y2="14" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          搜索
        </button>
      </div>
      <div v-else class="spacer"></div>

      <!-- 右侧 -->
      <nav class="nav-links">
        <a v-if="user" class="nav-link" @click="go('/shop')">商品</a>
        <a v-if="user" class="nav-link" @click="go('/chat')">客服</a>
        <a class="nav-link" @click="go('/demo')">关于</a>

        <div class="user-area">
          <template v-if="user">
            <div class="user-menu">
              <button class="avatar" @click="toggleMenu">
                {{ (user.display_name || user.username).charAt(0).toUpperCase() }}
              </button>
              <div v-if="menuOpen" class="dropdown">
                <div class="dropdown-header">
                  <div class="dropdown-name">{{ user.display_name || user.username }}</div>
                  <div class="dropdown-role">{{ user.role === 'admin' ? '管理员' : '普通用户' }}</div>
                </div>
                <hr/>
                <a v-if="user.role === 'admin'" class="dropdown-item" @click="go('/admin/analytics')">运营面板</a>
                <a class="dropdown-item" @click="go('/profile')">个人中心</a>
                <a class="dropdown-item" @click="go('/chat')">我的对话</a>
                <hr/>
                <a class="dropdown-item danger" @click="onLogout">退出登录</a>
              </div>
            </div>
          </template>
          <template v-else>
            <a class="nav-link login-link" @click="go('/login')">登录</a>
            <a class="nav-link register-link" @click="go('/login?tab=register')">注册</a>
          </template>
        </div>
      </nav>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  height: 72px;
  background: var(--gray-0);
  border-bottom: var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
  flex-shrink: 0;
}
.topbar-inner {
  max-width: var(--content-max);
  height: 100%;
  margin: 0 auto;
  padding: 0 var(--sp-6);
  display: flex;
  align-items: center;
  gap: var(--sp-8);
}

/* Logo */
.logo {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  cursor: pointer;
  user-select: none;
  flex-shrink: 0;
}
.logo-mark {
  width: 44px;
  height: 44px;
  background: var(--jd-red);
  color: #fff;
  font-size: 24px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.logo-text {
  display: flex;
  flex-direction: column;
  line-height: 1;
}
.logo-name {
  font-size: 20px;
  font-weight: 700;
  color: var(--gray-800);
}
.logo-en {
  font-size: 12px;
  color: var(--gray-500);
  letter-spacing: 1px;
  margin-top: 3px;
}

/* 搜索 */
.search {
  flex: 1;
  max-width: 500px;
  display: flex;
  border: 2px solid var(--jd-red);
  height: 44px;
}
.search input {
  flex: 1;
  border: none;
  outline: none;
  padding: 0 var(--sp-4);
  font-size: var(--fs-base);
  background: var(--gray-0);
  color: var(--gray-800);
}
.search input::placeholder {
  color: var(--gray-500);
}
.search-btn {
  width: 84px;
  border: none;
  background: var(--jd-red);
  color: #fff;
  font-size: var(--fs-base);
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.search-btn:hover {
  background: var(--jd-red-hover);
}

.spacer { flex: 1; }

/* 右侧导航 */
.nav-links {
  display: flex;
  align-items: center;
  gap: var(--sp-6);
}
.nav-link {
  font-size: var(--fs-base);
  color: var(--gray-700);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.nav-link:hover {
  color: var(--jd-red);
}
.login-link { color: var(--gray-600); }
.register-link {
  color: var(--jd-red);
  font-weight: 600;
}

/* 用户菜单 */
.user-area {
  position: relative;
}
.user-menu {
  position: relative;
}
.avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-base);
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.avatar:hover {
  background: var(--jd-red-hover);
}
.dropdown {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 220px;
  background: var(--gray-0);
  border: var(--border);
  box-shadow: var(--shadow-md);
  z-index: 200;
}
.dropdown hr {
  border: none;
  border-top: var(--border);
  margin: 0;
}
.dropdown-header {
  padding: var(--sp-4);
  background: var(--gray-50);
}
.dropdown-name {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--gray-800);
}
.dropdown-role {
  font-size: var(--fs-sm);
  color: var(--gray-500);
  margin-top: 4px;
}
.dropdown-item {
  display: block;
  padding: var(--sp-4);
  font-size: var(--fs-base);
  color: var(--gray-700);
  cursor: pointer;
}
.dropdown-item:hover {
  background: var(--gray-50);
  color: var(--jd-red);
}
.dropdown-item.danger:hover {
  background: var(--jd-red-light);
  color: var(--jd-red-dark);
}
</style>