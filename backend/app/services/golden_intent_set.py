"""V14（2.5）意图黄金评测集——每个意图 ≥30 条，含易/中/难分层 + E6' 对抗

设计原则：故意分类覆盖所有四档主意图（refund_query/policy_query/order_query/product_query）。
每档 35+ 条，其中：
  - 易（15）：规则模板直接命中（"我要退款"、"7天无理由"等）
  - 中（10）：口语化变体，需要规则顺序敏感性兜底（"不喜欢了"、"什么时候退我钱"）
  - 难（10）：规则几乎抓不住，应落 LLM 兜底（"那货我不想要了行不行"、"几号能打款到我账上"）

E6' persona 对抗（4 类×8 条=32 条）专测边界场景：
  - 矛盾陈述：自我否定（"我想买便宜但便宜的不要"）
  - 政策外诉求：业务域外（"你几点下班"、"你是真人吗"）
  - 多意图连珠：单句塞多个意图（"查订单 ORD001 顺便退款"）
  - 情绪化：强烈语气（"到底能不能退！差评！"）

不调用 LLM——规则层（method=rule）与 LLM 兜底（method=llm）分离评测；LLM 兜底测覆盖率时
mock 为稳定 return，方便 CI 离线跑。
"""
from __future__ import annotations

from typing import Dict, List


