"""
answer_quality.py - 真回答质量评估模块

按 2026-07-18 用户反馈整改：
- 新增：对比 Agent 输出 vs reference_answer（来自 real_corpus.json）关键政策术语覆盖率
- 真实依据：ref_answer 是从公开话术合集整理的真实客服标准回复

设计：
- 4 类业务（refund/logistics/order/policy）每类有关键词表
- 覆盖率 = ref_answer 中出现且 Agent 输出中也出现的关键词 / ref_answer 中出现的关键词总数

2026-07-19 修复（V5 P2 任务）：
- 当 ref_answer 不含 POLICY_KEYWORDS 时，coverage_rate 返 None（标记为"无指标"）
- 上层统计时跳过 None case（不计入分子分母）
- 修复前：硬编码 1.0 稀释真实数据（V4 16 个 case 中 4 个空 ref 贡献 25% 分子）

2026-07-20 修复（V10-C）：
- 关键词匹配前移除 Unicode 空白，兼容“24小时”与“24 小时”等同义格式
- 报告仍返回 POLICY_KEYWORDS 中的 canonical 关键词，保持历史 JSON 契约不变
"""
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# =============================================================
# 政策关键词表（按业务场景分类）
# 来源：综合自 京东/淘宝/拼多多 帮助中心 + 道客巴巴/帮客服/搜狐公开话术
# =============================================================
POLICY_KEYWORDS = {
    "refund": [
        "7天无理由", "质量问题", "凭证", "运费险", "退款时效", "仅退款",
        "退货退款", "图片", "运费承担", "签收", "审核", "72小时",
        "24小时", "到账", "运费", "补偿", "协商", "二次销售",
    ],
    "logistics": [
        "快递", "派送", "在途", "签收", "物流单号", "24小时",
        "联系物流", "催促", "投诉", "破损", "丢件", "改地址",
        "上门取件", "理赔", "暴力挤压",
    ],
    "order": [
        "订单号", "下单时间", "订单状态", "物流", "签收", "收货地址",
        "订单详情", "取消订单", "待发货", "已发货", "已签收", "我的订单",
    ],
    "policy": [
        "价保", "发票", "保修", "三包", "PLUS", "运费险", "特权",
        "免运费", "退换货", "申请售后", "维修", "电子发票",
    ],
    "escalate": [
        "转人工", "客服", "专员", "升级", "主管", "投诉", "曝光",
        "处理", "联系", "等待",
    ],
}


# =============================================================
# 数据结构
# =============================================================
@dataclass
class CoverageReport:
    """回答覆盖率报告（V5 修复：coverage_rate 可为 None）"""
    coverage_rate: Optional[float] = None  # None = ref 无关键词（无指标）
    ref_keywords: List[str] = field(default_factory=list)
    agent_keywords: List[str] = field(default_factory=list)
    missing_keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================
# 评估函数
# =============================================================
def _normalize_for_keyword_match(text: str) -> str:
    """移除 Unicode 空白，避免展示格式差异造成政策关键词漏判。"""
    return "".join(text.split())


def evaluate_coverage(
    agent_output: str,
    ref_answer: str,
    scenario_type: str = "refund",
) -> CoverageReport:
    """计算关键政策术语覆盖率。

    Args:
        agent_output: Agent / System 生成的最终文本
        ref_answer: reference_answer（来自 real_corpus.json）
        scenario_type: refund/logistics/order/policy/escalate

    Returns:
        CoverageReport: 覆盖率 + 命中关键词 + 缺失关键词
        coverage_rate=None 表示"ref 无关键词（无指标）"，上层应跳过
    """
    if not agent_output or not ref_answer:
        return CoverageReport(coverage_rate=None)

    keywords = POLICY_KEYWORDS.get(scenario_type, [])
    if not keywords:
        return CoverageReport(coverage_rate=None)  # 场景无关键词表 = 无指标

    normalized_ref = _normalize_for_keyword_match(ref_answer)
    normalized_agent = _normalize_for_keyword_match(agent_output)

    # V10-C：匹配前统一移除空白；返回值仍保留 canonical 关键词，避免报告契约变化。
    ref_present = [
        kw for kw in keywords
        if _normalize_for_keyword_match(kw) in normalized_ref
    ]
    if not ref_present:
        # V5 修复：ref 没有这些关键词 → 标记为 None（无指标）
        # 修复前硬编码 1.0 会稀释真实数据（如 V4 16 个 case 中 4 个空 ref 贡献 25% 分子）
        return CoverageReport(coverage_rate=None, ref_keywords=[], agent_keywords=[], missing_keywords=[])

    agent_covered = [
        kw for kw in ref_present
        if _normalize_for_keyword_match(kw) in normalized_agent
    ]
    missing = [kw for kw in ref_present if kw not in agent_covered]

    coverage = len(agent_covered) / len(ref_present) if ref_present else 0.0

    return CoverageReport(
        coverage_rate=round(coverage, 4),
        ref_keywords=ref_present,
        agent_keywords=agent_covered,
        missing_keywords=missing,
    )


# =============================================================
# 批量统计
# =============================================================
@dataclass
class CoverageStats:
    total: int = 0
    coverage_sum: float = 0.0
    fully_covered: int = 0  # coverage == 1.0
    partial_covered: int = 0  # 0 < coverage < 1.0
    none_covered: int = 0  # coverage == 0.0

    @property
    def avg_coverage(self) -> float:
        return self.coverage_sum / self.total if self.total else 0.0

    def add(self, report: CoverageReport) -> None:
        self.total += 1
        self.coverage_sum += report.coverage_rate
        if report.coverage_rate >= 0.999:
            self.fully_covered += 1
        elif report.coverage_rate > 0:
            self.partial_covered += 1
        else:
            self.none_covered += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "avg_coverage": round(self.avg_coverage, 4),
            "fully_covered": self.fully_covered,
            "partial_covered": self.partial_covered,
            "none_covered": self.none_covered,
            "by_bucket": {
                "fully": self.fully_covered,
                "partial": self.partial_covered,
                "none": self.none_covered,
            },
        }


# =============================================================
# CLI
# =============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ref = "您好，7天无理由退货要求商品完好，请提供质量问题的图片凭证。"
    agent = "您的订单可以7天无理由退货，请提供图片凭证。"

    r = evaluate_coverage(agent, ref, "refund")
    print(f"coverage={r.coverage_rate} agent={r.agent_keywords} missing={r.missing_keywords}")