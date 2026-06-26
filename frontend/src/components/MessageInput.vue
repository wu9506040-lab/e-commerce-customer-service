<script setup lang="ts">
import { ref } from 'vue';

const emit = defineEmits<{
  send: [text: string];
}>();

const props = defineProps<{
  disabled: boolean;
}>();

const text = ref('');

function send() {
  const t = text.value.trim();
  if (!t || props.disabled) return;
  emit('send', t);
  text.value = '';
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}
</script>

<template>
  <div class="input-bar">
    <textarea
      v-model="text"
      placeholder="输入消息... (Enter 发送，Shift+Enter 换行)"
      :disabled="disabled"
      rows="3"
      @keydown="onKeydown"
    />
    <button :disabled="disabled || !text.trim()" @click="send">
      {{ disabled ? '生成中…' : '发送' }}
    </button>
  </div>
</template>

<style scoped>
.input-bar {
  display: flex;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid #e0e0e0;
  background: #fafafa;
}
textarea {
  flex: 1;
  padding: 10px;
  border: 1px solid #ddd;
  border-radius: 6px;
  resize: vertical;
  outline: none;
  transition: border-color 0.2s;
}
textarea:focus {
  border-color: #667eea;
}
textarea:disabled {
  background: #f5f5f5;
  cursor: not-allowed;
}
button {
  padding: 0 24px;
  background: #667eea;
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.2s;
  align-self: stretch;
}
button:hover:not(:disabled) {
  background: #5568d3;
}
button:disabled {
  background: #ccc;
  cursor: not-allowed;
}
</style>