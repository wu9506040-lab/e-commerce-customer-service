<script setup lang="ts">
/**
 * 演示模式首页（M9 重构）
 * 给外部访问者（面试官 / 路人）30 秒 hook：
 * - Hero：项目 logo + 一句话价值主张
 * - 能力卡片：4 类意图 + 示例问题
 * - 技术栈标签
 * - 指标快照（从 /metrics 拉）
 * - CTA：注册体验 / 直接登录
 */
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { getMetrics, type MetricsSnapshot } from '../api';

const router = useRouter();
const metrics = ref<MetricsSnapshot | null>(null);
const loading = ref(true);

// 客户端粗判断登录态（真正鉴权由后端 cookie 决定）
const isLoggedIn = computed(() => {
  if (typeof document === 'undefined') return false;
  return document.cookie.includes('cs_token');
});

const capabilities = [
  {
    intent: '订单查询',
    icon: '📦',
    color: '#667eea',
    examples: ['ORD20260622003 现在到哪了', '我的订单有哪些', 'ORD20260621002 啥情况'],
  },
  {
    intent: '退款咨询',
    icon: '↩️',
    color: '#f5576c',
    examples: ['ORD20260622003 能退吗', '怎么申请退货', '退款多久到账'],
  },
  {
    intent: '商品咨询',
    icon: '🛍️',
    color: '#43e97b',
    examples: ['ZP1 现在多少钱', 'BP1 续航怎么样', '千元机推荐'],
  },
  {
    intent: '政策问答',
    icon: '📋',
    color: '#fee140',
    examples: ['7 天无理由退货运费谁出', '保修期多久', '支持哪些支付方式'],
  },
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
</script>

<template>
  <main class="demo">
    <!-- Hero -->
    <section class="hero">
      <div class="hero-inner">
        <div class="hero-badge">🎉 RAG + LangGraph 智能客服系统</div>
        <h1>让电商客服 <span class="grad">更智能</span></h1>
        <p class="hero-sub">
          基于意图识别的多轮对话，融合订单/商品/政策知识库，
          支持流式输出、上下文记忆、可观测监控。
        </p>
        <div class="hero-actions">
          <button class="btn-primary-lg" @click="goRegister">立即体验 →</button>
          <button class="btn-ghost-lg" @click="goLogin">已有账号登录</button>
        </div>
        <p class="hero-hint">
          💡 打开浏览器开发者工具查看 Network/SSE 看实时流式响应
        </p>
      </div>
    </section>

    <!-- 能力卡片 -->
    <section class="section">
      <h2 class="section-title">4 类意图，毫秒级响应</h2>
      <p class="section-sub">不同问题走不同流水线，规则 + LLM 智能融合</p>
      <div class="cap-grid">
        <div
          v-for="cap in capabilities"
          :key="cap.intent"
          class="cap-card"
          :style="{ borderTopColor: cap.color }"
        >
          <div class="cap-icon" :style="{ background: cap.color }">{{ cap.icon }}</div>
          <h3>{{ cap.intent }}</h3>
          <ul>
            <li v-for="ex in cap.examples" :key="ex" class="cap-example">
              <span class="q">"</span>{{ ex }}<span class="q">"</span>
            </li>
          </ul>
        </div>
      </div>
    </section>

    <!-- 指标快照 -->
    <section class="section metrics-section">
      <h2 class="section-title">📊 系统实时指标</h2>
      <p class="section-sub">从 /metrics 端点实时拉取</p>
      <div v-if="loading" class="metrics-loading">加载中…</div>
      <div v-else-if="metrics" class="metrics-grid">
        <div class="metric-card">
          <div class="metric-num">{{ metrics.chat?.total ?? 0 }}</div>
          <div class="metric-label">累计对话</div>
        </div>
        <div class="metric-card">
          <div class="metric-num">
            {{ metrics.chat?.latency_ms?.p50?.toFixed(0) ?? '-' }}
            <small>ms</small>
          </div>
          <div class="metric-label">首 token P50</div>
        </div>
        <div class="metric-card">
          <div class="metric-num">
            {{ ((metrics.rag?.qdrant_search_success ?? 0) / Math.max(1, metrics.rag?.qdrant_search_total ?? 1) * 100).toFixed(0) }}%
          </div>
          <div class="metric-label">RAG 检索成功率</div>
        </div>
        <div class="metric-card">
          <div class="metric-num">
            {{ ((metrics.hit_at_k?.['hit@5'] ?? 0) * 100).toFixed(0) }}%
          </div>
          <div class="metric-label">hit@5 命中率</div>
        </div>
      </div>
    </section>

    <!-- 技术栈 -->
    <section class="section">
      <h2 class="section-title">🛠 技术栈</h2>
      <div class="tech-tags">
        <span v-for="t in techStack" :key="t" class="tech-tag">{{ t }}</span>
      </div>
    </section>

    <!-- CTA -->
    <section class="cta">
      <h2>开始体验智能客服</h2>
      <p>注册新账号或使用测试账号登录，立即与 AI 客服对话</p>
      <div class="cta-actions">
        <button class="btn-primary-lg" @click="goRegister">注册账号</button>
        <button class="btn-ghost-lg" @click="goChat" v-if="isLoggedIn">
          直接对话
        </button>
        <button class="btn-ghost-lg" @click="goLogin" v-else>
          登录对话
        </button>
      </div>
    </section>

    <footer class="footer">
      <p>© 2026 智选电商客服 · 前后端分离 · Docker Compose 一键部署</p>
    </footer>
  </main>
</template>

<style scoped>
.demo {
  flex: 1;
  overflow-y: auto;
}

/* ============= Hero ============= */
.hero {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  padding: 80px 24px;
  text-align: center;
}
.hero-inner {
  max-width: 760px;
  margin: 0 auto;
}
.hero-badge {
  display: inline-block;
  padding: 6px 14px;
  background: rgba(255, 255, 255, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.3);
  border-radius: 20px;
  font-size: 13px;
  margin-bottom: 20px;
  backdrop-filter: blur(8px);
}
.hero h1 {
  font-size: 48px;
  font-weight: 800;
  margin: 0 0 16px;
  line-height: 1.2;
}
.grad {
  background: linear-gradient(90deg, #fbbf24 0%, #f97316 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
.hero-sub {
  font-size: 17px;
  opacity: 0.92;
  line-height: 1.6;
  margin: 0 0 32px;
}
.hero-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
  flex-wrap: wrap;
}
.hero-hint {
  margin: 24px 0 0;
  font-size: 13px;
  opacity: 0.7;
}

.btn-primary-lg {
  padding: 12px 28px;
  background: white;
  color: #4f46e5;
  border: none;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
}
.btn-primary-lg:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.15);
}
.btn-ghost-lg {
  padding: 12px 28px;
  background: transparent;
  color: white;
  border: 1px solid rgba(255, 255, 255, 0.5);
  border-radius: 8px;
  font-size: 16px;
  cursor: pointer;
}
.btn-ghost-lg:hover {
  background: rgba(255, 255, 255, 0.1);
}

