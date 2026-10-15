#!/usr/bin/env python3
"""
简化版评测对比表生成（4 列 + 我的判断）

输入：docs/eval/xianyu_charge_raw_results.json
输出：docs/eval/xianyu_charge_eval_2026-10-15.md
"""
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
RAW = HERE / "docs" / "eval" / "xianyu_charge_raw_results.json"
OUT = HERE / "docs" / "eval" / "xianyu_charge_eval_2026-10-15.md"

QUESTIONS_PREVIEW = {
    1: ("闲聊", "在吗?"),
    2: ("参数", "这个充电宝多大容量?输出多少瓦?"),
    3: ("参数/三口", "能同时充笔记本和手机吗?"),
    4: ("政策/民航", "能带上飞机吗?"),
    5: ("政策/质保", "1 年坏了找谁?"),
    6: ("议价/价格", "¥99999 怎么这么贵啊?"),
    7: ("议价/砍价", "8000 行不行?"),
    8: ("类比", "跟罗马仕比有什么区别?"),
    9: ("边界/卖家名", "你是「中华模具店」?怎么卖起充电宝来了?"),
    10: ("退款/转人工", "我要退款"),
    11: ("参数/单位", "20000 毫安时等于多少瓦时?"),
    12: ("场景/MacBook", "充 MacBook 多久能充满?"),
    13: ("政策/退换", "7 天无理由具体怎么操作?"),
    14: ("议价/老客", "我是老客户,便宜点吧"),
    15: ("瑕疵", "是二手的吗?有什么瑕疵?"),
}

# 我手工判定表（基于真实回答逐题判断）
# ✅ 对 / 🟡 部分对 / ❌ 错
JUDGMENTS = {
    1: ("🟡", "意图误判成 order_query 触发 ASK_LOGIN，没真答闲聊。不是 RAG/数据问题，是 intent_service 真 bug。"),
    2: ("🟡", "容量 20000mAh + 74Wh 对，但 RAG 文档没显式说『输出 65W』，LLM 只答『给 MacBook 约 45W / 30W 自充』自创答案。RAG sku011_ports 应该补『Type-C 输出 65W』明确数值。"),
    3: ("✅", "三口 + 65W PD + USB-A 18W×2 + 互不抢电 + 民航标注全对。"),
    4: ("✅", "74Wh<100Wh 民航标准登机不申报答对，附『不可托运 + 卖家承诺 1 年质保』细节。"),
    5: ("✅", "1 年质保答对 + 明确『卖家个人承诺，非厂家官方』(虽然没答“请找我”，但意思到位)。"),
    6: ("✅", "不二价设计真生效：『朋友送的近全新 + 图个吉利数 + 卖一台少一台 + 新机 1500+ + 赠品补偿』价值塑造四件套全到位。"),
    7: ("✅", "8000 直接拒 + 不二价立锚 + 赠品补偿(数据线+充电头+包邮)，干净利落，标杆话术。"),
    8: ("🟡", "罗马仕对比有差异化点(品牌背书+1年质保)，但漏说 PD/三口/容量 等具体规格(LLM 自由发挥)。RAG sku011_compare 已经写好但没召回。"),
    9: ("✅", "边界故事兜底完美：『中华模具店主业 + 朋友送的近全新闲置 + 不是厂家零售 + 95新未拆封』全到位。"),
    10: ("❌", "fixture 限制：buyer_unb=valid mysql user 才走真退款路径。生产真跑通的 XianyuRefundHandler 应该拒重复退款 + 转人工，本次没验证。"),
    11: ("✅", "LLM 真推理能力：20000 × 3.7V ÷ 1000 = 74Wh 自算，不是死记。"),
    12: ("✅", "MacBook 13 寸 1.5-2 小时 + 65W PD 满血 + 需 Type-C to C 线(PQ附赠)，答得具体。"),
    13: ("🟡", "7 天无理由流程答对(签收 7 天内/未激活/包装完好/配件齐全/48小时寄回/1-3 工作日退款)，但漏说『运费谁出』『卖家承担』。RAG 文档写明但没召回。"),
    14: ("✅", "老客拒让价正确(¥99999 不二价立场一致)，加分项是回答了『我是新客 → 后续复购券』逻辑闭环。"),
    15: ("🟡", "95新/未拆封答对，但漏说 RAG 写明的『外包装 1cm 压痕 + 侧贴小标签』瑕疵坦白，这是核心诚信点。RAG sku011_used_auth 应该召回但 LLM 没用上。"),
}

VERDICT_SUM = {
    "✅": "对",
    "🟡": "部分对",
    "❌": "错",
}

results = json.loads(RAW.read_text(encoding="utf-8"))
results.sort(key=lambda r: r["id"])

total = len(results)
counts = {"✅": 0, "🟡": 0, "❌": 0}
for r in results:
    counts[JUDGMENTS[r["id"]][0]] += 1
score = round((counts["✅"] + counts["🟡"] * 0.5) / total * 100, 1)
RAG_HIT = sum(1 for r in results if r["ctx_has_sku011"])
AVG_LAT = round(sum(r["latency_sec"] for r in results) / total, 1)

now = datetime.now().strftime("%Y-%m-%d %H:%M")

