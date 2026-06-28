<script setup lang="ts">
/**
 * 会话列表（M9 增强）
 * - 时间分组：今天 / 昨天 / 本周 / 更早（折叠分组）
 * - hover 显示删除按钮（×）
 * - 顶部「清空全部」按钮（二次确认）
 * - 自动标题 vs 首条消息：title 优先，回退 last_message 前 30 字
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
  weekStart.setDate(weekStart.getDate() - 6); // 今天 + 前 6 天 = 本周 7 天

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
      <h3>会话 ({{ conversations.length }})</h3>
      <div class="actions">
        <button class="icon-btn" :disabled="loading" :title="loading ? '加载中' : '刷新'" @click="emit('refresh')">
          {{ loading ? '⟳' : '↻' }}
        </button>
        <button class="icon-btn primary" title="新建会话" @click="emit('newChat')">+ 新会话</button>
      </div>
    </header>

    <div v-if="conversations.length" class="toolbar">
      <button class="clear-btn" @click="onClearAll">🗑 清空全部</button>
    </div>

    <div v-if="conversations.length" class="groups">
      <div v-for="g in grouped" :key="g.key" class="group">
        <div class="group-label">{{ g.label }} <span class="count">{{ g.items.length }}</span></div>
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
  width: 280px;
  background: #fafafa;
  border-right: 1px solid #e0e0e0;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid #e0e0e0;
  background: white;
}
header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.actions {
  display: flex;
  gap: 6px;
}
.icon-btn {
  padding: 4px 10px;
  background: white;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}
.icon-btn.primary {
  background: #667eea;
  color: white;
  border-color: #667eea;
}
.icon-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.toolbar {
  padding: 8px 16px;
  background: white;
  border-bottom: 1px solid #f0f0f0;
}
.clear-btn {
  width: 100%;
  padding: 5px 10px;
  background: white;
  border: 1px solid #fecaca;
  border-radius: 4px;
  font-size: 12px;
  color: #b91c1c;
  cursor: pointer;
  transition: all 0.15s;
}
.clear-btn:hover {
  background: #fef2f2;
}

.groups {
  flex: 1;
  overflow-y: auto;
}
.group-label {
  padding: 10px 16px 4px;
  font-size: 11px;
  font-weight: 600;
  color: #9ca3af;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.group-label .count {
  font-size: 10px;
  color: #d1d5db;
  font-weight: 500;
}
ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
li {
  position: relative;
  padding: 10px 16px;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  transition: background 0.15s;
}
li:hover {
  background: #f0f0f0;
}
li.active {
  background: #e8edff;
  border-left: 3px solid #667eea;
  padding-left: 13px;
}
.preview {
  font-size: 13px;
  color: #333;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-right: 22px; /* 给删除按钮留位 */
}
.meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #999;
}
.time {
  font-variant-numeric: tabular-nums;
}
.del-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: white;
  border: 1px solid #e5e7eb;
  color: #9ca3af;
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
  background: #fef2f2;
  border-color: #fecaca;
  color: #b91c1c;
}

.empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  color: #999;
  font-size: 13px;
  text-align: center;
}
</style>
