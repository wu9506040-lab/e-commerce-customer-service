"""
商品 Tool - 纯 DB 查询

按 CLAUDE.md §6：tool 层只做 DB 查询，不调 LLM 不做 RAG。
商品 RAG 增强（用 Qdrant 补 description）在 services/product_service.py 做。
"""
from typing import Optional

from app.clients.mysql_client import with_safe_session
from app.models.product import Product


class ProductTool:
    """商品查询工具"""

    @staticmethod
    def get_by_sku(sku: str) -> Optional[dict]:
        """按 SKU 查商品"""
        with with_safe_session(commit=False) as db:
            p = db.query(Product).filter(
                Product.sku == sku,
                Product.status == 1,
                Product.deleted == 0,
            ).first()
            return ProductTool._to_dict(p) if p else None

    @staticmethod
    def list_products(category: Optional[str] = None, limit: int = 20) -> list[dict]:
        """
        列出在售商品（可选按类目过滤）

        Args:
            category: 类目名（smartphone/earphone/...），用 name LIKE 简化匹配
            limit: 最大返回数

        V2.x 简化：不用 JSON 属性匹配（避免方言差异），靠 name contains 实现。
        V3 优化：解析 attributes JSON 字段精确过滤。
        """
        with with_safe_session(commit=False) as db:
            q = db.query(Product).filter(Product.status == 1, Product.deleted == 0)
            if category:
                # 类目词会出现在 name 里（如 "ZP1 旗舰手机" 含 "手机"）
                q = q.filter(Product.name.contains(category))
            products = q.order_by(Product.id).limit(limit).all()
            return [ProductTool._to_dict(p) for p in products]

    @staticmethod
    def search_by_keyword(keyword: str, limit: int = 10) -> list[dict]:
        """按商品名模糊搜索"""
        with with_safe_session(commit=False) as db:
            products = db.query(Product).filter(
                Product.name.contains(keyword),
                Product.status == 1,
                Product.deleted == 0,
            ).limit(limit).all()
            return [ProductTool._to_dict(p) for p in products]

    @staticmethod
    def _to_dict(p: Product) -> dict:
        return {
            "sku": p.sku,
            "name": p.name,
            "price": float(p.price),
            "stock": p.stock,
            "attributes": p.attributes,
            "description": p.description,
        }