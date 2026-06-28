<script setup lang="ts">
/**
 * 消息内嵌卡片（京东风）
 * 在 assistant 消息下方，根据 msg.intent + msg.entities 自动渲染：
 * - product_query + sku → ProductCard mini
 * - order_query / refund_query + order_no → OrderCard mini
 * - tool_result_preview 存在 → 折叠面板
 *
 * 设计原则：只在消息气泡下方追加，不替换正文（用户先看 LLM 答得对不对）
 */
import { ref, watch } from 'vue';
import type { Message, Product, OrderSummary } from '../types';
import { getProduct, getOrderDetail } from '../api';
import ProductCard from './ProductCard.vue';
import OrderCard from './OrderCard.vue';

const props = defineProps<{
  message: Message;
}>();

// ===== 商品卡 =====
const product = ref<Product | null>(null);
const productLoading = ref(false);
const productError = ref<string | null>(null);

async function loadProduct() {
  if (!props.message.entities?.sku) return;
  productLoading.value = true;
  productError.value = null;
  try {
    product.value = await getProduct(props.message.entities.sku);
  } catch (e) {
    productError.value = e instanceof Error ? e.message : '加载商品失败';
  } finally {
    productLoading.value = false;
  }
}

// ===== 订单卡 =====
const order = ref<OrderSummary | null>(null);
const orderLoading = ref(false);
const orderError = ref<string | null>(null);

async function loadOrder() {
  if (!props.message.entities?.order_no) return;
  orderLoading.value = true;
  orderError.value = null;
  try {
    const detail = await getOrderDetail(props.message.entities.order_no);
    order.value = detail.order;
  } catch (e) {
    orderError.value = e instanceof Error ? e.message : '加载订单失败';
  } finally {
    orderLoading.value = false;
  }
}

// 触发加载
watch(
  () => [props.message.entities?.sku, props.message.entities?.order_no],
  () => {
    loadProduct();
    loadOrder();
  },
  { immediate: true },
);

// intent 中文映射
const intentLabel = (intent: string): string => {
  const map: Record<string, string> = {
    product_query: '商品咨询',
    order_query: '订单查询',
    refund_query: '退款咨询',
    policy_query: '政策问答',
    greeting: '问候',
    chitchat: '闲聊',
  };
  return map[intent] ?? intent;
};

// 应不应该渲染卡片
const shouldRenderCard = () => {
  return !!(props.message.intent && props.message.entities);
};
</script>

<template>
  <div v-if="message.role === 'assistant' && shouldRenderCard()" class="message-card">

    <!-- product_query + sku → 商品卡 -->
    <ProductCard
      v-if="message.entities?.sku && message.intent === 'product_query'"
      :product="product"
      :loading="productLoading"
      :error="productError"
      density="mini"
      @retry="loadProduct"
    />

    <!-- order_query / refund_query + order_no → 订单卡 -->
    <OrderCard
      v-else-if="message.entities?.order_no && (message.intent === 'order_query' || message.intent === 'refund_query')"
      :order="order ?? { order_no: message.entities.order_no, status: 'unknown', total_amount: 0, create_time: null, item_count: 0 }"
      density="mini"
    />

    <!-- tool_result_preview 折叠面板 -->
    <details v-if="message.tool_result_preview" class="preview-details">
      <summary>查看工具调用预览</summary>
      <pre>{{ message.tool_result_preview }}</pre>
    </details>

    <!-- intent 徽章（京东风：扁平红章） -->
    <div class="intent-badge">
      <span class="badge-value">{{ intentLabel(message.intent ?? '') }}</span>
      <span v-if="message.entities?.sku" class="badge-entity">SKU {{ message.entities.sku }}</span>
      <span v-if="message.entities?.order_no" class="badge-entity">{{ message.entities.order_no }}</span>
    </div>
  </div>
</template>

<style scoped>
.message-card {
  margin-top: var(--sp-2);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.preview-details {
  font-size: var(--fs-xs);
  color: var(--gray-600);
  background: var(--gray-50);
  padding: var(--sp-2) var(--sp-3);
  border: var(--border);
}
.preview-details summary {
  cursor: pointer;
  user-select: none;
  font-weight: 500;
  color: var(--gray-700);
}
.preview-details summary:hover {
  color: var(--jd-red);
}
.preview-details pre {
  margin: var(--sp-2) 0 0;
  padding: var(--sp-2);
  background: var(--gray-0);
  border: var(--border);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow-y: auto;
}

.intent-badge {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-xs);
  padding: 2px 0;
}
.badge-value {
  padding: 1px 8px;
  background: var(--jd-red);
  color: #fff;
  font-weight: 500;
}
.badge-entity {
  padding: 1px 8px;
  background: var(--gray-50);
  color: var(--gray-700);
  border: 1px solid var(--gray-300);
  font-family: var(--font-mono);
}
</style>