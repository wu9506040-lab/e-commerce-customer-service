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
  /** 自动生成的标题（M9）：首条 user 消息前 20 字符 */
  title?: string | null;
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
  // M9 扩展：消息级 metadata（从 SSE meta 事件保存）
  intent?: string | null;
  entities?: Entities | null;
  tool_result_preview?: string | null;
  create_time: string;
}

/** Intent 分类器提取的实体（来自 /chat SSE meta） */
export interface Entities {
  order_no: string | null;
  sku: string | null;
  keywords: string[];
}

/** 分页消息响应 */
export interface MessagesPage {
  session_id: string;
  messages: Message[];
  has_more: boolean;
  next_cursor: number | null;
  limit: number;
}

// =============================================================
// M9 新增：商品 / 订单
// =============================================================
export interface Product {
  sku: string;
  name: string;
  price: number;
  stock: number;
  attributes?: Record<string, unknown> | null;
  description?: string | null;
  /** 相对路径，前端拼接 base URL（Vite dev / Nginx 都直接 serve /products/） */
  cover_url: string;
}

export interface ProductListResponse {
  products: Product[];
  total: number;
}

export interface OrderItem {
  sku: string;
  product_name: string;
  qty: number;
  unit_price: number;
  subtotal: number;
}

export interface Logistics {
  order_no: string;
  logistics_no?: string | null;
  status: string;
  last_location?: string | null;
  trajectory: Array<Record<string, unknown>>;
}

export interface OrderSummary {
  order_no: string;
  status: string;
  total_amount: number;
  create_time?: string | null;
  item_count: number;
}

export interface OrderListResponse {
  orders: OrderSummary[];
  total: number;
}

export interface OrderDetail {
  order: OrderSummary;
  items: OrderItem[];
  logistics?: Logistics | null;
}

/**
 * 订单状态流转响应（M10 闭环：创建/付款/发货/签收/退款）
 * 与后端 app/schemas/shop.py::OrderActionResponse 一一对应
 */
export interface OrderActionResponse {
  order_no: string;
  status: string;
  refund_no?: string | null;
}

/** 下单请求体（M10：前端 -> POST /orders） */
export interface CreateOrderPayload {
  sku: string;
  qty: number;
}

/** 退款申请请求体（M10：前端 -> POST /orders/:no/refund） */
export interface RefundPayload {
  reason?: string;
  remark?: string;
}

/** 注册请求体（前端 -> POST /auth/register） */
export interface RegisterPayload {
  username: string;
  password: string;
  display_name?: string;
  email?: string;
}

// =============================================================
/** /chat SSE 流式事件（自定义 JSON 协议） */
export type StreamEvent =
  | {
      type: 'meta';
      intent: string;
      entities: Entities;
      contexts: string[];
      scores: number[];
      // 扩展字段（按 intent 决定是否出现）
      products_found?: number;
      kb_hits?: number;
      policy_hits?: number;
      refundable?: boolean;
      reason?: string;
      order_no?: string;
      days_since_order?: number;
      v3_engine?: string;
      tool_result_preview?: string;
    }
  | { type: 'token'; text: string }
  | { type: 'done'; session_id: string }
  | { type: 'error'; message: string }
  | { type: 'heartbeat'; ts: number }
  | { type: 'closed' };
