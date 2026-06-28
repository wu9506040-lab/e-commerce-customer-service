<script setup lang="ts">
/**
 * 演示模式首页（京东红电商风）
 * - 顶部 banner：京东红 + 大字 + CTA
 * - 类目导航 + 商品精选（链接到 /shop）
 * - 能力卡片 4 类意图（无 emoji）
 * - 实时指标
 * - CTA
 */
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { getMetrics, type MetricsSnapshot } from '../api';

const router = useRouter();
const metrics = ref<MetricsSnapshot | null>(null);
const loading = ref(true);

const isLoggedIn = computed(() => {
  if (typeof document === 'undefined') return false;
  return document.cookie.includes('cs_token');
});

// 4 类意图卡片（京东红强调）
const capabilities = [
  {
    intent: '订单查询',
    desc: '订单状态 / 物流 / 详情一键查',
    examples: ['ORD20260622003 现在到哪了', '我的订单有哪些', 'ORD20260621002 啥情况'],
  },
  {
    intent: '退款咨询',
    desc: '智能判断可退性 + 流程引导',
    examples: ['ORD20260622003 能退吗', '怎么申请退货', '退款多久到账'],
  },
  {
    intent: '商品咨询',
    desc: '价格 / 库存 / 规格 + 跨 SKU 推荐',
    examples: ['ZP1 现在多少钱', 'BP1 续航怎么样', '千元机推荐'],
  },
  {
    intent: '政策问答',
    desc: 'RAG 检索 7 天无理由 / 保修 / 支付',
    examples: ['7 天无理由退货运费谁出', '保修期多久', '支持哪些支付方式'],
  },
];

// 类目（对应 /shop 侧边栏）
const categories = [
  { key: '手机', label: '手机' },
  { key: '耳机', label: '耳机' },
  { key: '手表', label: '智能手表' },
  { key: '平板', label: '平板电脑' },
  { key: '笔记本', label: '笔记本电脑' },
  { key: '键盘', label: '键盘' },
  { key: '鼠标', label: '鼠标' },
];

const techStack = [
  'Vue 3', 'Vite', 'TypeScript', 'FastAPI', 'Pydantic',
  'Qdrant', 'MySQL', 'Redis', 'Qwen LLM', 'LangGraph',
  'RAG', 'SSE',
];

onMounted(async () => {
  try {
    metrics.value = await getMetrics();
  } catch (e) {
    console.warn('拉取指标失败:', e);
  } finally {
    loading.value = false;
  }
});

function goRegister() {
  router.push({ name: 'login', query: { tab: 'register' } });
}
function goLogin() {
  router.push({ name: 'login' });
}
function goChat() {
  router.push({ name: 'chat' });
}
function goShop(cat?: string) {
  router.push({ name: 'shop', query: cat ? { q: cat } : {} });
}
</script>

