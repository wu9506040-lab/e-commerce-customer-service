"""
商品 / 订单公开 API - 给前端「商品橱窗」和「我的订单」页用

按 CLAUDE.md §5 Scope Lock：api/ 只做路由，不写业务逻辑
- 复用 backend/app/tools/product_tool.py 和 order_tool.py
- 订单端点强制 get_current_user（越权防护由 OrderTool 内 user_id 过滤保证）

路由前缀：/products /orders（在 main.py 注册）
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
import logging

from app.api.deps import get_current_user
from app.clients.mysql_client import get_db
from app.models.user import User
from app.schemas.shop import (
    CreateOrderRequest,
    LogisticsOut,
    OrderActionResponse,
    OrderDetailOut,
    OrderItemOut,
    OrderListResponse,
    OrderSummaryOut,
    ProductListResponse,
    ProductOut,
    RefundRequest,
)
from app.services.order_lifecycle import OrderLifecycle, OrderLifecycleError
from app.tools.order_tool import OrderTool
from app.tools.product_tool import ProductTool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["shop"])


def _handle_lifecycle_error(e: OrderLifecycleError) -> HTTPException:
    """OrderLifecycleError → HTTPException（统一错误出口）"""
    return HTTPException(status_code=e.status_code, detail=e.message)


# =============================================================
# 商品（公开，无需登录）
# =============================================================
def _product_to_out(p: dict) -> ProductOut:
    """tool 返回 dict → Pydantic；补 cover_url"""
    return ProductOut(
        sku=p["sku"],
        name=p["name"],
        price=p["price"],
        stock=p["stock"],
        attributes=p.get("attributes"),
        description=p.get("description"),
        # 相对路径，前端拼接 base URL（Vite dev / Nginx 都直接 serve /products/）
        cover_url=f"/products/{p['sku']}.svg",
    )


@router.get(
    "/products",
    response_model=ProductListResponse,
    summary="商品列表（公开）",
    description="无需登录。供前端商品橱窗 / 推荐位使用。",
)
def list_products(
    category: str | None = Query(None, description="类目关键词（name LIKE）"),
    limit: int = Query(50, ge=1, le=100),
):
    rows = ProductTool.list_products(category=category, limit=limit)
    products = [_product_to_out(r) for r in rows]
    return ProductListResponse(products=products, total=len(products))


@router.get(
    "/products/{sku}",
    response_model=ProductOut,
    summary="商品详情（公开）",
    responses={404: {"description": "商品不存在"}},
)
def get_product(sku: str):
    p = ProductTool.get_by_sku(sku)
    if p is None:
        raise HTTPException(status_code=404, detail=f"商品 {sku} 不存在")
    return _product_to_out(p)


# =============================================================
# 订单（需要登录，越权防护由 OrderTool 保证）
# =============================================================
def _order_summary(order_dict: dict, item_count: int) -> OrderSummaryOut:
    return OrderSummaryOut(
        order_no=order_dict["order_no"],
        status=order_dict["status"],
        total_amount=order_dict["total_amount"],
        create_time=order_dict.get("create_time"),
        item_count=item_count,
    )


@router.get(
    "/orders/my",
    response_model=OrderListResponse,
    summary="我的订单列表（需登录）",
    description="返回当前用户的所有未删除订单。",
)
def list_my_orders(
    current_user: User = Depends(get_current_user),
    status: str | None = Query(None, description="按状态过滤（pending/paid/...）"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    rows = OrderTool.list_user_orders(
        user_id=current_user.id, status=status, limit=limit
    )
    # 每个订单再查 items 数量（list API 已经 JOIN 过 item_count 但 tool 没暴露，重查一次）
    # V2.x 简化：tool 不暴露 item_count，这里单独查（订单详情页可缓存）
    orders_out: list[OrderSummaryOut] = []
    for o in rows:
        # 复用 get_order_by_no 拿内部 id，再查 items
        # 但 tool 层没暴露 id，用 order_no + user_id 反查
        from app.models.order import Order, OrderItem
        order_row = db.query(Order).filter(
            Order.order_no == o["order_no"],
            Order.user_id == current_user.id,
            Order.deleted == 0,
        ).first()
        if order_row is None:
            continue
        item_count = db.query(OrderItem).filter(
            OrderItem.order_id == order_row.id,
            OrderItem.deleted == 0,
        ).count()
        orders_out.append(_order_summary(o, item_count))

    return OrderListResponse(orders=orders_out, total=len(orders_out))


@router.get(
    "/orders/{order_no}",
    response_model=OrderDetailOut,
    summary="订单详情（需登录）",
    responses={404: {"description": "订单不存在或不属于当前用户"}},
)
def get_order_detail(
    order_no: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 主表（user_id 强过滤，越权防护）
    order_dict = OrderTool.get_order_by_no(current_user.id, order_no)
    if order_dict is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_no} 不存在")

    # 拿 order.id 查 items
    from app.models.order import Order, OrderItem
    order_row = db.query(Order).filter(
        Order.order_no == order_no,
        Order.user_id == current_user.id,
        Order.deleted == 0,
    ).first()
    if order_row is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_no} 不存在")

    item_rows = db.query(OrderItem).filter(
        OrderItem.order_id == order_row.id,
        OrderItem.deleted == 0,
    ).all()
    items = [
        OrderItemOut(
            sku=i.sku,
            product_name=i.product_name,
            qty=i.qty,
            unit_price=float(i.unit_price),
            subtotal=float(i.subtotal),
        )
        for i in item_rows
    ]

    # 物流
    logistics_dict = OrderTool.get_logistics(order_no)
    logistics = LogisticsOut(**logistics_dict) if logistics_dict else None

    return OrderDetailOut(
        order=_order_summary(order_dict, len(items)),
        items=items,
        logistics=logistics,
    )


# =============================================================
# 订单状态流转（闭环 demo 用）
# =============================================================
@router.post(
    "/orders",
    response_model=OrderActionResponse,
    summary="下单（创建订单，初始状态 pending）",
    description="从商品详情页发起下单。需要登录。",
    responses={404: {"description": "商品不存在或已下架"}, 400: {"description": "库存不足"}},
)
def create_order_endpoint(
    payload: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        result = OrderLifecycle.create_order(current_user.id, payload.sku, payload.qty)
    except OrderLifecycleError as e:
        raise _handle_lifecycle_error(e)
    return OrderActionResponse(order_no=result["order_no"], status=result["status"])


@router.post(
    "/orders/{order_no}/pay",
    response_model=OrderActionResponse,
    summary="付款（pending → paid）",
    responses={409: {"description": "状态不允许付款"}, 404: {"description": "订单不存在"}},
)
def pay_order_endpoint(
    order_no: str,
    current_user: User = Depends(get_current_user),
):
    try:
        result = OrderLifecycle.pay_order(current_user.id, order_no, role=current_user.role)
    except OrderLifecycleError as e:
        raise _handle_lifecycle_error(e)
    return OrderActionResponse(**result)


@router.post(
    "/orders/{order_no}/ship",
    response_model=OrderActionResponse,
    summary="发货（paid → shipped，demo 用用户触发）",
    responses={409: {"description": "状态不允许发货"}, 404: {"description": "订单不存在"}},
)
def ship_order_endpoint(
    order_no: str,
    current_user: User = Depends(get_current_user),
):
    try:
        result = OrderLifecycle.ship_order(current_user.id, order_no, role=current_user.role)
    except OrderLifecycleError as e:
        raise _handle_lifecycle_error(e)
    return OrderActionResponse(**result)


@router.post(
    "/orders/{order_no}/confirm",
    response_model=OrderActionResponse,
    summary="确认签收（shipped → delivered）",
    responses={409: {"description": "状态不允许签收"}, 404: {"description": "订单不存在"}},
)
def confirm_order_endpoint(
    order_no: str,
    current_user: User = Depends(get_current_user),
):
    try:
        result = OrderLifecycle.confirm_order(current_user.id, order_no, role=current_user.role)
    except OrderLifecycleError as e:
        raise _handle_lifecycle_error(e)
    return OrderActionResponse(**result)


@router.post(
    "/orders/{order_no}/refund",
    response_model=OrderActionResponse,
    summary="申请退款（任意非 pending 状态 → refunded）",
    description="已退款订单不可重复申请；pending 状态无需退款；completed 超 7 天不允许。",
    responses={
        409: {"description": "状态不允许退款或超过 7 天窗口"},
        404: {"description": "订单不存在"},
    },
)
def refund_order_endpoint(
    order_no: str,
    payload: RefundRequest | None = None,
    current_user: User = Depends(get_current_user),
):
    payload = payload or RefundRequest()
    try:
        result = OrderLifecycle.refund_order(
            user_id=current_user.id,
            order_no=order_no,
            reason=payload.reason,
            remark=payload.remark,
            role=current_user.role,
        )
    except OrderLifecycleError as e:
        raise _handle_lifecycle_error(e)
    return OrderActionResponse(**result)
