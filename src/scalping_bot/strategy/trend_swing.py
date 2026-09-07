from __future__ import annotations

from datetime import datetime, timedelta

from scalping_bot.config import Settings
from scalping_bot.indicators import atr, closes, last_value, rsi, sma
from scalping_bot.models import OpenTrade, Signal, SignalAction, infer_asset_class, round_price
from scalping_bot.strategy.base import MarketSnapshot, Strategy, register_strategy


def _trading_days_between(start: datetime, end: datetime) -> int:
    if end < start:
        return 0
    d = start.date()
    last = end.date()
    n = 0
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


@register_strategy
class TrendSwingStrategy(Strategy):
    """Long-only swing on daily bars. Hold days to ~2–3 weeks.

    Entry (all must be true):
      1. Enough history for SMA(50), SMA(200), ATR(14).
      2. Uptrend: close > SMA50 and SMA50 > SMA200.
      3. Pullback: in the last N bars a low tagged SMA50 (within 0.3%).
      4. Reclaim: last bar bullish and close back above SMA50.
      5. RSI(14) < 70 (not a climax).
      6. ATR > 0; stop/target from ATR clamps.
      7. Flat in the symbol.

    Exits (first match):
      - Stop / take-profit
      - Time stop: max_hold_days trading days (default 15)
      - Trend break: after min hold, close < SMA50
    """

    name = "trend_swing"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.open_trade is not None or (snapshot.position and snapshot.position.qty > 0):
            return self._exit_signal(snapshot)
        return self._entry_signal(snapshot)

    def _entry_signal(self, snapshot: MarketSnapshot) -> Signal:
        s = self.settings
        bars = snapshot.bars_1m
        need = max(s.sma_slow, s.atr_period, s.rsi_period + 1) + 2
        if len(bars) < need:
            return Signal.hold(snapshot.symbol, "insufficient_bars")

        price_closes = closes(bars)
        sma_fast = last_value(sma(price_closes, s.sma_fast))
        sma_slow = last_value(sma(price_closes, s.sma_slow))
        atr_now = last_value(atr(bars, s.atr_period))
        rsi_now = last_value(rsi(price_closes, s.rsi_period))
        last = bars[-1]
        asset = infer_asset_class(snapshot.symbol)

        if None in (sma_fast, sma_slow, atr_now):
            return Signal.hold(snapshot.symbol, "indicators_warming_up")
        assert sma_fast is not None and sma_slow is not None and atr_now is not None

        if last.close <= sma_fast:
            return Signal.hold(snapshot.symbol, "below_sma_fast")
        if sma_fast <= sma_slow:
            return Signal.hold(snapshot.symbol, "sma_stack_not_bullish")
        if not last.bullish:
            return Signal.hold(snapshot.symbol, "last_bar_not_bullish")
        look = bars[-max(s.swing_pullback_bars, 3) : -1]
        tagged = any(bar.low <= sma_fast * 1.003 for bar in look)
        if not tagged:
            return Signal.hold(snapshot.symbol, "no_sma_pullback")
        if rsi_now is not None and rsi_now >= 70:
            return Signal.hold(snapshot.symbol, f"rsi_extended:{rsi_now:.1f}")
        if atr_now <= 0:
            return Signal.hold(snapshot.symbol, "atr_zero")

        stop_dist, tp_dist = self._distances(last.close, atr_now)
        stop = round_price(last.close - stop_dist, asset)
        take = round_price(last.close + tp_dist, asset)
        if stop >= last.close or take <= last.close:
            return Signal.hold(snapshot.symbol, "invalid_stop_or_target")

        return Signal(
            action=SignalAction.BUY,
            symbol=snapshot.symbol,
            reason="trend_swing_entry",
            confidence=min(1.0, (sma_fast / sma_slow - 1.0) * 20),
            entry_price=last.close,
            stop_price=stop,
            take_profit_price=take,
            metadata={
                "sma_fast": sma_fast,
                "sma_slow": sma_slow,
                "atr": atr_now,
                "rsi": rsi_now,
            },
        )

    def _exit_signal(self, snapshot: MarketSnapshot) -> Signal:
        s = self.settings
        bars = snapshot.bars_1m
        last = bars[-1]
        trade = snapshot.open_trade
        if trade is None:
            return Signal.hold(snapshot.symbol, "in_untracked_position")

        if last.low <= trade.stop_price or last.close <= trade.stop_price:
            return Signal(
                action=SignalAction.SELL,
                symbol=snapshot.symbol,
                reason="stop_loss",
                entry_price=last.close,
                stop_price=trade.stop_price,
                take_profit_price=trade.take_profit_price,
            )
        if last.high >= trade.take_profit_price or last.close >= trade.take_profit_price:
            return Signal(
                action=SignalAction.SELL,
                symbol=snapshot.symbol,
                reason="take_profit",
                entry_price=last.close,
                stop_price=trade.stop_price,
                take_profit_price=trade.take_profit_price,
            )

        held_days = _trading_days_between(trade.entry_time, snapshot.now)
        max_days = s.max_hold_days or 15
        if held_days >= max_days:
            return Signal(
                action=SignalAction.SELL,
                symbol=snapshot.symbol,
                reason="time_stop",
                entry_price=last.close,
            )

        if held_days >= s.min_hold_before_trend_exit_days:
            price_closes = closes(bars)
            sma_fast = last_value(sma(price_closes, s.sma_fast))
            if sma_fast is not None and last.close < sma_fast:
                return Signal(
                    action=SignalAction.SELL,
                    symbol=snapshot.symbol,
                    reason="close_below_sma_fast",
                    entry_price=last.close,
                )
        return Signal.hold(snapshot.symbol, "manage_position")

    def _distances(self, price: float, atr_now: float) -> tuple[float, float]:
        s = self.settings
        stop = atr_now * s.stop_atr_mult
        take = atr_now * s.take_profit_atr_mult
        stop = min(max(stop, price * s.min_stop_pct), price * s.max_stop_pct)
        take = min(max(take, price * s.min_take_profit_pct), price * s.max_take_profit_pct)
        if take <= stop:
            take = stop * (s.take_profit_atr_mult / s.stop_atr_mult)
        return stop, take

    def trail_stop(self, trade: OpenTrade, last_high: float) -> float:
        """After +1R trail to breakeven; after +2R lock in +1R."""
        r = trade.risk_per_share()
        if r <= 0:
            return trade.stop_price
        if last_high >= trade.entry_price + 2 * r:
            return max(trade.stop_price, trade.entry_price + r)
        if last_high >= trade.entry_price + r:
            return max(trade.stop_price, trade.entry_price)
        return trade.stop_price
