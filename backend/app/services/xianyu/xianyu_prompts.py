"""
XianyuPrompts — 闲鱼场景化话术加载器（V16.2.4）

职责：
1. 加载 YAML 话术模板（presale/in_sale/after_sale）
2. 渲染 system prompt（含场景化指令 + few-shot examples + 买家 profile）
3. 占位符替换（{nick} / {tracking_no} 等）

调用方：
  ReplyScheduler → XianyuScenarioRouter.route(...) 拿 scenario
    → XianyuPrompts.render(scenario, system_base, buyer_profile)
    → 喂给智能客服 /chat API
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class XianyuPromptTemplate:
    """闲鱼话术模板"""

    scenario: str
    system: str
    examples: list[dict] = field(default_factory=list)


# 模板目录
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "config" / "prompts" / "xianyu"


class XianyuPrompts:
    """闲鱼场景化话术加载器

    设计取舍（V16.2.4）：
    - 复用智能客服 PROMPT_DIR 机制（manifest + 文件）
    - 简单 YAML 加载（不引入 PromptLoader 的复杂机制——V16.2 简单够用）
    - 占位符用 str.replace（不引入 jinja2）
    """

    def __init__(self, prompts_dir: Optional[Path] = None):
        self._dir = prompts_dir or _PROMPTS_DIR
        # 缓存
        self._cache: dict[str, XianyuPromptTemplate] = {}
        self._load_all()

    def load(self, scenario: str) -> XianyuPromptTemplate:
        """加载场景模板（带缓存）"""
        scenario = scenario.lower()
        if scenario in self._cache:
            return self._cache[scenario]
        # 未知场景 fallback 到售前
        if scenario not in ("presale", "in_sale", "after_sale"):
            scenario = "presale"
        return self._cache.get(scenario) or self._cache["presale"]

    def render(
        self,
        scenario: str,
        system_base: str = "",
        buyer_profile: str = "",
        nick: Optional[str] = None,
    ) -> str:
        """渲染完整 system prompt

        拼装顺序：
        1. 智能客服原 system_base（如"你是智能客服 Agent"）
        2. 闲鱼场景化 system（差异化人设 + 议价策略 + 场景话术）
        3. 买家 profile 块（昵称 + 老客/新客）
        4. few-shot examples（≤3 条）
        """
        template = self.load(scenario)
        parts: list[str] = []
        if system_base:
            parts.append(system_base)
            parts.append("")
        parts.append(template.system)
        if buyer_profile:
            parts.append("")
            parts.append(f"【当前买家】\n{buyer_profile}")
        # 占位符替换
        if nick:
            for i, p in enumerate(parts):
                parts[i] = p.replace("{nick}", nick)
        # few-shot（最多 3 条）
        if template.examples:
            parts.append("")
            parts.append("【对话示例】")
            for ex in template.examples[:6]:  # 3 轮 buyer/seller = 6 行
                role = ex.get("role", "?")
                text = ex.get("text", "")
                reasoning = ex.get("reasoning", "")
                parts.append(f"  {role}: {text}")
                if reasoning:
                    parts.append(f"  ({reasoning})")
        return "\n".join(parts)

    def _replace_placeholders(
        self, text: str, **kwargs
    ) -> str:
        """占位符替换（{nick}, {tracking_no} 等）"""
        result = text
        for key, value in kwargs.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result

    # =============================================================
    # 内部
    # =============================================================
    def _load_all(self) -> None:
        """启动时加载所有场景"""
        # V16.5 全链路日志：L8 启动期加载状态
        try:
            from app.core.runtime_log import log_event
        except Exception:
            log_event = None  # type: ignore

        for scenario in ("presale", "in_sale", "after_sale"):
            path = self._dir / f"{scenario}.yaml"
            if not path.exists():
                logger.warning(f"闲鱼话术模板缺失：{path}")
                if log_event:
                    log_event("YAML_LOAD", "WARNING", "-", "-",
                              f"template missing scenario={scenario} path={path}",
                              status="missing")
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                examples = data.get("examples", [])
                template = XianyuPromptTemplate(
                    scenario=scenario,
                    system=data.get("system", "").strip(),
                    examples=examples,
                )
                self._cache[scenario] = template
                logger.info(
                    f"闲鱼话术模板加载成功：{scenario} ({len(examples)} examples)"
                )
                if log_event:
                    log_event("YAML_LOAD", "INFO", "-", "-",
                              f"template loaded scenario={scenario}",
                              status="loaded", examples=len(examples),
                              path=str(path))
            except Exception as e:
                logger.error(f"加载闲鱼话术失败 {scenario}: {e}")
                if log_event:
                    log_event("YAML_LOAD", "ERROR", "-", "-",
                              f"template failed scenario={scenario}",
                              status="failed", error=str(e)[:200],
                              path=str(path))


__all__ = ["XianyuPrompts", "XianyuPromptTemplate"]