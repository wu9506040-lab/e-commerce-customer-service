<script setup lang="ts">
import type { Conversation } from '../types';

defineProps<{
  conversations: Conversation[];
  currentSessionId: string | null;
  loading: boolean;
}>();

const emit = defineEmits<{
  select: [sessionId: string];
  refresh: [];
  newChat: [];
}>();

function formatTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return d.toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
  return d.toLocaleDateString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
  });
}
</script>

<template>
  <aside class="conv-list">
    <header>
      <h3>会话 ({{ conversations.length }})</h3>
      <div class="actions">
        <button class="icon-btn" :disabled="loading" @click="emit('refresh')">
          {{ loading ? '⟳' : '↻' }}
        </button>
        <button class="icon-btn primary" @click="emit('newChat')">+ 新会话</button>
      </div>
    </header>
    <ul v-if="conversations.length">
      <li
        v-for="conv in conversations"
        :key="conv.session_id"
        :class="{ active: conv.session_id === currentSessionId }"
        @click="emit('select', conv.session_id)"
      >
        <div class="preview">
          {{ conv.last_message?.slice(0, 50) || '(空会话)' }}
        </div>
        <div class="meta">
          <span>{{ conv.message_count }} 条</span>
          <span>{{ formatTime(conv.updated_at) }}</span>
        </div>
      </li>
    </ul>
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
ul {
  flex: 1;
  overflow-y: auto;
}
li {
  padding: 12px 16px;
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
}
.preview {
  font-size: 14px;
  color: #333;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #999;
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