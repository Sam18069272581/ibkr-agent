"""币圈配置：读 settings.yaml 里的 `crypto:` 段。运行时只读，模型不能改。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CryptoCosts:
    maker_fee: float = 0.0008      # OKX 普通用户现货 maker，约值，以官网为准
    taker_fee: float = 0.0010
    spread_bps: float = 2          # 半个点差估计
    slippage_bps: float = 3


@dataclass(frozen=True)
class CryptoRisk:
    max_weight: float = 0.70           # 单个币占币圈净值上限
    drawdown_halt: float = 0.35        # 从高点回撤超过这个比例就停止买入
    cash_buffer: float = 0.02          # 始终留下的现金比例
    min_order_value: float = 10.0      # 单笔最少金额（计价货币）
    max_orders_per_run: int = 4
    max_buy_fraction_per_run: float = 0.25  # 每次运行最多买净值的 25%：从零建仓或入金后分几周买进（用户 2026-11-05 选的）
    max_spread_bps: float = 30         # 盘口价差过大视为异常
    kill_switch_file: str = "data/KILL"


@dataclass(frozen=True)
class CryptoExecution:
    price_band: float = 0.005          # 下单时的价格必须在计划价 ±0.5% 内，否则不追
    plan_ttl_minutes: int = 15         # 计划多久后作废
    max_quote_age_seconds: float = 2.0 # 报价超过这个秒数就重取
    fill_timeout_seconds: int = 300    # 挂单最多等多久，没成交就撤
    shock_move: float = 0.03           # 最近一段时间波动超过 3% 就不买
    shock_window_minutes: int = 15


@dataclass(frozen=True)
class CryptoTax:
    """德国私人出售交易（§23 EStG）的简化模型。不是税务建议。"""

    tax_free_after_days: int = 365     # 持有满一年卖出免税
    annual_threshold: float = 1000.0   # 年度短期收益 Freigrenze：不超过就不交，超过则全额计税
    est_tax_rate: float = 0.30         # 估计的个人边际税率，用来比较“卖不卖”


@dataclass(frozen=True)
class CryptoAI:
    """慢思考（Opus 每周定边界）和快哨兵（Haiku 开仓前答封闭问题）。代码定阈值，模型只给概率和边界。"""

    envelope_enabled: bool = False
    envelope_model: str = "claude-opus-5-5"
    envelope_effort: str = "high"
    envelope_valid_days: int = 8           # 每周跑一次，多给一天余量；过期就当没有边界
    envelope_history_days: int = 100       # 给 Opus 的日线天数（90 天高点要用到 90 天以上）
    envelope_max_tokens: int = 16000       # Opus 一次评审最多输出多少 token（含思考），也是预算估算的上限
    envelope_timeout_seconds: float = 600  # 每周评审单独跑，可以多等
    envelope_max_retries: int = 2
    max_tilt: float = 0.05                 # Opus 最多把买入目标上下调 5 个百分点；不能超过再平衡带的一半
    max_band: float = 0.25                 # 买入价区间最多离现价 ±25%
    gate_enabled: bool = False
    gate_model: str = "claude-haiku-5-5"
    gate_effort: str = "low"
    gate_max_tokens: int = 4000
    gate_timeout_seconds: float = 30       # 快哨兵在下单路径上，不能久等：超时就当没回答，不买
    gate_max_retries: int = 1
    gate_max_p_invalidated: float = 0.30   # “失效条件已触发”的概率高于这个就不买
    gate_max_p_abnormal: float = 0.30      # “行情异常”的概率高于这个就不买
    monthly_budget_usd: float = 8.0        # 本月 API 花费到上限后不再调用（快哨兵不答 = 不买）
    # 美元 / 百万 token：[输入, 输出, 写缓存, 读缓存]，按 2026-11 的 Anthropic 价目表。价格变了在 settings.yaml 里改。
    # 表里没有的模型按表里每一列的最高价估算并告警，宁可高估花费。
    prices: dict[str, tuple[float, float, float, float]] = field(default_factory=lambda: {
        "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
        "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
        "claude-opus-4-8": (5.0, 25.0, 6.25, 0.50),
        "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
        "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    })


@dataclass(frozen=True)
class CryptoSettings:
    enabled: bool = False
    exchange: str = "okx"
    hostname: str = ""                 # 留空用 ccxt 默认域名；欧洲站账户若连不上，填交易所给的 API 域名
    sandbox: bool = True               # True = OKX 模拟盘（Demo Trading）
    dry_run: bool = True               # True = 只打印计划，不下单
    paper_local: bool = False          # True = 不连交易所账户，用本地模拟账户 + 公开行情
    paper_start_cash: float = 300.0
    quote: str = "EUR"
    targets: dict[str, float] = field(default_factory=lambda: {"BTC": 0.6, "ETH": 0.4})
    rebalance_band: float = 0.10       # 实际比例偏离目标超过 10 个百分点才调
    cash_deploy_band: float = 0.05     # 现金比缓冲多出 5 个百分点以上时，低于目标的币都补到目标（不受上面的带限制）
    costs: CryptoCosts = field(default_factory=CryptoCosts)
    risk: CryptoRisk = field(default_factory=CryptoRisk)
    execution: CryptoExecution = field(default_factory=CryptoExecution)
    tax: CryptoTax = field(default_factory=CryptoTax)
    ai: CryptoAI = field(default_factory=CryptoAI)
    journal_path: str = "data/journal.sqlite"

    def symbol(self, asset: str) -> str:
        return f"{asset}/{self.quote}"


def parse(raw: dict, journal_path: str = "data/journal.sqlite") -> CryptoSettings:
    targets = {str(k).upper(): float(v) for k, v in (raw.get("targets") or {"BTC": 0.6, "ETH": 0.4}).items()}
    total = sum(targets.values())
    if total > 1.0 + 1e-9:
        raise ValueError(f"crypto.targets 加起来是 {total:.2f}，不能超过 1（剩下的是现金）")
    if any(v < 0 for v in targets.values()):
        raise ValueError("crypto.targets 不能有负数")
    s = CryptoSettings(
        enabled=bool(raw.get("enabled", False)),
        exchange=str(raw.get("exchange", "okx")),
        hostname=str(raw.get("hostname") or ""),
        sandbox=bool(raw.get("sandbox", True)),
        dry_run=bool(raw.get("dry_run", True)),
        paper_local=bool(raw.get("paper_local", False)),
        paper_start_cash=float(raw.get("paper_start_cash", 300.0)),
        quote=str(raw.get("quote", "EUR")).upper(),
        targets=targets,
        rebalance_band=float(raw.get("rebalance_band", 0.10)),
        cash_deploy_band=float(raw.get("cash_deploy_band", 0.05)),
        costs=CryptoCosts(**raw.get("costs", {})),
        risk=CryptoRisk(**raw.get("risk", {})),
        execution=CryptoExecution(**raw.get("execution", {})),
        tax=CryptoTax(**raw.get("tax", {})),
        ai=_ai(raw.get("ai") or {}),
        journal_path=journal_path,
    )
    if over := [a for a, t in targets.items() if t > s.risk.max_weight]:
        raise ValueError(f"crypto.targets 里 {', '.join(over)} 的目标超过单币上限 risk.max_weight {s.risk.max_weight:.0%}")
    if not 0 < s.execution.price_band < 0.05:
        raise ValueError("crypto.execution.price_band 应在 0 到 5% 之间")
    if not 0 < s.risk.max_buy_fraction_per_run <= 1:
        raise ValueError("crypto.risk.max_buy_fraction_per_run 应在 0 到 1 之间")
    if s.cash_deploy_band < 0:
        raise ValueError("crypto.cash_deploy_band 不能是负数")
    if not 0 <= s.ai.max_tilt <= s.rebalance_band / 2:
        # 倾斜只改买入目标，卖出线还是“配置目标 + 再平衡带”。倾斜用掉超过一半的带，买到倾斜后的目标
        # 离卖出线就太近，价格一涨就卖回去，来回付手续费。
        raise ValueError(f"crypto.ai.max_tilt 应在 0 到再平衡带的一半（{s.rebalance_band / 2:.0%}）之间，"
                         f"现在是 {s.ai.max_tilt:.0%}")
    for name in ("envelope_timeout_seconds", "gate_timeout_seconds", "envelope_max_tokens", "gate_max_tokens"):
        if getattr(s.ai, name) <= 0:
            raise ValueError(f"crypto.ai.{name} 应大于 0")
    for name in ("envelope_max_retries", "gate_max_retries"):
        if getattr(s.ai, name) < 0:
            raise ValueError(f"crypto.ai.{name} 不能是负数")
    return s


def _ai(raw: dict) -> CryptoAI:
    """settings.yaml 里的 prices 只需要写要改的模型，其余用默认价目。"""
    raw = dict(raw)
    prices = dict(CryptoAI().prices)
    for model, p in (raw.pop("prices", None) or {}).items():
        if len(p) != 4 or any(float(x) < 0 for x in p):
            raise ValueError(f"crypto.ai.prices.{model} 应是 4 个非负数：[输入, 输出, 写缓存, 读缓存]")
        prices[str(model)] = tuple(float(x) for x in p)
    return CryptoAI(**raw, prices=prices)


def load(path: str | Path) -> CryptoSettings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    journal = (raw.get("research") or {}).get("journal_path", "data/journal.sqlite")
    return parse(raw.get("crypto") or {}, journal)
