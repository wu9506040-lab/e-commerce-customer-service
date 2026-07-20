# Sprint 20 通用客服中台 · 三家共有场景补完 spec（2026-07-21）

> **目的**：CLAUDE.md §1 项目身份"电商智能客服 Agent"明确服务对象是淘宝/京东/拼多多这类大型电商平台。Sprint 18 已补 9 个高频场景（售后/售前/售中），但还差 9 个**三家平台共有**的真实场景。
>
> **本 Sprint 删掉的场景**（用户反馈不会出现在客服系统）：
> - 好评返现 / 删差评 —— 私下操作，客服不介入
> - 会员等级 / 积分 —— 账户系统自己处理，客服不管
>
> **本 Sprint 暂不做的场景**（平台特色，非三家共有）：
> - 京东保价 / 京东特色 30 天价保
> - 上门取件预约（京东自营强 / 淘宝弱 —— 但作为通用流程保留方法签名）
> - 企业增值税专用发票（低频，企业用户专用）

---

## 1. 总览

### 1.1 9 个新场景（按 4 个 Service 组织）

| Service | 场景数 | 场景列表 | 类型 |
|---------|--------|----------|------|
| `SupplementRuleService`（售前+售中补完）| 5 | 运费规则 / 限购规则 / 催发货 / 延长收货 / 上门取件预约 | 规则 + OrderService |
| `DisputeService`（售后纠纷）| 3 | 质量问题鉴定流程 / 平台介入检查 / 举报假货流程 | 规则 + OrderService |
| `InvoiceService`（发票）| 1 | 发票申请资格检查（满额 + 普通vs电子）| 规则 + OrderService |

**3 个新模块 + 4 个 YAML + 9 DTO + 25 测试**

### 1.2 完整业务场景覆盖度

| 阶段 | 进度 |
|------|------|
| S18 后 | ~60% |
| **S20 后** | **~85%**（三家共有客服场景基本完整） |
| 待 V3+ | 京东特色（保价）/ 拼多多特色（仅退款）/ 企业发票 / 跨平台适配 |

---

## 2. 售前 + 售中补完（SupplementRuleService · 5 场景）

### 2.1 接口契约（`app/services/supplement/protocols.py`）

```python
@runtime_checkable
class SupplementRuleService(Protocol):
    """售前+售中补完规则（CLAUDE.md §9.3.2 支持模块替换）

    5 个场景：运费 / 限购 / 催发货 / 延长收货 / 上门取件预约
    业务规则从 config/business_rules/supplement.yaml 加载。
    """

    # ===== 售前补完 =====

    async def get_shipping_fee(
        self, address_region: str, item_count: int, total_amount: float,
        payment_method: str,
    ) -> ShippingFeeInfo:
        """运费计算
        
        address_region: "华北"/"华东"/"华南"/"西部"/"东北"/"偏远"
        item_count: 商品件数
        total_amount: 订单金额
        payment_method: "online"/"cod"（货到付款）
        
        规则：满 X 元包邮 / 首件 Y 元 / 续件 Z 元 / 偏远地区加价
        """
        ...

    async def get_purchase_limit_info(
        self, sku: str, user_id: int,
    ) -> PurchaseLimitInfo:
        """限购规则
        
        返回：该商品是否限购 / 限购数量 / 活动期间限购 / 历史购买次数
        """
        ...

    # ===== 售中补完 =====

    async def urge_shipment(
        self, user_id: int, order_no: str,
    ) -> UrgeShipmentResult:
        """催发货
        
        规则：商家承诺发货时间 / 已催过几次 / 下次可催时间
        实际催发货 = 写操作？V2 仅返"催发货规则 + 建议"
        """
        ...

    async def extend_receipt(
        self, user_id: int, order_no: str, extend_days: int,
    ) -> ExtendReceiptResult:
        """延长收货时间
        
        规则：仅"已发货未确认"可延长 / 单次最长 7 天 / 累计不超过 15 天
        V2 仅返资格检查 + 规则说明
        """
        ...

    async def schedule_pickup(
        self, order_no: str, pickup_address: str, time_slot: str,
    ) -> SchedulePickupResult:
        """上门取件预约（退货物流）
        
        time_slot: "morning" / "afternoon" / "evening"
        V2 仅返可预约时段 + 注意事项
        """
        ...
```

