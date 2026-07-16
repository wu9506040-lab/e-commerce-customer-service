<script setup lang="ts">
/**
 * 订单卡片（京东风）
 * 复用：在 ProfilePage / MessageCard / ShopPage
 * density: list（默认，列表用）/ mini（消息内）
 *
 * M10 闭环：list 模式下根据 status 显示对应流转按钮
 * - pending   → [立即付款]
 * - paid      → [模拟发货]（demo 用，正常流程由商家触发）
 * - shipped   → [确认签收]
 * - delivered → [申请退款]（7 天无理由）
 * - refunded  → 无按钮
 *
 * 每次点击按钮 → 调后端状态机 API → 成功后 emit('changed') 让父组件 reload
 */
import { ref, computed, watch } from 'vue';
import { useRouter } from 'vue-router';
import type { OrderSummary, OrderDetail } from '../types';
import {
  confirmOrder,
  getOrderDetail,
  payOrder,
  refundOrder,
  shipOrder,
} from '../api';
import RefundReasonDialog from './RefundReasonDialog.vue';

const props = withDefaults(
  defineProps<{
    order: OrderSummary;
    density?: 'list' | 'mini';
  }>(),
  { density: 'list' },
);

// emit：状态变化后通知父组件 reload
const emit = defineEmits<{
  (e: 'changed', orderNo: string): void;
}>();

const router = useRouter();

// M9.5：跳到 chat 时携带 order_no → 后端注入【当前订单】到 prompt
function askAboutOrder() {
  router.push({
    name: 'chat',
    query: {
      q: `${props.order.order_no} 现在什么状态`,
      order_no: props.order.order_no,
    },
  });
}

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

// 状态徽章（京东风：扁平 + 红/灰）
const statusMeta = (status: string) => {
  const map: Record<string, { label: string; cls: string }> = {
    pending:   { label: '待支付', cls: 'st-pending' },
    paid:      { label: '已支付', cls: 'st-paid' },
    shipped:   { label: '运输中', cls: 'st-shipped' },
    delivered: { label: '已签收', cls: 'st-delivered' },
    completed: { label: '已完成', cls: 'st-delivered' },
    refunded:  { label: '已退款', cls: 'st-refunded' },
  };
  return map[status] ?? { label: status, cls: 'st-default' };
};

const totalText = (o: OrderSummary) =>
  `¥${o.total_amount.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`;

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('zh-CN');
}

// =============================================================
// M10 闭环：状态流转按钮
// =============================================================
type ActionKind = 'pay' | 'ship' | 'confirm' | 'refund';

interface ActionDef {
  key: ActionKind;
  label: string;
  cls: string;
  // 能否执行（按当前 status 判断）
  available: boolean;
}

const actionDef = computed<ActionDef | null>(() => {
  switch (props.order.status) {
    case 'pending':
      return { key: 'pay', label: '立即付款', cls: 'btn-primary', available: true };
    case 'paid':
      // demo 用：让用户自己点发货（真实流程由商家操作）
      return { key: 'ship', label: '模拟发货', cls: 'btn-primary', available: true };
    case 'shipped':
      return { key: 'confirm', label: '确认签收', cls: 'btn-primary', available: true };
    case 'delivered':
      return { key: 'refund', label: '申请退款', cls: 'btn-danger', available: true };
    case 'completed':
      // completed 状态：业务上已完成；如果还想演示退款流程也允许（demo）
      return { key: 'refund', label: '申请退款', cls: 'btn-danger', available: true };
    default:
      return null;
  }
});

const acting = ref(false);
const actionError = ref<string | null>(null);

// P0-C：退款原因对话框（替代 window.prompt）
const refundDialogShow = ref(false);
const pendingRefund = ref<{ orderNo: string } | null>(null);

async function runAction() {
  if (!actionDef.value || acting.value) return;
  const kind = actionDef.value.key;
  const orderNo = props.order.order_no;
  // 退款走 dialog 收集原因，其他动作直接执行
  if (kind === 'refund') {
    pendingRefund.value = { orderNo };
    refundDialogShow.value = true;
    return;
  }
  acting.value = true;
  actionError.value = null;
  try {
    if (kind === 'pay') await payOrder(orderNo);
    else if (kind === 'ship') await shipOrder(orderNo);
    else if (kind === 'confirm') await confirmOrder(orderNo);
    emit('changed', orderNo);
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : '操作失败';
  } finally {
    acting.value = false;
  }
}

async function onRefundConfirm(reason: string) {
  if (!pendingRefund.value) return;
  const { orderNo } = pendingRefund.value;
  refundDialogShow.value = false;
  pendingRefund.value = null;
  acting.value = true;
  actionError.value = null;
  try {
    await refundOrder(orderNo, { reason: reason.trim() || '用户申请退款' });
    emit('changed', orderNo);
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : '退款失败';
  } finally {
    acting.value = false;
  }
}

function onRefundCancel() {
  pendingRefund.value = null;
}
</script>

