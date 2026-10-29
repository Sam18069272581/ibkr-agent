"""Claude 研究员：读报表数据，给出结构化的定性判断。

Claude 不决定买卖、不决定仓位、不生成财务数字。它只输出一个有上限的
预期收益调整值和信心等级，由 decision.py 按规则使用。
任何失败（报错、拒答、格式不对）都视为“没有提案”，默认不交易。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

from .fundamentals import Fundamentals
from .valuation import Valuation

log = logging.getLogger(__name__)

CONFIDENCE_LEVELS = ("low", "medium", "high")

SYSTEM_PROMPT = """你是一名严谨的长线基本面分析师，服务于一个规则驱动的美股组合系统。

系统已经用代码从 SEC 报表算出了基线预期年化收益（自由现金流收益率 + 增长 − 杠杆惩罚）。
你的工作是判断这个基线是否漏掉了重要的定性因素，例如竞争格局、护城河变化、
一次性项目、会计质量、管理层资本配置、监管或诉讼风险。

要求：
- 先写多方论点，再写同样认真的空方论点，最后才给结论。
- adjustment 是对基线年化收益的调整（小数，0.01 = 1 个百分点），范围 -0.03 到 0.03。
  没有充分理由时给 0。
- 只依据提供的数据和你确信的公开事实；不确定就降低 confidence。
- 不要给出买卖建议或仓位建议，那由系统的规则决定。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "bull_case": {"type": "string"},
        "bear_case": {"type": "string"},
        "key_risks": {"type": "array", "items": {"type": "string"}},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
        "adjustment": {"type": "number"},
        "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
    },
    "required": [
        "bull_case",
        "bear_case",
        "key_risks",
        "invalidation_conditions",
        "adjustment",
        "confidence",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ResearchView:
    ticker: str
    adjustment: float
    confidence: str
    bull_case: str
    bear_case: str
    key_risks: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    raw: str


def parse_view(ticker: str, text: str, cap: float) -> ResearchView | None:
    try:
        data = json.loads(text)
        confidence = data["confidence"]
        if confidence not in CONFIDENCE_LEVELS:
            return None
        adjustment = max(-cap, min(cap, float(data["adjustment"])))
        return ResearchView(
            ticker=ticker,
            adjustment=adjustment,
            confidence=confidence,
            bull_case=str(data["bull_case"]),
            bear_case=str(data["bear_case"]),
            key_risks=tuple(map(str, data["key_risks"])),
            invalidation_conditions=tuple(map(str, data["invalidation_conditions"])),
            raw=text,
        )
    except (ValueError, KeyError, TypeError):
        return None


class ClaudeAnalyst:
    def __init__(self, model: str, effort: str, adjustment_cap: float, client: anthropic.Anthropic | None = None):
        self._client = client or anthropic.Anthropic()
        self._model = model
        self._effort = effort
        self._cap = adjustment_cap

    def review(self, f: Fundamentals, v: Valuation, price: float) -> ResearchView | None:
        payload = {
            "fundamentals": f.as_dict(),
            "price_usd": price,
            "baseline": {
                "fcf_yield": round(v.fcf_yield, 4),
                "growth_used": round(v.growth, 4),
                "leverage_penalty": v.leverage_penalty,
                "expected_annual_return": round(v.expected_return, 4),
            },
        }
        try:
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[
                    {
                        "role": "user",
                        "content": f"请评估 {f.ticker}。数据如下（JSON）：\n{json.dumps(payload, ensure_ascii=False)}",
                    }
                ],
            )
        except anthropic.APIError as e:
            log.warning("Claude 调用失败，%s 视为无提案：%s", f.ticker, e)
            return None

        if response.stop_reason != "end_turn":
            log.warning("Claude 未正常结束（%s），%s 视为无提案", response.stop_reason, f.ticker)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        view = parse_view(f.ticker, text, self._cap)
        if view is None:
            log.warning("Claude 输出无法解析，%s 视为无提案", f.ticker)
        return view
