"""持仓批次（FIFO）和德国一年持有期。

每笔买入记一个批次；卖出按先进先出消耗批次，并记一笔处置记录（是否短期、收益多少）。
批次按账户分开（例如 okx-demo、okx-live、local-paper），模拟和实盘互不影响。

税务模型是简化的，只用来决定“现在卖还是等满一年再卖”，不是报税依据。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import CryptoTax

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_lots (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    ts TEXT NOT NULL,            -- 买入时间（UTC ISO）
    qty REAL NOT NULL,           -- 剩余数量
    unit_cost REAL NOT NULL,     -- 每个币的成本（含手续费，计价货币）
    source TEXT NOT NULL         -- fill | unknown（交易所上有、但不是本系统买的）
);
CREATE TABLE IF NOT EXISTS crypto_disposals (
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    ts TEXT NOT NULL,
    qty REAL NOT NULL,
    proceeds REAL NOT NULL,
    cost REAL NOT NULL,
    short_term INTEGER NOT NULL, -- 1 = 持有不满一年
    source TEXT NOT NULL         -- fill | external
);
"""

EPS = 1e-12


@dataclass(frozen=True)
class Lot:
    id: int
    asset: str
    ts: datetime
    qty: float
    unit_cost: float


def marginal_tax(ytd_short_gain: float, new_short_gain: float, cfg: CryptoTax) -> float:
    """这次卖出额外要交的税。Freigrenze：全年短期收益不超过阈值则全免，超过则全额计税。"""
    def due(total: float) -> float:
        return total * cfg.est_tax_rate if total > cfg.annual_threshold else 0.0
    return max(0.0, due(ytd_short_gain + new_short_gain) - due(ytd_short_gain))


class LotBook:
    def __init__(self, db: sqlite3.Connection, account: str, tax: CryptoTax):
        self.db = db
        self.account = account
        self.tax = tax
        db.executescript(SCHEMA)

    def lots(self, asset: str) -> list[Lot]:
        rows = self.db.execute(
            "SELECT id, asset, ts, qty, unit_cost FROM crypto_lots WHERE account=? AND asset=? AND qty > ? ORDER BY ts, id",
            (self.account, asset, EPS),
        ).fetchall()
        return [Lot(r[0], r[1], datetime.fromisoformat(r[2]), r[3], r[4]) for r in rows]

    def held(self, asset: str) -> float:
        return sum(l.qty for l in self.lots(asset))

    def add(self, asset: str, ts: datetime, qty: float, unit_cost: float, source: str = "fill") -> None:
        self.db.execute(
            "INSERT INTO crypto_lots (account, asset, ts, qty, unit_cost, source) VALUES (?,?,?,?,?,?)",
            (self.account, asset, ts.isoformat(), qty, unit_cost, source),
        )
        self.db.commit()

    def _is_short(self, lot: Lot, now: datetime) -> bool:
        return now - lot.ts < timedelta(days=self.tax.tax_free_after_days)

    def dispose(self, asset: str, qty: float, price: float, now: datetime, source: str = "fill") -> float:
        """按 FIFO 卖出 qty，返回短期收益（可为负）。数量超过批次合计时，超出部分按零成本、短期处理。"""
        short_gain = 0.0
        left = qty
        for lot in self.lots(asset):
            if left <= EPS:
                break
            take = min(lot.qty, left)
            short = self._is_short(lot, now)
            gain = take * (price - lot.unit_cost)
            if short:
                short_gain += gain
            self.db.execute("UPDATE crypto_lots SET qty = qty - ? WHERE id=?", (take, lot.id))
            self.db.execute("INSERT INTO crypto_disposals VALUES (?,?,?,?,?,?,?,?)",
                            (self.account, asset, now.isoformat(), take, take * price, take * lot.unit_cost, int(short), source))
            left -= take
        if left > EPS:
            short_gain += left * price
            self.db.execute("INSERT INTO crypto_disposals VALUES (?,?,?,?,?,?,?,?)",
                            (self.account, asset, now.isoformat(), left, left * price, 0.0, 1, source))
        self.db.commit()
        return short_gain

    def ytd_short_gain(self, now: datetime) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(proceeds - cost), 0) FROM crypto_disposals WHERE account=? AND short_term=1 AND substr(ts,1,4)=?",
            (self.account, f"{now.year:04d}"),
        ).fetchone()
        return float(row[0])

    def tax_free_qty(self, asset: str, price: float, now: datetime) -> float:
        """按 FIFO 顺序，从头开始卖、不产生额外税负时最多能卖多少。"""
        ytd = self.ytd_short_gain(now)
        acc_gain = 0.0
        qty = 0.0
        for lot in self.lots(asset):
            if not self._is_short(lot, now):
                qty += lot.qty
                continue
            gain = lot.qty * (price - lot.unit_cost)
            if marginal_tax(ytd, acc_gain + gain, self.tax) <= 0:
                acc_gain += gain
                qty += lot.qty
                continue
            # 这个批次只能卖一部分：找出不越过 Freigrenze 的最大数量（只有盈利时才会到这里）
            per_unit = price - lot.unit_cost
            if per_unit > 0:
                room = self.tax.annual_threshold - ytd - acc_gain
                qty += max(0.0, min(lot.qty, room / per_unit))
            break
        return qty

    def next_tax_free_date(self, asset: str, now: datetime) -> datetime | None:
        for lot in self.lots(asset):
            if self._is_short(lot, now):
                return lot.ts + timedelta(days=self.tax.tax_free_after_days)
        return None

    def sync(self, asset: str, exchange_qty: float, price: float, now: datetime) -> str | None:
        """让批次合计和交易所余额一致。返回一条说明；一致时返回 None。"""
        held = self.held(asset)
        diff = exchange_qty - held
        if abs(diff) <= max(EPS, 1e-9 * max(exchange_qty, held)):
            return None
        if diff > 0:
            # 不知道什么时候买的：按“现在、按现价”记，持有期从现在算（对税务是最保守的假设）
            self.add(asset, now, diff, price, source="unknown")
            return f"{asset} 交易所比记录多 {diff:.8f}，按现在、现价补记一个批次"
        self.dispose(asset, -diff, price, now, source="external")
        return f"{asset} 交易所比记录少 {-diff:.8f}，按 FIFO 记为在系统外卖出"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
