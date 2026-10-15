#!/usr/bin/env python3
"""
闲鱼充电宝 RAG 灌库脚本

数据源：闲鱼商品页截图（智选 20000mAh 充电宝 ¥99999）
目标：Qdrant knowledge_base collection
格式：仿照现有 product_sku00X 结构

使用：
  python ingest_xianyu_charge_to_rag.py

环境：
  QDRANT_URL=http://127.0.0.1:6333
  COLLECTION=knowledge_base
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from uuid import uuid4

# 直接从 deploy/.env.dev 读 QWEN_API_KEY + DASHSCOPE_BASE_URL（不依赖 app.core.config）
ENV_FILE = Path(__file__).resolve().parents[2] / "deploy" / ".env.dev"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.getenv("COLLECTION", os.environ.get("QDRANT_COLLECTION", "knowledge_base"))
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 1024


def make_chunk(idx, doc_type, source, title, text, vector=None):
    """生成一个 chunk,id 用 uuid 保唯一;可携带 vector"""
    point = {
        "id": str(uuid4()),
        "payload": {
            "chunk_index": 0,
            "doc_type": doc_type,
            "source": source,
            "title": title,
            "text": text,
        },
    }
    if vector is not None:
        point["vector"] = vector
    return point


def embed_text(text: str, max_retry: int = 3) -> list:
    """调 DashScope text-embedding-v3 -> 1024 维向量"""
    from openai import OpenAI
    client = OpenAI(api_key=QWEN_API_KEY, base_url=DASHSCOPE_BASE_URL, timeout=15.0)
    last_err = None
    for attempt in range(max_retry + 1):
        try:
            r = client.embeddings.create(
                model=EMBEDDING_MODEL, input=text, encoding_format="float", timeout=15
            )
            return r.data[0].embedding
        except Exception as e:
            last_err = e
            if attempt < max_retry:
                wait = 1.0 * (2 ** attempt)
                print(f"  [embed] 重试 {attempt+1}/{max_retry}: {type(e).__name__}, waiting {wait}s")
                time.sleep(wait)
    raise RuntimeError(f"embedding 失败 {max_retry+1} 次: {last_err}")


def embed_texts_batch(texts: list) -> list:
    """批量 embedding;失败时 fallback 单条"""
    from openai import OpenAI
    client = OpenAI(api_key=QWEN_API_KEY, base_url=DASHSCOPE_BASE_URL, timeout=30.0)
    try:
        r = client.embeddings.create(
            model=EMBEDDING_MODEL, input=texts, encoding_format="float", timeout=30
        )
        return [d.embedding for d in r.data]
    except Exception as e:
        print(f"  [batch] {type(e).__name__}, fallback 单条")
        return [embed_text(t) for t in texts]


# =============================================================================
# 10 篇充电宝 chunk
# =============================================================================

CHUNKS = [
    # ------------------------------------------------------------------
    # 0. 速查表 — 最高频问答一次召回（V16.4 修复 RAG 召回率）
    # ------------------------------------------------------------------
    make_chunk(
        0,
        "product",
        "product_sku011_quick_facts",
        "PB1 充电宝 速查 FAQ（高频问答单条召回）",
        """【智选 PB1 充电宝 速查 FAQ】

【基础参数一句话】
- 容量：20000mAh · 能量：74Wh · 重量：约 420g
- 输出：Type-C **65W** + USB-A **18W ×2**（三输出口）
- 输入：Type-C 30W 自充

【政策高频问答】
- Q：能带上飞机吗？ → **能**。74Wh < 民航 100Wh 上限，**可登机不申报**
- Q：1 年坏了找谁？ → **1 年卖家个人质保**(非厂家)，找我我担
- Q：7 天无理由具体？ → 7 天内 + 商品保持原样 + 包装完整 + **运费卖家承担** + 私聊卖家申请

【价格口径】
- 价格：**¥99999 不二价**（朋友送的近全新闲置转让价）
- **不议价、不砍价、不打折**
- 赠品：原装数据线 + 18W 充电头 + 包邮

