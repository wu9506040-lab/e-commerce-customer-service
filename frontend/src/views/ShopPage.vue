<script setup lang="ts">
/**
 * 商品橱窗（M9 新增）
 * 公开页（无需登录），但「问客服」按钮需登录（路由守卫会自动跳 /login）
 */
import { ref, onMounted, computed } from 'vue';
import { useRouter } from 'vue-router';
import { listProducts } from '../api';
import type { Product } from '../types';
import ProductCard from '../components/ProductCard.vue';

const router = useRouter();
const products = ref<Product[]>([]);
const loading = ref(true);
const error = ref('');
const activeCategory = ref<string | null>(null);

// 类目（从 product.name 模糊匹配，简单够用）
const categories = [
  { key: null, label: '全部', emoji: '🛒' },
  { key: '手机', label: '手机', emoji: '📱' },
  { key: '耳机', label: '耳机', emoji: '🎧' },
  { key: '手表', label: '手表', emoji: '⌚' },
  { key: '平板', label: '平板', emoji: '📲' },
  { key: '笔记本', label: '笔记本', emoji: '💻' },
  { key: '键盘', label: '键盘', emoji: '⌨️' },
  { key: '鼠标', label: '鼠标', emoji: '🖱️' },
];

const filtered = computed(() => {
  if (!activeCategory.value) return products.value;
  const cat = activeCategory.value;
  return products.value.filter((p) => p.name.includes(cat));
});

onMounted(async () => {
  try {
    const data = await listProducts({ limit: 50 });
    products.value = data.products;
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载商品失败';
  } finally {
    loading.value = false;
  }
});

function onAsk(sku: string) {
  router.push({
    name: 'chat',
    query: { q: `${sku} 怎么样` },
  });
}
</script>

<template>
  <main class="shop">
    <header class="shop-header">
      <div class="shop-header-inner">
        <h1>商品橱窗</h1>
        <p>浏览全部在售商品 · 点击「问客服」直接对话</p>
      </div>
    </header>

    <!-- 类目筛选 -->
    <div class="category-bar">
      <button
        v-for="cat in categories"
        :key="cat.key ?? 'all'"
        :class="['cat-chip', { active: activeCategory === cat.key }]"
        @click="activeCategory = cat.key"
      >
        <span class="cat-emoji">{{ cat.emoji }}</span>
        {{ cat.label }}
      </button>
    </div>

    <!-- 商品网格 -->
    <section class="grid-wrap">
      <div v-if="loading" class="loading-state">
        <div class="spinner"></div>
        <p>商品加载中…</p>
      </div>
      <div v-else-if="error" class="error-state">⚠️ {{ error }}</div>
      <div v-else-if="filtered.length === 0" class="empty-state">
        <p>该类目暂无商品</p>
      </div>
      <div v-else class="grid">
        <ProductCard
          v-for="p in filtered"
          :key="p.sku"
          :product="p"
          density="shop"
          @ask="onAsk"
        />
      </div>
    </section>
  </main>
</template>

<style scoped>
.shop {
  flex: 1;
  overflow-y: auto;
}
.shop-header {
  background: linear-gradient(135deg, #f9fafb 0%, #f3f4f6 100%);
  padding: 40px 24px 24px;
  text-align: center;
  border-bottom: 1px solid #e5e7eb;
}
.shop-header-inner {
  max-width: 760px;
  margin: 0 auto;
}
.shop-header h1 {
  margin: 0 0 6px;
  font-size: 32px;
  font-weight: 700;
  color: #1f2937;
}
.shop-header p {
  margin: 0;
  color: #6b7280;
}

.category-bar {
  max-width: 1100px;
  margin: 0 auto;
  padding: 20px 24px;
  display: flex;
  gap: 8px;
  overflow-x: auto;
  flex-wrap: wrap;
}
.cat-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 18px;
  font-size: 13px;
  color: #4b5563;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.cat-chip:hover {
  border-color: #667eea;
  color: #4f46e5;
}
.cat-chip.active {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-color: transparent;
}
.cat-emoji {
  font-size: 14px;
}

.grid-wrap {
  max-width: 1100px;
  margin: 0 auto;
  padding: 0 24px 60px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 18px;
}
.loading-state, .error-state, .empty-state {
  text-align: center;
  padding: 60px 20px;
  color: #9ca3af;
  font-size: 14px;
}
.error-state {
  color: #b91c1c;
}
.loading-state .spinner {
  width: 24px;
  height: 24px;
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
