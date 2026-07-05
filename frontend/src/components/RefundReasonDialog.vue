<script setup lang="ts">
/**
 * 退款原因对话框（替代 window.prompt）
 * 京东红风格 + Teleport 到 body 避免 z-index 问题
 * 支持：快捷原因 chips + 自定义输入 + 取消/确认
 */
import { ref, watch, nextTick } from 'vue';

const props = withDefaults(
  defineProps<{
    show: boolean;
    orderNo?: string;
    defaultReason?: string;
  }>(),
  {
    orderNo: '',
    defaultReason: '用户申请退款',
  },
);

const emit = defineEmits<{
  'update:show': [v: boolean];
  'confirm': [reason: string];
  'cancel': [];
}>();

// 快捷原因 chips（演示场景预设）
const QUICK_REASONS = [
  '商品质量问题',
  '不想要了',
  '收到破损',
  '与描述不符',
];

const reason = ref('');
const inputRef = ref<HTMLTextAreaElement | null>(null);

watch(
  () => props.show,
  (v) => {
    if (v) {
      reason.value = props.defaultReason;
      nextTick(() => inputRef.value?.focus());
    }
  },
);

function pickQuick(text: string) {
  reason.value = text;
  nextTick(() => inputRef.value?.focus());
}

function onConfirm() {
  const text = reason.value.trim();
  if (!text) return; // 必填校验
  emit('confirm', text);
}

function onCancel() {
  emit('cancel');
  emit('update:show', false);
}

function onBackdrop() {
  onCancel();
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') onCancel();
}
</script>

<template>
  <Teleport to="body">
    <Transition name="refund-dialog">
      <div v-if="show" class="refund-dialog-mask" @click.self="onBackdrop" @keydown="onKeydown" tabindex="-1">
        <div class="refund-dialog" role="dialog" aria-modal="true">
          <header class="refund-dialog-head">
            <h3>申请退款</h3>
            <button class="refund-dialog-close" type="button" @click="onCancel" aria-label="关闭">×</button>
          </header>

          <div class="refund-dialog-body">
            <p v-if="orderNo" class="refund-dialog-order">
              订单号：<span>{{ orderNo }}</span>
            </p>

            <label class="refund-dialog-label">退款原因 <em>*</em></label>

            <!-- 快捷 chips -->
            <div class="refund-chips">
              <button
                v-for="(q, i) in QUICK_REASONS"
                :key="i"
                type="button"
                :class="['refund-chip', { active: reason === q }]"
                @click="pickQuick(q)"
              >{{ q }}</button>
            </div>

            <!-- 自定义输入 -->
            <textarea
              ref="inputRef"
              v-model="reason"
              class="refund-textarea"
              placeholder="请详细描述退款原因（5-200 字）"
              rows="3"
              maxlength="200"
            />
            <div class="refund-counter">{{ reason.length }} / 200</div>
          </div>

          <footer class="refund-dialog-foot">
            <button type="button" class="refund-btn-cancel" @click="onCancel">取消</button>
            <button
              type="button"
              class="refund-btn-confirm"
              :disabled="!reason.trim()"
              @click="onConfirm"
            >确认退款</button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.refund-dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  padding: var(--sp-4);
}
.refund-dialog {
  background: var(--gray-0);
  width: 100%;
  max-width: 420px;
  border: var(--border);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
  display: flex;
  flex-direction: column;
}

/* Head */
.refund-dialog-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
  background: var(--jd-red);
  color: #fff;
}
.refund-dialog-head h3 {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
}
.refund-dialog-close {
  background: transparent;
  border: none;
  color: #fff;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
  padding: 0 6px;
  opacity: 0.85;
}
.refund-dialog-close:hover {
  opacity: 1;
}

/* Body */
.refund-dialog-body {
  padding: var(--sp-4);
}
.refund-dialog-order {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--gray-600);
}
.refund-dialog-order span {
  font-family: var(--font-mono);
  color: var(--gray-800);
  font-weight: 600;
}
.refund-dialog-label {
  display: block;
  font-size: var(--fs-sm);
  color: var(--gray-700);
  margin-bottom: var(--sp-2);
}
.refund-dialog-label em {
  color: var(--jd-red);
  font-style: normal;
  margin-left: 2px;
}

.refund-chips {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-3);
}
.refund-chip {
  padding: 5px 12px;
  background: var(--gray-50);
  border: 1px solid var(--gray-300);
  color: var(--gray-700);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: all 0.15s;
}
.refund-chip:hover {
  border-color: var(--jd-red);
  color: var(--jd-red);
}
.refund-chip.active {
  background: var(--jd-red-light);
  border-color: var(--jd-red);
  color: var(--jd-red);
}

.refund-textarea {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--gray-300);
  font-size: var(--fs-base);
  font-family: inherit;
  resize: vertical;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}
.refund-textarea:focus {
  border-color: var(--jd-red);
}
.refund-counter {
  margin-top: 4px;
  font-size: var(--fs-xs);
  color: var(--gray-500);
  text-align: right;
}

/* Foot */
.refund-dialog-foot {
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  border-top: var(--border);
  background: var(--gray-50);
}
.refund-btn-cancel,
.refund-btn-confirm {
  flex: 1;
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-base);
  cursor: pointer;
  border: 1px solid;
  transition: all 0.15s;
}
.refund-btn-cancel {
  background: var(--gray-0);
  border-color: var(--gray-300);
  color: var(--gray-700);
}
.refund-btn-cancel:hover {
  border-color: var(--gray-500);
}
.refund-btn-confirm {
  background: var(--jd-red);
  border-color: var(--jd-red);
  color: #fff;
  font-weight: 500;
}
.refund-btn-confirm:hover:not(:disabled) {
  background: var(--jd-red-hover, #c81623);
}
.refund-btn-confirm:disabled {
  background: var(--gray-400);
  border-color: var(--gray-400);
  cursor: not-allowed;
}

/* Transition */
.refund-dialog-enter-active,
.refund-dialog-leave-active {
  transition: opacity 0.18s ease;
}
.refund-dialog-enter-from,
.refund-dialog-leave-to {
  opacity: 0;
}
</style>