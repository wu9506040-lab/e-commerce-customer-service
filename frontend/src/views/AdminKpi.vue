<script setup lang="ts">
/**
 * WP1 业务 KPI 大盘（客服域）
 * 公司里给运营/老板看的页面：自助解决率、转人工率、CSAT、拦截率、平均轮次 + 按日趋势。
 * 数据源：GET /api/admin/analytics/kpi 与 /kpi/trend（require_admin）。
 */
import { onMounted, ref } from 'vue';
import { getAdminKpi, getKpiTrend, type KpiSnapshot, type KpiTrendResp } from '../api';

const days = ref(14);
const kpi = ref<KpiSnapshot | null>(null);
const trend = ref<KpiTrendResp | null>(null);
const loading = ref(false);
const error = ref('');

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    kpi.value = await getAdminKpi(days.value);
    trend.value = await getKpiTrend(days.value);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="kpi">
    <header>
      <h1>业务 KPI 大盘</h1>
      <select v-model.number="days" @change="load">
        <option :value="7">近 7 天</option>
        <option :value="14">近 14 天</option>
        <option :value="30">近 30 天</option>
      </select>
      <button :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </header>
    <p v-if="error" class="error">{{ error }}</p>

    <template v-if="kpi">
      <section class="cards">
        <div class="card hl">
          <div class="label">AI 自助解决率</div>
          <div class="val">{{ pct(kpi.deflection_rate) }}</div>
          <div class="sub">未转人工会话 / 总会话 {{ kpi.sessions_total }}</div>
        </div>
        <div class="card">
          <div class="label">转人工率</div>
          <div class="val">{{ pct(kpi.handoff_rate) }}</div>
          <div class="sub">工单 {{ kpi.handoff.tickets_total }} 张（P0 {{ kpi.handoff.p0_total }}）</div>
        </div>
        <div class="card">
          <div class="label">CSAT 满意度</div>
          <div class="val">{{ pct(kpi.csat) }}</div>
          <div class="sub">👍{{ kpi.ratings.up }} / 👎{{ kpi.ratings.down }} · 覆盖率 {{ pct(kpi.rating_coverage) }}</div>
        </div>
        <div class="card">
          <div class="label">Guard 拦截率</div>
          <div class="val">{{ pct(kpi.guard_block_rate) }}</div>
          <div class="sub">拦截 {{ kpi.guard_blocked }} 次（零 LLM Token 消耗）</div>
        </div>
        <div class="card">
          <div class="label">平均解决轮次</div>
          <div class="val">{{ kpi.avg_rounds ?? '—' }}</div>
          <div class="sub">用户消息 {{ kpi.messages_user }} 条</div>
        </div>
      </section>

      <section v-if="trend" class="trend">
        <h2>按日趋势（会话量 vs 自助解决率）</h2>
        <table>
          <thead>
            <tr><th>日期</th><th>会话</th><th>转人工</th><th>自助解决率</th><th style="width:40%"></th></tr>
          </thead>
          <tbody>
            <tr v-for="d in trend.series" :key="d.date">
              <td>{{ d.date }}</td>
              <td>{{ d.sessions }}</td>
              <td>{{ d.handoffs }}</td>
              <td>{{ pct(d.deflection_rate) }}</td>
              <td>
                <div class="bar">
                  <div class="fill" :style="{ width: `${(d.deflection_rate ?? 0) * 100}%` }"></div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
      <p class="note">
        口径说明：CSAT 仅在评价覆盖的会话内计算，覆盖率低时不作趋势结论依据；
        指标定义见 backend/app/services/kpi_service.py 模块注释（含每个指标的公式与分母）。
      </p>
    </template>
  </div>
</template>

<style scoped>
.kpi { max-width: 980px; margin: 24px auto; padding: 0 16px; }
header { display: flex; align-items: center; gap: 12px; }
header h1 { font-size: 20px; margin: 0; flex: 1; }
select, header button { padding: 4px 10px; border: 1px solid #d9d9d9; border-radius: 6px; background: #fff; cursor: pointer; }
.error { color: #c0392b; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0; }
.card { border: 1px solid #eee; border-radius: 10px; padding: 14px; background: #fafbfe; }
.card.hl { background: #eef6ee; border-color: #bfdcbf; }
.label { color: #667; font-size: 12px; }
.val { font-size: 26px; font-weight: 700; margin: 4px 0; }
.sub { color: #99a; font-size: 12px; }
.trend h2 { font-size: 15px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #f0f0f0; }
.bar { background: #eef0f4; border-radius: 4px; height: 10px; }
.fill { background: #3ba55d; height: 10px; border-radius: 4px; min-width: 2px; }
.note { color: #99a; font-size: 12px; margin-top: 14px; }
</style>
