<script setup lang="ts">
/**
 * 个人中心（京东个人中心风）
 * 左侧菜单 + 右侧内容区
 * 无紫色 banner，无头像渐变，纯京东风
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
  return new Date(user.value.create_time).toLocaleDateString('zh-CN');
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

// M10 闭环：订单状态变化 → 重新拉订单列表
async function onOrderChanged(_orderNo: string) {
  await load();
}

// 订单状态分组
const ordersByStatus = computed(() => {
  const groups = {
    pending: [] as OrderSummary[],
    paid: [] as OrderSummary[],
    shipped: [] as OrderSummary[],
    delivered: [] as OrderSummary[],
    completed: [] as OrderSummary[],
    refunded: [] as OrderSummary[],
  };
  for (const o of orders.value) {
    const key = o.status as keyof typeof groups;
    if (key in groups) groups[key].push(o);
    else groups.completed.push(o);
  }
  return groups;
});

const STATUS_TABS = [
  { key: 'all', label: '全部', count: () => orders.value.length },
  { key: 'pending', label: '待付款', count: () => ordersByStatus.value.pending.length },
  { key: 'paid', label: '待发货', count: () => ordersByStatus.value.paid.length },
  { key: 'shipped', label: '运输中', count: () => ordersByStatus.value.shipped.length },
  { key: 'delivered', label: '已签收', count: () => ordersByStatus.value.delivered.length },
];
const activeStatus = ref('all');

const filteredOrders = computed(() => {
  if (activeStatus.value === 'all') return orders.value;
  return ordersByStatus.value[activeStatus.value as keyof typeof ordersByStatus.value] || [];
});
</script>

<template>
  <main class="profile">
    <div v-if="loading" class="loading-state">
      <div class="spinner"></div>
      <p>加载中…</p>
    </div>

    <div v-else-if="error" class="error-state">{{ error }}</div>

    <template v-else-if="user">
      <div class="profile-body">
        <!-- 左侧菜单 -->
        <aside class="sidebar">
          <div class="user-card">
            <div class="avatar">{{ avatarLetter }}</div>
            <div class="user-info">
              <div class="username">
                {{ user.display_name || user.username }}
                <span v-if="user.role === 'admin'" class="admin-tag">管理员</span>
              </div>
              <div class="member">注册于 {{ memberSince }}</div>
            </div>
          </div>

          <nav class="menu">
            <div class="menu-section">
              <div class="menu-title">订单中心</div>
              <a class="menu-item active">
                <span>我的订单</span>
                <span class="count">{{ orders.length }}</span>
              </a>
              <a class="menu-item">
                <span>退款/售后</span>
              </a>
            </div>

            <div class="menu-section">
              <div class="menu-title">账户管理</div>
              <a class="menu-item">
                <span>个人资料</span>
              </a>
              <a class="menu-item">
                <span>收货地址</span>
              </a>
              <a class="menu-item disabled">
                <span>修改密码</span>
                <span class="badge">即将开放</span>
              </a>
            </div>

            <div class="menu-section">
              <button class="logout-btn" @click="onLogout">退出登录</button>
            </div>
          </nav>
        </aside>

        <!-- 右侧内容 -->
        <section class="content">
          <!-- 统计卡片 -->
          <div class="stats">
            <div class="stat-box">
              <div class="stat-num">{{ user.message_count ?? 0 }}</div>
              <div class="stat-label">累计消息</div>
            </div>
            <div class="stat-box">
              <div class="stat-num">{{ user.conversation_count ?? 0 }}</div>
              <div class="stat-label">历史会话</div>
            </div>
            <div class="stat-box highlight">
              <div class="stat-num">{{ orders.length }}</div>
              <div class="stat-label">订单总数</div>
            </div>
            <div class="stat-box">
              <div class="stat-num">¥0</div>
              <div class="stat-label">账户余额</div>
            </div>
          </div>

          <!-- 订单列表 -->
          <div class="orders-panel">
            <div class="orders-head">
              <h2>我的订单</h2>
              <div class="tabs">
                <a
                  v-for="t in STATUS_TABS"
                  :key="t.key"
                  :class="{ active: activeStatus === t.key }"
                  @click="activeStatus = t.key"
                >
                  {{ t.label }}
                  <span v-if="t.count() > 0" class="tab-count">{{ t.count() }}</span>
                </a>
              </div>
            </div>

            <div v-if="filteredOrders.length === 0" class="empty-state">
              <p>暂无相关订单</p>
              <button class="link-btn" @click="router.push('/shop')">去逛逛商品 →</button>
            </div>
            <div v-else class="orders-list">
              <OrderCard
                v-for="o in filteredOrders"
                :key="o.order_no"
                :order="o"
                density="list"
                @changed="onOrderChanged"
              />
            </div>
          </div>
        </section>
      </div>
    </template>
  </main>
</template>

<style scoped>
.profile {
  flex: 1;
  overflow-y: auto;
  background: var(--gray-50);
}
.profile-body {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-4) var(--sp-6);
  display: flex;
  gap: var(--sp-4);
}

/* Sidebar */
.sidebar {
  width: 220px;
  flex-shrink: 0;
}
.user-card {
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-4);
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.avatar {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--jd-red);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 600;
  flex-shrink: 0;
}
.user-info {
  flex: 1;
  min-width: 0;
}
.username {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--gray-800);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.admin-tag {
  padding: 1px 6px;
  background: var(--jd-red);
  color: #fff;
  font-size: var(--fs-xs);
  font-weight: 500;
}
.member {
  font-size: var(--fs-xs);
  color: var(--gray-500);
  margin-top: 4px;
}

