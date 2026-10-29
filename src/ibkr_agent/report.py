"""模拟盘成绩与转实盘门槛。只读 journal，不连 IBKR。

只统计 dry_run = false 的运行，演练不算成绩。
注意：净值按美元计，期间如果入金、出金或欧元兑美元汇率变化，收益会失真。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import GraduationConfig


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Report:
    mode: str
    runs: int
    errors: int
    mismatches: int
    consecutive_ok: int
    first_ts: str | None
    last_ts: str | None
    nav_start: float | None
    nav_end: float | None
    total_return: float | None
    benchmark_return: float | None
    max_drawdown: float
    drawdown_halted: bool
    fills: int
    actual_cost: float
    modelled_cost: float
    realized_wins: int
    realized_trades: int
    open_winners: int
    open_positions: int
    gates: tuple[Gate, ...]

    @property
    def cost_ratio(self) -> float | None:
        return self.actual_cost / self.modelled_cost if self.modelled_cost > 0 else None

    @property
    def excess_return(self) -> float | None:
        if self.total_return is None or self.benchmark_return is None:
            return None
        return self.total_return - self.benchmark_return

    @property
    def graduated(self) -> bool:
        return all(g.passed for g in self.gates)


def _max_drawdown(navs: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


def build(db: sqlite3.Connection, mode: str, g: GraduationConfig) -> Report:
    runs = db.execute(
        "SELECT id, ts, status, net_liquidation_usd, benchmark_price, drawdown_halted "
        "FROM runs WHERE mode=? AND dry_run=0 AND status != 'running' ORDER BY id",
        (mode,),
    ).fetchall()
    run_ids = [r[0] for r in runs]
    ok = [r for r in runs if r[2] == "ok"]

    consecutive = 0
    for r in reversed(runs):
        if r[2] != "ok":
            break
        consecutive += 1

    navs = [r[3] for r in ok if r[3]]
    bench = [r[4] for r in ok if r[4]]
    nav_start, nav_end = (navs[0], navs[-1]) if navs else (None, None)
    total_return = nav_end / nav_start - 1 if navs and len(navs) > 1 else None
    benchmark_return = bench[-1] / bench[0] - 1 if len(bench) > 1 else None
    halted = any(r[5] for r in runs)

    marks = ",".join("?" * len(run_ids)) or "NULL"
    fills = db.execute(
        f"SELECT side, shares, price, commission, realized_pnl, ref_mid, modelled_cost "
        f"FROM fills WHERE run_id IN ({marks})",
        run_ids,
    ).fetchall()
    actual = sum(
        c + (p - mid) * n * (1 if side == "BUY" else -1) for side, n, p, c, _, mid, _ in fills
    )
    modelled = sum(f[6] for f in fills)
    closed = [f[4] for f in fills if f[4] is not None]

    snap = []
    if ok:
        snap = db.execute(
            "SELECT shares, market_value, avg_cost FROM snapshots WHERE run_id=?", (ok[-1][0],)
        ).fetchall()
    open_winners = sum(1 for n, mv, cost in snap if mv > n * cost)

    errors = sum(1 for r in runs if r[2] == "error")
    mismatches = sum(1 for r in runs if r[2] == "mismatch")
    cost_ratio = actual / modelled if modelled > 0 else None
    excess = (
        total_return - benchmark_return
        if total_return is not None and benchmark_return is not None
        else None
    )

    gates = (
        Gate(
            "连续成功运行",
            consecutive >= g.min_consecutive_ok_runs,
            f"{consecutive} / {g.min_consecutive_ok_runs} 次",
        ),
        Gate("对账一致", mismatches == 0, f"不一致 {mismatches} 次"),
        Gate(
            "成本符合预期",
            cost_ratio is not None and cost_ratio <= g.max_cost_ratio,
            "还没有成交" if cost_ratio is None else f"实际 / 模型 = {cost_ratio:.2f}（上限 {g.max_cost_ratio}）",
        ),
        Gate(
            "未触发回撤熔断",
            g.allow_drawdown_halt or not halted,
            "触发过" if halted else "没有触发",
        ),
        Gate(
            "不明显落后基准",
            excess is not None and excess >= -g.max_underperformance,
            "数据不足" if excess is None else f"超额 {excess:+.2%}（下限 {-g.max_underperformance:+.0%}）",
        ),
    )

    return Report(
        mode=mode,
        runs=len(runs),
        errors=errors,
        mismatches=mismatches,
        consecutive_ok=consecutive,
        first_ts=runs[0][1] if runs else None,
        last_ts=runs[-1][1] if runs else None,
        nav_start=nav_start,
        nav_end=nav_end,
        total_return=total_return,
        benchmark_return=benchmark_return,
        max_drawdown=_max_drawdown(navs),
        drawdown_halted=halted,
        fills=len(fills),
        actual_cost=actual,
        modelled_cost=modelled,
        realized_wins=sum(1 for p in closed if p > 0),
        realized_trades=len(closed),
        open_winners=open_winners,
        open_positions=len(snap),
        gates=gates,
    )


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2%}"


def render(r: Report) -> str:
    lines = [
        f"模式：{r.mode}　运行 {r.runs} 次（出错 {r.errors}，对账不一致 {r.mismatches}）",
        f"区间：{r.first_ts or '—'} → {r.last_ts or '—'}",
        f"净值：{r.nav_start or 0:,.0f} → {r.nav_end or 0:,.0f} 美元　收益 {_pct(r.total_return)}"
        f"　基准 {_pct(r.benchmark_return)}　超额 {_pct(r.excess_return)}",
        f"最大回撤：{r.max_drawdown:.2%}",
        f"成交 {r.fills} 笔，实际成本 ${r.actual_cost:.2f}，模型估计 ${r.modelled_cost:.2f}",
        f"胜率（仅供参考）：已平仓 {r.realized_wins}/{r.realized_trades}，持仓浮盈 {r.open_winners}/{r.open_positions}",
        "",
        "转实盘门槛：",
    ]
    lines += [f"  [{'通过' if gt.passed else '未通过'}] {gt.name}：{gt.detail}" for gt in r.gates]
    lines.append("")
    lines.append("结论：可以考虑转实盘（小资金起步）" if r.graduated else "结论：继续跑模拟盘")
    return "\n".join(lines)
