<script setup lang="ts">
/**
 * 商品橱窗（京东风）
 * 左侧 200px 类目筛选 + 右侧商品网格 4 列
 * 京东风：1px 边框 + 京东红价格 + 无圆角 + 1px 分隔
 */
import { ref, onMounted, computed, watch } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { listProducts } from '../api';
import type { Product } from '../types';
import ProductCard from '../components/ProductCard.vue';

const router = useRouter();
const route = useRoute();
const products = ref<Product[]>([]);
const loading = ref(true);
const error = ref('');
const activeCategory = ref<string | null>(null);

// 类目（从 product.name 模糊匹配，简单够用）
const categories = [
  { key: null, label: '全部商品' },
  { key: '手机', label: '手机' },
  { key: '耳机', label: '耳机' },
  { key: '手表', label: '智能手表' },
  { key: '平板', label: '平板电脑' },
  { key: '笔记本', label: '笔记本电脑' },
  { key: '键盘', label: '键盘' },
  { key: '鼠标', label: '鼠标' },
];

const searchText = computed(() => (route.query.q as string) || '');

const filtered = computed(() => {
  let list = products.value;
  if (activeCategory.value) {
    list = list.filter((p) => p.name.includes(activeCategory.value!));
  }
  if (searchText.value) {
    const q = searchText.value.toLowerCase();
    list = list.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.sku.toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q),
    );
  }
  return list;
});

watch(
  () => route.query.q,
  (q) => {
    if (typeof q === 'string' && q && !loading.value) {
      // 触发重新筛选（computed 自动）
    }
  },
);

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

function openProduct(sku: string) {
  router.push({ name: 'product-detail', params: { sku } });
}
</script>

<template>
  <main class="shop">
    <!-- 顶部 banner（小幅） -->
    <div class="shop-banner">
      <div class="banner-inner">
        <div class="banner-title">商品精选</div>
        <div class="banner-sub">{{ products.length }} 款在售 · 智选科技官方直营</div>
      </div>
    </div>

    <div class="shop-body">
      <!-- 左侧类目 -->
      <aside class="sidebar">
        <div class="sidebar-section">
          <div class="sidebar-title">商品分类</div>
          <ul class="cat-list">
            <li
              v-for="cat in categories"
              :key="cat.key ?? 'all'"
              :class="{ active: activeCategory === cat.key }"
              @click="activeCategory = cat.key"
            >
              {{ cat.label }}
            </li>
          </ul>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-title">客服承诺</div>
          <ul class="promise">
            <li>✓ 24h 在线</li>
            <li>✓ 智能识别意图</li>
            <li>✓ 一键查看订单</li>
            <li>✓ 自动生成标题</li>
          </ul>
        </div>
      </aside>

      <!-- 右侧商品 -->
      <section class="content">
        <div class="content-head">
          <div class="result-info">
            <span v-if="searchText">搜索 "<b>{{ searchText }}</b>"</span>
            <span v-else-if="activeCategory">{{ activeCategory }} 类目</span>
            <span v-else>全部商品</span>
            <span class="count">共 {{ filtered.length }} 件</span>
          </div>
        </div>

        <div v-if="loading" class="loading-state">
          <div class="spinner"></div>
          <p>商品加载中…</p>
        </div>
        <div v-else-if="error" class="error-state">{{ error }}</div>
        <div v-else-if="filtered.length === 0" class="empty-state">
          <p>该类目暂无商品</p>
          <button class="reset-btn" @click="activeCategory = null">查看全部</button>
        </div>
        <div v-else class="grid">
          <div
            v-for="p in filtered"
            :key="p.sku"
            class="grid-item"
            @click="openProduct(p.sku)"
          >
            <ProductCard :product="p" density="shop" @ask="onAsk" />
          </div>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.shop {
  flex: 1;
  overflow-y: auto;
  background: var(--gray-50);
  display: flex;
  flex-direction: column;
}

/* Banner */
.shop-banner {
  background: var(--gray-0);
  border-bottom: var(--border);
}
.banner-inner {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-6) var(--sp-5);
}
.banner-title {
  font-size: var(--fs-xl);
  font-weight: 700;
  color: var(--gray-800);
}
.banner-sub {
  font-size: var(--fs-sm);
  color: var(--gray-500);
  margin-top: 4px;
}

/* Body */
.shop-body {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-4) var(--sp-6);
  display: flex;
  gap: var(--sp-4);
  width: 100%;
  flex: 1;
}

/* Sidebar */
.sidebar {
  width: var(--sidebar-w);
  flex-shrink: 0;
}
.sidebar-section {
  background: var(--gray-0);
  border: var(--border);
  margin-bottom: var(--sp-3);
}
.sidebar-title {
  padding: var(--sp-3) var(--sp-4);
  background: var(--gray-50);
  border-bottom: var(--border);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--gray-800);
}
.cat-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.cat-list li {
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-base);
  color: var(--gray-700);
  cursor: pointer;
  border-bottom: 1px dashed var(--gray-200);
  transition: all 0.15s;
}
.cat-list li:last-child {
  border-bottom: none;
}
.cat-list li:hover {
  background: var(--gray-50);
  color: var(--jd-red);
}
.cat-list li.active {
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-weight: 500;
  border-left: 3px solid var(--jd-red);
  padding-left: calc(var(--sp-4) - 3px);
}
.promise {
  list-style: none;
  margin: 0;
  padding: var(--sp-3) var(--sp-4);
}
.promise li {
  padding: var(--sp-1) 0;
  font-size: var(--fs-sm);
  color: var(--gray-600);
}

/* Content */
.content {
  flex: 1;
  min-width: 0;
}
.content-head {
  background: var(--gray-0);
  border: var(--border);
  border-bottom: none;
  padding: var(--sp-3) var(--sp-4);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.result-info {
  font-size: var(--fs-base);
  color: var(--gray-700);
}
.result-info b {
  color: var(--jd-red);
}
.count {
  margin-left: var(--sp-3);
  color: var(--gray-500);
  font-size: var(--fs-sm);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 0;
  background: var(--gray-200);
  border: var(--border);
  border-top: none;
}
.grid-item {
  background: var(--gray-0);
  margin: -0.5px; /* 抵消边框重叠 */
  position: relative;
  cursor: pointer;
}

/* States */
.loading-state, .error-state, .empty-state {
  background: var(--gray-0);
  border: var(--border);
  border-top: none;
  text-align: center;
  padding: 60px 20px;
  color: var(--gray-500);
  font-size: var(--fs-base);
}
.error-state {
  color: var(--jd-red);
}
.empty-state p {
  margin: 0 0 var(--sp-3);
}
.reset-btn {
  padding: 6px 16px;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-sm);
  cursor: pointer;
}

.loading-state .spinner {
  width: 24px;
  height: 24px;
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
  .shop-body {
    flex-direction: column;
  }
  .sidebar {
    width: 100%;
  }
}
</style>