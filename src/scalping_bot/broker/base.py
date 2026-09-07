from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from scalping_bot.models import (
    Account,
    AssetClass,
    Bar,
    MarketClock,
    Order,
    OrderRequest,
    Position,
)


class Broker(ABC):
    """Single interface used by the engine for stocks and crypto."""

    name: str
    paper: bool

    @abstractmethod
    def get_account(self) -> Account:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    def get_position(self, symbol: str) -> Position | None:
        raise NotImplementedError

    @abstractmethod
    def get_clock(self) -> MarketClock:
        raise NotImplementedError

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        raise NotImplementedError

    @abstractmethod
    def last_price(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> Order:
        raise NotImplementedError

    @abstractmethod
    def close_position(self, symbol: str, reason: str = "") -> Order | None:
        raise NotImplementedError

    @abstractmethod
    def cancel_open_orders(self, symbol: str | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def asset_class(self, symbol: str) -> AssetClass:
        raise NotImplementedError

    def is_tradable_now(self, symbol: str, now: datetime | None = None) -> bool:
        clock = self.get_clock()
        if self.asset_class(symbol) is AssetClass.CRYPTO:
            return True
        return clock.is_open

    def advance(self) -> None:
        """Simulators may step synthetic time; live brokers no-op."""
        return None
