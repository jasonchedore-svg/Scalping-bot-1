from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scalping_bot.config import Settings
from scalping_bot.models import Account, AssetClass, Position, Side, Signal, SignalAction
from scalping_bot.risk.manager import RiskManager
from scalping_bot.state.store import StateStore

ET = ZoneInfo("America/New_York")


def _mgr(tmp_path: Path, **overrides: object) -> tuple[RiskManager, StateStore, Settings]:
    payload = Settings().model_dump()
    payload.update(overrides)
    payload["state_dir"] = tmp_path
    settings = Settings.model_validate(payload)
    store = StateStore(tmp_path / "t.db")
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    store.ensure_trading_day(now, 100_000.0)
    return RiskManager(settings, store), store, settings


def _account(equity: float = 100_000.0, cash: float = 100_000.0) -> Account:
    return Account(equity=equity, cash=cash, buying_power=cash, paper=True)


def _buy(symbol: str = "AAPL", price: float = 100.0) -> Signal:
    return Signal(
        action=SignalAction.BUY,
        symbol=symbol,
        reason="test",
        entry_price=price,
        stop_price=price * 0.997,
        take_profit_price=price * 0.997 + (price * 0.0045),
    )


def test_allows_sized_long(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    decision = mgr.evaluate_entry(_buy(), _account(), [], now, 100.0)
    assert decision.allowed, decision.reason
    assert decision.qty > 0
    assert decision.notional <= 2000.0 + 1e-6


def test_refuses_without_stop(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    signal = Signal(action=SignalAction.BUY, symbol="AAPL", reason="x", entry_price=100)
    decision = mgr.evaluate_entry(signal, _account(), [], now, 100.0)
    assert not decision.allowed
    assert decision.reason == "missing_stop_or_take_profit"


def test_max_open_positions(tmp_path: Path) -> None:
    mgr, _store, s = _mgr(tmp_path, max_open_positions=1)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    positions = [
        Position(
            symbol="MSFT",
            qty=5,
            avg_entry_price=400,
            market_value=2000,
            unrealized_pl=0,
            asset_class=AssetClass.STOCK,
            side=Side.BUY,
        )
    ]
    decision = mgr.evaluate_entry(_buy(), _account(), positions, now, 100.0)
    assert not decision.allowed
    assert decision.reason == "max_open_positions"


def test_already_in_position(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    positions = [
        Position(
            symbol="AAPL",
            qty=5,
            avg_entry_price=100,
            market_value=500,
            unrealized_pl=0,
            asset_class=AssetClass.STOCK,
        )
    ]
    decision = mgr.evaluate_entry(_buy(), _account(), positions, now, 100.0)
    assert not decision.allowed
    assert decision.reason == "already_in_position"


def test_max_trades_per_day(tmp_path: Path) -> None:
    mgr, store, s = _mgr(tmp_path, max_trades_per_day=1)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    store.record_trade_event("AAPL", Side.BUY, 1, 100, now, "entry")
    decision = mgr.evaluate_entry(_buy("NVDA"), _account(), [], now, 100.0)
    assert not decision.allowed
    assert decision.reason == "max_trades_per_day"


def test_cooldown(tmp_path: Path) -> None:
    mgr, store, s = _mgr(tmp_path, cooldown_seconds=120)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    store.record_trade_event("AAPL", Side.BUY, 1, 100, now - timedelta(seconds=30), "entry")
    decision = mgr.evaluate_entry(_buy(), _account(), [], now, 100.0)
    assert not decision.allowed
    assert decision.reason == "cooldown"


def test_max_daily_loss_kill_switch(tmp_path: Path) -> None:
    mgr, store, s = _mgr(tmp_path, max_daily_loss_pct=0.02)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    account = _account(equity=97_900.0, cash=97_900.0)
    assert mgr.should_kill(account)
    assert store.kill_switch_active()
    decision = mgr.evaluate_entry(_buy(), account, [], now, 100.0)
    assert not decision.allowed
    assert decision.reason in {"kill_switch_active", "max_daily_loss"}


def test_insufficient_cash(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path, min_notional=10)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    decision = mgr.evaluate_entry(_buy(), _account(equity=100_000, cash=1.0), [], now, 100.0)
    assert not decision.allowed
    assert decision.reason in {"insufficient_cash", "position_too_small"}


def test_crypto_fractional_qty(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    signal = _buy("BTC/USD", price=65_000.0)
    decision = mgr.evaluate_entry(signal, _account(), [], now, 65_000.0)
    assert decision.allowed, decision.reason
    assert 0 < decision.qty < 1
    assert decision.notional <= 2000.0 + 1e-6


def test_exits_always_allowed(tmp_path: Path) -> None:
    mgr, store, _s = _mgr(tmp_path)
    store.set_kill_switch(True, "test")
    signal = Signal(action=SignalAction.SELL, symbol="AAPL", reason="stop_loss")
    assert mgr.evaluate_exit(signal).allowed


def test_stop_too_wide(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    signal = Signal(
        action=SignalAction.BUY,
        symbol="AAPL",
        reason="x",
        entry_price=100,
        stop_price=95.0,
        take_profit_price=101.0,
    )
    decision = mgr.evaluate_entry(signal, _account(), [], now, 100.0)
    assert not decision.allowed
    assert decision.reason == "stop_too_wide_for_scalp"


def test_reject_non_buy_entry(tmp_path: Path) -> None:
    mgr, _store, _s = _mgr(tmp_path)
    now = datetime(2024, 6, 13, 10, 30, tzinfo=ET)
    signal = Signal(action=SignalAction.SELL, symbol="AAPL", reason="short")
    decision = mgr.evaluate_entry(signal, _account(), [], now, 100.0)
    assert not decision.allowed
