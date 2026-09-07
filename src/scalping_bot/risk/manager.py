from __future__ import annotations

from datetime import datetime, timedelta

from scalping_bot.config import Settings
from scalping_bot.models import (
    Account,
    AssetClass,
    OpenTrade,
    Position,
    RiskDecision,
    Side,
    Signal,
    SignalAction,
    infer_asset_class,
    round_qty,
)
from scalping_bot.state.store import StateStore


class RiskManager:
    """Hard limits. Any breach refuses a new order; exits are always allowed."""

    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store

    def evaluate_entry(
        self,
        signal: Signal,
        account: Account,
        positions: list[Position],
        now: datetime,
        last_price: float,
    ) -> RiskDecision:
        s = self.settings
        symbol = signal.symbol
        asset = infer_asset_class(symbol)

        if signal.action is not SignalAction.BUY:
            return RiskDecision(False, f"unsupported_entry_action:{signal.action.value}")
        if not s.allow_short and signal.action is SignalAction.BUY:
            pass
        if s.allow_margin is False and account.buying_power + 1e-9 < account.cash:
            # Margin buying power > cash; we still size off cash only below.
            pass
        if self.store.kill_switch_active():
            return RiskDecision(False, "kill_switch_active")
        if self._daily_loss_breached(account):
            self.store.set_kill_switch(True, reason="max_daily_loss")
            return RiskDecision(False, "max_daily_loss")
        if not s.allow_short and False:
            return RiskDecision(False, "shorts_disabled")

        open_longs = [p for p in positions if p.qty != 0]
        if any(p.symbol == symbol and p.qty != 0 for p in open_longs):
            return RiskDecision(False, "already_in_position")
        if len(open_longs) >= s.max_open_positions:
            return RiskDecision(False, "max_open_positions")

        day = self.store.trading_day(now)
        if self.store.trades_today(day) >= s.max_trades_per_day:
            return RiskDecision(False, "max_trades_per_day")

        last_ts = self.store.last_trade_time(symbol)
        if last_ts is not None and now - last_ts < timedelta(seconds=s.cooldown_seconds):
            return RiskDecision(False, "cooldown")

        if signal.stop_price is None or signal.take_profit_price is None:
            return RiskDecision(False, "missing_stop_or_take_profit")
        if last_price <= 0:
            return RiskDecision(False, "invalid_price")
        if signal.stop_price >= last_price:
            return RiskDecision(False, "stop_not_below_entry")
        if signal.take_profit_price <= last_price:
            return RiskDecision(False, "take_profit_not_above_entry")

        stop_dist = last_price - signal.stop_price
        if stop_dist / last_price > s.max_stop_pct * 1.25:
            return RiskDecision(False, "stop_too_wide_for_scalp")

        cash_cap = max(account.cash, 0.0)
        if not s.allow_margin:
            cash_cap = min(cash_cap, max(account.buying_power, 0.0), max(account.cash, 0.0))
        equity = max(account.equity, 0.0)
        notional_cap = min(
            s.max_position_notional,
            equity * s.max_position_pct,
            cash_cap,
        )
        risk_budget = equity * s.risk_per_trade_pct
        qty_by_risk = risk_budget / stop_dist if stop_dist > 0 else 0.0
        qty_by_notional = notional_cap / last_price if last_price > 0 else 0.0
        qty = min(qty_by_risk, qty_by_notional)
        qty = round_qty(qty, asset, s.allow_fractional_shares)
        if asset is AssetClass.STOCK and qty < 1 and not s.allow_fractional_shares:
            return RiskDecision(False, "qty_rounds_to_zero")
        notional = qty * last_price
        if qty <= 0 or notional < s.min_notional:
            return RiskDecision(False, "position_too_small")
        if notional > cash_cap + 1e-6:
            return RiskDecision(False, "insufficient_cash")
        if notional > s.max_position_notional + 1e-6:
            return RiskDecision(False, "max_position_notional")

        return RiskDecision(
            allowed=True,
            reason="ok",
            qty=qty,
            notional=notional,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
        )

    def evaluate_exit(self, signal: Signal) -> RiskDecision:
        if signal.action in (SignalAction.SELL, SignalAction.FLATTEN):
            return RiskDecision(True, "risk_reducing_exit")
        return RiskDecision(False, "not_an_exit")

    def should_kill(self, account: Account) -> bool:
        if self.store.kill_switch_active():
            return True
        if self._daily_loss_breached(account):
            self.store.set_kill_switch(True, reason="max_daily_loss")
            return True
        return False

    def _daily_loss_breached(self, account: Account) -> bool:
        start = self.store.starting_equity()
        if start <= 0:
            return False
        loss_pct = (start - account.equity) / start
        return loss_pct >= self.settings.max_daily_loss_pct

    def record_entry(self, trade: OpenTrade, now: datetime) -> None:
        self.store.record_trade_event(
            symbol=trade.symbol,
            side=Side.BUY,
            qty=trade.qty,
            price=trade.entry_price,
            ts=now,
            reason="entry",
        )

    def record_exit(
        self,
        symbol: str,
        qty: float,
        price: float,
        now: datetime,
        reason: str,
        realized_pl: float,
    ) -> None:
        self.store.record_trade_event(
            symbol=symbol,
            side=Side.SELL,
            qty=qty,
            price=price,
            ts=now,
            reason=reason,
            realized_pl=realized_pl,
        )

    def reset_day_if_needed(self, now: datetime, equity: float) -> None:
        self.store.ensure_trading_day(now, equity)
