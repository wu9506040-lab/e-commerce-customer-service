# Sprint 19 Tool 接入 spec · 9 个 Service 包装为 Agent Function Calling（2026-07-21）

> **目的**：Sprint 18 落地了 9 个 Service（售后/售前/售中）但 AI 不能调。本 Sprint 把它们包装成 Tool，注册到现有 Agent Function Calling 框架，让 AI 自动选择调用。
>
> **关键设计决策（用户拍板）**：
> - **售中写操作需要 confirmed 参数**——AI 调用前必须主动询问用户确认，未确认则返"请确认"提示（防误操作）。
> - **售后/售前只读操作无需 confirmed**——读操作零风险，可直接调。
> - **agent_fc.yaml prompt 同步更新**——从 4 工具 → 13 工具。

---

## 1. 总览

### 1.1 三个新 Tool 类

| Tool 类 | 源 Service | 方法数 | 类型 | 写操作需确认 |
|---------|-----------|--------|------|--------------|
| `AfterSalesTool` | `AfterSalesRuleService` | 3 | 只读 | ❌ |
| `PromotionTool` | `PromotionRuleService` | 3 | 只读 | ❌ |
| `OrderModifyTool` | `OrderModifyService` | 3 | 写 | ✅（需 confirmed=true）|

### 1.2 现有 Tool（保持不变）

| Tool 类 | 方法 | 状态 |
|---------|------|------|
| `OrderTool` | lookup_order | 已有 |
| `ProductTool` | search_product | 已有 |
| `RefundTool` | check_refundable | 已有 |
| `PolicyService` | search_policy | 已有 |

### 1.3 注册后总数

**13 个 Tool**（4 已有 + 9 新增）

---

## 2. Tool 类设计

### 2.1 AfterSalesTool（售后规则 · 只读）

**文件**：`backend/app/tools/after_sales_tool.py`

```python
class AfterSalesTool:
    """售后规则咨询 Tool（V2：仅 Service 接口适配，不接 Tool 层）"""
    
    @staticmethod
    def get_refund_reason_advice(user_id: int, order_no: str, reason_category: str) -> dict:
        """退款原因填写指导 → Service.get_refund_reason_advice → DTO → dict"""
        ...
    
    @staticmethod
    def get_shipping_insurance_info(order_no: str, return_status: str) -> dict:
        """运费险规则"""
        ...
    
    @staticmethod
    def get_refund_type_advice(user_id: int, order_no: str) -> dict:
        """仅退款 vs 退货退款建议"""
        ...
```

**DTO→dict 适配**（统一 helper）：
```python
def _advice_to_dict(advice: RefundReasonAdvice) -> dict:
    return advice.model_dump()  # Pydantic v2
```

### 2.2 PromotionTool（售前规则 · 只读）

**文件**：`backend/app/tools/promotion_tool.py`

```python
class PromotionTool:
    """售前优惠规则咨询 Tool"""
    
    @staticmethod
    def get_active_promotions(user_id: int, cart_items: list[dict]) -> dict:
        """当前可用优惠活动"""
        ...
    
    @staticmethod
    def check_coupon_stackable(coupon_ids: list[str]) -> dict:
        """优惠券叠加校验"""
        ...
    
    @staticmethod
    def calculate_bundle_discount(store_totals: dict[str, float]) -> dict:
        """跨店满减计算"""
        ...
```

### 2.3 OrderModifyTool（售中写操作 · 需确认）

**文件**：`backend/app/tools/order_modify_tool.py`

```python
class OrderModifyTool:
    """售中订单修改 Tool（V2：写操作前必须 confirmed=True）"""
    
    @staticmethod
    def modify_address(
        user_id: int, order_no: str, new_address: str, confirmed: bool = False,
    ) -> dict:
        """修改收货地址
        
        Args:
            user_id: 用户 ID（ToolContext 注入）
            order_no: 订单号
            new_address: 新地址
            confirmed: 用户是否已确认（默认 False 触发确认提示）
        
        Returns:
            confirmed=False: {"status": "needs_confirmation", "prompt": "将订单 ORD... 的收货地址改为「XXX」，确定吗？"}
            confirmed=True: ModifyResult.to_dict() 或异常信息
        """
        ...
    
    @staticmethod
    def modify_item_spec(
        user_id: int, order_no: str, sku: str, new_qty: int,
        confirmed: bool = False,
    ) -> dict:
        """修改商品规格/数量"""
        ...
    
    @staticmethod
    def merge_orders(
        user_id: int, order_nos: list[str], confirmed: bool = False,
    ) -> dict:
        """合并订单"""
        ...
```

