"""先复现、再修：每个测试对应一个确认过的问题，修之前都应该失败。"""

import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_crypto import BOOKS, NOW, NOW_DT, FakeExchange
from test_crypto_ai import RAW, FakeClient, ai_settings, live

from ibkr_agent.crypto import calibration
from ibkr_agent.crypto.cycle import run_cycle
from ibkr_agent.crypto.config import CryptoAI, parse
from ibkr_agent.crypto.envelope import (Envelope, EnvelopeStore, Invalidation, buy_block, clamp, facts,
                                        tilted_targets, weekly)
from ibkr_agent.crypto.exchange import Book
from ibkr_agent.crypto.gate import SYSTEM_PROMPT, Gate, dossier
from ibkr_agent.crypto.gate import parse as parse_gate
from ibkr_agent.crypto.llm import Budget
from ibkr_agent.journal import Journal

PASS = {"p_invalidated": 0.05, "p_abnormal": 0.05, "p_up_7d": 0.6, "note": ""}


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class ClockedExchange(FakeExchange):
    """盘口时间跟着假时钟走，才能测出“时间过去了多少”。"""

    def __init__(self, clock, *a, **kw):
        super().__init__(*a, **kw)
        self.clock = clock

    def book(self, symbol):
        b = super().book(symbol)
        return Book(symbol, b.bid, b.ask, self.clock())


class SlowClient(FakeClient):
    """每次调用让假时钟往前走 minutes 分钟，模拟 Anthropic 接口卡住。"""

    def __init__(self, clock, minutes, *payloads):
        super().__init__(*payloads)
        self.clock = clock
        self.minutes = minutes

    def _create(self, **kw):
        self.clock.t += self.minutes * 60
        return super()._create(**kw)


def test_slow_gate_does_not_make_sells_expire(tmp_path):
    # 问题：快哨兵在所有订单之前被问，接口卡 16 分钟，卖出计划（15 分钟有效）就过期了，卖不出去
    s = live(tmp_path, gate_enabled=True)
    clock = Clock(NOW)
    ex = ClockedExchange(clock, {"EUR": 100.0, "BTC": 0.0055, "ETH": 0.01}, BOOKS)   # BTC 超配要卖，ETH 低配要买
    run_cycle(s, ex, Journal(":memory:"), out=lambda _: None, clock=clock, sleep=lambda _: None,
              ai_client=SlowClient(clock, 16, PASS))
    assert [p[1] for p in ex.placed] == ["SELL"]


def test_fallback_attempts_are_all_billed():
    # 问题：服务端换模型重答时，只按最后那次（而且按“不认识的模型”价）记账，被拒的那次 Opus 没记
    b = Budget(Journal(":memory:").db, 10)

    def it(kind, model, i, o):
        return SimpleNamespace(type=kind, model=model, input_tokens=i, output_tokens=o,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)

    usage = SimpleNamespace(input_tokens=3000, output_tokens=2000, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0,
                            iterations=[it("message", "claude-opus-5-5", 3000, 15000),
                                        it("fallback_message", "claude-opus-5", 3000, 2000)])
    cost = b.record("envelope", "claude-opus-5", usage)
    assert cost == pytest.approx((3000 * 4 + 15000 * 20 + 3000 * 5 + 2000 * 25) / 1e6)


def test_gate_prompt_does_not_push_uncertain_answers_over_the_block_threshold():
    # 问题：提示词让模型“拿不准就答 0.5”，但失效和异常两题超过 0.3 就不买，没把握就等于永远不买
    for line in SYSTEM_PROMPT.splitlines():
        if "0.5" in line:
            assert "p_up_7d" in line, line


