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
from ibkr_agent.crypto.gate import GateAnswer, judge, parse
from ibkr_agent.crypto.llm import Budget, Call, ask_json, cost_usd
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

    def _create(self, **kw):
        self.calls.append(kw)
        p = self.payloads.pop(0) if self.payloads else {}
        text = p if isinstance(p, str) else json.dumps(p)
        return SimpleNamespace(model=kw["model"], stop_reason=self.stop_reason, usage=self.usage,
                               content=[SimpleNamespace(type="text", text=text)])


def ai_settings(tmp_path, **ai):
    return settings(tmp_path, ai=CryptoAI(**ai))


def call(model="claude-haiku-5-5", estimate=0.01):
    return Call("gate", model, "low", "sys", "user", {"type": "object"}, estimate)


# ---------- 预算和调用 ----------

def test_cost_and_budget_cap():
    assert cost_usd("claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(4 + 2)
    assert cost_usd("unknown-model", 1_000_000, 0) == pytest.approx(10)  # 不认识的模型按贵的估
    b = Budget(Journal(":memory:").db, monthly_cap_usd=0.005)
    client = FakeClient({"x": 1})
    assert ask_json(client, b, call(estimate=0.001)) == {"x": 1}
    assert b.spent_this_month() == pytest.approx(cost_usd("claude-haiku-5-5", 1000, 200))
    assert ask_json(client, b, call(estimate=0.01)) is None      # 超上限就不调用
    assert len(client.calls) == 1


def test_bad_answers_become_none():
    b = Budget(Journal(":memory:").db, 10)
    assert ask_json(FakeClient("不是 JSON"), b, call()) is None
    assert ask_json(FakeClient({"x": 1}, stop_reason="max_tokens"), b, call()) is None
    assert b.by_purpose()["gate"][0] == 2                          # 失败的调用也照样记账


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
    assert e.tilt == pytest.approx(0.10)                           # ±10pp 上限
    assert (e.buy_low, e.buy_high) == pytest.approx((75.0, 125.0))  # 离现价最多 ±25%
    assert [c.kind for c in e.invalidation] == ["price_below"]      # 7 天跌幅 300% 无意义，丢掉
    assert e.valid_until == (NOW_DT + timedelta(days=8)).isoformat()
    assert clamp({**RAW, "confidence": "low"}, 100.0, s, NOW_DT).effective_tilt() == 0
    assert clamp({**RAW, "stance": "yolo"}, 100.0, s, NOW_DT) is None


def test_tilted_targets_respect_caps():
    env = lambda t: Envelope("X", "add", t, 0, 1e9, (), "high", "", "", "")
    t = tilted_targets({"BTC": 0.6, "ETH": 0.4}, {"BTC": env(0.1), "ETH": env(0.1)}, 0.7)
    assert t["BTC"] <= 0.7 and sum(t.values()) == pytest.approx(1.0)
    assert t["BTC"] / t["ETH"] == pytest.approx(0.7 / 0.5)
    t = tilted_targets({"BTC": 0.6, "ETH": 0.4}, {"BTC": env(-0.1)}, 0.7)
    assert t == pytest.approx({"BTC": 0.5, "ETH": 0.4})            # 减下来的部分留成现金


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
    assert any("快哨兵没有给出答案" in l for l in lines)


def test_gate_pass_buys_and_records(tmp_path):
    s = live(tmp_path, gate_enabled=True)
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    j = Journal(":memory:")
    ok = {"p_invalidated": 0.05, "p_abnormal": 0.05, "p_up_7d": 0.6, "note": ""}
    client = FakeClient(ok, ok)
    run_cycle(s, ex, j, out=lambda _: None, clock=lambda: NOW, sleep=lambda _: None, ai_client=client)
    assert [p[1] for p in ex.placed] == ["BUY", "BUY"]
    assert j.db.execute("SELECT COUNT(*) FROM crypto_gate WHERE verdict='pass'").fetchone()[0] == 2
    assert j.db.execute("SELECT COUNT(*) FROM crypto_prices").fetchone()[0] == 2
    assert all(c["model"] == "claude-haiku-5-5" for c in client.calls)


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
    assert any("只减不买" in l for l in lines)


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
    assert EnvelopeStore(j.db, "okx-demo").current("BTC", NOW_DT).tilt == pytest.approx(0.10)
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
