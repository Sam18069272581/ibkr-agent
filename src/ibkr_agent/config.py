"""配置加载。所有规则参数都从 YAML 读入，运行时只读。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class IbkrConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 17
    account: str = ""


@dataclass(frozen=True)
class DecisionConfig:
    baseline_annual_return: float = 0.07
    min_excess_return: float = 0.02
    holding_years: float = 2.0
    claude_adjustment_cap: float = 0.03
    claude_weight_low: float = 0.0
    claude_weight_medium: float = 0.5
    claude_weight_high: float = 1.0
    rebalance_min_gain: float = 0.01


@dataclass(frozen=True)
class CostConfig:
    per_share: float = 0.005
    min_per_order: float = 1.0
    max_pct_of_value: float = 0.01
    spread_bps: float = 5
    slippage_bps: float = 5


@dataclass(frozen=True)
class RiskConfig:
    max_positions: int = 6
    max_weight: float = 0.25
    max_sector_weight: float = 0.45
    min_order_value: float = 200
    cash_buffer: float = 0.03
    max_orders_per_run: int = 6
    limit_offset_bps: float = 10
    max_quote_spread_bps: float = 50
    drawdown_halt: float = 0.20
    kill_switch_file: str = "data/KILL"


@dataclass(frozen=True)
class ResearchConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    journal_path: str = "data/journal.sqlite"


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"
    dry_run: bool = True
    ibkr: IbkrConfig = field(default_factory=IbkrConfig)
    universe: tuple[str, ...] = ()
    sectors: dict[str, str] = field(default_factory=dict)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    mode = raw.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ValueError(f"mode 必须是 paper 或 live，收到 {mode!r}")
    return Settings(
        mode=mode,
        dry_run=bool(raw.get("dry_run", True)),
        ibkr=IbkrConfig(**raw.get("ibkr", {})),
        universe=tuple(raw.get("universe", [])),
        sectors=dict(raw.get("sectors", {})),
        decision=DecisionConfig(**raw.get("decision", {})),
        costs=CostConfig(**raw.get("costs", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        research=ResearchConfig(**raw.get("research", {})),
    )