【卖家身份】
- 卖家账号：**中华模具店**（闲鱼昵称）
- 主业：模具设计制造
- 这台充电宝是**朋友送的近全新闲置转让**，不是厂家零售

【瑕疵坦白（必说）】
- 成色 95 新 / 本体未使用 / 配件齐全
- 外包装 1cm 压痕 + 侧贴闲鱼小标签（撕掉即可）
- 接口/屏幕/外壳无划痕、无瑕疵""",
    ),
    # ------------------------------------------------------------------
    # 1. 总览 — 总入口
    # ------------------------------------------------------------------
    make_chunk(
        0,
        "product",
        "product_sku011_overview",
        "PB1 充电宝 20000mAh 闲置转让详情",
        """【智选科技 PB1 充电宝 20000mAh 闲置转让详情】

【商品来源】
- 品牌型号：智选 20000mAh 充电宝（官方货源，品牌官方）
- 来源类型：**二手闲置转让**（不是厂家零售新品，是店家闲置清出）
- 来源故事：朋友送的/买的近全新货，店家拆开外包装试了下就闲置了，原价 1399 元，因用不上挂在闲鱼闲置转让
- **标的价格 ¥99999 是店家的闲置转让定价**，对应卖家"凑个吉利数 + 闲置留个纪念 + 一分钱一分货"的意图

【核心参数】
- 容量：20000mAh 3.7V（74Wh）
- 输出：Type-C 65W + USB-A 18W ×2（合计 3 个输出口）
- 输入：Type-C 30W 自充
- 重量：约 420g

【产品特点】
- 65W PD 快充，可充 MacBook（约 1.5-2 小时充满一台 MBP 13 寸）
- 三口同时输出（笔记本 + 手机 + 平板同时充）
- 符合民航标准（74Wh < 100Wh），**可携带登机无需申报**
- 智能数显剩余电量（屏显百分比）

【卖家服务承诺】
- 1 年质保（**卖家个人承诺**，非厂家官方，但有问题我负责）
- 容量保 80%（1 年内）
- 7 天无理由退货（保持原样 + 我承担运费）

【卖家备注】
- 卖家账号名："中华模具店"——主业是模具设计制造，**闲置数码配件是店主副业清出**，和模具店主营无关
- 闲置商品秉持"诚信二售、瑕疵坦白、售后我担"原则""",
    ),

    # ------------------------------------------------------------------
    # 2. 电池 / 民航
    # ------------------------------------------------------------------
    make_chunk(
        1,
        "product",
        "product_sku011_battery",
        "PB1 充电宝 电池与民航标准",
        """【智选 PB1 充电宝 电池与民航标准】

【电池核心参数】
- 容量：20000mAh
- 电压：3.7V
- **能量：74Wh**（= 20000 × 3.7 / 1000）
- 满电可充 MacBook 13 寸约 1 次 + iPhone 15 约 4 次 + iPad Air 约 2 次

【民航标准】
- ICAO/IATA 国际民航规定：≤ **100Wh** 可**直接登机无需申报**
- 本品 74Wh < 100Wh → **可携带登机，无需航空公司批准**
- 100Wh-160Wh 之间需航司同意 + 申报
- > 160Wh 禁止登机

【自充规格】
- 自充输入：Type-C 30W
- 30W PD 自充：约 3.5 小时从 0 到 100%
- 5V/2A 普通充：约 12 小时（不推荐）
- 建议使用 30W+ PD 充电头自充（买充电头附带的另算）

【电池保养】
- 长期不用建议保持 50% 电（不要满电也不要空电）
- 1 年内容量保持 ≥ 80%（卖家承诺）""",
    ),

    # ------------------------------------------------------------------
    # 3. 接口与三口
    # ------------------------------------------------------------------
    make_chunk(
        2,
        "product",
        "product_sku011_ports",
        "PB1 充电宝 接口与三口输出",
        """【智选 PB1 充电宝 接口与三口输出】

