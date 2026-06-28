<script setup lang="ts">
/**
 * 登录 / 注册页（M9 重构）
 * 双 tab：登录 / 注册，URL ?tab=register 可深链直跳注册
 */
import { ref, onMounted } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { login, register } from '../api';
import type { User } from '../types';

const router = useRouter();
const route = useRoute();

type Tab = 'login' | 'register';
const tab = ref<Tab>(
  (route.query.tab as Tab) === 'register' ? 'register' : 'login',
);

// 表单字段
const username = ref('');
const password = ref('');
const passwordConfirm = ref('');
const displayName = ref('');
const email = ref('');

const loading = ref(false);
const error = ref('');

function switchTab(t: Tab) {
  tab.value = t;
  error.value = '';
  // 同步 URL 便于深链
  router.replace({ query: { ...route.query, tab: t === 'register' ? 'register' : undefined } });
}

async function onLogin() {
  if (!username.value.trim() || !password.value) {
    error.value = '请填写用户名和密码';
    return;
  }
  loading.value = true;
  error.value = '';
  try {
    const user: User = await login(username.value.trim(), password.value);
    onAuthSuccess(user);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录失败';
  } finally {
    loading.value = false;
  }
}

async function onRegister() {
  if (!username.value.trim() || !password.value) {
    error.value = '请填写用户名和密码';
    return;
  }
  if (password.value.length < 6) {
    error.value = '密码至少 6 位';
    return;
  }
  if (password.value !== passwordConfirm.value) {
    error.value = '两次密码不一致';
    return;
  }
  loading.value = true;
  error.value = '';
  try {
    // 注册成功后再自动登录（后端 register 不发 cookie，所以走 login）
    await register({
      username: username.value.trim(),
      password: password.value,
      display_name: displayName.value.trim() || undefined,
      email: email.value.trim() || undefined,
    });
    const user: User = await login(username.value.trim(), password.value);
    onAuthSuccess(user);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '注册失败';
  } finally {
    loading.value = false;
  }
}

function onAuthSuccess(_user: User) {
  const redirect = (route.query.redirect as string) || '/shop';
  router.push(redirect);
}

onMounted(() => {
  // 防止已登录用户进 /login（路由守卫已处理，这里兜底）
});
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="brand">
        <span class="brand-icon">🤖</span>
        <h1>智选电商客服</h1>
        <p class="brand-sub">RAG + LangGraph · 多意图智能客服</p>
      </div>

      <div class="tabs">
        <button
          :class="['tab', { active: tab === 'login' }]"
          @click="switchTab('login')"
        >
          登录
        </button>
        <button
          :class="['tab', { active: tab === 'register' }]"
          @click="switchTab('register')"
        >
          注册
        </button>
      </div>

      <form v-if="tab === 'login'" @submit.prevent="onLogin" class="form">
        <label>
          <span class="label">用户名</span>
          <input
            v-model="username"
            placeholder="请输入用户名"
            autocomplete="username"
            required
            :disabled="loading"
          />
        </label>
        <label>
          <span class="label">密码</span>
          <input
            v-model="password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            required
            :disabled="loading"
          />
        </label>
        <button type="submit" class="btn-submit" :disabled="loading">
          {{ loading ? '登录中…' : '登录' }}
        </button>
        <p class="alt-action">
          还没账号？
          <a href="#" @click.prevent="switchTab('register')">立即注册</a>
        </p>
      </form>

      <form v-else @submit.prevent="onRegister" class="form">
        <label>
          <span class="label">用户名 <em>*</em></span>
          <input
            v-model="username"
            placeholder="3-20 位字母数字下划线"
            autocomplete="username"
            required
            :disabled="loading"
          />
        </label>
        <label>
          <span class="label">密码 <em>*</em></span>
          <input
            v-model="password"
            type="password"
            placeholder="至少 6 位"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
        </label>
        <label>
          <span class="label">确认密码 <em>*</em></span>
          <input
            v-model="passwordConfirm"
            type="password"
            placeholder="再输入一次"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
        </label>
        <label>
          <span class="label">昵称（可选）</span>
          <input v-model="displayName" placeholder="显示用" :disabled="loading" />
        </label>
        <label>
          <span class="label">邮箱（可选）</span>
          <input
            v-model="email"
            type="email"
            placeholder="用于找回密码"
            autocomplete="email"
            :disabled="loading"
          />
        </label>
        <button type="submit" class="btn-submit" :disabled="loading">
          {{ loading ? '注册中…' : '注册并登录' }}
        </button>
        <p class="alt-action">
          已有账号？
          <a href="#" @click.prevent="switchTab('login')">直接登录</a>
        </p>
      </form>

      <p v-if="error" class="error-msg">⚠️ {{ error }}</p>
    </div>

    <p class="footer-tip">© 2026 智选电商 · 智能客服系统</p>
  </div>
</template>

<style scoped>
.auth-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
}
.auth-card {
  width: 100%;
  max-width: 420px;
  background: white;
  border-radius: 16px;
  padding: 40px 36px 32px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.08);
}
.brand {
  text-align: center;
  margin-bottom: 28px;
}
.brand-icon {
  font-size: 40px;
  display: block;
  margin-bottom: 8px;
}
.brand h1 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 700;
  color: #1f2937;
}
.brand-sub {
  margin: 0;
  font-size: 13px;
  color: #9ca3af;
}
.tabs {
  display: flex;
  background: #f3f4f6;
  border-radius: 8px;
  padding: 4px;
  margin-bottom: 24px;
}
.tab {
  flex: 1;
  padding: 8px 0;
  font-size: 14px;
  font-weight: 500;
  background: none;
  border: none;
  border-radius: 6px;
  color: #6b7280;
  cursor: pointer;
  transition: all 0.15s;
}
.tab.active {
  background: white;
  color: #4f46e5;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
}
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.label {
  font-size: 13px;
  color: #4b5563;
  font-weight: 500;
}
.label em {
  color: #ef4444;
  font-style: normal;
}
input {
  padding: 10px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
input:focus {
  border-color: #667eea;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
}
input:disabled {
  background: #f9fafb;
  cursor: not-allowed;
}
.btn-submit {
  margin-top: 6px;
  padding: 11px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.15s;
}
.btn-submit:hover:not(:disabled) {
  opacity: 0.92;
}
.btn-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.alt-action {
  margin: 4px 0 0;
  text-align: center;
  font-size: 13px;
  color: #6b7280;
}
.alt-action a {
  color: #4f46e5;
  font-weight: 500;
}
.alt-action a:hover {
  text-decoration: underline;
}
.error-msg {
  margin: 12px 0 0;
  padding: 8px 12px;
  background: #fef2f2;
  color: #b91c1c;
  border-radius: 6px;
  font-size: 13px;
  text-align: center;
}
.footer-tip {
  margin-top: 24px;
  font-size: 12px;
  color: #9ca3af;
}
</style>
