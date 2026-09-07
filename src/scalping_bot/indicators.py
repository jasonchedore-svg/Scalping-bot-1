from __future__ import annotations

from datetime import datetime, timedelta

from scalping_bot.models import Bar


def closes(bars: list[Bar]) -> list[float]:
    return [bar.close for bar in bars]


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = 0.0
    for i, value in enumerate(values):
        window += value
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or not values:
        return out
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period + 1:
        return out
    gains = [0.0] * len(values)
    losses = [0.0] * len(values)
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_from_avg(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_avg(avg_gain, avg_loss)
    return out


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    relative = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative))


def true_ranges(bars: list[Bar]) -> list[float]:
    trs: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        high_low = bar.high - bar.low
        if prev_close is None:
            trs.append(max(high_low, 0.0))
        else:
            trs.append(
                max(
                    high_low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )
        prev_close = bar.close
    return trs


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    trs = true_ranges(bars)
    out: list[float | None] = [None] * len(bars)
    if period <= 0 or len(trs) < period:
        return out
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def session_vwap(bars: list[Bar]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    cum_pv = 0.0
    cum_vol = 0.0
    last_session: datetime | None = None
    for i, bar in enumerate(bars):
        session_key = _session_bucket(bar)
        if last_session is not None and session_key != last_session:
            cum_pv = 0.0
            cum_vol = 0.0
        last_session = session_key
        vol = max(bar.volume, 0.0)
        cum_pv += bar.typical_price * vol
        cum_vol += vol
        if cum_vol > 0:
            out[i] = cum_pv / cum_vol
        else:
            out[i] = bar.typical_price
    return out


def _session_bucket(bar: Bar) -> datetime:
    ts = bar.timestamp
    if "/" in bar.symbol:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def last_value(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def resample_minutes(bars: list[Bar], minutes: int) -> list[Bar]:
    if minutes <= 1 or not bars:
        return list(bars)
    grouped: dict[datetime, list[Bar]] = {}
    order: list[datetime] = []
    for bar in bars:
        ts = bar.timestamp.replace(second=0, microsecond=0)
        floored = ts - timedelta(minutes=ts.minute % minutes)
        if floored not in grouped:
            grouped[floored] = []
            order.append(floored)
        grouped[floored].append(bar)
    out: list[Bar] = []
    for key in order:
        chunk = grouped[key]
        out.append(
            Bar(
                symbol=chunk[0].symbol,
                timestamp=key,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=sum(b.volume for b in chunk),
            )
        )
    return out
