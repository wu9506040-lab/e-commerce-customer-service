"""
订单生命周期服务 - 状态机 + 业务校验

按 CLAUDE.md §5 Scope Lock：
- services/ 做业务编排（调 tool + 校验状态机）
- api/ 只调本服务，不写业务逻辑

状态机：
    pending → paid → shipped → delivered → completed
                                              ↘ refunded
    paid / shipped → refunded（用户申请退款）

每次状态流转校验：
1. 订单存在且属于当前 user（越权防护）
2. 当前状态是合法的 from 状态
3. 必要时插入 refund 记录
4. 写 orders 表 + update_time 自动更新
"""
import datetime
import logging
import uuid
from typing import Optional

from sqlalchemy import select

from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.refund import Refund, RefundStatus

logger = logging.getLogger(__name__)

# 业务常量：从「下单时间」到「默认签收时间」的天数偏移
# 用于 delivered 订单的 7 天无理由窗口计算（实际签收时间 = 下单时间 + 偏移天数）
# 业务含义：演示场景里没有真实物流系统，假设平均 2 天送达
DELIVERY_OFFSET_DAYS = 2


class OrderLifecycleError(Exception):
    """订单状态流转业务错误（携带 message 给前端）"""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class OrderLifecycle:
    """订单状态机服务（封装所有状态流转业务逻辑）"""

    # =============================================================
    # 下单（创建订单，初始状态 pending）
    # =============================================================
    @staticmethod
    def create_order(user_id: int, sku: str, qty: int = 1) -> dict:
        """
        下单（创建订单，初始状态 pending）

        Args:
            user_id: 用户 ID（必传，从 session 取）
            sku: 商品 SKU
            qty: 数量（默认 1）

        Returns:
            新订单 dict（OrderTool 格式）

        Raises:
            OrderLifecycleError: 商品不存在/已下架/库存不足
        """
        if qty <= 0:
            raise OrderLifecycleError("数量必须大于 0")

        with with_safe_session(commit=True) as db:
            product = db.scalar(
                select(Product).where(
                    Product.sku == sku,
                    Product.deleted == 0,
                    Product.status == 1,  # 1=上架
                )
            )
            if product is None:
                raise OrderLifecycleError(f"商品 {sku} 不存在或已下架", status_code=404)
            if product.stock < qty:
                raise OrderLifecycleError(
                    f"库存不足（{product.stock} < {qty}），请减少数量"
                )

            # 扣库存
            product.stock -= qty

            # 生成订单号（ORD + 年月日 + 6位随机）
            order_no = f"ORD{datetime.datetime.now().strftime('%Y%m%d')}{uuid.uuid4().hex[:6].upper()}"
            now = datetime.datetime.now()

            order = Order(
                order_no=order_no,
                user_id=user_id,
                status=OrderStatus.PENDING.value,
                total_amount=float(product.price) * qty,
                create_time=now,
                update_time=now,
                deleted=0,
            )
            db.add(order)
            db.flush()

            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                sku=product.sku,
                product_name=product.name,
                qty=qty,
                unit_price=float(product.price),
                subtotal=float(product.price) * qty,
                create_time=now,
                update_time=now,
                deleted=0,
            )
            db.add(item)

            logger.info(f"[create_order] user={user_id} order={order_no} sku={sku} qty={qty}")

            return {
                "order_no": order.order_no,
                "status": order.status,
                "total_amount": float(order.total_amount),
                "create_time": order.create_time.isoformat() if order.create_time else None,
            }

    # =============================================================
    # 内部工具：取订单（带越权防护）
    # =============================================================
    @staticmethod
    def _get_order(user_id: int, order_no: str):
        """取订单 row + 校验归属（user_id 强过滤，越权防护）"""
        with with_safe_session(commit=False) as db:
            order = db.scalar(
                select(Order).where(
                    Order.order_no == order_no,
                    Order.user_id == user_id,
                    Order.deleted == 0,
                )
            )
            if order is None:
                raise OrderLifecycleError(
                    "订单不存在或不属于当前用户",
                    status_code=404,
                )
            return order

    @staticmethod
    def _update_status(order_no: str, new_status: str) -> None:
        """原子更新订单状态"""
        with with_safe_session(commit=True) as db:
            order = db.scalar(select(Order).where(Order.order_no == order_no))
            if order is None:
                raise OrderLifecycleError("订单不存在", status_code=404)
            order.status = new_status
            order.update_time = datetime.datetime.now()
            logger.info(f"[status] order={order_no} → {new_status}")

    # =============================================================
    # pending → paid（付款）
    # =============================================================
    @staticmethod
    def pay_order(user_id: int, order_no: str, role: str = "user") -> dict:
        """付款（pending → paid）

        Args:
            role: 用户角色。visitor 体验账号禁止付款（403）。
        """
        if role == "visitor":
            raise OrderLifecycleError(
                "体验账号不支持付款。如需完整体验，请注册正式账号。",
                status_code=403,
            )
        order = OrderLifecycle._get_order(user_id, order_no)
        if order.status != OrderStatus.PENDING.value:
            raise OrderLifecycleError(
                f"订单当前状态为「{order.status}」，无法付款（只有待付款订单可付款）",
                status_code=409,
            )
        OrderLifecycle._update_status(order_no, OrderStatus.PAID.value)
        return {"order_no": order_no, "status": OrderStatus.PAID.value}

    # =============================================================
    # paid → shipped（发货，演示场景允许用户触发）
    # =============================================================
    @staticmethod
    def ship_order(user_id: int, order_no: str, role: str = "user") -> dict:
        """发货（paid → shipped）

        Args:
            role: 用户角色。visitor 体验账号禁止发货（403）。
        """
        if role == "visitor":
            raise OrderLifecycleError(
                "体验账号不支持发货操作。如需完整体验，请注册正式账号。",
                status_code=403,
            )
        order = OrderLifecycle._get_order(user_id, order_no)
        if order.status != OrderStatus.PAID.value:
            raise OrderLifecycleError(
                f"订单当前状态为「{order.status}」，无法发货（只有已付款订单可发货）",
                status_code=409,
            )
        OrderLifecycle._update_status(order_no, OrderStatus.SHIPPED.value)
        return {"order_no": order_no, "status": OrderStatus.SHIPPED.value}

    # =============================================================
    # shipped → delivered（确认签收）
    # =============================================================
    @staticmethod
    def confirm_order(user_id: int, order_no: str, role: str = "user") -> dict:
        """确认签收（shipped → delivered）

        Args:
            role: 用户角色。visitor 体验账号禁止签收（403）。
        """
        if role == "visitor":
            raise OrderLifecycleError(
                "体验账号不支持确认签收。如需完整体验，请注册正式账号。",
                status_code=403,
            )
        order = OrderLifecycle._get_order(user_id, order_no)
        if order.status != OrderStatus.SHIPPED.value:
            raise OrderLifecycleError(
                f"订单当前状态为「{order.status}」，无法确认签收（只有运输中订单可签收）",
                status_code=409,
            )
        OrderLifecycle._update_status(order_no, OrderStatus.DELIVERED.value)
        return {"order_no": order_no, "status": OrderStatus.DELIVERED.value}

    # =============================================================
    # * → refunded（申请退款）— 同时插入 refund 记录
    # =============================================================
    @staticmethod
    def refund_order(
        user_id: int,
        order_no: str,
        reason: str = "用户申请退款",
        remark: Optional[str] = None,
        role: str = "user",
    ) -> dict:
        """
        申请退款（任意状态 → refunded）

        规则：
        - paid / shipped / delivered / completed → 可申请（但 completed 超 7 天不允许）
        - pending（待付款）→ 不能"退款"，直接取消订单即可（暂不实现取消）
        - refunded → 不可重复
        - role=visitor 体验账号禁止退款（403）— AI 客服 LangGraph 退款演示用真实订单号仍可走

        Returns:
            {"order_no", "status": "refunded", "refund_no": "RF..."}
        """
        if role == "visitor":
            raise OrderLifecycleError(
                "体验账号不持有真实订单，无法发起退款。如需完整体验，请注册正式账号。",
                status_code=403,
            )
        order = OrderLifecycle._get_order(user_id, order_no)

        # 状态校验
        if order.status == OrderStatus.REFUNDED.value:
            raise OrderLifecycleError(
                "该订单已退款，无法重复申请",
                status_code=409,
            )
        if order.status == OrderStatus.PENDING.value:
            raise OrderLifecycleError(
                "待付款订单暂未扣款，无需退款。如需取消，请直接放弃支付。",
                status_code=409,
            )
        if order.status == OrderStatus.COMPLETED.value:
            # 已完成订单：7 天无理由窗口已过
            raise OrderLifecycleError(
                "该订单已完成且超过 7 天无理由退货期限，无法在线退款。如需售后请联系人工客服。",
                status_code=409,
            )

        # delivered 订单：校验 7 天窗口（按"已下单+DELIVERY_OFFSET_DAYS 天"作为签收日推算）
        if order.status == OrderStatus.DELIVERED.value:
            if order.create_time:
                delivery_time = order.create_time + datetime.timedelta(days=DELIVERY_OFFSET_DAYS)
                days_since_delivery = (datetime.datetime.now() - delivery_time).days
                if days_since_delivery > 7:
                    raise OrderLifecycleError(
                        f"该订单已签收 {days_since_delivery} 天，超过 7 天无理由退货期限，无法在线退款。"
                        "请联系人工客服协助。",
                        status_code=409,
                    )

        # 创建 refund 记录 + 更新订单状态
        refund_no = f"RF{datetime.datetime.now().strftime('%Y%m%d')}{uuid.uuid4().hex[:6].upper()}"
        now = datetime.datetime.now()

        with with_safe_session(commit=True) as db:
            # 双重校验（防止并发）
            order_row = db.scalar(
                select(Order).where(
                    Order.order_no == order_no,
                    Order.user_id == user_id,
                    Order.deleted == 0,
                ).with_for_update()
            )
            if order_row is None:
                raise OrderLifecycleError("订单不存在", status_code=404)
            if order_row.status == OrderStatus.REFUNDED.value:
                raise OrderLifecycleError("该订单已退款", status_code=409)

            refund = Refund(
                refund_no=refund_no,
                order_id=order_row.id,
                user_id=user_id,
                reason=reason,
                status=RefundStatus.COMPLETED.value,  # 演示场景直接完成
                amount=float(order_row.total_amount),
                remark=remark,
                create_time=now,
                update_time=now,
                deleted=0,
            )
            db.add(refund)

            order_row.status = OrderStatus.REFUNDED.value
            order_row.update_time = now

        logger.info(
            f"[refund] user={user_id} order={order_no} refund_no={refund_no} "
            f"amount={order.total_amount}"
        )

        return {
            "order_no": order_no,
            "status": OrderStatus.REFUNDED.value,
            "refund_no": refund_no,
        }
