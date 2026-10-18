<script setup lang="ts">
// V18.2 数据看板（BI 网格）— ECharts，当前为演示数据，接口就绪后替换 fetch
import * as echarts from 'echarts';
import { onBeforeUnmount, onMounted, ref } from 'vue';

const charts: echarts.ECharts[] = [];
const demo = ref(true);

function mkOption(cfg: Record<string, unknown>) {
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 42, right: 16, top: 34, bottom: 26 },
    ...cfg,
  };
}

const KPIS = [
  { label: '今日消息量', value: '1,284', delta: '+12.4%', up: true },
  { label: '平均响应时间', value: '11.2s', delta: '-18.3%', up: true },
  { label: 'AI 自动回复率', value: '93.6%', delta: '+1.8%', up: true },
  { label: '转人工率', value: '6.4%', delta: '-1.8%', up: true },
  { label: '议价成功率', value: '71.3%', delta: '+4.1%', up: true },
  { label: '今日新增客户', value: '37', delta: '+9', up: true },
];

function initCharts() {
  const mount = (id: string, option: echarts.EChartsCoreOption) => {
    const el = document.getElementById(id);
    if (!el) return;
    const c = echarts.init(el);
    c.setOption(option);
    charts.push(c);
  };

  // 1. 消息量趋势（近 14 天双线）
  const days = Array.from({ length: 14 }, (_, i) => {
    const d = new Date(2026, 9, 5 + i);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  });
  mount('chart-msg-trend', mkOption({
    legend: { top: 0, textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: days, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [
      { name: '闲鱼', type: 'line', smooth: true, data: [86, 102, 95, 130, 118, 142, 160, 138, 155, 172, 190, 168, 205, 232], itemStyle: { color: '#f97316' } },
      { name: '商城', type: 'line', smooth: true, data: [640, 590, 660, 610, 700, 655, 720, 690, 745, 710, 780, 760, 820, 854], itemStyle: { color: '#3b82f6' } },
    ],
  }));

  // 2. 渠道分布
  mount('chart-channel', {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { formatter: '{b}\n{d}%', fontSize: 11 },
      data: [
        { value: 854, name: '商城', itemStyle: { color: '#3b82f6' } },
        { value: 232, name: '闲鱼', itemStyle: { color: '#f97316' } },
      ],
    }],
  });

  // 3. 峰值时段（24h 柱状）
  mount('chart-hours', mkOption({
    xAxis: { type: 'category', data: Array.from({ length: 24 }, (_, i) => `${i}`), axisLabel: { fontSize: 9 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [{
      type: 'bar', barWidth: '55%', itemStyle: { color: '#6366f1', borderRadius: [3, 3, 0, 0] },
      data: [12, 8, 5, 3, 2, 4, 10, 28, 55, 88, 120, 145, 132, 118, 126, 140, 158, 170, 152, 130, 108, 76, 45, 24],
    }],
  }));

  // 4. 响应时间趋势
  mount('chart-resp', mkOption({
    legend: { top: 0, textStyle: { fontSize: 11 } },
    xAxis: { type: 'category', data: days, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 }, name: '秒' },
    series: [
      { name: '平均', type: 'line', smooth: true, data: [14.2, 13.8, 13.1, 12.6, 12.9, 12.1, 11.8, 11.5, 11.2, 10.8, 11.4, 10.9, 11.6, 11.2], itemStyle: { color: '#3b82f6' } },
      { name: '最长', type: 'line', smooth: true, lineStyle: { type: 'dashed' }, data: [38, 35, 33, 30, 32, 29, 27, 28, 26, 25, 27, 24, 26, 24], itemStyle: { color: '#f59e0b' } },
      { name: '最短', type: 'line', smooth: true, lineStyle: { type: 'dashed' }, data: [4.1, 3.8, 3.5, 3.6, 3.4, 3.2, 3.3, 3.1, 3.0, 2.9, 3.1, 2.8, 3.0, 2.8], itemStyle: { color: '#10b981' } },
    ],
  }));

  // 5. 热门问题 TOP10
  mount('chart-top10', mkOption({
    grid: { left: 150, right: 30, top: 10, bottom: 24 },
    xAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'category',
      inverse: true,
      axisLabel: { fontSize: 11 },
      data: ['能便宜点吗', '在吗', '能退款吗', '容量多大', '正品吗', '多久发货', '能带上飞机吗', '怎么收费', '质量怎么样', '怎么用'],
    },
    series: [{
      type: 'bar', barWidth: '55%', itemStyle: { color: '#3b82f6', borderRadius: [0, 4, 4, 0] },
      data: [96, 81, 64, 52, 47, 43, 38, 31, 27, 22],
    }],
  }));

  // 6. 意图分类
  mount('chart-intent', {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { formatter: '{b} {d}%', fontSize: 11 },
      data: [
        { value: 520, name: '售前咨询', itemStyle: { color: '#3b82f6' } },
        { value: 310, name: '议价', itemStyle: { color: '#f59e0b' } },
        { value: 180, name: '售后/退款', itemStyle: { color: '#ef4444' } },
        { value: 96, name: '其他', itemStyle: { color: '#a1a1aa' } },
      ],
    }],
  });

  // 7. AI 回复率仪表
  mount('chart-ai-rate', {
    series: [{
      type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100,
      progress: { show: true, width: 14, itemStyle: { color: '#10b981' } },
      axisLine: { lineStyle: { width: 14, color: [[1, '#e5e7eb']] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      pointer: { show: false },
      detail: { valueAnimation: true, fontSize: 26, offsetCenter: [0, 0], formatter: '{value}%', color: '#047857' },
      data: [{ value: 93.6 }],
    }],
  });

  // 8. 转人工率仪表
  mount('chart-handoff-rate', {
    series: [{
      type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100,
      progress: { show: true, width: 14, itemStyle: { color: '#ef4444' } },
      axisLine: { lineStyle: { width: 14, color: [[1, '#e5e7eb']] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      pointer: { show: false },
      detail: { valueAnimation: true, fontSize: 26, offsetCenter: [0, 0], formatter: '{value}%', color: '#b91c1c' },
      data: [{ value: 6.4 }],
    }],
  });

  // 9. 议价成功率仪表
  mount('chart-bargain-rate', {
    series: [{
      type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100,
      progress: { show: true, width: 14, itemStyle: { color: '#f59e0b' } },
      axisLine: { lineStyle: { width: 14, color: [[1, '#e5e7eb']] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      pointer: { show: false },
      detail: { valueAnimation: true, fontSize: 26, offsetCenter: [0, 0], formatter: '{value}%', color: '#b45309' },
      data: [{ value: 71.3 }],
    }],
  });

  // 10. 新增客户（近 14 天柱状）
  mount('chart-new-cust', mkOption({
    xAxis: { type: 'category', data: days, axisLabel: { fontSize: 9 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    series: [{
      type: 'bar', barWidth: '50%', itemStyle: { color: '#10b981', borderRadius: [3, 3, 0, 0] },
      data: [18, 22, 19, 25, 21, 28, 31, 26, 30, 33, 29, 35, 32, 37],
    }],
  }));

  // 11. 客户标签分布
  mount('chart-tags', {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { formatter: '{b} {d}%', fontSize: 11 },
      data: [
        { value: 45, name: '老客', itemStyle: { color: '#10b981' } },
        { value: 38, name: '议价中', itemStyle: { color: '#f59e0b' } },
        { value: 26, name: '售前意向', itemStyle: { color: '#3b82f6' } },
        { value: 12, name: '售后中', itemStyle: { color: '#ef4444' } },
      ],
    }],
  });
}

function onResize() {
  for (const c of charts) c.resize();
}

onMounted(() => {
  initCharts();
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
      <span v-if="demo" class="db-demo">演示数据 · 接口就绪后自动切换真实统计</span>
    </div>

    <!-- KPI 卡片行 -->
    <div class="db-kpis">
      <div v-for="k in KPIS" :key="k.label" class="db-kpi">
        <div class="db-kpi-label">{{ k.label }}</div>
        <div class="db-kpi-value">{{ k.value }}</div>
        <div class="db-kpi-delta" :class="{ up: k.up }">{{ k.delta }} 较上周</div>
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
        <div class="db-rate-item"><div class="db-card-title">议价成功率</div><div id="chart-bargain-rate" class="db-chart h180"></div></div>
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
