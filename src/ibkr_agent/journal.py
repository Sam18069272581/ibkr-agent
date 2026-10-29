"""决策日志：每次运行的打分、Claude 观点、风控结果、下单都落库，方便复盘。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    net_liquidation_usd REAL,
    high_water_mark_usd REAL
);
CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL,      -- score | view | order | approved | rejected
    ticker TEXT,
    payload TEXT NOT NULL
);
"""


class Journal:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def high_water_mark(self) -> float:
        row = self._db.execute("SELECT MAX(high_water_mark_usd) FROM runs").fetchone()
        return row[0] or 0.0

    def start_run(self, mode: str, dry_run: bool, net_liq: float, hwm: float) -> int:
        cur = self._db.execute(
            "INSERT INTO runs (ts, mode, dry_run, net_liquidation_usd, high_water_mark_usd) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), mode, int(dry_run), net_liq, hwm),
        )
        self._db.commit()
        return cur.lastrowid

    def log(self, run_id: int, kind: str, obj) -> None:
        data = asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
        self._db.execute(
            "INSERT INTO events (run_id, kind, ticker, payload) VALUES (?,?,?,?)",
            (run_id, kind, data.get("ticker"), json.dumps(data, ensure_ascii=False, default=str)),
        )
        self._db.commit()
