from __future__ import annotations

import bisect
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from scalping_bot.broker.base import Broker
from scalping_bot.broker.ledger import SimulatedLedger
from scalping_bot.models import (
    Account,
    AssetClass,
    Bar,
    BrokerError,
    MarketClock,
    Order,
    OrderRequest,
    Position,
    Side,
    infer_asset_class,
    normalize_symbol,
)

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


class HistoricalReplayBroker(Broker):
    """Replays recorded OHLCV through the same paper ledger as the demo simulator."""

    name = "historical"
    paper = True

    def __init__(
        self,
        bars_by_symbol: dict[str, list[Bar]],
        cash: float = 100_000.0,
        *,
        interval_minutes: int = 1,
        start_index: int | None = None,
        min_bars: int = 80,
    ) -> None:
        self.interval_minutes = interval_minutes
        self._bars = {
            normalize_symbol(sym): sorted(bars, key=lambda b: b.timestamp)
            for sym, bars in bars_by_symbol.items()
            if bars
        }
        if not self._bars:
            raise ValueError("HistoricalReplayBroker needs at least one symbol with bars")
        self.symbols = list(self._bars)
        self._ts: dict[str, list[datetime]] = {
            sym: [bar.timestamp for bar in series] for sym, series in self._bars.items()
        }
        self.ledger = SimulatedLedger(cash)
        stamps: set[datetime] = set()
        for bars in self._bars.values():
            for bar in bars:
                stamps.add(bar.timestamp)
        self._timestamps = sorted(stamps)
        if len(self._timestamps) < 2:
            raise ValueError("Need at least two timestamps to replay")
        self._cursor = start_index if start_index is not None else self._warmup_index(min_bars)
        self._cursor = min(max(self._cursor, 0), len(self._timestamps) - 1)
        self._now = self._timestamps[self._cursor]
        self.equity_curve: list[tuple[datetime, float]] = [
            (self._now, self.ledger.get_account().equity)
        ]

    def _warmup_index(self, min_bars: int) -> int:
        for i, ts in enumerate(self._timestamps):
            ready = [
                bisect.bisect_right(self._ts[sym], ts) >= min_bars for sym in self.symbols
            ]
            if any(ready) and all(ready):
                return i
        for i, ts in enumerate(self._timestamps):
            if any(bisect.bisect_right(self._ts[sym], ts) >= min_bars for sym in self.symbols):
                return i
        return min(min_bars, len(self._timestamps) - 1)

    def remaining_steps(self) -> int:
        return max(len(self._timestamps) - 1 - self._cursor, 0)

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
        ts = self._now
        return MarketClock(
            timestamp=ts,
            is_open=self._session_open(ts),
            next_close=ts.replace(hour=16, minute=0, second=0, microsecond=0) if ts.tzinfo else ts,
        )

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        series = self._bars.get(symbol, [])
        stamps = self._ts.get(symbol, [])
        if not series:
            return []
        end = bisect.bisect_right(stamps, self._now)
        asof = series[:end]
        tf = timeframe.upper().replace(" ", "")
        if tf in {"5MIN", "5T", "5MINUTE"} and self.interval_minutes == 1:
            from scalping_bot.indicators import resample_minutes

            asof = resample_minutes(asof, 5)
        return asof[-limit:]

    def last_price(self, symbol: str) -> float:
        bars = self.get_bars(symbol, "1Min", 1)
        if not bars:
            raise BrokerError(f"no bars for {symbol}")
        return bars[-1].close

    def submit_order(self, request: OrderRequest) -> Order:
        raw = self.last_price(request.symbol)
        order = self.ledger.fill(request, raw)
        self._mark_to_market()
        return order

    def close_position(self, symbol: str, reason: str = "") -> Order | None:
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
        ts = now or self._now
        if self.asset_class(symbol) is AssetClass.CRYPTO:
            return True
        if not self._session_open(ts):
            return False
        bars = self.get_bars(symbol, "1Min", 1)
        if not bars:
            return False
        # Daily Yahoo bars sit at a session timestamp; allow weekend/holiday gaps.
        if self.interval_minutes >= 390:
            return ts - bars[-1].timestamp <= timedelta(days=5)
        max_age = timedelta(minutes=max(self.interval_minutes, 1) * 2)
        return ts - bars[-1].timestamp <= max_age

    def advance(self) -> None:
        if self._cursor < len(self._timestamps) - 1:
            self._cursor += 1
            self._now = self._timestamps[self._cursor]
            self._mark_to_market()
            self.equity_curve.append((self._now, self.ledger.get_account().equity))

    def _mark_to_market(self) -> None:
        def px(symbol: str) -> float:
            key = normalize_symbol(symbol)
            series = self._bars.get(key, [])
            stamps = self._ts.get(key, [])
            if not series:
                return 0.0
            end = bisect.bisect_right(stamps, self._now)
            if end <= 0:
                return 0.0
            return series[end - 1].close

        self.ledger.mark_to_market(px)

    def _session_open(self, ts: datetime) -> bool:
        local = ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)
        if local.weekday() >= 5:
            return False
        # Daily (and coarser) bars are not stamped inside 09:30–16:00.
        if self.interval_minutes >= 390:
            return True
        return self._is_rth(ts)

    @staticmethod
    def _is_rth(ts: datetime) -> bool:
        local = ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)
        if local.weekday() >= 5:
            return False
        return RTH_OPEN <= local.time() < RTH_CLOSE
