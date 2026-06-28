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
  MessagesPage,
  OrderDetail,
  OrderListResponse,
  Product,
  ProductListResponse,
  RegisterPayload,
  StreamEvent,
  User,
} from './types';

/** API 基础路径（同源，Vite 代理转发） */
const API = '';

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
  return data.user;
}

/** 注册（M9 新增） */
export async function register(payload: RegisterPayload): Promise<User> {
  const data = await http<{ user: User }>('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return data.user;
}

export async function logout(): Promise<void> {
  await http<{ message: string }>('/auth/logout', { method: 'POST' });
}

/** 获取当前登录用户，未登录返回 null */
export async function getMe(): Promise<User | null> {
  const res = await fetch(`${API}/auth/me`, { credentials: 'include' });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`getMe failed: ${res.status}`);
  return res.json();
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
  hit_at_k?: { 'hit@1': number; 'hit@3': number; 'hit@5': number; 'hit@10': number };
}

export async function getMetrics(): Promise<MetricsSnapshot> {
  return http('/metrics');
}

// =============================================================
// Chat - SSE 流式（核心）
// =============================================================
export async function* streamChat(
  query: string,
  sessionId?: string,
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
