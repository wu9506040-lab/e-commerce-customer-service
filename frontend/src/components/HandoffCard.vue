<script setup lang="ts">
/**
 * 转人工卡片（M14 V3）
 *
 * 用途：Agent 异常 / 用户要求转人工 / 业务规则触发时，
 *      在 assistant 消息气泡下方展示 handoff payload 给用户看
 *
 * 设计：
 * - 橙红色 alert 视觉权重（与 OrderCard 京东红区分，转人工是"重要事件"）
 * - 不可关闭（说明已升级）
 * - 展示：工单号 + 触发原因 + 用户名片 + 最近对话上下文
 * - agent_failure_context 仅 agent_unavailable 时展开
 */
import { computed } from 'vue';
import type { HandoffPayload, OrderSummary } from '../types';

const props = defineProps<{
  handoff: HandoffPayload;
}>();

// 触发原因中文标签（后端也有一份，前端做兜底）
const reasonLabelMap: Record<HandoffPayload['reason'], string> = {
  user_requested: '您要求转人工',
  agent_unavailable: '系统繁忙已自动升级',
  business_rule: '业务规则触发升级',
};

// 角色颜色：user / assistant
const roleClass = (role: string): string => {
  return role === 'user' ? 'role-user' : 'role-assistant';
};

// 触发原因是否需要展示失败上下文（仅 agent_unavailable）
const showFailureContext = computed(() => {
  return props.handoff.reason === 'agent_unavailable' && props.handoff.agent_failure_context;
});

// 失败上下文简化展示
const failureStage = computed(() => {
  return props.handoff.agent_failure_context?.failed_stage ?? '';
});

const failureErrorClass = computed(() => {
  const fc = props.handoff.agent_failure_context;
  if (!fc) return '';
  if (fc.v3_error_class && fc.v2_error_class) {
    return `V3: ${fc.v3_error_class} / V2: ${fc.v2_error_class}`;
  }
  return fc.v3_error_class || fc.v2_error_class || '';
});

const failureErrorMsg = computed(() => {
  const fc = props.handoff.agent_failure_context;
  if (!fc) return '';
  return fc.v3_error_msg || fc.v2_error_msg || '';
});

// 订单格式化
const formatOrder = (o: OrderSummary): string => {
  const amount = `¥${o.total_amount.toFixed(0)}`;
  return `${o.order_no} · ${o.status} · ${amount}`;
};
</script>

<template>
  <div class="handoff-card" :data-reason="handoff.reason">
    <!-- 头部：工单号 + 触发原因 -->
    <div class="handoff-header">
      <div class="handoff-icon">
        <!-- 紧急转人工图标 -->
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
          <circle cx="8.5" cy="7" r="4"/>
          <line x1="20" y1="8" x2="20" y2="14"/>
          <line x1="23" y1="11" x2="17" y2="11"/>
        </svg>
      </div>
      <div class="handoff-title-block">
        <div class="handoff-title">{{ handoff.reason_label }}</div>
        <div class="handoff-subtitle">
          {{ reasonLabelMap[handoff.reason] }} · 工单号
          <span class="handoff-id">{{ handoff.handoff_id }}</span>
        </div>
      </div>
    </div>

    <!-- 一句话摘要 -->
    <div v-if="handoff.summary_text && handoff.summary_text !== '（无摘要）'" class="handoff-summary">
      <span class="summary-label">摘要：</span>{{ handoff.summary_text }}
    </div>

    <!-- 用户名片 -->
    <div class="handoff-user-card">
      <span class="user-card-label">用户：</span>
      <span class="user-card-value">ID {{ handoff.user_card.user_id }}</span>
      <span class="user-card-divider">·</span>
      <span class="user-card-value">订单 {{ handoff.user_card.total_orders }} 个</span>
    </div>

    <!-- 最近订单（折叠） -->
    <details v-if="handoff.recent_orders.length > 0" class="handoff-details">
      <summary>最近订单（{{ handoff.recent_orders.length }}）</summary>
      <ul class="orders-list">
        <li v-for="o in handoff.recent_orders" :key="o.order_no">{{ formatOrder(o) }}</li>
      </ul>
    </details>

    <!-- 最近对话上下文（折叠） -->
    <details v-if="handoff.recent_messages.length > 0" class="handoff-details">
      <summary>最近对话（最近 {{ handoff.recent_messages.length }} 条）</summary>
      <ul class="messages-list">
        <li v-for="(m, idx) in handoff.recent_messages" :key="idx" :class="roleClass(m.role)">
          <span class="msg-role">{{ m.role === 'user' ? '用户' : 'AI' }}</span>
          <span class="msg-content">{{ m.content }}</span>
        </li>
      </ul>
    </details>

    <!-- 失败上下文（仅 agent_unavailable） -->
    <details v-if="showFailureContext" class="handoff-details failure-details" open>
      <summary>失败上下文（debug 用）</summary>
      <div class="failure-info">
        <div><span class="failure-key">失败阶段：</span>{{ failureStage }}</div>
        <div v-if="failureErrorClass"><span class="failure-key">异常类型：</span>{{ failureErrorClass }}</div>
        <div v-if="failureErrorMsg"><span class="failure-key">异常信息：</span>{{ failureErrorMsg }}</div>
      </div>
    </details>

    <!-- 底部：等待说明 -->
    <div class="handoff-footer">
      <span class="footer-dot"></span>
      人工客服会尽快通过站内信或电话联系您
    </div>
  </div>
