from ibkr_agent.config import GraduationConfig
from ibkr_agent.journal import FillRecord, Journal, PositionSnapshot
from ibkr_agent.report import build, render

G = GraduationConfig(min_consecutive_ok_runs=3)


def add_run(j, status="ok", nav=2000.0, spy=500.0, halted=False, dry_run=False):
    rid = j.start_run("paper", dry_run)
    j.set_account(rid, nav, nav, spy, halted)
    j.finish_run(rid, status)
    return rid


def test_passes_when_all_gates_met():
    j = Journal(":memory:")
    rid = add_run(j, nav=2000, spy=500)
    j.record_fill(rid, FillRecord("e1", "t", "A", "BUY", 7, 50.02, 1.0, None, 50.0, 1.35))
    add_run(j, nav=2050, spy=510)
    last = add_run(j, nav=2080, spy=515)
    j.record_snapshot(last, [PositionSnapshot("A", 7, 380, 50.0)])
    r = build(j.db, "paper", G)
    assert r.consecutive_ok == 3
    assert abs(r.total_return - 0.04) < 1e-9
    assert abs(r.benchmark_return - 0.03) < 1e-9
    assert abs(r.cost_ratio - (1.0 + 0.14) / 1.35) < 1e-9
    assert r.open_winners == 1
    assert r.graduated, render(r)


def test_error_resets_streak_and_dry_runs_are_ignored():
    j = Journal(":memory:")
    add_run(j)
    add_run(j, status="error")
    add_run(j)
    add_run(j, dry_run=True)
    r = build(j.db, "paper", G)
    assert r.runs == 3 and r.errors == 1 and r.consecutive_ok == 1
    assert not r.graduated


def test_no_fills_fails_cost_gate():
    j = Journal(":memory:")
    for nav in (2000, 2010, 2020):
        add_run(j, nav=nav)
    r = build(j.db, "paper", G)
    cost_gate = next(g for g in r.gates if g.name == "成本符合预期")
    assert not cost_gate.passed and not r.graduated


def test_drawdown_halt_and_mismatch_fail():
    j = Journal(":memory:")
    add_run(j, halted=True)
    add_run(j, status="mismatch")
    r = build(j.db, "paper", G)
    failed = {g.name for g in r.gates if not g.passed}
    assert {"对账一致", "未触发回撤熔断"} <= failed


def test_underperformance_gate():
    j = Journal(":memory:")
    rid = add_run(j, nav=2000, spy=500)
    j.record_fill(rid, FillRecord("e1", "t", "A", "BUY", 7, 50.0, 1.0, None, 50.0, 1.35))
    add_run(j, nav=2000, spy=550)
    add_run(j, nav=2000, spy=550)  # 落后 10 个百分点
    r = build(j.db, "paper", G)
    assert not next(g for g in r.gates if g.name == "不明显落后基准").passed


def test_render_empty_journal():
    assert "继续跑模拟盘" in render(build(Journal(":memory:").db, "paper", G))
