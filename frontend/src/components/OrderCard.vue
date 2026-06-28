<script setup lang="ts">
/**
 * 订单卡片（M9 新增）
 * 复用：在 ProfilePage / MessageCard / DemoLanding
 * density: list（默认，列表用）/ mini（消息内）
 */
import { ref, watch } from 'vue';
import type { OrderSummary, OrderDetail } from '../types';
import { getOrderDetail } from '../api';

const props = withDefaults(
  defineProps<{
    order: OrderSummary;
    density?: 'list' | 'mini';
  }>(),
  { density: 'list' },
);

// mini 模式下自动展开 detail
const detail = ref<OrderDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref<string | null>(null);

async function loadDetail() {
  if (props.density !== 'mini') return;
  detailLoading.value = true;
  detailError.value = null;
  try {
    detail.value = await getOrderDetail(props.order.order_no);
  } catch (e) {
    detailError.value = e instanceof Error ? e.message : '加载失败';
  } finally {
    detailLoading.value = false;
  }
}

watch(
  () => props.density,
  (d) => {
    if (d === 'mini') loadDetail();
  },
  { immediate: true },
);

// 状态徽章颜色
const statusMeta = (status: string) => {
  const map: Record<string, { label: string; bg: string; fg: string }> = {
    pending:   { label: '待支付', bg: '#fef3c7', fg: '#92400e' },
    paid:      { label: '已支付', bg: '#dbeafe', fg: '#1e40af' },
    shipped:   { label: '运输中', bg: '#e0e7ff', fg: '#3730a3' },
    delivered: { label: '已签收', bg: '#d1fae5', fg: '#065f46' },
    completed: { label: '已完成', bg: '#d1fae5', fg: '#065f46' },
    refunded:  { label: '已退款', bg: '#fee2e2', fg: '#991b1b' },
  };
  return map[status] ?? { label: status, bg: '#f3f4f6', fg: '#4b5563' };
};

const totalText = (o: OrderSummary) =>
  `¥${o.total_amount.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`;

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('zh-CN');
}
</script>

<template>
  <div :class="['order-card', `density-${density}`]">
    <header class="order-header">
      <div class="order-no">📦 {{ order.order_no }}</div>
      <span
        class="status-badge"
        :style="{
          background: statusMeta(order.status).bg,
          color: statusMeta(order.status).fg,
        }"
      >
        {{ statusMeta(order.status).label }}
      </span>
    </header>

    <!-- list 模式：简略 -->
    <template v-if="density === 'list'">
      <div class="order-row">
        <span>共 {{ order.item_count }} 件商品</span>
        <span class="amount">{{ totalText(order) }}</span>
      </div>
      <div class="order-row time">
        <span>{{ formatTime(order.create_time) }}</span>
      </div>
    </template>

    <!-- mini 模式：详细 -->
    <template v-else>
      <div v-if="detailLoading" class="detail-loading">
        <div class="spinner"></div> 加载订单详情…
      </div>
      <div v-else-if="detailError" class="detail-error">⚠️ {{ detailError }}</div>
      <template v-else-if="detail">
        <ul class="items">
          <li v-for="item in detail.items" :key="item.sku" class="item">
            <div class="item-name">{{ item.product_name }}</div>
            <div class="item-qty">x{{ item.qty }}</div>
            <div class="item-price">¥{{ item.subtotal.toLocaleString('zh-CN') }}</div>
          </li>
        </ul>
        <div class="total-row">
          <span>合计</span>
          <strong>{{ totalText(order) }}</strong>
        </div>
        <div v-if="detail.logistics" class="logistics">
          <div class="logi-row">
            <span class="logi-label">📮 物流</span>
            <span class="logi-no">{{ detail.logistics.logistics_no || '—' }}</span>
          </div>
          <div class="logi-status">{{ detail.logistics.status }} · {{ detail.logistics.last_location }}</div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.order-card {
  background: white;
  border-radius: 10px;
  padding: 16px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  border: 1px solid #e5e7eb;
}
.order-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.order-no {
  font-size: 14px;
  font-weight: 600;
  color: #1f2937;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.status-badge {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
}
.order-row {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: #4b5563;
  padding: 4px 0;
}
.order-row.time {
  color: #9ca3af;
  font-size: 12px;
}
.amount {
  font-size: 16px;
  font-weight: 700;
  color: #dc2626;
}

/* mini 模式样式 */
.density-mini {
  max-width: 420px;
}
.items {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 1px solid #f3f4f6;
}
.item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid #f3f4f6;
  font-size: 13px;
}
.item-name {
  color: #1f2937;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.item-qty {
  color: #6b7280;
}
.item-price {
  color: #4b5563;
  font-weight: 500;
}
.total-row {
  display: flex;
  justify-content: space-between;
  padding: 12px 0 4px;
  font-size: 13px;
  color: #6b7280;
}
.total-row strong {
  font-size: 18px;
  color: #dc2626;
}
.logistics {
  margin-top: 12px;
  padding: 10px 12px;
  background: #f9fafb;
  border-radius: 6px;
  font-size: 12px;
  color: #4b5563;
}
.logi-row {
  display: flex;
  justify-content: space-between;
}
.logi-label {
  font-weight: 500;
}
.logi-no {
  font-family: ui-monospace, monospace;
}
.logi-status {
  margin-top: 4px;
}
.detail-loading {
  text-align: center;
  color: #9ca3af;
  padding: 12px;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.detail-error {
  text-align: center;
  color: #b91c1c;
  padding: 8px;
  font-size: 13px;
}
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid #e5e7eb;
  border-top-color: #667eea;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
