from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from scalping_bot.broker.base import Broker
from scalping_bot.config import PAPER_BASE_URL, Settings
from scalping_bot.logging_setup import get_logger
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
    TimeInForce,
    infer_asset_class,
    normalize_symbol,
    round_price,
)

log = get_logger("broker.alpaca")


class AlpacaBroker(Broker):
    """Alpaca adapter covering US stocks and crypto with the same interface."""

    name = "alpaca"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paper = settings.paper
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:
            raise BrokerError("alpaca-py is required for the Alpaca broker") from exc

        paper = settings.paper
        key = settings.alpaca_api_key
        secret = settings.alpaca_secret_key
        url = settings.effective_base_url()
        self._trading = TradingClient(
            api_key=key,
            secret_key=secret,
            paper=paper,
            url_override=url if url else None,
        )
        self._stock_data = StockHistoricalDataClient(key, secret)
        self._crypto_data = CryptoHistoricalDataClient(key, secret)
        log.info(
            "alpaca_connected",
            paper=paper,
            base_url=url or PAPER_BASE_URL,
        )

    def get_account(self) -> Account:
        try:
            acct = self._trading.get_account()
        except Exception as exc:
            raise BrokerError(f"account fetch failed: {type(exc).__name__}") from exc
        return Account(
            equity=float(acct.equity or 0),
            cash=float(acct.cash or 0),
            buying_power=float(acct.buying_power or acct.cash or 0),
            paper=self.paper,
            pattern_day_trader=bool(getattr(acct, "pattern_day_trader", False)),
        )

    def get_positions(self) -> list[Position]:
        try:
            raw = self._trading.get_all_positions()
        except Exception as exc:
            raise BrokerError(f"positions fetch failed: {type(exc).__name__}") from exc
        return [_map_position(p) for p in raw]

    def get_position(self, symbol: str) -> Position | None:
        symbol = normalize_symbol(symbol)
        try:
            raw = self._trading.get_open_position(symbol)
        except Exception:
            return None
        return _map_position(raw)

    def get_clock(self) -> MarketClock:
        try:
            clock = self._trading.get_clock()
        except Exception as exc:
            raise BrokerError(f"clock fetch failed: {type(exc).__name__}") from exc
        ts = _as_dt(clock.timestamp)
        return MarketClock(
            timestamp=ts,
            is_open=bool(clock.is_open),
            next_open=_as_dt(getattr(clock, "next_open", None)),
            next_close=_as_dt(getattr(clock, "next_close", None)),
        )

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        asset = self.asset_class(symbol)
        tf = _parse_timeframe(timeframe)
        try:
            if asset is AssetClass.CRYPTO:
                from alpaca.data.requests import CryptoBarsRequest

                req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, limit=limit)
                barset = self._crypto_data.get_crypto_bars(req)
            else:
                from alpaca.data.enums import DataFeed
                from alpaca.data.requests import StockBarsRequest

                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    limit=limit,
                    feed=DataFeed.IEX,
                )
                barset = self._stock_data.get_stock_bars(req)
        except Exception as exc:
            raise BrokerError(f"bars fetch failed for {symbol}: {type(exc).__name__}") from exc
        return _bars_from_barset(barset, symbol)

    def last_price(self, symbol: str) -> float:
        bars = self.get_bars(symbol, "1Min", 1)
        if not bars:
            raise BrokerError(f"no bars for {symbol}")
        return bars[-1].close

    def submit_order(self, request: OrderRequest) -> Order:
        from alpaca.trading.enums import OrderClass, OrderSide
        from alpaca.trading.enums import TimeInForce as AlpacaTIF
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        symbol = normalize_symbol(request.symbol)
        asset = self.asset_class(symbol)
        tif = (
            AlpacaTIF.GTC
            if asset is AssetClass.CRYPTO or request.time_in_force is TimeInForce.GTC
            else AlpacaTIF.DAY
        )
        side = OrderSide.BUY if request.side is Side.BUY else OrderSide.SELL
        client_id = request.client_order_id or str(uuid4())
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "qty": request.qty,
            "side": side,
            "time_in_force": tif,
            "client_order_id": client_id,
        }
        use_bracket = (
            self.settings.use_broker_brackets
            and request.side is Side.BUY
            and request.stop_price is not None
            and request.take_profit_price is not None
        )
        if use_bracket:
            kwargs["order_class"] = OrderClass.BRACKET
            kwargs["take_profit"] = TakeProfitRequest(
                limit_price=round_price(request.take_profit_price, asset)
            )
            kwargs["stop_loss"] = StopLossRequest(
                stop_price=round_price(request.stop_price, asset)
            )
        try:
            order_req = MarketOrderRequest(**kwargs)
            raw = self._trading.submit_order(order_data=order_req)
        except Exception as exc:
            if use_bracket:
                log.warning(
                    "bracket_order_failed_retry_simple",
                    symbol=symbol,
                    error=type(exc).__name__,
                )
                kwargs.pop("order_class", None)
                kwargs.pop("take_profit", None)
                kwargs.pop("stop_loss", None)
                try:
                    order_req = MarketOrderRequest(**kwargs)
                    raw = self._trading.submit_order(order_data=order_req)
                except Exception as exc2:
                    raise BrokerError(
                        f"submit_order failed for {symbol}: {type(exc2).__name__}"
                    ) from exc2
            else:
                raise BrokerError(
                    f"submit_order failed for {symbol}: {type(exc).__name__}"
                ) from exc
        log.info(
            "order_submitted",
            symbol=symbol,
            side=request.side.value,
            qty=request.qty,
            reason=request.reason,
            paper=self.paper,
            order_id=str(getattr(raw, "id", "")),
        )
        return _map_order(raw)

    def close_position(self, symbol: str, reason: str = "") -> Order | None:
        symbol = normalize_symbol(symbol)
        try:
            raw = self._trading.close_position(symbol)
        except Exception as exc:
            log.warning(
                "close_position_failed",
                symbol=symbol,
                error=type(exc).__name__,
                reason=reason,
            )
            return None
        log.info("position_closed", symbol=symbol, reason=reason, paper=self.paper)
        if raw is None:
            return None
        try:
            return _map_order(raw)
        except Exception:
            return None

    def cancel_open_orders(self, symbol: str | None = None) -> None:
        try:
            if symbol:
                from alpaca.trading.enums import QueryOrderStatus
                from alpaca.trading.requests import GetOrdersRequest

                req = GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, symbols=[normalize_symbol(symbol)]
                )
                orders = self._trading.get_orders(filter=req)
                for order in orders:
                    self._trading.cancel_order_by_id(order.id)
            else:
                self._trading.cancel_orders()
        except Exception as exc:
            log.warning("cancel_orders_failed", error=type(exc).__name__)

    def asset_class(self, symbol: str) -> AssetClass:
        symbol = normalize_symbol(symbol)
        inferred = infer_asset_class(symbol)
        try:
            asset = self._trading.get_asset(symbol)
            cls = str(getattr(asset, "asset_class", "")).lower()
            if "crypto" in cls:
                return AssetClass.CRYPTO
            return AssetClass.STOCK
        except Exception:
            return inferred

    def is_tradable_now(self, symbol: str, now: datetime | None = None) -> bool:
        if self.asset_class(symbol) is AssetClass.CRYPTO:
            return True
        return self.get_clock().is_open


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


