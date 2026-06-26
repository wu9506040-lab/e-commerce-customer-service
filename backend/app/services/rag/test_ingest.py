"""
Ingest 端到端测试

覆盖：
    1. chunk_text 边界（空字符串 / 参数异常 / 短文 / 长文带 overlap）
    2. ingest_text 入库后，pipeline.run() 能检索到
    3. 幂等性：同 source 二次入库不会重复新增

运行：
    docker exec customer-service-api python -m app.services.rag.test_ingest
"""
import logging
import sys

from app.services.rag.ingest import chunk_text, ingest_text
from app.services.rag.pipeline import run as rag_run
from app.clients.qdrant import get_collection_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================
# 测试 1: chunk_text 边界
# =============================================================
def test_chunk_boundaries():
    print("=" * 60)
    print("Test 1: chunk_text 边界")
    print("=" * 60)

    # 空字符串
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []
    print("  [PASS] 空字符串 → []")

    # 短文本（< chunk_size）应返回单片
    chunks = chunk_text("hello world", chunk_size=100)
    assert chunks == ["hello world"]
    print(f"  [PASS] 短文本 → 1 片")

    # 长文本带 overlap：120 chars / chunk_size=100 / overlap=10
    # 期望：step=90, start=[0,90] → 第一片 100，第二片 30（120-90=30）
    text = "a" * 120
    chunks = chunk_text(text, chunk_size=100, overlap=10)
    assert len(chunks) == 2, f"期望 2 片，实际 {len(chunks)}"
    assert len(chunks[0]) == 100, "第一片应 100 字符"
    assert len(chunks[1]) == 30, "第二片应剩 30 字符"
    print(f"  [PASS] 长文本 → 2 片（100+30），overlap 正确")

    # 参数异常
    try:
        chunk_text("abc", chunk_size=10)  # 低于 MIN_CHUNK_SIZE=100
        assert False, "应该抛异常"
    except ValueError as e:
        print(f"  [PASS] chunk_size<100 → ValueError")

    try:
        chunk_text("abc", chunk_size=3000)  # 高于 MAX_CHUNK_SIZE=2000
        assert False, "应该抛异常"
    except ValueError as e:
        print(f"  [PASS] chunk_size>2000 → ValueError")

    try:
        chunk_text("abc", chunk_size=500, overlap=600)  # overlap >= chunk_size
        assert False, "应该抛异常"
    except ValueError as e:
        print(f"  [PASS] overlap>=chunk_size → ValueError")

    print()


# =============================================================
# 测试 2: 中文真实切片
# =============================================================
SAMPLE_CN = """
智能客服系统使用指南。

第一章：账户注册
新用户可通过手机号或邮箱注册账户。注册时需设置密码，密码长度不少于 8 位，必须包含字母和数字。
注册成功后，系统会自动赠送 100 元体验金，可用于任何付费咨询服务。

第二章：充值与提现
最低充值金额为 10 元，最高单次充值 50000 元。支持微信、支付宝、银行卡三种支付方式。
提现需绑定本人实名认证的银行卡，提现到账时间为 1-3 个工作日，提现手续费按金额的 0.1% 收取。

第三章：客服接入
在线客服响应时间：工作日 9:00-18:00 平均 30 秒，非工作日平均 2 分钟。
电话客服热线：400-888-1234，工作时间外请留言，我们会尽快回电。
"""


def test_chinese_chunk():
    print("=" * 60)
    print("Test 2: 中文切片")
    print("=" * 60)
    chunks = chunk_text(SAMPLE_CN, chunk_size=200, overlap=30)
    print(f"  原文长度: {len(SAMPLE_CN)} 字符")
    print(f"  切片数: {len(chunks)}")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] ({len(c)} chars) {c[:50]}...")
    assert len(chunks) >= 2, f"期望至少 2 片，实际 {len(chunks)}"
    assert all(len(c) <= 200 for c in chunks), "每片不应超过 chunk_size"
    print()


# =============================================================
# 测试 3: 端到端入库 + 检索联调
# =============================================================
def test_ingest_and_retrieve():
    print("=" * 60)
    print("Test 3: 入库 + 检索联调")
    print("=" * 60)

    source = "test_guide_v1"
    print(f"  入库 source={source}")

    # 第一次入库（chunk_size=120 让 312 字产生 3 片）
    result1 = ingest_text(SAMPLE_CN, source=source, chunk_size=120, overlap=20)
    print(f"  入库完成: chunks={result1['ingested_chunks']}, ids={len(result1['chunk_ids'])}")
    assert result1["ingested_chunks"] >= 2
    first_ids = result1["chunk_ids"]

    # 检索联调：问一个原文里有的关键词
    print("\n  检索测试 1: '充值方式有哪些？'")
    r1 = rag_run("充值方式有哪些？")
    print(f"    top1 score: {r1['scores'][0]:.4f}")
    print(f"    answer: {r1['answer'][:80]}")
    assert r1["scores"][0] > 0.5, f"top1 score 偏低: {r1['scores'][0]}"
    print("    [PASS] 命中知识库")

    print("\n  检索测试 2: '客服热线是多少？'")
    r2 = rag_run("客服热线是多少？")
    print(f"    top1 score: {r2['scores'][0]:.4f}")
    print(f"    answer: {r2['answer'][:80]}")
    assert r2["scores"][0] > 0.5
    print("    [PASS] 命中知识库")

    # 幂等性：同 source 第二次入库，ID 列表应完全一致
    print("\n  幂等性测试: 同 source 二次入库")
    result2 = ingest_text(SAMPLE_CN, source=source, chunk_size=120, overlap=20)
    assert result2["chunk_ids"] == first_ids, "幂等性失败：ID 列表变了"
    print(f"    [PASS] 二次入库 ID 列表一致（{len(first_ids)} 片）")

    # 集合总量检查
    info = get_collection_info()
    print(f"\n  collection 总点数: {info.get('points_count', 'N/A')}")

    print()


if __name__ == "__main__":
    try:
        test_chunk_boundaries()
        test_chinese_chunk()
        test_ingest_and_retrieve()
        print("=" * 60)
        print("所有测试通过")
        print("=" * 60)
        sys.exit(0)
    except Exception:
        logger.exception("test_ingest 失败")
        sys.exit(1)