**确认机制（统一 helper）**：
```python
def _check_confirmation(
    confirmed: bool, action_desc: str,
) -> Optional[dict]:
    """未确认时返确认提示 dict；已确认返 None（继续执行）"""
    if not confirmed:
        return {
            "status": "needs_confirmation",
            "prompt": f"即将{action_desc}，请用户回复「确认」后再执行",
            "requires_user_input": True,
        }
    return None
```

### 2.4 Tool 调用 Service 的桥接模式

**复用 Service 工厂**（沿用 S15 OrderService 模式）：

```python
# after_sales_tool.py
from app.services.after_sales.factory import get_after_sales_rule_service_factory
from app.services.order.factory import run_sync

class AfterSalesTool:
    @staticmethod
    def get_refund_reason_advice(user_id: int, order_no: str, reason_category: str) -> dict:
        svc = get_after_sales_rule_service_factory().get_after_sales_rule_service()
        advice = run_sync(svc.get_refund_reason_advice(user_id, order_no, reason_category))
        return advice.model_dump() if advice else {"error": "no advice available"}
```

---

## 3. registry.py 改造

### 3.1 新增 9 个 ToolSpec

**位置**：`backend/app/tools/registry.py` 的 `REGISTRY` 字典

**模式**：参照现有 `_run_lookup_order` 等，9 个新 `_run_xxx_xxx` 函数 + 9 个 ToolSpec 注册

```python
def _run_get_refund_reason_advice(args: dict, ctx: ToolContext) -> dict:
    from app.tools.after_sales_tool import AfterSalesTool
    user_id = ctx.user_id
    order_no = args.get("order_no")
    reason_category = args.get("reason_category")
    if not order_no or not reason_category:
        return {"error": "order_no and reason_category required"}
    if not user_id:
        return {"error": "user_id required"}
    return AfterSalesTool.get_refund_reason_advice(user_id, order_no, reason_category)

# 类似地：9 个 _run_xxx 函数 + 9 个 ToolSpec 注册
```

### 3.2 写操作 Tool 的 confirmed 参数

```python
def _run_modify_address(args: dict, ctx: ToolContext) -> dict:
    from app.tools.order_modify_tool import OrderModifyTool
    user_id = ctx.user_id
    order_no = args.get("order_no")
    new_address = args.get("new_address")
    confirmed = bool(args.get("confirmed", False))  # 默认 False 触发确认
    if not order_no or not new_address:
        return {"error": "order_no and new_address required"}
    if not user_id:
        return {"error": "user_id required"}
    return OrderModifyTool.modify_address(user_id, order_no, new_address, confirmed)
```

### 3.3 13 个 ToolSpec 描述（写给 LLM 看的）

```python
REGISTRY = {
    # ===== 已有 4 个 =====
    "lookup_order": ToolSpec(...),
    "search_product": ToolSpec(...),
    "search_policy": ToolSpec(...),
    "check_refundable": ToolSpec(...),
    
    # ===== 新增 9 个 =====
    
    # 售后 3（只读）
    "get_refund_reason_advice": ToolSpec(
        name="get_refund_reason_advice",
        description="退款原因填写指导：建议具体原因文字 + 需要的凭证 + 成功率提示。"
                    "用户问'怎么填退款原因'/'什么理由容易过'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "reason_category": {
                    "type": "string",
                    "enum": ["quality", "no_reason", "size", "not_as_described", "late", "other"],
                    "description": "原因类别",
                },
            },
            "required": ["order_no", "reason_category"],
        },
        runner=_run_get_refund_reason_advice,
    ),
    "get_shipping_insurance_info": ToolSpec(...),
    "get_refund_type_advice": ToolSpec(...),
    
    # 售前 3（只读）
    "get_active_promotions": ToolSpec(
        name="get_active_promotions",
        description="查当前用户可用的优惠活动（满减/折扣/赠品）。"
                    "用户问'有什么优惠'/'双11怎么减'/'我能用什么券'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "cart_items": {
                    "type": "array",
                    "description": "购物车商品列表 [{sku, qty, unit_price}, ...]，可选",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "qty": {"type": "integer"},
                            "unit_price": {"type": "number"},
                        },
                    },
                },
            },
        },
        runner=_run_get_active_promotions,
    ),
    "check_coupon_stackable": ToolSpec(...),
    "calculate_bundle_discount": ToolSpec(...),
    
    # 售中 3（写操作 · 需确认）
    "modify_address": ToolSpec(
        name="modify_address",
        description="修改订单收货地址（限未发货订单）。"
                    "**写操作前必须设置 confirmed=true**，否则会返回确认提示。"
                    "用户问'能改地址吗'/'我想改收货地址'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string"},
                "new_address": {"type": "string", "description": "新收货地址"},
                "confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": "用户已确认修改（true=执行；false=返回确认提示）",
                },
            },
            "required": ["order_no", "new_address"],
        },
        runner=_run_modify_address,
    ),
    "modify_item_spec": ToolSpec(...),
    "merge_orders": ToolSpec(...),
}
```

