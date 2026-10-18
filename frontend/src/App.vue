<script setup lang="ts">
import { computed } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import AppNav from './components/AppNav.vue';

const route = useRoute();
// V18: /admin 走独立管理布局（深色侧边栏），隐藏商城导航
const isAdminArea = computed(() => route.path.startsWith('/admin'));
</script>

<template>
  <div class="app-root">
    <AppNav v-if="!isAdminArea" />
    <RouterView />
  </div>
</template>

<style>
/* 全局样式（Vite 默认全局） */
* {
  box-sizing: border-box;
}
html, body, #app {
  margin: 0;
  padding: 0;
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
  color: #333;
  background: #f7f8fa;
}
a {
  color: inherit;
  text-decoration: none;
}
button {
  font-family: inherit;
}
.app-root {
  display: flex;
  flex-direction: column;
  height: 100%;
}
</style>
