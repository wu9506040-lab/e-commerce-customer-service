<script setup lang="ts">
// V18.3 数据看板（BI 网格）— ECharts，真实统计（GET /admin/analytics/board）
// 演示数据仅作接口失败时的兜底展示（顶栏会保留"演示数据"角标）
import * as echarts from 'echarts';
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { http } from '../api';

const charts: echarts.ECharts[] = [];
const demo = ref(true);
const errMsg = ref('');

interface BoardData {
  generated_at: string;
  days: number;
  excluded_test_messages: number;
  kpis: {
    today_messages: number;
    avg_response_sec: number;
    ai_reply_rate: number;
    handoff_rate: number;
    bargain_resolve_rate: number;
    new_customers_today: number;
  };
  msg_trend: { dates: string[]; xianyu: number[]; mall: number[] };
  channel_split: { name: string; value: number }[];
  hourly: number[];
  resp_trend: { dates: string[]; avg: number[]; max: number[]; min: number[] };
  top_questions: { q: string; count: number }[];
  intent_split: { name: string; value: number }[];
  new_cust_trend: { dates: string[]; counts: number[] };
  customer_tags: { name: string; value: number }[];
}

const KPIS = ref<{ label: string; value: string; delta: string; up: boolean }[]>([]);

function kpisFrom(d: BoardData) {
  const k = d.kpis;
  return [
    { label: '今日消息量', value: String(k.today_messages), delta: `近${d.days}日累计 ${d.msg_trend.xianyu.reduce((a, b) => a + b, 0) + d.msg_trend.mall.reduce((a, b) => a + b, 0)} 条`, up: true },
    { label: '平均响应时间', value: `${k.avg_response_sec}s`, delta: '买家消息 → 客服回复', up: true },
    { label: 'AI 自动回复率', value: `${k.ai_reply_rate}%`, delta: 'assistant 消息占比', up: true },
    { label: '转人工率', value: `${k.handoff_rate}%`, delta: '工单数 / 会话数', up: true },
    { label: '议价解决率', value: `${k.bargain_resolve_rate}%`, delta: '议价诉求未升级工单', up: true },
    { label: '今日新增会话', value: String(k.new_customers_today), delta: '首次出现的会话', up: true },
  ];
}

function mkOption(cfg: Record<string, unknown>) {
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 42, right: 16, top: 34, bottom: 26 },
    ...cfg,
  };
}

function initCharts(d: BoardData) {
  const mount = (id: string, option: echarts.EChartsCoreOption) => {
    const el = document.getElementById(id);
    if (!el) return;
    const c = echarts.init(el);
    c.setOption(option);
    charts.push(c);
  };

  const dates = d.msg_trend.dates;

  // 1. 消息量趋势（双线）
  mount('chart-msg-trend', mkOption({
    legend: { top: 0, textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [
      { name: '闲鱼', type: 'line', smooth: true, data: d.msg_trend.xianyu, itemStyle: { color: '#f97316' } },
      { name: '商城', type: 'line', smooth: true, data: d.msg_trend.mall, itemStyle: { color: '#3b82f6' } },
    ],
  }));

  // 2. 渠道分布
  mount('chart-channel', {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { formatter: '{b}\n{d}%', fontSize: 11 },
      data: d.channel_split.map((x, i) => ({
        ...x, itemStyle: { color: ['#3b82f6', '#f97316'][i % 2] },
      })),
    }],
  });

  // 3. 峰值时段（24h 柱状）
  mount('chart-hours', mkOption({
    xAxis: { type: 'category', data: Array.from({ length: 24 }, (_, i) => `${i}`), axisLabel: { fontSize: 9 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [{
      type: 'bar', barWidth: '55%', itemStyle: { color: '#6366f1', borderRadius: [3, 3, 0, 0] },
      data: d.hourly,
    }],
  }));

  // 4. 响应时间趋势
  mount('chart-resp', mkOption({
    legend: { top: 0, textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: d.resp_trend.dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 }, name: '秒' },
    series: [
      { name: '平均', type: 'line', smooth: true, data: d.resp_trend.avg, itemStyle: { color: '#3b82f6' } },
      { name: '最长', type: 'line', smooth: true, lineStyle: { type: 'dashed' }, data: d.resp_trend.max, itemStyle: { color: '#f59e0b' } },
      { name: '最短', type: 'line', smooth: true, lineStyle: { type: 'dashed' }, data: d.resp_trend.min, itemStyle: { color: '#10b981' } },
    ],
  }));

  // 5. 热门问题 TOP10
  const tq = d.top_questions.slice(0, 10);
  mount('chart-top10', mkOption({
    grid: { left: 150, right: 30, top: 10, bottom: 24 },
    xAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'category',
      inverse: true,
      axisLabel: { fontSize: 11 },
      data: tq.length ? tq.map((x) => x.q) : ['（暂无数据）'],
    },
    series: [{
      type: 'bar', barWidth: '55%', itemStyle: { color: '#3b82f6', borderRadius: [0, 4, 4, 0] },
      data: tq.length ? tq.map((x) => x.count) : [0],
    }],
  }));

  // 6. 意图分类
  const intentColors = ['#3b82f6', '#f59e0b', '#ef4444', '#10b981', '#a1a1aa'];
  mount('chart-intent', {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { formatter: '{b} {d}%', fontSize: 11 },
      data: (d.intent_split.length ? d.intent_split : [{ name: '暂无', value: 0 }])
        .map((x, i) => ({ ...x, itemStyle: { color: intentColors[i % intentColors.length] } })),
    }],
  });

  // 7-9. 三个率仪表
  const gauge = (id: string, value: number, color: string, textColor: string) =>
    mount(id, {
      series: [{
        type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100,
        progress: { show: true, width: 14, itemStyle: { color } },
        axisLine: { lineStyle: { width: 14, color: [[1, '#e5e7eb']] } },
        axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
        pointer: { show: false },
        detail: { valueAnimation: true, fontSize: 26, offsetCenter: [0, 0], formatter: '{value}%', color: textColor },
        data: [{ value }],
      }],
    });
  gauge('chart-ai-rate', d.kpis.ai_reply_rate, '#10b981', '#047857');
  gauge('chart-handoff-rate', d.kpis.handoff_rate, '#ef4444', '#b91c1c');
  gauge('chart-bargain-rate', d.kpis.bargain_resolve_rate, '#f59e0b', '#b45309');

  // 10. 新增客户
  mount('chart-new-cust', mkOption({
    xAxis: { type: 'category', data: d.new_cust_trend.dates, axisLabel: { fontSize: 9 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [{
      type: 'bar', barWidth: '50%', itemStyle: { color: '#10b981', borderRadius: [3, 3, 0, 0] },
      data: d.new_cust_trend.counts,
    }],
  }));

  // 11. 客户标签分布
  const tagColors = ['#10b981', '#f59e0b', '#3b82f6', '#ef4444'];
  mount('chart-tags', {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { formatter: '{b} {d}%', fontSize: 11 },
      data: (d.customer_tags.length ? d.customer_tags : [{ name: '暂无', value: 0 }])
        .map((x, i) => ({ ...x, itemStyle: { color: tagColors[i % tagColors.length] } })),
    }],
  });
}

