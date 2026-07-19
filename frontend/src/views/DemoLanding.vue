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
import { getMetrics, type MetricsSnapshot, isAuthed } from '../api';

const router = useRouter();
const metrics = ref<MetricsSnapshot | null>(null);
const loading = ref(true);

const isLoggedIn = computed(() => isAuthed.value === true);

// 4 类意图卡片（京东红强调）+ M14 V3：退款主动查询 + 转人工兜底
const capabilities = [
  {
    intent: '订单查询',
    desc: '订单状态 / 物流 / 详情一键查',
    examples: ['ORD20260622003 现在到哪了', '我的订单有哪些', 'ORD20260621002 啥情况'],
  },
  {
    intent: '退款咨询',
    desc: 'M14 V3：自动解析最近订单 · 不再问订单号',
    examples: ['我的衣服有问题能退吗', '我想退件商品', '前天买的想退款'],
  },
  {
    intent: '商品咨询',
    desc: '价格 / 库存 / 规格 + 跨 SKU 推荐',
    examples: ['Z1 旗舰手机多少钱', 'E1 耳机续航怎么样', '千元机推荐'],
  },
  {
    intent: '转人工兜底',
    desc: 'M14 V3：Agent 异常 / 用户主动升级 · 工单 + 名片打包',
    examples: ['我要转人工', '找真人客服', '这 AI 答不了'],
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
/**
 * M13 cloud：首页"立即体验"直跳 + 一键 demo
 * 公开 demo 站点访客无需注册，秒进系统
 * P0-B：demo 体验的目的是让用户立刻看到 AI 客服 → 跳 /chat（最有说服力），而不是 /shop
 */
async function goDemo() {
  try {
    const { demoLogin } = await import('../api');
    await demoLogin();
    router.push({ name: 'chat' });
  } catch (e) {
    console.error('demo 登录失败:', e);
    // fallback 跳登录页
    router.push({ name: 'login' });
  }
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
    <!-- ============= Hero Banner（京东红 + 渐变 + Live 指示） ============= -->
    <section class="hero">
      <div class="hero-bg"></div>
      <div class="hero-inner">
        <div class="hero-tag">
          <span class="live-dot"></span>
          RAG · LangGraph · 多意图智能客服
        </div>
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
          <div class="metric-label">近期 RAG 检索成功率</div>
          <div class="metric-sub">n={{ metrics.rag?.qdrant_search_total ?? 0 }}（运行滑窗）</div>
        </div>
        <div class="metric-box">
          <div class="metric-num">
            {{ ((metrics.hit_at_k?.['hit@5'] ?? 0) * 100).toFixed(0) }}%
          </div>
          <div class="metric-label">近窗 hit@5 命中率</div>
          <div class="metric-sub">n={{ metrics.hit_at_k?.total_samples ?? 0 }}（运行滑窗）</div>
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

    <!-- ============= 架构亮点（M13 cloud：5 秒看懂技术深度）============= -->
    <section class="section highlights-section">
      <div class="section-head">
        <h2>工程亮点</h2>
        <p>不堆 chat 模板，从生产工程视角看真实难度</p>
      </div>
      <div class="highlight-grid">
        <div class="highlight-box">
          <div class="highlight-num">385 / 385</div>
          <div class="highlight-label">pytest 测试通过</div>
          <div class="highlight-desc">退款反幻觉 / 防串单 / 上下文贯通 / Token 防滥用 25+ 案例</div>
        </div>
        <div class="highlight-box">
          <div class="highlight-num">0 <small>token</small></div>
          <div class="highlight-label">3 层 InputGuard</div>
          <div class="highlight-desc">规则 + embedding 闲聊识别 + 行为监控，挡 95% 异常请求</div>
        </div>
        <div class="highlight-box">
          <div class="highlight-num">4 节点</div>
          <div class="highlight-label">LangGraph 退款状态机</div>
          <div class="highlight-desc">可退/质量问题/超期/已退 4 路径分支 + V2 fallback</div>
        </div>
        <div class="highlight-box">
          <div class="highlight-num">M14 <small>V3</small></div>
          <div class="highlight-label">主动查询 + 转人工兜底</div>
          <div class="highlight-desc">Resolver 4 决策自动解析最近订单 · 异常/用户升级触发 HandoffCard</div>
        </div>
      </div>
    </section>

    <!-- ============= CTA ============= -->
    <section class="cta">
      <div class="cta-inner">
        <h2>开始体验智选客服</h2>
        <p>无需注册，一键体验 demo 账号，立即与 AI 客服对话</p>
        <div class="cta-actions">
          <!-- M13 cloud：访客一键体验，主推 -->
          <button class="btn-cta-primary" @click="goDemo">
            立即体验 demo →
          </button>
          <button class="btn-cta-ghost" @click="goChat" v-if="isLoggedIn">直接对话</button>
          <button class="btn-cta-ghost" @click="goRegister" v-else>注册正式账号</button>
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
  position: relative;
  background: var(--jd-red);
  color: #fff;
  padding: 90px 24px;
  text-align: center;
  overflow: hidden;
}
.hero-bg {
  /* 渐变 + 隐约几何纹理，比纯红更现代 */
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at top right, rgba(255, 255, 255, 0.12), transparent 60%),
    radial-gradient(ellipse at bottom left, rgba(168, 0, 10, 0.4), transparent 60%),
    linear-gradient(135deg, var(--jd-red) 0%, var(--jd-red-hover) 100%);
  pointer-events: none;
}
.hero-inner {
  position: relative;
  max-width: 760px;
  margin: 0 auto;
}
.hero-tag {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  background: rgba(255, 255, 255, 0.18);
  border: 1px solid rgba(255, 255, 255, 0.3);
  font-size: var(--fs-sm);
  margin-bottom: var(--sp-4);
  letter-spacing: 1px;
  backdrop-filter: blur(4px);
}
.live-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  background: #4ade80;
  border-radius: 50%;
  box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7);
  animation: live-pulse 1.6s ease-out infinite;
}
@keyframes live-pulse {
  0% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7); }
  70% { box-shadow: 0 0 0 10px rgba(74, 222, 128, 0); }
  100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0); }
}
.hero-title {
  font-size: var(--fs-4xl);
  font-weight: 800;
  margin: 0 0 var(--sp-4);
  line-height: 1.1;
  letter-spacing: 6px;
  text-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}
