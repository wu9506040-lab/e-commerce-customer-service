<script setup lang="ts">
import { ref, onMounted } from 'vue';
import LoginForm from './components/LoginForm.vue';
import ChatPage from './components/ChatPage.vue';
import { getMe, login as apiLogin, logout as apiLogout } from './api';
import type { User } from './types';

const user = ref<User | null>(null);
const initializing = ref(true);

onMounted(async () => {
  try {
    user.value = await getMe();
  } catch (e) {
    console.error('getMe failed:', e);
  } finally {
    initializing.value = false;
  }
});

async function onLogin(username: string, password: string) {
  user.value = await apiLogin(username, password);
}

async function onLogout() {
  await apiLogout();
  user.value = null;
}
</script>

<template>
  <div class="app-root">
    <div v-if="initializing" class="splash">加载中…</div>
    <LoginForm v-else-if="!user" @login="onLogin" />
    <ChatPage v-else :user="user" @logout="onLogout" />
  </div>
</template>

<style scoped>
.app-root {
  height: 100%;
}
.splash {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #999;
}
</style>