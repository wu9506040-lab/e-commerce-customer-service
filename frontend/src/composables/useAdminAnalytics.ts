import { computed, onMounted, ref } from 'vue';

import { getAdminAnalytics } from '../api';
import type { AdminAnalyticsResponse } from '../types';

function toDateInput(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function addDays(value: string, days: number): string {
  const [year, month, day] = value.split('-').map(Number);
  const result = new Date(year, month - 1, day);
  result.setDate(result.getDate() + days);
  return toDateInput(result);
}

export function useAdminAnalytics() {
  const today = new Date();
  const sevenDaysAgo = new Date(today);
  sevenDaysAgo.setDate(today.getDate() - 6);

  const startDate = ref(toDateInput(sevenDaysAgo));
  const endDate = ref(toDateInput(today));
  const data = ref<AdminAnalyticsResponse | null>(null);
  const loading = ref(false);
  const error = ref('');

  const totalConversations = computed(() =>
    data.value?.daily_activity.reduce((sum, item) => sum + item.conversations, 0) ?? 0,
  );
  const totalActiveUserDays = computed(() =>
    data.value?.daily_activity.reduce((sum, item) => sum + item.active_users, 0) ?? 0,
  );
  const maxConversations = computed(() =>
    Math.max(0, ...(data.value?.daily_activity.map((item) => item.conversations) ?? [])),
  );
  const handoffPriorities = computed(() => ['P0', 'P1', 'P2', 'unclassified'].map((priority) => ({
    priority,
    count: data.value?.handoffs.by_priority[priority] ?? 0,
  })));
  const handoffCategories = computed(() =>
    Object.entries(data.value?.handoffs.by_category ?? {})
      .sort((left, right) => right[1] - left[1]),
  );

  function formatPercent(value: number): string {
    return `${(value * 100).toFixed(1)}%`;
  }

  function formatTimestamp(value: string): string {
    return new Date(value).toLocaleString('zh-CN', { hour12: false });
  }

  function activityWidth(value: number): string {
    if (!maxConversations.value || value <= 0) return '0%';
    return `${Math.max(4, (value / maxConversations.value) * 100)}%`;
  }

  function handoffWidth(value: number): string {
    const total = data.value?.handoffs.total ?? 0;
    if (!total || value <= 0) return '0%';
    return `${Math.max(4, (value / total) * 100)}%`;
  }

  async function refresh() {
    if (startDate.value > endDate.value) {
      error.value = '开始日期不能晚于结束日期';
      return;
    }
    loading.value = true;
    error.value = '';
    try {
      data.value = await getAdminAnalytics(
        `${startDate.value}T00:00:00`,
        `${addDays(endDate.value, 1)}T00:00:00`,
      );
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '运营指标加载失败';
    } finally {
      loading.value = false;
    }
  }

  onMounted(refresh);

  return {
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
  };
}
