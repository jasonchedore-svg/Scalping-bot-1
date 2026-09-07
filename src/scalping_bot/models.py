from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AssetClass(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(StrEnum):
    HOLD = "hold"
    BUY = "buy"
    SELL = "sell"
    FLATTEN = "flatten"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_STOP = "time_stop"
    MOMENTUM_FADE = "momentum_fade"
    SESSION_FLATTEN = "session_flatten"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"
    SIGNAL = "signal"


KNOWN_CRYPTO_ALIASES: dict[str, str] = {
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "SOLUSD": "SOL/USD",
    "DOGEUSD": "DOGE/USD",
    "LTCUSD": "LTC/USD",
    "AVAXUSD": "AVAX/USD",
    "LINKUSD": "LINK/USD",
    "UNIUSD": "UNI/USD",
    "AAVEUSD": "AAVE/USD",
    "BCHUSD": "BCH/USD",
}


def normalize_symbol(symbol: str) -> str:
    raw = symbol.strip().upper().replace("-", "/")
    if "/" in raw:
        base, quote = raw.split("/", 1)
        return f"{base}/{quote}"
    return KNOWN_CRYPTO_ALIASES.get(raw, raw)


def is_crypto_symbol(symbol: str) -> bool:
    return "/" in normalize_symbol(symbol)


def infer_asset_class(symbol: str) -> AssetClass:
    return AssetClass.CRYPTO if is_crypto_symbol(symbol) else AssetClass.STOCK


def round_qty(qty: float, asset_class: AssetClass, fractional_shares: bool = True) -> float:
    if qty <= 0:
        return 0.0
    if asset_class is AssetClass.CRYPTO:
        return float(int(qty * 1_000_000)) / 1_000_000
    if fractional_shares:
        return float(int(qty * 10_000)) / 10_000
    return float(int(qty))


def round_price(price: float, asset_class: AssetClass) -> float:
    if asset_class is AssetClass.CRYPTO:
        if price >= 1000:
            return round(price, 2)
        if price >= 1:
            return round(price, 4)
        return round(price, 6)
    if price < 1:
        return round(price, 4)
    return round(price, 2)


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    paper: bool
    pattern_day_trader: bool = False


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    asset_class: AssetClass
    side: Side = Side.BUY

    @property
    def notional(self) -> float:
        return abs(self.market_value)


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    qty: float
    side: Side
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    client_order_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    qty: float
    side: Side
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class Signal:
    action: SignalAction
    symbol: str
    reason: str
    confidence: float = 0.0
    entry_price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def hold(cls, symbol: str, reason: str = "no_setup") -> Signal:
        return cls(action=SignalAction.HOLD, symbol=symbol, reason=reason)


@dataclass
class OpenTrade:
    symbol: str
    qty: float
    entry_price: float
    entry_time: datetime
    stop_price: float
    take_profit_price: float
    asset_class: AssetClass
    high_water: float
    client_order_id: str | None = None

    def risk_per_share(self) -> float:
        return max(self.entry_price - self.stop_price, 0.0)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    qty: float = 0.0
    notional: float = 0.0
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    timestamp: datetime
    reason: str = ""
    realized_pl: float = 0.0


@dataclass(frozen=True)
class DailyStats:
    day: str
    starting_equity: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    trades: int
    wins: int
    losses: int
    kill_switch: bool

    @property
    def day_pnl(self) -> float:
        return self.equity - self.starting_equity

    @property
    def day_pnl_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return self.day_pnl / self.starting_equity


class LiveTradingDisabledError(RuntimeError):
    """Raised when live trading is requested without the hard safety gates."""


class BrokerError(RuntimeError):
    """Broker adapter failure (never includes secrets)."""


class RiskBreachError(RuntimeError):
    """Order refused because it would breach a risk limit."""
