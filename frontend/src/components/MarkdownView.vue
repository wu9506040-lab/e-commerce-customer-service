<script setup lang="ts">
import { computed, ref } from 'vue';
import { marked, type Tokens } from 'marked';

const props = defineProps<{
  text: string;
}>();

// 复制按钮：每个 <pre> 块按 token 索引跟踪"已复制"状态
const copiedIdx = ref<number | null>(null);

async function copyCode(text: string, idx: number) {
  try {
    await navigator.clipboard.writeText(text);
    copiedIdx.value = idx;
    setTimeout(() => {
      if (copiedIdx.value === idx) copiedIdx.value = null;
    }, 1500);
  } catch {
    // 兜底：旧浏览器 / 非 https 下 clipboard API 不可用
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      copiedIdx.value = idx;
      setTimeout(() => {
        if (copiedIdx.value === idx) copiedIdx.value = null;
      }, 1500);
    } catch {
      /* ignore */
    } finally {
      document.body.removeChild(ta);
    }
  }
}

// 用 marked.lexer() 拆成 token 数组，模板用 {{ }} 渲染文本（自动 HTML 转义，防 XSS）
// 严禁 v-html
const tokens = computed<Tokens.Generic[]>(() => {
  if (!props.text) return [];
  try {
    return marked.lexer(props.text);
  } catch {
    return [{ type: 'paragraph', text: props.text, raw: props.text } as Tokens.Paragraph];
  }
});
</script>

<template>
  <div class="md">
    <template v-for="(t, i) in tokens" :key="i">
      <p v-if="t.type === 'paragraph'">{{ (t as Tokens.Paragraph).text }}</p>

      <h1
        v-else-if="t.type === 'heading' && (t as Tokens.Heading).depth === 1"
      >{{ (t as Tokens.Heading).text }}</h1>
      <h2
        v-else-if="t.type === 'heading' && (t as Tokens.Heading).depth === 2"
      >{{ (t as Tokens.Heading).text }}</h2>
      <h3
        v-else-if="t.type === 'heading' && (t as Tokens.Heading).depth === 3"
      >{{ (t as Tokens.Heading).text }}</h3>
      <h4
        v-else-if="t.type === 'heading' && (t as Tokens.Heading).depth >= 4"
      >{{ (t as Tokens.Heading).text }}</h4>

      <div v-else-if="t.type === 'code'" class="code-block">
        <button class="copy-btn" @click="copyCode((t as Tokens.Code).text, i)">
          {{ copiedIdx === i ? '已复制' : '复制' }}
        </button>
        <pre><code>{{ (t as Tokens.Code).text }}</code></pre>
      </div>

      <ul v-else-if="t.type === 'list' && !(t as Tokens.List).ordered">
        <li v-for="(item, j) in (t as Tokens.List).items" :key="j">
          {{ (item as Tokens.ListItem).text }}
        </li>
      </ul>
      <ol v-else-if="t.type === 'list' && (t as Tokens.List).ordered">
        <li v-for="(item, j) in (t as Tokens.List).items" :key="j">
          {{ (item as Tokens.ListItem).text }}
        </li>
      </ol>

      <blockquote v-else-if="t.type === 'blockquote'">
        {{ (t as Tokens.Blockquote).text }}
      </blockquote>

      <hr v-else-if="t.type === 'hr'" />

      <!-- 兜底：未知 token 类型 → 纯文本 -->
      <p v-else>{{ (t as any).text ?? '' }}</p>
    </template>
  </div>
</template>

<style scoped>
.md {
  line-height: 1.65;
  word-break: break-word;
}
.md p {
  margin: 0 0 8px 0;
}
.md p:last-child {
  margin-bottom: 0;
}
.md h1, .md h2, .md h3, .md h4 {
  margin: 12px 0 6px 0;
  font-weight: 600;
  line-height: 1.3;
}
.md h1 { font-size: 1.4em; }
.md h2 { font-size: 1.2em; }
.md h3 { font-size: 1.1em; }
.md h4 { font-size: 1.0em; }
.md ul, .md ol {
  margin: 6px 0;
  padding-left: 22px;
}
.md li {
  margin: 2px 0;
}
.md pre {
  background: #1f2937;
  color: #e5e7eb;
  padding: 10px 12px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 8px 0;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  line-height: 1.5;
}
.code-block {
  position: relative;
  margin: 8px 0;
}
.copy-btn {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 2px 8px;
  background: rgba(255, 255, 255, 0.1);
  color: #e5e7eb;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  transition: all 0.15s;
  z-index: 1;
}
.copy-btn:hover {
  background: rgba(255, 255, 255, 0.2);
  border-color: rgba(255, 255, 255, 0.3);
}
.md pre code {
  background: transparent;
  padding: 0;
  color: inherit;
  font-size: inherit;
}
.md code {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 5px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 0.9em;
}
.md pre code {
  background: transparent;
  padding: 0;
}
.md blockquote {
  border-left: 3px solid #d1d5db;
  padding-left: 10px;
  color: #6b7280;
  margin: 8px 0;
}
.md hr {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 10px 0;
}
</style>