/* ============= Section ============= */
.section {
  max-width: 1100px;
  margin: 0 auto;
  padding: 60px 24px;
}
.section-title {
  font-size: 30px;
  font-weight: 700;
  color: #1f2937;
  margin: 0 0 8px;
  text-align: center;
}
.section-sub {
  font-size: 15px;
  color: #6b7280;
  margin: 0 0 36px;
  text-align: center;
}

/* ============= 能力卡片 ============= */
.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 20px;
}
.cap-card {
  background: white;
  border-radius: 12px;
  padding: 24px 20px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.05);
  border-top: 4px solid;
  transition: transform 0.15s, box-shadow 0.15s;
}
.cap-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
}
.cap-icon {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  margin-bottom: 12px;
}
.cap-card h3 {
  margin: 0 0 12px;
  font-size: 16px;
  font-weight: 600;
  color: #1f2937;
}
.cap-card ul {
  margin: 0;
  padding: 0;
  list-style: none;
}
.cap-example {
  font-size: 13px;
  color: #6b7280;
  padding: 4px 0;
  line-height: 1.5;
}
.cap-example .q {
  color: #9ca3af;
}

/* ============= 指标 ============= */
.metrics-section {
  background: white;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
}
.metric-card {
  background: linear-gradient(135deg, #f9fafb 0%, #f3f4f6 100%);
  padding: 24px;
  border-radius: 12px;
  text-align: center;
}
.metric-num {
  font-size: 36px;
  font-weight: 700;
  color: #4f46e5;
}
.metric-num small {
  font-size: 14px;
  font-weight: 400;
  color: #9ca3af;
}
.metric-label {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
.metrics-loading {
  text-align: center;
  color: #9ca3af;
  padding: 40px;
}

/* ============= 技术栈 ============= */
.tech-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
}
.tech-tag {
  padding: 6px 14px;
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  font-size: 13px;
  color: #4b5563;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}

/* ============= CTA ============= */
.cta {
  background: #1f2937;
  color: white;
  padding: 60px 24px;
  text-align: center;
}
.cta h2 {
  font-size: 28px;
  margin: 0 0 12px;
}
.cta p {
  margin: 0 0 28px;
  color: #d1d5db;
}
.cta .btn-ghost-lg {
  border-color: #4b5563;
}
.cta-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
  flex-wrap: wrap;
}

/* ============= Footer ============= */
.footer {
  text-align: center;
  padding: 24px;
  background: #f9fafb;
  color: #6b7280;
  font-size: 13px;
}
</style>
