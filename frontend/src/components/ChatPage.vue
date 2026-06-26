<script setup lang="ts">
import { ref, onMounted } from 'vue';
import ConversationList from './ConversationList.vue';
import MessageList from './MessageList.vue';
import MessageInput from './MessageInput.vue';
import {
  listConversations,
  getMessages,
  streamChat,
} from '../api';
import type { Conversation, Message, User } from '../types';

const props = defineProps<{
  user: User;
}>();

const emit = defineEmits<{
  logout: [];
}>();

// =============================================================
// 状态
// =============================================================
const conversations = ref<Conversation[]>([]);
const currentSessionId = ref<string | null>(null);
const messages = ref<Message[]>([]);

const streaming = ref(false);
const streamingText = ref('');

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
// 切换会话 → 加载历史消息
// =============================================================
async function selectConversation(sessionId: string) {
  if (streaming.value) return; // 生成中禁止切换
  if (sessionId === currentSessionId.value) return;

  currentSessionId.value = sessionId;
  messages.value = [];
  streamingText.value = '';
  error.value = '';
  await loadMessages();
}

async function loadMessages() {
  if (!currentSessionId.value) return;
  messagesLoading.value = true;
  try {
    // 一次拉够（这里 limit=100 足够日常；分页 cursor 仅作后端能力预留）
    const page = await getMessages(currentSessionId.value, undefined, 100);
    // 后端按 id DESC 返回，前端要按时间正序展示
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
  error.value = '';
}

// =============================================================
// 发送消息（SSE 流式）
// =============================================================
async function sendMessage(text: string) {
  if (streaming.value) return;

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
  let fullAnswer = '';
  // 捕获 meta 事件中的 references（最终挂到 assistant 消息上）
  let capturedContexts: string[] = [];
  const startSessionId = currentSessionId.value;

  try {
    for await (const event of streamChat(text, startSessionId ?? undefined)) {
      switch (event.type) {
        case 'meta':
          capturedContexts = event.contexts;
          break;

        case 'token':
          streamingText.value += event.text;
          fullAnswer += event.text;
          break;

        case 'done':
          // 后端在 done 事件中返回最终 session_id
          currentSessionId.value = event.session_id;
          // 把流式文本固化为 assistant 消息
          const assistantMsg: Message = {
            role: 'assistant',
            content: fullAnswer,
            contexts: capturedContexts.length ? capturedContexts : null,
            scores: null,
            create_time: new Date().toISOString(),
          };
          messages.value = [...messages.value, assistantMsg];
          streamingText.value = '';
          // 刷新左侧会话列表（新会话/新消息）
          await loadConversations();
          break;

        case 'error':
          error.value = event.message || '生成失败';
          streamingText.value = '';
          break;
      }
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '请求失败';
    streamingText.value = '';
  } finally {
    streaming.value = false;
  }
}

// =============================================================
// 生命周期
// =============================================================
onMounted(() => {
  loadConversations();
});
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
    />

    <main class="chat-main">
      <header class="chat-header">
        <div class="user-info">
          <span class="username">{{ user.display_name || user.username }}</span>
          <span class="role">{{ user.role }}</span>
        </div>
        <button class="logout-btn" @click="emit('logout')">退出登录</button>
      </header>

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
  display: flex;
  height: 100%;
  background: white;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 20px;
  border-bottom: 1px solid #e0e0e0;
  background: white;
  flex-shrink: 0;
}
.user-info {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.username {
  font-size: 15px;
  font-weight: 600;
  color: #333;
}
.role {
  font-size: 12px;
  color: #999;
  padding: 2px 8px;
  background: #f0f0f0;
  border-radius: 3px;
}
.logout-btn {
  padding: 6px 14px;
  background: white;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 13px;
  color: #666;
  cursor: pointer;
  transition: all 0.15s;
}
.logout-btn:hover {
  background: #f5f5f5;
  border-color: #ccc;
}
.messages {
  flex: 1;
  min-height: 0;
}
.loading-tip {
  padding: 8px 20px;
  text-align: center;
  color: #999;
  font-size: 13px;
  background: #fafafa;
  border-bottom: 1px solid #f0f0f0;
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
.error-banner {
  padding: 8px 20px;
  background: #fef2f2;
  color: #c33;
  font-size: 13px;
  border-top: 1px solid #fecaca;
  display: flex;
  justify-content: space-between;
  align-items: center;
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
