<script setup lang="ts">
/**
 * 商品详情页（M9 新增）
 * 大图 + 名称 + 价格 + 描述 + 属性表 + 问客服按钮
 */
import { ref, computed, onMounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getProduct } from '../api';
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

onMounted(load);
watch(sku, load);

// 属性表：把 attributes dict 转成 [{key, value}]
const attributeRows = computed(() => {
  if (!product.value?.attributes) return [];
  return Object.entries(product.value.attributes).map(([key, value]) => ({
    key,
    value: Array.isArray(value) ? value.join(' / ') : String(value),
  }));
});

function onAsk() {
  if (!product.value) return;
  router.push({
    name: 'chat',
    query: { q: `${product.value.sku} 怎么样` },
  });
}
</script>

<template>
  <main class="detail-page">
    <div class="detail-inner">
      <button class="back-btn" @click="router.back()">← 返回</button>

      <div v-if="loading" class="loading-state">
        <div class="spinner"></div>
        <p>商品加载中…</p>
      </div>
      <div v-else-if="error" class="error-state">⚠️ {{ error }}</div>
      <div v-else-if="product" class="detail-grid">
        <div class="left">
          <ProductCard :product="product" density="detail" @ask="onAsk" />
        </div>
        <div class="right">
          <h1>{{ product.name }}</h1>
          <div class="price-line">
            <span class="price">¥{{ product.price.toLocaleString('zh-CN') }}</span>
            <span class="stock">📦 库存 {{ product.stock }} 件</span>
          </div>

          <p v-if="product.description" class="description">
            {{ product.description }}
          </p>

          <div v-if="attributeRows.length" class="attrs">
            <h3>规格参数</h3>
            <table>
              <tbody>
                <tr v-for="row in attributeRows" :key="row.key">
                  <th>{{ row.key }}</th>
                  <td>{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="actions">
            <button class="ask-btn" @click="onAsk">💬 问问客服</button>
            <span class="hint">登录后即可对话，AI 客服 24h 在线</span>
          </div>
        </div>
      </div>
    </div>
  </main>
</template>

<style scoped>
.detail-page {
  flex: 1;
  overflow-y: auto;
  background: white;
}
.detail-inner {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}
.back-btn {
  margin-bottom: 16px;
  padding: 6px 14px;
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  font-size: 13px;
  color: #4b5563;
  cursor: pointer;
}
.back-btn:hover {
  background: #f9fafb;
}
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
  gap: 40px;
}
@media (max-width: 768px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}
.right h1 {
  margin: 0 0 16px;
  font-size: 24px;
  font-weight: 600;
  color: #1f2937;
  line-height: 1.4;
}
.price-line {
  display: flex;
  align-items: baseline;
  gap: 16px;
  padding: 16px 0;
  border-top: 1px solid #f3f4f6;
  border-bottom: 1px solid #f3f4f6;
}
.price {
  font-size: 32px;
  font-weight: 700;
  color: #dc2626;
}
.stock {
  font-size: 13px;
  color: #6b7280;
}
.description {
  margin: 20px 0;
  color: #4b5563;
  line-height: 1.7;
  font-size: 14px;
}
.attrs {
  margin: 20px 0;
}
.attrs h3 {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: #1f2937;
}
.attrs table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.attrs th {
  width: 100px;
  text-align: left;
  padding: 10px 12px;
  background: #f9fafb;
  color: #6b7280;
  font-weight: 500;
  border: 1px solid #f3f4f6;
}
.attrs td {
  padding: 10px 12px;
  color: #1f2937;
  border: 1px solid #f3f4f6;
}
.actions {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 24px;
}
.ask-btn {
  padding: 12px 28px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
}
.ask-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(102, 126, 234, 0.3);
}
.hint {
  font-size: 13px;
  color: #9ca3af;
}
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
