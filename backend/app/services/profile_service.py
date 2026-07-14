"""
Profile Service - 用户长程记忆 (P2 长程记忆)

按 §6 规则：services/ 编排层，调 models/user_profile
被 chat/orchestrator.py 调用，每轮 /chat 启动期加载 + done 后更新。

设计：
- 1:1 → users.id（user_profiles.user_id 是 PK）
- 灰度开关：调用方按 settings.ENABLE_USER_PROFILE 决定是否调用
- best-effort 写：profile DB 异常仅 warning，不影响主流程
- 隐私保护：clear() 接口让用户可彻底删除；匿名（user_id=0）不维护

§3.3 YAGNI 边界：
- 不做事件流（user_profile_events），需要时 messages JOIN 即可
- 不做派生画像（user_personas），summary 字段够用
- 不做租户级画像，profile 跟 user_id 1:1
"""
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from app.clients.mysql_client import with_safe_session
from app.models.user_profile import UserProfile

logger = logging.getLogger(__name__)


# =============================================================
# 读取
# =============================================================
def get_or_create(user_id: int) -> Optional[UserProfile]:
    """
    加载用户 profile（不存在则建空）。

    返回:
        UserProfile 实例（行不存在时自动 INSERT 空行）；DB 异常返 None（best-effort）

    注意:
        返回的 ORM 对象在 session close 后字段仍可访问（expire_on_commit=False），
        但跨 session 边界访问 lazy-loaded 字段会触发 DetachedInstanceError。
        调用方应在 with_safe_session 块内完成字段读取，或用 to_prompt_block 转换。
    """
    if not user_id:
        return None

    try:
        with with_safe_session(commit=True) as db:
            row = db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id, UserProfile.deleted == 0
                )
            ).scalar_one_or_none()
            if row is not None:
                return row

            # 不存在 → 建空 profile（best-effort 写）
            new_row = UserProfile(
                user_id=user_id,
                summary=None,
                frequent_skus=[],
                preferences={},
                interaction_count=0,
                last_active_at=None,
            )
            db.add(new_row)
            # commit 由 with_safe_session 块结束统一触发
            return new_row
    except Exception as e:
        logger.warning(f"profile_service.get_or_create failed: user_id={user_id}, {e}")
        return None


# =============================================================
# 写入
# =============================================================
def update_summary(user_id: int, summary: str) -> bool:
    """
    替换式更新 summary 字段（LLM 摘要后调用）。

    Returns:
        True=成功；False=失败（仅 warning 不抛）
    """
    if not user_id:
        return False
    try:
        with with_safe_session(commit=True) as db:
            row = db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id, UserProfile.deleted == 0
                )
            ).scalar_one_or_none()
            if row is None:
                row = UserProfile(user_id=user_id, summary=summary)
                db.add(row)
            else:
                row.summary = summary
                row.last_active_at = datetime.utcnow()
            return True
    except Exception as e:
        logger.warning(f"profile_service.update_summary failed: user_id={user_id}, {e}")
        return False


def append_frequent_skus(user_id: int, new_skus: List[str], max_keep: int = 20) -> bool:
    """
    追加 SKU 到 frequent_skus（去重 + 截断到 max_keep）。

    Args:
        user_id: 用户 ID
        new_skus: 本轮新提过的 SKU 列表
        max_keep: 最多保留多少个 SKU（防 list 膨胀）

    Returns:
        True=成功；False=失败
    """
    if not user_id or not new_skus:
        return False
    try:
        with with_safe_session(commit=True) as db:
            row = db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id, UserProfile.deleted == 0
                )
            ).scalar_one_or_none()
            existing: list = []
            if row is not None and row.frequent_skus:
                existing = list(row.frequent_skus)
            # 去重合并（保序：旧的在前，新的在后；新的 SKU 排后面）
            seen = set(existing)
            for s in new_skus:
                if s and s not in seen:
                    existing.append(s)
                    seen.add(s)
            merged = existing[-max_keep:]  # 仅保留最近 max_keep 个

            if row is None:
                row = UserProfile(
                    user_id=user_id,
                    frequent_skus=merged,
                    last_active_at=datetime.utcnow(),
                )
                db.add(row)
            else:
                row.frequent_skus = merged
                row.last_active_at = datetime.utcnow()
            return True
    except Exception as e:
        logger.warning(
            f"profile_service.append_frequent_skus failed: user_id={user_id}, {e}"
        )
        return False


