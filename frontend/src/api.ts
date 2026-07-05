// =============================================================
// API 封装 - 与后端 FastAPI 通信
//
// 设计要点：
// 1. 所有 fetch 带 credentials:'include'（自动携带 httpOnly Cookie）
// 2. 流式用 fetch + ReadableStream，不用 EventSource（要支持 POST body）
// 3. 错误统一抛 Error，调用方 try/catch
// =============================================================
import type {
  Conversation,
  CreateOrderPayload,
  MessagesPage,
  OrderActionResponse,
  OrderDetail,
  OrderListResponse,
  Product,
  ProductListResponse,
  RefundPayload,
  RegisterPayload,
  StreamEvent,
  User,
} from './types';
import { ref, type Ref } from 'vue';

/** API 基础路径（企业级 /api/ 前缀分层，与 SPA 路由空间隔离） */
const API = '/api';

/**
 * 登录态缓存（解决 httpOnly Cookie 不可被 document.cookie 读取的问题）
 *
 * 设计：API 层维护唯一可信的登录状态。
 * - 初始 null：未知（路由守卫首次调 /auth/me 探测）
 * - true：已登录（login/register 成功后置 true，getMe 返回有效用户）
 * - false：未登录（getMe 返回 401 或 logout 后）
 */
export const isAuthed: Ref<boolean | null> = ref(null);

/** 统一 fetch 包装：带 cookie + JSON */
async function http<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

// =============================================================
// Auth
// =============================================================
export async function login(
  username: string,
  password: string,
): Promise<User> {
  const form = new URLSearchParams();
  form.append('username', username);
  form.append('password', password);
  const data = await http<{ user: User }>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: form.toString(),
  });
  isAuthed.value = true;  // 同步登录态
  return data.user;
}

/** 注册（M9 新增） */
export async function register(payload: RegisterPayload): Promise<User> {
  const data = await http<{ user: User }>('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  isAuthed.value = true;  // 同步登录态（注册接口不返 cookie，由调用方再 login）
  return data.user;
}

/**
 * 一键 demo 体验（M13 cloud）
 * 后端自动创建 visitor_<uuid> 账号 + 立即发 token，
 * 前端无需注册，直接进系统。
 *
 * 适用场景：公网公开 demo（云平台简历展示用）
 * 失败：服务端 ENABLE_DEMO_LOGIN=false 或网络错误
 */
export async function demoLogin(): Promise<User> {
  const data = await http<{ user: User }>('/public/demo-account', {
    method: 'POST',
  });
  isAuthed.value = true;
  return data.user;
}

export async function logout(): Promise<void> {
  await http<{ message: string }>('/auth/logout', { method: 'POST' });
  isAuthed.value = false;  // 同步登出态
}

/** 获取当前登录用户，未登录返回 null；同时更新 isAuthed 缓存 */
export async function getMe(): Promise<User | null> {
  const res = await fetch(`${API}/auth/me`, { credentials: 'include' });
  if (res.status === 401) {
    isAuthed.value = false;
    return null;
  }
  if (!res.ok) throw new Error(`getMe failed: ${res.status}`);
  const user = (await res.json()) as User;
  isAuthed.value = true;
  return user;
}

// =============================================================
// Conversations
// =============================================================
export async function listConversations(): Promise<{
  conversations: Conversation[];
  total: number;
}> {
  return http('/conversations');
}

export async function getMessages(
  sessionId: string,
  cursor?: number,
  limit = 20,
): Promise<MessagesPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor !== undefined) params.append('cursor', String(cursor));
  return http(`/conversations/${encodeURIComponent(sessionId)}/messages?${params}`);
}

