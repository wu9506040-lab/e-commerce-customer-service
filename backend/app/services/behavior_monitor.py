"""
BehaviorMonitor - 异常行为监控（M11.5 P2）

按 CLAUDE.md §5 Scope Lock：services/ 做业务编排
本服务被 app/api/chat.py 在 guard 检查之后、Synthesizer 之前调用

监控维度（5 类，按 Redis 滑动窗口）：
  - IP 高频：同 IP 1min 内 > 30 请求（脚本/撞库）
  - IP 多账号：同 IP 1h 内关联 > 5 个 user_id（撞库 / 共享账号）
  - User 高频：同 user 1min 内 > 15 请求（脚本/滥用）
  - SKU 探测：同 user 1min 内切换 > 5 个不同 SKU（试探商品库）
  - Order 探测：同 user 1min 内切换 > 3 个不同 order_no（试探订单库）

设计原则：
  - 只监控不拦截（异常时 WARNING 日志 + metrics 计数）
  - 不阻塞业务（Redis 异常一律放行 + log）
  - 阈值保守：正常人/前端打字机速率打不到上限
  - 不留 PII：日志只记录 user_id / sku / order_no 摘要（不记录 query 全文）

Redis Key 设计：
  - bm:ip:req:{minute_bucket}          INCR + EXPIRE 120s
  - bm:ip:users:{hour_bucket}          SADD user_id + EXPIRE 3700s
  - bm:user:req:{minute_bucket}        INCR + EXPIRE 120s
  - bm:user:sku:{minute_bucket}        SADD sku + EXPIRE 120s
  - bm:user:order:{minute_bucket}      SADD order_no + EXPIRE 120s

副作用：
  - metrics.inc_behavior_alert(alert_type)
  - logger.warning(f"[behavior] alert_type={type} ...")
"""
import logging
import time
from typing import Optional

from app.clients.redis_client import get_client as redis_get

logger = logging.getLogger(__name__)


# =============================================================
# 阈值配置（保守值，正常用户操作不会触发）
# =============================================================
# IP 高频：1min 内 > 30 次请求（正常人手工 ≈ 1次/2s = 30/min 是上限）
IP_REQ_PER_MIN_THRESHOLD = 30
# IP 多账号：1h 内 > 5 个不同 user_id（撞库 / 共享账号）
IP_USERS_PER_HOUR_THRESHOLD = 5
# User 高频：1min 内 > 15 次请求（前端打字机 + 切页面 ≈ 1次/4s = 15/min 是上限）
USER_REQ_PER_MIN_THRESHOLD = 15
# SKU 探测：1min 内切 > 5 个不同 SKU（人手翻商品也打不到）
USER_SKU_SWITCH_PER_MIN = 5
# Order 探测：1min 内切 > 3 个不同 order_no（最敏感的指标）
USER_ORDER_SWITCH_PER_MIN = 3


# =============================================================
# Redis key 前缀
# =============================================================
_IP_REQ_KEY = "bm:ip:req:"            # {ip}:{minute_bucket}
_IP_USERS_KEY = "bm:ip:users:"        # {ip}:{hour_bucket}
_USER_REQ_KEY = "bm:user:req:"        # {user_id}:{minute_bucket}
_USER_SKU_KEY = "bm:user:sku:"        # {user_id}:{minute_bucket}
_USER_ORDER_KEY = "bm:user:order:"    # {user_id}:{minute_bucket}

# key TTL（留点余量，避免边界时刻少算）
_REQ_TTL = 120    # 2min 覆盖一个完整分钟桶
_HOUR_TTL = 3700   # 1h+100s 覆盖一个完整小时桶


