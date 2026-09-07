from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scalping_bot.broker.base import Broker
from scalping_bot.broker.ledger import SimulatedLedger
from scalping_bot.logging_setup import get_logger
from scalping_bot.models import (
    Account,
    AssetClass,
    Bar,
    MarketClock,
    Order,
    OrderRequest,
    Position,
    Side,
    infer_asset_class,
    normalize_symbol,
    round_price,
)

ET = ZoneInfo("America/New_York")

log = get_logger("broker.simulator")


class LocalPaperBroker(Broker):
    """In-process paper simulator used when Alpaca keys are absent."""

    name = "simulator"
    paper = True

    def __init__(
        self,
        symbols: list[str],
        cash: float = 100_000.0,
        seed: int = 42,
        now: datetime | None = None,
        stock_session_open: bool = True,
    ) -> None:
        self.symbols = [normalize_symbol(s) for s in symbols]
        self._rng = random.Random(seed)
        self._now = now or datetime(2024, 6, 13, 10, 0, tzinfo=ET)
        self.stock_session_open = stock_session_open
        self._bars: dict[str, list[Bar]] = {s: [] for s in self.symbols}
        self._start_prices = {s: _default_price(s) for s in self.symbols}
        self.ledger = SimulatedLedger(cash)
        self.slippage_bps = self.ledger.slippage_bps
        self.crypto_fee_bps = self.ledger.crypto_fee_bps
        self._seed_history(120)

    @property
    def cash(self) -> float:
        return self.ledger.cash

    @cash.setter
    def cash(self, value: float) -> None:
        self.ledger.cash = value

    @property
    def _positions(self) -> dict[str, Position]:
        return self.ledger.positions

    @property
    def _orders(self) -> list[Order]:
        return self.ledger.orders

    def _seed_history(self, n: int) -> None:
        for symbol in self.symbols:
            price = self._start_prices[symbol]
            ts = self._now - timedelta(minutes=n)
            bars: list[Bar] = []
            for i in range(n):
                bar, price = self._make_bar(symbol, ts, price, i)
                bars.append(bar)
                ts += timedelta(minutes=1)
            self._bars[symbol] = bars
            self._start_prices[symbol] = price

    def _make_bar(self, symbol: str, ts: datetime, price: float, i: int) -> tuple[Bar, float]:
        asset = infer_asset_class(symbol)
        vol = 0.0007 if asset is AssetClass.STOCK else 0.0012
        # Occasional momentum burst so the demo strategy can fire.
        burst = 1.0
        if 40 <= (i % 70) <= 52:
            burst = 1.8
            drift = 0.00045 if asset is AssetClass.STOCK else 0.0007
        else:
            drift = 0.00002
        shock = self._rng.gauss(drift * burst, vol)
        nxt = max(price * (1.0 + shock), 0.01)
        high = max(price, nxt) * (1.0 + abs(self._rng.gauss(0, vol / 3)))
        low = min(price, nxt) * (1.0 - abs(self._rng.gauss(0, vol / 3)))
        volume = abs(self._rng.gauss(1_000_000 if asset is AssetClass.STOCK else 50.0, 200_000))
        if burst > 1:
            volume *= 2.2
        bar = Bar(
            symbol=symbol,
            timestamp=ts,
            open=round_price(price, asset),
            high=round_price(high, asset),
            low=round_price(max(low, 0.01), asset),
            close=round_price(nxt, asset),
            volume=max(volume, 1.0),
        )
        return bar, nxt

    def get_account(self) -> Account:
        self._mark_to_market()
        return self.ledger.get_account()

    def get_positions(self) -> list[Position]:
        self._mark_to_market()
        return self.ledger.get_positions()

    def get_position(self, symbol: str) -> Position | None:
        self._mark_to_market()
        return self.ledger.get_position(symbol)

    def get_clock(self) -> MarketClock:
        return MarketClock(
            timestamp=self._now,
            is_open=self.stock_session_open,
            next_close=self._now.replace(hour=16, minute=0, second=0, microsecond=0),
        )

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        if symbol not in self._bars:
            self.symbols.append(symbol)
            self._start_prices[symbol] = _default_price(symbol)
            self._bars[symbol] = []
            self._seed_one(symbol, max(limit, 120))
        bars = self._bars[symbol]
        if timeframe.upper() in {"5MIN", "5T", "5MINUTE"}:
            from scalping_bot.indicators import resample_minutes

            bars = resample_minutes(bars, 5)
        return bars[-limit:]

    def last_price(self, symbol: str) -> float:
        bars = self.get_bars(symbol, "1Min", 1)
        if not bars:
            return _default_price(symbol)
        return bars[-1].close

    def submit_order(self, request: OrderRequest) -> Order:
        symbol = normalize_symbol(request.symbol)
        raw = self.last_price(symbol)
        order = self.ledger.fill(request, raw)
        log.info(
            "simulator_fill",
            symbol=symbol,
            side=request.side.value,
            qty=request.qty,
            price=order.filled_avg_price,
            reason=request.reason,
        )
        self._mark_to_market()
        return order

    def close_position(self, symbol: str, reason: str = "") -> Order | None:
        symbol = normalize_symbol(symbol)
        pos = self.get_position(symbol)
        if pos is None:
            return None
        return self.submit_order(
            OrderRequest(
                symbol=symbol,
                qty=pos.qty,
                side=Side.SELL,
                reason=reason or "close_position",
            )
        )

    def cancel_open_orders(self, symbol: str | None = None) -> None:
        return None

    def asset_class(self, symbol: str) -> AssetClass:
        return infer_asset_class(symbol)

    def is_tradable_now(self, symbol: str, now: datetime | None = None) -> bool:
        if self.asset_class(symbol) is AssetClass.CRYPTO:
            return True
        return self.stock_session_open

    def advance(self) -> None:
        self._now += timedelta(minutes=1)
        for symbol in list(self._bars):
            if self._bars[symbol]:
                price = self._bars[symbol][-1].close
            else:
                price = self._start_prices[symbol]
            bar, nxt = self._make_bar(symbol, self._now, price, len(self._bars[symbol]))
            self._bars[symbol].append(bar)
            self._start_prices[symbol] = nxt
        self._mark_to_market()

    def _mark_to_market(self) -> None:
        self.ledger.mark_to_market(self.last_price)

    def _seed_one(self, symbol: str, n: int) -> None:
        price = self._start_prices[symbol]
        ts = self._now - timedelta(minutes=n)
        bars: list[Bar] = []
        for i in range(n):
            bar, price = self._make_bar(symbol, ts, price, i)
            bars.append(bar)
            ts += timedelta(minutes=1)
        self._bars[symbol] = bars
        self._start_prices[symbol] = price


def _default_price(symbol: str) -> float:
    table = {
        "AAPL": 190.0,
        "MSFT": 420.0,
        "NVDA": 120.0,
        "SPY": 530.0,
        "QQQ": 460.0,
        "BTC/USD": 65_000.0,
        "ETH/USD": 3_400.0,
    }
    if symbol in table:
        return table[symbol]
    # Stable dummy price from symbol hash so tests are deterministic-ish.
    acc = sum(ord(c) for c in symbol)
    return 50.0 + (acc % 200)
