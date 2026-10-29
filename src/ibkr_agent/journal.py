"""决策日志：每次运行的打分、Claude 观点、风控结果、成交、持仓快照都落库。

这是闭环的记录端：report.py 只从这里读数据来算成绩和转实盘门槛。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | error | mismatch
    error TEXT,
    finished_ts TEXT,
    net_liquidation_usd REAL,
    high_water_mark_usd REAL,
    benchmark_price REAL,
    drawdown_halted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL,      -- score | view | order | approved | rejected | warning
    ticker TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    exec_id TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,      -- BUY | SELL
    shares REAL NOT NULL,
    price REAL NOT NULL,
    commission REAL NOT NULL,
    realized_pnl REAL,       -- 只有平仓成交才有
    ref_mid REAL NOT NULL,   -- 下单时的中间价，用来算实际滑点
    modelled_cost REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ticker TEXT NOT NULL,
    shares REAL NOT NULL,
    market_value REAL NOT NULL,
    avg_cost REAL NOT NULL
);
"""


@dataclass(frozen=True)
class FillRecord:
    exec_id: str
    ts: str
    ticker: str
    side: str
    shares: float
    price: float
    commission: float
    realized_pnl: float | None
    ref_mid: float
    modelled_cost: float


@dataclass(frozen=True)
class PositionSnapshot:
    ticker: str
    shares: float
    market_value: float
    avg_cost: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def high_water_mark(self) -> float:
        row = self.db.execute("SELECT MAX(high_water_mark_usd) FROM runs WHERE status != 'error' AND dry_run = 0").fetchone()
        return row[0] or 0.0

    def start_run(self, mode: str, dry_run: bool) -> int:
        cur = self.db.execute("INSERT INTO runs (ts, mode, dry_run) VALUES (?,?,?)", (_now(), mode, int(dry_run)))
        self.db.commit()
        return cur.lastrowid

    def set_account(self, run_id: int, net_liq: float, hwm: float, benchmark_price: float | None, halted: bool) -> None:
        self.db.execute(
            "UPDATE runs SET net_liquidation_usd=?, high_water_mark_usd=?, benchmark_price=?, drawdown_halted=? WHERE id=?",
            (net_liq, hwm, benchmark_price, int(halted), run_id),
        )
        self.db.commit()

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, error=?, finished_ts=? WHERE id=?", (status, error, _now(), run_id)
        )
        self.db.commit()

    def log(self, run_id: int, kind: str, obj) -> None:
        data = asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
        self.db.execute(
            "INSERT INTO events (run_id, kind, ticker, payload) VALUES (?,?,?,?)",
            (run_id, kind, data.get("ticker"), json.dumps(data, ensure_ascii=False, default=str)),
        )
        self.db.commit()

    def record_fill(self, run_id: int, f: FillRecord) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f.exec_id, run_id, f.ts, f.ticker, f.side, f.shares, f.price, f.commission,
             f.realized_pnl, f.ref_mid, f.modelled_cost),
        )
        self.db.commit()

    def record_snapshot(self, run_id: int, positions: list[PositionSnapshot]) -> None:
        self.db.executemany(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            [(run_id, p.ticker, p.shares, p.market_value, p.avg_cost) for p in positions],
        )
        self.db.commit()

    def last_snapshot(self) -> dict[str, float] | None:
        """上一次成功运行结束时的持仓股数；还没有成功运行过则返回 None。"""
        row = self.db.execute("SELECT MAX(id) FROM runs WHERE status IN ('ok', 'mismatch')").fetchone()
        if row[0] is None:
            return None
        rows = self.db.execute("SELECT ticker, shares FROM snapshots WHERE run_id=?", (row[0],)).fetchall()
        return {t: s for t, s in rows}
