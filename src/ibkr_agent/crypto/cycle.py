"""币圈一次运行：取数 → 对齐批次 → 读 Opus 边界、倾斜目标 → 再平衡计划 → 税务过滤 → 风控
→ 买入才检查：波动、边界、快哨兵 → 下单前重取价 → 挂单等成交 → 记账。

所有事件写进同一个 journal.sqlite，运行模式记为 crypto-<账户>，和美股的成绩分开统计。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Callable

from .. import alerts
from ..journal import FillRecord, Journal, PositionSnapshot
from .config import CryptoSettings
from .envelope import EnvelopeStore, buy_block, tilted_targets
from .exchange import Exchange
from .gate import Gate, dossier, record_prices
from .guard import Rejected, Send, preflight, risk_check, shocked, tax_filter
from .llm import Budget
from .lots import LotBook
from .plan import OrderPlan, rebalance, weights

log = logging.getLogger(__name__)


def account_name(s: CryptoSettings) -> str:
    if s.paper_local:
        return "local-paper"
    return f"{s.exchange}-{'demo' if s.sandbox else 'live'}"


def _avg_cost(lots: LotBook, asset: str) -> float:
    """批次加权平均成本（含手续费）。报告用它判断持仓是否浮盈；没有批次时为 0。"""
    ls = lots.lots(asset)
    qty = sum(l.qty for l in ls)
    return sum(l.qty * l.unit_cost for l in ls) / qty if qty > 0 else 0.0


def _wait_fill(ex: Exchange, symbol: str, oid: str, timeout: int, sleep: Callable[[float], None],
               clock: Callable[[], float]):
    deadline = clock() + timeout
    st = ex.order(symbol, oid)
    while st.status == "open" and clock() < deadline:
        sleep(5)
        st = ex.order(symbol, oid)
    if st.status == "open":
        ex.cancel(symbol, oid)
        st = ex.order(symbol, oid)
    return st


def _buy_check(p: OrderPlan, s: CryptoSettings, ex: Exchange, books: dict, envelopes: dict, gate: Gate | None,
               now: datetime) -> str | None:
    """返回不买的原因；None 表示可以买。按便宜到贵的顺序检查，快哨兵放在最后。"""
    try:
        window = ex.recent_range(p.symbol, s.execution.shock_window_minutes)
    except Exception as e:
        return f"拿不到近期行情，不买：{e}"
    o, lo, hi = window
    if shocked(o, lo, hi, s.execution.shock_move):
        alerts.send("币圈波动过大", f"{p.symbol} 暂停买入，建议运行 crypto envelope 让 Opus 重新评估")
        return f"{s.execution.shock_window_minutes} 分钟内波动 {(hi - lo) / o:.1%}，超过 {s.execution.shock_move:.0%}，暂停买入"
    env = envelopes.get(p.ticker)
    closes = None
    if env is not None or gate is not None:
        try:
            closes = ex.daily_closes(p.symbol, 8)
        except Exception as e:
            if env is not None:
                return f"拿不到日线，没法检查边界，不买：{e}"
    price = books[p.ticker].mid
    if env is not None:
        week_ago = closes[-8] if closes and len(closes) >= 8 else None
        if why := buy_block(env, price, week_ago):
            if why.startswith("失效条件"):
                alerts.send("币圈边界失效", f"{p.ticker}：{why}。停止买入，请运行 crypto envelope 重新评估")
            return why
    if gate is not None:
        result = gate.ask(dossier(p.ticker, price, books[p.ticker].spread_bps, window,
                                  s.execution.shock_window_minutes, closes, env, p.notional, s.quote), now)
        if result.verdict != "pass":
            return result.why
    return None


def run_cycle(s: CryptoSettings, ex: Exchange, journal: Journal, out: Callable[[str], None] = print,
              clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
              ai_client=None) -> str:
    """ai_client：Anthropic 客户端，只有 crypto.ai.gate_enabled 时用到。没有客户端时快哨兵视为不回答，不买。"""
    account = account_name(s)
    mode = f"crypto-{account}"
    run_id = journal.start_run(mode, s.dry_run)
    try:
        status = _cycle(s, ex, journal, run_id, account, mode, out, clock, sleep, ai_client)
    except Exception as e:
        journal.finish_run(run_id, "error", repr(e))
        alerts.send(f"币圈运行失败（{account}）", repr(e))
        raise
    journal.finish_run(run_id, status)
    return status


def _cycle(s, ex, journal, run_id, account, mode, out, clock, sleep, ai_client) -> str:
    status = "ok"
    now_dt = datetime.fromtimestamp(clock(), timezone.utc)
    lots = LotBook(journal.db, account, s.tax)

    bal = ex.balances()
    cash = bal.get(s.quote, 0.0)
    assets = sorted(set(s.targets) | {a for a in bal if a != s.quote and bal[a] > 0})
    books = {}
    for a in assets:
        try:
            books[a] = ex.book(s.symbol(a))
        except Exception as e:
            log.warning("%s 拿不到盘口：%s", a, e)
    prices = {a: b.mid for a, b in books.items()}
    holdings = {a: bal.get(a, 0.0) for a in assets if a in prices}
    unpriced = [a for a in assets if a not in prices and bal.get(a, 0.0) > 0]
    if unpriced:
        journal.log(run_id, "warning", {"kind": "unpriced_holdings", "assets": unpriced})

    for a, q in holdings.items():
        if note := lots.sync(a, q, prices[a], now_dt):
            journal.log(run_id, "warning", {"kind": "lots_synced", "ticker": a, "note": note})
            if "系统外" in note:
                status = "mismatch"
                alerts.send("币圈持仓与记录不一致", note)

    record_prices(journal.db, account, prices, now_dt)

    envelopes = {}
    if s.ai.envelope_enabled:
        store = EnvelopeStore(journal.db, account)
        envelopes = {a: e for a in s.targets if (e := store.current(a, now_dt))}
        missing = [a for a in s.targets if a not in envelopes]
        if missing:
            journal.log(run_id, "note", {"text": f"{', '.join(missing)} 没有有效的 Opus 边界，按配置目标"})
        if envelopes:
            s = replace(s, targets=tilted_targets(s.targets, envelopes, s.risk.max_weight))

    nav, ws = weights(prices, holdings, cash, s.targets)
    hwm = max(journal.high_water_mark(mode), nav)
    halted = hwm > 0 and nav < hwm * (1 - s.risk.drawdown_halt)
    journal.set_account(run_id, nav, hwm, prices.get("BTC"), halted)
    if halted:
        alerts.send("币圈回撤熔断", f"净值 {nav:.2f} {s.quote}，高点 {hwm:.2f}，停止买入")

    out(f"[{account}] 净值 {nav:.2f} {s.quote}，现金 {cash:.2f}" + ("（演练，不下单）" if s.dry_run else ""))
    for w in ws:
        out(f"  {w.ticker:5} {w.value:9.2f} {s.quote}  占 {w.weight:5.1%}  目标 {w.target:5.1%}")
    for a, e in envelopes.items():
        out(f"  ◇ {a} 边界：{e.stance}，倾斜 {e.effective_tilt():+.0%}（信心 {e.confidence}），"
            f"买入区间 {e.buy_low:.2f}–{e.buy_high:.2f}，失效条件 {len(e.invalidation)} 条，到 {e.valid_until[:10]}")

    plans, notes = rebalance(prices, holdings, cash, s, clock())
    for n in notes:
        journal.log(run_id, "note", {"text": n})
        out("  · " + n)

    plans, held_back = tax_filter(plans, lots, {w.ticker: w.weight for w in ws}, s, now_dt)
    approved, rejected = risk_check(plans, nav, hwm, cash, {w.ticker: w.value for w in ws}, s)
    rejected = held_back + rejected

    gate = Gate(ai_client, Budget(journal.db, s.ai.monthly_budget_usd), journal.db, account, s) if s.ai.gate_enabled else None
    final: list[OrderPlan] = []
    for p in approved:
        if p.side == "BUY":  # 卖出不经过下面任何一道：波动、边界、快哨兵都只拦开仓
            why = _buy_check(p, s, ex, books, envelopes, gate, now_dt)
            if why:
                rejected.append(Rejected(p.ticker, p.side, why))
                continue
        final.append(p)

    for r in rejected:
        journal.log(run_id, "rejected", r)
        out(f"  ✗ {r.side} {r.ticker}：{r.why}")
    for p in final:
        journal.log(run_id, "order", p)

    if not final:
        out("本次不交易。")

    for p in final:
        book = ex.book(p.symbol)  # 下单前一秒重新取价
        decision = preflight(p, book, clock(), s)
        if not isinstance(decision, Send):
            journal.log(run_id, "skipped", {"ticker": p.ticker, "side": p.side, "why": decision.why})
            out(f"  ↷ {p.side} {p.ticker} 不下：{decision.why}")
            continue
        amount = ex.amount_to_precision(p.symbol, decision.amount)
        if amount <= 0 or amount < ex.min_amount(p.symbol):
            journal.log(run_id, "skipped", {"ticker": p.ticker, "side": p.side, "why": "数量低于交易所最小下单量"})
            out(f"  ↷ {p.side} {p.ticker} 数量 {amount} 低于交易所最小下单量")
            continue
        line = f"{p.side} {p.ticker} {amount} @ {decision.price:.2f}（计划价 {p.ref_price:.2f} ±{p.band:.1%}）：{p.reason}"
        if s.dry_run:
            journal.log(run_id, "approved", {**asdict(p), "price": decision.price, "amount": amount})
            out("  [演练] " + line)
            continue
        oid = ex.place_post_only(p.symbol, p.side, amount, decision.price)
        out("  → " + line)
        st = _wait_fill(ex, p.symbol, oid, s.execution.fill_timeout_seconds, sleep, clock)
        if st.filled <= 0:
            journal.log(run_id, "skipped", {"ticker": p.ticker, "side": p.side, "why": "挂单超时未成交，已撤单"})
            out(f"  ↷ {p.ticker} 挂单超时未成交，已撤单")
            continue
        avg = float(st.average or decision.price)
        ts = datetime.fromtimestamp(clock(), timezone.utc)
        if p.side == "BUY":
            lots.add(p.ticker, ts, st.filled, (st.filled * avg + st.fee) / st.filled)
            pnl = None
        else:
            pnl = lots.dispose(p.ticker, st.filled, avg - st.fee / st.filled, ts)
        c = s.costs
        modelled = st.filled * avg * (c.maker_fee + (c.spread_bps + c.slippage_bps) / 10_000)
        journal.record_fill(run_id, FillRecord(oid, ts.isoformat(), p.ticker, p.side, st.filled, avg, st.fee,
                                               pnl, p.ref_price, modelled))
        out(f"  ✓ 成交 {st.filled} @ {avg:.2f}，手续费 {st.fee:.4f}")

    if not s.dry_run:
        after = ex.balances()
        for a in holdings:
            held = lots.held(a)
            if abs(after.get(a, 0.0) - held) > max(1e-8, 1e-6 * held):
                status = "mismatch"
                journal.log(run_id, "warning", {"kind": "post_trade_mismatch", "ticker": a,
                                                "exchange": after.get(a, 0.0), "lots": held})
                alerts.send("币圈成交后对账不一致", f"{a}：交易所 {after.get(a, 0.0)}，记录 {held}")
        journal.record_snapshot(run_id, [PositionSnapshot(a, after.get(a, 0.0), after.get(a, 0.0) * prices[a],
                                                          _avg_cost(lots, a))
                                         for a in holdings])
    return status
