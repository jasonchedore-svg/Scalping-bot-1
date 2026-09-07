from __future__ import annotations

from tests.helpers import bars_from_closes, momentum_long_setup

from scalping_bot.indicators import atr, ema, last_value, resample_minutes, rsi, session_vwap, sma


def test_sma_basic() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = sma(values, 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0
    assert out[3] == 3.0
    assert out[4] == 4.0


def test_ema_rises_with_prices() -> None:
    values = [float(i) for i in range(1, 40)]
    out = ema(values, 9)
    assert out[8] is not None
    assert last_value(out) is not None
    assert last_value(out) > out[8]  # type: ignore[operator]


def test_rsi_uptrend_is_high() -> None:
    values = [100.0 + i for i in range(30)]
    out = rsi(values, 14)
    assert last_value(out) is not None
    assert last_value(out) > 70  # type: ignore[operator]


def test_rsi_flat_near_50() -> None:
    values = [100.0] * 40
    out = rsi(values, 14)
    assert last_value(out) == 50.0


def test_atr_and_vwap_positive() -> None:
    bars = momentum_long_setup()
    atr_vals = atr(bars, 14)
    vwap_vals = session_vwap(bars)
    assert last_value(atr_vals) is not None
    assert last_value(atr_vals) > 0  # type: ignore[operator]
    assert last_value(vwap_vals) is not None


def test_resample_5m() -> None:
    closes = [100.0 + i * 0.1 for i in range(15)]
    bars = bars_from_closes("SPY", closes)
    out = resample_minutes(bars, 5)
    assert len(out) == 3
    assert out[0].open == bars[0].open
    assert out[0].close == bars[4].close
    assert out[0].volume == sum(b.volume for b in bars[:5])
