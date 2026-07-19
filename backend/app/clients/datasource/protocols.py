"""
DataSource Protocol — 业务数据源统一访问接口

按 CLAUDE.md §9.3 Interface First + §9.7 自检 5 问设计：
  Q1：业务模块依赖此 Protocol，不依赖具体实现（StaticSeedSource / TaobaoAdapter / MockSource）
  Q2：单一职责（数据源访问）；不调 LLM / 不做 RAG
  Q3：Protocol 先于具体实现（先定义签名）
  Q4：新模块，不破现有接口
  Q5：MockSource 实现可独立单测，不依赖 MySQL/Redis

调用方（已规划 / M15+）：
  - OrderService.list_user_orders / get_order_detail
  - ProductService.list_products / get_by_sku
  - ProfileService.get_user_profile
  - ConversationService（未来查用户上下文）

当前（M14 V3）状态：业务层仍走 ProductTool/OrderTool 直连 MySQL；
DataSource 层作为"扩展点"预留，避免每次新加平台（如 TaobaoAdapter）时改 N 处业务代码。
"""
from typing import AsyncIterator, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class DataSourceProtocol(Protocol):
    """
    业务数据源统一接口

    设计原则（CLAUDE.md §7.3 / §9.3）：
    - 输入参数必须显式收 user_id 等上下文（禁止"查全部再过滤"）
    - 返回 dict 列表而非 ORM 模型（避免暴露 ORM 细节）
    - 所有 fetch_* 方法都是同步（M15+ 才考虑 async）
    - subscribe_webhook 是 async（外部 webhook 推送场景）

    方法说明：
    - fetch_products：商品查询；sku=None 返回全量
    - fetch_orders：订单查询；必须 user_id 显式收（防越权）
    - fetch_user_profile：用户档案
    - search_products_by_keyword：商品全文搜索（备用 · M15+ RAG 改造时启用）
    - subscribe_webhook：订阅外部平台推送（M18+ Taobao webhook）
    """

    # ---------- 商品 ----------

    def fetch_products(
        self,
        *,
        sku: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """
        查询商品

        Args:
            sku: 商品 SKU（精确匹配）；None=不限制
            category: 类目名（手机/耳机 等）；None=不限制
            limit: 最大返回数

        Returns:
            商品 dict 列表；空列表表示无匹配

        单条商品字段：
            {
                "sku": "SKU001",
                "name": "ZP1 旗舰手机",
                "price": 3999.0,
                "category": "手机",
                "stock": 100,
                "status": 1,
                "attributes": {...},  # JSON 字段（如有）
            }
        """
        ...

    def search_products_by_keyword(
        self,
        keyword: str,
        *,
        limit: int = 10,
    ) -> List[dict]:
        """
        商品关键词搜索（name LIKE 实现）

        Returns:
            商品 dict 列表（同 fetch_products schema）
        """
        ...

    # ---------- 订单 ----------

    def fetch_orders(
        self,
        *,
        user_id: int,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """
        查询用户的订单

        Args:
            user_id: 用户 ID（必传；防越权）
            status: 订单状态过滤（pending/paid/shipped/delivered/completed/refunded）
            limit: 最大返回数

        Returns:
            订单 dict 列表，按 create_time DESC

        单条订单字段：
            {
                "order_no": "ORD20260615004",
                "status": "shipped",
                "total_amount": 3999.0,
                "create_time": "2026-06-15T10:30:00",
            }
        """
        ...

    def fetch_user_orders_with_logistics(
        self,
        *,
        user_id: int,
        order_no: str,
    ) -> Optional[dict]:
        """
        查订单详情（含明细 + 物流 mock）

        Returns:
            {
                "order": {...},
                "items": [...],
                "logistics": {...},
            }
            或 None（订单不存在 / 不属于该 user）
        """
        ...

    # ---------- 用户 ----------

    def fetch_user_profile(self, *, user_id: int) -> Optional[dict]:
        """
        查询用户档案

        Returns:
            用户 dict（含 username / role / email 等公开字段；不含 password_hash）
            或 None（用户不存在）
        """
        ...

    # ---------- 自更新（M18+ · 当前方法体 raise NotImplementedError）----------

    def subscribe_webhook(self, event_type: str) -> AsyncIterator[dict]:
        """
        订阅外部平台 webhook（M18+ 接 Taobao Open API 时启用）

        当前实现 = StaticSeedSource：raise NotImplementedError（占位 + YAGNI）
        M18+ 实现 = TaobaoAdapter：拉淘宝订单变化事件

        Args:
            event_type: 事件类型（如 "trade.create" / "trade.update"）

        Yields:
            webhook payload（dict）

        Why 占位：
            当前阶段（V3.2）只规划文档 + 接口；实现留到 M18+ 真接入淘宝时。
            保留签名可在 Protocol 层面给"自更新 Agent"提供稳定 contract。
        """
        ...