.menu {
  background: var(--gray-0);
  border: var(--border);
}
.menu-section {
  border-bottom: var(--border);
}
.menu-section:last-child {
  border-bottom: none;
}
.menu-title {
  padding: var(--sp-2) var(--sp-4);
  background: var(--gray-50);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--gray-700);
  border-bottom: var(--border);
}
.menu-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-base);
  color: var(--gray-700);
  cursor: pointer;
  border-bottom: 1px dashed var(--gray-200);
}
.menu-item:last-child {
  border-bottom: none;
}
.menu-item:hover {
  background: var(--jd-red-light);
  color: var(--jd-red);
}
.menu-item.active {
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-weight: 500;
  border-left: 3px solid var(--jd-red);
  padding-left: calc(var(--sp-4) - 3px);
}
.menu-item.disabled {
  color: var(--gray-400);
  cursor: not-allowed;
}
.menu-item.disabled:hover {
  background: transparent;
  color: var(--gray-400);
}
.count {
  font-size: var(--fs-xs);
  background: var(--jd-red);
  color: #fff;
  padding: 1px 6px;
  min-width: 20px;
  text-align: center;
}
.badge {
  font-size: var(--fs-xs);
  color: var(--gray-400);
}
.logout-btn {
  width: 100%;
  padding: var(--sp-3);
  background: var(--gray-0);
  border: none;
  font-size: var(--fs-base);
  color: var(--jd-red);
  cursor: pointer;
}
.logout-btn:hover {
  background: var(--jd-red-light);
}

/* Content */
.content {
  flex: 1;
  min-width: 0;
}

/* Stats */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.stat-box {
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-4);
  text-align: center;
}
.stat-box.highlight {
  border-color: var(--jd-red);
}
.stat-num {
  font-size: var(--fs-2xl);
  font-weight: 700;
  color: var(--gray-800);
  line-height: 1.2;
}
.stat-box.highlight .stat-num {
  color: var(--jd-red);
}
.stat-label {
  margin-top: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--gray-500);
}

/* Orders */
.orders-panel {
  background: var(--gray-0);
  border: var(--border);
}
.orders-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: var(--border);
}
.orders-head h2 {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--gray-800);
}
.tabs {
  display: flex;
  gap: var(--sp-4);
}
.tabs a {
  font-size: var(--fs-sm);
  color: var(--gray-600);
  cursor: pointer;
  padding: var(--sp-1) 0;
  border-bottom: 2px solid transparent;
  display: flex;
  align-items: center;
  gap: 4px;
}
.tabs a:hover {
  color: var(--jd-red);
}
.tabs a.active {
  color: var(--jd-red);
  border-bottom-color: var(--jd-red);
}
.tab-count {
  font-size: var(--fs-xs);
  background: var(--gray-200);
  color: var(--gray-600);
  padding: 0 6px;
  min-width: 18px;
  text-align: center;
}
.tabs a.active .tab-count {
  background: var(--jd-red);
  color: #fff;
}

.orders-list {
  display: flex;
  flex-direction: column;
}
.empty-state {
  padding: 60px 20px;
  text-align: center;
  color: var(--gray-500);
}
.empty-state p {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-base);
}
.link-btn {
  padding: var(--sp-2) var(--sp-4);
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-sm);
  cursor: pointer;
}

/* States */
.loading-state, .error-state {
  text-align: center;
  padding: 80px 20px;
  color: var(--gray-500);
}
.error-state {
  color: var(--jd-red);
}
.spinner {
  width: 28px;
  height: 28px;
  border: 2px solid var(--gray-200);
  border-top-color: var(--jd-red);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto var(--sp-3);
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 768px) {
  .profile-body {
    flex-direction: column;
  }
  .sidebar {
    width: 100%;
  }
  .stats {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>