### 2.2 Schema（追加到 `app/schemas/business.py`）

```python
class ShippingFeeInfo(BaseModel):
    """运费计算结果"""
    base_fee: float                    # 首件运费
    additional_fee: float              # 续件运费
    total_fee: float                   # 合计运费
    free_shipping_threshold: float     # 包邮门槛
    is_free_shipping: bool             # 是否包邮
    remote_area_surcharge: float       # 偏远地区加价
    notes: List[str]                   # 备注


class PurchaseLimitInfo(BaseModel):
    """限购规则"""
    sku: str
    limited: bool                      # 是否限购
    max_quantity: Optional[int] = None # 限购数量
    activity_limited: bool             # 活动期间限购
    user_purchase_count: Optional[int] = None  # 用户已购次数
    remaining_quota: Optional[int] = None     # 剩余可购


class UrgeShipmentResult(BaseModel):
    """催发货结果（V2 仅规则说明）"""
    order_no: str
    order_status: str                  # 订单当前状态
    promised_ship_time: Optional[datetime]  # 商家承诺发货时间
    urged_count: int                   # 已催次数
    next_urge_available: Optional[datetime]  # 下次可催时间
    tips: List[str]                    # 催发货技巧


class ExtendReceiptResult(BaseModel):
    """延长收货结果（V2 仅资格检查）"""
    eligible: bool                     # 是否可延长
    max_extension_days: int            # 单次最长可延长
    remaining_extension_days: int      # 剩余可延长天数
    current_status: str                # 订单当前状态
    reason: str                        # 中文原因（不可延长时填）


class SchedulePickupResult(BaseModel):
    """上门取件预约结果"""
    available: bool
    available_time_slots: List[str]    # 可预约时段
    pickup_fee: float                  # 上门取件费
    notes: List[str]                   # 注意事项
```

### 2.3 YAML 配置（`config/business_rules/supplement.yaml`）

```yaml
SHIPPING_RULES:
  free_shipping_threshold: 99.0       # 满 99 包邮
  base_fee_per_region:
    华北: 10.0
    华东: 8.0
    华南: 12.0
    西部: 15.0
    东北: 12.0
    偏远: 25.0
  additional_fee_per_item: 2.0       # 续件每件 2 元
  cod_surcharge: 5.0                  # 货到付款加 5 元
  remote_area_keywords: ["新疆", "西藏", "内蒙古"]


PURCHASE_LIMITS:
  default_max_per_user: 5             # 默认每人限购 5 件
  activity_limited_skus:              # 活动期间限购 SKU
    - "PROMO_2026_D11_001"
    - "PROMO_2026_D11_002"
  activity_max_per_user: 1            # 活动期间每人限购 1 件


URGE_SHIPMENT_RULES:
  promised_ship_hours: 48             # 商家承诺 48 小时内发货
  max_urge_per_day: 2                 # 每天最多催 2 次
  next_urge_interval_hours: 12        # 两次催发货间隔 12 小时


EXTEND_RECEIPT_RULES:
  eligible_statuses: ["shipped"]      # 仅"已发货"可延长
  max_days_per_extension: 7           # 单次最长 7 天
  max_days_total: 15                  # 累计不超过 15 天


SCHEDULE_PICKUP_RULES:
  available_time_slots: ["morning", "afternoon", "evening"]
  pickup_fee: 0.0                     # 京东自营免费 / 淘宝卖家承担
  advance_booking_hours: 24           # 至少提前 24 小时预约
```

### 2.4 测试用例（`backend/tests/test_supplement_protocol.py`）

| # | 用例 | 验证 |
|---|------|------|
| 1 | get_shipping_fee 满 99 包邮 | total_amount=99 → is_free_shipping=True |
| 2 | get_shipping_fee 偏远地区加价 | address_region="西部" → remote_area_surcharge>0 |
| 3 | get_shipping_fee 货到付款加价 | payment_method="cod" → base_fee+5 |
| 4 | get_purchase_limit_info 普通商品 | limited=True, max_quantity=5 |
| 5 | get_purchase_limit_info 活动商品 | activity_limited=True, max_quantity=1 |
| 6 | urge_shipment 待发货订单 | 返 promised_ship_time + urged_count |
| 7 | extend_receipt shipped 状态 + 5 天 | eligible=True, max=7 |
| 8 | extend_receipt 已签收状态 | eligible=False, reason="已签收不可延长" |
| 9 | schedule_pickup 提前 24 小时 | available=True, 3 时段 |
| 10 | schedule_pickup 提前不足 24 小时 | available=False |

