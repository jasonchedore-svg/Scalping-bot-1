from __future__ import annotations

import os
import signal
import tempfile
from pathlib import Path

import typer

from scalping_bot.config import Settings
from scalping_bot.logging_setup import setup_logging
from scalping_bot.models import LiveTradingDisabledError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Paper-first trading bot (swing recommended; scalp is educational/legacy).",
)


def _load_settings(
    *,
    live: bool = False,
    dry_run: bool = False,
    watchlist: str | None = None,
    force_simulator: bool = False,
    state_dir: Path | None = None,
    mode: str | None = None,
) -> Settings:
    payload = Settings().model_dump()
    if mode == "swing":
        from scalping_bot.config import apply_swing_profile

        payload = apply_swing_profile(payload)
    if live:
        payload["paper"] = False
        payload["live_confirmed"] = True
    if dry_run:
        payload["dry_run"] = True
    if watchlist:
        payload["watchlist"] = watchlist
    if force_simulator:
        payload["force_simulator"] = True
        payload["paper"] = True
    if state_dir is not None:
        payload["state_dir"] = state_dir
    settings = Settings.model_validate(payload)
    settings.assert_trading_mode()
    return settings


def _boot(settings: Settings):
    from scalping_bot.broker.factory import make_broker
    from scalping_bot.engine.bot import BotEngine
    from scalping_bot.risk.manager import RiskManager
    from scalping_bot.state.store import StateStore
    from scalping_bot.strategy import load_strategy

    setup_logging(settings.log_level, settings.log_json)
    store = StateStore(settings.db_path(), persist_overnight_stocks=settings.hold_overnight)
    broker = make_broker(settings)
    strategy = load_strategy(settings.strategy, settings=settings)
    risk = RiskManager(settings, store)
    engine = BotEngine(settings, broker, strategy, store, risk)
    return engine, broker, store


@app.command()
def start(
    dry_run: bool = typer.Option(False, help="Evaluate signals but do not send orders."),
    live: bool = typer.Option(
        False,
        "--live",
        help="DANGEROUS. Real-money mode. Requires ALLOW_LIVE_TRADING=true and a live API URL.",
    ),
    watchlist: str | None = typer.Option(None, help="Comma-separated symbols, e.g. AAPL,BTC/USD"),
    simulator: bool = typer.Option(False, help="Force the local paper simulator."),
    mode: str = typer.Option(
        "scalp",
        help="scalp (legacy educational default) or swing (recommended).",
    ),
) -> None:
    """Run the bot loop (paper by default). Foreground process."""
    try:
        settings = _load_settings(
            live=live,
            dry_run=dry_run,
            watchlist=watchlist,
            force_simulator=simulator,
            mode=mode,
        )
    except LiveTradingDisabledError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if not settings.paper:
        typer.secho(
            "LIVE TRADING MODE. Real money. This is not financial advice.",
            fg=typer.colors.RED,
        )
    else:
        typer.echo("Paper trading (no real money).")

    engine, _broker, _store = _boot(settings)
    pid_path = settings.pid_path()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        engine.run(sleep=True, handle_signals=True)
    finally:
        if pid_path.exists():
            pid_path.unlink()


@app.command()
def stop() -> None:
    """Stop a background/foreground bot via its PID file (SIGTERM)."""
    settings = Settings()
    pid_path = settings.pid_path()
    if not pid_path.exists():
        typer.echo("No PID file found; bot does not appear to be running.")
        raise typer.Exit(code=1)
    pid = int(pid_path.read_text(encoding="utf-8").strip())
    try:
        os.kill(pid, signal.SIGTERM)
        typer.echo(f"Sent SIGTERM to pid {pid}.")
    except ProcessLookupError:
        typer.echo(f"Process {pid} is not running. Removing stale PID file.")
        pid_path.unlink(missing_ok=True)


@app.command()
def status() -> None:
    """Show paper/live flags, broker, kill-switch, and daily P&L."""
    settings = Settings()
    from scalping_bot.state.store import StateStore

    store = StateStore(settings.db_path())
    typer.echo(f"paper={settings.paper} dry_run={settings.dry_run}")
    typer.echo(f"strategy={settings.strategy} watchlist={','.join(settings.watchlist)}")
    typer.echo(f"simulator={settings.use_simulator()} keys_present={settings.has_alpaca_keys()}")
    typer.echo(
        f"kill_switch={store.kill_switch_active()} "
        f"reason={store.get_meta('kill_reason', '')}"
    )
    typer.echo(f"open_trades={len(store.all_open_trades())} trades_today={store.trades_today()}")
    running = settings.pid_path().exists()
    typer.echo(f"pid_file={running}")


