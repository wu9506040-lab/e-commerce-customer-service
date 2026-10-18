<script setup lang="ts">
// V18.2: 管理后台专属登录页（与商城登录完全隔离）
// 深色风格与 AdminLayout 一致；登录成功 → /admin/xianyu
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { isAuthed } from '../api';

const router = useRouter();
const route = useRoute();

const username = ref('');
const password = ref('');
const error = ref('');
const busy = ref(false);

async function onLogin() {
  if (!username.value.trim() || !password.value || busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    const body = new URLSearchParams({
      username: username.value.trim(),
      password: password.value,
    });
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const b = await res.json();
        detail = b.detail ?? detail;
      } catch { /* ignore */ }
      error.value = typeof detail === 'string' ? detail : '用户名或密码错误';
      return;
    }
    const d = await res.json();
    if (d?.user?.role !== 'admin') {
      error.value = '该账号不是管理员，请使用管理员账号登录';
      return;
    }
    isAuthed.value = true;  // ⚠️ 守卫读这个 ref；不走 api.ts login() 就必须手动置位
    router.push((route.query.redirect as string) || '/admin/xianyu');
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="alog">
    <div class="alog-card">
      <div class="alog-brand">
        <span class="alog-logo">⚡</span>
        <span>智能客服管理台</span>
      </div>
      <div class="alog-sub">管理员登录 · 闲鱼人工后台</div>

      <form class="alog-form" @submit.prevent="onLogin">
        <input
          v-model="username"
          placeholder="管理员账号"
          autocomplete="username"
        />
        <input
          v-model="password"
          type="password"
          placeholder="密码"
          autocomplete="current-password"
        />
        <button type="submit" :disabled="busy">
          {{ busy ? '登录中…' : '登 录' }}
        </button>
        <div v-if="error" class="alog-error">{{ error }}</div>
      </form>

      <div class="alog-footer">
        <a href="/">← 返回商城首页</a>
      </div>
    </div>
  </div>
</template>

<style scoped>
.alog {
  min-height: 100vh; background: #001529;
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.alog-card {
  width: 380px; background: #fff; border-radius: 12px; padding: 36px 32px;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.35);
}
.alog-brand {
  display: flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 700;
  color: #001529; justify-content: center;
}
.alog-logo { font-size: 20px; }
.alog-sub {
  text-align: center; font-size: 12px; color: #71717a; margin: 8px 0 26px;
}
.alog-form { display: flex; flex-direction: column; gap: 14px; }
.alog-form input {
  padding: 11px 14px; font-size: 14px; border: 1px solid #d4d4d8;
  border-radius: 8px; outline: none; transition: border-color 0.2s;
}
.alog-form input:focus { border-color: #1677ff; }
.alog-form button {
  padding: 11px 0; font-size: 15px; font-weight: 600; color: #fff;
  background: #1677ff; border: none; border-radius: 8px; cursor: pointer;
  transition: background 0.2s;
}
.alog-form button:hover { background: #0958d9; }
.alog-form button:disabled { opacity: 0.6; cursor: not-allowed; }
.alog-error {
  font-size: 13px; color: #dc2626; background: #fef2f2;
  border: 1px solid #fecaca; border-radius: 6px; padding: 8px 12px;
}
.alog-footer { text-align: center; margin-top: 22px; }
.alog-footer a { font-size: 12px; color: #a1a1aa; }
.alog-footer a:hover { color: #1677ff; }
</style>
