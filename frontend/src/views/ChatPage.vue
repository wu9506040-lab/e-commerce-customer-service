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
  // Sprint P2 / SSE Resume：流式中断续传
  resumeChat,
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
    // 后台刷会话列表失败不应弹错（用户视角：SSE Resume 后短暂离线导致的
    // "Failed to fetch" 不应该把"消息未送达"或全局 error banner 给用户看）。
    // 静默吞掉，下次 done 后或下次 user 操作自然重试。
    console.warn('loadConversations 失败（静默）:', e);
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
// 发送消息（SSE 流式 + M9.5 context 透传 + Sprint P2 / SSE Resume）
// ctx: { sku?, orderNo? } —— 从 /shop/:sku 或订单卡片跳转过来时携带
// =============================================================
// SSE Resume 边界：
//   - 自动 resume 最多 1 次（同 stream_id；后端限 2 次，留 1 次给手动重试）
//   - 失败 fallback：仅显示"消息未送达"，不暴露 AI 痕迹
const MAX_AUTO_RESUME = 1;
const RESUME_ONLINE_TIMEOUT_MS = 5000;

/**
 * 等待浏览器恢复联网（最多 5s）。
 * 防止 offline→catch→立即 resume→又撞 offline 的死循环。
 * 用户视角：网络抖动的常见模式（数百 ms 断网）静默恢复；超过 5s 真的没网才放弃。
 */
function waitForOnline(maxMs: number = RESUME_ONLINE_TIMEOUT_MS): Promise<boolean> {
  if (typeof navigator === 'undefined' || navigator.onLine) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(navigator.onLine), maxMs);
    const handler = () => {
      clearTimeout(timer);
      resolve(true);
    };
    window.addEventListener('online', handler, { once: true });
  });
}

async function sendMessage(text: string, ctx?: { sku?: string; orderNo?: string }) {
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
  // 新会话预生成 UUID：让后端复用作 session_id，
  // 保证流中途断开时 resumeChat 能拿到 sid（不再受限于 done 事件）
  const startSessionId =
    currentSessionId.value ?? crypto.randomUUID().replace(/-/g, '');
  const isFirstUserMessage = !currentSessionId.value; // 新会话才设标题

  // Sprint P2 / SSE Resume：本回合 stream_id + 最后 seq（catch 时用于续传）
  let streamId: string | undefined;
  let lastEventId: number | undefined;

  try {
    // 3) 流式消费（带自动 resume：最多 MAX_AUTO_RESUME 次静默续传）
    let resumeAttempt = 0;
    while (true) {
      let iter: AsyncGenerator<StreamEvent, void, void>;
      if (resumeAttempt === 0) {
        iter = streamChat(text, startSessionId, ctx);
      } else if (streamId && lastEventId !== undefined) {
        // startSessionId 现在总是有效（新会话已预生成 UUID）
        iter = resumeChat(
          startSessionId,
          streamId,
          text,
          lastEventId,
          ctx,
        );
      } else {
        // 无法 resume（无 streamId）→ 抛出原始错误
        throw new Error('网络异常');
      }

      try {
        let streamEnded = false;
        for await (const event of iter) {
          // Sprint P2 / SSE Resume：捕获 event.id 给续传用
          if (event.id !== undefined) lastEventId = event.id;

          switch (event.type) {
            case 'meta':
              if (event.stream_id) streamId = event.stream_id;
              capturedMeta = event;
              streamingMeta.value = event;
              break;
            case 'token':
              streamingText.value += event.text;
              fullAnswer += event.text;
              break;
            case 'resume_prefix':
              // Sprint P2 / SSE Resume：resume 端点一次性重发 prefix
              // 前一次流已清空 streamingText → 用 prefix 重置
              streamingText.value = event.prefix_text;
              fullAnswer = event.prefix_text;
              lastEventId = event.from_event_id;
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
                  card: meta?.card ?? null,
                  contexts: meta?.contexts ?? null,
                  scores: meta?.scores ?? null,
                  create_time: new Date().toISOString(),
                };
                messages.value = [...messages.value, assistantMsg];
              }
              streamingText.value = '';
              streamingMeta.value = null;
              if (isFirstUserMessage && currentSessionId.value) {
                await setAutoTitle(currentSessionId.value, text);
              }
              await loadConversations();
              streamEnded = true;
              break;
            case 'error':
              error.value = event.message || '生成失败';
              streamingText.value = '';
              streamingMeta.value = null;
              streamEnded = true;
              break;
          }
        }
        // 正常结束（reader done）→ 跳出循环
        return;
      } catch (streamErr) {
        // 流中断（reader 抛错 / 网络断开）
        // 等网络恢复后再 resume（避免 offline → 立即 resume → 又撞 offline）
        const backOnline = await waitForOnline();
        if (
          backOnline &&
          resumeAttempt < MAX_AUTO_RESUME &&
          streamId &&
          lastEventId !== undefined
        ) {
          // 静默 resume：不显示任何提示，用户视角无感
          resumeAttempt++;
          continue;
        }
        // 超限或无法 resume / 仍未恢复 → 抛出给外层 catch
        throw streamErr;
      }
    }
  } catch (e) {
    // 用户视角的"消息未送达"（不暴露 AI 痕迹 / 网络 / 流中断等技术细节）
    error.value = '消息未送达，请重试';
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

// 监听 ?q= 自动发问（M9.5：同时提取 ?sku= / ?order_no= context）
// 关键：immediate: true —— 路由跳转时 ChatPage 是新挂载，初始 query 就该触发
// 否则用户从商品详情跳过来时不会自动发问
watch(
  () => [route.query.q, route.query.sku, route.query.order_no],
  async ([q, sku, orderNo]) => {
    if (q && typeof q === 'string' && !streaming.value) {
      const ctx: { sku?: string; orderNo?: string } = {};
      if (typeof sku === 'string' && sku) ctx.sku = sku;
      if (typeof orderNo === 'string' && orderNo) ctx.orderNo = orderNo;
      // 清掉 query 避免重复发（在 sendMessage 之后清，避免 watch 重入时丢失 context）
      await sendMessage(q, Object.keys(ctx).length ? ctx : undefined);
      router.replace({ query: {} });
    }
  },
  { immediate: true },
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
  background: var(--gray-50);
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
  padding: var(--sp-2) var(--sp-5);
  text-align: center;
  color: var(--gray-500);
  font-size: var(--fs-sm);
  background: var(--gray-100);
  border-bottom: var(--border);
  flex-shrink: 0;
}
.streaming-indicator {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-5);
  background: var(--jd-red-light);
  color: var(--jd-red);
  font-size: var(--fs-sm);
  border-top: 1px solid var(--jd-red);
  flex-shrink: 0;
}
.streaming-indicator .dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  background: var(--jd-red);
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
  padding: var(--sp-2) var(--sp-5);
  background: var(--jd-red-light);
  color: var(--jd-red-dark);
  font-size: var(--fs-sm);
  border-top: 1px solid var(--jd-red);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
}
.error-banner .dismiss {
  background: none;
  border: none;
  color: var(--jd-red-dark);
  font-size: 18px;
  cursor: pointer;
  padding: 0 var(--sp-1);
  line-height: 1;
}
</style>