.hero-sub {
  font-size: var(--fs-lg);
  opacity: 0.95;
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
  padding: 14px 36px;
  background: #fff;
  color: var(--jd-red);
  border: none;
  font-size: var(--fs-md);
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}
.btn-hero-primary:hover {
  background: var(--gray-100);
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.15);
}
.btn-hero-ghost {
  padding: 14px 36px;
  background: transparent;
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.5);
  font-size: var(--fs-md);
  cursor: pointer;
  transition: all 0.15s;
}
.btn-hero-ghost:hover {
  background: rgba(255, 255, 255, 0.12);
  border-color: #fff;
}
.hero-hint {
  margin: var(--sp-5) 0 0;
  font-size: var(--fs-sm);
  opacity: 0.75;
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
  padding: 70px var(--sp-6);
}
.section-head {
  margin-bottom: var(--sp-6);
}
.section-head h2 {
  font-size: var(--fs-2xl);
  font-weight: 700;
  color: var(--gray-800);
  margin: 0 0 var(--sp-2);
  padding-left: var(--sp-3);
  border-left: 4px solid var(--jd-red);
}
.section-head p {
  font-size: var(--fs-base);
  color: var(--gray-500);
  margin: 0;
  padding-left: calc(var(--sp-3) + 4px);
}

/* ============= 能力卡片 ============= */
.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--sp-4);
}
.cap-card {
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-5);
  transition: all 0.2s ease;
  position: relative;
  overflow: hidden;
}
.cap-card::before {
  /* 顶部红色细线，hover 时拉宽 */
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 0;
  height: 3px;
  background: var(--jd-red);
  transition: width 0.25s ease;
}
.cap-card:hover {
  border-color: var(--jd-red);
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(225, 37, 27, 0.08);
}
.cap-card:hover::before {
  width: 100%;
}
.cap-head {
  margin-bottom: var(--sp-3);
}
.cap-tag {
  display: inline-block;
  padding: 3px 12px;
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-size: var(--fs-sm);
  font-weight: 600;
}
.cap-desc {
  margin: 0 0 var(--sp-3);
  color: var(--gray-700);
  font-size: var(--fs-base);
  line-height: 1.6;
}
.cap-examples {
  list-style: none;
  margin: 0;
  padding: var(--sp-3) 0 0;
  border-top: 1px dashed var(--gray-200);
}
.cap-examples li {
  font-size: var(--fs-sm);
  color: var(--gray-600);
  padding: 5px 0;
  line-height: 1.6;
}
.qmark {
  color: var(--jd-red);
  font-weight: 600;
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
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--sp-4);
}
.metric-box {
  background: linear-gradient(135deg, var(--gray-0) 0%, var(--gray-50) 100%);
  border: var(--border);
  padding: var(--sp-5) var(--sp-4);
  text-align: center;
  transition: all 0.2s;
  position: relative;
}
.metric-box:hover {
  border-color: var(--jd-red);
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(225, 37, 27, 0.06);
}
.metric-num {
  font-size: var(--fs-3xl);
  font-weight: 700;
  color: var(--jd-red);
  line-height: 1;
  letter-spacing: -1px;
}
.metric-num small {
  font-size: var(--fs-lg);
  font-weight: 400;
  color: var(--gray-500);
  margin-left: 2px;
}
.metric-label {
  margin-top: var(--sp-3);
  font-size: var(--fs-base);
  color: var(--gray-600);
}
.metric-sub {
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--gray-500);
  letter-spacing: 0.2px;
}
.metrics-loading {
  text-align: center;
  color: var(--gray-500);
  padding: var(--sp-8);
}

