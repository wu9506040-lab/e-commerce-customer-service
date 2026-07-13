"""
InputGuard - 3 层输入守卫，防 token 滥用

按 CLAUDE.md §5 Scope Lock：services/ 做业务编排（不直接连 DB，不写路由）
本服务被 app/api/chat.py 在 Synthesizer.run_stream 之前调用

3 层防御（按成本从低到高）：
  L1 规则（0 token）：
    - 长度 [2, 500]
    - 字符多样性 ≥ 0.15（防"啊啊啊啊"）
    - 中文比例 ≥ 20%（防纯英文 / 纯数字）
    - 黑名单关键词（攻击 prompt）
  L2 Embedding 闲聊识别（极便宜 ~0.0001 元/次）：
    - cosine(query, domain_centroid) < 0.4 → 闲聊
    - centroid 由 guard_centroid.py 懒加载
  L3 行为（Redis 计数）：
    - 短期重复：1min 内同 md5(query) > 3 次 → spam
    - 切换频率：1min 内不同 query > 20 次 → 静默限速（M11+ 再加）

设计原则：
  - 0 LLM token 消耗（不调 Qwen，不调 Qwen 包装的 LLM）
  - 黑名单静默不响应（不告诉攻击者"被识别了"）
  - 闲聊走固定模板话术（不让用户感觉被冷落）
  - 命中时返 GuardResult.allowed=False，调用方走 SSE 流
"""
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from app.clients.redis_client import get_client as redis_get
from app.core.providers.embedding import EmbeddingError, get_embedding_provider
from app.services.config_loader import get_config_loader
from app.services.guard_centroid import get_domain_centroid

logger = logging.getLogger(__name__)


# =============================================================
# 业务规则（启动期加载一次，来自 config/business_rules/guard.yaml）
# 改阈值 / 话术 → 改 YAML → 重启服务（roadmap §3.5 不参与热更新）
# 加载失败 → RuntimeError（启动期 fail-fast，不在运行时隐藏错误）
# =============================================================
_RULES = get_config_loader().load("guard")

# === L1 规则阈值（0 token 拦截）===
MIN_LEN: int = _RULES["MIN_LEN"]
MAX_LEN: int = _RULES["MAX_LEN"]
MIN_CHAR_DIVERSITY: float = _RULES["MIN_CHAR_DIVERSITY"]
MIN_CHINESE_RATIO: float = _RULES["MIN_CHINESE_RATIO"]

# === L2 Embedding 领域相关性阈值 ===
# 经验值 0.4 太松（"今天天气" 类闲聊仍会过），提到 0.55 更稳
# 0.55 含义：与"电商领域 30 条代表 query"的平均向量 cosine 至少 0.55 才算领域内
DOMAIN_RELEVANCE_THRESHOLD: float = _RULES["DOMAIN_RELEVANCE_THRESHOLD"]

# === L3 重复检测窗口 ===
REPEAT_WINDOW_SECONDS: int = _RULES["REPEAT_WINDOW_SECONDS"]
REPEAT_MAX_IN_WINDOW: int = _RULES["REPEAT_MAX_IN_WINDOW"]

# === 闲聊 / 拒答 话术模板 ===
CHITCHAT_RESPONSES: dict = _RULES["CHITCHAT_RESPONSES"]

# 重复检测的 Redis key 前缀
_REPEAT_KEY_PREFIX = "guard:repeat:"


# 黑名单关键词（prompt injection / jailbreak）
_BLACKLIST_PATTERNS = [
    re.compile(r"忽略.{0,5}(指令|提示|以上|之前)", re.IGNORECASE),
    re.compile(r"\bDAN\b", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"reveal\s*prompt", re.IGNORECASE),
    re.compile(r"越狱", re.IGNORECASE),
]

# 英文 SKU 前缀（豁免中文比例检查）
_SKU_PATTERN = re.compile(r"\b(?:ZP|BP|WS|PT|LB|KB|MS|ORD)[\w-]*\b", re.IGNORECASE)

# M13 修复：纯订单号查询应当直接放行（不应被 L2 embedding 误判为闲聊）
# 因为订单号（ORD+8位日期+3-6位字母数字）的 embedding 与电商领域 centroid cosine 通常 < 0.4
_ORDER_NO_FULL_RE = re.compile(r"^ORD\d{8}[A-Z0-9]{3,6}$", re.IGNORECASE)


# =============================================================
# GuardResult
# =============================================================
@dataclass
class GuardResult:
    """guard 检查结果"""
    allowed: bool
    reason: Optional[str] = None       # "too_short" / "no_service" / "spam" / "blacklist"
    response: Optional[str] = None     # 命中时直接返给用户的话术（None=静默）
    layer: Optional[str] = None        # 哪一层拦的："L1" / "L2" / "L3"


# =============================================================
# 字符分析工具
# =============================================================
def _char_diversity(s: str) -> float:
    """字符多样性 = 不同字符数 / 总字符数（去空白）"""
    stripped = s.replace(" ", "").replace("\n", "").replace("\t", "")
    if not stripped:
        return 0.0
    return len(set(stripped)) / len(stripped)


def _chinese_ratio(s: str) -> float:
    """中文字符占比（CJK Unified Ideographs U+4E00–U+9FFF）"""
    if not s:
        return 0.0
    chinese = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return chinese / len(s)


def _has_english_sku(s: str) -> bool:
    """是否含英文 SKU（如 ZP1 / BP1 / ORD20260621002）"""
    return bool(_SKU_PATTERN.search(s))


