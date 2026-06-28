<script setup lang="ts">
/**
 * 对话页（M9 重构 + 路由支持）
 * - 支持 /chat/:sessionId（恢复指定会话）
 * - 支持 ?q= query（从商品/订单跳转过来自动发问）
 * - SSE meta 完整保存到 message（intent/entities/tool_result_preview）
 * - 会话列表：左侧侧边栏
 */
import { ref, onMounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  listConversations,
  getMessages,
  deleteConversation,
  updateConversationTitle,
  streamChat,
} from '../api';
import type { Conversation, Message, StreamEvent } from '../types';
import ConversationList from '../components/ConversationList.vue';
import MessageList from '../components/MessageList.vue';
import MessageInput from '../components/MessageInput.vue';

const route = useRoute();
const router = useRouter();

const conversations = ref<Conversation[]>([]);
const currentSessionId = ref<string | null>(null);
const messages = ref<Message[]>([]);

const streaming = ref(false);
const streamingText = ref('');
const streamingMeta = ref<StreamEvent | null>(null);

const conversationsLoading = ref(false);
const messagesLoading = ref(false);
const error = ref('');

// =============================================================
// 会话列表
// =============================================================
async function loadConversations() {
  conversationsLoading.value = true;
  try {
    const data = await listConversations();
    conversations.value = data.conversations;
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载会话失败';
  } finally {
    conversationsLoading.value = false;
  }
}

// =============================================================
// 切换/恢复会话
// =============================================================
async function selectConversation(sessionId: string) {
  if (streaming.value) return;
  if (sessionId === currentSessionId.value) return;
  await switchToSession(sessionId);
}

async function switchToSession(sessionId: string) {
  currentSessionId.value = sessionId;
  messages.value = [];
  streamingText.value = '';
  streamingMeta.value = null;
  error.value = '';
  await loadMessages();

  // 同步路由（不带 ?q=）
  router.replace({ name: 'chat-session', params: { sessionId } });
}

async function loadMessages() {
  if (!currentSessionId.value) return;
  messagesLoading.value = true;
  try {
    const page = await getMessages(currentSessionId.value, undefined, 100);
    messages.value = [...page.messages].reverse();
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载消息失败';
  } finally {
    messagesLoading.value = false;
  }
}

// =============================================================
// 新会话
// =============================================================
function newChat() {
  if (streaming.value) return;
  currentSessionId.value = null;
  messages.value = [];
  streamingText.value = '';
  streamingMeta.value = null;
  error.value = '';
  router.replace({ name: 'chat' });
}

// =============================================================
// 删会话
// =============================================================
async function deleteConv(sessionId: string) {
  if (streaming.value) return;
  if (!confirm('删除这个会话？此操作不可恢复。')) return;
  try {
    await deleteConversation(sessionId);
    if (currentSessionId.value === sessionId) {
      newChat();
    }
    await loadConversations();
  } catch (e) {
    error.value = e instanceof Error ? e.message : '删除失败';
  }
}

// =============================================================
// 清空全部会话
// =============================================================
async function clearAllConvs() {
  if (streaming.value) return;
  const targets = conversations.value;
  if (!targets.length) return;
  // 已在外层 confirm 二次确认（ConversationList onClearAll）
  try {
    // 并发删除（不阻塞 UI，后端软删除极快）
    const currentSid = currentSessionId.value;
    let currentDeleted = false;
    await Promise.all(
      targets.map(async (c) => {
        try {
          await deleteConversation(c.session_id);
          if (c.session_id === currentSid) currentDeleted = true;
        } catch (e) {
          console.warn('删除失败', c.session_id, e);
        }
      }),
    );
    if (currentDeleted) newChat();
    await loadConversations();
  } catch (e) {
    error.value = e instanceof Error ? e.message : '清空失败';
  }
}

// =============================================================
// 自动标题（首条 user 消息前 20 字符）
// =============================================================
const MAX_TITLE_LEN = 20;
function buildAutoTitle(text: string): string {
  // 去首尾空白 + 折叠中间空白
  const cleaned = text.trim().replace(/\s+/g, ' ');
  if (!cleaned) return '新会话';
  if (cleaned.length <= MAX_TITLE_LEN) return cleaned;
  return `${cleaned.slice(0, MAX_TITLE_LEN)}…`;
}

async function setAutoTitle(sessionId: string, firstUserText: string) {
  const title = buildAutoTitle(firstUserText);
  try {
    await updateConversationTitle(sessionId, title);
  } catch (e) {
    console.warn('设置自动标题失败', e);
  }
}

