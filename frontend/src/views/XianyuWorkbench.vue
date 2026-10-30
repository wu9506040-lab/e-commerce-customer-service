<script setup lang="ts">
// V18.2 全渠道人工客服工作台 — 设计对齐 xianyu-auto-reply chat-new（Tailwind 风格）
// 布局：左（渠道+会话列表） · 中（对话窗口） · 右（客户信息+快捷短语）
// 消息区分：买家=灰底左侧 / AI=蓝底右侧 / 人工=靛蓝底右侧，气泡上方标注发送者+时间
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  getXianyuMessages,
  getOpenTickets,
  listXianyuConversations,
  releaseXianyu,
  sendXianyuMessage,
  takeoverXianyu,
  type XianyuConversation,
  type XianyuMessage,
  type XianyuTicket,
} from '../api';

interface Conv extends XianyuConversation {
  channel: 'xianyu' | 'mall';
  name: string;
}

const channelTab = ref<'all' | 'xianyu' | 'mall'>('all');
const search = ref('');
const conversations = ref<Conv[]>([]);
const currentChannel = ref<'xianyu' | 'mall'>('xianyu');
const currentCid = ref('');
const currentName = ref('');
const messages = ref<XianyuMessage[]>([]);
const draft = ref('');
const busy = ref(false);
const error = ref('');
const takeoverMap = ref<Record<string, boolean>>({});
const chatBox = ref<HTMLElement | null>(null);
// V18.2.2: 工单通知（平台退款等自动建单 → 顶栏红条提醒）
const ticketCount = ref(0);
const p0Count = ref(0);
const latestTicket = ref('');
let ticketTimer: number | undefined;
let listTimer: number | undefined;
let msgTimer: number | undefined;
const openTickets = ref<XianyuTicket[]>([]);
const notifyBusy = ref(false);
const notifyResult = ref('');

const quickPhrases = [
  '在的~有什么可以帮您？',
  '今天 17:00 前拍下当天发货～',
  '全场包邮，发的顺丰',
  '支持 7 天无理由退换',
  '拍下后我改价，请稍等',
  '感谢支持，欢迎再次光临～',
];
function insertPhrase(p: string) {
  draft.value = p;
}

const filtered = computed(() => {
  const kw = search.value.trim().toLowerCase();
  return conversations.value.filter((c) => {
    if (channelTab.value !== 'all' && c.channel !== channelTab.value) return false;
    if (kw && !(c.name.toLowerCase().includes(kw) || (c.last_text ?? '').toLowerCase().includes(kw)))
      return false;
    return true;
  });
});

const current = computed(() =>
  conversations.value.find(
    (c) => c.channel === currentChannel.value && c.cid === currentCid.value,
  ) ?? null,
);
const isTakeover = computed(() => !!takeoverMap.value[currentCid.value]);
const isXianyu = computed(() => currentChannel.value === 'xianyu');

// V18.2.3: 会话状态标签（待处理红 > 人工接管蓝 > AI 回复中绿）
const pendingTicketCids = computed(() => {
  const set = new Set<string>();
  for (const t of openTickets.value) {
    const sid = t.session_id || '';
    if (sid.startsWith('xianyu-')) set.add(sid.slice(7));
  }
  return set;
});
function statusOf(c: Conv): { cls: string; label: string } {
  if (pendingTicketCids.value.has(c.cid)) return { cls: 'red', label: '待处理' };
  if (takeoverMap.value[c.cid]) return { cls: 'blue', label: '人工接管' };
  return { cls: 'green', label: 'AI 回复中' };
}
// 议价中判定：最近一条买家消息含价格/便宜类词
const isBargaining = computed(() => {
  const t = current.value?.last_text ?? '';
  return /便宜|价格|少点|优惠|砍价|改价/.test(t);
});
// 正在咨询的商品（当前会话归属商品）
const currentItem = computed(() =>
  current.value?.channel === 'xianyu'
    ? { name: '智选 PB1 充电宝 20000mAh', desc: '65W 快充 · 74Wh 民航可带 · 95 新' }
    : { name: '（商城会话）', desc: '' },
);
// 历史会话预览（最近 6 条，取自当前消息列表）
const historyPreview = computed(() =>
  messages.value.slice(-6).map((m) => `${m.role === 'user' ? '买家' : isHuman(m) ? '人工' : 'AI'}: ${m.content.slice(0, 24)}`),
);

