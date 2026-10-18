<script setup lang="ts">
// V18 管理后台布局：Ant Design Pro 风格（深色侧边栏 + 顶栏 + 内容区）
import { computed, ref } from 'vue';
import { RouterView, useRoute, useRouter } from 'vue-router';

const route = useRoute();
const router = useRouter();

const menus = [
  { path: '/admin/board', icon: '🗂', label: '数据看板' },
  { path: '/admin/analytics', icon: '📊', label: '运营数据驾驶舱' },
  { path: '/admin/kpi', icon: '📈', label: '业务 KPI 大盘' },
  { path: '/admin/xianyu', icon: '💬', label: '闲鱼人工后台' },
  { path: '/admin/accounts', icon: '👥', label: '账号管理' },
  { path: '/admin/handoff', icon: '🎫', label: '人工工单' },
];

const activePath = computed(() => '/' + route.path.split('/')[1] + (route.path.split('/')[2] ? '/' + route.path.split('/')[2] : ''));
const pageTitle = computed(() => (route.meta.title as string) || '管理后台');

const user = ref<{ username: string; display_name?: string } | null>(null);
fetch('/api/auth/me', { credentials: 'include' })
  .then((r) => (r.ok ? r.json() : null))
  .then((d) => { if (d?.user) user.value = d.user; })
  .catch(() => {});

async function onLogout() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
  router.push('/admin/login');
}
</script>

<template>
  <div class="adm-shell">
    <aside class="adm-side">
      <div class="adm-logo">
        <span class="adm-logo-icon">⚡</span>
        <span class="adm-logo-text">智能客服管理台</span>
      </div>
      <nav class="adm-menu">
        <div
          v-for="m in menus"
          :key="m.path"
          class="adm-menu-item"
          :class="{ active: activePath === m.path }"
          @click="router.push(m.path)"
        >
          <span class="adm-menu-icon">{{ m.icon }}</span>
          <span>{{ m.label }}</span>
        </div>
      </nav>
      <div class="adm-side-footer">
        <span class="adm-dot"></span> 系统运行中
      </div>
    </aside>

    <div class="adm-main">
      <header class="adm-header">
        <div class="adm-header-title">{{ pageTitle }}</div>
        <div class="adm-header-right">
          <a class="adm-header-link" href="/shop">商城首页</a>
          <span class="adm-user">
            <span class="adm-avatar">{{ (user?.username ?? 'A').slice(0, 1).toUpperCase() }}</span>
            <span>{{ user?.display_name || user?.username || '管理员' }}</span>
          </span>
          <button class="adm-logout" @click="onLogout">退出</button>
        </div>
      </header>
      <main class="adm-content">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.adm-shell { display: flex; height: 100vh; overflow: hidden; }
.adm-side {
  width: 220px; flex-shrink: 0; background: #001529; color: rgba(255,255,255,0.65);
  display: flex; flex-direction: column;
}
.adm-logo {
  height: 56px; display: flex; align-items: center; gap: 8px; padding: 0 20px;
  color: #fff; font-size: 16px; font-weight: 600; border-bottom: 1px solid rgba(255,255,255,0.08);
}
.adm-logo-icon { font-size: 18px; }
.adm-menu { flex: 1; padding: 8px 0; overflow-y: auto; }
.adm-menu-item {
  display: flex; align-items: center; gap: 10px; padding: 12px 20px;
  cursor: pointer; transition: all 0.2s; font-size: 14px; position: relative;
}
.adm-menu-item:hover { color: #fff; background: rgba(255,255,255,0.06); }
.adm-menu-item.active { color: #fff; background: #1677ff; }
.adm-menu-icon { width: 20px; text-align: center; }
.adm-side-footer {
  padding: 14px 20px; font-size: 12px; border-top: 1px solid rgba(255,255,255,0.08);
  display: flex; align-items: center; gap: 6px;
}
.adm-dot { width: 8px; height: 8px; border-radius: 50%; background: #52c41a; box-shadow: 0 0 6px #52c41a; }
.adm-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.adm-header {
  height: 56px; background: #fff; border-bottom: 1px solid #e5e7eb;
  display: flex; align-items: center; justify-content: space-between; padding: 0 20px;
  box-shadow: 0 1px 4px rgba(0,21,41,0.08); z-index: 10;
}
.adm-header-title { font-size: 15px; font-weight: 600; color: #1f2937; }
.adm-header-right { display: flex; align-items: center; gap: 16px; }
.adm-header-link { font-size: 13px; color: #6b7280; }
.adm-header-link:hover { color: #1677ff; }
.adm-user { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #374151; }
.adm-avatar {
  width: 28px; height: 28px; border-radius: 50%; background: #1677ff; color: #fff;
  display: inline-flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600;
}
.adm-logout {
  border: none; background: transparent; color: #6b7280; cursor: pointer; font-size: 13px;
}
.adm-logout:hover { color: #dc2626; }
.adm-content { flex: 1; overflow: auto; background: #f0f2f5; padding: 16px; }
</style>