<template>
  <div :class="['order-card', `density-${density}`]">
    <header class="order-header">
      <div class="order-no">订单号 {{ order.order_no }}</div>
      <span :class="['status-badge', statusMeta(order.status).cls]">
        {{ statusMeta(order.status).label }}
      </span>
    </header>

    <!-- list 模式：简略 -->
    <template v-if="density === 'list'">
      <div v-if="order.item_count > 0" class="order-row">
        <span>共 {{ order.item_count }} 件商品</span>
        <span class="amount">{{ totalText(order) }}</span>
      </div>
      <div v-else class="order-row">
        <span></span>
        <span class="amount">{{ totalText(order) }}</span>
      </div>
      <div class="order-row time">
        <span>下单时间 {{ formatTime(order.create_time) }}</span>
      </div>
      <div v-if="actionError" class="action-error">{{ actionError }}</div>
      <div class="order-actions">
        <button class="ask-btn" @click="askAboutOrder" type="button">
          咨询客服
        </button>
        <button
          v-if="actionDef"
          :class="['action-btn', actionDef.cls]"
          :disabled="acting"
          type="button"
          @click="runAction"
        >
          {{ acting ? '处理中…' : actionDef.label }}
        </button>
      </div>
    </template>

    <!-- mini 模式：详细 -->
    <template v-else>
      <div v-if="detailLoading" class="detail-loading">
        <div class="spinner"></div> 加载订单详情…
      </div>
      <div v-else-if="detailError" class="detail-error">{{ detailError }}</div>
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
            <span class="logi-label">物流</span>
            <span class="logi-no">{{ detail.logistics.logistics_no || '—' }}</span>
          </div>
          <div class="logi-status">{{ detail.logistics.status }} · {{ detail.logistics.last_location }}</div>
        </div>
      </template>
    </template>

    <!-- P0-C：退款原因对话框（替代 window.prompt） -->
    <RefundReasonDialog
      v-model:show="refundDialogShow"
      :order-no="pendingRefund?.orderNo || order.order_no"
      @confirm="onRefundConfirm"
      @cancel="onRefundCancel"
    />
  </div>
</template>

<style scoped>
.order-card {
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-3) var(--sp-4);
}
.order-card-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.order-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--sp-2);
}
.order-no {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--gray-800);
  font-family: var(--font-mono);
}
.status-badge {
  padding: 1px 8px;
  font-size: var(--fs-xs);
  font-weight: 500;
  border: 1px solid;
}
/* 状态色：京东风偏中性，关键态（待支付/退款）用红 */
.st-pending   { color: var(--jd-red);   border-color: var(--jd-red);   background: var(--jd-red-light); }
.st-paid      { color: var(--gray-700); border-color: var(--gray-300); background: var(--gray-50); }
.st-shipped   { color: var(--jd-red);   border-color: var(--jd-red);   background: var(--jd-red-light); }
.st-delivered { color: var(--gray-700); border-color: var(--gray-300); background: var(--gray-50); }
.st-refunded  { color: var(--gray-500); border-color: var(--gray-300); background: var(--gray-100); }
.st-default   { color: var(--gray-500); border-color: var(--gray-300); background: var(--gray-50); }

.order-row {
  display: flex;
  justify-content: space-between;
  font-size: var(--fs-sm);
  color: var(--gray-600);
  padding: 4px 0;
}
.order-row.time {
  color: var(--gray-500);
  font-size: var(--fs-xs);
}
.order-actions {
  margin-top: var(--sp-2);
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--gray-200);
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
}
.ask-btn {
  padding: 6px 14px;
  background: var(--jd-red-light);
  color: var(--jd-red);
  border: 1px solid var(--jd-red);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: all 0.15s;
}
.ask-btn:hover {
  background: var(--jd-red);
  color: #fff;
}
/* M10 状态流转按钮：左浅红咨询，右主操作 */
.action-btn {
  padding: 6px 14px;
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: all 0.15s;
  border: 1px solid;
}
.action-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.action-btn.btn-primary {
  background: var(--jd-red);
  color: #fff;
  border-color: var(--jd-red);
}
.action-btn.btn-primary:hover:not(:disabled) {
  background: var(--jd-red-hover, #c81623);
}
.action-btn.btn-danger {
  background: var(--gray-0);
  color: var(--jd-red);
  border-color: var(--jd-red);
}
.action-btn.btn-danger:hover:not(:disabled) {
  background: var(--jd-red-light);
}
.action-error {
  margin-top: var(--sp-2);
  padding: 6px 10px;
  background: #fff3e0;
  color: #c81623;
  font-size: var(--fs-xs);
  border: 1px solid #c81623;
}
.amount {
  font-size: var(--fs-md);
  font-weight: 700;
  color: var(--jd-red);
}

/* mini 模式样式 */
.density-mini {
  max-width: 420px;
}
.items {
  list-style: none;
  margin: var(--sp-2) 0 0;
  padding: 0;
  border-top: var(--border);
}
.item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
  border-bottom: var(--border);
  font-size: var(--fs-sm);
}
.item-name {
  color: var(--gray-800);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.item-qty {
  color: var(--gray-500);
}
.item-price {
  color: var(--gray-700);
  font-weight: 500;
}
.total-row {
  display: flex;
  justify-content: space-between;
  padding: var(--sp-3) 0 var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--gray-600);
}
.total-row strong {
  font-size: var(--fs-md);
  color: var(--jd-red);
}
.logistics {
  margin-top: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--gray-50);
  font-size: var(--fs-xs);
  color: var(--gray-700);
  border-left: 2px solid var(--jd-red);
}
.logi-row {
  display: flex;
  justify-content: space-between;
}
.logi-label {
  font-weight: 600;
  color: var(--gray-800);
}
.logi-no {
  font-family: var(--font-mono);
  color: var(--gray-700);
}
.logi-status {
  margin-top: 4px;
  color: var(--gray-600);
}
.detail-loading {
  text-align: center;
  color: var(--gray-500);
  padding: var(--sp-3);
  font-size: var(--fs-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
}
.detail-error {
  text-align: center;
  color: var(--jd-red);
  padding: var(--sp-2);
  font-size: var(--fs-sm);
  background: var(--jd-red-light);
  border: 1px solid var(--jd-red);
}
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--gray-200);
  border-top-color: var(--jd-red);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>