@app.command()
def positions() -> None:
    """List tracked open scalps and, if keys exist, broker positions."""
    settings = Settings()
    from scalping_bot.state.store import StateStore

    store = StateStore(settings.db_path())
    tracked = store.all_open_trades()
    if not tracked:
        typer.echo("No tracked open trades.")
    for trade in tracked:
        typer.echo(
            f"{trade.symbol} qty={trade.qty} entry={trade.entry_price} "
            f"stop={trade.stop_price} tp={trade.take_profit_price}"
        )
    if settings.has_alpaca_keys() and not settings.force_simulator:
        from scalping_bot.broker.factory import make_broker

        try:
            broker = make_broker(settings)
            for pos in broker.get_positions():
                typer.echo(
                    f"broker {pos.symbol} qty={pos.qty} avg={pos.avg_entry_price} "
                    f"uP&L={pos.unrealized_pl:.2f}"
                )
        except Exception as exc:
            typer.echo(f"Broker positions unavailable: {type(exc).__name__}")


@app.command()
def pnl() -> None:
    """Print persisted daily P&L (and broker equity if keys are present)."""
    settings = Settings()
    from scalping_bot.state.store import StateStore

    store = StateStore(settings.db_path())
    equity = store.starting_equity()
    unreal = 0.0
    if settings.has_alpaca_keys() and not settings.force_simulator:
        from scalping_bot.broker.factory import make_broker

        try:
            broker = make_broker(settings)
            acct = broker.get_account()
            equity = acct.equity
            unreal = sum(p.unrealized_pl for p in broker.get_positions())
        except Exception:
            pass
    stats = store.daily_stats(equity or 0.0, unreal)
    typer.echo(store.export_json(stats.equity, stats.unrealized_pnl))


@app.command()
def watchlist() -> None:
    """Show the configured universe (stocks + crypto)."""
    settings = Settings()
    for symbol in settings.watchlist:
        kind = "crypto" if "/" in symbol else "stock"
        typer.echo(f"{symbol}\t{kind}")


@app.command()
def demo(
    minutes: int = typer.Option(90, help="Simulated 1-minute bars to replay."),
    symbols: str = typer.Option("AAPL,BTC/USD", help="Demo universe."),
    seed: int = typer.Option(7, help="Simulator RNG seed."),
) -> None:
    """Run a fast local paper demo. No API keys required."""
    tmp = Path(tempfile.mkdtemp(prefix="scalping-demo-"))
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": True,
            "force_simulator": True,
            "dry_run": False,
            "watchlist": symbols,
            "state_dir": tmp,
            "simulator_seed": seed,
            "poll_interval_seconds": 0,
            "max_trades_per_day": 50,
            "cooldown_seconds": 2,
        }
    )
    settings = Settings.model_validate(payload)
    setup_logging(settings.log_level, settings.log_json)
    from scalping_bot.broker.simulator import LocalPaperBroker
    from scalping_bot.engine.bot import BotEngine
    from scalping_bot.risk.manager import RiskManager
    from scalping_bot.state.store import StateStore
    from scalping_bot.strategy import load_strategy

    store = StateStore(settings.db_path())
    broker = LocalPaperBroker(
        symbols=list(settings.watchlist),
        cash=settings.initial_cash,
        seed=seed,
    )
    strategy = load_strategy(settings.strategy, settings=settings)
    engine = BotEngine(settings, broker, strategy, store, RiskManager(settings, store))
    typer.echo(
        f"Demo starting (local simulator, paper only). "
        f"minutes={minutes} symbols={','.join(settings.watchlist)}"
    )
    summary = engine.run(max_iterations=minutes, sleep=False, handle_signals=False)
    typer.echo("")
    typer.echo("=== Demo summary ===")
    typer.echo(f"broker={summary.broker} paper={summary.paper} dry_run={summary.dry_run}")
    typer.echo(f"iterations={summary.iterations}")
    typer.echo(f"starting_equity={summary.starting_equity:.2f}")
    typer.echo(f"ending_equity={summary.ending_equity:.2f}")
    typer.echo(f"day_pnl={summary.day_pnl:.2f}")
    typer.echo(f"trades={summary.trades} wins={summary.wins} losses={summary.losses}")
    typer.echo(f"orders_refused={summary.refused} kill_switch={summary.kill_switch}")
    typer.echo("Not financial advice. Paper simulation only.")