def increment_interaction(user_id: int, delta: int = 1) -> bool:
    """
    累加 interaction_count（每轮 user 消息 +1）。

    Returns:
        True=成功；False=失败
    """
    if not user_id:
        return False
    try:
        with with_safe_session(commit=True) as db:
            row = db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id, UserProfile.deleted == 0
                )
            ).scalar_one_or_none()
            if row is None:
                row = UserProfile(
                    user_id=user_id,
                    interaction_count=delta,
                    last_active_at=datetime.utcnow(),
                )
                db.add(row)
            else:
                row.interaction_count = (row.interaction_count or 0) + delta
                row.last_active_at = datetime.utcnow()
            return True
    except Exception as e:
        logger.warning(
            f"profile_service.increment_interaction failed: user_id={user_id}, {e}"
        )
        return False


def clear(user_id: int) -> bool:
    """
    隐私删除（软删）：deleted=1，保留行做审计。

    Returns:
        True=成功；False=失败 / 行不存在
    """
    if not user_id:
        return False
    try:
        with with_safe_session(commit=True) as db:
            row = db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id, UserProfile.deleted == 0
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted = 1
            row.summary = None
            row.frequent_skus = []
            row.preferences = {}
            return True
    except Exception as e:
        logger.warning(f"profile_service.clear failed: user_id={user_id}, {e}")
        return False


# =============================================================
# 格式化（注入 LLM prompt）
# =============================================================
# M2：profile_block 硬上限（防 prompt 膨胀；与现有 context_block 同设计）
MAX_PROFILE_PROMPT_LEN = 200


def to_prompt_block(profile: Optional[UserProfile], max_len: int = MAX_PROFILE_PROMPT_LEN) -> str:
    """
    把 UserProfile 转成可注入 LLM 的 prompt 文本块。

    设计：
    - 仅在 profile 有内容时才返回非空字符串
    - 输出结构化（用户能一眼看到；LLM 也好引用）
    - 硬上限 max_len 字（防膨胀）
    - 反幻觉：尾部加"仅作参考"标签，避免 LLM 编造未在 profile 中的用户事实

    Args:
        profile: UserProfile 实例（None 返空串）
        max_len: 块最大字符数（默认 200）

    Returns:
        多行字符串；profile 为空时返 ""
    """
    if profile is None:
        return ""

    lines: List[str] = []

    # 1. 偏好 tags（结构化优先展示）
    if profile.preferences:
        # 取前 3 个偏好 tag
        pref_items = list(profile.preferences.items())[:3]
        pref_str = "、".join(f"{k}={v}" for k, v in pref_items if v)
        if pref_str:
            lines.append(f"- 偏好：{pref_str}")

    # 2. 提过的商品 SKU
    if profile.frequent_skus:
        skus = profile.frequent_skus[:5]  # 最多展示 5 个
        lines.append(f"- 最近提过的商品：{' / '.join(skus)}")

    # 3. 摘要（截断到 max_len 的 60%）
    if profile.summary:
        summary_max = max_len * 6 // 10
        summary_text = profile.summary[:summary_max]
        if len(profile.summary) > summary_max:
            summary_text += "…"
        lines.append(f"- 画像摘要：{summary_text}")

    # 4. 交互次数（轻量元数据）
    if profile.interaction_count and profile.interaction_count >= 3:
        lines.append(f"- 累计对话：{profile.interaction_count} 轮")

    if not lines:
        return ""

    # 反幻觉 hard label：与现有 M9.5 反幻觉 prompt 同模式
    # max_len 是整个 block 的总上限（含 label），防 prompt 膨胀
    prefix = "【当前用户画像】(跨 session 长程记忆，仅作参考，不得编造未在 profile 中出现的用户事实)\n"
    body = "\n".join(lines)
    block = prefix + body

    # 整体硬截断（prefix + body 总和超 max_len → 末尾加 "…"）
    if len(block) > max_len:
        block = block[: max_len - 1] + "…"
    return block