/* ============= 工程亮点（M13 cloud：数字锚点）============= */
.highlights-section {
  background: linear-gradient(180deg, #fff8f7 0%, var(--gray-50) 100%);
}
.highlight-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-4);
}
@media (max-width: 768px) {
  .highlight-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
.highlight-box {
  background: var(--gray-0);
  padding: var(--sp-6) var(--sp-5);
  text-align: center;
  border: var(--border);
  transition: all 0.2s;
}
.highlight-box:hover {
  border-color: var(--jd-red);
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(225, 37, 27, 0.06);
}
.highlight-num {
  font-size: var(--fs-3xl);
  font-weight: 700;
  color: var(--jd-red);
  line-height: 1;
  letter-spacing: -1px;
}
.highlight-num small {
  font-size: var(--fs-lg);
  font-weight: 400;
  color: var(--gray-500);
  margin-left: 2px;
}
.highlight-label {
  margin-top: var(--sp-3);
  font-size: var(--fs-base);
  font-weight: 500;
  color: var(--gray-800);
}
.highlight-desc {
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--gray-500);
  line-height: 1.6;
  min-height: 2.6em;
}

/* ============= 技术栈 ============= */
.tech-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.tech-tag {
  padding: 8px 16px;
  background: var(--gray-0);
  border: var(--border);
  font-size: var(--fs-sm);
  color: var(--gray-700);
  transition: all 0.15s;
}
.tech-tag:hover {
  border-color: var(--jd-red);
  color: var(--jd-red);
  transform: translateY(-1px);
}

/* ============= CTA ============= */
.cta {
  background: var(--jd-red);
  color: #fff;
  padding: 70px var(--sp-6);
  text-align: center;
}
.cta-inner {
  max-width: var(--content-max);
  margin: 0 auto;
}
.cta h2 {
  font-size: var(--fs-3xl);
  font-weight: 700;
  margin: 0 0 var(--sp-2);
}
.cta p {
  margin: 0 0 var(--sp-5);
  opacity: 0.95;
  font-size: var(--fs-md);
}
.cta-actions {
  display: flex;
  gap: var(--sp-3);
  justify-content: center;
  flex-wrap: wrap;
}
.btn-cta-primary {
  padding: 14px 36px;
  background: #fff;
  color: var(--jd-red);
  border: none;
  font-size: var(--fs-md);
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}
.btn-cta-primary:hover {
  background: var(--gray-100);
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.15);
}
.btn-cta-ghost {
  padding: 14px 36px;
  background: transparent;
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.5);
  font-size: var(--fs-md);
  cursor: pointer;
  transition: all 0.15s;
}
.btn-cta-ghost:hover {
  background: rgba(255, 255, 255, 0.12);
  border-color: #fff;
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