// =============================================================
// SSE 流式上下文持久化（M14 + Sprint P2 / SSE Resume 共用）
//
// 用途：
// - 存 (session_id, stream_id, last_event_id, query) 到 sessionStorage
// - 流中断（网络断开/浏览器关闭）时，前端可凭 stream_id 调 /chat/resume 续传
// - M14 Stage 2：与 OrderCard card payload 复用同一 hook（D7 决策）
//
// 设计：
// - sessionStorage 而非 localStorage：刷新页面不丢；关闭 tab 自动清（不持久跨设备）
// - key 含 session_id 区分多会话；多 tab 同 session_id 各自独立（key 用 sessionStorage 自动隔离）
// - 全局单例（不暴露 ref），调用方按需 set/get/clear
// =============================================================

import { ref, type Ref } from 'vue';

export interface SseContext {
  session_id: string;
  stream_id: string;
  last_event_id: number;
  query: string;
  /** M14 Stage 2：捕获的 card payload（可选，用于 resume 后渲染对齐） */
  card?: unknown | null;
  /** meta 接收时间戳（ms）；超过 10min 视为过期，clear 时跳过 */
  captured_at?: number;
}

// =============================================================
// sessionStorage key 前缀
// =============================================================
const KEY_PREFIX = 'chat:sse_ctx:';

/**
 * 构造 sessionStorage key（按 session_id 分组）。
 * 注：单 tab 内同 session_id 共享；不同 tab 因 sessionStorage 隔离天然独立。
 */
function keyOf(sessionId: string): string {
  return `${KEY_PREFIX}${sessionId}`;
}

// =============================================================
// 全局缓存（同一 tab 内 5min 内重复读 sessionStorage 走内存）
// 简化：直接每次读 sessionStorage，无 LRU；场景 SSE 中断是低频事件可接受
// =============================================================

/**
 * 设置流上下文（M14：meta 事件到达时调）
 *
 * 副作用：
 * - 写 sessionStorage
 * - 更新内部 ref（用于响应式观察）
 */
export function setSseContext(ctx: SseContext): void {
  try {
    const payload: SseContext = { ...ctx, captured_at: Date.now() };
    sessionStorage.setItem(keyOf(ctx.session_id), JSON.stringify(payload));
  } catch (e) {
    // sessionStorage 不可用（隐私模式满 / quota 超限）→ 静默失败，不阻塞主流程
    console.warn('setSseContext 失败（sessionStorage 不可用）:', e);
  }
}

/**
 * 读取流上下文（SSE 中断且准备 resume 时调）
 *
 * 返回 null 表示：
 * - 该 session_id 无上下文
 * - 上下文已过期（> 10min，Redis checkpoint 也大概率过期）
 */
export function getSseContext(sessionId: string): SseContext | null {
  try {
    const raw = sessionStorage.getItem(keyOf(sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as SseContext;
    // 过期检查：10min（Redis checkpoint TTL 同 10min）
    if (parsed.captured_at && Date.now() - parsed.captured_at > 10 * 60 * 1000) {
      clearSseContext(sessionId);
      return null;
    }
    return parsed;
  } catch (e) {
    console.warn('getSseContext 失败:', e);
    return null;
  }
}

/**
 * 清除流上下文（done 事件 / 用户主动取消 / 续传成功后调）
 */
export function clearSseContext(sessionId: string): void {
  try {
    sessionStorage.removeItem(keyOf(sessionId));
  } catch {
    /* ignore */
  }
}

/**
 * 响应式版本：返回该 session_id 当前是否有可 resume 的上下文（用于 UI 提示）
 *
 * 用法：在 ChatPage onMounted 时调一次，watch sessionId 变化重新查
 * 注：响应式靠 setInterval 轮询（10s），简化实现；YAGNI 不引 watch 监听 storage 事件
 */
export function useSseContext(sessionId: Ref<string | null | undefined>): Ref<SseContext | null> {
  const ctxRef = ref<SseContext | null>(null);
  const refresh = () => {
    ctxRef.value = sessionId.value ? getSseContext(sessionId.value) : null;
  };
  refresh();
  // 简化：5s 轮询；M14 用例中断续传对实时性不敏感
  const timer = window.setInterval(refresh, 5000);
  // 注意：composable 本身不持有 unmount 清理；调用方在 onUnmounted 清 timer
  // 这里把 timer 挂到 ctxRef._timer 上让调用方能取（YAGNI 不暴露 useTimer）
  (ctxRef as unknown as { _timer: number })._timer = timer;
  return ctxRef;
}

/**
 * 清理 useSseContext 创建的轮询 timer（在 ChatPage onUnmounted 调用）
 */
export function disposeSseContextWatcher(ctxRef: Ref<SseContext | null>): void {
  const timer = (ctxRef as unknown as { _timer?: number })._timer;
  if (timer !== undefined) {
    window.clearInterval(timer);
  }
}