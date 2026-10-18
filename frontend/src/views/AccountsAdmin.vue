<script setup lang="ts">
// V18.2 账号管理页：多账号连接状态 + 启停/重启
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { accountAction, listAccounts, type XianyuAccount } from '../api';

const accounts = ref<XianyuAccount[]>([]);
const error = ref('');
const busyId = ref('');
let timer: number | undefined;

function statusLabel(a: XianyuAccount): string {
  if (a.status === 'running') return '运行中';
  if (a.status === 'starting') return '连接中';
  if (a.status === 'error') return '异常';
  return '已停止';
}
function statusCls(a: XianyuAccount): string {
  return a.status; // running/starting/error/stopped
}

async function refresh() {
  try {
    const res = await listAccounts();
    accounts.value = res.accounts;
  } catch (e) {
    error.value = String(e);
  }
}

async function doAction(id: string, action: 'start' | 'stop' | 'restart') {
  busyId.value = id;
  error.value = '';
  try {
    await accountAction(id, action);
    setTimeout(refresh, 2000);
  } catch (e) {
    error.value = String(e);
  } finally {
    busyId.value = '';
  }
}

onMounted(() => {
  void refresh();
  timer = window.setInterval(refresh, 8000);
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="ac">
    <div class="ac-head">
      <span class="ac-title">账号管理</span>
      <span class="ac-hint">添加账号：把该账号的 cookie 导出到 backend/config/accounts.json 注册表后点「重载」</span>
    </div>

    <div v-if="error" class="ac-error">{{ error }}</div>

    <div class="ac-table">
      <div v-for="a in accounts" :key="a.id" class="ac-row">
        <div class="ac-id">
          <span class="ac-avatar">{{ a.id }}</span>
          <div>
            <div class="ac-name">{{ a.name }}</div>
            <div class="ac-sub">{{ a.id }}</div>
          </div>
        </div>
        <div class="ac-status" :class="statusCls(a)">
          <span class="ac-dot"></span>{{ statusLabel(a) }}
        </div>
        <div class="ac-err" :title="a.last_error">{{ a.last_error || '—' }}</div>
        <div class="ac-actions">
          <button class="cw-btn" :disabled="busyId === a.id" @click="doAction(a.id, 'restart')">重启</button>
          <button class="cw-btn" :disabled="busyId === a.id" @click="doAction(a.id, 'stop')">停止</button>
          <button class="cw-btn cw-btn-blue" :disabled="busyId === a.id" @click="doAction(a.id, 'start')">启动</button>
        </div>
      </div>
      <div v-if="!accounts.length" class="ac-empty">暂无账号（检查 daemon 是否运行 + accounts.json）</div>
    </div>

    <div class="ac-actions-bar">
      <button class="cw-btn cw-btn-blue" @click="accountAction('all', 'reload').then(refresh)">重载账号注册表</button>
    </div>
  </div>
</template>

<style scoped>
.ac { display: flex; flex-direction: column; gap: 12px; }
.ac-head { display: flex; align-items: baseline; gap: 12px; }
.ac-title { font-size: 16px; font-weight: 700; color: #111827; }
.ac-hint { font-size: 12px; color: #9ca3af; }
.ac-error { font-size: 12px; color: #dc2626; }
.ac-table { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; }
.ac-row {
  display: flex; align-items: center; gap: 16px; padding: 14px 16px;
  border-bottom: 1px solid #f3f4f6;
}
.ac-id { display: flex; align-items: center; gap: 10px; width: 220px; }
.ac-avatar {
  width: 38px; height: 38px; border-radius: 8px; background: #eef2ff; color: #4f46e5;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 13px;
}
.ac-name { font-size: 14px; font-weight: 600; color: #1f2937; }
.ac-sub { font-size: 11px; color: #9ca3af; }
.ac-status { width: 110px; font-size: 12px; display: flex; align-items: center; gap: 6px; }
.ac-dot { width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; }
.ac-status.running .ac-dot { background: #10b981; box-shadow: 0 0 6px #34d399; }
.ac-status.running { color: #047857; }
.ac-status.starting { color: #b45309; }
.ac-status.starting .ac-dot { background: #f59e0b; }
.ac-status.error { color: #b91c1c; }
.ac-status.error .ac-dot { background: #ef4444; }
.ac-err { flex: 1; font-size: 11px; color: #9ca3af; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ac-actions { display: flex; gap: 6px; }
.cw-btn {
  padding: 5px 12px; font-size: 12px; border-radius: 7px; cursor: pointer;
  border: 1px solid #e5e7eb; background: #fff; color: #374151;
}
.cw-btn:hover { background: #f3f4f6; }
.cw-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.cw-btn-blue { background: #3b82f6; color: #fff; border-color: #3b82f6; }
.cw-btn-blue:hover { background: #2563eb; }
.ac-actions-bar { display: flex; }
.ac-empty { padding: 32px; text-align: center; color: #9ca3af; font-size: 13px; }
</style>
