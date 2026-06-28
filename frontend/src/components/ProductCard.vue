<script setup lang="ts">
/**
 * 商品卡片（M9 新增）
 * 复用：在 ShopPage / ProductDetail / MessageCard / ProfilePage 都用
 * 三种密度：shop（默认）、detail（大图）、mini（消息内）
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

// 颜色数组 join（attributes.color 可能是 array）
const colorText = computed(() => {
  const c = props.product?.attributes?.color;
  if (Array.isArray(c)) return c.join(' / ');
  return null;
});

// 图片 URL：相对路径 + 拼接 base（Vite dev 直接 serve）
function coverSrc(p: Product): string {
  if (!p.cover_url) return '';
  // cover_url 是相对路径 "/products/SKU001.jpg"，Vite dev / Nginx 都直接 serve
  return p.cover_url;
}

// 价格格式化
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
      <span>加载商品…</span>
    </div>

    <!-- 错误态 -->
    <div v-else-if="error" class="state-box error">
      <span>⚠️ {{ error }}</span>
      <button @click="emit('retry')">重试</button>
    </div>

    <!-- 正常态 -->
    <template v-else-if="product">
      <div class="cover">
        <img
          :src="coverSrc(product)"
          :alt="product.name"
          loading="lazy"
          @error="($event.target as HTMLImageElement).style.opacity = '0.3'"
        />
      </div>
      <div class="info">
        <h4 class="name">{{ product.name }}</h4>
        <div class="price-row">
          <span class="price">{{ priceText }}</span>
          <span class="sku">{{ product.sku }}</span>
        </div>
        <div v-if="colorText" class="meta-row">
          🎨 {{ colorText }}
        </div>
        <div v-if="density !== 'mini'" class="stock-row">
          📦 库存 {{ product.stock }} 件
        </div>
        <button
          v-if="density !== 'mini'"
          class="ask-btn"
          @click="onAsk"
        >
          💬 问客服
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.product-card {
  background: white;
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  transition: transform 0.15s, box-shadow 0.15s;
  display: flex;
  flex-direction: column;
}
.product-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.1);
}

/* ============= Density ============= */
.density-shop .cover {
  aspect-ratio: 1;
  background: #f3f4f6;
}
.density-detail .cover {
  aspect-ratio: 4 / 3;
  max-height: 420px;
  background: #f9fafb;
}
.density-detail .name {
  font-size: 22px;
}
.density-detail .price {
  font-size: 28px;
}
.density-mini {
  flex-direction: row;
  max-width: 360px;
}
.density-mini .cover {
  width: 80px;
  height: 80px;
  flex-shrink: 0;
  background: #f3f4f6;
}
.density-mini .info {
  flex: 1;
  padding: 10px 14px;
  min-width: 0;
}
.density-mini .name {
  font-size: 14px;
  margin: 0 0 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.density-mini .price {
  font-size: 16px;
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
}
.info {
  padding: 14px;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.name {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  color: #1f2937;
  line-height: 1.4;
  /* 2 行截断 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.price-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.price {
  font-size: 18px;
  font-weight: 700;
  color: #dc2626;
}
.sku {
  font-size: 11px;
  color: #9ca3af;
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 4px;
}
.meta-row, .stock-row {
  font-size: 12px;
  color: #6b7280;
}
.ask-btn {
  margin-top: auto;
  padding: 8px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  transition: opacity 0.15s;
}
.ask-btn:hover {
  opacity: 0.9;
}

/* ============= State box ============= */
.state-box {
  padding: 24px;
  text-align: center;
  color: #9ca3af;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  min-height: 100px;
  justify-content: center;
}
.state-box.error button {
  padding: 4px 12px;
  background: #667eea;
  color: white;
  border: none;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}
.spinner {
  width: 18px;
  height: 18px;
  border: 2px solid #e5e7eb;
  border-top-color: #667eea;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
