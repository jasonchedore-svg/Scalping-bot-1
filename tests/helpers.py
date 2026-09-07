from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scalping_bot.models import Bar

ET = ZoneInfo("America/New_York")


def session_start() -> datetime:
    return datetime(2024, 6, 13, 10, 15, tzinfo=ET)


def bars_from_closes(
    symbol: str,
    closes: list[float],
    *,
    start: datetime | None = None,
    volumes: list[float] | None = None,
    bullish: bool = True,
    step: timedelta | None = None,
    skip_weekends: bool = False,
) -> list[Bar]:
    start = start or session_start()
    step = step or timedelta(minutes=1)
    out: list[Bar] = []
    prev = closes[0]
    ts = start
    for i, close in enumerate(closes):
        open_px = prev if i else close * 0.999
        if bullish and close >= open_px:
            high = max(open_px, close) * 1.0003
            low = min(open_px, close) * 0.9997
        else:
            high = max(open_px, close) * 1.0003
            low = min(open_px, close) * 0.9997
            if not bullish and i == len(closes) - 1:
                open_px = close * 1.001
                high = open_px
                low = close * 0.999
        vol = 1_000_000.0
        if volumes is not None:
            vol = volumes[i]
        out.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=open_px,
                high=high,
                low=low,
                close=close,
                volume=vol,
            )
        )
        prev = close
        ts = ts + step
        if skip_weekends:
            while ts.weekday() >= 5:
                ts += timedelta(days=1)
    return out


def momentum_long_setup(symbol: str = "AAPL", n: int = 87) -> list[Bar]:
    """Reproducible 1-minute series that satisfies momentum_scalp entry rules."""
    rng = random.Random(7)
    px = 100.0
    seq: list[float] = []
    for _ in range(70):
        px *= 1 + rng.uniform(-0.0008, 0.0009)
        seq.append(px)
    for _ in range(17):
        px *= 1 + rng.uniform(-0.0006, 0.0009)
        seq.append(px)
    if not (seq[-1] > seq[-2] > seq[-3]):
        seq[-3] = seq[-4] * 1.0002
        seq[-2] = seq[-3] * 1.00025
        seq[-1] = seq[-2] * 1.0003
    volumes = [800_000.0] * len(seq)
    volumes[-1] = 2_500_000.0
    return bars_from_closes(symbol, seq, volumes=volumes)


def choppy_setup(symbol: str = "AAPL", n: int = 80) -> list[Bar]:
    closes: list[float] = []
    price = 100.0
    for i in range(n):
        price *= 1.0004 if i % 2 == 0 else 0.9996
        closes.append(price)
    return bars_from_closes(symbol, closes)


def pullback_reclaim_setup(symbol: str = "AAPL", n: int = 90) -> list[Bar]:
    """Uptrend, prior bar tags EMA(9), last bar reclaims it with volume."""
    from scalping_bot.indicators import closes, ema, last_value

    base = momentum_long_setup(symbol, n=max(n, 87))[:-2]
    fast = last_value(ema(closes(base), 9))
    assert fast is not None
    t0 = base[-1].timestamp + timedelta(minutes=1)
    dip = Bar(
        symbol=symbol,
        timestamp=t0,
        open=fast + 0.02,
        high=fast + 0.03,
        low=fast - 0.06,
        close=fast - 0.01,
        volume=900_000.0,
    )
    reclaim = Bar(
        symbol=symbol,
        timestamp=t0 + timedelta(minutes=1),
        open=fast - 0.01,
        high=fast + 0.08,
        low=fast - 0.02,
        close=fast + 0.05,
        volume=2_800_000.0,
    )
    return base + [dip, reclaim]


def trend_swing_entry_setup(symbol: str = "SPY", n: int = 230) -> list[Bar]:
    """Daily uptrend, SMA50 pullback, bullish reclaim. Satisfies trend_swing entry."""
    from scalping_bot.indicators import closes, last_value, sma

    start = datetime(2023, 1, 3, 15, 30, tzinfo=ET)
    px = 100.0
    seq: list[float] = []
    for i in range(max(n, 220)):
        px *= 0.9985 if i % 8 == 7 else 1.0022
        seq.append(px)
    # Drop last 10 placeholders; rebuild them against SMA50.
    body = seq[:-10]
    bars = bars_from_closes(
        symbol,
        body,
        start=start,
        step=timedelta(days=1),
        skip_weekends=True,
    )
    fast = last_value(sma(closes(bars), 50))
    assert fast is not None
    ts = bars[-1].timestamp
    extra: list[Bar] = []
    px = bars[-1].close
    # Nine pullback sessions that tag SMA50, then a bullish reclaim.
    for i in range(9):
        ts = ts + timedelta(days=1)
        while ts.weekday() >= 5:
            ts += timedelta(days=1)
        tag = fast * (0.997 if i < 8 else 0.999)
        close = fast * 1.001 if i < 8 else fast * 1.004
        open_px = max(tag, close * 0.999)
        extra.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=open_px,
                high=max(open_px, close) * 1.002,
                low=min(tag, close) * 0.999,
                close=close,
                volume=1_200_000.0,
            )
        )
        px = close
        fast = last_value(sma(closes(bars + extra), 50)) or fast
    ts = ts + timedelta(days=1)
    while ts.weekday() >= 5:
        ts += timedelta(days=1)
    fast = last_value(sma(closes(bars + extra), 50)) or fast
    close = fast * 1.006
    open_px = fast * 0.999
    extra.append(
        Bar(
            symbol=symbol,
            timestamp=ts,
            open=open_px,
            high=close * 1.004,
            low=min(open_px, fast * 0.998),
            close=close,
            volume=1_500_000.0,
        )
    )
    out = bars + extra
    return out

