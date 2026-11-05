"""交易所接入。只暴露系统需要的几个动作，方便测试时替换。

- CcxtExchange：真实交易所（默认 OKX），sandbox=True 时连 OKX 模拟盘。
- LocalPaperExchange：只读公开行情，余额和成交都在本地 JSON 文件里模拟，不需要 API key。

API key 从环境变量读：OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE。
请在 OKX 创建只有“读取 + 交易”权限的 key，不要开提币权限，并绑定 IP 白名单。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Book:
    """一次盘口快照。ts 是拿到它的本地时间（秒），用来判断报价是否过期。"""

    symbol: str
    bid: float
    ask: float
    ts: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000


@dataclass(frozen=True)
class OrderStatus:
    id: str
    status: str          # open | closed | canceled
    filled: float        # 已成交数量（币）
    average: float | None
    fee: float           # 计价货币


class Exchange(Protocol):
    def balances(self) -> dict[str, float]: ...
    def book(self, symbol: str) -> Book: ...
    def recent_range(self, symbol: str, minutes: int) -> tuple[float, float, float]: ...
    def amount_to_precision(self, symbol: str, amount: float) -> float: ...
    def min_amount(self, symbol: str) -> float: ...
    def place_post_only(self, symbol: str, side: str, amount: float, price: float) -> str: ...
    def order(self, symbol: str, order_id: str) -> OrderStatus: ...
    def cancel(self, symbol: str, order_id: str) -> None: ...


def _ccxt(name: str, sandbox: bool, with_keys: bool, hostname: str = ""):
    import ccxt  # 延迟导入：只跑美股时不需要装 ccxt

    params: dict = {"enableRateLimit": True}
    if hostname:  # 例如欧洲站的 API 域名和全球站不同时，在 settings.yaml 里设置 crypto.hostname
        params["hostname"] = hostname
    if with_keys:
        prefix = name.upper()
        missing = [k for k in ("API_KEY", "API_SECRET", "API_PASSPHRASE") if not os.environ.get(f"{prefix}_{k}")]
        if missing:
            raise RuntimeError(f"缺少环境变量：{', '.join(prefix + '_' + m for m in missing)}（写在 .env 里）")
        params.update(
            apiKey=os.environ[f"{prefix}_API_KEY"],
            secret=os.environ[f"{prefix}_API_SECRET"],
            password=os.environ[f"{prefix}_API_PASSPHRASE"],
        )
    ex = getattr(ccxt, name)(params)
    if sandbox:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


class CcxtExchange:
    def __init__(self, name: str = "okx", sandbox: bool = True, client=None, hostname: str = ""):
        self.ex = client or _ccxt(name, sandbox, with_keys=True, hostname=hostname)

    def balances(self) -> dict[str, float]:
        bal = self.ex.fetch_balance()
        return {k: float(v) for k, v in (bal.get("total") or {}).items() if v}

    def book(self, symbol: str) -> Book:
        ob = self.ex.fetch_order_book(symbol, limit=5)
        if not ob["bids"] or not ob["asks"]:
            raise RuntimeError(f"{symbol} 盘口为空")
        return Book(symbol, float(ob["bids"][0][0]), float(ob["asks"][0][0]), time.time())

    def recent_range(self, symbol: str, minutes: int) -> tuple[float, float, float]:
        """最近 minutes 分钟的（开盘价, 最低价, 最高价），用 1 分钟 K 线。"""
        candles = self.ex.fetch_ohlcv(symbol, timeframe="1m", limit=max(2, minutes))
        if not candles:
            raise RuntimeError(f"{symbol} 没有 K 线")
        return float(candles[0][1]), min(float(c[3]) for c in candles), max(float(c[2]) for c in candles)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.ex.amount_to_precision(symbol, amount))

    def min_amount(self, symbol: str) -> float:
        return float(((self.ex.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0.0)

    def place_post_only(self, symbol: str, side: str, amount: float, price: float) -> str:
        price = float(self.ex.price_to_precision(symbol, price))
        o = self.ex.create_order(symbol, "limit", side.lower(), amount, price, {"postOnly": True})
        return str(o["id"])

    def order(self, symbol: str, order_id: str) -> OrderStatus:
        o = self.ex.fetch_order(order_id, symbol)
        fee = sum(float(f.get("cost") or 0) for f in (o.get("fees") or [])) or float((o.get("fee") or {}).get("cost") or 0)
        status = {"open": "open", "closed": "closed"}.get(o.get("status"), "canceled")
        return OrderStatus(str(o["id"]), status, float(o.get("filled") or 0), o.get("average"), abs(fee))

    def cancel(self, symbol: str, order_id: str) -> None:
        self.ex.cancel_order(order_id, symbol)


class LocalPaperExchange:
    """本地模拟账户。行情来自交易所公开接口（或注入的 public 对象），成交按挂单价立即成交、收 maker 费。

    这比真实情况乐观（真实 post-only 单可能等不到成交），所以只用来跑通流程，
    转实盘前要用 OKX 模拟盘再跑一轮。
    """

    def __init__(self, state_path: str, start_cash: float, quote: str, maker_fee: float,
                 public=None, exchange_name: str = "okx", hostname: str = ""):
        self.path = Path(state_path)
        self.quote = quote
        self.maker_fee = maker_fee
        self.public = public or CcxtExchange(client=_ccxt(exchange_name, sandbox=False, with_keys=False, hostname=hostname))
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"balances": {quote: start_cash}, "orders": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def balances(self) -> dict[str, float]:
        return {k: v for k, v in self.state["balances"].items() if v}

    def book(self, symbol: str) -> Book:
        return self.public.book(symbol)

    def recent_range(self, symbol: str, minutes: int) -> tuple[float, float, float]:
        return self.public.recent_range(symbol, minutes)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return self.public.amount_to_precision(symbol, amount)

    def min_amount(self, symbol: str) -> float:
        return self.public.min_amount(symbol)

    def place_post_only(self, symbol: str, side: str, amount: float, price: float) -> str:
        base, quote = symbol.split("/")
        b = self.state["balances"]
        value = amount * price
        fee = value * self.maker_fee
        if side == "BUY":
            if b.get(quote, 0.0) < value + fee:
                raise RuntimeError("模拟账户现金不足")
            b[quote] = b.get(quote, 0.0) - value - fee
            b[base] = b.get(base, 0.0) + amount
        else:
            if b.get(base, 0.0) < amount - 1e-12:
                raise RuntimeError("模拟账户持币不足")
            b[base] = b.get(base, 0.0) - amount
            b[quote] = b.get(quote, 0.0) + value - fee
        oid = "paper-" + uuid.uuid4().hex[:12]
        self.state["orders"][oid] = {"symbol": symbol, "filled": amount, "average": price, "fee": fee}
        self._save()
        return oid

    def order(self, symbol: str, order_id: str) -> OrderStatus:
        o = self.state["orders"][order_id]
        return OrderStatus(order_id, "closed", o["filled"], o["average"], o["fee"])

    def cancel(self, symbol: str, order_id: str) -> None:
        return None
