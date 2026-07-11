"""
RAG Pipeline 端到端测试脚本

不在 pytest 框架内（按需求是 docker 内 curl 可跑通的脚本）
步骤：
    1. 准备样本知识库（embed + upsert 到 Qdrant）
    2. 调用 run(query) 验证链路
    3. 打印 answer / contexts / scores

运行：
    docker exec customer-service-api python -m app.services.rag.test_pipeline
"""
# 这不是 pytest 测试，是手动跑的脚本（按文件 docstring 约定）
# M7/M4/V3 重构移除了 pipeline.run()，pytest 自动按 test_*.py 收集会 ImportError
__test__ = False

import logging
import sys
import uuid

from qdrant_client.models import PointStruct

from app.core.providers.embedding import get_embedding_provider
from app.clients.qdrant import (
    upsert_points,
    get_collection_info,
    ensure_collection,
)
from app.services.rag.pipeline import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================
# 样本知识库
# =============================================================
SAMPLE_DOCS = [
    {
        "text": "退款流程：用户提交退款申请后，客服会在 1-3 个工作日内审核。审核通过后，款项将原路退回支付账户。",
        "source": "faq_refund.md",
    },
    {
        "text": "会员等级分为：普通会员、银卡会员、金卡会员和钻石会员。等级越高，享受的折扣越多。",
        "source": "faq_membership.md",
    },
    {
        "text": "配送时效：普通商品下单后 48 小时内发货，定制商品 7 个工作日内发货。包邮地区为江浙沪。",
        "source": "faq_shipping.md",
    },
    {
        "text": "客服热线：400-123-4567，工作时间为周一至周五 9:00-18:00。周末可在线提交工单。",
        "source": "faq_contact.md",
    },
    {
        "text": "优惠券使用规则：满 100 减 10，满 200 减 25，不可叠加使用，有效期为发放后 30 天。",
        "source": "faq_coupon.md",
    },
]


def seed_knowledge_base():
    """
    把样本数据灌进 Qdrant（生产环境会有独立的 ingest 脚本，
    测试阶段直接在 test 里 seed，避免外部依赖）
    """
    print("=" * 60)
    print("Step 1: 准备知识库（embed + upsert）")
    print("=" * 60)

    ensure_collection()

    texts = [d["text"] for d in SAMPLE_DOCS]
    vectors = get_embedding_provider().embed_texts(texts)

    points = []
    for doc, vec in zip(SAMPLE_DOCS, vectors):
        # 用稳定 UUID（基于 source）确保重跑不重复
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, doc["source"]))
        points.append(
            PointStruct(
                id=point_id,
                vector=vec,
                payload={"text": doc["text"], "source": doc["source"]},
            )
        )

    upsert_points(points)

    info = get_collection_info()
    print(f"  collection: {info['name']}")
    print(f"  vectors_count: {info['vectors_count']}")
    print(f"  vector_size: {info['vector_size']}")
    print()


def run_tests():
    """
    多场景测试：中文 / 英文 / 知识库未覆盖的问题
    """
    test_queries = [
        ("退款要多久？", "中文 · 应命中知识库"),
        ("How long is the refund process?", "英文 · 应命中知识库"),
        ("你们的营业时间是什么？", "中文 · 可能命中较弱"),
        ("量子纠缠的物理机制", "中文 · 知识库外，期望 '我不知道'"),
    ]

    print("=" * 60)
    print("Step 2: RAG Pipeline 端到端测试")
    print("=" * 60)

    for idx, (q, note) in enumerate(test_queries, start=1):
        print(f"\n--- Test {idx}: {note} ---")
        print(f"Query: {q}")
        try:
            result = run(q)
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            continue

        # 校验返回结构
        assert "answer" in result, "缺 answer 字段"
        assert "contexts" in result, "缺 contexts 字段"
        assert "scores" in result, "缺 scores 字段"
        assert isinstance(result["answer"], str), "answer 应为 str"
        assert isinstance(result["contexts"], list), "contexts 应为 list"
        assert isinstance(result["scores"], list), "scores 应为 list"
        assert len(result["contexts"]) == len(result["scores"]), "contexts 与 scores 长度不一致"

        print(f"  answer: {result['answer'][:200]}{'...' if len(result['answer']) > 200 else ''}")
        print(f"  hit_count: {len(result['contexts'])}")
        print(f"  scores: {[round(s, 4) for s in result['scores']]}")
        if result["contexts"]:
            first = result["contexts"][0][:80] if result["contexts"][0] else "(空)"
            print(f"  top1_context: {first}{'...' if first and len(first) > 80 else ''}")

    print("\n" + "=" * 60)
    print("所有测试执行完毕")
    print("=" * 60)


if __name__ == "__main__":
    try:
        seed_knowledge_base()
        run_tests()
        sys.exit(0)
    except Exception:
        logger.exception("test_pipeline 失败")
        sys.exit(1)