<template>
  <main class="demo">
    <!-- ============= Hero Banner（京东红） ============= -->
    <section class="hero">
      <div class="hero-inner">
        <div class="hero-tag">RAG · LangGraph · 多意图智能客服</div>
        <h1 class="hero-title">智选客服</h1>
        <p class="hero-sub">让电商客服更智能 · 4 类意图毫秒级分流 · 实时流式回答</p>
        <div class="hero-actions">
          <button class="btn-hero-primary" @click="goRegister">立即注册体验</button>
          <button class="btn-hero-ghost" @click="goShop()">浏览商品 →</button>
        </div>
        <p class="hero-hint">打开浏览器开发者工具 Network/SSE 看实时流式响应</p>
      </div>
    </section>

    <!-- ============= 类目导航条 ============= -->
    <nav class="cat-nav">
      <div class="cat-nav-inner">
        <span
          v-for="cat in categories"
          :key="cat.key"
          class="cat-nav-item"
          @click="goShop(cat.key)"
        >{{ cat.label }}</span>
      </div>
    </nav>

    <!-- ============= 能力卡片 ============= -->
    <section class="section">
      <div class="section-head">
        <h2>4 类意图 · 毫秒级分流</h2>
        <p>不同问题走不同流水线，规则 + LLM 智能融合</p>
      </div>
      <div class="cap-grid">
        <div v-for="cap in capabilities" :key="cap.intent" class="cap-card">
          <div class="cap-head">
            <span class="cap-tag">{{ cap.intent }}</span>
          </div>
          <p class="cap-desc">{{ cap.desc }}</p>
          <ul class="cap-examples">
            <li v-for="ex in cap.examples" :key="ex">
              <span class="qmark">"</span>{{ ex }}<span class="qmark">"</span>
            </li>
          </ul>
        </div>
      </div>
    </section>

    <!-- ============= 实时指标 ============= -->
    <section class="section metrics-section">
      <div class="section-head">
        <h2>系统实时指标</h2>
        <p>从 /metrics 端点实时拉取</p>
      </div>
      <div v-if="loading" class="metrics-loading">加载中…</div>
      <div v-else-if="metrics" class="metrics-grid">
        <div class="metric-box">
          <div class="metric-num">{{ metrics.chat?.total ?? 0 }}</div>
          <div class="metric-label">累计对话</div>
        </div>
        <div class="metric-box">
          <div class="metric-num">
            {{ metrics.chat?.latency_ms?.p50?.toFixed(0) ?? '-' }}
            <small>ms</small>
          </div>
          <div class="metric-label">首 token P50</div>
        </div>
        <div class="metric-box">
          <div class="metric-num">
            {{ ((metrics.rag?.qdrant_search_success ?? 0) / Math.max(1, metrics.rag?.qdrant_search_total ?? 1) * 100).toFixed(0) }}%
          </div>
          <div class="metric-label">RAG 检索成功率</div>
        </div>
        <div class="metric-box">
          <div class="metric-num">
            {{ ((metrics.hit_at_k?.['hit@5'] ?? 0) * 100).toFixed(0) }}%
          </div>
          <div class="metric-label">hit@5 命中率</div>
        </div>
      </div>
    </section>

    <!-- ============= 技术栈 ============= -->
    <section class="section">
      <div class="section-head">
        <h2>技术栈</h2>
      </div>
      <div class="tech-tags">
        <span v-for="t in techStack" :key="t" class="tech-tag">{{ t }}</span>
      </div>
    </section>

    <!-- ============= CTA ============= -->
    <section class="cta">
      <div class="cta-inner">
        <h2>开始体验智选客服</h2>
        <p>注册新账号或直接登录，立即与 AI 客服对话</p>
        <div class="cta-actions">
          <button class="btn-cta-primary" @click="goRegister">注册账号</button>
          <button class="btn-cta-ghost" @click="goChat" v-if="isLoggedIn">直接对话</button>
          <button class="btn-cta-ghost" @click="goLogin" v-else>登录对话</button>
        </div>
      </div>
    </section>

    <!-- ============= Footer ============= -->
    <footer class="footer">
      <p>© 2026 智选电商客服 · 前后端分离 · Docker Compose 一键部署</p>
    </footer>
  </main>
</template>

<style scoped>
.demo {
  flex: 1;
  overflow-y: auto;
  background: var(--gray-50);
}

/* ============= Hero ============= */
.hero {
  background: var(--jd-red);
  color: #fff;
  padding: 80px 24px;
  text-align: center;
}
.hero-inner {
  max-width: 760px;
  margin: 0 auto;
}
.hero-tag {
  display: inline-block;
  padding: 4px 14px;
  background: rgba(255, 255, 255, 0.18);
  border: 1px solid rgba(255, 255, 255, 0.3);
  font-size: var(--fs-xs);
  margin-bottom: var(--sp-4);
  letter-spacing: 1px;
}
.hero-title {
  font-size: 56px;
  font-weight: 800;
  margin: 0 0 var(--sp-3);
  line-height: 1.1;
  letter-spacing: 4px;
}
.hero-sub {
  font-size: var(--fs-md);
  opacity: 0.92;
  line-height: 1.6;
  margin: 0 0 var(--sp-6);
}
.hero-actions {
  display: flex;
  gap: var(--sp-3);
  justify-content: center;
  flex-wrap: wrap;
}
.btn-hero-primary {
  padding: 12px 32px;
  background: #fff;
  color: var(--jd-red);
  border: none;
  font-size: var(--fs-md);
  font-weight: 600;
  cursor: pointer;
}
.btn-hero-primary:hover {
  background: var(--gray-100);
}
.btn-hero-ghost {
  padding: 12px 32px;
  background: transparent;
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.5);
  font-size: var(--fs-md);
  cursor: pointer;
}
.btn-hero-ghost:hover {
  background: rgba(255, 255, 255, 0.1);
}
.hero-hint {
  margin: var(--sp-5) 0 0;
  font-size: var(--fs-xs);
  opacity: 0.7;
}