【3 个输出口 + 1 个输入口】

| 接口 | 规格 | 用途 |
|------|------|------|
| Type-C | **65W PD 输出** + 30W 输入（双向） | 充笔记本/MacBook/手机/自充 |
| USB-A #1 | 18W 输出（QC3.0/AFC/FCP）| 充手机/平板 |
| USB-A #2 | 18W 输出（QC3.0/AFC/FCP）| 充手机/平板/相机 |

【三口同时输出策略】
- Type-C 65W（笔记本优先）+ USB-A ×2（手机 + 平板）
- 笔记本 + 手机 + 平板 三台同时充，**三口独立不抢电**
- 单口最大输出 65W（Type-C），可以单独给 MacBook 满血 PD 65W

【可充设备清单】
- **笔记本**：MacBook Pro/Air 13/14/15/16、ThinkPad X1、Surface、Dell XPS（65W PD 协议）
- **手机**：iPhone 15/Pro Max、华为 Mate/P、小米/红米、OPPO/vivo（PD/QC/AFC/FCP 全协议）
- **平板**：iPad Pro/Air/mini、华为 MatePad、小米平板
- **不能充**：Switch 主机（需 15V，PD 协议通但电压档不对）""",
    ),

    # ------------------------------------------------------------------
    # 4. 适用场景
    # ------------------------------------------------------------------
    make_chunk(
        3,
        "product",
        "product_sku011_usage",
        "PB1 充电宝 适用场景",
        """【智选 PB1 充电宝 适用场景】

【典型使用场景】
1. **出差/旅行**：三口同时输出，笔记本 + 手机 + 平板一机搞定，不用带多个充电宝
2. **MacBook 用户**：65W PD 给 MBP 13 寸满血快充（约 1.5-2 小时充满）
3. **飞机党**：74Wh < 民航 100Wh 限制，**直接登机不申报**
4. **数字游民/咖啡馆办公**：随时补电不焦虑
5. **露营/户外**：可同时充头灯/手机/相机，420g 重量可接受
6. **应急备用**：停电/会议无插座时

【智能数显屏】
- 屏显：剩余电量百分比（精确到 1%）
- 不需要靠"几盏灯"猜剩余电
- 不用的时候按一下电源键就显

【使用注意点】
- 长期不用保持 50% 电量（保电池）
- 飞机上建议全程关闭电源（或按空乘要求使用）
- 65W 输出需 PD 协议充电线（买时附 Type-C to C 线）""",
    ),

    # ------------------------------------------------------------------
    # 5. 售后承诺
    # ------------------------------------------------------------------
    make_chunk(
        4,
        "product",
        "product_sku011_warranty",
        "PB1 充电宝 售后承诺（卖家个人担保）",
        """【智选 PB1 充电宝 售后承诺】

【三大承诺】（**卖家"中华模具店"个人承诺，非厂家官方质保**）

| 承诺 | 时长 | 适用条件 |
|------|------|----------|
| **1 年质保** | 1 年内 | 非人为损坏（自然故障/电池容量低于 80%/接口松动等）|
| **容量保 80%** | 1 年内 | 1 年后实测容量 ≥ 标称 80%（74Wh × 80% = 59Wh）|
| **7 天无理由** | 7 天内 | 商品保持原样 + 包装完整 + 不影响二次销售 |

【质保范围】
- ✅ 包：自然故障、电池衰减、Type-C 接口不良、屏显不准
- ❌ 不包：进水/摔坏/私自拆机/外力损坏

【出问题怎么办】
1. 私聊"中华模具店"客服，说明问题 + 拍照
2. 客服判断是否在质保范围
3. 在范围内：免费补发或退款
4. 不在范围：协商维修费（成本价）或折价回收

