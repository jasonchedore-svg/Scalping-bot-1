from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from uuid import uuid4
from zoneinfo import ZoneInfo

from scalping_bot.broker.base import Broker
from scalping_bot.config import Settings
from scalping_bot.logging_setup import get_logger
from scalping_bot.models import (
    AssetClass,
    BrokerError,
    OpenTrade,
    OrderRequest,
    Side,
    SignalAction,
    TimeInForce,
    infer_asset_class,
)
from scalping_bot.risk.manager import RiskManager
from scalping_bot.state.store import StateStore
from scalping_bot.strategy.base import MarketSnapshot, Strategy
from scalping_bot.strategy.momentum_scalp import MomentumScalpStrategy

ET = ZoneInfo("America/New_York")
log = get_logger("engine")


@dataclass
class RunSummary:
    iterations: int = 0
    broker: str = ""
    paper: bool = True
    dry_run: bool = False
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    refused: int = 0
    kill_switch: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def day_pnl(self) -> float:
        return self.ending_equity - self.starting_equity


class BotEngine:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        strategy: Strategy,
        store: StateStore,
        risk: RiskManager | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.strategy = strategy
        self.store = store
        self.risk = risk or RiskManager(settings, store)
        self._stop = False
        self.refused = 0

    def request_stop(self, *_args: object) -> None:
        self._stop = True
        log.info("stop_requested")

    def run(
        self,
        max_iterations: int | None = None,
        sleep: bool = True,
        handle_signals: bool = False,
    ) -> RunSummary:
        if handle_signals:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)

        account = self.broker.get_account()
        now = self._now()
        self.risk.reset_day_if_needed(now, account.equity)
        summary = RunSummary(
            broker=self.broker.name,
            paper=self.settings.paper,
            dry_run=self.settings.dry_run,
            starting_equity=self.store.starting_equity() or account.equity,
        )
        log.info(
            "engine_start",
            broker=self.broker.name,
            paper=self.settings.paper,
            dry_run=self.settings.dry_run,
            strategy=self.strategy.name,
            watchlist=self.settings.watchlist,
            live=not self.settings.paper,
        )
        iterations = 0
        while not self._stop:
            iterations += 1
            try:
                self._step()
            except BrokerError as exc:
                log.error("broker_error", error=str(exc))
            except Exception:
                log.exception("engine_step_failed")
            if max_iterations is not None and iterations >= max_iterations:
                break
            if sleep:
                time.sleep(self.settings.poll_interval_seconds)
            self.broker.advance()

        account = self.broker.get_account()
        stats = self.store.daily_stats(
            account.equity,
            sum(p.unrealized_pl for p in self.broker.get_positions()),
        )
        summary.iterations = iterations
        summary.ending_equity = account.equity
        summary.realized_pnl = stats.realized_pnl
        summary.trades = stats.trades
        summary.wins = stats.wins
        summary.losses = stats.losses
        summary.kill_switch = stats.kill_switch
        summary.refused = self.refused
        log.info(
            "engine_stop",
            iterations=iterations,
            equity=round(account.equity, 2),
            day_pnl=round(summary.day_pnl, 2),
            trades=summary.trades,
            kill_switch=summary.kill_switch,
        )
        return summary

    def _now(self) -> datetime:
        clock = self.broker.get_clock()
        ts = clock.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        return ts

    def _step(self) -> None:
        now = self._now()
        account = self.broker.get_account()
        self.risk.reset_day_if_needed(now, account.equity)
        positions = self.broker.get_positions()

        if self.risk.should_kill(account):
            log.error("kill_switch_flatten", reason="max_daily_loss_or_flag")
            self._flatten_all("kill_switch")
            return

        et_now = now.astimezone(ET)
        if self._past(et_now.time(), self.settings.flatten_et):
            stock_open = [p for p in positions if p.asset_class is AssetClass.STOCK]
            if stock_open:
                log.info("session_flatten", count=len(stock_open))
                for pos in stock_open:
                    self._exit(pos.symbol, "session_flatten")
                positions = self.broker.get_positions()

        leftover = [
            p
            for p in positions
            if p.asset_class is AssetClass.STOCK
            and not self.broker.is_tradable_now(p.symbol, now)
        ]
        if leftover:
            log.info("session_closed_flatten", count=len(leftover))
            for pos in leftover:
                self._exit(pos.symbol, "session_closed")
            positions = self.broker.get_positions()

        for pos in positions:
            self._manage_open(pos.symbol, now)

        if self.settings.dry_run:
            self._scan_entries(now, account, dry_run=True)
            return
        if self.store.kill_switch_active():
            return
        self._scan_entries(now, account, dry_run=False)

    def _manage_open(self, symbol: str, now: datetime) -> None:
        bars = self._bars(symbol)
        if not bars:
            return
        pos = self.broker.get_position(symbol)
        trade = self.store.get_open_trade(symbol)
        if pos is None:
            if trade:
                self.store.delete_open_trade(symbol)
            return
        if trade is None:
            # Adopt unknown position with conservative scalp stops.
            last = bars[-1].close
            trade = OpenTrade(
                symbol=symbol,
                qty=pos.qty,
                entry_price=pos.avg_entry_price,
                entry_time=now,
                stop_price=pos.avg_entry_price * (1 - self.settings.max_stop_pct),
                take_profit_price=pos.avg_entry_price * (1 + self.settings.max_take_profit_pct),
                asset_class=infer_asset_class(symbol),
                high_water=max(last, pos.avg_entry_price),
            )
            self.store.upsert_open_trade(trade)

        last = bars[-1]
        high_water = max(trade.high_water, last.high, last.close)
        if isinstance(self.strategy, MomentumScalpStrategy):
            new_stop = self.strategy.breakeven_stop(trade, high_water)
        else:
            new_stop = trade.stop_price
        if high_water != trade.high_water or new_stop != trade.stop_price:
            self.store.update_high_water_and_stop(symbol, high_water, new_stop)
            trade.high_water = high_water
            trade.stop_price = new_stop

        snap = MarketSnapshot(
            symbol=symbol,
            bars_1m=bars,
            now=now,
            position=pos,
            open_trade=trade,
        )
        signal = self.strategy.evaluate(snap)
        if signal.action in (SignalAction.SELL, SignalAction.FLATTEN):
            self._exit(symbol, signal.reason)

    def _scan_entries(self, now: datetime, account: object, dry_run: bool) -> None:
        et_now = now.astimezone(ET)
        account_obj = self.broker.get_account()
        positions = self.broker.get_positions()
        for symbol in self.settings.watchlist:
            asset = infer_asset_class(symbol)
            if asset is AssetClass.STOCK:
                if not self.broker.is_tradable_now(symbol, now):
                    continue
                if et_now.time() < self.settings.entry_start_et:
                    continue
                if self._past(et_now.time(), self.settings.entry_cutoff_et):
                    continue
            bars = self._bars(symbol)
            if not bars:
                continue
            stale_after = timedelta(minutes=max(self.settings.bar_minutes(), 1) * 2)
            if now - bars[-1].timestamp > stale_after:
                continue
            snap = MarketSnapshot(
                symbol=symbol,
                bars_1m=bars,
                now=now,
                position=self.broker.get_position(symbol),
                open_trade=self.store.get_open_trade(symbol),
            )
            signal = self.strategy.evaluate(snap)
            if signal.action is not SignalAction.BUY:
                continue
            decision = self.risk.evaluate_entry(
                signal, account_obj, positions, now, signal.entry_price or bars[-1].close
            )
            if not decision.allowed:
                self.refused += 1
                log.info("order_refused", symbol=symbol, reason=decision.reason)
                continue
            if dry_run:
                log.info(
                    "dry_run_entry",
                    symbol=symbol,
                    qty=decision.qty,
                    stop=decision.stop_price,
                    take=decision.take_profit_price,
                    reason=signal.reason,
                )
                continue
            self._enter(symbol, decision.qty, signal, now)
            account_obj = self.broker.get_account()
            positions = self.broker.get_positions()

    def _enter(self, symbol: str, qty: float, signal, now: datetime) -> None:
        asset = infer_asset_class(symbol)
        tif = TimeInForce.GTC if asset is AssetClass.CRYPTO else TimeInForce.DAY
        request = OrderRequest(
            symbol=symbol,
            qty=qty,
            side=Side.BUY,
            time_in_force=tif,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            client_order_id=str(uuid4()),
            reason=signal.reason,
        )
        try:
            order = self.broker.submit_order(request)
        except BrokerError as exc:
            log.error("entry_failed", symbol=symbol, error=str(exc))
            return
        fill_px = order.filled_avg_price or signal.entry_price or 0.0
        fill_qty = order.filled_qty or qty
        if fill_qty <= 0 or fill_px <= 0:
            log.warning("entry_not_filled", symbol=symbol, status=order.status)
            return
        trade = OpenTrade(
            symbol=symbol,
            qty=fill_qty,
            entry_price=fill_px,
            entry_time=now,
            stop_price=signal.stop_price or fill_px * (1 - self.settings.min_stop_pct),
            take_profit_price=signal.take_profit_price
            or fill_px * (1 + self.settings.min_take_profit_pct),
            asset_class=asset,
            high_water=fill_px,
            client_order_id=order.client_order_id,
        )
        self.store.upsert_open_trade(trade)
        self.risk.record_entry(trade, now)
        log.info(
            "entered",
            symbol=symbol,
            qty=fill_qty,
            price=fill_px,
            stop=trade.stop_price,
            take=trade.take_profit_price,
        )

    def _exit(self, symbol: str, reason: str) -> None:
        pos = self.broker.get_position(symbol)
        trade = self.store.get_open_trade(symbol)
        if pos is None:
            if trade:
                self.store.delete_open_trade(symbol)
            return
        try:
            self.broker.cancel_open_orders(symbol)
            order = self.broker.close_position(symbol, reason=reason)
        except BrokerError as exc:
            log.error("exit_failed", symbol=symbol, error=str(exc), reason=reason)
            return
        px = 0.0
        qty = pos.qty
        if order and order.filled_avg_price:
            px = order.filled_avg_price
            qty = order.filled_qty or qty
        else:
            try:
                px = self.broker.last_price(symbol)
            except BrokerError:
                px = trade.entry_price if trade else 0.0
        entry = trade.entry_price if trade else pos.avg_entry_price
        realized = (px - entry) * qty if px else 0.0
        now = self._now()
        self.risk.record_exit(symbol, qty, px, now, reason, realized)
        self.store.delete_open_trade(symbol)
        log.info(
            "exited",
            symbol=symbol,
            qty=qty,
            price=px,
            reason=reason,
            realized_pl=round(realized, 4),
        )

    def _flatten_all(self, reason: str) -> None:
        for pos in self.broker.get_positions():
            self._exit(pos.symbol, reason)

    def _bars(self, symbol: str):
        try:
            return self.broker.get_bars(
                symbol, self.settings.bar_timeframe, self.settings.bar_lookback
            )
        except BrokerError as exc:
            log.warning("bars_unavailable", symbol=symbol, error=str(exc))
            return []

    @staticmethod
    def _past(now: dt_time, cutoff: dt_time) -> bool:
        return now >= cutoff
