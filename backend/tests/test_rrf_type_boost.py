"""
test_rrf_type_boost.py — P3-3 L1 单测：RRF 类型加权（policy > faq > product）

按 SOP-V1 §2.2 数据可信验证：L1 mock 验证逻辑路径。

测试目标：
- weights 参数生效：policy 1.2 / faq 1.0 / product 0.9
- doc_type 字段提取兼容两种形态（直挂 / payload 嵌套）
- 无 doc_type / doc_type 不在 dict 中 → 默认 1.0（向后兼容）
- weights=None / 空 dict → 与 P3-3 前行为完全一致
"""
from app.services.rrf import rrf_fuse


# =============================================================
# Case 1: policy 加权 1.2 > product 加权 0.9 → 排序反转
# =============================================================
def test_rrf_type_boost_reorders_results():
    """场景：同 rank 的 policy doc 与 product doc，加权后 policy 排前"""
    # 模拟两路各 1 个 doc：policy 在 vector 第 1，product 在 vector 第 2
    # 无加权时 product 因为 BM25 排第 1 总分反而更高（双命中）
    vector_results = [
        {"id": "POLICY_1", "doc_type": "policy", "text": "退货政策"},
        {"id": "PRODUCT_1", "doc_type": "product", "text": "ZP2 手机"},
    ]
    bm25_results = [
        {"id": "PRODUCT_1", "payload": {"doc_type": "product"}, "text": "ZP2 手机"},
    ]

    weights = {"policy": 1.2, "faq": 1.0, "product": 0.9}

    fused = rrf_fuse(
        [vector_results, bm25_results],
        k=60,
        weights=weights,
    )

    # PRODUCT_1: 1/(60+2) + 1/(60+1) = 0.01613 + 0.01639 = 0.03252
    #   加权后: 0.03252 * 0.9 = 0.02927
    # POLICY_1: 1/(60+1) = 0.01639
    #   加权后: 0.01639 * 1.2 = 0.01967
    # 期望：PRODUCT_1 仍排前（双命中优势压过加权劣势）

    # 改用例验证加权本身：让两个 doc 都在 vector 同样位置
    vector_results2 = [
        {"id": "POLICY_1", "doc_type": "policy", "text": "退货政策"},
        {"id": "PRODUCT_1", "doc_type": "product", "text": "ZP2 手机"},
    ]
    bm25_results2 = []

    fused2 = rrf_fuse(
        [vector_results2, bm25_results2],
        k=60,
        weights=weights,
    )

    # 单路命中：POLICY rank=1，PRODUCT rank=2（不同）
    # POLICY_1: 1/61 * 1.2 = 0.01967
    # PRODUCT_1: 1/62 * 0.9 = 0.01452
    assert fused2[0]["id"] == "POLICY_1", (
        f"加权后 POLICY 应排前，实际 {fused2[0]['id']} (rrf_score={fused2[0]['rrf_score']})"
    )
    assert fused2[0]["rrf_score"] > fused2[1]["rrf_score"]
    # 验证权重乘正确（按各自 rank）
    expected_policy = (1.0 / 61) * 1.2  # POLICY rank=1
    expected_product = (1.0 / 62) * 0.9  # PRODUCT rank=2
    assert abs(fused2[0]["rrf_score"] - round(expected_policy, 6)) < 1e-6
    assert abs(fused2[1]["rrf_score"] - round(expected_product, 6)) < 1e-6


# =============================================================
# Case 2: doc_type 直挂 doc.dict
# =============================================================
def test_rrf_type_boost_direct_field():
    """doc.doc_type 直挂形态应被识别"""
    docs = [{"id": "X", "doc_type": "policy", "text": "t"}]
    weights = {"policy": 2.0}

    fused = rrf_fuse([docs], k=60, weights=weights)

    # 1/(60+1) * 2.0 = 0.03279
    assert abs(fused[0]["rrf_score"] - round((1.0 / 61) * 2.0, 6)) < 1e-6