【为什么不走厂家】
- 本品是厂家零售品，但**个人闲置转让**不在厂家零售保修通道内
- 卖家承诺：出问题找我我负责，**不甩锅给厂家**
- 这就是为什么价格可以不议价：因为**售后服务我做实了**""",
    ),

    # ------------------------------------------------------------------
    # 6. 议价话术（关键：绝不议价）
    # ------------------------------------------------------------------
    make_chunk(
        5,
        "product",
        "product_sku011_pricing",
        "PB1 充电宝 不二价议价话术（核心）",
        """【智选 PB1 充电宝 不二价议价话术】

【核心原则：¥99999 不二价】
- **价格不议不砍**，¥99999 是店家闲置转让定价，**不接受任何砍价**
- 但用话术让买家**主动接受**这个价，而不是让步

【价值塑造四件套】（买家砍价时按顺序套用）

1. **稀缺性塑造**
   "{nick}~这价格是店家挂闲鱼的闲置转让价，全新国行 1399，店家朋友送的近全新，**图个吉利数 99999（留个纪念）**
   这玩意儿不批量，全闲鱼也就这一台，真心要的话别犹豫~"

2. **品质塑造**
   "{nick}~你对比下规格：20000mAh 74Wh + 65W PD + 三口同输 + 民航标准 + 智能数显屏
   这配置新机上市 1500+ 我都看过，我这 99999 算走心了，**不是因为便宜，是因为闲置转让**~"

3. **服务塑造**
   "{nick}~1 年卖家个人质保 + 7 天无理由 + 容量保 80%，**出问题找我我负责**，不是甩锅给厂家
   这服务在闲鱼二手里算顶格了，新机都不一定这么保~"

4. **诚意塑造 + 赠品代替让价**
   "{nick}~价格不让了，但可以**送这条 100W PD 编织数据线 + 一个 18W PD 充电头 + 包邮**
   这三样单独买 80+，我送你，**总价算下来你更划算**~"

【常见砍价场景应对】（禁用"亲，已经是最低价了"）

| 砍价 | 回应 |
|------|------|
| "太贵了，能少点吗" | → 价值塑造 #1-4 + 赠品 |
| "8000 / 800 / 直接砍到 X" | → "这价格不让的，亲~送数据线 + 充电头 + 包邮" |
| "别人家 5000" | → "别人家什么规格？截图给我对比，我帮你看着" |
| "老客户便宜点" | → "老客也不让价，但**送数据线 + 充电头 + 包邮**，我送多你一点" |
| "我是学生党/刚工作" | → "理解，但价格不让，**送你 18W 充电头**（单买 49）" |
| "我再考虑一下" | → "好，你考虑~但我这台就一台，**卖了我也拿不到第二台**" |

【什么时候降价】
- 不降价。本价位已经是店家底线。
- 实在想砍价的：**拒绝 + 赠品 + 引导拍下** 是唯一话术""",
    ),

    # ------------------------------------------------------------------
    # 7. 差异化对比
    # ------------------------------------------------------------------
    make_chunk(
        6,
        "product",
        "product_sku011_compare",
        "PB1 充电宝 vs 同价位竞品差异化对比",
        """【智选 PB1 充电宝 vs 同价位差异化对比】

【闲鱼同价位竞品】（20000mAh + 65W PD 二手闲置）

| 品牌型号 | 容量 | PD | 价格 | 优 | 劣 |
|---------|-----|-----|------|----|----|
| 罗马仕 20000mAh 65W | 20000mAh 74Wh | 65W | 二手 600-800 | 便宜 | 牌子不够硬、售后无 |
| 小米 20000mAh 50W MAX | 20000mAh 74Wh | 50W | 二手 400-500 | 性价比 | PD 50W 不够 MBP 满血 |
| 华为 12000mAh 66W | 12000mAh | 66W | 二手 500 | 品牌硬 | 容量只有 60%（14400mAh 标称实际容量）|
| Anker 737 24000mAh 140W | 24000 | 140W | 二手 700-900 | 高功率 | 重、贵 |
| **智选 PB1 本品** | 20000mAh 74Wh | **65W** | **¥99999（我这台闲置）** | **74Wh 足 + 三口 + 卖家个人担保** | 不议价（因为本就是闲置转让定价）|