**10 用例**

---

## 3. 售后纠纷（DisputeService · 3 场景）

### 3.1 接口契约（`app/services/dispute/protocols.py`）

```python
@runtime_checkable
class DisputeService(Protocol):
    """售后纠纷处理（CLAUDE.md §9.3.2 支持模块替换）

    3 个场景：质量问题鉴定 / 平台介入 / 举报假货
    业务流程从 config/business_rules/dispute.yaml 加载。
    """

    async def get_quality_dispute_process(
        self, user_id: int, order_no: str,
    ) -> QualityDisputeProcess:
        """质量问题鉴定流程
        
        返回：举证责任方 / 举证时限 / 处理步骤 / 上诉渠道
        """
        ...

    async def check_platform_intervene_eligibility(
        self, user_id: int, order_no: str, dispute_type: str,
    ) -> PlatformInterveneCheck:
        """平台介入条件检查
        
        dispute_type: "refund_rejected"/"no_response"/"partial_refund"
        检查：是否已与卖家沟通 N 次 / 是否有聊天记录 / 时限内
        """
        ...

    async def get_report_fake_goods_process(
        self, order_no: str,
    ) -> ReportFakeGoodsProcess:
        """举报假货流程
        
        返回：举报路径 / 需要证据 / 处罚措施 / 处理时限
        V2 仅规则说明，不实际举报
        """
        ...
```

### 3.2 Schema

```python
class QualityDisputeProcess(BaseModel):
    """质量问题鉴定流程"""
    order_no: str
    burden_of_proof: str               # "buyer" / "seller"（谁举证）
    evidence_required: List[str]       # 需要的证据
    evidence_deadline_hours: int       # 举证时限
    process_steps: List[str]           # 处理步骤
    appeal_channels: List[str]         # 上诉渠道


class PlatformInterveneCheck(BaseModel):
    """平台介入条件检查"""
    eligible: bool
    order_no: str
    dispute_type: str
    reason: str                        # 中文原因
    required_conditions: List[str]    # 所需条件列表
    consequences: List[str]            # 介入后果（卖家/买家）


class ReportFakeGoodsProcess(BaseModel):
    """举报假货流程"""
    order_no: str
    report_channels: List[str]         # 举报渠道
    evidence_required: List[str]       # 需要的证据
    possible_penalties: List[str]      # 卖家可能处罚
    processing_days: int               # 处理时限
    notes: List[str]                   # 注意事项
```

### 3.3 YAML 配置（`config/business_rules/dispute.yaml`）

```yaml
QUALITY_DISPUTE_RULES:
  # 举证责任
  burden_of_proof_by_value:
    high_value_threshold: 1000.0       # 高价值（>1000）卖家举证
    low_value_burden: "buyer"          # 低价值（≤1000）买家举证
  
  # 举证时限
  evidence_deadline_hours: 48         # 需在 48 小时内举证
  
  # 通用处理步骤
  process_steps:
    - "买家提交问题描述 + 证据"
    - "卖家举证（48 小时内）"
    - "平台审核（3-5 工作日）"
    - "判定结果通知双方"
  
  # 上诉渠道
  appeal_channels:
    - "客服热线"
    - "在线申诉入口"
    - "12315 消费者协会"


PLATFORM_INTERVENE_RULES:
  # 必须满足的最低条件
  min_communication_rounds: 3         # 至少沟通 3 轮
  must_have_chat_log: true            # 必须有聊天记录
  deadline_hours: 168                 # 订单完成后 7 天内可介入
  
  # 不同纠纷类型的条件
  dispute_type_requirements:
    refund_rejected:
      - "已申请退款被拒绝"
      - "有聊天记录证明已沟通"
    no_response:
      - "卖家 48 小时未回应"
      - "有 2 次及以上沟通记录"
    partial_refund:
      - "已收到部分退款但金额不符"
      - "有沟通记录"
  
  # 介入后果
  consequences:
    buyer_side:
      - "若举证不足，可能败诉"
      - "影响后续维权"
    seller_side:
      - "若举证不足，可能被处罚"
      - "影响店铺评分"


REPORT_FAKE_GOODS_RULES:
  report_channels:
    - "平台举报入口"
    - "客服协助举报"
    - "12315 平台"
  
  evidence_required:
    - "商品对比图（真品 vs 收到的）"
    - "权威鉴定报告（可选但加分）"
    - "购买凭证"
    - "聊天记录"
  
  possible_penalties:
    - "商品下架"
    - "店铺扣分"
    - "店铺封禁"
    - "消费者退款 + 赔偿"
  
  processing_days: 7                  # 7 个工作日内处理
```

