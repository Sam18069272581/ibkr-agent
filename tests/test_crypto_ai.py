import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_crypto import BOOKS, NOW, NOW_DT, FakeExchange, settings

from ibkr_agent.crypto import calibration
from ibkr_agent.crypto.config import CryptoAI
from ibkr_agent.crypto.cycle import run_cycle
from ibkr_agent.crypto.envelope import EnvelopeStore, Envelope, Invalidation, buy_block, clamp, tilted_targets, weekly
from ibkr_agent.crypto.gate import Gate, GateAnswer, dossier, judge, parse
from ibkr_agent.crypto.llm import Budget, Call, ask_json, cost_usd, usd, worst_case_usd
from ibkr_agent.journal import Journal


class FakeClient:
    """假的 Anthropic 客户端：按顺序返回准备好的 JSON，记录每次请求的参数。"""

    def __init__(self, *payloads, stop_reason="end_turn", usage=(1000, 200)):
        self.payloads = list(payloads)
        self.calls = []
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1],
                                     cache_creation_input_tokens=0, cache_read_input_tokens=0)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.options = []

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def _create(self, **kw):
        self.calls.append(kw)
        p = self.payloads.pop(0) if self.payloads else {}
        text = p if isinstance(p, str) else json.dumps(p)
        return SimpleNamespace(model=kw["model"], stop_reason=self.stop_reason, usage=self.usage,
                               content=[SimpleNamespace(type="text", text=text)])


def ai_settings(tmp_path, **ai):
    return settings(tmp_path, ai=CryptoAI(**ai))


PRICES = CryptoAI().prices


def call(model="claude-haiku-5-5", max_tokens=4000):
    return Call("gate", model, "low", "sys", "user", {"type": "object"}, max_tokens, timeout_s=30, max_retries=1)


# ---------- 预算和调用 ----------

def test_cost_and_budget_cap():
    assert cost_usd(PRICES, "claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(4 + 2)
    assert cost_usd(PRICES, "claude-haiku-5-5-20261001", 1_000_000, 0) == pytest.approx(0.10)  # 带日期的型号按前缀
    b = Budget(Journal(":memory:").db, monthly_cap_usd=0.005)
    client = FakeClient({"x": 1})
    assert ask_json(client, b, call()).data == {"x": 1}          # Haiku 最坏也就 $0.002
    assert b.spent_this_month() == pytest.approx(cost_usd(PRICES, "claude-haiku-5-5", 1000, 200))
    refused = ask_json(client, b, call("claude-opus-5-5", 16000))  # Opus 最坏要几毛钱，超上限就不调用
    assert refused.data is None and "上限" in refused.error
    assert len(client.calls) == 1


def test_unknown_model_is_priced_at_the_most_expensive_column_and_warned(caplog):
    with caplog.at_level("WARNING"):
        assert cost_usd(PRICES, "claude-next", 1_000_000, 1_000_000) == pytest.approx(5 + 25)
    assert "claude-next" in caplog.text


def test_worst_case_estimate_comes_from_prices_and_limits():
    # 调用前的预算检查用“最坏情况”：输出写满 max_tokens，开了服务端兜底再加一次最贵的
    haiku = worst_case_usd(call(), PRICES)
    assert haiku == pytest.approx((50 * 0.125 + 4000 * 0.5) / 1e6, rel=0.05)
    opus = worst_case_usd(call("claude-opus-5-5", 16000), PRICES)
    assert opus > 16000 * (20 + 25) / 1e6


def test_records_one_row_per_call_even_with_fallback():
    b = Budget(Journal(":memory:").db, 10)
    it = [SimpleNamespace(model="claude-opus-5-5", input_tokens=10, output_tokens=10),
          SimpleNamespace(model="claude-opus-5", input_tokens=10, output_tokens=10)]
    b.record("envelope", "claude-opus-5", SimpleNamespace(input_tokens=10, output_tokens=10, iterations=it))
    assert b.by_purpose()["envelope"][0] == 1
    assert b.db.execute("SELECT model FROM api_usage").fetchone()[0] == "claude-opus-5-5→claude-opus-5"


def test_small_costs_are_not_shown_as_zero():
    assert usd(0.0007) == "$0.0007" and usd(0.05) == "$0.05" and usd(0) == "$0.00"


def test_bad_answers_become_no_answer_with_a_reason(caplog):
    b = Budget(Journal(":memory:").db, 10)
    with caplog.at_level("WARNING"):
        assert "不是 JSON" in ask_json(FakeClient("不是 JSON"), b, call()).error
        assert "max_tokens" in ask_json(FakeClient({"x": 1}, stop_reason="max_tokens"), b, call()).error
        assert "list" in ask_json(FakeClient("[1, 2]"), b, call()).error
    assert b.by_purpose()["gate"][0] == 3                          # 失败的调用也照样记账
    assert caplog.text.count("没有答案") == 3                       # 每次都有日志


class Broken:
    """创建请求时抛出意外错误的客户端。"""

    def __init__(self, error):
        self.error = error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **kw):
        return self

    def _create(self, **kw):
        raise self.error


