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
    costs: CryptoCosts = field(default_factory=CryptoCosts)
    risk: CryptoRisk = field(default_factory=CryptoRisk)
    execution: CryptoExecution = field(default_factory=CryptoExecution)
    tax: CryptoTax = field(default_factory=CryptoTax)
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
        costs=CryptoCosts(**raw.get("costs", {})),
        risk=CryptoRisk(**raw.get("risk", {})),
        execution=CryptoExecution(**raw.get("execution", {})),
        tax=CryptoTax(**raw.get("tax", {})),
        journal_path=journal_path,
    )
    if not 0 < s.execution.price_band < 0.05:
        raise ValueError("crypto.execution.price_band 应在 0 到 5% 之间")
    return s


def load(path: str | Path) -> CryptoSettings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    journal = (raw.get("research") or {}).get("journal_path", "data/journal.sqlite")
    return parse(raw.get("crypto") or {}, journal)