【为什么我这台值 ¥99999】
1. **真实容量足**：20000mAh 标称足 74Wh（不是缩水容量）
2. **品牌官方货源**：外包装/序列号/3C 标齐全（不卖杂牌货）
3. **卖家担保**：1 年质保 + 7 天无理由，不是厂家但我负责
4. **三口同输**：MacBook + 手机 + 平板同时充
5. **卖家承诺"卖家电给我负责"**——闲鱼二手里 **顶格服务**

【买家对比话术】
"你对比下容量 + PD + 三口 + 卖家担保 这 4 项，我这个都满足，**且因为闲置转让定价不议价**，真心喜欢就拍下，价格不让但赠品给你到位~""",
    ),

    # ------------------------------------------------------------------
    # 8. 卖家故事
    # ------------------------------------------------------------------
    make_chunk(
        7,
        "product",
        "product_sku011_seller_story",
        "卖家「中华模具店」闲置转让故事",
        """【卖家"中华模具店"闲置转让故事】

【卖家身份】
- **卖家账号**：中华模具店（闲鱼昵称）
- **主营**：模具设计制造（与数码配件无关，这是主业）
- **副业**：清库存 + 朋友送的闲置数码配件，挂在闲鱼闲置转让

【为什么"中华模具店"会卖充电宝】
- 店主朋友送的闲置礼品，收着没用，又不想便宜贱卖
- **店主闲置转让原则**："诚信二售 + 瑕疵坦白 + 售后我担"
- 不是厂家零售，不是批发，不是赚差价——**纯粹个人闲置转让**

【价格为什么 ¥99999】
- 不是厂家零售定价（原厂 1399）
- 是**朋友送的近全新货**，店家**图个吉利数"99999"留个纪念**
- 这价位是**实诚闲置转让价**，**对应店主"卖一台少一台"的心态**

【怎么看待定价 + 不议价】
- 价格不让，是因为店主觉得"我这成色 + 这配置 + 我担保"值这个价
- 不给砍价空间，是为了避免"砍到一半嫌烦装"走人
- **送数据线 + 充电头 + 包邮作补偿**，让买家**实际到手价值大于支付价**

【买这类"非主营"店家的好处】
- ✅ 闲置转让商品成色好（朋友送的不会买残次）
- ✅ 卖家个人担保（出问题真赔，不是厂家甩锅）
- ✅ 价格实在（不让价但送赠品，**实付价值高于标价**）
- ✅ 沟通直接（店主有话直说，不会绕弯话术）""",
    ),

    # ------------------------------------------------------------------
    # 9. 成色与瑕疵
    # ------------------------------------------------------------------
    make_chunk(
        8,
        "product",
        "product_sku011_used_auth",
        "PB1 充电宝 成色与瑕疵坦白",
        """【智选 PB1 充电宝 成色与瑕疵坦白】

【成色评级】
- **成色**：95 新（近全新未拆封）
- 拆封状态：外包装已拆，**本体未使用**（店家试了下接口显示就收起来了）
- 配件齐全：充电宝 ×1 + Type-C to C 100W 线 ×1 + 说明书 ×1 + 原包装盒 ×1

【瑕疵坦白】（**主动说，不藏着**）

1. **外包装轻微压痕** —— 闲置存放期间外盒一角有 1cm 折痕（**不影响内部商品**，纸盒本身就皱）
2. 序列号外露 —— 包装侧有店家贴的闲鱼交易小标签（撕掉即可，**不影响二次转售**）

【没有任何的瑕疵】
- ✅ Type-C 接口：全新无划痕
- ✅ USB-A ×2 接口：全新无插痕
- ✅ 数显屏：无划痕、无亮点坏点
- ✅ 机身外壳：无划痕、无掉漆、无磕碰
- ✅ 电池容量：100% 满电（店家没放过电）

【拍照展示】
- 实物图为闲鱼商品页主图 3 张
- 闲鱼商品页：**¥99999「智选 20000mAh 充电宝」**，商品页有 3 张实拍图 + 商品详情页

【为什么这么坦白】
- 卖家原则：**瑕疵坦白的商品才值得买**
- 让买家**收到货不惊讶**，**拍照与实物一致**""",
    ),

    # ------------------------------------------------------------------
    # 10. 退换规则
    # ------------------------------------------------------------------
    make_chunk(
        9,
        "product",
        "product_sku011_return_rules",
        "PB1 充电宝 退换货与到手后流程",
        """【智选 PB1 充电宝 退换与到手后流程】

【7 天无理由退款】（适用条件）
- 时效：签收后 7 天（含第 7 天）
- 商品状态：保持原样 + 包装完整 + 不影响二次销售
- 流程：
  1. 私聊卖家"中华模具店"申请退款（提供订单号）
  2. 卖家同意后，寄回商品（**运费卖家承担**）
  3. 卖家收到货检查无问题 → 全额退款

【7 天无理由退款不适用情况】
- 商品已使用（外壳有划痕/接口有插痕/电池电量低于 95%）
- 包装丢失或破损
- 超 7 天

【质量问题退换】（适用条件）
- 1 年质保期内，非人为损坏
- 容量低于标称 80%（实测 ≤ 59Wh）
- Type-C 接口不良 / USB-A 接口不良 / 屏显不准
- 流程：拍照 + 录视频 + 私聊卖家 → 卖家免费补发或退款

【到手后该做什么】（给新买家）
1. **开箱拍照录视频**（保留证据 + 展示给卖家）
2. **测试一下三口输出**：Type-C 65W 给笔记本 + USB-A 同时给手机（验真）
3. **第一次自充**：用 30W+ PD 头自充至 100%
4. **用 PD 线**：不要随便用 5V/2A 的线（功率上不去）

【运费谁出】
- 7 天无理由（保持原样）：**卖家承担**
- 质量问题：卖家承担（来回运费全免）
- 个人原因（不喜欢/多买）：**买家承担**
- 二手闲置不支持换货（同型号无库存），只支持退款""",
    ),
]


