from ibkr_agent.journal import Journal


def add_run(j, hwm, status="ok", dry_run=False):
    rid = j.start_run("paper", dry_run)
    j.set_account(rid, hwm, hwm, 500.0, False)
    j.finish_run(rid, status)


def test_high_water_mark_ignores_dry_runs_and_errors():
    j = Journal(":memory:")
    add_run(j, 1_006_765, dry_run=True)  # 模拟盘余额重置前的演练
    add_run(j, 9_999, status="error")
    assert j.high_water_mark() == 0.0
    add_run(j, 3_468)
    assert j.high_water_mark() == 3_468
