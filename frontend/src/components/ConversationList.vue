<script setup lang="ts">
/**
 * 会话列表（京东极简风）
 * - 时间分组：今天 / 昨天 / 本周 / 更早
 * - hover 显示删除按钮
 * - 顶部「清空全部」按钮（二次确认）
 * - 自动标题 vs 首条消息：title 优先，回退 last_message 截断
 */
import { computed } from 'vue';
import type { Conversation } from '../types';

const props = defineProps<{
  conversations: Conversation[];
  currentSessionId: string | null;
  loading: boolean;
}>();

const emit = defineEmits<{
  select: [sessionId: string];
  refresh: [];
  newChat: [];
  delete: [sessionId: string];
  clearAll: [];
}>();

// =============================================================
// 时间格式化 + 分组
// =============================================================
type GroupKey = 'today' | 'yesterday' | 'thisWeek' | 'earlier';

const GROUP_LABELS: Record<GroupKey, string> = {
  today: '今天',
  yesterday: '昨天',
  thisWeek: '本周',
  earlier: '更早',
};

const GROUP_ORDER: GroupKey[] = ['today', 'yesterday', 'thisWeek', 'earlier'];

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function getGroupKey(iso: string | null | undefined): GroupKey {
  if (!iso) return 'earlier';
  const d = new Date(iso);
  const now = new Date();
  const todayStart = startOfDay(now);
  const yesterdayStart = new Date(todayStart);
  yesterdayStart.setDate(yesterdayStart.getDate() - 1);
  const weekStart = new Date(todayStart);
  weekStart.setDate(weekStart.getDate() - 6);

  if (d >= todayStart) return 'today';
  if (d >= yesterdayStart) return 'yesterday';
  if (d >= weekStart) return 'thisWeek';
  return 'earlier';
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  const group = getGroupKey(iso);
  if (group === 'today' || group === 'yesterday') {
    return d.toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
  if (group === 'thisWeek') {
    const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return weekdays[d.getDay()];
  }
  return d.toLocaleDateString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
  });
}

// =============================================================
// 分组聚合（computed）
// =============================================================
const grouped = computed(() => {
  const groups: Record<GroupKey, Conversation[]> = {
    today: [],
    yesterday: [],
    thisWeek: [],
    earlier: [],
  };
  for (const c of props.conversations) {
    const key = getGroupKey(c.updated_at);
    groups[key].push(c);
  }
  return GROUP_ORDER
    .filter((k) => groups[k].length > 0)
    .map((k) => ({ key: k, label: GROUP_LABELS[k], items: groups[k] }));
});

// =============================================================
// 显示文本：title 优先 → last_message 截断
// =============================================================
function displayTitle(c: Conversation): string {
  if (c.title && c.title.trim()) return c.title;
  if (!c.last_message) return '(空会话)';
  return c.last_message.length > 30
    ? `${c.last_message.slice(0, 30)}…`
    : c.last_message;
}

function onDelete(e: MouseEvent, sessionId: string) {
  e.stopPropagation(); // 不触发 select
  emit('delete', sessionId);
}

function onClearAll() {
  if (!props.conversations.length) return;
  if (confirm(`确认删除全部 ${props.conversations.length} 个会话？此操作不可恢复。`)) {
    emit('clearAll');
  }
}
</script>

<template>
  <aside class="conv-list">
    <header>
      <h3>历史会话 ({{ conversations.length }})</h3>
      <button class="new-btn" title="新建会话" @click="emit('newChat')">+ 新会话</button>
    </header>

    <div v-if="conversations.length" class="toolbar">
      <button class="refresh-btn" :disabled="loading" @click="emit('refresh')">
        {{ loading ? '刷新中…' : '刷新列表' }}
      </button>
      <button class="clear-btn" @click="onClearAll">清空全部</button>
    </div>

    <div v-if="conversations.length" class="groups">
      <div v-for="g in grouped" :key="g.key" class="group">
        <div class="group-label">
          {{ g.label }}
          <span class="count">{{ g.items.length }}</span>
        </div>
        <ul>
          <li
            v-for="conv in g.items"
            :key="conv.session_id"
            :class="{ active: conv.session_id === currentSessionId }"
            @click="emit('select', conv.session_id)"
          >
            <div class="preview">{{ displayTitle(conv) }}</div>
            <div class="meta">
              <span>{{ conv.message_count }} 条</span>
              <span class="time">{{ formatTime(conv.updated_at) }}</span>
            </div>
            <button
              class="del-btn"
              title="删除会话"
              @click="onDelete($event, conv.session_id)"
            >×</button>
          </li>
        </ul>
      </div>
    </div>

    <div v-else class="empty">
      {{ loading ? '加载中…' : '暂无会话，点击「+ 新会话」开始' }}
    </div>
  </aside>
</template>

<style scoped>
.conv-list {
  width: var(--convlist-w);
  background: var(--gray-0);
  border-right: var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: var(--border);
  background: var(--gray-50);
}
header h3 {
  margin: 0;
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--gray-800);
}
.new-btn {
  padding: 4px 10px;
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-xs);
  font-family: var(--font-base);
  cursor: pointer;
  letter-spacing: 1px;
  transition: background 0.15s;
}
.new-btn:hover {
  background: var(--jd-red-hover);
}

.toolbar {
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4);
  background: var(--gray-0);
  border-bottom: var(--border);
}
.refresh-btn,
.clear-btn {
  flex: 1;
  padding: 4px 8px;
  background: var(--gray-0);
  border: 1px solid var(--gray-300);
  font-family: var(--font-base);
  font-size: var(--fs-xs);
  color: var(--gray-700);
  cursor: pointer;
  transition: all 0.15s;
}
.refresh-btn:hover:not(:disabled),
.clear-btn:hover {
  border-color: var(--jd-red);
  color: var(--jd-red);
}
.refresh-btn:disabled {
  color: var(--gray-400);
  cursor: not-allowed;
}
.clear-btn:hover {
  color: var(--jd-red-dark);
}

.groups {
  flex: 1;
  overflow-y: auto;
}
.group-label {
  padding: var(--sp-3) var(--sp-4) var(--sp-1);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--gray-500);
  letter-spacing: 0.5px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.group-label .count {
  font-size: var(--fs-xs);
  color: var(--gray-400);
  font-weight: 400;
}
ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
li {
  position: relative;
  padding: var(--sp-3) var(--sp-4);
  border-bottom: var(--border);
  cursor: pointer;
  transition: background 0.15s;
  background: var(--gray-0);
}
li:hover {
  background: var(--gray-50);
}
li.active {
  background: var(--jd-red-light);
  border-left: 3px solid var(--jd-red);
  padding-left: calc(var(--sp-4) - 3px);
}
.preview {
  font-size: var(--fs-sm);
  color: var(--gray-800);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-right: 22px;
  font-weight: 500;
}
li.active .preview {
  color: var(--jd-red);
}
.meta {
  display: flex;
  justify-content: space-between;
  font-size: var(--fs-xs);
  color: var(--gray-500);
}
.time {
  font-variant-numeric: tabular-nums;
}
.del-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 18px;
  height: 18px;
  background: var(--gray-0);
  border: 1px solid var(--gray-300);
  color: var(--gray-500);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
}
li:hover .del-btn {
  opacity: 1;
}
.del-btn:hover {
  background: var(--jd-red);
  border-color: var(--jd-red);
  color: #fff;
}

.empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-5);
  color: var(--gray-500);
  font-size: var(--fs-sm);
  text-align: center;
}
</style>