<script setup lang="ts">
/**
 * 个人中心（M9 新增）
 * 用户卡片 + 统计 + 我的订单列表
 */
import { ref, onMounted, computed } from 'vue';
import { useRouter } from 'vue-router';
import { getMe, listMyOrders, logout } from '../api';
import type { User, OrderSummary } from '../types';
import OrderCard from '../components/OrderCard.vue';

const router = useRouter();

const user = ref<User | null>(null);
const orders = ref<OrderSummary[]>([]);
const loading = ref(true);
const error = ref('');

const avatarLetter = computed(() => {
  const name = user.value?.display_name || user.value?.username || '?';
  return name.charAt(0).toUpperCase();
});

const memberSince = computed(() => {
  if (!user.value?.create_time) return '';
  return new Date(user.value.create_time).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    user.value = await getMe();
    if (!user.value) {
      router.push({ name: 'login', query: { redirect: '/profile' } });
      return;
    }
    const orderData = await listMyOrders({ limit: 50 });
    orders.value = orderData.orders;
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

onMounted(load);

async function onLogout() {
  await logout();
  router.push('/demo');
}
</script>

<template>
  <main class="profile">
    <div v-if="loading" class="loading-state">
      <div class="spinner"></div>
      <p>加载中…</p>
    </div>

    <div v-else-if="error" class="error-state">⚠️ {{ error }}</div>

    <template v-else-if="user">
      <!-- 用户卡片 -->
      <section class="user-card">
        <div class="user-banner"></div>
        <div class="user-info">
          <div class="avatar-xl">{{ avatarLetter }}</div>
          <div class="user-meta">
            <h1>
              {{ user.display_name || user.username }}
              <span v-if="user.role === 'admin'" class="admin-badge">管理员</span>
            </h1>
            <p class="username">@{{ user.username }}</p>
            <p v-if="user.email" class="email">📧 {{ user.email }}</p>
            <p class="joined">🗓 加入于 {{ memberSince }}</p>
          </div>
        </div>
      </section>

      <!-- 统计卡片 -->
      <section class="stats-row">
        <div class="stat-card">
          <div class="stat-num">{{ user.message_count ?? 0 }}</div>
          <div class="stat-label">累计消息</div>
        </div>
        <div class="stat-card">
          <div class="stat-num">{{ user.conversation_count ?? 0 }}</div>
          <div class="stat-label">历史会话</div>
        </div>
        <div class="stat-card">
          <div class="stat-num">{{ orders.length }}</div>
          <div class="stat-label">订单总数</div>
        </div>
      </section>

      <!-- 我的订单 -->
      <section class="orders-section">
        <div class="section-head">
          <h2>📦 我的订单</h2>
          <button class="ghost-btn" @click="load">↻ 刷新</button>
        </div>

        <div v-if="orders.length === 0" class="empty-state">
          <p>暂无订单</p>
          <button class="link-btn" @click="router.push('/shop')">去逛逛商品 →</button>
        </div>
        <div v-else class="orders-list">
          <OrderCard
            v-for="o in orders"
            :key="o.order_no"
            :order="o"
            density="list"
          />
        </div>
      </section>

      <!-- 账户操作 -->
      <section class="actions-section">
        <h2>⚙️ 账户</h2>
        <button class="action-btn" disabled>🔑 修改密码（即将开放）</button>
        <button class="action-btn danger" @click="onLogout">🚪 退出登录</button>
      </section>
    </template>
  </main>
</template>

<style scoped>
.profile {
  flex: 1;
  overflow-y: auto;
  max-width: 880px;
  width: 100%;
  margin: 0 auto;
  padding: 24px;
}

/* ============= User Card ============= */
.user-card {
  background: white;
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.05);
  margin-bottom: 20px;
}
.user-banner {
  height: 100px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.user-info {
  padding: 0 24px 20px;
  display: flex;
  gap: 16px;
  align-items: flex-end;
  margin-top: -40px;
}
.avatar-xl {
  width: 80px;
  height: 80px;
  border-radius: 50%;
  background: white;
  color: #4f46e5;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 32px;
  font-weight: 700;
  border: 4px solid white;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  flex-shrink: 0;
}
.user-meta {
  flex: 1;
  padding-bottom: 4px;
}
.user-meta h1 {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 700;
  color: #1f2937;
  display: flex;
  align-items: center;
  gap: 8px;
}
.admin-badge {
  padding: 2px 8px;
  background: linear-gradient(135deg, #fbbf24 0%, #f97316 100%);
  color: white;
  font-size: 11px;
  font-weight: 500;
  border-radius: 10px;
}
.username, .email, .joined {
  margin: 4px 0 0;
  font-size: 13px;
  color: #6b7280;
}

/* ============= Stats ============= */
.stats-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 20px;
}
.stat-card {
  background: white;
  padding: 20px;
  border-radius: 10px;
  text-align: center;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}
.stat-num {
  font-size: 28px;
  font-weight: 700;
  color: #4f46e5;
}
.stat-label {
  margin-top: 4px;
  font-size: 13px;
  color: #6b7280;
}

/* ============= Section Head ============= */
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.section-head h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #1f2937;
}
.ghost-btn {
  padding: 4px 12px;
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  font-size: 13px;
  color: #4b5563;
  cursor: pointer;
}
.ghost-btn:hover {
  background: #f9fafb;
}

/* ============= Orders ============= */
.orders-section {
  margin-bottom: 20px;
}
.orders-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.empty-state {
  background: white;
  padding: 40px;
  border-radius: 10px;
  text-align: center;
  color: #9ca3af;
}
.link-btn {
  margin-top: 8px;
  padding: 6px 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
}

/* ============= Actions ============= */
.actions-section {
  background: white;
  border-radius: 10px;
  padding: 20px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}
.actions-section h2 {
  margin: 0 0 12px;
  font-size: 18px;
  font-weight: 600;
  color: #1f2937;
}
.action-btn {
  display: block;
  width: 100%;
  text-align: left;
  padding: 12px 16px;
  margin-bottom: 8px;
  background: #f9fafb;
  border: 1px solid #f3f4f6;
  border-radius: 8px;
  font-size: 14px;
  color: #4b5563;
  cursor: pointer;
  transition: all 0.15s;
}
.action-btn:not(:disabled):hover {
  background: #f3f4f6;
  border-color: #e5e7eb;
}
.action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.action-btn.danger {
  color: #b91c1c;
}
.action-btn.danger:hover {
  background: #fef2f2;
  border-color: #fecaca;
}

/* ============= Loading / Error ============= */
.loading-state, .error-state {
  text-align: center;
  padding: 80px 20px;
  color: #9ca3af;
}
.error-state {
  color: #b91c1c;
}
.spinner {
  width: 28px;
  height: 28px;
  border: 3px solid #e5e7eb;
  border-top-color: #667eea;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto 12px;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
