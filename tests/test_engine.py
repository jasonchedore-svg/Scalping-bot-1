from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from scalping_bot.broker.simulator import LocalPaperBroker
from scalping_bot.cli import app
from scalping_bot.config import Settings
from scalping_bot.engine.bot import BotEngine
from scalping_bot.risk.manager import RiskManager
from scalping_bot.state.store import StateStore
from scalping_bot.strategy import load_strategy


def test_engine_demo_loop(tmp_path: Path) -> None:
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": True,
            "force_simulator": True,
            "watchlist": ["AAPL", "BTC/USD"],
            "state_dir": tmp_path,
            "max_trades_per_day": 40,
            "cooldown_seconds": 1,
            "poll_interval_seconds": 0,
        }
    )
    settings = Settings.model_validate(payload)
    store = StateStore(settings.db_path())
    broker = LocalPaperBroker(symbols=list(settings.watchlist), cash=100_000, seed=11)
    engine = BotEngine(
        settings,
        broker,
        load_strategy("momentum_scalp", settings=settings),
        store,
        RiskManager(settings, store),
    )
    summary = engine.run(max_iterations=80, sleep=False)
    assert summary.iterations == 80
    assert summary.paper is True
    assert summary.ending_equity > 0
    assert summary.broker == "simulator"
    stats = store.daily_stats(summary.ending_equity, 0.0)
    assert stats.day
    assert stats.trades == summary.trades


def test_dry_run_places_no_orders(tmp_path: Path) -> None:
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": True,
            "dry_run": True,
            "watchlist": ["AAPL"],
            "state_dir": tmp_path,
            "poll_interval_seconds": 0,
        }
    )
    settings = Settings.model_validate(payload)
    store = StateStore(settings.db_path())
    broker = LocalPaperBroker(symbols=["AAPL"], cash=100_000, seed=3)
    engine = BotEngine(
        settings,
        broker,
        load_strategy("momentum_scalp", settings=settings),
        store,
        RiskManager(settings, store),
    )
    summary = engine.run(max_iterations=40, sleep=False)
    assert summary.trades == 0
    assert broker.get_positions() == []


def test_cli_help_and_demo() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "demo" in help_result.stdout
    demo = runner.invoke(
        app, ["demo", "--minutes", "25", "--symbols", "AAPL,BTC/USD", "--seed", "5"]
    )
    assert demo.exit_code == 0, demo.output
    assert "Demo summary" in demo.output
    assert "paper=True" in demo.output