### 3.4 测试用例（`backend/tests/test_dispute_protocol.py`）

| # | 用例 | 验证 |
|---|------|------|
| 1 | get_quality_dispute_process 高价值订单 | burden_of_proof="seller" |
| 2 | get_quality_dispute_process 低价值订单 | burden_of_proof="buyer" |
| 3 | check_platform_intervene_eligibility 满足条件 | eligible=True |
| 4 | check_platform_intervene_eligibility 沟通 < 3 轮 | eligible=False |
| 5 | get_report_fake_goods_process 完整流程 | 返 channels + evidence + penalties |
| 6 | get_report_fake_goods_process 处理时限 | processing_days=7 |

**6 用例**

---

## 4. 发票（InvoiceService · 1 场景）

### 4.1 接口契约（`app/services/invoice/protocols.py`）

```python
@runtime_checkable
class InvoiceService(Protocol):
    """发票申请服务（CLAUDE.md §9.3.2 支持模块替换）

    V2 范围：仅发票资格检查 + 流程说明，不实际开具发票。
    实际开票 = 接入企业 ERP / 税控系统（V3+ 留）。
    """

    async def check_invoice_eligibility(
        self, user_id: int, order_no: str, invoice_type: str,
    ) -> InvoiceEligibility:
        """发票申请资格检查
        
        invoice_type: "personal_paper" / "personal_electronic" / "company_special"
        检查：满额条件 / 订单状态 / 企业资质（公司专票）
        """
        ...
```

### 4.2 Schema

```python
class InvoiceEligibility(BaseModel):
    """发票申请资格"""
    eligible: bool
    order_no: str
    invoice_type: str
    invoice_title_required: bool        # 是否需要填抬头
    tax_id_required: bool              # 是否需要税号
    amount_threshold_met: bool         # 是否满足满额条件
    minimum_amount: float              # 最低开票金额
    current_order_amount: float        # 当前订单金额
    eligible_amount: float             # 可开票金额
    application_url: str               # 申请入口 URL
    notes: List[str]                   # 注意事项（如"纸质发票邮费自理"）
```

### 4.3 YAML 配置（`config/business_rules/invoice.yaml`）

```yaml
INVOICE_RULES:
  # 最低开票金额
  minimum_amount: 50.0                # 订单满 50 元可开票
  
  # 各类型发票要求
  invoice_type_requirements:
    personal_paper:
      title_required: false           # 个人无需填抬头
      tax_id_required: false          # 个人无需税号
      shipping_fee_borne_by: "buyer"  # 邮费买家承担
      processing_days: 7
    personal_electronic:
      title_required: false
      tax_id_required: false
      shipping_fee_borne_by: "none"   # 电子发票无邮费
      processing_days: 1
    company_special:
      title_required: true            # 公司专票必须填抬头
      tax_id_required: true           # 必须填税号
      shipping_fee_borne_by: "buyer"
      processing_days: 10
      additional_requirements:
        - "公司资质证明"
        - "开户许可证"
  
  # 通用说明
  notes:
    - "发票申请需在订单完成后 30 天内"
    - "纸质发票邮费由买家承担"
    - "电子发票发送至用户邮箱"
```

### 4.4 测试用例（`backend/tests/test_invoice_protocol.py`）

| # | 用例 | 验证 |
|---|------|------|
| 1 | check_invoice_eligibility 个人电子发票 + 满 50 元 | eligible=True, tax_id_required=False |
| 2 | check_invoice_eligibility 个人电子发票 + 不足 50 元 | eligible=False, amount_threshold_met=False |
| 3 | check_invoice_eligibility 公司专票 + 满 50 元 | eligible=True, tax_id_required=True |
| 4 | check_invoice_eligibility 申请 URL 完整 | application_url 非空 |