def _parse_timeframe(timeframe: str) -> Any:
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    normalized = timeframe.replace(" ", "").lower()
    mapping = {
        "1min": TimeFrame.Minute,
        "1m": TimeFrame.Minute,
        "1minute": TimeFrame.Minute,
        "5min": TimeFrame(5, TimeFrameUnit.Minute),
        "5m": TimeFrame(5, TimeFrameUnit.Minute),
        "5minute": TimeFrame(5, TimeFrameUnit.Minute),
    }
    if normalized not in mapping:
        raise BrokerError(f"unsupported timeframe {timeframe!r}")
    return mapping[normalized]


def _bars_from_barset(barset: Any, symbol: str) -> list[Bar]:
    rows: list[Any] = []
    data = getattr(barset, "data", None)
    if isinstance(data, dict) and symbol in data:
        rows = list(data[symbol])
    elif isinstance(barset, dict) and symbol in barset:
        rows = list(barset[symbol])
    else:
        try:
            rows = list(barset[symbol])
        except Exception:
            return []
    out: list[Bar] = []
    for raw in rows:
        ts = _as_dt(getattr(raw, "timestamp", None)) or datetime.now(UTC)
        vwap = getattr(raw, "vwap", None)
        out.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=float(raw.open),
                high=float(raw.high),
                low=float(raw.low),
                close=float(raw.close),
                volume=float(getattr(raw, "volume", 0) or 0),
                vwap=float(vwap) if vwap is not None else None,
            )
        )
    out.sort(key=lambda b: b.timestamp)
    return out


def _map_order(raw: Any) -> Order:
    side_raw = str(getattr(raw, "side", "buy")).lower()
    side = Side.BUY if "buy" in side_raw else Side.SELL
    filled_px = getattr(raw, "filled_avg_price", None)
    return Order(
        id=str(getattr(raw, "id", "")),
        symbol=normalize_symbol(str(getattr(raw, "symbol", ""))),
        qty=float(getattr(raw, "qty", 0) or 0),
        side=side,
        status=str(getattr(raw, "status", "")),
        filled_qty=float(getattr(raw, "filled_qty", 0) or 0),
        filled_avg_price=float(filled_px) if filled_px else None,
        client_order_id=getattr(raw, "client_order_id", None),
    )


def _map_position(raw: Any) -> Position:
    symbol = normalize_symbol(str(getattr(raw, "symbol", "")))
    qty = float(getattr(raw, "qty", 0) or 0)
    avg = float(getattr(raw, "avg_entry_price", 0) or 0)
    mv = float(getattr(raw, "market_value", 0) or 0)
    upl = float(getattr(raw, "unrealized_pl", 0) or 0)
    cls = str(getattr(raw, "asset_class", "")).lower()
    asset = AssetClass.CRYPTO if "crypto" in cls else infer_asset_class(symbol)
    side = Side.BUY if qty >= 0 else Side.SELL
    return Position(
        symbol=symbol,
        qty=abs(qty),
        avg_entry_price=avg,
        market_value=abs(mv),
        unrealized_pl=upl,
        asset_class=asset,
        side=side,
    )