async function refreshTickets() {
  try {
    const res = await getOpenTickets();
    openTickets.value = res.tickets;
    ticketCount.value = res.open_count;
    p0Count.value = res.p0_count;
    latestTicket.value = res.tickets[0]?.summary_text ?? '';
  } catch { /* 通知失败不打扰 */ }
}

async function onTestNotify() {
  notifyBusy.value = true;
  notifyResult.value = '';
  try {
    const res = await fetch('/api/admin/xianyu/notify/test', { method: 'POST', credentials: 'include' });
    const d = await res.json();
    if (d.results?.length) {
      notifyResult.value = d.results.map((r: { type: string; ok: boolean; detail?: string }) => r.type + (r.ok ? ' ✅' : ' ❌ ' + (r.detail ?? '').slice(0, 40))).join(' | ');
    } else {
      notifyResult.value = '无结果';
    }
  } catch (e) {
    notifyResult.value = String(e);
  } finally {
    notifyBusy.value = false;
  }
}

async function refreshList() {
  try {
    const res = await listXianyuConversations(60);
    conversations.value = res.conversations as Conv[];
    const map = { ...takeoverMap.value };
    for (const c of res.conversations) if (c.channel === 'xianyu') map[c.cid] = !!c.takeover;
    takeoverMap.value = map;
  } catch (e) {
    error.value = String(e);
  }
}

async function refreshMessages() {
  if (!currentCid.value) return;
  try {
    const res = await getXianyuMessages(currentChannel.value, currentCid.value, 100);
    const prevLen = messages.value.length;
    const prevLast = messages.value[messages.value.length - 1]?.id;
    const newLast = res.messages[res.messages.length - 1]?.id;
    messages.value = res.messages;
    if (res.messages.length !== prevLen || newLast !== prevLast) {
      void nextTick(() => chatBox.value?.scrollTo({ top: chatBox.value.scrollHeight }));
    }
  } catch (e) {
    error.value = String(e);
  }
}

function select(c: Conv) {
  currentChannel.value = c.channel;
  currentCid.value = c.cid;
  currentName.value = c.name;
  messages.value = [];
  error.value = '';
  void refreshMessages();
}