### 3.4 dispatch() 改造

**`confirmed=False` 路径处理**：

```python
# 当前 dispatch 不需要改（写操作 Tool 内部处理 confirmed，dispatch 只是路由）
# 但需要在 dispatch 完成后，如果返回 {"status": "needs_confirmation"}，
# 应在 Agent runner 层中断主循环，告诉用户确认（V3+ Agent 层处理）

# V2 简化：dispatch 直接返回 confirmation dict，LLM 看到后下一轮询问用户
#（符合现有 FC 协议，LLM 看到 needs_confirmation 会主动生成询问文本）
```

---

## 4. agent_fc.yaml prompt 更新

**位置**：`backend/config/prompts/agent_fc.yaml`

### 4.1 工具列表更新（4 → 13）

```yaml
content: |
  你是一个专业的电商客服助手。你可以调用以下 13 个工具：

  ## 售前（3 个）
  1. search_product(keyword, limit)
     - 按关键词搜商品；用户问商品信息时调用
  2. get_active_promotions(cart_items?)
     - 查当前可用优惠活动；用户问"有什么优惠"/"双11怎么减"时调用
  3. check_coupon_stackable(coupon_ids: list[str])
     - 查优惠券能否叠加；用户问"我的券能一起用吗"时调用
  4. calculate_bundle_discount(store_totals: dict)
     - 算跨店满减 + 凑单建议；用户问"几家店凑单怎么减"时调用

  ## 售中（4 个）
  5. lookup_order(order_no)
     - 查订单详情；用户提到订单号时调用
  6. get_logistics(order_no)
     - 查物流（V2 由 lookup_order 合并提供，单独 Tool 暂不暴露）
  7. modify_address(order_no, new_address, confirmed=false)
     - **写操作**：修改收货地址。**首次调用必须 confirmed=false**，
       拿到确认提示后再问用户，用户回复"确认"后第二次调用 confirmed=true
  8. modify_item_spec(order_no, sku, new_qty, confirmed=false)
     - **写操作**：修改规格/数量，确认机制同上
  9. merge_orders(order_nos, confirmed=false)
     - **写操作**：合并订单，确认机制同上

  ## 售后（4 个）
  10. check_refundable(order_no)
      - 判断能否退款
  11. get_refund_reason_advice(order_no, reason_category)
      - 退款原因填写指导；用户问"怎么填理由"时调用
  12. get_shipping_insurance_info(order_no, return_status)
      - 运费险规则；用户问"运费险赔多少"时调用
  13. get_refund_type_advice(order_no)
      - 仅退款 vs 退货退款建议
  14. search_policy(query, top_k)
      - 政策 RAG 检索（退货、运费、保修、发票等）

  ## 写操作确认机制（重要！）
  modify_address / modify_item_spec / merge_orders 是写操作，
  必须遵循 2 步确认：
  Step 1: 首次调用 confirmed=false → 拿到 {"status": "needs_confirmation", "prompt": "..."}
  Step 2: 向用户展示 prompt 内容，问"确定吗？"
  Step 3: 用户回复"确认"后，**同一订单号 + 同一操作**再次调用 confirmed=true
  **禁止**跳确认直接调 confirmed=true（除非用户在上一轮已经明确说过"确认"/"是的"）
  
  ## 工作流程
  1. 判断问题类别（售前/售中/售后/政策）
  2. 调对应工具；写操作必须走确认流程
  3. 拿到结果后综合成自然语言回答
  4. 工具结果为空或出错时直接告诉用户 + 建议人工

  ## 输出约束
  - 严格基于工具结果回答，禁止编造
  - 写操作前必须确认（见"写操作确认机制"）
  - 回答控制在 200 字以内
```

### 4.2 prompt 版本管理

按 CLAUDE.md §9.6（Prompt 独立管理）+ Sprint 5 Phase 1（版本化），更新应作为 prompt v2：

- v1: 当前（3 工具）
- v2: 新增 9 工具 + 写操作确认机制

具体更新方式留 subagent 决策（参考 Sprint 5 manifest 机制）。

---

## 5. 文件边界（强制 · 防越权）

