from __future__ import annotations

from datetime import timedelta

from scalping_bot.config import Settings
from scalping_bot.models import OpenTrade, SignalAction, infer_asset_class
from scalping_bot.strategy.base import MarketSnapshot
from scalping_bot.strategy.momentum_scalp import MomentumReclaimStrategy, MomentumScalpStrategy
from tests.helpers import choppy_setup, momentum_long_setup, pullback_reclaim_setup, session_start


def _settings() -> Settings:
    return Settings.model_validate(
        {
            **Settings().model_dump(),
            "use_5m_trend_filter": False,
        }
    )


def test_momentum_entry_on_setup() -> None:
    bars = momentum_long_setup("AAPL")
    snap = MarketSnapshot(symbol="AAPL", bars_1m=bars, now=bars[-1].timestamp)
    signal = MomentumScalpStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.BUY, signal.reason
    assert signal.stop_price is not None and signal.take_profit_price is not None
    assert signal.stop_price < bars[-1].close < signal.take_profit_price
    # Scalp clamps: stop at most 40 bps, target at most 60 bps.
    stop_pct = (bars[-1].close - signal.stop_price) / bars[-1].close
    tp_pct = (signal.take_profit_price - bars[-1].close) / bars[-1].close
    assert 0.0007 <= stop_pct <= 0.0041
    assert 0.0011 <= tp_pct <= 0.0061


def test_choppy_market_holds() -> None:
    bars = choppy_setup("MSFT")
    snap = MarketSnapshot(symbol="MSFT", bars_1m=bars, now=bars[-1].timestamp)
    signal = MomentumScalpStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.HOLD


def test_insufficient_bars_hold() -> None:
    bars = momentum_long_setup("AAPL")[:10]
    snap = MarketSnapshot(symbol="AAPL", bars_1m=bars, now=bars[-1].timestamp)
    signal = MomentumScalpStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.HOLD
    assert signal.reason == "insufficient_bars"


def test_stop_loss_exit() -> None:
    bars = momentum_long_setup("AAPL")
    last = bars[-1]
    trade = OpenTrade(
        symbol="AAPL",
        qty=10,
        entry_price=last.close + 1,
        entry_time=last.timestamp - timedelta(minutes=3),
        stop_price=last.close + 0.5,
        take_profit_price=last.close + 5,
        asset_class=infer_asset_class("AAPL"),
        high_water=last.close + 1,
    )
    snap = MarketSnapshot(
        symbol="AAPL", bars_1m=bars, now=last.timestamp, open_trade=trade
    )
    signal = MomentumScalpStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.SELL
    assert signal.reason == "stop_loss"


def test_take_profit_exit() -> None:
    bars = momentum_long_setup("AAPL")
    last = bars[-1]
    trade = OpenTrade(
        symbol="AAPL",
        qty=10,
        entry_price=last.close - 5,
        entry_time=last.timestamp - timedelta(minutes=3),
        stop_price=last.close - 6,
        take_profit_price=last.low,  # already traded through
        asset_class=infer_asset_class("AAPL"),
        high_water=last.high,
    )
    snap = MarketSnapshot(
        symbol="AAPL", bars_1m=bars, now=last.timestamp, open_trade=trade
    )
    signal = MomentumScalpStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.SELL
    assert signal.reason == "take_profit"


def test_time_stop_exit() -> None:
    bars = momentum_long_setup("AAPL")
    last = bars[-1]
    settings = _settings()
    trade = OpenTrade(
        symbol="AAPL",
        qty=10,
        entry_price=last.close,
        entry_time=last.timestamp - timedelta(minutes=settings.max_hold_minutes + 1),
        stop_price=last.close * 0.99,
        take_profit_price=last.close * 1.05,
        asset_class=infer_asset_class("AAPL"),
        high_water=last.close,
    )
    snap = MarketSnapshot(
        symbol="AAPL", bars_1m=bars, now=last.timestamp, open_trade=trade
    )
    signal = MomentumScalpStrategy(settings).evaluate(snap)
    assert signal.action is SignalAction.SELL
    assert signal.reason == "time_stop"


def test_breakeven_trail() -> None:
    start = session_start()
    trade = OpenTrade(
        symbol="AAPL",
        qty=10,
        entry_price=100.0,
        entry_time=start,
        stop_price=99.7,
        take_profit_price=100.5,
        asset_class=infer_asset_class("AAPL"),
        high_water=100.0,
    )
    strat = MomentumScalpStrategy(_settings())
    assert strat.breakeven_stop(trade, 100.2) == 99.7
    assert strat.breakeven_stop(trade, 100.4) == 100.0


def test_reclaim_holds_on_extension_chase() -> None:
    from scalping_bot.indicators import closes, ema, last_value
    from scalping_bot.models import Bar

    bars = momentum_long_setup("AAPL")
    fast = last_value(ema(closes(bars), 9))
    assert fast is not None
    prior = bars[-2]
    # Lift the prior bar fully above EMA so this is a 3-bar chase, not a tag.
    bars[-2] = Bar(
        symbol=prior.symbol,
        timestamp=prior.timestamp,
        open=max(prior.open, fast + 0.04),
        high=max(prior.high, fast + 0.08),
        low=fast + 0.03,
        close=max(prior.close, fast + 0.05),
        volume=prior.volume,
    )
    snap = MarketSnapshot(symbol="AAPL", bars_1m=bars, now=bars[-1].timestamp)
    signal = MomentumReclaimStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.HOLD
    assert signal.reason == "no_ema_pullback"


def test_reclaim_enters_on_pullback() -> None:
    bars = pullback_reclaim_setup("AAPL")
    snap = MarketSnapshot(symbol="AAPL", bars_1m=bars, now=bars[-1].timestamp)
    signal = MomentumReclaimStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.BUY, signal.reason
    assert signal.reason == "momentum_reclaim_entry"
    assert signal.stop_price is not None and signal.take_profit_price is not None
    assert signal.stop_price < bars[-1].close < signal.take_profit_price


def test_crypto_entry_requires_target_to_cover_fees() -> None:
    bars = momentum_long_setup("BTC/USD")
    snap = MarketSnapshot(symbol="BTC/USD", bars_1m=bars, now=bars[-1].timestamp)
    signal = MomentumScalpStrategy(_settings()).evaluate(snap)
    assert signal.action is SignalAction.HOLD
    assert signal.reason == "target_inside_round_trip_costs"