export async function deleteConversation(
  sessionId: string,
): Promise<{ session_id: string; message: string }> {
  return http(`/conversations/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
}

/** 更新会话标题（M9 自动生成标题用） */
export async function updateConversationTitle(
  sessionId: string,
  title: string,
): Promise<{ session_id: string; title: string; message: string }> {
  return http(`/conversations/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
}

// =============================================================
// Shop（M9 新增：商品 / 订单公开 API）
// =============================================================
export async function listProducts(params?: {
  category?: string;
  limit?: number;
}): Promise<ProductListResponse> {
  const search = new URLSearchParams();
  if (params?.category) search.append('category', params.category);
  if (params?.limit) search.append('limit', String(params.limit));
  const qs = search.toString();
  return http(`/products${qs ? `?${qs}` : ''}`);
}

export async function getProduct(sku: string): Promise<Product> {
  return http(`/products/${encodeURIComponent(sku)}`);
}

export async function listMyOrders(params?: {
  status?: string;
  limit?: number;
}): Promise<OrderListResponse> {
  const search = new URLSearchParams();
  if (params?.status) search.append('status', params.status);
  if (params?.limit) search.append('limit', String(params.limit));
  const qs = search.toString();
  return http(`/orders/my${qs ? `?${qs}` : ''}`);
}

export async function getOrderDetail(orderNo: string): Promise<OrderDetail> {
  return http(`/orders/${encodeURIComponent(orderNo)}`);
}

// =============================================================
// 订单状态流转（M10 闭环 demo）
// 对应后端 /api/orders 状态机：pending → paid → shipped → delivered → refunded
// =============================================================
export async function createOrder(
  payload: CreateOrderPayload,
): Promise<OrderActionResponse> {
  return http('/orders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function payOrder(orderNo: string): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/pay`, {
    method: 'POST',
  });
}

export async function shipOrder(orderNo: string): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/ship`, {
    method: 'POST',
  });
}

export async function confirmOrder(orderNo: string): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/confirm`, {
    method: 'POST',
  });
}

export async function refundOrder(
  orderNo: string,
  payload?: RefundPayload,
): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/refund`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  });
}

// =============================================================
// Metrics（M9 demo 首页用：展示能力指标）
// =============================================================
export interface MetricsSnapshot {
  uptime_seconds: number;
  chat?: {
    total: number;
    by_intent: Record<string, number>;
    latency_ms?: { p50: number; p90: number; p95: number; max: number; samples: number };
  };
  rag?: { qdrant_search_success: number; qdrant_search_total: number };
  embedding?: { calls_total: number; errors_total: number };
  hit_at_k?: {
    window_size: number;
    total_samples: number;
    'hit@1': number;
    'hit@3': number;
    'hit@5': number;
    'hit@10': number;
  };
}

export async function getMetrics(): Promise<MetricsSnapshot> {
  return http('/metrics');
}

// =============================================================
// Chat - SSE 流式（核心）
// =============================================================
/**
 * 流式对话（M9.5：支持 sku/order_no context 透传）
 * - 从 /shop/:sku 跳转带 ?sku=ZP1 → 后端注入【当前商品】到 prompt
 * - 从订单卡片跳转带 ?order_no=ORD... → 后端注入【当前订单】到 prompt
 * - 用户追问时会带当前 session_id（后端可继续注入同 context）
 */
export async function* streamChat(
  query: string,
  sessionId?: string,
  opts?: { sku?: string; orderNo?: string },
): AsyncGenerator<StreamEvent, void, void> {
  const res = await fetch(`${API}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    credentials: 'include',
    body: JSON.stringify({
      query,
      session_id: sessionId ?? null,
      // M9.5：用户从商品详情/订单卡片跳转过来时携带 context
      sku: opts?.sku ?? null,
      order_no: opts?.orderNo ?? null,
    }),
  });

  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE 协议：事件之间用 \n\n 分隔
      // 用 split('\n\n') 切，最后一段可能不完整，留到 buffer
      const parts = buffer.split('\n\n');
      buffer = parts.pop() ?? '';

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          const event = JSON.parse(payload) as StreamEvent;
          yield event;
        } catch (e) {
          console.warn('SSE 解析失败:', payload, e);
        }
      }
    }

    // 收尾：处理 buffer 里残留的最后一段
    if (buffer.trim().startsWith('data:')) {
      const payload = buffer.trim().slice(5).trim();
      if (payload) {
        try {
          yield JSON.parse(payload) as StreamEvent;
        } catch {
          /* ignore */
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