def test_unexpected_client_error_is_no_answer(caplog):
    with caplog.at_level("ERROR"):
        a = ask_json(Broken(TypeError("bad kwarg")), Budget(Journal(":memory:").db, 10), call())
    assert a.data is None and "bad kwarg" in a.error
    assert "bad kwarg" in caplog.text                              # 带 traceback 记进日志


def test_fallbacks_only_for_models_that_support_them():
    b = Budget(Journal(":memory:").db, 10)
    haiku, opus = FakeClient({}), FakeClient({})
    ask_json(haiku, b, call("claude-haiku-5-5"))
    ask_json(opus, b, call("claude-opus-5-5"))
    assert "fallbacks" not in haiku.calls[0]
    assert opus.calls[0]["fallbacks"] == "default"


# ---------- Opus 边界 ----------

RAW = {"asset": "BTC", "bull_case": "b", "bear_case": "s", "stance": "add", "tilt": 0.5,
       "buy_low": 1.0, "buy_high": 1e9, "confidence": "high",
       "invalidation": [{"kind": "price_below", "value": 90.0, "why": "破位"},
                        {"kind": "drop_7d_above", "value": 3.0, "why": "无效值"}]}


def test_clamp_limits_what_opus_can_set(tmp_path):
    s = ai_settings(tmp_path)
    e = clamp(RAW, 100.0, s, NOW_DT)
    assert e.tilt == pytest.approx(0.05)                           # ±5pp 上限
    assert (e.buy_low, e.buy_high) == pytest.approx((75.0, 125.0))  # 离现价最多 ±25%
    assert [c.kind for c in e.invalidation] == ["price_below"]      # 7 天跌幅 300% 无意义，丢掉
    assert e.valid_until == (NOW_DT + timedelta(days=8)).isoformat()
    assert clamp({**RAW, "confidence": "low"}, 100.0, s, NOW_DT).effective_tilt() == 0
    assert clamp({**RAW, "stance": "yolo"}, 100.0, s, NOW_DT) is None


def test_tilted_targets_respect_caps():
    def env(t):
        return Envelope("X", "add", t, 0, 1e9, (), "high", "", "", "")

    t = tilted_targets({"BTC": 0.6, "ETH": 0.4}, {"BTC": env(0.05), "ETH": env(0.05)}, 0.62, 0.05)
    assert t == pytest.approx({"BTC": 0.62, "ETH": 0.45})          # 不超过单币上限；总和略超 1 时买入受现金约束
    t = tilted_targets({"BTC": 0.6, "ETH": 0.4}, {"BTC": env(-0.2)}, 0.7, 0.05)
    assert t == pytest.approx({"BTC": 0.55, "ETH": 0.4})           # 按现在的 max_tilt 裁剪；只是少买，多出来的留成现金


def test_buy_block_rules():
    e = Envelope("BTC", "add", 0, 90, 110, (Invalidation("price_below", 95, "破位"),
                                             Invalidation("drop_7d_above", 0.15, "急跌")), "high", "", "", "")
    assert buy_block(e, 100, 100) is None
    assert "失效条件" in buy_block(e, 94, 100)
    assert "失效条件" in buy_block(e, 100, 120)                    # 7 天跌了 16.7%
    assert "区间" in buy_block(e, 111, 111)
    assert "只减不买" in buy_block(replace(e, stance="reduce_only"), 100, 100)


# ---------- 快哨兵 ----------

def test_gate_parse_and_thresholds(tmp_path):
    s = ai_settings(tmp_path)
    assert parse({"p_invalidated": 1.2, "p_abnormal": 0, "p_up_7d": 0.5, "note": ""}) is None
    assert parse({"p_abnormal": 0, "p_up_7d": 0.5}) is None
    assert judge(None, s).verdict == "no_answer"
    assert judge(GateAnswer(0.1, 0.1, 0.6, ""), s).verdict == "pass"
    assert judge(GateAnswer(0.4, 0.1, 0.6, ""), s).verdict == "block"
    assert judge(GateAnswer(0.1, 0.31, 0.6, ""), s).verdict == "block"


# ---------- 接进运行流程 ----------

def live(tmp_path, **ai):
    return replace(ai_settings(tmp_path, **ai), dry_run=False)


def test_gate_without_client_blocks_buys_but_not_sells(tmp_path):
    s = live(tmp_path, gate_enabled=True)
    ex = FakeExchange({"EUR": 100.0, "BTC": 0.0055, "ETH": 0.01}, BOOKS)   # BTC 超配要卖，ETH 低配要买
    j = Journal(":memory:")
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    assert [p[1] for p in ex.placed] == ["SELL"]
    assert any("快哨兵不可用" in line for line in lines)


def test_broken_gate_still_lets_sells_through(tmp_path):
    s = live(tmp_path, gate_enabled=True)
    ex = FakeExchange({"EUR": 100.0, "BTC": 0.0055, "ETH": 0.01}, BOOKS)
    j = Journal(":memory:")
    lines = []
    assert run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None,
                     ai_client=Broken(RuntimeError("boom"))) == "ok"
    assert [p[1] for p in ex.placed] == ["SELL"]
    assert j.db.execute("SELECT verdict FROM crypto_gate").fetchall() == [("no_answer",)]
    assert any("快哨兵没有回答" in line and "boom" in line for line in lines)   # 为什么不买要看得到


