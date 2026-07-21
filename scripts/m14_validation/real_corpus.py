"""
real_corpus.py - 真实电商客服话术库加载器

话术来源（人工整理自公开资料）：
- 道客巴巴《淘宝京东拼多多电商类客服常用话术集锦》
- 帮客服《电商常用退款话术快捷回复》
- 帮客服《售后客服精华话术处理退货退款》
- 卖家网《拼多多售后话术大全》
- 搜狐《电商退款挽单话术合集》
- 搜狐《电商售后客服退款审核话术》
- 知乎《拼多多仅退款话术怎么说》
- 京东帮助中心 help.jd.com 官方 FAQ
- 微博/今日头条真实 case 截图

每条 schema：
{
  "id": "RC001",
  "scenario_type": "refund|order|logistics|policy|escalate",
  "query": "真实用户提问表述",
  "reference_answer": "真实客服标准回复模板",
  "expected_resolver_action": "direct_answer|show_picker|not_found|ask_login|ask_login_or_list",
  "expected_flow_branch": "synthesize|escalate|ask_order_no|not_found（V11-A 起;invalid_order 收编）",
  "escalate_trigger": "quality_no_proof|emotion_high|amount_high|manual_request|none",
  "source": "来源标注",
  "platform_ref": "京东|淘宝|拼多多",
  "tags": ["场景标签", ...]
}
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# 数据文件路径（gitignored）
CORPUS_PATH = Path(__file__).parent / "data" / "real_corpus.json"

_cache: Dict[str, Any] = {}


def load_corpus() -> List[Dict[str, Any]]:
    """加载全部真实话术（带缓存）"""
    if "corpus" not in _cache:
        if not CORPUS_PATH.exists():
            logger.error(f"real_corpus.json 不存在: {CORPUS_PATH}")
            return []
        _cache["corpus"] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        logger.info(f"加载 real_corpus: {len(_cache['corpus'])} 条")
    return _cache["corpus"]


def get_by_id(rc_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 查单条话术"""
    for item in load_corpus():
        if item["id"] == rc_id:
            return item
    return None


def filter_by_type(scenario_type: str) -> List[Dict[str, Any]]:
    """按 scenario_type 过滤"""
    return [c for c in load_corpus() if c.get("scenario_type") == scenario_type]


def filter_by_action(action: str) -> List[Dict[str, Any]]:
    """按 expected_resolver_action 过滤"""
    return [c for c in load_corpus() if c.get("expected_resolver_action") == action]


def filter_by_branch(branch: str) -> List[Dict[str, Any]]:
    """按 expected_flow_branch 过滤"""
    return [c for c in load_corpus() if c.get("expected_flow_branch") == branch]


def filter_by_escalate_trigger(trigger: str) -> List[Dict[str, Any]]:
    """按 escalate_trigger 过滤"""
    return [c for c in load_corpus() if c.get("escalate_trigger") == trigger]


def stats() -> Dict[str, Any]:
    """统计分布（debug 用）"""
    corpus = load_corpus()
    type_dist: Dict[str, int] = {}
    source_dist: Dict[str, int] = {}
    for c in corpus:
        type_dist[c.get("scenario_type", "unknown")] = type_dist.get(c.get("scenario_type", "unknown"), 0) + 1
        src = c.get("source", "unknown")
        source_dist[src] = source_dist.get(src, 0) + 1
    return {
        "total": len(corpus),
        "by_type": type_dist,
        "by_source": source_dist,
    }


# =============================================================
# CLI
# =============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    corpus = load_corpus()
    print(f"total: {len(corpus)}")
    print(f"stats: {json.dumps(stats(), ensure_ascii=False, indent=2)}")