</template>

<style scoped>
.handoff-card {
  border: 2px solid var(--handoff-color, #ff6b35);
  background: var(--handoff-bg, #fff5f0);
  padding: var(--sp-3) var(--sp-4);
  margin-top: var(--sp-2);
  /* 橙红色 alert 视觉权重，与 OrderCard 京东红区分 */
}

.handoff-card[data-reason="agent_unavailable"] {
  --handoff-color: #d4380d;
  --handoff-bg: #fff1f0;
}

.handoff-card[data-reason="user_requested"] {
  --handoff-color: #fa8c16;
  --handoff-bg: #fff7e6;
}

.handoff-card[data-reason="business_rule"] {
  --handoff-color: #722ed1;
  --handoff-bg: #f9f0ff;
}

.handoff-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}

.handoff-icon {
  flex-shrink: 0;
  color: var(--handoff-color);
  display: flex;
  align-items: center;
  justify-content: center;
}

.handoff-title-block {
  flex: 1;
  min-width: 0;
}

.handoff-title {
  font-weight: 600;
  color: var(--handoff-color);
  font-size: var(--fs-md, 14px);
  line-height: 1.4;
}

.handoff-subtitle {
  font-size: var(--fs-xs, 12px);
  color: var(--gray-700);
  margin-top: 2px;
}

.handoff-id {
  font-family: var(--font-mono, monospace);
  background: var(--gray-0, #fff);
  padding: 1px 6px;
  border: 1px solid var(--handoff-color);
  color: var(--handoff-color);
  margin-left: 4px;
  font-weight: 500;
}

.handoff-summary {
  font-size: var(--fs-sm, 13px);
  color: var(--gray-800);
  padding: var(--sp-2) var(--sp-3);
  background: rgba(255, 255, 255, 0.6);
  border-left: 3px solid var(--handoff-color);
  margin-bottom: var(--sp-2);
}

.summary-label {
  font-weight: 600;
  color: var(--handoff-color);
  margin-right: 4px;
}

.handoff-user-card {
  font-size: var(--fs-xs, 12px);
  color: var(--gray-700);
  padding: var(--sp-1) 0;
  border-top: 1px dashed var(--gray-200);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.user-card-label {
  font-weight: 500;
  color: var(--gray-600);
}

.user-card-divider {
  color: var(--gray-300);
}

.handoff-details {
  font-size: var(--fs-xs, 12px);
  margin-top: var(--sp-1);
}

.handoff-details summary {
  cursor: pointer;
  user-select: none;
  padding: var(--sp-1) 0;
  color: var(--gray-700);
  font-weight: 500;
}

.handoff-details summary:hover {
  color: var(--handoff-color);
}

.orders-list,
.messages-list {
  list-style: none;
  padding: var(--sp-1) 0 0 var(--sp-3);
  margin: 0;
}

.orders-list li {
  padding: 2px 0;
  color: var(--gray-700);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
}

.messages-list li {
  padding: 4px 0;
  border-bottom: 1px dashed var(--gray-100);
  display: flex;
  gap: 6px;
  align-items: flex-start;
}

.messages-list li:last-child {
  border-bottom: none;
}

.msg-role {
  flex-shrink: 0;
  font-weight: 600;
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 2px;
}

.role-user .msg-role {
  background: var(--jd-red, #e1251b);
  color: #fff;
}

.role-assistant .msg-role {
  background: var(--gray-200, #eee);
  color: var(--gray-700);
}

.msg-content {
  color: var(--gray-800);
  word-break: break-word;
}

.failure-details summary {
  color: var(--handoff-color);
}

.failure-info {
  padding: var(--sp-2) var(--sp-3);
  background: rgba(255, 255, 255, 0.8);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--gray-700);
}

.failure-info > div {
  padding: 2px 0;
}

.failure-key {
  font-weight: 600;
  color: var(--handoff-color);
  margin-right: 4px;
}

.handoff-footer {
  margin-top: var(--sp-2);
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--gray-200);
  font-size: var(--fs-xs, 12px);
  color: var(--gray-600);
  display: flex;
  align-items: center;
  gap: 6px;
}

.footer-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--handoff-color);
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% {
    opacity: 0.4;
  }
  50% {
    opacity: 1;
  }
}
</style>