@app.command()
def backtest(
    symbols: str = typer.Option(
        "SPY,QQQ,AAPL,BTC/USD,ETH/USD",
        help="Comma-separated symbols (stocks/ETFs + crypto pairs).",
    ),
    days: int = typer.Option(7, help="Calendar days of history (5–30 scalp; 180–365 swing)."),
    starting_equity: float = typer.Option(100_000.0, help="Starting cash/equity."),
    seed: int | None = typer.Option(
        None,
        help="RNG seed. Used by --source simulator only; historical replay is deterministic.",
    ),
    interval: str = typer.Option(
        "auto",
        help="Bar size: auto, 1m, 5m, or 1d. auto: 1m if days<=7, 5m if days<=59, else 1d.",
    ),
    strategy: str = typer.Option(
        "momentum_scalp",
        help="Registered strategy name. Use trend_swing for daily swing backtests.",
    ),
    source: str = typer.Option(
        "historical",
        help="historical (Yahoo/Coinbase/yfinance, no keys) or simulator (synthetic random-walk).",
    ),
    cache: bool = typer.Option(
        True,
        "--cache/--no-cache",
        help="Cache downloaded bars under data/cache (default on).",
    ),
    mode: str = typer.Option(
        "scalp",
        help="scalp (intraday) or swing (daily bars, wider stops, trend_swing).",
    ),
) -> None:
    """Replay free historical bars through Strategy + RiskManager. No broker keys."""
    from scalping_bot.backtest.runner import BacktestRequest, run_backtest
    from scalping_bot.config import _parse_watchlist

    if days < 1 or days > 800:
        typer.secho("--days must be between 1 and 800", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    req = BacktestRequest(
        symbols=_parse_watchlist(symbols),
        days=days,
        starting_equity=starting_equity,
        strategy=strategy,
        interval=interval,
        source=source,
        seed=seed,
        use_cache=cache,
        mode=mode,
    )
    typer.echo(
        f"Backtest starting source={source} days={days} interval={interval} "
        f"strategy={strategy} mode={mode} symbols={','.join(req.symbols)}"
    )
    try:
        report = run_backtest(req)
    except Exception as exc:
        typer.secho(f"Backtest failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("")
    typer.echo(report.render())


@app.command("swing-backtest")
def swing_backtest(
    symbols: str = typer.Option(
        "SPY,QQQ,AAPL,BTC/USD,ETH/USD",
        help="Comma-separated symbols (stocks/ETFs + crypto pairs).",
    ),
    days: int = typer.Option(365, help="Calendar days of daily history (6–12 months typical)."),
    starting_equity: float = typer.Option(100_000.0, help="Starting cash/equity."),
    cache: bool = typer.Option(
        True,
        "--cache/--no-cache",
        help="Cache downloaded bars under data/cache (default on).",
    ),
) -> None:
    """Daily-bar swing backtest (recommended). No broker keys. Includes SPY buy-and-hold."""
    from scalping_bot.backtest.runner import BacktestRequest, run_backtest
    from scalping_bot.config import _parse_watchlist

    if days < 60 or days > 800:
        typer.secho(
            "--days must be between 60 and 800 for swing-backtest",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    req = BacktestRequest(
        symbols=_parse_watchlist(symbols),
        days=days,
        starting_equity=starting_equity,
        strategy="trend_swing",
        interval="1d",
        source="historical",
        use_cache=cache,
        mode="swing",
    )
    typer.echo(
        f"Swing backtest days={days} interval=1d strategy=trend_swing "
        f"symbols={','.join(req.symbols)}"
    )
    try:
        report = run_backtest(req)
    except Exception as exc:
        typer.secho(f"Backtest failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("")
    typer.echo(report.render())


@app.command("dry-run")
def dry_run_cmd(
    minutes: int = typer.Option(5, help="Iterations to evaluate."),
    simulator: bool = typer.Option(True, help="Use local simulator (default)."),
) -> None:
    """Print would-be orders without submitting them."""
    settings = _load_settings(dry_run=True, force_simulator=simulator)
    engine, _broker, _store = _boot(settings)
    summary = engine.run(max_iterations=minutes, sleep=False)
    typer.echo(f"dry-run complete. refused={summary.refused} trades={summary.trades} (should be 0)")


@app.callback()
def _root() -> None:
    """Scalping bot CLI."""
    return None


def main() -> None:
    app()