// =============================================================
// 发送消息（SSE 流式）
// =============================================================
async function sendMessage(text: string) {
  if (streaming.value) return;
  if (!text.trim()) return;

  error.value = '';

  // 1) 乐观插入用户消息
  const userMsg: Message = {
    role: 'user',
    content: text,
    create_time: new Date().toISOString(),
  };
  messages.value = [...messages.value, userMsg];

  // 2) 准备流式状态
  streaming.value = true;
  streamingText.value = '';
  streamingMeta.value = null;
  let fullAnswer = '';
  let capturedMeta: StreamEvent | null = null;
  const startSessionId = currentSessionId.value;
  const isFirstUserMessage = !startSessionId; // 新会话（无 sid）才设标题

  try {
    for await (const event of streamChat(text, startSessionId ?? undefined)) {
      switch (event.type) {
        case 'meta':
          capturedMeta = event;
          streamingMeta.value = event;
          break;
        case 'token':
          streamingText.value += event.text;
          fullAnswer += event.text;
          break;
        case 'done':
          currentSessionId.value = event.session_id;
          {
            const meta = capturedMeta && capturedMeta.type === 'meta' ? capturedMeta : null;
            const assistantMsg: Message = {
              role: 'assistant',
              content: fullAnswer,
              intent: meta?.intent ?? null,
              entities: meta?.entities ?? null,
              tool_result_preview: meta?.tool_result_preview ?? null,
              contexts: meta?.contexts ?? null,
              scores: meta?.scores ?? null,
              create_time: new Date().toISOString(),
            };
            messages.value = [...messages.value, assistantMsg];
          }
          streamingText.value = '';
          streamingMeta.value = null;
          // 新会话首条 user 消息 → 先 PATCH 改短标题，再 refresh 一次避免列表闪烁
          if (isFirstUserMessage && currentSessionId.value) {
            await setAutoTitle(currentSessionId.value, text);
          }
          await loadConversations();
          break;
        case 'error':
          error.value = event.message || '生成失败';
          streamingText.value = '';
          streamingMeta.value = null;
          break;
      }
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '请求失败';
    streamingText.value = '';
    streamingMeta.value = null;
  } finally {
    streaming.value = false;
  }
}

// =============================================================
// 路由参数处理
// =============================================================
onMounted(async () => {
  await loadConversations();

  // /chat/:sessionId → 恢复会话
  const sid = route.params.sessionId as string | undefined;
  if (sid) {
    await switchToSession(sid);
  }
});

// 监听路由变化（用户手动改 URL）
watch(
  () => route.params.sessionId,
  async (sid) => {
    if (sid && sid !== currentSessionId.value) {
      await switchToSession(sid as string);
    } else if (!sid && currentSessionId.value) {
      // 用户回到 /chat（无 sid） → 新会话
      newChat();
    }
  },
);

// 监听 ?q= 自动发问
watch(
  () => route.query.q,
  async (q) => {
    if (q && typeof q === 'string' && !streaming.value) {
      // 清掉 query 避免重复发
      router.replace({ query: {} });
      await sendMessage(q);
    }
  },
  { immediate: false },
);
</script>

<template>
  <div class="chat-page">
    <ConversationList
      :conversations="conversations"
      :current-session-id="currentSessionId"
      :loading="conversationsLoading"
      @select="selectConversation"
      @refresh="loadConversations"
      @new-chat="newChat"
      @delete="deleteConv"
      @clear-all="clearAllConvs"
    />

    <main class="chat-main">
      <div v-if="messagesLoading" class="loading-tip">加载消息中…</div>

      <div v-if="streaming" class="streaming-indicator">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="label">正在生成…</span>
      </div>

      <MessageList
        class="messages"
        :messages="messages"
        :streaming-text="streamingText"
        :streaming="streaming"
        :sources="[]"
        :loading="messagesLoading"
        :streaming-meta="streamingMeta"
        @pick-suggestion="sendMessage"
      />

      <div v-if="error" class="error-banner">
        {{ error }}
        <button class="dismiss" @click="error = ''">×</button>
      </div>

      <MessageInput :disabled="streaming" @send="sendMessage" />
    </main>
  </div>
</template>

<style scoped>
.chat-page {
  flex: 1;
  display: flex;
  height: 100%;
  background: white;
  overflow: hidden;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100%;
}
.loading-tip {
  padding: 8px 20px;
  text-align: center;
  color: #999;
  font-size: 13px;
  background: #fafafa;
  border-bottom: 1px solid #f0f0f0;
  flex-shrink: 0;
}
.streaming-indicator {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 20px;
  background: #f5f3ff;
  color: #6d28d9;
  font-size: 13px;
  border-top: 1px solid #e9d5ff;
  flex-shrink: 0;
}
.streaming-indicator .dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  background: #6d28d9;
  border-radius: 50%;
  animation: bounce 1.2s ease-in-out infinite;
}
.streaming-indicator .dot:nth-child(2) { animation-delay: 0.15s; }
.streaming-indicator .dot:nth-child(3) { animation-delay: 0.3s; }
.streaming-indicator .label {
  margin-left: 6px;
}
@keyframes bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
  40% { transform: translateY(-4px); opacity: 1; }
}
.messages {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.error-banner {
  padding: 8px 20px;
  background: #fef2f2;
  color: #c33;
  font-size: 13px;
  border-top: 1px solid #fecaca;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
}
.error-banner .dismiss {
  background: none;
  border: none;
  color: #c33;
  font-size: 18px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
</style>
