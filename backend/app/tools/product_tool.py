"""
商品 Tool - 商品查询（DB 访问经 ProductService Protocol）

按 CLAUDE.md §6：tool 层只做数据查询，不调 LLM 不做 RAG。
Sprint 15：查询下沉到 ProductService Protocol（CLAUDE.md §9.3.2），
Tool 层只做 Product(DTO) → dict 适配，保持既有返回字段不变。
"""
from typing import Optional

from app.services.order.factory import get_order_service_factory, run_sync


class ProductTool:
    """商品查询工具"""

    @staticmethod
    def get_by_sku(sku: str) -> Optional[dict]:
        """按 SKU 查商品"""
        svc = get_order_service_factory().get_product_service()
        p = run_sync(svc.get_product(sku))
        return ProductTool._dto_to_dict(p) if p else None

    @staticmethod
    def list_products(category: Optional[str] = None, limit: int = 20) -> list[dict]:
        """
        列出在售商品（可选按类目过滤）

        Args:
            category: 类目名（smartphone/earphone/...），用 name LIKE 简化匹配
            limit: 最大返回数

        V2.x 简化：不用 JSON 属性匹配（避免方言差异），靠 name contains 实现。
        """
        # query="" → name LIKE '%%' 匹配全部（等价旧 list_products 无关键词场景）
        svc = get_order_service_factory().get_product_service()
        products = run_sync(svc.search_products("", category=category, limit=limit))
        return [ProductTool._dto_to_dict(p) for p in products]

    @staticmethod
    def search_by_keyword(keyword: str, limit: int = 10) -> list[dict]:
        """按商品名模糊搜索"""
        svc = get_order_service_factory().get_product_service()
        products = run_sync(svc.search_products(keyword, limit=limit))
        return [ProductTool._dto_to_dict(p) for p in products]

    @staticmethod
    def _dto_to_dict(p) -> dict:
        """Product(DTO) → dict（与旧 _to_dict 字段一致）"""
        return {
            "sku": p.sku,
            "name": p.name,
            "price": float(p.price),
            "stock": p.stock,
            "attributes": p.attributes,
            "description": p.description,
        }