# 4 类主意图 × 每类 35+ 条（含易/中/难）
GOLDEN_INTENT_SET: List[Dict[str, str]] = [
    # ============ refund_query（35 条）============
    {"query": "我要退款", "expected": "refund_query", "difficulty": "easy"},
    {"query": "我要退货", "expected": "refund_query", "difficulty": "easy"},
    {"query": "我要退换", "expected": "refund_query", "difficulty": "easy"},
    {"query": "我要换货", "expected": "refund_query", "difficulty": "easy"},
    {"query": "我要申请退款", "expected": "refund_query", "difficulty": "easy"},
    {"query": "我要申请退货", "expected": "refund_query", "difficulty": "easy"},
    {"query": "我要发起退款", "expected": "refund_query", "difficulty": "easy"},
    {"query": "想退", "expected": "refund_query", "difficulty": "easy"},
    {"query": "想退掉", "expected": "refund_query", "difficulty": "easy"},
    {"query": "想退款", "expected": "refund_query", "difficulty": "easy"},
    {"query": "能退吗", "expected": "refund_query", "difficulty": "easy"},
    {"query": "可以退", "expected": "refund_query", "difficulty": "easy"},
    {"query": "能不能退", "expected": "refund_query", "difficulty": "easy"},
    {"query": "能退款吗", "expected": "refund_query", "difficulty": "easy"},
    {"query": "还能退吗", "expected": "refund_query", "difficulty": "easy"},
    {"query": "不喜欢了能退吗", "expected": "refund_query", "difficulty": "medium"},
    {"query": "货有问题要退", "expected": "refund_query", "difficulty": "medium"},
    {"query": "尺寸不合适想退换", "expected": "refund_query", "difficulty": "medium"},
    {"query": "质量差退掉算了", "expected": "refund_query", "difficulty": "medium"},
    {"query": "重复收到了退掉一份", "expected": "refund_query", "difficulty": "medium"},
    {"query": "快递一直没到，退了", "expected": "refund_query", "difficulty": "medium"},
    {"query": "我买错了申请退款", "expected": "refund_query", "difficulty": "medium"},
    {"query": "商品有问题，退货", "expected": "refund_query", "difficulty": "medium"},
    {"query": "想把这个订单退了", "expected": "refund_query", "difficulty": "medium"},
    {"query": "颜色不对能退吗", "expected": "refund_query", "difficulty": "medium"},
    {"query": "商品不想要了要退", "expected": "refund_query", "difficulty": "hard"},
    {"query": "几号能打款到我账上", "expected": "refund_query", "difficulty": "hard"},
    {"query": "能不能把钱退回给我", "expected": "refund_query", "difficulty": "hard"},
    {"query": "那个我要退掉", "expected": "refund_query", "difficulty": "hard"},
    {"query": "这单取消吧", "expected": "refund_query", "difficulty": "hard"},
    {"query": "不想要了帮我取消订单", "expected": "refund_query", "difficulty": "hard"},
    {"query": "商品有问题需要退", "expected": "refund_query", "difficulty": "hard"},
    {"query": "收到了但是要退掉", "expected": "refund_query", "difficulty": "hard"},
    {"query": "这个我不想要了能退吗", "expected": "refund_query", "difficulty": "hard"},

    # ============ policy_query（35 条）============
    {"query": "7天无理由", "expected": "policy_query", "difficulty": "easy"},
    {"query": "七天无理由", "expected": "policy_query", "difficulty": "easy"},
    {"query": "怎么退款", "expected": "policy_query", "difficulty": "easy"},
    {"query": "怎么退货", "expected": "policy_query", "difficulty": "easy"},
    {"query": "怎么退换", "expected": "policy_query", "difficulty": "easy"},
    {"query": "如何退款", "expected": "policy_query", "difficulty": "easy"},
    {"query": "退款流程", "expected": "policy_query", "difficulty": "easy"},
    {"query": "退货流程", "expected": "policy_query", "difficulty": "easy"},
    {"query": "退款怎么", "expected": "policy_query", "difficulty": "easy"},
    {"query": "退款到账多久", "expected": "policy_query", "difficulty": "easy"},
    {"query": "退款几天到账", "expected": "policy_query", "difficulty": "easy"},
    {"query": "退款什么时候到", "expected": "policy_query", "difficulty": "easy"},
    {"query": "包邮吗", "expected": "policy_query", "difficulty": "easy"},
    {"query": "保修多久", "expected": "policy_query", "difficulty": "easy"},
    {"query": "发票怎么开", "expected": "policy_query", "difficulty": "easy"},
    {"query": "什么时候发货", "expected": "policy_query", "difficulty": "medium"},
    {"query": "退货运费谁出", "expected": "policy_query", "difficulty": "medium"},
    {"query": "多久到货", "expected": "policy_query", "difficulty": "medium"},
    {"query": "运费多少", "expected": "policy_query", "difficulty": "medium"},
    {"query": "能开发票吗", "expected": "policy_query", "difficulty": "medium"},
    {"query": "发什么快递", "expected": "policy_query", "difficulty": "medium"},
    {"query": "几天能收到", "expected": "policy_query", "difficulty": "medium"},
    {"query": "活动力度大不大", "expected": "policy_query", "difficulty": "medium"},
    {"query": "优惠券怎么用", "expected": "policy_query", "difficulty": "medium"},
    {"query": "满减怎么算", "expected": "policy_query", "difficulty": "medium"},
    {"query": "支持七天无理由退货吗", "expected": "policy_query", "difficulty": "medium"},
    {"query": "几天之内能退", "expected": "policy_query", "difficulty": "medium"},
    {"query": "退款什么时候打到我账上", "expected": "policy_query", "difficulty": "hard"},
    {"query": "你们这里能开企业发票吗", "expected": "policy_query", "difficulty": "hard"},
    {"query": "海外能发吗", "expected": "policy_query", "difficulty": "hard"},
    {"query": "运费险怎么买", "expected": "policy_query", "difficulty": "hard"},
    {"query": "有什么优惠活动", "expected": "policy_query", "difficulty": "hard"},
    {"query": "多长时间送到", "expected": "policy_query", "difficulty": "hard"},
    {"query": "超出七天还能退吗", "expected": "policy_query", "difficulty": "hard"},
    {"query": "已发货还能取消吗", "expected": "policy_query", "difficulty": "hard"},

    # ============ order_query（35 条）============
    {"query": "查订单 ORD20260628001", "expected": "order_query", "difficulty": "easy"},
    {"query": "我的订单到哪了", "expected": "order_query", "difficulty": "easy"},
    {"query": "物流", "expected": "order_query", "difficulty": "easy"},
    {"query": "快递", "expected": "order_query", "difficulty": "easy"},
    {"query": "签收", "expected": "order_query", "difficulty": "easy"},
    {"query": "订单状态", "expected": "order_query", "difficulty": "easy"},
    {"query": "修改地址", "expected": "order_query", "difficulty": "easy"},
    {"query": "改地址", "expected": "order_query", "difficulty": "easy"},
    {"query": "催单", "expected": "order_query", "difficulty": "easy"},
    {"query": "到哪了", "expected": "order_query", "difficulty": "easy"},
    {"query": "查我的订单", "expected": "order_query", "difficulty": "easy"},
    {"query": "还没收到货", "expected": "order_query", "difficulty": "easy"},
    {"query": "什么时候送到", "expected": "order_query", "difficulty": "easy"},
    {"query": "快递单号多少", "expected": "order_query", "difficulty": "easy"},
    {"query": "改收货人", "expected": "order_query", "difficulty": "easy"},
    {"query": "订单 ORD20260628005 状态", "expected": "order_query", "difficulty": "medium"},
    {"query": "还没发货怎么办", "expected": "order_query", "difficulty": "medium"},
    {"query": "催一下快递", "expected": "order_query", "difficulty": "medium"},
    {"query": "可以换地址吗", "expected": "order_query", "difficulty": "medium"},
    {"query": "改一下收件地址", "expected": "order_query", "difficulty": "medium"},
    {"query": "帮我催单", "expected": "order_query", "difficulty": "medium"},
    {"query": "能改地址不", "expected": "order_query", "difficulty": "medium"},
    {"query": "查订单号 ORD999", "expected": "order_query", "difficulty": "medium"},
    {"query": "帮我看看 ORD20260628001", "expected": "order_query", "difficulty": "medium"},
    {"query": "订单合单", "expected": "order_query", "difficulty": "medium"},
    {"query": "能不能合到一起", "expected": "order_query", "difficulty": "medium"},
    {"query": "取消订单", "expected": "order_query", "difficulty": "hard"},
    {"query": "我没收到东西", "expected": "order_query", "difficulty": "hard"},
    {"query": "催一下", "expected": "order_query", "difficulty": "hard"},
    {"query": "东西还没到", "expected": "order_query", "difficulty": "hard"},
    {"query": "这个订单帮我处理", "expected": "order_query", "difficulty": "hard"},
    {"query": "我的快递呢", "expected": "order_query", "difficulty": "hard"},
    {"query": "有没有发货", "expected": "order_query", "difficulty": "hard"},
    {"query": "签收了没", "expected": "order_query", "difficulty": "hard"},
    {"query": "收件地址能改不", "expected": "order_query", "difficulty": "hard"},

    # ============ product_query（35 条）============
    {"query": "查询商品 SKU 价格", "expected": "product_query", "difficulty": "easy"},
    {"query": "这款手机参数", "expected": "product_query", "difficulty": "easy"},
    {"query": "有什么颜色", "expected": "product_query", "difficulty": "easy"},
    {"query": "尺寸表", "expected": "product_query", "difficulty": "easy"},
    {"query": "便宜对比", "expected": "product_query", "difficulty": "easy"},
    {"query": "优惠对比", "expected": "product_query", "difficulty": "easy"},
    {"query": "对比", "expected": "product_query", "difficulty": "easy"},
    {"query": "怎么选", "expected": "product_query", "difficulty": "easy"},
    {"query": "推荐哪个", "expected": "product_query", "difficulty": "easy"},
    {"query": "适合什么人", "expected": "product_query", "difficulty": "easy"},
    {"query": "有哪些规格", "expected": "product_query", "difficulty": "easy"},
    {"query": "什么材质", "expected": "product_query", "difficulty": "easy"},
    {"query": "保修多久", "expected": "product_query", "difficulty": "medium"},
    {"query": "适合学生党吗", "expected": "product_query", "difficulty": "medium"},
    {"query": "跟另一款比怎么样", "expected": "product_query", "difficulty": "medium"},
    {"query": "推荐个性价比高的", "expected": "product_query", "difficulty": "medium"},
    {"query": "这个和那个哪个好", "expected": "product_query", "difficulty": "medium"},
    {"query": "几号能发货", "expected": "product_query", "difficulty": "medium"},
    {"query": "型号有什么不同", "expected": "product_query", "difficulty": "medium"},
    {"query": "规格我看看", "expected": "product_query", "difficulty": "medium"},
    {"query": "好用吗", "expected": "product_query", "difficulty": "medium"},
    {"query": "销量怎么样", "expected": "product_query", "difficulty": "medium"},
    {"query": "口碑如何", "expected": "product_query", "difficulty": "medium"},
    {"query": "评价怎么样", "expected": "product_query", "difficulty": "medium"},
    {"query": "评价怎么样", "expected": "product_query", "difficulty": "medium"},
    {"query": "看看评论", "expected": "product_query", "difficulty": "medium"},
    {"query": "我想要性价比高的", "expected": "product_query", "difficulty": "hard"},
    {"query": "帮我挑个合适的", "expected": "product_query", "difficulty": "hard"},
    {"query": "跟旗舰款比呢", "expected": "product_query", "difficulty": "hard"},
    {"query": "年轻人用哪个好", "expected": "product_query", "difficulty": "hard"},
    {"query": "爸妈用适合哪个", "expected": "product_query", "difficulty": "hard"},
    {"query": "学生党适合买吗", "expected": "product_query", "difficulty": "hard"},
    {"query": "哪个耐用", "expected": "product_query", "difficulty": "hard"},
    {"query": "高个子穿什么尺码", "expected": "product_query", "difficulty": "hard"},

    # ============ E6' persona 对抗（32 条：4 类×8 条）============
    # 矛盾陈述
    {"query": "我想要便宜的，但又不要太便宜的", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "想要质量好的但便宜但又要贵的", "expected": "product_query", "difficulty": "adversarial"},
    {"query": "我想买但不想付款", "expected": "order_query", "difficulty": "adversarial"},
    {"query": "我要退款但是刚买的不想退", "expected": "refund_query", "difficulty": "adversarial"},
    # 政策外诉求
    {"query": "你们几点下班", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "你是真人吗", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "你会说英文吗", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "你家老板是谁", "expected": "policy_query", "difficulty": "adversarial"},
    # 多意图连珠
    {"query": "查订单 ORD20260628001 顺便帮我退款顺便推荐个手机", "expected": "order_query",
     "difficulty": "adversarial"},
    {"query": "看下这个产品然后告诉我退货流程", "expected": "product_query", "difficulty": "adversarial"},
    {"query": "我要查订单顺便问下快递多快", "expected": "order_query", "difficulty": "adversarial"},
    {"query": "退款到账需要多久另外我订单有什么优惠", "expected": "policy_query", "difficulty": "adversarial"},
    # 情绪化
    {"query": "到底能不能退！差评！", "expected": "refund_query", "difficulty": "adversarial"},
    {"query": "我等了三天才发货！", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "说好的当天发呢骗子", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "什么破玩意儿要退货", "expected": "refund_query", "difficulty": "adversarial"},
    # 多意图+情绪 组合
    {"query": "查订单 ORD20260628001 快发货 ！！气死", "expected": "order_query", "difficulty": "adversarial"},
    {"query": "这个产品是不是假的，怎么退？", "expected": "product_query", "difficulty": "adversarial"},
    {"query": "我要退款为什么还没退！", "expected": "refund_query", "difficulty": "adversarial"},
    {"query": "想要便宜但怕贵的", "expected": "product_query", "difficulty": "adversarial"},
    # 边界指代
    {"query": "那个", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "这个怎么操作", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "上次那个怎么弄", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "买那个", "expected": "product_query", "difficulty": "adversarial"},
    # 业务外域
    {"query": "你们老板欠我钱", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "我要投诉到 12315", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "上次的发票报销给我", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "把之前的话重复扣了", "expected": "policy_query", "difficulty": "adversarial"},
    # 极端问句
    {"query": "？", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": " ", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "!!!", "expected": "policy_query", "difficulty": "adversarial"},
    {"query": "呵呵", "expected": "policy_query", "difficulty": "adversarial"},
]


def get_intent_summary() -> Dict[str, Dict[str, int]]:
    """黄金集按意图×难度统计条数（脚本自检用）"""
    out: Dict[str, Dict[str, int]] = {}
    for item in GOLDEN_INTENT_SET:
        intent = item["expected"]
        diff = item["difficulty"]
        out.setdefault(intent, {}).setdefault(diff, 0)
        out[intent][diff] += 1
    return out