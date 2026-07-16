<script setup lang="ts">
/**
 * 消息列表（M9 重构）
 * 在 assistant 消息气泡下方追加 <MessageCard>（根据 msg.intent 自动渲染商品/订单卡）
 */
import { ref, watch, nextTick, onMounted } from 'vue';
import type { Message } from '../types';
import MarkdownView from './MarkdownView.vue';
import MessageCard from './MessageCard.vue';

const props = defineProps<{
  messages: Message[];
  streamingText: string;
  streaming: boolean;
  sources: string[];
  loading?: boolean;
  streamingMeta?: import('../types').StreamEvent | null;
}>();

const emit = defineEmits<{
  'pick-suggestion': [text: string];
}>();

const scrollEl = ref<HTMLElement | null>(null);

// 建议 chip（空态展示）
const suggestions = [
  '你们有哪些产品？',
  '如何联系客服？',
  '支持哪些支付方式？',
];

let scrollScheduled = false;
function scrollToBottom() {
  if (scrollScheduled) return;
  scrollScheduled = true;
  requestAnimationFrame(() => {
    if (scrollEl.value) {
      scrollEl.value.scrollTop = scrollEl.value.scrollHeight;
    }
    scrollScheduled = false;
  });
}

watch(
  () => [props.messages.length, props.streamingText],
  async () => {
    await nextTick();
    scrollToBottom();
  },
);

onMounted(scrollToBottom);
</script>

<template>
  <div ref="scrollEl" class="message-list">
    <!-- Skeleton -->
    <div v-if="loading" class="skeletons">
      <div v-for="i in 3" :key="i" class="skeleton" :class="`w-${i}`">
        <div class="sk-line"></div>
        <div class="sk-line"></div>
        <div class="sk-line short"></div>
      </div>
    </div>

    <!-- 空态 -->
    <div v-if="messages.length === 0 && !streaming && !loading" class="empty">
      <div class="empty-mark">智</div>
      <p class="empty-title">开始对话吧</p>
      <p class="empty-sub">可以从下面这些话题开始：</p>
      <div class="chips">
        <button
          v-for="(s, i) in suggestions"
          :key="i"
          class="chip"
          @click="emit('pick-suggestion', s)"
        >{{ s }}</button>
      </div>
    </div>

    <!-- 消息气泡 -->
    <div
      v-for="(msg, idx) in messages"
      :key="`${msg.create_time}-${idx}`"
      :class="['message', msg.role]"
    >
      <div class="avatar-mini" v-if="msg.role === 'assistant'">
        <span>智</span>
      </div>
      <div class="bubble">
        <div class="content">
          <MarkdownView :text="msg.content" />
        </div>
        <!-- 上下文来源 -->
        <details v-if="msg.contexts && msg.contexts.length" class="sources">
          <summary>{{ msg.contexts.length }} 个参考来源</summary>
          <ol>
            <li v-for="(ctx, i) in msg.contexts" :key="i">{{ ctx }}</li>
          </ol>
        </details>
        <!-- 消息内嵌卡（按 intent 路由） -->
        <MessageCard v-if="msg.role === 'assistant'" :message="msg" />
      </div>
    </div>

    <!-- 流式中 -->
    <div v-if="streaming" class="message assistant">
      <div class="avatar-mini"><span>智</span></div>
      <div class="bubble streaming">
        <div class="content">
          <MarkdownView :text="streamingText" /><span class="cursor"></span>
        </div>
        <MessageCard
          v-if="streamingMeta && streamingMeta.type === 'meta' && streamingMeta.intent"
          :message="{
            role: 'assistant',
            content: streamingText,
            intent: streamingMeta.intent,
            entities: streamingMeta.entities,
            tool_result_preview: streamingMeta.tool_result_preview,
            card: streamingMeta.card,
            create_time: new Date().toISOString(),
          }"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.message-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-5);
  background: var(--gray-50);
}

/* 空态 */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--gray-500);
  gap: var(--sp-2);
  padding: 0 var(--sp-5);
}
.empty-mark {
  width: 64px;
  height: 64px;
  background: var(--jd-red);
  color: #fff;
  font-size: 32px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: var(--sp-2);
}
.empty-title {
  font-size: var(--fs-xl);
  color: var(--gray-700);
  margin: 0;
  font-weight: 600;
}
.empty-sub {
  font-size: var(--fs-sm);
  color: var(--gray-500);
  margin: 0;
}
.chips {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  flex-wrap: wrap;
  justify-content: center;
}
.chip {
  padding: 6px 14px;
  background: var(--gray-0);
  border: var(--border);
  font-size: var(--fs-sm);
  color: var(--gray-700);
  cursor: pointer;
  transition: all 0.15s;
}
.chip:hover {
  background: var(--jd-red-light);
  color: var(--jd-red);
  border-color: var(--jd-red);
}

/* Skeleton */
.skeletons {
  padding: var(--sp-5) 0;
}
.skeleton {
  padding: var(--sp-2) var(--sp-3);
  background: var(--gray-100);
  margin: 0 0 var(--sp-3) 0;
  display: inline-block;
  max-width: 70%;
}
.skeleton.w-1 { width: 60%; }
.skeleton.w-2 { width: 80%; }
.skeleton.w-3 { width: 45%; }
.sk-line {
  height: 12px;
  margin: 6px 0;
  background: linear-gradient(90deg, var(--gray-200) 0%, var(--gray-100) 50%, var(--gray-200) 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s linear infinite;
}
.sk-line.short { width: 60%; }
@keyframes shimmer {
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* 消息气泡 */
.message {
  margin-bottom: var(--sp-4);
  display: flex;
  animation: fadeIn 0.2s ease;
  gap: var(--sp-2);
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
.message.user { justify-content: flex-end; }
.message.assistant { justify-content: flex-start; }

/* AI 头像 */
.avatar-mini {
  width: 32px;
  height: 32px;
  background: var(--jd-red);
  color: #fff;
  font-size: var(--fs-sm);
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  align-self: flex-start;
}

.bubble {
  max-width: 75%;
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-base);
  line-height: 1.6;
  word-break: break-word;
}
.message.user .bubble {
  background: var(--jd-red);
  color: #fff;
}
.message.assistant .bubble {
  background: var(--gray-0);
  color: var(--gray-800);
  border: var(--border);
}
.bubble.streaming {
  background: var(--gray-0);
  border: var(--border);
}

.cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  background: var(--jd-red);
  margin-left: 2px;
  vertical-align: text-bottom;
  animation: cursor-pulse 1.1s ease-in-out infinite;
}
@keyframes cursor-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}

/* 来源 */
.sources {
  margin-top: var(--sp-2);
  padding-top: var(--sp-2);
  border-top: var(--border);
  font-size: var(--fs-xs);
  color: var(--gray-500);
}
.sources summary {
  cursor: pointer;
  user-select: none;
}
.sources summary:hover {
  color: var(--jd-red);
}
.sources ol {
  margin-top: 6px;
  padding-left: 20px;
}
.sources li {
  margin-bottom: 4px;
  line-height: 1.4;
}
</style>
