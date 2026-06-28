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
      <p class="empty-title">开始对话吧 👋</p>
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
        <!-- M9: assistant 消息下方挂卡片 -->
        <MessageCard v-if="msg.role === 'assistant'" :message="msg" />
      </div>
    </div>

    <!-- 流式中 -->
    <div v-if="streaming" class="message assistant">
      <div class="bubble streaming">
        <div class="content">
          <MarkdownView :text="streamingText" /><span class="cursor"></span>
        </div>
        <!-- 流式过程中也挂卡片（meta 已有数据时） -->
        <MessageCard
          v-if="streamingMeta && streamingMeta.type === 'meta' && streamingMeta.intent"
          :message="{
            role: 'assistant',
            content: streamingText,
            intent: streamingMeta.intent,
            entities: streamingMeta.entities,
            tool_result_preview: streamingMeta.tool_result_preview,
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
  padding: 20px;
  background: white;
}
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #999;
  gap: 8px;
  padding: 0 20px;
}
.empty-title {
  font-size: 20px;
  color: #bbb;
  margin: 0;
}
.empty-sub {
  font-size: 13px;
  color: #ccc;
  margin: 0;
}
.chips {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
  justify-content: center;
}
.chip {
  padding: 6px 14px;
  background: white;
  border: 1px solid #d1d5db;
  border-radius: 16px;
  font-size: 13px;
  color: #4b5563;
  cursor: pointer;
  transition: all 0.15s;
}
.chip:hover {
  background: #667eea;
  color: white;
  border-color: #667eea;
}

/* Skeleton */
.skeletons {
  padding: 20px 0;
}
.skeleton {
  padding: 10px 14px;
  background: #f9fafb;
  border-radius: 8px;
  margin: 0 0 12px 0;
  display: inline-block;
  max-width: 70%;
}
.skeleton.w-1 { width: 60%; }
.skeleton.w-2 { width: 80%; }
.skeleton.w-3 { width: 45%; }
.sk-line {
  height: 12px;
  margin: 6px 0;
  border-radius: 3px;
  background: linear-gradient(90deg, #e5e7eb 0%, #f3f4f6 50%, #e5e7eb 100%);
  background-size: 200% 100%;
  animation: shimmer 1.5s linear infinite;
}
.sk-line.short { width: 60%; }
@keyframes shimmer {
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* 消息 */
.message {
  margin-bottom: 16px;
  display: flex;
  animation: fadeIn 0.2s ease;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
.message.user { justify-content: flex-end; }
.message.assistant { justify-content: flex-start; }
.bubble {
  max-width: 75%;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}
.message.user .bubble {
  background: #667eea;
  color: white;
}
.message.assistant .bubble {
  background: #f3f4f6;
  color: #333;
}
.bubble.streaming {
  background: #f9fafb;
  border: 1px solid #e5e7eb;
}
.cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  background: linear-gradient(180deg, #667eea 0%, #5568d3 100%);
  margin-left: 2px;
  vertical-align: text-bottom;
  animation: cursor-pulse 1.1s ease-in-out infinite;
  border-radius: 1px;
}
@keyframes cursor-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}
.sources {
  margin-top: 8px;
  font-size: 12px;
  color: #888;
}
.sources summary {
  cursor: pointer;
  user-select: none;
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