def upsert_chunks(chunks):
    """Upsert 到 Qdrant"""
    url = f"{QDRANT_URL}/collections/{COLLECTION}/points"
    body = {"points": chunks}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return {"error": str(e), "body": body_text}


def main():
    print(f"[ingest] QDRANT_URL={QDRANT_URL}")
    print(f"[ingest] COLLECTION={COLLECTION}")
    print(f"[ingest] 共 {len(CHUNKS)} 篇充电宝 chunk")
    print(f"[ingest] QWEN_API_KEY={'SET' if QWEN_API_KEY else 'MISSING'}")
    print(f"[ingest] DASHSCOPE_BASE_URL={DASHSCOPE_BASE_URL}")

    if not QWEN_API_KEY:
        print("[ingest] ❌ QWEN_API_KEY 未配置,无法算 embedding")
        sys.exit(1)

    # 批量 embed
    texts = [c["payload"]["text"] for c in CHUNKS]
    print(f"[ingest] 调 embedding (text-embedding-v3, 1024 维), {len(texts)} 篇...")
    vectors = embed_texts_batch(texts)
    assert len(vectors) == len(CHUNKS), f"vector 数 {len(vectors)} != chunks {len(CHUNKS)}"
    assert len(vectors[0]) == EMBEDDING_DIM, f"dim {len(vectors[0])} != {EMBEDDING_DIM}"

    for c, v in zip(CHUNKS, vectors):
        c["vector"] = v
    print(f"[ingest] embedding 完成 ✅")

    # upsert
    result = upsert_chunks(CHUNKS)
    print(f"[ingest] upsert 返回：{result}")

    if isinstance(result, dict) and result.get("status") == "ok":
        print(f"[ingest] ✅ {len(CHUNKS)} 篇已写入 Qdrant")
    else:
        print(f"[ingest] ❌ 写入失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