def _md5(s: str) -> str:
    """短 md5（用于 Redis key）"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


# =============================================================
# InputGuard
# =============================================================
class InputGuard:
    """3 层输入守卫（单例即可，无状态）"""

    # -------------------------------------------------------------
    # L1 规则
    # -------------------------------------------------------------
    def _check_l1(self, query: str) -> Optional[GuardResult]:
        """L1 规则检查；通过返 None，否则返 GuardResult"""
        stripped = (query or "").strip()

        if not stripped:
            return GuardResult(False, "too_short", CHITCHAT_RESPONSES["too_short"], "L1")
        if len(stripped) < MIN_LEN:
            return GuardResult(False, "too_short", CHITCHAT_RESPONSES["too_short"], "L1")
        if len(stripped) > MAX_LEN:
            return GuardResult(False, "too_long", CHITCHAT_RESPONSES["too_long"], "L1")

        # 字符多样性（"啊啊啊啊啊啊啊" 类）
        if _char_diversity(stripped) < MIN_CHAR_DIVERSITY:
            return GuardResult(False, "spam", CHITCHAT_RESPONSES["too_short"], "L1")

        # 黑名单（prompt injection / jailbreak）— 静默
        for pat in _BLACKLIST_PATTERNS:
            if pat.search(stripped):
                logger.warning(f"[guard L1] 黑名单命中: {stripped[:50]!r}")
                return GuardResult(False, "blacklist", None, "L1")

        # 中文比例（纯英文 / 纯数字 → 不像中文用户）
        ratio = _chinese_ratio(stripped)
        if ratio < MIN_CHINESE_RATIO and not _has_english_sku(stripped):
            return GuardResult(False, "english_no_sku", CHITCHAT_RESPONSES["english_no_sku"], "L1")

        return None

    # -------------------------------------------------------------
    # L2 Embedding 领域相关性
    # -------------------------------------------------------------
    def _check_l2(self, query: str) -> Optional[GuardResult]:
        """L2 闲聊识别；centroid 失败时静默放行"""
        # M13.1 修复：纯订单号或含 SKU 的 query 直接放行
        # （L2 cosine 对"ZP1 规格参数"这类纯属性词几乎必然 < 0.4，业务 query 不该被拦）
        stripped = query.strip()
        if _ORDER_NO_FULL_RE.match(stripped) or _SKU_PATTERN.search(stripped):
            return None
        centroid = get_domain_centroid()
        if centroid is None:
            # centroid 算不出来（embedding API 挂了等）— 放行，不误伤
            logger.warning("[guard L2] centroid 不可用，跳过闲聊识别")
            return None

        try:
            # embed_text_or_mock 失败时返零向量，但 zero vector cosine=0 会被判闲聊
            # 为了避免误伤 embedding 失败的情况，单独 try 一遍
            try:
                q_emb = get_embedding_provider().embed_text(query)
            except EmbeddingError as e:
                logger.warning(f"[guard L2] embed_text 失败，跳过闲聊识别: {e}")
                return None

            # cosine similarity
            dot = sum(a * b for a, b in zip(q_emb, centroid))
            # q_emb 和 centroid 都是 normalized（embedding API 输出 normalized，centroid 也 normalize 过）
            # 所以 cosine = dot
            sim = dot
            logger.info(f"[guard L2] domain cosine={sim:.3f} query={query[:30]!r}")

            if sim < DOMAIN_RELEVANCE_THRESHOLD:
                return GuardResult(False, "no_service", CHITCHAT_RESPONSES["no_service"], "L2")
            return None
        except Exception as e:
            logger.exception(f"[guard L2] 异常（放行）: {e}")
            return None

    # -------------------------------------------------------------
    # L3 行为：短期重复
    # -------------------------------------------------------------
    def _check_l3(self, user_id: int, query: str) -> Optional[GuardResult]:
        """L3 重复检测（Redis 计数 1min 窗口）"""
        # 匿名用户（user_id=0）跳过 L3（无法稳定计数）
        if user_id <= 0:
            return None

        try:
            key = f"{_REPEAT_KEY_PREFIX}{user_id}:{_md5(query)}"
            r = redis_get()
            count = r.incr(key)
            if count == 1:
                r.expire(key, REPEAT_WINDOW_SECONDS)
            if count > REPEAT_MAX_IN_WINDOW:
                logger.info(
                    f"[guard L3] 重复检测: user={user_id} count={count} "
                    f"query={query[:30]!r}"
                )
                return GuardResult(False, "spam", CHITCHAT_RESPONSES["spam"], "L3")
            return None
        except Exception as e:
            # Redis 挂了别误伤，放行
            logger.warning(f"[guard L3] Redis 异常（放行）: {e}")
            return None

    # -------------------------------------------------------------
    # 主入口
    # -------------------------------------------------------------
    def check(self, query: str, user_id: int) -> GuardResult:
        """
        完整 3 层检查

        Args:
            query: 用户输入
            user_id: 用户 ID（0 = 匿名）

        Returns:
            GuardResult(allowed=True) = 通过
            GuardResult(allowed=False, response=...) = 命中，调用方应直接返 response
        """
        if query is None:
            return GuardResult(False, "too_short", CHITCHAT_RESPONSES["too_short"], "L1")

        # L1：规则（最便宜）
        r = self._check_l1(query)
        if r is not None:
            self._record_block(r)
            return r

        # L2：embedding 闲聊识别
        r = self._check_l2(query)
        if r is not None:
            self._record_block(r)
            return r

        # L3：行为（短期重复）
        r = self._check_l3(user_id, query)
        if r is not None:
            self._record_block(r)
            return r

        return GuardResult(allowed=True)

    @staticmethod
    def _record_block(r: GuardResult) -> None:
        """记录 guard 命中 metrics（best-effort）"""
        try:
            from app.services.metrics import metrics
            metrics.inc_embedding("guard_block")
        except Exception:
            pass
        logger.info(f"[guard] blocked layer={r.layer} reason={r.reason}")


# 单例
guard = InputGuard()
