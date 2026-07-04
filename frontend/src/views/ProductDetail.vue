<script setup lang="ts">
/**
 * 商品详情页（京东风）
 * 左侧大图 + 右侧名称/价格/规格/描述/CTA
 * 京东风格：1px 边框 + 表格化规格 + 红价格 + 红 CTA
 *
 * M10 闭环：
 * - "立即购买" → 调 POST /orders → 跳 /profile 看到 pending 订单
 * - 必须登录（未登录跳 /login?redirect=...）
 */
import { ref, computed, onMounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { createOrder, getProduct, isAuthed, listProducts } from '../api';
import type { Product } from '../types';
import ProductCard from '../components/ProductCard.vue';

const route = useRoute();
const router = useRouter();
const sku = computed(() => route.params.sku as string);
const product = ref<Product | null>(null);
const loading = ref(true);
const error = ref('');

async function load() {
  loading.value = true;
  error.value = '';
  product.value = null;
  try {
    product.value = await getProduct(sku.value);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

// 相关推荐（同类别其它商品）
const related = ref<Product[]>([]);
async function loadRelated() {
  if (!product.value) return;
  try {
    const data = await listProducts({ limit: 50 });
    related.value = data.products
      .filter((p) => p.sku !== product.value!.sku)
      .slice(0, 4);
  } catch {
    /* 静默 */
  }
}

onMounted(async () => {
  await load();
  if (product.value) await loadRelated();
});
watch(sku, async () => {
  await load();
  if (product.value) await loadRelated();
});

const attributeRows = computed(() => {
  if (!product.value?.attributes) return [];
  return Object.entries(product.value.attributes).map(([key, value]) => ({
    key,
    value: Array.isArray(value) ? value.join(' / ') : String(value),
  }));
});

function onAsk() {
  if (!product.value) return;
  // M9.5：把 sku 一起带过去 → chat 后端注入【当前商品】到 prompt
  // 这样 AI 不会再问"您问的是哪款"，直接基于商品信息回答
  router.push({
    name: 'chat',
    query: {
      q: `${product.value.sku} 怎么样`,
      sku: product.value.sku,
    },
  });
}

// M10 闭环：立即购买 → 创建订单 → 跳个人中心看新订单
const buying = ref(false);
const buyError = ref<string | null>(null);

async function buyNow() {
  if (!product.value || buying.value) return;
  buyError.value = null;
  // 未登录跳登录页（保留 redirect 到当前商品详情）
  if (!isLoggedIn.value) {
    router.push({
      name: 'login',
      query: { redirect: `/shop/${product.value.sku}` },
    });
    return;
  }
  buying.value = true;
  try {
    const r = await createOrder({ sku: product.value.sku, qty: 1 });
    // 跳个人中心，新订单会出现在列表
    router.push({
      name: 'profile',
      query: { highlight: r.order_no },
    });
  } catch (e) {
    buyError.value = e instanceof Error ? e.message : '下单失败';
  } finally {
    buying.value = false;
  }
}

// 复用 api.ts 里的 isAuthed ref 判断登录态
const isLoggedIn = computed(() => isAuthed.value === true);
function openProduct(s: string) {
  router.push({ name: 'product-detail', params: { sku: s } });
}
</script>

<template>
  <main class="detail-page">
    <div class="detail-inner">
      <!-- 面包屑 -->
      <nav class="breadcrumb">
        <a @click="router.push('/shop')">商品</a>
        <span class="sep">/</span>
        <span>{{ product?.name || '加载中…' }}</span>
      </nav>

      <div v-if="loading" class="loading-state">
        <div class="spinner"></div>
        <p>商品加载中…</p>
      </div>
      <div v-else-if="error" class="error-state">{{ error }}</div>
      <div v-else-if="product" class="detail-grid">
        <!-- 左：商品图 -->
        <div class="left">
          <div class="cover">
            <img :src="product.cover_url" :alt="product.name" />
          </div>
          <div class="thumbs">
            <div class="thumb active"><img :src="product.cover_url" /></div>
            <div class="thumb"><img :src="product.cover_url" /></div>
            <div class="thumb"><img :src="product.cover_url" /></div>
            <div class="thumb"><img :src="product.cover_url" /></div>
          </div>
        </div>

        <!-- 右：商品信息 -->
        <div class="right">
          <h1>{{ product.name }}</h1>
          <div class="badges">
            <span class="badge">自营</span>
            <span class="badge">7 天无理由退换</span>
            <span class="badge">正品保障</span>
          </div>

          <!-- 价格区 -->
          <div class="price-box">
            <div class="price-label">智 选 价</div>
            <div class="price-line">
              <span class="price-symbol">¥</span>
              <span class="price-num">{{ product.price.toLocaleString('zh-CN') }}</span>
              <span class="stock-tag" :class="{ low: product.stock < 5 && product.stock > 0, out: product.stock === 0 }">
                {{ product.stock > 0 ? (product.stock < 5 ? `仅剩 ${product.stock} 件` : `库存 ${product.stock} 件`) : '无货' }}
              </span>
            </div>
          </div>

          <!-- 描述 -->
          <p v-if="product.description" class="description">
            {{ product.description }}
          </p>

          <!-- 规格参数 -->
          <div v-if="attributeRows.length" class="attrs">
            <div class="attrs-title">规格参数</div>
            <table>
              <tbody>
                <tr v-for="row in attributeRows" :key="row.key">
                  <th>{{ row.key }}</th>
                  <td>{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- CTA -->
          <div class="actions">
            <button class="ask-btn-primary" @click="onAsk">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path d="M21 12a9 9 0 11-9-9 9 9 0 019 9z" stroke="currentColor" stroke-width="2"/>
                <path d="M9 10h.01M15 10h.01M9 14c1 1 2 1.5 3 1.5s2-.5 3-1.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              </svg>
              咨询客服
            </button>
            <button
              class="buy-btn-primary"
              :disabled="buying || product.stock === 0"
              @click="buyNow"
            >
              {{ buying ? '下单中…' : (product.stock === 0 ? '无货' : '立即购买') }}
            </button>
            <span class="hint">登录后即可下单 · AI 客服 24h 在线</span>
          </div>
          <div v-if="buyError" class="buy-error">{{ buyError }}</div>
        </div>
      </div>

      <!-- 相关推荐 -->
      <section v-if="related.length" class="related">
        <div class="related-title">看了又看</div>
        <div class="related-grid">
          <div
            v-for="p in related"
            :key="p.sku"
            class="related-item"
            @click="openProduct(p.sku)"
          >
            <div class="related-cover"><img :src="p.cover_url" /></div>
            <div class="related-name">{{ p.name }}</div>
            <div class="related-price">¥{{ p.price.toLocaleString('zh-CN') }}</div>
          </div>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.detail-page {
  flex: 1;
  overflow-y: auto;
  background: var(--gray-50);
}
.detail-inner {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-4) var(--sp-6);
}

/* 面包屑 */
.breadcrumb {
  font-size: var(--fs-sm);
  color: var(--gray-500);
  margin-bottom: var(--sp-3);
}
.breadcrumb a {
  color: var(--gray-600);
  cursor: pointer;
}
.breadcrumb a:hover {
  color: var(--jd-red);
}
.breadcrumb .sep {
  margin: 0 var(--sp-2);
}

/* 主区 */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--sp-6);
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-6);
}
@media (max-width: 768px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}

