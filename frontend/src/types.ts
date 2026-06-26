// =============================================================
// 后端 API 类型定义（与 backend/app/schemas/*.py 一一对应）
// =============================================================

/** 用户信息（来自 /auth/me，含 stats 字段） */
export interface User {
  id: number;
  username: string;
  display_name?: string | null;
  email?: string | null;
  role: string;
  status: number;
  create_time: string;
  // 可选：仅 /me 返回（UserOutStats），login 响应不带
  message_count?: number;
  conversation_count?: number;
}

/** 会话列表项（来自 GET /conversations） */
export interface Conversation {
  session_id: string;
  last_message: string | null;
  updated_at: string | null;
  message_count: number;
}

/** 单条消息（来自 GET /conversations/{sid}/messages） */
export interface Message {
  role: 'user' | 'assistant';
  content: string;
  contexts?: string[] | null;
  scores?: number[] | null;
  create_time: string;
}

/** 分页消息响应 */
export interface MessagesPage {
  session_id: string;
  messages: Message[];
  has_more: boolean;
  next_cursor: number | null;
  limit: number;
}

/** /chat SSE 流式事件（自定义 JSON 协议） */
export type StreamEvent =
  | { type: 'meta'; contexts: string[]; scores: number[] }
  | { type: 'token'; text: string }
  | { type: 'done'; session_id: string }
  | { type: 'error'; message: string };