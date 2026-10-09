<script setup lang="ts">
// M15 人工介入坐席工作台：转人工工单排队 / 认领 / 回复结单
// 数据链路：GET /admin/handoff/tickets（P0 优先）→ 详情含会话最近 20 条 → resolve 注入会话
import { onBeforeUnmount, onMounted, ref } from 'vue';
import {
  closeHandoffTicket,
  getHandoffTicket,
  getHandoffTickets,
  resolveHandoffTicket,
  takeHandoffTicket,
  type HandoffTicketBrief,
  type HandoffTicketDetail,
} from '../api';

const statusTab = ref<'pending' | 'taken' | 'resolved' | 'closed'>('pending');
const tickets = ref<HandoffTicketBrief[]>([]);
const total = ref(0);
const current = ref<HandoffTicketDetail | null>(null);
const reply = ref('');
const busy = ref(false);
const error = ref('');
let timer: number | undefined;

const TABS = [
  { key: 'pending', label: '待接' },
  { key: 'taken', label: '已认领' },
  { key: 'resolved', label: '已结单' },
  { key: 'closed', label: '已关闭' },
] as const;

async function refreshList(): Promise<void> {
  try {
    const resp = await getHandoffTickets(statusTab.value);
    tickets.value = resp.items;
    total.value = resp.total;
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  }
}

async function openTicket(id: number): Promise<void> {
  try {
    current.value = await getHandoffTicket(id);
    reply.value = '';
    error.value = '';
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  }
}

async function act(fn: () => Promise<unknown>): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    await fn();
    if (current.value) await openTicket(current.value.id);
    await refreshList();
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  } finally {
    busy.value = false;
  }
}

function take(): void {
  if (!current.value) return;
  void act(() => takeHandoffTicket(current.value!.id));
}
function resolve(): void {
  if (!current.value || !reply.value.trim()) return;
  const text = reply.value.trim();
  void act(() => resolveHandoffTicket(current.value!.id, text));
}
function close(): void {
  if (!current.value) return;
  void act(() => closeHandoffTicket(current.value!.id));
}

function priorityClass(p?: string | null): string {
  return p === 'P0' ? 'pri pri-p0' : p === 'P1' ? 'pri pri-p1' : 'pri';
}

onMounted(() => {
  void refreshList();
  timer = window.setInterval(() => {
    if (statusTab.value === 'pending' && !current.value) void refreshList();
  }, 15000);
});
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<template>
  <div class="workbench">
    <header class="wb-head">
      <h1>人工介入工作台</h1>
      <div class="tabs">
        <button
          v-for="t in TABS"
          :key="t.key"
          :class="{ active: statusTab === t.key }"
          @click="statusTab = t.key; current = null; refreshList()"
        >{{ t.label }}</button>
      </div>
      <span class="total">共 {{ total }}</span>
    </header>
    <p v-if="error" class="error">{{ error }}</p>

    <div class="wb-body">
      <aside class="queue">
        <li v-for="t in tickets" :key="t.id" class="ticket"
            :class="{ cur: current?.id === t.id }" @click="openTicket(t.id)">
          <div class="row1">
            <span :class="priorityClass(t.priority)">{{ t.priority }}</span>
            <b>{{ t.ticket_no }}</b>
          </div>
          <div class="row2">用户 #{{ t.user_id }} · {{ t.category || t.reason }}</div>
          <div class="row3">{{ t.create_time?.slice(0, 16).replace('T', ' ') }}
            <span v-if="t.assignee"> · {{ t.assignee }}</span>
          </div>
        </li>
        <p v-if="!tickets.length" class="empty">队列为空</p>
      </aside>

      <section v-if="current" class="detail">
        <div class="d-head">
          <b>{{ current.ticket_no }}</b>
          <span :class="priorityClass(current.priority)">{{ current.priority }}</span>
          <span class="status">{{ current.status }}</span>
          <span v-if="current.matched_keyword" class="kw">触发词：{{ current.matched_keyword }}</span>
        </div>
        <div class="msgs">
          <div v-for="m in current.messages" :key="m.id"
               class="msg" :class="[m.role, { agent: m.is_human_agent }]">
            <span v-if="m.is_human_agent" class="badge">人工客服</span>
            {{ m.content }}
          </div>
          <p v-if="!current.messages.length" class="empty">（该工单未关联会话或无历史）</p>
        </div>
        <textarea v-model="reply" rows="3"
          placeholder="输入人工回复内容，结单后将注入用户会话…" :disabled="current.status === 'resolved' || current.status === 'closed'"></textarea>
        <div class="ops">
          <button v-if="current.status === 'pending'" :disabled="busy" @click="take">认领</button>
          <button v-if="current.status !== 'resolved' && current.status !== 'closed'"
                  class="primary" :disabled="busy || !reply.trim()" @click="resolve">回复并结单</button>
          <button v-if="current.status !== 'resolved' && current.status !== 'closed'"
                  :disabled="busy" @click="close">直接关闭</button>
        </div>
      </section>
      <section v-else class="detail placeholder">← 选择左侧工单查看会话上下文</section>
    </div>
  </div>
</template>

<style scoped>
.workbench { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
.wb-head { display: flex; align-items: center; gap: 16px; margin-bottom: 12px; }
.wb-head h1 { font-size: 20px; margin: 0; }
.tabs { display: flex; gap: 6px; }
.tabs button, .ops button { border: 1px solid #d9d9d9; background: #fff; border-radius: 6px; padding: 4px 12px; cursor: pointer; }
.tabs button.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
.total { color: #888; margin-left: auto; }
.error { color: #c0392b; }
.wb-body { display: flex; gap: 16px; min-height: 420px; }
.queue { list-style: none; width: 300px; margin: 0; padding: 0; border-right: 1px solid #eee; }
.ticket { padding: 10px; border-radius: 8px; cursor: pointer; }
.ticket:hover, .ticket.cur { background: #f3f7ff; }
.row1 { display: flex; gap: 8px; align-items: center; }
.row2, .row3 { color: #666; font-size: 12px; margin-top: 2px; }
.pri { font-size: 11px; padding: 1px 6px; border-radius: 4px; background: #eee; }
.pri-p0 { background: #e74c3c; color: #fff; }
.pri-p1 { background: #f39c12; color: #fff; }
.detail { flex: 1; display: flex; flex-direction: column; gap: 10px; }
.detail.placeholder { align-items: center; justify-content: center; color: #aaa; }
.d-head { display: flex; gap: 10px; align-items: center; }
.kw { color: #888; font-size: 12px; }
.msgs { flex: 1; overflow-y: auto; max-height: 380px; border: 1px solid #f0f0f0; border-radius: 8px; padding: 10px; }
.msg { margin: 6px 0; padding: 8px 10px; border-radius: 8px; max-width: 85%; white-space: pre-wrap; }
.msg.user { background: #eef3ff; }
.msg.assistant { background: #f4f4f5; }
.msg.agent { background: #fff7e6; border: 1px solid #ffe1a6; }
.badge { font-size: 11px; color: #b26a00; margin-right: 6px; font-weight: 700; }
.empty { color: #aaa; text-align: center; }
textarea { border: 1px solid #d9d9d9; border-radius: 8px; padding: 8px; resize: vertical; }
.ops { display: flex; gap: 8px; }
.ops .primary { background: #1f6feb; color: #fff; border-color: #1f6feb; }
.ops button:disabled { opacity: .5; cursor: not-allowed; }
</style>