# =============================================================
# 时间桶
# =============================================================
def _minute_bucket() -> int:
    return int(time.time() // 60)


def _hour_bucket() -> int:
    return int(time.time() // 3600)


# =============================================================
# 异常类型常量（metrics 用，避免 typo）
# =============================================================
class AlertType:
    IP_HIGH_FREQ = "ip_high_freq"            # IP 单分钟请求过多
    IP_MULTI_ACCOUNT = "ip_multi_account"    # IP 单小时多 user_id
    USER_HIGH_FREQ = "user_high_freq"        # user 单分钟请求过多
    USER_SKU_PROBE = "user_sku_probe"        # user 单分钟切 SKU 过多
    USER_ORDER_PROBE = "user_order_probe"    # user 单分钟切 order 过多


# =============================================================
# BehaviorMonitor
# =============================================================
class BehaviorMonitor:
    """M11.5 P2：异常行为监控（只告警不拦截）"""

    @staticmethod
    def record_request(
        ip: Optional[str],
        user_id: Optional[int],
        sku: Optional[str] = None,
        order_no: Optional[str] = None,
    ) -> None:
        """记录一次 chat 请求行为

        Args:
            ip: 客户端 IP（None = 取不到，可能是本地测试）
            user_id: 用户 ID（None 或 0 = 匿名）
            sku: 当前商品 SKU（M9.5 从 /shop/:sku 跳转携带）
            order_no: 当前订单号（M9.5 从 OrderCard 跳转携带）

        行为：
            1. Redis INCR/SADD 计数
            2. 检测 5 类阈值
            3. 触发告警 → metrics + WARNING 日志

        降级：Redis 异常一律放行 + log，不影响业务。
        """
        try:
            r = redis_get()
            minute = _minute_bucket()
            hour = _hour_bucket()

            # ---- IP 维度 ----
            if ip:
                ip_req_key = f"{_IP_REQ_KEY}{ip}:{minute}"
                ip_req_count = r.incr(ip_req_key)
                if ip_req_count == 1:
                    r.expire(ip_req_key, _REQ_TTL)

                ip_users_key = f"{_IP_USERS_KEY}{ip}:{hour}"
                # user_id 有效才计入多账号（匿名不计）
                if user_id and user_id > 0:
                    r.sadd(ip_users_key, user_id)
                    r.expire(ip_users_key, _HOUR_TTL)

                # 阈值 1：IP 高频
                if ip_req_count == IP_REQ_PER_MIN_THRESHOLD + 1:
                    _alert(AlertType.IP_HIGH_FREQ, {
                        "ip": ip,
                        "req_count": ip_req_count,
                        "window": "1min",
                        "user_id": user_id,
                    })

                # 阈值 2：IP 多账号（每次 sadd 后查 size）
                if user_id and user_id > 0:
                    user_count = r.scard(ip_users_key)
                    if user_count == IP_USERS_PER_HOUR_THRESHOLD + 1:
                        _alert(AlertType.IP_MULTI_ACCOUNT, {
                            "ip": ip,
                            "user_count": user_count,
                            "window": "1h",
                        })

            # ---- User 维度 ----
            if user_id and user_id > 0:
                user_req_key = f"{_USER_REQ_KEY}{user_id}:{minute}"
                user_req_count = r.incr(user_req_key)
                if user_req_count == 1:
                    r.expire(user_req_key, _REQ_TTL)

                # SKU 探测
                if sku:
                    sku_key = f"{_USER_SKU_KEY}{user_id}:{minute}"
                    r.sadd(sku_key, sku)
                    r.expire(sku_key, _REQ_TTL)
                    sku_count = r.scard(sku_key)
                    if sku_count == USER_SKU_SWITCH_PER_MIN + 1:
                        _alert(AlertType.USER_SKU_PROBE, {
                            "user_id": user_id,
                            "sku_count": sku_count,
                            "window": "1min",
                            "ip": ip,
                        })

                # Order 探测
                if order_no:
                    order_key = f"{_USER_ORDER_KEY}{user_id}:{minute}"
                    r.sadd(order_key, order_no)
                    r.expire(order_key, _REQ_TTL)
                    order_count = r.scard(order_key)
                    if order_count == USER_ORDER_SWITCH_PER_MIN + 1:
                        _alert(AlertType.USER_ORDER_PROBE, {
                            "user_id": user_id,
                            "order_count": order_count,
                            "order_no": order_no,
                            "window": "1min",
                            "ip": ip,
                        })

                # 阈值 3：User 高频
                if user_req_count == USER_REQ_PER_MIN_THRESHOLD + 1:
                    _alert(AlertType.USER_HIGH_FREQ, {
                        "user_id": user_id,
                        "req_count": user_req_count,
                        "window": "1min",
                        "ip": ip,
                    })

        except Exception as e:
            # Redis 挂了别误伤业务，静默 + log
            logger.warning(f"[behavior] Redis 异常（放行）: {e}")


def _alert(alert_type: str, detail: dict) -> None:
    """触发告警：metrics 计数 + WARNING 日志

    Args:
        alert_type: AlertType.* 之一
        detail: 告警上下文（用于日志，不含 PII）
    """
    try:
        from app.services.metrics import metrics
        metrics.inc_behavior_alert(alert_type)
    except Exception as e:
        logger.warning(f"[behavior] metrics 计数失败: {e}")

    # WARNING 级别：让 ops/ELK 容易捡到
    logger.warning(
        f"[behavior] ALERT type={alert_type} "
        + " ".join(f"{k}={v}" for k, v in detail.items())
    )


# 单例（无状态，只是命名空间）
behavior_monitor = BehaviorMonitor()