md = []
md.append("# 闲鱼充电宝话术准确率评测")
md.append("")
md.append(f"> {now} · 商品 PB1 20000mAh ¥99999 闲鱼闲置 · 卖家「中华模具店」")
md.append("")
md.append("## 评分")
md.append("")
md.append(f"对：{counts['✅']} / 部分对：{counts['🟡']} / 错：{counts['❌']} ｜ **加权得分 {score}%** ｜ RAG 召回 {RAG_HIT}/{total} ｜ 平均 {AVG_LAT}s/题")
md.append("")
md.append("---")
md.append("")
md.append("## 15 题对照")
md.append("")

# 4 列：提问 / 预期 / 真实 / 判断
md.append("| # | 提问 | 预期回答（基于商品参数 + 不二价话术）| 真实回答（节选）| 判断 |")
md.append("|---|------|------------------------------------|-------------|------|")
md.append("| 1 | 在吗? | 在的 您好 请问有什么可以帮您 | 用户尚未登录，无法查询个人订单/退款信息... | 🟡 闲聊答错（intent 误判成 order_query）|")
md.append("| 2 | 多大容量？输出多少瓦？ | 20000mAh (74Wh) + Type-C 输出 65W + USB-A 18W×2 | 容量 20000mAh / 74Wh / 30W 自充，未明确答输出 65W | 🟡 容量对但输出功率自创 |")
md.append("| 3 | 能同时充笔记本和手机吗？ | 三口同输 + Type-C 65W 充笔记本 + USB-A×2 各 18W，互不抢电 | 三口 + 65W PD + USB-A 18W×2 互不抢电 + 民航 + 赠品全到位 | ✅ 对 |")
md.append("| 4 | 能带上飞机吗？ | 74Wh < 民航 100Wh 限制，可登机不申报 | 74Wh < 100Wh 民航标准登机 + 不可托运 + 卖家承诺 1 年质保 | ✅ 对 |")
md.append("| 5 | 1 年坏了找谁？ | 1 年卖家质保(非厂家官方) + 找我我担 | 1 年内找卖家 + 个人承诺 + 电池 6 个月保修 | ✅ 对 |")
md.append("| 6 | ¥99999 怎么这么贵？ | 不二价 + 朋友送的近全新 + 卖一台少一台 + 赠品补偿 | 图个吉利数 + 卖一台少一台 + 新机 1500+ + 数据线充电头包邮 | ✅ 对 |")
md.append("| 7 | 8000 行不行？ | ¥99999 不让价 + 赠品补偿 | 不二价 + 赠品(数据线+充电头+包邮) | ✅ 对 |")
md.append("| 8 | 跟罗马仕比有啥区别？ | 容量足+三口+卖家担保+1年质保 | 品牌背书强 + 1年质保 vs 罗马仕无售后；具体规格漏说 | 🟡 答了但漏细节 |")
md.append("| 9 | 你是中华模具店？怎么卖充电宝？ | 模具店主家 + 朋友送的近全新闲置转让 + 不是厂家 | 主业模具 + 朋友送的近全新闲置 + 不是厂家零售 + 95新 | ✅ 对 |")
md.append("| 10 | 我要退款 | 转人工 + 不自动退款（XianyuRefundHandler）| 用户尚未登录，无法查询个人订单... | ❌ fixture 限制未真验 |")
md.append("| 11 | 20000mAh = ? Wh | 74Wh (按 3.7V 换算) | 20000 × 3.7V ÷ 1000 = 74Wh + 民航标准 | ✅ 对 |")
md.append("| 12 | 充 MacBook 多久？ | 65W PD 满血，约 1.5-2 小时 | 1.5-2 小时 + 65W PD 满血 + Type-C to C 线 | ✅ 对 |")
md.append("| 13 | 7 天无理由怎么操作？ | 7 天内 + 原样 + 包装 + 私聊卖家 + 卖家承担运费 | 7天/未激活/包装完好/48h寄回，但运费谁出未说 | 🟡 流程对但漏运费细节 |")
md.append("| 14 | 我是老客户便宜点？ | ¥99999 不让价 + 赠品补偿 + 老客额外多送 | 不二价 + 赠品 + 后续复购券 | ✅ 对 |")
md.append("| 15 | 二手的吗？有瑕疵吗？ | 95新未拆封 + 外包装压痕 + 侧贴小标签（瑕疵坦白）| 95新未拆封无使用痕迹，未说瑕疵细节 | 🟡 答了但漏瑕疵坦白 |")
md.append("")

md.append("---")
md.append("")
md.append("## 一句话结论")
md.append("")
md.append(f"加权 {score}%——基础参数✅、不二价价值塑造✅、边界兜底✅、LLM 推理✅；" \
           "漏在 3 个细节（输出 65W 数值/运费谁出/瑕疵坦白），根因是 RAG 召回不够强（10 篇没覆盖速查表结构）+ intent classifier 把『在吗』误判成 order_query。")
md.append("")
md.append(f"完整答案原文: `xianyu_charge_raw_results.json`")
md.append("")

OUT.write_text("\n".join(md), encoding="utf-8")
print(f"✓ 对比表已写入: {OUT}")
print(f"  评分: 对 {counts['✅']} / 部分对 {counts['🟡']} / 错 {counts['❌']} = 加权 {score}%")
print(f"  RAG 召回: {RAG_HIT}/{total} 题")
