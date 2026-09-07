from __future__ import annotations

from scalping_bot.config import Settings
from scalping_bot.indicators import (
    atr,
    closes,
    ema,
    last_value,
    resample_minutes,
    rsi,
    session_vwap,
    sma,
)
from scalping_bot.models import OpenTrade, Signal, SignalAction, infer_asset_class, round_price
from scalping_bot.strategy.base import MarketSnapshot, Strategy, register_strategy


@register_strategy
class MomentumScalpStrategy(Strategy):
    """Short-hold long-only momentum scalp on 1-minute bars.

    Exact entry rules (ALL must be true):
      1. Enough bars to compute EMA(slow), RSI, ATR, volume SMA.
      2. Fast EMA(9) > slow EMA(21) on 1-minute closes.
      3. Last close > session/rolling VWAP (buyer control).
      4. RSI(14) between 45 and 70 (momentum, not exhausted).
      5. Last 1-minute bar is bullish (close > open).
      6. Last three 1-minute closes are rising (micro-momentum).
      7. Last bar volume > 1.15 × 20-bar volume SMA.
      8. ATR is positive and usable for stops.
      9. Optional 5-minute filter: EMA(9) > EMA(21) on resampled 5-minute bars.
     10. Not already in a position (entries only when flat).

    Exact exit rules (first match wins):
      - Stop: last low/close <= stop_price (stop = entry − clamped ATR stop).
      - Take profit: last high/close >= take_profit_price (entry + clamped ATR target).
      - Time stop: held >= max_hold_minutes (default 20).
      - Momentum fade: held >= 5 minutes AND (RSI > 75 OR close < EMA9).
      - Engine also flattens stocks at flatten_et and on the daily loss kill-switch.

    Stop / take-profit (scalp clamps):
      raw_stop = ATR(14) × stop_atr_mult (default 1.0)
      stop_distance = clamp(raw_stop, min_stop_pct, max_stop_pct) of price
      raw_tp = ATR(14) × take_profit_atr_mult (default 1.5)
      tp_distance = clamp(raw_tp, min_take_profit_pct, max_take_profit_pct) of price

    Default clamps keep stops roughly 8–40 bps and targets 12–60 bps — suitable
    for minutes-long scalps, not swing trades. Long-only; no short, margin, or options.
    """

    name = "momentum_scalp"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.open_trade is not None or (snapshot.position and snapshot.position.qty > 0):
            return self._exit_signal(snapshot)
        return self._entry_signal(snapshot)

    def _entry_signal(self, snapshot: MarketSnapshot) -> Signal:
        s = self.settings
        bars = snapshot.bars_1m
        need = max(s.ema_slow, s.rsi_period + 1, s.atr_period, s.volume_sma_period) + 2
        if len(bars) < need:
            return Signal.hold(snapshot.symbol, "insufficient_bars")

        price_closes = closes(bars)
        fast = last_value(ema(price_closes, s.ema_fast))
        slow = last_value(ema(price_closes, s.ema_slow))
        vwap = last_value(session_vwap(bars))
        rsi_now = last_value(rsi(price_closes, s.rsi_period))
        atr_now = last_value(atr(bars, s.atr_period))
        volumes = [b.volume for b in bars]
        vol_sma = last_value(sma(volumes, s.volume_sma_period))
        last = bars[-1]
        asset = infer_asset_class(snapshot.symbol)

        if None in (fast, slow, vwap, rsi_now, atr_now, vol_sma):
            return Signal.hold(snapshot.symbol, "indicators_warming_up")
        assert fast is not None and slow is not None
        assert vwap is not None and rsi_now is not None
        assert atr_now is not None and vol_sma is not None

        if fast <= slow:
            return Signal.hold(snapshot.symbol, "ema_not_bullish")
        if last.close <= vwap:
            return Signal.hold(snapshot.symbol, "below_vwap")
        if not (s.rsi_entry_low < rsi_now < s.rsi_entry_high):
            return Signal.hold(snapshot.symbol, f"rsi_out_of_range:{rsi_now:.1f}")
        if not last.bullish:
            return Signal.hold(snapshot.symbol, "last_bar_not_bullish")
        if not (bars[-1].close > bars[-2].close > bars[-3].close):
            return Signal.hold(snapshot.symbol, "no_micro_momentum")
        if vol_sma <= 0 or last.volume < s.volume_spike * vol_sma:
            return Signal.hold(snapshot.symbol, "no_volume_spike")
        if atr_now <= 0:
            return Signal.hold(snapshot.symbol, "atr_zero")

        if s.use_5m_trend_filter:
            bars_5m = snapshot.bars_5m or resample_minutes(bars, 5)
            if len(bars_5m) >= s.ema_slow:
                c5 = closes(bars_5m)
                f5 = last_value(ema(c5, s.ema_fast))
                s5 = last_value(ema(c5, s.ema_slow))
                if f5 is not None and s5 is not None and f5 <= s5:
                    return Signal.hold(snapshot.symbol, "5m_trend_not_bullish")

        stop_dist, tp_dist = self._distances(last.close, atr_now)
        stop = round_price(last.close - stop_dist, asset)
        take = round_price(last.close + tp_dist, asset)
        if stop >= last.close or take <= last.close:
            return Signal.hold(snapshot.symbol, "invalid_stop_or_target")

        return Signal(
            action=SignalAction.BUY,
            symbol=snapshot.symbol,
            reason="momentum_scalp_entry",
            confidence=min(1.0, (fast / slow - 1.0) * 50 + (last.volume / vol_sma - 1.0)),
            entry_price=last.close,
            stop_price=stop,
            take_profit_price=take,
            metadata={
                "ema_fast": fast,
                "ema_slow": slow,
                "vwap": vwap,
                "rsi": rsi_now,
                "atr": atr_now,
                "volume_sma": vol_sma,
            },
        )

    def _exit_signal(self, snapshot: MarketSnapshot) -> Signal:
        s = self.settings
        bars = snapshot.bars_1m
        last = bars[-1]
        trade = snapshot.open_trade
        if trade is None and snapshot.position:
            # Fallback if state was lost: exit on any bearish break of EMA9.
            price_closes = closes(bars)
            fast = last_value(ema(price_closes, s.ema_fast))
            if fast is not None and last.close < fast:
                return Signal(
                    action=SignalAction.SELL,
                    symbol=snapshot.symbol,
                    reason=f"untracked_position_below_ema{s.ema_fast}",
                    entry_price=last.close,
                )
            return Signal.hold(snapshot.symbol, "in_untracked_position")

        assert trade is not None
        held_min = (snapshot.now - trade.entry_time).total_seconds() / 60.0  # type: ignore[operator]

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
        if held_min >= s.max_hold_minutes:
            return Signal(
                action=SignalAction.SELL,
                symbol=snapshot.symbol,
                reason="time_stop",
                entry_price=last.close,
            )

        if held_min >= s.min_hold_before_fade_minutes:
            price_closes = closes(bars)
            fast = last_value(ema(price_closes, s.ema_fast))
            rsi_now = last_value(rsi(price_closes, s.rsi_period))
            fade = False
            why = ""
            if rsi_now is not None and rsi_now >= s.rsi_fade:
                fade, why = True, f"rsi_fade:{rsi_now:.1f}"
            elif fast is not None and last.close < fast:
                fade, why = True, "close_below_ema_fast"
            if fade:
                return Signal(
                    action=SignalAction.SELL,
                    symbol=snapshot.symbol,
                    reason=why,
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

    def breakeven_stop(self, trade: OpenTrade, last_high: float) -> float:
        """Once price reaches +1R, trail the stop up to entry (giveaway protection)."""
        r = trade.risk_per_share()
        if r <= 0:
            return trade.stop_price
        if last_high >= trade.entry_price + r:
            return max(trade.stop_price, trade.entry_price)
        return trade.stop_price