/* ============= 类目导航 ============= */
.cat-nav {
  background: var(--gray-0);
  border-bottom: var(--border);
}
.cat-nav-inner {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: 0 var(--sp-6);
  display: flex;
  overflow-x: auto;
}
.cat-nav-item {
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-base);
  color: var(--gray-700);
  cursor: pointer;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
}
.cat-nav-item:hover {
  color: var(--jd-red);
  border-bottom-color: var(--jd-red);
}

/* ============= Section ============= */
.section {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: 60px var(--sp-6);
}
.section-head {
  margin-bottom: var(--sp-6);
}
.section-head h2 {
  font-size: var(--fs-2xl);
  font-weight: 700;
  color: var(--gray-800);
  margin: 0 0 var(--sp-1);
  padding-left: var(--sp-3);
  border-left: 4px solid var(--jd-red);
}
.section-head p {
  font-size: var(--fs-sm);
  color: var(--gray-500);
  margin: 0;
  padding-left: calc(var(--sp-3) + 4px);
}

/* ============= 能力卡片 ============= */
.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--sp-3);
}
.cap-card {
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-4);
  transition: border-color 0.15s;
}
.cap-card:hover {
  border-color: var(--jd-red);
}
.cap-head {
  margin-bottom: var(--sp-2);
}
.cap-tag {
  display: inline-block;
  padding: 2px 10px;
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-size: var(--fs-xs);
  font-weight: 600;
}
.cap-desc {
  margin: 0 0 var(--sp-3);
  color: var(--gray-700);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.cap-examples {
  list-style: none;
  margin: 0;
  padding: var(--sp-2) 0 0;
  border-top: 1px dashed var(--gray-200);
}
.cap-examples li {
  font-size: var(--fs-xs);
  color: var(--gray-600);
  padding: 4px 0;
  line-height: 1.5;
}
.qmark {
  color: var(--jd-red);
}

/* ============= 指标 ============= */
.metrics-section {
  background: var(--gray-0);
  border-top: var(--border);
  border-bottom: var(--border);
  max-width: none;
  padding-left: 0;
  padding-right: 0;
}
.metrics-section .section-head {
  max-width: var(--content-max);
  margin: 0 auto var(--sp-4);
  padding: 0 var(--sp-6);
}
.metrics-grid {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: 0 var(--sp-6) 60px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-3);
}
.metric-box {
  background: var(--gray-50);
  border: var(--border);
  padding: var(--sp-5);
  text-align: center;
}
.metric-num {
  font-size: var(--fs-3xl);
  font-weight: 700;
  color: var(--jd-red);
  line-height: 1;
}
.metric-num small {
  font-size: var(--fs-sm);
  font-weight: 400;
  color: var(--gray-500);
  margin-left: 2px;
}
.metric-label {
  margin-top: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--gray-600);
}
.metrics-loading {
  text-align: center;
  color: var(--gray-500);
  padding: var(--sp-8);
}

/* ============= 技术栈 ============= */
.tech-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.tech-tag {
  padding: 6px 14px;
  background: var(--gray-0);
  border: var(--border);
  font-size: var(--fs-sm);
  color: var(--gray-700);
}

/* ============= CTA ============= */
.cta {
  background: var(--jd-red);
  color: #fff;
  padding: 60px var(--sp-6);
  text-align: center;
}
.cta-inner {
  max-width: var(--content-max);
  margin: 0 auto;
}
.cta h2 {
  font-size: var(--fs-2xl);
  margin: 0 0 var(--sp-2);
}
.cta p {
  margin: 0 0 var(--sp-5);
  opacity: 0.92;
}
.cta-actions {
  display: flex;
  gap: var(--sp-3);
  justify-content: center;
  flex-wrap: wrap;
}
.btn-cta-primary {
  padding: 12px 32px;
  background: #fff;
  color: var(--jd-red);
  border: none;
  font-size: var(--fs-md);
  font-weight: 600;
  cursor: pointer;
}
.btn-cta-primary:hover {
  background: var(--gray-100);
}
.btn-cta-ghost {
  padding: 12px 32px;
  background: transparent;
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.5);
  font-size: var(--fs-md);
  cursor: pointer;
}
.btn-cta-ghost:hover {
  background: rgba(255, 255, 255, 0.1);
}

/* ============= Footer ============= */
.footer {
  text-align: center;
  padding: var(--sp-5);
  background: var(--gray-100);
  color: var(--gray-500);
  font-size: var(--fs-xs);
}
</style>