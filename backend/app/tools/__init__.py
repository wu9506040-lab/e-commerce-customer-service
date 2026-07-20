"""
Tools Layer - 函数调用层（V2 新增，非 Agent）

按 PROJECT_DESIGN.md §3 设计：
- 每个 tool 文件 = 一组纯函数 / 一个 Tool 类
- 不调 LLM、不做 RAG，只做 DB 查询 / 外部 API mock
- service 层调 tool，tool 不调 service（避免循环依赖）

按 CLAUDE.md §6：
- tools/ 属于业务能力层（与 services/ 平级）
- tool 函数的入参必须显式收 user_id 等上下文，禁止"查所有再过滤"

Sprint 19：新增 3 个 Tool 类（售后 / 售前 / 售中），共 6 个。
- 售后：AfterSalesTool（只读）
- 售前：PromotionTool（只读）
- 售中：OrderModifyTool（写操作 · 2 步确认）
"""
from app.tools.after_sales_tool import AfterSalesTool
from app.tools.order_modify_tool import OrderModifyTool
from app.tools.order_tool import OrderTool
from app.tools.product_tool import ProductTool
from app.tools.promotion_tool import PromotionTool
from app.tools.refund_tool import RefundTool

__all__ = [
    "AfterSalesTool",
    "OrderModifyTool",
    "OrderTool",
    "ProductTool",
    "PromotionTool",
    "RefundTool",
]