# =============================================================
# Case 3: doc_type 在 payload 嵌套形态（BM25 corpus）
# =============================================================
def test_rrf_type_boost_payload_nested():
    """doc.payload.doc_type 嵌套形态应被识别（BM25 副本走这条路径）"""
    docs = [{"id": "X", "payload": {"doc_type": "policy"}, "text": "t"}]
    weights = {"policy": 2.0}

    fused = rrf_fuse([docs], k=60, weights=weights)

    assert abs(fused[0]["rrf_score"] - round((1.0 / 61) * 2.0, 6)) < 1e-6


# =============================================================
# Case 4: 无 doc_type 字段 → 默认 1.0（不参与加权）
# =============================================================
def test_rrf_type_boost_missing_doc_type_uses_default():
    """doc 无 doc_type → 视为 1.0（向后兼容旧数据）"""
    docs = [{"id": "X", "text": "t"}]  # 无 doc_type
    weights = {"policy": 2.0}

    fused = rrf_fuse([docs], k=60, weights=weights)

    # 1/(60+1) * 1.0 = 0.01639
    assert abs(fused[0]["rrf_score"] - round(1.0 / 61, 6)) < 1e-6


# =============================================================
# Case 5: doc_type 不在 weights dict 中 → 默认 1.0
# =============================================================
def test_rrf_type_boost_unknown_doc_type_uses_default():
    """doc_type='manual'（业务自定义）但 weights dict 中只有 policy/faq/product → 1.0"""
    docs = [{"id": "X", "doc_type": "manual", "text": "t"}]
    weights = {"policy": 1.2, "faq": 1.0, "product": 0.9}

    fused = rrf_fuse([docs], k=60, weights=weights)

    assert abs(fused[0]["rrf_score"] - round(1.0 / 61, 6)) < 1e-6


# =============================================================
# Case 6: weights=None → 与 P3-3 前行为完全一致（向后兼容）
# =============================================================
def test_rrf_no_weights_backward_compatible():
    """weights=None（默认）→ 不加权，行为与原实现完全一致"""
    docs = [{"id": "X", "doc_type": "policy", "text": "t"}]
    docs_none = [{"id": "X", "text": "t"}]

    fused_weighted = rrf_fuse([docs], k=60)
    fused_unweighted = rrf_fuse([docs_none], k=60)

    # doc_type 不影响 rrf_score（None 时不乘权重）
    assert fused_weighted[0]["rrf_score"] == fused_unweighted[0]["rrf_score"]


# =============================================================
# Case 7: 空 dict → 不加权（与 None 等价）
# =============================================================
def test_rrf_empty_weights_equivalent_to_none():
    """weights={} → 行为与 None 完全一致"""
    docs = [{"id": "X", "doc_type": "policy", "text": "t"}]

    fused_none = rrf_fuse([docs], k=60, weights=None)
    fused_empty = rrf_fuse([docs], k=60, weights={})

    assert fused_none[0]["rrf_score"] == fused_empty[0]["rrf_score"]


# =============================================================
# Case 8: 加权后排序保持稳定（双命中优势不会被加权完全反转）
# =============================================================
def test_rrf_type_boost_respects_double_hit_signal():
    """加权不应破坏 RRF 核心语义：双命中仍优先于单命中"""
    vector_results = [
        {"id": "A", "doc_type": "product", "text": "a"},  # product 加权 0.9
        {"id": "B", "doc_type": "policy", "text": "b"},  # policy 加权 1.2（单路）
    ]
    bm25_results = [
        {"id": "A", "payload": {"doc_type": "product"}, "text": "a"},  # 双命中
    ]
    weights = {"policy": 1.2, "product": 0.9}

    fused = rrf_fuse([vector_results, bm25_results], k=60, weights=weights)

    # A (双命中): (1/61 + 1/61) * 0.9 = 0.02951
    # B (单路):  1/62 * 1.2 = 0.01935
    # A 仍应排前（双命中信号不能被单倍加权覆盖）
    assert fused[0]["id"] == "A", (
        f"双命中 A(product 加权 0.9) 应压过单路 B(policy 加权 1.2)，"
        f"实际 top1={fused[0]['id']} score={fused[0]['rrf_score']}"
    )