from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from scalping_bot.models import Bar, OpenTrade, Position, Signal


@dataclass
class MarketSnapshot:
    symbol: str
    bars_1m: list[Bar]
    now: datetime
    position: Position | None = None
    open_trade: OpenTrade | None = None
    bars_5m: list[Bar] = field(default_factory=list)


class Strategy(ABC):
    """Pluggable entry/exit logic. Implementations must be side-effect free."""

    name: str

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        """Return a BUY/SELL/HOLD/FLATTEN signal for this symbol."""


STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    STRATEGY_REGISTRY[cls.name] = cls
    return cls


def load_strategy(name: str, **kwargs: object) -> Strategy:
    try:
        strategy_cls = STRATEGY_REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(STRATEGY_REGISTRY)) or "(none registered)"
        raise ValueError(f"Unknown strategy {name!r}. Available: {known}") from exc
    return strategy_cls(**kwargs)  # type: ignore[arg-type]