def test_unusable_gate_answer_says_what_was_wrong(tmp_path):
    s = ai_settings(tmp_path, gate_enabled=True)
    j = Journal(":memory:")
    r = Gate(FakeClient({"p_invalidated": 0.1, "p_abnormal": 0.1, "note": ""}), Budget(j.db, 10), j.db, "a", s).ask(
        dossier("BTC", 60005.0, 1.7, (60000.0, 59700.0, 60300.0), 15, None, None, 50.0, "EUR"), NOW_DT)
    assert r.verdict == "no_answer" and "p_up_7d" in r.why


def test_gate_pass_buys_and_records(tmp_path):
    s = live(tmp_path, gate_enabled=True)
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    j = Journal(":memory:")
    ok = {"p_invalidated": 0.05, "p_abnormal": 0.05, "p_up_7d": 0.6, "note": ""}
    client = FakeClient(ok, ok)
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None, ai_client=client)
    assert [p[1] for p in ex.placed] == ["BUY", "BUY"]
    assert j.db.execute("SELECT COUNT(*) FROM crypto_gate WHERE verdict='pass'").fetchone()[0] == 2
    assert j.db.execute("SELECT COUNT(*) FROM events WHERE kind='gate'").fetchone()[0] == 2
    assert sum("快哨兵放行" in line for line in lines) == 2          # 放行也要在输出里看得到
    assert j.db.execute("SELECT COUNT(*) FROM crypto_prices").fetchone()[0] == 2
    assert all(c["model"] == "claude-haiku-5-5" for c in client.calls)
    assert sum("问快哨兵" in line for line in lines) == 2             # 等模型回答时有提示


def test_envelope_reduce_only_stops_that_coin(tmp_path):
    s = live(tmp_path, envelope_enabled=True)
    j = Journal(":memory:")
    store = EnvelopeStore(j.db, "okx-demo")
    store.save(Envelope("BTC", "reduce_only", 0.0, 1, 1e9, (), "high", NOW_DT.isoformat(),
                        (NOW_DT + timedelta(days=8)).isoformat(), ""))
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    assert [p[0] for p in ex.placed] == ["ETH/EUR"]
    assert any("只减不买" in line for line in lines)


def test_expired_envelope_is_ignored(tmp_path):
    j = Journal(":memory:")
    store = EnvelopeStore(j.db, "a")
    store.save(Envelope("BTC", "reduce_only", 0.0, 1, 2, (), "high", "x", (NOW_DT - timedelta(hours=1)).isoformat(), ""))
    assert store.current("BTC", NOW_DT) is None


def test_weekly_review_stores_clamped_envelopes(tmp_path):
    s = ai_settings(tmp_path, envelope_enabled=True)
    j = Journal(":memory:")
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    raw = {"assets": [{**RAW, "asset": "BTC"}, {**RAW, "asset": "DOGE"}], "summary": "ok"}
    client = FakeClient(raw)
    envs = weekly(s, ex, j.db, "okx-demo", client, NOW_DT, out=lambda _: None)
    assert [e.asset for e in envs] == ["BTC"]                      # 不在目标里的币不收
    assert EnvelopeStore(j.db, "okx-demo").current("BTC", NOW_DT).tilt == pytest.approx(0.05)
    assert client.calls[0]["model"] == "claude-opus-5-5"
    sent = json.loads(client.calls[0]["messages"][0]["content"].split("\n", 1)[1])
    assert {d["asset"] for d in sent} == {"BTC", "ETH"} and "missing" in sent[0]


# ---------- 校准表 ----------

def test_calibration_scores_answers_after_a_week():
    j = Journal(":memory:")
    db = j.db
    calibration.build(db, "a", NOW_DT)  # 建表
    t0 = NOW_DT - timedelta(days=8)
    for i, (p, later) in enumerate([(0.9, 110), (0.9, 90), (0.1, 90), (0.7, 120)]):
        ts = (t0 + timedelta(minutes=i)).isoformat()
        db.execute("INSERT INTO crypto_gate VALUES (?,?,?,?,?,?,?,?,?)", (ts, "a", f"C{i}", 100, 0, 0, p, "pass", "m"))
        db.execute("INSERT INTO crypto_prices VALUES (?,?,?,?)",
                   ((t0 + timedelta(days=7, minutes=i)).isoformat(), "a", f"C{i}", later))
    db.execute("INSERT INTO crypto_gate VALUES (?,?,?,?,?,?,?,?,?)",
               (NOW_DT.isoformat(), "a", "C9", 100, 0, 0, 0.5, "pass", "m"))   # 还没到 7 天
    c = calibration.build(db, "a", NOW_DT)
    assert (c.scored, c.pending) == (4, 1)
    assert c.brier == pytest.approx((0.01 + 0.81 + 0.01 + 0.09) / 4)
    top = next(b for b in c.buckets if b.low == 0.8)
    assert (top.n, top.hit_rate) == (2, 0.5)
    assert "Brier" in calibration.render(c)
