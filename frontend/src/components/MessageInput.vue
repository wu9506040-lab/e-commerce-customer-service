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
      placeholder="输入消息…  (Enter 发送 · Shift+Enter 换行)"
      :disabled="disabled"
      rows="3"
      @keydown="onKeydown"
    />
    <button :disabled="disabled || !text.trim()" @click="send">
      {{ disabled ? '生成中…' : '发 送' }}
    </button>
  </div>
</template>

<style scoped>
.input-bar {
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-5);
  border-top: var(--border);
  background: var(--gray-50);
}
textarea {
  flex: 1;
  padding: var(--sp-3);
  border: 1px solid var(--gray-300);
  font-family: var(--font-base);
  font-size: var(--fs-base);
  color: var(--gray-800);
  background: var(--gray-0);
  resize: vertical;
  outline: none;
  transition: border-color 0.15s;
  line-height: 1.5;
}
textarea::placeholder {
  color: var(--gray-400);
}
textarea:focus {
  border-color: var(--jd-red);
}
textarea:disabled {
  background: var(--gray-100);
  cursor: not-allowed;
}
button {
  padding: 0 var(--sp-5);
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-family: var(--font-base);
  font-size: var(--fs-base);
  font-weight: 500;
  letter-spacing: 2px;
  cursor: pointer;
  transition: background 0.15s;
  align-self: stretch;
  min-width: 88px;
}
button:hover:not(:disabled) {
  background: var(--jd-red-hover);
}
button:disabled {
  background: var(--gray-400);
  cursor: not-allowed;
}
</style>