/* 左图 */
.cover {
  width: 100%;
  aspect-ratio: 1;
  background: var(--gray-50);
  border: var(--border);
}
.cover img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.thumbs {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}
.thumb {
  width: 60px;
  height: 60px;
  border: 1px solid var(--gray-200);
  cursor: pointer;
  padding: 2px;
  background: var(--gray-50);
}
.thumb.active {
  border-color: var(--jd-red);
}
.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 右信息 */
.right h1 {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-xl);
  font-weight: 600;
  color: var(--gray-800);
  line-height: 1.4;
}
.badges {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-4);
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-size: var(--fs-xs);
  border: 1px solid var(--jd-red);
}

.price-box {
  background: var(--jd-red-light);
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-4);
}
.price-label {
  font-size: var(--fs-xs);
  color: var(--jd-red);
  margin-bottom: var(--sp-1);
}
.price-line {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.price-symbol {
  font-size: var(--fs-lg);
  color: var(--jd-red);
  font-weight: 600;
}
.price-num {
  font-size: 32px;
  color: var(--jd-red);
  font-weight: 700;
}
.stock-tag {
  display: inline-block;
  padding: 2px 8px;
  background: var(--gray-0);
  color: var(--jd-red);
  font-size: var(--fs-xs);
  border: 1px solid var(--jd-red);
}
.stock-tag.low {
  background: #fff3e0;
  color: #ff8800;
  border-color: #ff8800;
}
.stock-tag.out {
  background: var(--gray-100);
  color: var(--gray-500);
  border-color: var(--gray-300);
}

.description {
  margin: 0 0 var(--sp-4);
  color: var(--gray-700);
  line-height: 1.7;
  font-size: var(--fs-base);
}

.attrs {
  margin: 0 0 var(--sp-4);
}
.attrs-title {
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--gray-800);
  margin-bottom: var(--sp-2);
  padding-bottom: var(--sp-2);
  border-bottom: var(--border);
}
.attrs table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
.attrs th {
  width: 100px;
  text-align: left;
  padding: var(--sp-2) var(--sp-3);
  background: var(--gray-50);
  color: var(--gray-600);
  font-weight: 400;
  border: 1px solid var(--gray-200);
}
.attrs td {
  padding: var(--sp-2) var(--sp-3);
  color: var(--gray-800);
  border: 1px solid var(--gray-200);
}

.actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding-top: var(--sp-4);
  border-top: var(--border);
}
.ask-btn-primary {
  padding: 12px 32px;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-md);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ask-btn-primary:hover {
  background: var(--jd-red-hover);
}
.ask-btn-secondary {
  padding: 12px 24px;
  background: var(--gray-0);
  color: var(--jd-red);
  border: 1px solid var(--jd-red);
  font-size: var(--fs-md);
  cursor: pointer;
}
.ask-btn-secondary:hover {
  background: var(--jd-red-light);
}
/* M10 立即购买按钮：右红主操作，比咨询略宽 */
.buy-btn-primary {
  padding: 12px 36px;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-md);
  cursor: pointer;
}
.buy-btn-primary:hover:not(:disabled) {
  background: var(--jd-red-hover, #c81623);
}
.buy-btn-primary:disabled {
  background: var(--gray-300);
  color: var(--gray-500);
  cursor: not-allowed;
}
.buy-error {
  margin-top: var(--sp-2);
  padding: 8px 12px;
  background: #fff3e0;
  color: #c81623;
  font-size: var(--fs-sm);
  border: 1px solid #c81623;
}
.hint {
  margin-left: auto;
  font-size: var(--fs-xs);
  color: var(--gray-500);
}

/* 相关推荐 */
.related {
  margin-top: var(--sp-4);
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-4) var(--sp-6);
}
.related-title {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--gray-800);
  margin-bottom: var(--sp-3);
  padding-bottom: var(--sp-2);
  border-bottom: 2px solid var(--jd-red);
  display: inline-block;
}
.related-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-3);
}
@media (max-width: 768px) {
  .related-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
.related-item {
  cursor: pointer;
  border: var(--border);
  transition: border-color 0.15s;
}
.related-item:hover {
  border-color: var(--jd-red);
}
.related-cover {
  aspect-ratio: 1;
  background: var(--gray-50);
}
.related-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.related-name {
  padding: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--gray-800);
  height: 40px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.related-price {
  padding: 0 var(--sp-2) var(--sp-2);
  font-size: var(--fs-md);
  color: var(--jd-red);
  font-weight: 700;
}

/* States */
.loading-state, .error-state {
  background: var(--gray-0);
  border: var(--border);
  text-align: center;
  padding: 80px 20px;
  color: var(--gray-500);
}
.error-state {
  color: var(--jd-red);
}
.spinner {
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
</style>