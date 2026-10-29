"""命令行入口。

- ibkr-agent run：一个完整周期：取数 → 估值 → Claude 研究 → 决策 → 风控 → 下单并等成交 → 对账 → 快照
- ibkr-agent report：模拟盘（或实盘）成绩
- ibkr-agent graduate：检查转实盘门槛，全部通过时退出码为 0

长线策略建议每周运行一次 run，不需要常驻。
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from . import alerts
from .broker import Broker
from .config import Settings, load_settings
from .decision import Candidate, plan
from .env import load_dotenv
from .fundamentals import EdgarClient
from .journal import FillRecord, Journal
from .reconcile import apply_fills, differences
from .report import build, render
from .research import ClaudeAnalyst
from .risk import check
from .valuation import value

log = logging.getLogger("ibkr_agent")
NEW_YORK = ZoneInfo("America/New_York")


def in_trading_window(now: datetime, window: tuple[str, str]) -> bool:
    """美东时间工作日的交易时段内。不处理节假日：节假日下的单会因为没成交在超时后撤掉。"""
    local = now.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    start, end = (time.fromisoformat(w) for w in window)
    return start <= local.time() <= end


def _cycle(s: Settings, journal: Journal, run_id: int, skip_research: bool) -> str:
    """返回运行状态：ok 或 mismatch。出错直接抛异常。"""
    status = "ok"
    edgar = EdgarClient()
    analyst = None if skip_research else ClaudeAnalyst(
        s.research.model, s.research.effort, s.decision.claude_adjustment_cap
    )

    with Broker(s.ibkr, readonly=s.dry_run) as broker:
        account = broker.account_state(journal.high_water_mark())
        halted = account.net_liquidation < account.high_water_mark * (1 - s.risk.drawdown_halt)
        bench = broker.last_prices([s.execution.benchmark]).get(s.execution.benchmark)
        journal.set_account(run_id, account.net_liquidation, account.high_water_mark, bench, halted)
        if halted:
            alerts.send("回撤熔断", f"净值 ${account.net_liquidation:,.0f}，高点 ${account.high_water_mark:,.0f}，停止开新仓")

        holdings = broker.holdings()
        previous = journal.last_snapshot()
        if previous is not None and (diff := differences(previous, holdings)):
            status = "mismatch"
            journal.log(run_id, "warning", {"kind": "positions_changed_outside_agent", "diff": diff})
            alerts.send("持仓与上次记录不一致", f"可能有人手动交易过：{diff}")

        tickers = sorted(set(s.universe) | set(holdings))
        prices = broker.last_prices(tickers)

        candidates: list[Candidate] = []
        for t in tickers:
            price = prices.get(t)
            if price is None:
                log.warning("%s 没有价格，跳过", t)
                continue
            baseline, view = None, None
            try:
                f = edgar.fundamentals(t)
                v = value(f, price * (f.diluted_shares or 0))
            except Exception as e:  # 单只股票的数据问题不应该让整个运行失败
                log.warning("%s 报表获取失败：%s", t, e)
                f, v = None, None
            if v is not None:
                baseline = v.expected_return
                if analyst is not None:
                    view = analyst.review(f, v, price)
                    if view is not None:
                        journal.log(run_id, "view", view)
            candidates.append(Candidate(t, price, s.sectors.get(t, "unknown"), baseline, view))

        orders, scored = plan(candidates, holdings, account.net_liquidation, s.decision, s.costs, s.risk)
        for sc in scored:
            journal.log(run_id, "score", sc)
        for o in orders:
            journal.log(run_id, "order", o)

        quotes = broker.quotes([o.ticker for o in orders]) if orders else {}
        approved, rejected = check(orders, quotes, account, s.sectors, s.risk, s.costs)
        for r in rejected:
            journal.log(run_id, "rejected", r)
            log.info("拒单 %s %s：%s", r.action, r.ticker, r.why)
        for a in approved:
            journal.log(run_id, "approved", a)

        print(f"净值 ${account.net_liquidation:,.0f}，美元现金 ${account.usd_cash:,.0f}")
        for sc in scored:
            print(f"  {sc.ticker:6} 打分 {sc.score:6.2%}  超额 {sc.excess:+6.2%}  净超额 {sc.net_excess:+6.2%}  {sc.reason}")
        if not approved:
            print("本次不交易。")
        for a in approved:
            print(f"{'[演练] ' if s.dry_run else ''}{a.action} {a.ticker} {a.shares} 股 @ {a.limit_price}：{a.reason}")

        fills: list[FillRecord] = []
        if approved and not s.dry_run:
            if in_trading_window(datetime.now(timezone.utc), s.execution.trading_window_et):
                mids = {t: q.mid for t, q in quotes.items()}
                fills = broker.execute(approved, mids, s.costs, s.execution.fill_timeout_seconds)
                for fr in fills:
                    journal.record_fill(run_id, fr)
                print(f"成交 {len(fills)} 笔")
            else:
                journal.log(run_id, "warning", {"kind": "outside_trading_window"})
                print("不在交易时段，本次不下单。")

        if not s.dry_run:
            expected = apply_fills({t: float(q) for t, q in holdings.items()}, fills)
            if diff := differences(expected, broker.refresh_positions()):
                status = "mismatch"
                journal.log(run_id, "warning", {"kind": "post_trade_mismatch", "diff": diff})
                alerts.send("成交后对账不一致", str(diff))
        journal.record_snapshot(run_id, broker.snapshot())
    return status


def run(config_path: str, skip_research: bool) -> int:
    s = load_settings(config_path)
    journal = Journal(s.research.journal_path)
    if s.mode == "live" and not s.dry_run:
        log.warning("实盘模式且会真实下单")
    run_id = journal.start_run(s.mode, s.dry_run)
    try:
        status = _cycle(s, journal, run_id, skip_research)
    except Exception as e:
        journal.finish_run(run_id, "error", repr(e))
        alerts.send(f"ibkr-agent 运行失败（{s.mode}）", repr(e))
        log.exception("运行失败")
        return 1
    journal.finish_run(run_id, status)
    return 0


def report(config_path: str, graduate: bool) -> int:
    s = load_settings(config_path)
    r = build(Journal(s.research.journal_path).db, s.mode, s.graduation)
    print(render(r))
    return 0 if (r.graduated or not graduate) else 2


def main() -> None:
    p = argparse.ArgumentParser(prog="ibkr-agent")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="运行一个完整周期（默认）")
    run_p.add_argument("--skip-research", action="store_true", help="不调用 Claude，只用报表基线")
    sub.add_parser("report", help="显示成绩")
    sub.add_parser("graduate", help="检查转实盘门槛，通过时退出码为 0")
    args = p.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if args.command in ("report", "graduate"):
        raise SystemExit(report(args.config, graduate=args.command == "graduate"))
    raise SystemExit(run(args.config, getattr(args, "skip_research", False)))