| 模块 | 文件 | 范围 |
|------|------|------|
| 新 Tool | `backend/app/tools/after_sales_tool.py`（< 200 行） |
| 新 Tool | `backend/app/tools/promotion_tool.py`（< 200 行） |
| 新 Tool | `backend/app/tools/order_modify_tool.py`（< 250 行 · 含 confirmed 机制） |
| 修改 | `backend/app/tools/registry.py`（+9 ToolSpec + 9 _run 函数） |
| 修改 | `backend/app/tools/__init__.py`（导出 3 个新 Tool 类） |
| 修改 | `backend/config/prompts/agent_fc.yaml`（v1 → v2） |
| 测试 | `backend/tests/test_after_sales_tool.py`（6 用例） |
| 测试 | `backend/tests/test_promotion_tool.py`（5 用例） |
| 测试 | `backend/tests/test_order_modify_tool.py`（6 用例，含 confirmed 验证） |
| 测试 | `backend/tests/test_tool_registry.py`（13 用例，每个 ToolSpec 1 个 dispatch 测试）|

**总文件：10 个**（3 Tool + 1 修改 registry + 1 修改 __init__ + 1 修改 prompt + 4 测试）

**禁止事项**：
- ❌ 不允许改 Service 层（V2 仅 Tool 适配，Service 接口已稳定）
- ❌ 不允许改 Agent runner（V2 仅 Tool 注册，Agent 走 to_openai_tools() 自动获得新 Tool）
- ❌ 不允许在 Tool 类直接 import models.*（走 Service Protocol）
- ❌ 不允许复制 run_sync（统一从 app.services.order.factory import）
- ❌ 不允许跳过 confirmed 机制（写操作默认 confirmed=False）

---

## 6. 验证标准

| 项 | 标准 |
|----|------|
| 新增测试 | 6 + 5 + 6 + 13 = **30 用例 PASS** |
| 全量回归 | baseline + 30（无新失败）|
| 13 Tool 全部注册 | `len(REGISTRY) == 13` |
| 写操作 confirmed 默认 False | 单元测试：OrderModifyTool.modify_address(..., confirmed=False) → 返 needs_confirmation |
| prompt 更新 | agent_fc.yaml v2 包含 13 工具 + 写操作确认机制 |
| LLM 可识别 | to_openai_tools() 输出 13 个 function 描述 |
| 模块隔离 | `grep "from app.models" app/tools/{after_sales,promotion,order_modify}_tool.py` = 0（走 Service） |

---

## 7. 8 件套交付（CLAUDE.md §9.8）

1. 模块职责：learning_log §63「Tool 适配层 · 9 Service 接 Agent FC」
2. 接口契约：本 spec §2（3 Tool 类）
3. 输入输出模型：本 spec §3（13 ToolSpec）
4. ORM / 数据模型：复用现有（Sprint 18 已有）
5. 依赖关系：Tool → Service Protocol（不直连 ORM）
6. 调用流程：Agent → dispatch → Tool → Service → DTO → dict → LLM
7. 测试方案：本 spec §6
8. 已知限制：
   - 写操作 2 步确认依赖 LLM 正确解析 needs_confirmation prompt（V3+ 可加 Agent 层硬约束）
   - 13 个 Tool 可能让 LLM 选错（V2 测试观察；V3+ 按需精简）
   - agent_fc.yaml v2 灰度开关沿用 ENABLE_AGENT_FC（V2 默认 False 不影响生产）

---

## 8. 集成计划

1. subagent 在 worktree 实施（commit hash 待集成时记录）
2. 主 agent 在 master 用 `git checkout <commit> -- <files>` 按文件粒度集成（避开 learning_log 冲突）
3. 主 agent 手工追加 §63 到 learning_log 末尾
4. 全量 pytest 验证（baseline + 30）
5. commit `feat(arch): S19 9 Service 接 Agent FC · 写操作 2 步确认`
6. push 双 remote

---

## 9. 后续 Sprint 路线

| Sprint | 目标 | 依赖 |
|--------|------|------|
| **S19（本 spec）** | 9 Service → Tool + Agent FC 注册 | S18 ✓ |
| S20 | 数据导入 / 知识库初始化 | S17 ✓ |
| S21 | 业务广度补 40%（评价类/投诉升级/账户类/发票） | S18 ✓ |
| V3+ | LLM 选 Tool 准确率优化（按真实对话数据回归） | S19 ✓ |

S19 完成后，13 个 Tool 全部就绪；Agent 可自动处理售前/售中/售后高频场景；写操作 2 步确认机制就位。