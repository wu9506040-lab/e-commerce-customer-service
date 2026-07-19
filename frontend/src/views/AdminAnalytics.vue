<script setup lang="ts">
import { useAdminAnalytics } from '../composables/useAdminAnalytics';

const {
  startDate,
  endDate,
  data,
  loading,
  error,
  totalConversations,
  totalActiveUserDays,
  handoffPriorities,
  handoffCategories,
  activityWidth,
  handoffWidth,
  formatPercent,
  formatTimestamp,
  refresh,
} = useAdminAnalytics();
</script>

<template>
  <main class="analytics-page" data-testid="admin-analytics-page">
    <section class="page-heading">
      <div>
        <p class="eyebrow">ADMIN ANALYTICS</p>
        <h1>运营数据驾驶舱</h1>
        <p class="page-description">观察会话活跃度、响应效率、转人工分布与 RAG 实时质量。</p>
      </div>
      <form class="date-filter" @submit.prevent="refresh">
        <label>
          开始日期
          <input v-model="startDate" type="date" required />
        </label>
        <label>
          结束日期
          <input v-model="endDate" type="date" required />
        </label>
        <button type="submit" :disabled="loading">
          {{ loading ? '查询中' : '刷新数据' }}
        </button>
      </form>
    </section>

    <div v-if="error" class="status-box error" role="alert">
      <strong>加载失败</strong>
      <span>{{ error }}</span>
    </div>
    <div v-else-if="loading && !data" class="status-box">正在计算运营指标...</div>

    <template v-if="data">
      <section class="meta-line">
        <span>生成时间 {{ formatTimestamp(data.generated_at) }}</span>
        <span>{{ data.cache_hit ? 'Redis 缓存命中' : '数据库实时计算' }}</span>
        <span>缓存 {{ data.cache_ttl_seconds }} 秒</span>
      </section>

      <section class="kpi-grid" aria-label="核心指标">
        <article class="kpi-card">
          <span class="kpi-label">会话总量</span>
          <strong>{{ totalConversations }}</strong>
          <small>所选时间窗内按日去重</small>
        </article>
        <article class="kpi-card">
          <span class="kpi-label">活跃用户日</span>
          <strong>{{ totalActiveUserDays }}</strong>
          <small>每日去重用户数之和</small>
        </article>
        <article class="kpi-card">
          <span class="kpi-label">响应 P95</span>
          <strong>{{ data.latency.p95_ms }}<em> ms</em></strong>
          <small>{{ data.latency.samples }} 条 assistant 样本</small>
        </article>
        <article class="kpi-card accent">
          <span class="kpi-label">RAG hit@5</span>
          <strong>{{ formatPercent(data.hit_at_k.hit_at_5) }}</strong>
          <small>最近 {{ data.hit_at_k.window_size }} 次检索窗口</small>
        </article>
      </section>

      <section class="dashboard-grid">
        <article class="panel activity-panel">
          <header class="panel-header">
            <div>
              <h2>每日会话活跃度</h2>
              <p>会话按当日消息去重，空白日期自动补零。</p>
            </div>
          </header>
          <div class="activity-list">
            <div v-for="point in data.daily_activity" :key="point.date" class="activity-row">
              <time :datetime="point.date">{{ point.date.slice(5) }}</time>
              <div class="bar-track" :title="`${point.conversations} 个会话`">
                <div class="activity-bar" :style="{ width: activityWidth(point.conversations) }"></div>
              </div>
              <strong>{{ point.conversations }}</strong>
              <span>{{ point.active_users }} 用户 / {{ point.messages }} 消息</span>
            </div>
          </div>
        </article>

        <article class="panel latency-panel">
          <header class="panel-header">
            <div>
              <h2>响应延迟</h2>
              <p>来自 MySQL assistant 消息。</p>
            </div>
          </header>
          <div class="latency-values">
            <div>
              <span>P50</span>
              <strong>{{ data.latency.p50_ms }}</strong>
              <small>ms</small>
            </div>
            <div>
              <span>P95</span>
              <strong>{{ data.latency.p95_ms }}</strong>
              <small>ms</small>
            </div>
          </div>
        </article>

        <article class="panel handoff-panel">
          <header class="panel-header">
            <div>
              <h2>转人工分布</h2>
              <p>共 {{ data.handoffs.total }} 次持久化 handoff 事件。</p>
            </div>
            <span class="partial-badge">部分口径</span>
          </header>
          <div class="priority-list">
            <div v-for="item in handoffPriorities" :key="item.priority" class="priority-row">
              <span :class="['priority-dot', item.priority.toLowerCase()]" aria-hidden="true"></span>
              <span class="priority-name">{{ item.priority }}</span>
              <div class="bar-track">
                <div
                  :class="['handoff-bar', item.priority.toLowerCase()]"
                  :style="{ width: handoffWidth(item.count) }"
                ></div>
              </div>
              <strong>{{ item.count }}</strong>
            </div>
          </div>
          <div v-if="handoffCategories.length" class="category-list">
            <span v-for="([category, count]) in handoffCategories" :key="category">
              {{ category }} · {{ count }}
            </span>
          </div>
        </article>

        <article class="panel hit-panel">
          <header class="panel-header">
            <div>
              <h2>RAG 实时命中率</h2>
              <p>进程内最近 100 次查询滑动窗口。</p>
            </div>
          </header>
          <div class="hit-grid">
            <div><span>hit@1</span><strong>{{ formatPercent(data.hit_at_k.hit_at_1) }}</strong></div>
            <div><span>hit@3</span><strong>{{ formatPercent(data.hit_at_k.hit_at_3) }}</strong></div>
            <div><span>hit@5</span><strong>{{ formatPercent(data.hit_at_k.hit_at_5) }}</strong></div>
            <div><span>hit@10</span><strong>{{ formatPercent(data.hit_at_k.hit_at_10) }}</strong></div>
          </div>
          <p class="sample-note">累计样本 {{ data.hit_at_k.total_samples }}，当前窗口 {{ data.hit_at_k.window_size }}。</p>
        </article>
      </section>

      <section class="limitations" aria-label="指标口径限制">
        <h2>口径说明</h2>
        <ul>
          <li v-for="item in data.limitations" :key="item">{{ item }}</li>
        </ul>
      </section>
    </template>
  </main>