async function onSend() {
  const text = draft.value.trim();
  if (!text || busy.value || !currentCid.value) return;
  busy.value = true;
  error.value = '';
  try {
    await sendXianyuMessage(currentCid.value, text, '', currentChannel.value || 'xianyu');
    draft.value = '';
    setTimeout(refreshMessages, 600);
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}

async function onToggleTakeover() {
  if (!currentCid.value || !isXianyu.value) return;
  try {
    if (isTakeover.value) {
      await releaseXianyu(currentCid.value);
      takeoverMap.value[currentCid.value] = false;
    } else {
      await takeoverXianyu(currentCid.value);
      takeoverMap.value[currentCid.value] = true;
    }
  } catch (e) {
    error.value = String(e);
  }
}

function senderLabel(m: XianyuMessage): string {
  if (m.role === 'user') return '买家';
  const snap = m.intent_snapshot as unknown as
    | { human?: boolean; agent?: string }
    | undefined;
  return snap?.human ? `人工 · ${snap.agent ?? '坐席'}` : 'AI 客服';
}
function isHuman(m: XianyuMessage): boolean {
  const snap = m.intent_snapshot as unknown as { human?: boolean } | undefined;
  return m.role === 'assistant' && snap?.human === true;
}
function fmtTime(t: string | null): string {
  if (!t) return '';
  return t.replace('T', ' ').slice(5, 16);
}

watch(currentCid, () => {
  void nextTick(() => chatBox.value?.scrollTo({ top: chatBox.value.scrollHeight }));
});

onMounted(() => {
  void refreshList();
  listTimer = window.setInterval(refreshList, 5000);
  msgTimer = window.setInterval(refreshMessages, 3000);
  ticketTimer = window.setInterval(refreshTickets, 10000);
});
onBeforeUnmount(() => {
  if (listTimer) clearInterval(listTimer);
  if (msgTimer) clearInterval(msgTimer);
  if (ticketTimer) clearInterval(ticketTimer);
});
</script>

<template>
  <div class="cw-wrap">
    <!-- 工单通知条 -->
    <div v-if="ticketCount > 0" class="cw-ticket-bar">
      🔔 <b>{{ ticketCount }}</b> 个待处理工单<template v-if="p0Count > 0">（<b class="p0">{{ p0Count }} 个 P0</b>）</template>
      —— 最新：{{ latestTicket || '（无摘要）' }}
      <a href="/admin/handoff" class="cw-ticket-link">去处理 →</a>
    </div>
    <div v-else class="cw-ticket-bar off">
      🔕 暂无待处理工单
      <button class="cw-btn" style="padding: 3px 10px; font-size: 12px; margin-left: auto" :disabled="notifyBusy" @click="onTestNotify">
        {{ notifyBusy ? '发送中…' : '测试外发通知' }}
      </button>
      <span v-if="notifyResult" class="cw-notify-result">{{ notifyResult }}</span>
      <span class="cw-ticket-link" title="编辑 backend/config/notify.json 后立即生效">通知配置说明</span>
    </div>
    <div class="cw">
    <!-- 左栏：会话列表（chat-new 会话列表样式：白卡片 + 蓝色选中态） -->
    <aside class="cw-card cw-w-list">
      <div class="cw-head">
        <span class="cw-head-title">会话列表</span>
        <span class="cw-head-count">{{ filtered.length }}</span>
      </div>
      <div class="cw-search">
        <input v-model="search" placeholder="搜索会话…" />
      </div>
      <div class="cw-tabs">
        <button :class="{ on: channelTab === 'all' }" @click="channelTab = 'all'">全部</button>
        <button :class="{ on: channelTab === 'xianyu' }" @click="channelTab = 'xianyu'">闲鱼</button>
        <button :class="{ on: channelTab === 'mall' }" @click="channelTab = 'mall'">商城</button>
      </div>
      <div class="cw-conv-list">
        <div
          v-for="c in filtered"
          :key="c.channel + c.cid"
          class="cw-conv"
          :class="{ sel: c.channel === currentChannel && c.cid === currentCid }"
          @click="select(c)"
        >
          <div class="cw-conv-avatar" :class="c.channel">
            {{ c.channel === 'xianyu' ? '🐟' : '🛍' }}
            <span v-if="c.last_role === 'user'" class="cw-dot"></span>
          </div>
          <div class="cw-conv-main">
            <div class="cw-conv-top">
              <span class="cw-conv-name">{{ c.name }}</span>
              <span class="cw-st" :class="statusOf(c).cls">{{ statusOf(c).label }}</span>
              <span class="cw-conv-time">{{ fmtTime(c.last_time) }}</span>
            </div>
            <div v-if="c.channel === 'xianyu'" class="cw-conv-item">{{ c.account || 'a1' }} · 闲鱼 · {{ c.msg_count }} 条</div>
            <div v-else class="cw-conv-item">商城 · {{ c.msg_count }} 条</div>
            <div class="cw-conv-snip">
              <span>{{ (c.last_role === 'user' ? '' : '[回复] ') + (c.last_text || '暂无消息') }}</span>
            </div>
          </div>
        </div>
        <div v-if="!filtered.length" class="cw-empty">暂无会话</div>
      </div>
    </aside>

    <!-- 中栏：聊天记录 -->
    <section class="cw-card cw-w-chat">
      <div class="cw-head">
        <span class="cw-head-title">
          💬 {{ currentName || '聊天记录' }}
          <span v-if="current" class="cw-ch" :class="current.channel">
            {{ current.channel === 'xianyu' ? '闲鱼' : '商城' }}
          </span>
          <span v-if="isTakeover" class="cw-ch manual">人工接管中</span>
        </span>
        <button
          v-if="currentCid && isXianyu"
          class="cw-btn"
          :class="isTakeover ? 'cw-btn-ghost' : 'cw-btn-blue'"
          @click="onToggleTakeover"
        >
          {{ isTakeover ? '恢复 AI 自动回复' : '接管会话' }}
        </button>
      </div>

      <div ref="chatBox" class="cw-msgs">
        <template v-if="currentCid">
          <div
            v-for="m in messages"
            :key="m.id"
            class="cw-msg-row"
            :class="{ self: m.role === 'assistant' }"
          >
            <div class="cw-msg-col">
              <div class="cw-msg-meta" :class="{ right: m.role === 'assistant' }">
                {{ senderLabel(m) }}<span class="cw-msg-time">{{ fmtTime(m.create_time) }}</span>
              </div>
              <div class="cw-msg-flex" :class="{ rev: m.role === 'assistant' }">
                <div
                  class="cw-bubble"
                  :class="m.role === 'assistant' ? (isHuman(m) ? 'self human' : 'self ai') : ''"
                >{{ m.content }}<span v-if="m.role === 'assistant'" class="cw-corner" :class="isHuman(m) ? 'h' : 'a'">{{ isHuman(m) ? '人工' : 'AI' }}</span></div>
              </div>
            </div>
          </div>
          <div v-if="!messages.length" class="cw-empty">暂无消息记录</div>
        </template>
        <div v-else class="cw-empty">← 请选择一个会话查看聊天记录</div>
      </div>

      <div v-if="currentCid" class="cw-input">
        <div v-if="error" class="cw-error">{{ error }}</div>
        <!-- 快捷短语：输入框上方，点击填入 -->
        <div class="cw-quick">
          <button v-for="p in quickPhrases" :key="p" class="cw-qchip" @click="draft = p">{{ p }}</button>
        </div>
        <div class="cw-input-row">
          <textarea
            v-model="draft"
            rows="2"
            :placeholder="isXianyu ? '输入回复，Enter 发送（Shift+Enter 换行）' : '输入人工回复，Enter 发送（买家端拉取可见）'"
            :disabled="false"
            @keydown.enter.exact.prevent="onSend"
          ></textarea>
          <button class="cw-btn cw-btn-blue cw-send" :disabled="busy || !draft.trim()" @click="onSend">
            发送
          </button>
        </div>
      </div>
    </section>

    <!-- 右栏：客户信息（chat-new 工作区样式） -->
    <aside class="cw-card cw-w-side">
      <div class="cw-head"><span class="cw-head-title">客户信息</span></div>
      <div v-if="current" class="cw-profile">
        <div class="cw-profile-avatar" :class="currentChannel">
          {{ currentChannel === 'xianyu' ? '🐟' : '🛍' }}
        </div>
        <div class="cw-profile-name">{{ current.name }}</div>
        <div class="cw-profile-tags">
          <span class="cw-ch" :class="currentChannel">
            {{ currentChannel === 'xianyu' ? '闲鱼渠道' : '商城渠道' }}
          </span>
          <span class="cw-mode" :class="isTakeover ? 'manual' : 'ai'">
            {{ isTakeover ? '人工接管' : 'AI 自动' }}
          </span>
        </div>
        <!-- 标签/备注 -->
        <div class="cw-memo">
          <span class="cw-tagchip" v-if="isBargaining">💬 议价中</span>
          <span class="cw-tagchip" v-if="(current.msg_count ?? 0) >= 20">⭐ 老客</span>
        </div>
      </div>
      <dl v-if="current" class="cw-kv">
        <dt>会话 ID</dt><dd class="cw-mono">{{ current.cid }}</dd>
        <dt>消息总数</dt><dd>{{ current.msg_count }}</dd>
        <dt>最后活跃</dt><dd>{{ fmtTime(current.last_time) }}</dd>
      </dl>
      <div class="cw-side-title">正在咨询的商品</div>
      <div v-if="current" class="cw-item-card">
        <div class="cw-item-name">{{ currentItem?.name || '智选 PB1 充电宝 20000mAh' }}</div>
        <div class="cw-item-desc" v-if="currentItem?.desc">{{ currentItem.desc }}</div>
      </div>
      <div class="cw-side-title">历史会话</div>
      <div class="cw-hist">
        <div v-for="h in historyPreview" :key="h" class="cw-hist-row">{{ h }}</div>
        <div v-if="!current" class="cw-empty">未选择会话</div>
      </div>
    </aside>
    </div>
  </div>
</template>

<style scoped>
/* 设计对齐 chat-new：白卡片 + rounded-lg + shadow-sm + border-gray-200 + blue-500 主色 */
.cw-wrap { display: flex; flex-direction: column; height: calc(100vh - 104px); }
.cw-ticket-bar {
  margin-bottom: 10px; padding: 9px 14px; font-size: 13px; color: #7f1d1d;
  background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
}
.cw-ticket-bar .p0 { color: #dc2626; }
.cw-ticket-bar.off { background: #f9fafb; border-color: #f3f4f6; color: #9ca3af; display: flex; align-items: center; gap: 10px; }
.cw-notify-result { font-size: 11px; color: #6b7280; }
.cw-ticket-link { margin-left: auto; color: #2563eb; font-weight: 500; }
.cw { display: flex; gap: 12px; flex: 1; min-height: 0; }
.cw-card {
  background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); display: flex; flex-direction: column; min-height: 0;
}
.cw-w-list { width: 280px; flex-shrink: 0; }
.cw-w-chat { flex: 1; min-width: 0; }
.cw-w-side { width: 300px; flex-shrink: 0; }

.cw-head {
  padding: 10px 12px; border-bottom: 1px solid #e5e7eb;
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.cw-head-title { font-size: 13px; font-weight: 600; color: #374151; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.cw-head-count { font-size: 11px; color: #9ca3af; background: #f3f4f6; border-radius: 999px; padding: 1px 8px; }

/* 渠道标签 */
.cw-ch { font-size: 10px; padding: 1px 7px; border-radius: 999px; white-space: nowrap; font-weight: 500; }
.cw-ch.xianyu { background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }
.cw-ch.mall { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
.cw-ch.manual { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }

/* 会话列表 */
.cw-search { padding: 10px 10px 6px; }
.cw-search input {
  width: 100%; padding: 7px 10px; font-size: 13px; border: 1px solid #e5e7eb;
  border-radius: 8px; outline: none;
}
.cw-search input:focus { border-color: #93c5fd; box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.15); }
.cw-tabs { display: flex; gap: 4px; padding: 0 10px 8px; }
.cw-tabs button {
  flex: 1; padding: 5px 0; font-size: 12px; border: 1px solid #e5e7eb; border-radius: 7px;
  background: #fff; cursor: pointer; color: #6b7280;
}
.cw-tabs button.on { background: #eff6ff; color: #2563eb; border-color: #bfdbfe; font-weight: 500; }
.cw-conv-list { flex: 1; overflow-y: auto; min-height: 0; }
.cw-conv {
  display: flex; gap: 10px; padding: 10px 12px; cursor: pointer;
  border-bottom: 1px solid #f3f4f6; transition: background 0.15s;
}
.cw-conv:hover { background: #f9fafb; }
.cw-conv.sel { background: #eff6ff; }
.cw-conv-avatar {
  position: relative;
  width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0; background: #e5e7eb;
  display: flex; align-items: center; justify-content: center; font-size: 15px;
}
.cw-dot {
  position: absolute; top: -2px; right: -2px; width: 10px; height: 10px;
  border-radius: 50%; background: #ef4444; border: 2px solid #fff;
}
.cw-st { font-size: 10px; padding: 1px 6px; border-radius: 999px; flex-shrink: 0; font-weight: 500; }
.cw-st.green { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
.cw-st.blue { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
.cw-st.red { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.cw-conv-avatar.xianyu { background: #ffedd5; }
.cw-conv-avatar.mall { background: #dbeafe; }
.cw-conv-main { flex: 1; min-width: 0; }
.cw-conv-top { display: flex; justify-content: space-between; gap: 6px; }
.cw-conv-name { font-size: 13px; font-weight: 500; color: #1f2937; }
.cw-conv-time { font-size: 11px; color: #9ca3af; flex-shrink: 0; }
.cw-conv-item { font-size: 11px; color: #60a5fa; margin-top: 1px; }
.cw-conv-snip {
  display: flex; justify-content: space-between; gap: 6px; margin-top: 2px;
  font-size: 12px; color: #9ca3af;
}
.cw-conv-snip > span:first-child { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cw-badge-human {
  flex-shrink: 0; font-size: 10px; color: #b91c1c; background: #fef2f2;
  border: 1px solid #fecaca; border-radius: 4px; padding: 0 4px;
}

/* 消息区（chat-new：isSelf → justify-end + bg-blue-500；对方 → bg-gray-100） */
.cw-msgs { flex: 1; overflow-y: auto; padding: 16px; min-height: 0; background: #fafafa; }
.cw-msg-row { display: flex; margin-bottom: 14px; }
.cw-msg-row.self { justify-content: flex-end; }
.cw-msg-col { max-width: 72%; }
.cw-msg-meta { font-size: 11px; color: #9ca3af; margin-bottom: 4px; }
.cw-msg-meta.right { text-align: right; }
.cw-msg-time { margin-left: 8px; }
.cw-msg-flex { display: flex; align-items: center; gap: 6px; }
.cw-msg-flex.rev { flex-direction: row-reverse; }
.cw-bubble {
  position: relative; padding: 9px 12px; font-size: 14px; line-height: 1.5;
  border-radius: 10px; white-space: pre-wrap; word-break: break-word;
  background: #f3f4f6; color: #1f2937;                       /* 买家：gray-100 */
}
.cw-bubble.self.ai { background: #60a5fa; color: #fff; }      /* AI：浅蓝 */
.cw-bubble.self.human { background: #1d4ed8; color: #fff; }   /* 人工：深蓝 */
.cw-corner {
  position: absolute; top: -8px; right: -4px; font-size: 10px; line-height: 1;
  padding: 2px 6px; border-radius: 999px; border: 1px solid #e5e7eb; background: #fff;
}
.cw-corner.a { color: #2563eb; }
.cw-corner.h { color: #1d4ed8; }

/* 输入区 */
.cw-input { padding: 10px 12px; border-top: 1px solid #e5e7eb; }
.cw-input-row { display: flex; gap: 8px; }
.cw-input textarea {
  flex: 1; resize: none; border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 9px 12px; font-size: 14px; outline: none; font-family: inherit;
}
.cw-input textarea:focus { border-color: #93c5fd; box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.15); }
.cw-input textarea:disabled { background: #f9fafb; color: #a1a1aa; }
.cw-send { flex-shrink: 0; align-self: flex-end; }
.cw-btn {
  padding: 6px 14px; font-size: 13px; border-radius: 8px; cursor: pointer;
  border: 1px solid #e5e7eb; background: #fff; color: #374151;
}
.cw-btn-blue { background: #3b82f6; color: #fff; border-color: #3b82f6; }
.cw-btn-blue:hover { background: #2563eb; }
.cw-btn-ghost:hover { background: #f3f4f6; }
.cw-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* 右栏 */
.cw-profile { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 14px 0; border-bottom: 1px solid #f3f4f6; }
.cw-profile-avatar {
  width: 52px; height: 52px; border-radius: 50%; font-size: 24px;
  display: flex; align-items: center; justify-content: center;
}
.cw-profile-avatar.xianyu { background: #ffedd5; }
.cw-profile-avatar.mall { background: #dbeafe; }
.cw-profile-name { font-size: 15px; font-weight: 600; color: #1f2937; }
.cw-profile-tags { display: flex; gap: 6px; }
.cw-kv { display: grid; grid-template-columns: 72px 1fr; gap: 8px 6px; font-size: 12px; padding: 12px 0; border-bottom: 1px solid #f3f4f6; margin-bottom: 12px; }
.cw-kv dt { color: #9ca3af; }
.cw-kv dd { margin: 0; color: #1f2937; word-break: break-all; }
.cw-mono { font-family: ui-monospace, monospace; font-size: 11px; }
.cw-mode { font-size: 11px; padding: 2px 8px; border-radius: 999px; }
.cw-mode.ai { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
.cw-mode.manual { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.cw-side-title { font-size: 12px; font-weight: 600; color: #6b7280; margin-bottom: 8px; }
.cw-quick { display: flex; gap: 6px; overflow-x: auto; margin-bottom: 8px; }
.cw-qchip {
  flex-shrink: 0; font-size: 12px; color: #374151; background: #f9fafb;
  border: 1px solid #e5e7eb; border-radius: 999px; padding: 5px 12px; cursor: pointer;
  white-space: nowrap;
}
.cw-qchip:hover { border-color: #93c5fd; color: #2563eb; background: #eff6ff; }
.cw-memo { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; justify-content: center; }
.cw-tagchip { font-size: 11px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 999px; padding: 2px 8px; }
.cw-item-card { background: #f9fafb; border: 1px solid #f3f4f6; border-radius: 8px; padding: 10px; margin-bottom: 12px; }
.cw-item-name { font-size: 13px; font-weight: 600; color: #1f2937; }
.cw-item-desc { font-size: 11px; color: #6b7280; margin-top: 4px; }
.cw-hist-row { font-size: 11px; color: #6b7280; padding: 4px 0; border-bottom: 1px dashed #f3f4f6; }
.cw-empty { color: #9ca3af; font-size: 13px; padding: 24px; text-align: center; }
.cw-error { font-size: 12px; color: #dc2626; margin-bottom: 6px; }
</style>
