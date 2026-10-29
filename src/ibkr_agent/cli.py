"""每次运行完成一个完整周期：取数 → 估值 → Claude 研究 → 决策 → 风控 → 下单（或只打印）。

长线策略建议每周或每月运行一次，不需要常驻。
"""

from __future__ import annotations

import argparse
import logging

from .broker import Broker
from .config import load_settings
from .decision import Candidate, plan
from .fundamentals import EdgarClient
from .journal import Journal
from .research import ClaudeAnalyst
from .risk import check
from .valuation import value

log = logging.getLogger("ibkr_agent")


def run(config_path: str, skip_research: bool) -> int:
    s = load_settings(config_path)
    journal = Journal(s.research.journal_path)
    edgar = EdgarClient()
    analyst = None if skip_research else ClaudeAnalyst(
        s.research.model, s.research.effort, s.decision.claude_adjustment_cap
    )
    if s.mode == "live" and not s.dry_run:
        log.warning("实盘模式且会真实下单")

    with Broker(s.ibkr, readonly=s.dry_run) as broker:
        account = broker.account_state(journal.high_water_mark())
        run_id = journal.start_run(s.mode, s.dry_run, account.net_liquidation, account.high_water_mark)
        holdings = broker.holdings()
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
            except Exception as e:  # 数据源问题不应该让整个运行失败
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

        print(f"净值 ${account.net_liquidation:,.0f}，美元现金 ${account.usd_cash:,.0f}")
        for sc in scored:
            print(f"  {sc.ticker:6} 打分 {sc.score:6.2%}  超额 {sc.excess:+6.2%}  净超额 {sc.net_excess:+6.2%}  {sc.reason}")
        if not approved:
            print("本次不交易。")
        for a in approved:
            journal.log(run_id, "approved", a)
            print(f"{'[演练] ' if s.dry_run else ''}{a.action} {a.ticker} {a.shares} 股 @ {a.limit_price}：{a.reason}")
            if not s.dry_run:
                broker.place(a)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="ibkr-agent")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--skip-research", action="store_true", help="不调用 Claude，只用报表基线")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(run(args.config, args.skip_research))