</template>

<style scoped>
.analytics-page {
  width: min(1180px, calc(100% - 48px));
  margin: 0 auto;
  padding: 36px 0 64px;
  color: var(--gray-800);
}
.page-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 32px;
  margin-bottom: 24px;
}
.eyebrow {
  margin: 0 0 8px;
  color: var(--jd-red);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 1.6px;
}
h1 {
  margin: 0;
  font-size: 30px;
  line-height: 1.2;
}
.page-description {
  margin: 10px 0 0;
  color: var(--gray-500);
}
.date-filter {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  padding: 16px;
  border: var(--border);
  background: var(--gray-0);
}
.date-filter label {
  display: grid;
  gap: 6px;
  color: var(--gray-600);
  font-size: 12px;
}
.date-filter input {
  height: 38px;
  padding: 0 10px;
  border: var(--border);
  color: var(--gray-800);
  font: inherit;
}
.date-filter button {
  height: 38px;
  padding: 0 18px;
  border: 0;
  background: var(--jd-red);
  color: #fff;
  cursor: pointer;
}
.date-filter button:disabled {
  cursor: wait;
  opacity: 0.65;
}
.status-box {
  display: flex;
  gap: 12px;
  padding: 18px;
  border: var(--border);
  background: var(--gray-0);
}
.status-box.error {
  border-color: #ffb8b8;
  background: var(--jd-red-light);
  color: var(--jd-red-dark);
}
.meta-line {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-bottom: 16px;
  color: var(--gray-500);
  font-size: 12px;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.kpi-card {
  min-height: 136px;
  padding: 20px;
  border: var(--border);
  border-top: 3px solid var(--gray-300);
  background: var(--gray-0);
  box-shadow: var(--shadow-sm);
}
.kpi-card.accent {
  border-top-color: var(--jd-red);
}
.kpi-label {
  display: block;
  color: var(--gray-500);
  font-size: 13px;
}
.kpi-card strong {
  display: block;
  margin: 12px 0 8px;
  font-size: 30px;
  line-height: 1;
}
.kpi-card em {
  color: var(--gray-500);
  font-size: 14px;
  font-style: normal;
  font-weight: 500;
}
.kpi-card small {
  color: var(--gray-500);
}
.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(280px, 1fr);
  gap: 16px;
}
.panel {
  padding: 22px;
  border: var(--border);
  background: var(--gray-0);
  box-shadow: var(--shadow-sm);
}
.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 20px;
}
.panel h2,
.limitations h2 {
  margin: 0;
  font-size: 18px;
}
.panel-header p {
  margin: 6px 0 0;
  color: var(--gray-500);
  font-size: 13px;
}
.activity-list,
.priority-list {
  display: grid;
  gap: 13px;
}
.activity-row {
  display: grid;
  grid-template-columns: 44px minmax(120px, 1fr) 28px 128px;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.activity-row time,
.activity-row span {
  color: var(--gray-500);
}
.bar-track {
  height: 9px;
  overflow: hidden;
  background: var(--gray-100);
}
.activity-bar,
.handoff-bar {
  height: 100%;
  background: var(--jd-red);
  transition: width 0.25s ease;
}
.latency-values {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.latency-values div,
.hit-grid div {
  padding: 18px;
  background: var(--gray-50);
}
.latency-values span,
.hit-grid span {
  display: block;
  color: var(--gray-500);
  font-size: 12px;
}
.latency-values strong,
.hit-grid strong {
  display: inline-block;
  margin-top: 8px;
  font-size: 24px;
}
.latency-values small {
  margin-left: 4px;
  color: var(--gray-500);
}
.partial-badge {
  padding: 4px 8px;
  background: #fff7e6;
  color: #ad6800;
  font-size: 12px;
}
.priority-row {
  display: grid;
  grid-template-columns: 10px 72px minmax(100px, 1fr) 28px;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.priority-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--gray-400);
}
.priority-dot.p0,
.handoff-bar.p0 { background: #cf1322; }
.priority-dot.p1,
.handoff-bar.p1 { background: #d48806; }
.priority-dot.p2,
.handoff-bar.p2 { background: #1677ff; }
.handoff-bar.unclassified { background: var(--gray-400); }
.priority-name { color: var(--gray-600); }
.category-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}
.category-list span {
  padding: 5px 8px;
  background: var(--gray-50);
  color: var(--gray-600);
  font-size: 12px;
}
.hit-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.sample-note {
  margin: 14px 0 0;
  color: var(--gray-500);
  font-size: 12px;
}
.limitations {
  margin-top: 16px;
  padding: 20px 22px;
  border: 1px solid #ffe58f;
  background: #fffbe6;
}
.limitations ul {
  margin: 10px 0 0;
  padding-left: 20px;
  color: #614700;
  font-size: 13px;
  line-height: 1.8;
}
@media (max-width: 1024px) {
  .page-heading { align-items: stretch; flex-direction: column; }
  .date-filter { align-self: flex-start; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dashboard-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .analytics-page { width: min(100% - 28px, 1180px); padding-top: 24px; }
  .date-filter { align-self: stretch; align-items: stretch; flex-direction: column; }
  .kpi-grid { grid-template-columns: 1fr; }
  .activity-row { grid-template-columns: 42px minmax(80px, 1fr) 24px; }
  .activity-row > span:last-child { display: none; }
}
</style>