def test_invalid_envelope_is_logged_not_silently_dropped(tmp_path, caplog):
    # 问题：Opus 的回答不合规时直接返回 None，没有任何日志
    s = ai_settings(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert clamp({**RAW, "stance": "yolo"}, 100.0, s, NOW_DT) is None
    assert any("yolo" in r.getMessage() for r in caplog.records)


def test_weekly_review_says_what_it_ignored_or_changed(tmp_path):
    # 问题：不在目标里的币、没拿到边界的币、被裁剪的倾斜、被丢掉的失效条件，输出里都看不到
    s = ai_settings(tmp_path, envelope_enabled=True)
    raw = {"assets": [{**RAW, "asset": "BTC"}, {**RAW, "asset": "DOGE"}], "summary": "ok"}
    lines = []
    weekly(s, FakeExchange({"EUR": 300.0}, BOOKS), Journal(":memory:").db, "okx-demo", FakeClient(raw), NOW_DT,
           out=lines.append)
    assert any("DOGE" in line for line in lines)
    assert any("ETH" in line and "按配置目标" in line for line in lines)
    assert any("倾斜" in line and "上限" in line for line in lines)
    assert any("drop_7d_above" in line and "不采用" in line for line in lines)


def test_bad_gate_answer_is_logged(caplog):
    # 问题：快哨兵的回答格式不对时直接当成没回答，日志里看不出哪里不对
    with caplog.at_level(logging.WARNING):
        assert parse_gate({"p_invalidated": 1.2, "p_abnormal": 0, "p_up_7d": 0.5, "note": ""}) is None
    assert any("p_invalidated" in r.getMessage() for r in caplog.records)


def test_missing_envelope_is_shown_in_run_output(tmp_path):
    # 问题：打开了边界但还没跑 crypto envelope 时，只写进日志库，运行输出里看不到
    s = live(tmp_path, envelope_enabled=True)
    lines = []
    run_cycle(s, FakeExchange({"EUR": 300.0}, BOOKS), Journal(":memory:"), out=lines.append, clock=lambda: NOW,
              sleep=lambda _: None)
    assert any("没有有效的 Opus 边界" in line for line in lines)


def test_gate_call_has_a_short_timeout(tmp_path):
    # 问题（同第一个测试的另一半原因）：SDK 默认等 600 秒、重试 2 次，快哨兵一次调用最长能卡半小时
    s = ai_settings(tmp_path, gate_enabled=True)
    client = FakeClient(PASS)
    j = Journal(":memory:")
    Gate(client, Budget(j.db, 10), j.db, "a", s).ask(
        dossier("BTC", 60005.0, 1.7, (60000.0, 59700.0, 60300.0), 15, None, None, 50.0, "EUR"), NOW_DT)
    assert client.options == [{"timeout": s.ai.gate_timeout_seconds, "max_retries": s.ai.gate_max_retries}]
    assert s.ai.gate_timeout_seconds <= 60


# ---------- Opus 的倾斜只能影响买入 ----------

def envelope(asset, stance="add", tilt=0.0, low=1.0, high=1e9, inv=()):
    return Envelope(asset, stance, tilt, low, high, tuple(inv), "high", NOW_DT.isoformat(),
                    (NOW_DT + timedelta(days=8)).isoformat(), "")


def run_with(tmp_path, balances, *envs):
    s = live(tmp_path, envelope_enabled=True)
    j = Journal(":memory:")
    store = EnvelopeStore(j.db, "okx-demo")
    for e in envs:
        store.save(e)
    ex = FakeExchange(balances, BOOKS)
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    return ex, lines


def holdings(btc_w, eth_w, nav=300.0):
    btc, eth = sum(BOOKS["BTC/EUR"]) / 2, sum(BOOKS["ETH/EUR"]) / 2
    return {"EUR": nav * (1 - btc_w - eth_w), "BTC": nav * btc_w / btc, "ETH": nav * eth_w / eth}


def test_negative_tilt_never_creates_a_sell(tmp_path):
    # 问题：倾斜同时改了卖出的目标，Opus 给 −10pp（哪怕立场是 hold）就会让系统卖出，违背“卖出不经过模型”
    ex, _ = run_with(tmp_path, holdings(0.608, 0.372), envelope("BTC", "hold", -0.10))
    assert not any(side == "SELL" for _, side, _, _ in ex.placed)


def test_tilt_cannot_keep_a_coin_above_max_weight(tmp_path):
    # 问题：正倾斜把卖出线也往上推，BTC 72% 超过单币上限 70% 也不卖
    ex, _ = run_with(tmp_path, holdings(0.72, 0.26), envelope("BTC", "add", 0.10))
    assert ("BTC/EUR", "SELL") in [(sym, side) for sym, side, _, _ in ex.placed]


def test_tilt_only_applies_to_add_stance_and_only_to_its_own_coin():
    # 问题：hold 也在用倾斜；只有 BTC 有边界时，归一化把 ETH 的目标也改了，日志却说 ETH “按配置目标”
    t = tilted_targets({"BTC": 0.6, "ETH": 0.4}, {"BTC": envelope("BTC", "add", 0.05)}, 0.7, 0.05)
    assert t == pytest.approx({"BTC": 0.65, "ETH": 0.4})
    assert tilted_targets({"BTC": 0.6, "ETH": 0.4}, {"BTC": envelope("BTC", "hold", 0.05)}, 0.7, 0.05)["BTC"] == 0.6


def test_max_tilt_must_leave_room_in_the_band():
    # 问题：倾斜 10pp、再平衡带 10pp 时，买到倾斜后的目标就正好碰到卖出线，下次运行又卖回去
    with pytest.raises(ValueError, match="max_tilt"):
        parse({"rebalance_band": 0.10, "ai": {"max_tilt": 0.10}})


def test_buy_is_rechecked_with_the_price_at_send_time(tmp_path):
    # 问题：买入检查在所有订单之前用开头的盘口做完；卖出挂单等成交的几分钟里 ETH 跌破失效价，买入照样发出
    s = live(tmp_path, envelope_enabled=True)
    j = Journal(":memory:")
    EnvelopeStore(j.db, "okx-demo").save(
        envelope("ETH", inv=[Invalidation("price_below", 2950.0, "跌破支撑")]))
    ex = FakeExchange({"EUR": 100.0, "BTC": 0.0055, "ETH": 0.01}, BOOKS,      # BTC 超配要卖，ETH 低配要买
                      move_after_plan={"ETH/EUR": (2895.0, 2896.0)})           # 第二次取 ETH 盘口时已经跌了
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    assert [side for _, side, _, _ in ex.placed] == ["SELL"]
    assert any("ETH" in line and "失效条件" in line for line in lines)


# ---------- 行情数字的口径 ----------

def test_volatility_uses_neighbouring_closes_with_short_history():
    # 问题：日线少于 31 根时，zip 把每个收盘价和它自己配对，波动率总是 0，Opus 以为这个币完全不波动
    closes = [95.0 if i % 2 else 105.0 for i in range(20)]
    assert facts("BTC", closes, closes[-1], 0.5, 0.6)["annualized_vol_30d"] > 0.5


class SeriesExchange(FakeExchange):
    """日线按给定的序列返回（最后一根是今天没收完的），要几根给最后几根。"""

    def __init__(self, series, *a, **kw):
        super().__init__(*a, **kw)
        self.series = series

    def daily_closes(self, symbol, days):
        return self.series[symbol][-days:]


def test_week_ago_price_is_at_least_seven_days_old(tmp_path):
    # 问题：最后一根日线是今天没收完的，closes[-8] 其实是 6 天多之前（UTC 零点）的价格。
    # ETH 在 6 天 21 小时前从 3600 跌到 3000（跌了 17%），超过 Opus 设的“7 天跌幅 15%”，却没拦住
    s = live(tmp_path, envelope_enabled=True)
    j = Journal(":memory:")
    EnvelopeStore(j.db, "okx-demo").save(envelope("ETH", inv=[Invalidation("drop_7d_above", 0.15, "急跌")]))
    eth = [3600.0] * 3 + [3000.0] * 8           # 最后 8 根都是 3000；再往前一根（7 天多以前）是 3600
    ex = SeriesExchange({"ETH/EUR": eth, "BTC/EUR": [60000.0] * 11}, {"EUR": 100.0, "BTC": 0.0055, "ETH": 0.01}, BOOKS)
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    assert not any(side == "BUY" and sym == "ETH/EUR" for sym, side, _, _ in ex.placed)
    assert any("ETH" in line and "失效条件" in line for line in lines)


def test_drop_condition_without_a_week_ago_price_blocks_buying():
    # 问题：日线不够长时拿不到 7 天前的价格，“7 天跌幅”失效条件被跳过，照样买（应当拿不准就不买）
    e = envelope("BTC", inv=[Invalidation("drop_7d_above", 0.10, "急跌")])
    assert buy_block(e, 100.0, None) is not None


def test_lowering_max_tilt_applies_to_envelopes_already_stored(tmp_path):
    # 问题：边界存下时的倾斜不会按现在的 max_tilt 重新裁剪，把 max_tilt 调成 0 也要等边界过期（最多 8 天）才生效
    s = live(tmp_path, envelope_enabled=True)
    s = s.__class__(**{**s.__dict__, "ai": CryptoAI(envelope_enabled=True, max_tilt=0.0)})
    j = Journal(":memory:")
    EnvelopeStore(j.db, "okx-demo").save(envelope("BTC", "add", 0.05))
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    run_cycle(s, ex, j, out=lambda _: None, clock=lambda: NOW, sleep=lambda _: None)
    spent = {sym: amount * price for sym, _, amount, price in ex.placed}
    assert spent["BTC/EUR"] / spent["ETH/EUR"] == pytest.approx(0.6 / 0.4, rel=1e-3)


# ---------- Opus 回答里的重复和校准表 ----------

def test_duplicate_envelopes_keep_only_the_first_and_say_so(tmp_path):
    # 问题：同一个币返回两条（例如 BTC 和 btc）时都存下来，运行时用哪条取决于数据库的顺序
    s = ai_settings(tmp_path, envelope_enabled=True)
    raw = {"assets": [{**RAW, "asset": "BTC"}, {**RAW, "asset": "btc", "stance": "reduce_only"}, {**RAW, "asset": "ETH"}],
           "summary": ""}
    j = Journal(":memory:")
    lines = []
    envs = weekly(s, FakeExchange({"EUR": 300.0}, BOOKS), j.db, "okx-demo", FakeClient(raw), NOW_DT, out=lines.append)
    assert [e.asset for e in envs] == ["BTC", "ETH"]
    assert EnvelopeStore(j.db, "okx-demo").current("BTC", NOW_DT).stance == "add"
    assert any("BTC" in line and "不止一条" in line for line in lines)


def test_answers_whose_window_passed_without_a_price_are_not_pending_forever():
    # 问题：6.5–10 天的窗口里没有记到价格（比如那周电脑没开），这条回答永远显示“等待中”
    j = Journal(":memory:")
    calibration.build(j.db, "a", NOW_DT)
    t0 = NOW_DT - timedelta(days=30)
    j.db.execute("INSERT INTO crypto_gate VALUES (?,?,?,?,?,?,?,?,?)", (t0.isoformat(), "a", "BTC", 100, 0, 0, 0.6, "pass", "m"))
    j.db.execute("INSERT INTO crypto_prices VALUES (?,?,?,?)", ((t0 + timedelta(days=14)).isoformat(), "a", "BTC", 110))
    c = calibration.build(j.db, "a", NOW_DT)
    assert c.pending == 0
    assert c.expired == 1 and "没有记到价格" in calibration.render(c)