**4 用例**

---

## 5. 文件边界（强制 · 防越权）

| 模块 | 文件 |
|------|------|
| 售前+售中补完 | `app/services/supplement/{protocols,yaml_impl,factory,__init__}.py`（4 文件）|
| 售后纠纷 | `app/services/dispute/{protocols,yaml_impl,factory,__init__}.py`（4 文件）|
| 发票 | `app/services/invoice/{protocols,yaml_impl,factory,__init__}.py`（4 文件）|
| 配置 | `config/business_rules/{supplement,dispute,invoice}.yaml`（3 文件）|
| Schema | `app/schemas/business.py`（追加 9 DTO · + ~140 行）|
| 测试 | `test_supplement_protocol.py`(10) + `test_dispute_protocol.py`(6) + `test_invoice_protocol.py`(4) = **20 用例** |

**总文件：15 个**（3 Service × 4 + 3 YAML + 1 schemas 修改 + 3 测试）

**禁止事项**：
- ❌ 不允许新增数据库表（所有规则从 YAML 读）
- ❌ 不允许复制 run_sync（统一从 `app.services.order.factory` import）
- ❌ 不允许在 supplement/dispute/invoice 模块 import channels / rag / agents
- ❌ 不允许实际写操作（催发货/延长收货/上门取件 V2 仅返规则说明）
- ❌ 不允许实际开发票（V2 仅资格检查）

---

## 6. 验证标准

| 项 | 标准 |
|----|------|
| 新增测试 | 10 + 6 + 4 = **20 用例 PASS** |
| 全量回归 | baseline + 20（无新失败）|
| YAML 加载 | `get_config_loader()` 启动期一次加载 |
| run_sync 复用 | `grep "def run_sync" app/services/{supplement,dispute,invoice}/` = 0 |
| 模块隔离 | `grep "from app.channels\|from app.rag" app/services/{supplement,dispute,invoice}/` = 0 |
| 三家共有 | 9 个 Service 方法都从"淘宝/京东/拼多多共有"角度设计 |

---

## 7. 8 件套交付（CLAUDE.md §9.8）

1. 模块职责：learning_log §64「通用客服中台 · 三家共有场景补完」
2. 接口契约：本 spec §2.1 + §3.1 + §4.1
3. 输入输出模型：本 spec §2.2 + §3.2 + §4.2
4. ORM / 数据模型：无新表，全部 YAML 配置
5. 依赖关系：上游 = 未来 Tool 层；下游 = config_loader（YAML）+ OrderService（订单状态查询）
6. 调用流程：Service.get_xxx → YAML 规则 + OrderService → DTO → 业务层消费
7. 测试方案：本 spec §2.4 + §3.4 + §4.4
8. 已知限制：
   - 催发货 / 延长收货 / 上门取件 V2 仅规则说明，不实际执行（接具体平台时再写）
   - 发票 V2 仅资格检查，不实际开票（接企业 ERP / 税控留 V3+）
   - 京东保价 / 拼多多仅退款 / 企业专票 暂不做（非三家共有或低频）

---

## 8. 集成计划

1. subagent 在 worktree 实施（commit hash 待集成时记录）
2. 主 agent 在 master 用 `git checkout <commit> -- <files>` 按文件粒度集成
3. 主 agent 手工追加 §64 到 learning_log 末尾
4. 全量 pytest 验证（baseline + 20）
5. commit `feat(arch): S20 通用客服中台 · 三家共有场景补完（9 Service）`
6. push 双 remote

---

## 9. 业务场景覆盖度演进

| Sprint | 场景数 | 覆盖度 | 备注 |
|--------|--------|--------|------|
| 起点 | 0 | 0% | 仅基础 RAG |
| S14-S17 | 3 | ~30% | Channel / Order / Knowledge Protocol |
| S18 | 9 | ~60% | 售后/售前/售中基础 9 个 |
| S19 | 9 Tool | + | 9 Service 接 Agent FC |
| **S20（本 spec）** | **9** | **~85%** | **三家共有场景补完** |
| V3+ | - | ~95% | 京东保价/拼多多仅退款/企业票/跨平台适配 |

S20 完成后，"通用客服中台"对淘宝/京东/拼多多三家共有客服能力基本完整。