<script setup lang="ts">
/**
 * 商品卡片（京东风）
 * 三种密度：shop（橱窗）/ detail（详情页大图）/ mini（消息内嵌）
 * 京东风：1px 边框 + 京东红价格 + 无圆角阴影 + 文字主导
 */
import { computed } from 'vue';
import type { Product } from '../types';

const props = withDefaults(
  defineProps<{
    product: Product | null;
    density?: 'shop' | 'detail' | 'mini';
    loading?: boolean;
    error?: string | null;
  }>(),
  { density: 'shop', loading: false, error: null },
);

const emit = defineEmits<{
  ask: [sku: string];
  retry: [];
}>();

const colorText = computed(() => {
  const c = props.product?.attributes?.color;
  if (Array.isArray(c)) return c.join(' / ');
  if (typeof c === 'string') return c;
  return null;
});

const stockStatus = computed(() => {
  if (!props.product) return '';
  if (props.product.stock === 0) return '无货';
  if (props.product.stock < 5) return `仅剩 ${props.product.stock} 件`;
  return `库存 ${props.product.stock}`;
});

const priceText = computed(() => {
  if (!props.product) return '';
  return `¥${props.product.price.toLocaleString('zh-CN')}`;
});

function onAsk() {
  if (props.product) emit('ask', props.product.sku);
}
</script>

<template>
  <div :class="['product-card', `density-${density}`]">
    <!-- 加载态 -->
    <div v-if="loading" class="state-box">
      <div class="spinner"></div>
      <span>加载中…</span>
    </div>

    <!-- 错误态 -->
    <div v-else-if="error" class="state-box error">
      <span>{{ error }}</span>
      <button @click="emit('retry')">重试</button>
    </div>

    <!-- 正常态 -->
    <template v-else-if="product">
      <!-- shop 密度：直接 img（点击进详情），detail：左侧大图 -->
      <div v-if="density !== 'mini'" class="cover">
        <img :src="product.cover_url" :alt="product.name" loading="lazy" />
      </div>

      <div class="info">
        <h4 v-if="density !== 'detail'" class="name">{{ product.name }}</h4>

        <!-- shop / detail：完整信息 -->
        <template v-if="density !== 'mini'">
          <div v-if="colorText" class="attr-row">颜色：{{ colorText }}</div>

          <div class="price-row">
            <span class="price">
              <span class="price-symbol">¥</span>
              <span class="price-num">{{ product.price.toLocaleString('zh-CN') }}</span>
            </span>
          </div>

          <div v-if="density === 'detail'" class="stock-detail">
            <span class="stock-tag" :class="{ low: product.stock < 5 && product.stock > 0, out: product.stock === 0 }">
              {{ stockStatus }}
            </span>
          </div>

          <button
            class="ask-btn"
            @click="onAsk"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
              <path d="M21 12a9 9 0 11-9-9 9 9 0 019 9z" stroke="currentColor" stroke-width="2"/>
              <path d="M9 10h.01M15 10h.01M9 14c1 1 2 1.5 3 1.5s2-.5 3-1.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
            咨询客服
          </button>
        </template>

        <!-- mini 密度：横向紧凑 -->
        <template v-else>
          <div class="mini-name">{{ product.name }}</div>
          <div class="mini-price">{{ priceText }}</div>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.product-card {
  background: var(--gray-0);
  border: var(--border);
  display: flex;
  flex-direction: column;
  transition: border-color 0.15s;
}
.product-card:hover {
  border-color: var(--jd-red);
}

/* ============= Density ============= */
.density-shop {
  width: 100%;
}
.density-shop .cover {
  aspect-ratio: 1;
  background: var(--gray-50);
  border-bottom: var(--border);
}
.density-shop .info {
  padding: var(--sp-3);
}
.density-shop .name {
  font-size: var(--fs-base);
  font-weight: 400;
  color: var(--gray-800);
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  height: 40px;
  margin: 0 0 var(--sp-2);
}

.density-detail {
  flex-direction: row;
  border: none;
  gap: var(--sp-6);
}
.density-detail .cover {
  flex: 0 0 420px;
  max-width: 420px;
  background: var(--gray-50);
  border: var(--border);
}
.density-detail .info {
  flex: 1;
  padding: 0;
  justify-content: flex-start;
}

.density-mini {
  flex-direction: row;
  max-width: 360px;
  border: var(--border);
}
.density-mini .info {
  flex: 1;
  padding: var(--sp-3);
  min-width: 0;
  justify-content: center;
}
.mini-name {
  font-size: var(--fs-sm);
  color: var(--gray-800);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-bottom: 4px;
}
.mini-price {
  font-size: var(--fs-md);
  font-weight: 700;
  color: var(--jd-red);
}

/* ============= Sub-elements ============= */
.cover {
  width: 100%;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.info {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.attr-row {
  font-size: var(--fs-sm);
  color: var(--gray-600);
}

.price-row {
  margin-top: var(--sp-1);
}
.price {
  color: var(--jd-red);
  font-weight: 700;
  display: inline-flex;
  align-items: baseline;
}
.price-symbol {
  font-size: var(--fs-base);
  margin-right: 1px;
}
.density-detail .price-symbol {
  font-size: var(--fs-lg);
}
.density-shop .price-num {
  font-size: var(--fs-lg);
}
.density-detail .price-num {
  font-size: var(--fs-3xl);
}

.stock-detail {
  margin-top: var(--sp-2);
}
.stock-tag {
  display: inline-block;
  padding: 2px 8px;
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-size: var(--fs-xs);
}
.stock-tag.low {
  background: #fff3e0;
  color: #ff8800;
}
.stock-tag.out {
  background: var(--gray-100);
  color: var(--gray-500);
}

.ask-btn {
  margin-top: var(--sp-3);
  padding: 10px 16px;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-base);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  align-self: flex-start;
}
.ask-btn:hover {
  background: var(--jd-red-hover);
}

/* ============= State box ============= */
.state-box {
  padding: var(--sp-6);
  text-align: center;
  color: var(--gray-500);
  font-size: var(--fs-sm);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-2);
  min-height: 100px;
  justify-content: center;
}
.state-box.error button {
  padding: 4px 12px;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-xs);
  cursor: pointer;
}
.spinner {
  width: 18px;
  height: 18px;
  border: 2px solid var(--gray-200);
  border-top-color: var(--jd-red);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>