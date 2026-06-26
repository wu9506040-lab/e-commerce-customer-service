<script setup lang="ts">
import { ref } from 'vue';

const emit = defineEmits<{
  login: [username: string, password: string];
}>();

const username = ref('');
const password = ref('');
const loading = ref(false);
const error = ref('');

async function submit() {
  if (!username.value.trim() || !password.value) return;
  loading.value = true;
  error.value = '';
  try {
    emit('login', username.value, password.value);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录失败';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <h2>智能客服</h2>
      <p class="subtitle">RAG + 流式输出</p>
      <input
        v-model="username"
        placeholder="用户名"
        autocomplete="username"
        required
        :disabled="loading"
      />
      <input
        v-model="password"
        type="password"
        placeholder="密码"
        autocomplete="current-password"
        required
        :disabled="loading"
      />
      <button type="submit" :disabled="loading">
        {{ loading ? '登录中…' : '登录' }}
      </button>
      <p v-if="error" class="error">{{ error }}</p>
      <p class="hint">测试账号：convtest / ConvTest123</p>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.login-card {
  width: 360px;
  padding: 32px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
h2 {
  margin: 0;
  text-align: center;
}
.subtitle {
  text-align: center;
  color: #999;
  font-size: 12px;
  margin-bottom: 8px;
}
input {
  padding: 10px 12px;
  border: 1px solid #ddd;
  border-radius: 4px;
  outline: none;
  transition: border-color 0.2s;
}
input:focus {
  border-color: #667eea;
}
button {
  padding: 10px;
  background: #667eea;
  color: white;
  border: none;
  border-radius: 4px;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.2s;
}
button:hover:not(:disabled) {
  background: #5568d3;
}
button:disabled {
  background: #ccc;
  cursor: not-allowed;
}
.error {
  color: #e74c3c;
  font-size: 13px;
  text-align: center;
}
.hint {
  color: #999;
  font-size: 12px;
  text-align: center;
  margin-top: 8px;
}
</style>