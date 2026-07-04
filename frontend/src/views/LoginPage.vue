<script setup lang="ts">
/**
 * 登录 / 注册页（M9 重构）
 * 双 tab：登录 / 注册，URL ?tab=register 可深链直跳注册
 */
import { ref, onMounted } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { login, register, demoLogin } from '../api';
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

/**
 * 一键 demo 体验（M13 cloud）
 * 不需要填任何信息，后端自动建账号 + 登录
 */
async function onDemoLogin() {
  loading.value = true;
  error.value = '';
  try {
    const user: User = await demoLogin();
    onAuthSuccess(user);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '体验失败，请稍后重试';
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
        <div class="brand-mark">智</div>
        <h1>智选客服</h1>
        <p class="brand-sub">RAG + LangGraph · 多意图智能客服</p>
      </div>

      <div class="tabs">
        <button
          :class="['tab', { active: tab === 'login' }]"
          @click="switchTab('login')"
        >
          账号登录
        </button>
        <button
          :class="['tab', { active: tab === 'register' }]"
          @click="switchTab('register')"
        >
          新用户注册
        </button>
      </div>

      <form v-if="tab === 'login'" @submit.prevent="onLogin" class="form">
        <div class="field">
          <label>用户名</label>
          <input
            v-model="username"
            placeholder="请输入用户名"
            autocomplete="username"
            required
            :disabled="loading"
          />
        </div>
        <div class="field">
          <label>密码</label>
          <input
            v-model="password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            required
            :disabled="loading"
          />
        </div>
        <button type="submit" class="btn-submit" :disabled="loading">
          {{ loading ? '登录中…' : '登 录' }}
        </button>

        <!-- M13 cloud：一键体验按钮（公开 demo 站点） -->
        <div class="demo-divider">
          <span>或</span>
        </div>
        <button
          type="button"
          class="btn-demo"
          :disabled="loading"
          @click="onDemoLogin"
        >
          立即体验 demo 账号 →
        </button>

        <p class="alt-action">
          还没有账号？
          <a href="#" @click.prevent="switchTab('register')">立即注册</a>
        </p>
      </form>

      <form v-else @submit.prevent="onRegister" class="form">
        <div class="field">
          <label>用户名 <em>*</em></label>
          <input
            v-model="username"
            placeholder="3-20 位字母数字下划线"
            autocomplete="username"
            required
            :disabled="loading"
          />
        </div>
        <div class="field">
          <label>密码 <em>*</em></label>
          <input
            v-model="password"
            type="password"
            placeholder="至少 6 位"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
        </div>
        <div class="field">
          <label>确认密码 <em>*</em></label>
          <input
            v-model="passwordConfirm"
            type="password"
            placeholder="再输入一次"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
        </div>
        <div class="field">
          <label>昵称（可选）</label>
          <input v-model="displayName" placeholder="显示用" :disabled="loading" />
        </div>
        <div class="field">
          <label>邮箱（可选）</label>
          <input
            v-model="email"
            type="email"
            placeholder="用于找回密码"
            autocomplete="email"
            :disabled="loading"
          />
        </div>
        <button type="submit" class="btn-submit" :disabled="loading">
          {{ loading ? '注册中…' : '注册并登录' }}
        </button>
        <p class="alt-action">
          已有账号？
          <a href="#" @click.prevent="switchTab('login')">直接登录</a>
        </p>
      </form>

      <p v-if="error" class="error-msg">{{ error }}</p>
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
  padding: var(--sp-10) var(--sp-5);
  background: var(--gray-50);
}
.auth-card {
  width: 100%;
  max-width: 380px;
  background: var(--gray-0);
  border: var(--border);
  padding: var(--sp-8) var(--sp-8) var(--sp-6);
}

/* Brand */
.brand {
  text-align: center;
  margin-bottom: var(--sp-6);
  padding-bottom: var(--sp-5);
  border-bottom: var(--border);
}
.brand-mark {
  width: 48px;
  height: 48px;
  background: var(--jd-red);
  color: #fff;
  font-size: 26px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto var(--sp-2);
}
.brand h1 {
  margin: 0 0 var(--sp-1);
  font-size: var(--fs-lg);
  font-weight: 700;
  color: var(--gray-800);
}
.brand-sub {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--gray-500);
}

/* Tabs */
.tabs {
  display: flex;
  margin-bottom: var(--sp-5);
  border-bottom: var(--border);
}
.tab {
  flex: 1;
  padding: var(--sp-2) 0;
  font-size: var(--fs-base);
  background: none;
  border: none;
  color: var(--gray-600);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: all 0.15s;
}
.tab.active {
  color: var(--jd-red);
  border-bottom-color: var(--jd-red);
  font-weight: 500;
}
.tab:hover:not(.active) {
  color: var(--gray-800);
}

/* Form */
.form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.field label {
  font-size: var(--fs-sm);
  color: var(--gray-700);
}
.field label em {
  color: var(--jd-red);
  font-style: normal;
  margin-left: 2px;
}
.field input {
  padding: var(--sp-3);
  border: 1px solid var(--gray-300);
  font-size: var(--fs-base);
  outline: none;
  transition: border-color 0.15s;
  background: var(--gray-0);
}
.field input:focus {
  border-color: var(--jd-red);
}
.field input:disabled {
  background: var(--gray-100);
  cursor: not-allowed;
}

/* Submit */
.btn-submit {
  margin-top: var(--sp-2);
  padding: var(--sp-3);
  background: var(--jd-red);
  color: #fff;
  border: none;
  font-size: var(--fs-md);
  font-weight: 500;
  cursor: pointer;
  letter-spacing: 4px;
}
.btn-submit:hover:not(:disabled) {
  background: var(--jd-red-hover);
}
.btn-submit:disabled {
  background: var(--gray-400);
  cursor: not-allowed;
}

/* Demo 一键体验按钮（M13 cloud） */
.demo-divider {
  display: flex;
  align-items: center;
  margin: var(--sp-3) 0 var(--sp-2);
  color: var(--gray-400);
  font-size: var(--fs-xs);
}
.demo-divider::before,
.demo-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--gray-200);
}
.demo-divider span {
  padding: 0 var(--sp-3);
}

.btn-demo {
  padding: var(--sp-3);
  background: var(--gray-0);
  color: var(--jd-red);
  border: 1px solid var(--jd-red);
  font-size: var(--fs-base);
  font-weight: 500;
  cursor: pointer;
  letter-spacing: 1px;
  transition: all 0.15s;
}
.btn-demo:hover:not(:disabled) {
  background: var(--jd-red-light);
}
.btn-demo:disabled {
  border-color: var(--gray-400);
  color: var(--gray-400);
  cursor: not-allowed;
}

/* Alt action */
.alt-action {
  margin: var(--sp-2) 0 0;
  text-align: center;
  font-size: var(--fs-sm);
  color: var(--gray-600);
}
.alt-action a {
  color: var(--jd-red);
  font-weight: 500;
  cursor: pointer;
}
.alt-action a:hover {
  text-decoration: underline;
}

/* Error */
.error-msg {
  margin: var(--sp-3) 0 0;
  padding: var(--sp-2) var(--sp-3);
  background: var(--jd-red-light);
  color: var(--jd-red-dark);
  border: 1px solid var(--jd-red);
  font-size: var(--fs-sm);
  text-align: center;
}

.footer-tip {
  margin-top: var(--sp-5);
  font-size: var(--fs-xs);
  color: var(--gray-500);
}
</style>