function onResize() {
  for (const c of charts) c.resize();
}

onMounted(async () => {
  try {
    const d = await http<BoardData>('/admin/analytics/board?days=14');
    KPIS.value = kpisFrom(d);
    initCharts(d);
    demo.value = false;
  } catch (e) {
    errMsg.value = String(e);
    // 接口失败：保持演示数据兜底
  }
  window.addEventListener('resize', onResize);
});
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
  for (const c of charts) c.dispose();
  charts.length = 0;
});
</script>

<template>
  <div class="db">
    <div class="db-head">
      <span class="db-title">数据看板</span>
      <span v-if="demo" class="db-demo">演示数据{{ errMsg ? '（接口失败：' + errMsg.slice(0, 60) + '）' : ' · 加载中' }}</span>
      <span v-else class="db-live">真实统计</span>
    </div>

    <!-- KPI 卡片行 -->
    <div class="db-kpis">
      <div v-for="k in KPIS" :key="k.label" class="db-kpi">
        <div class="db-kpi-label">{{ k.label }}</div>
        <div class="db-kpi-value">{{ k.value }}</div>
        <div class="db-kpi-delta" :class="{ up: k.up }">{{ k.delta }}</div>
      </div>
    </div>

    <!-- 图表网格 -->
    <div class="db-grid">
      <div class="db-card db-span2"><div class="db-card-title">消息量趋势（近 14 天）</div><div id="chart-msg-trend" class="db-chart h280"></div></div>
      <div class="db-card"><div class="db-card-title">渠道分布</div><div id="chart-channel" class="db-chart h280"></div></div>

      <div class="db-card"><div class="db-card-title">峰值时段（24 小时消息量）</div><div id="chart-hours" class="db-chart h240"></div></div>
      <div class="db-card"><div class="db-card-title">响应时间趋势（秒）</div><div id="chart-resp" class="db-chart h240"></div></div>

      <div class="db-card db-span2"><div class="db-card-title">热门问题 TOP10</div><div id="chart-top10" class="db-chart h300"></div></div>

      <div class="db-card"><div class="db-card-title">意图分类</div><div id="chart-intent" class="db-chart h240"></div></div>
      <div class="db-card db-rates">
        <div class="db-rate-item"><div class="db-card-title">AI 自动回复率</div><div id="chart-ai-rate" class="db-chart h180"></div></div>
        <div class="db-rate-item"><div class="db-card-title">转人工率</div><div id="chart-handoff-rate" class="db-chart h180"></div></div>
        <div class="db-rate-item"><div class="db-card-title">议价解决率</div><div id="chart-bargain-rate" class="db-chart h180"></div></div>
      </div>

      <div class="db-card"><div class="db-card-title">新增客户（近 14 天）</div><div id="chart-new-cust" class="db-chart h240"></div></div>
      <div class="db-card"><div class="db-card-title">客户标签分布</div><div id="chart-tags" class="db-chart h240"></div></div>
    </div>
  </div>
</template>

<style scoped>
.db { display: flex; flex-direction: column; gap: 12px; }
.db-head { display: flex; align-items: center; gap: 12px; }
.db-title { font-size: 16px; font-weight: 700; color: #111827; }
.db-demo {
  font-size: 11px; color: #92400e; background: #fffbeb;
  border: 1px solid #fde68a; border-radius: 999px; padding: 2px 10px;
}
.db-live {
  font-size: 11px; color: #047857; background: #ecfdf5;
  border: 1px solid #a7f3d0; border-radius: 999px; padding: 2px 10px;
}

.db-kpis { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; }
.db-kpi {
  background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.db-kpi-label { font-size: 12px; color: #6b7280; }
.db-kpi-value { font-size: 24px; font-weight: 700; color: #111827; margin: 4px 0; }
.db-kpi-delta { font-size: 11px; color: #9ca3af; }
.db-kpi-delta.up { color: #059669; }

.db-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.db-card {
  background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.db-span2 { grid-column: span 2; }
.db-card-title { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 6px; }
.db-chart { width: 100%; }
.h280 { height: 280px; }
.h240 { height: 240px; }
.h300 { height: 300px; }
.h180 { height: 170px; }
.db-rates { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.db-rate-item { text-align: center; }
</style>
