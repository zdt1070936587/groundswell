"""LLM 调用的唯一入口：LiteLLM 多厂商 + JSON 结构化输出 + 预算熔断 + 用量记账。

未配置模型（GS_LLM_CHEAP_MODEL / GS_LLM_STRONG_MODEL）时 `available()` 为 False，
上层（命名、漏斗）自动走启发式路径，系统仍可完整运行。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from groundswell.config import Settings, get_settings
from groundswell.db.models import LlmUsage
from groundswell.db.session import session_scope

Tier = Literal["cheap", "strong"]
T = TypeVar("T", bound=BaseModel)

# LiteLLM 价格表缺失时的保守估计（USD / 百万 token：输入, 输出）
FALLBACK_PRICE = {"cheap": (0.5, 2.0), "strong": (3.0, 15.0)}


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


def model_for(tier: Tier, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    if tier == "strong":
        return settings.llm_strong_model or settings.llm_cheap_model
    return settings.llm_cheap_model or settings.llm_strong_model


def available(tier: Tier = "cheap", settings: Settings | None = None) -> bool:
    if not model_for(tier, settings):
        return False
    try:
        import litellm  # noqa: F401
    except ImportError:
        return False
    return True


def spent_today_cny(settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    tz = ZoneInfo(settings.report_timezone)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        usd = s.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)).where(LlmUsage.ts >= start)
        )
    return float(usd or 0.0) * settings.usd_to_cny


def budget_left_cny(settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    return settings.llm_daily_budget_cny - spent_today_cny(settings)


def _record(
    model: str, stage: str, prompt_tokens: int, completion_tokens: int, cost: float, ok: bool
) -> None:
    with session_scope() as s:
        s.add(
            LlmUsage(
                model=model[:64],
                stage=stage[:32],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
                ok=ok,
            )
        )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(content: str) -> Any:
    content = _FENCE.sub("", content.strip())
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # 模型偶尔在 JSON 前后加解释文字
        m = re.search(r"(\{.*\}|\[.*\])", content, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(1))


def complete(
    messages: list[dict[str, str]],
    *,
    tier: Tier = "cheap",
    stage: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    json_mode: bool = True,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    model = model_for(tier, settings)
    if not model:
        raise LLMError("未配置 LLM 模型")
    if budget_left_cny(settings) <= 0:
        raise BudgetExceeded(f"今日 LLM 预算 ¥{settings.llm_daily_budget_cny} 已用完")
    import litellm

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": 180,
        "num_retries": 2,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = litellm.completion(**kwargs)
    except Exception as exc:
        _record(model, stage, 0, 0, 0.0, ok=False)
        raise LLMError(f"{model} 调用失败: {exc}") from exc
    usage = getattr(resp, "usage", None)
    pt = int(getattr(usage, "prompt_tokens", 0) or 0)
    ct = int(getattr(usage, "completion_tokens", 0) or 0)
    try:
        cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        cost = 0.0
    if cost <= 0 and (pt or ct):
        pin, pout = FALLBACK_PRICE[tier]
        cost = (pt * pin + ct * pout) / 1e6
    _record(model, stage, pt, ct, cost, ok=True)
    return resp.choices[0].message.content or ""


def complete_json(
    prompt: str,
    *,
    system: str,
    schema: type[T] | None = None,
    tier: Tier = "cheap",
    stage: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    settings: Settings | None = None,
) -> Any:
    """返回解析后的 JSON（或 schema 实例）。校验失败时把错误回灌给模型重试一次。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    last_error: Exception | None = None
    for _ in range(2):
        content = complete(
            messages,
            tier=tier,
            stage=stage,
            max_tokens=max_tokens,
            temperature=temperature,
            settings=settings,
        )
        try:
            data = parse_json(content)
            return schema.model_validate(data) if schema else data
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning(f"LLM 输出不符合格式（{stage}）：{str(exc)[:300]}")
            messages += [
                {"role": "assistant", "content": content[:4000]},
                {
                    "role": "user",
                    "content": f"输出不符合要求：{str(exc)[:800]}\n请只输出修正后的 JSON。",
                },
            ]
    raise LLMError(f"LLM 结构化输出失败: {last_error}")
