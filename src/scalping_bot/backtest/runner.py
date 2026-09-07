from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from scalping_bot.backtest.broker import HistoricalReplayBroker
from scalping_bot.backtest.fetch import HistoryBundle, IntervalChoice, fetch_bundle
from scalping_bot.backtest.report import BacktestReport, build_report
from scalping_bot.broker.base import Broker
from scalping_bot.broker.simulator import LocalPaperBroker
from scalping_bot.config import Settings
from scalping_bot.engine.bot import BotEngine
from scalping_bot.logging_setup import setup_logging
from scalping_bot.risk.manager import RiskManager
from scalping_bot.state.store import StateStore
from scalping_bot.strategy import load_strategy

DEFAULT_BACKTEST_SYMBOLS = ["SPY", "QQQ", "AAPL", "BTC/USD", "ETH/USD"]


@dataclass
class BacktestRequest:
    symbols: list[str]
    days: int = 7
    starting_equity: float = 100_000.0
    strategy: str = "momentum_scalp"
    interval: str = "auto"
    source: str = "historical"
    seed: int | None = None
    cache_dir: Path | None = Path("data/cache")
    use_cache: bool = True
    state_dir: Path | None = None
    bundle: HistoryBundle | None = None
    log_level: str = "WARNING"
    steps: int | None = None


def _settings_for(
    req: BacktestRequest, *, bar_timeframe: str, watchlist: list[str], state_dir: Path
) -> Settings:
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": True,
            "force_simulator": True,
            "dry_run": False,
            "watchlist": watchlist,
            "state_dir": state_dir,
            "initial_cash": req.starting_equity,
            "poll_interval_seconds": 0,
            "bar_timeframe": bar_timeframe,
            "bar_lookback": 150,
            "strategy": req.strategy,
        }
    )
    if req.seed is not None:
        payload["simulator_seed"] = req.seed
    return Settings.model_validate(payload)


def _attach_equity_curve(broker: Broker) -> list[tuple]:
    curve = getattr(broker, "equity_curve", None)
    if curve is not None:
        return curve
    recorded: list[tuple] = [
        (broker.get_clock().timestamp, broker.get_account().equity)
    ]
    original = broker.advance

    def wrapped() -> None:
        original()
        recorded.append((broker.get_clock().timestamp, broker.get_account().equity))

    broker.advance = wrapped  # type: ignore[method-assign]
    return recorded


def _run_engine(settings: Settings, broker: Broker, steps: int) -> tuple[BotEngine, StateStore]:
    store = StateStore(settings.db_path())
    strategy = load_strategy(settings.strategy, settings=settings)
    engine = BotEngine(settings, broker, strategy, store, RiskManager(settings, store))
    engine.run(max_iterations=steps, sleep=False, handle_signals=False)
    try:
        engine._flatten_all("end_of_backtest")  # noqa: SLF001
    except Exception:
        pass
    return engine, store


def run_historical(req: BacktestRequest) -> BacktestReport:
    bundle = req.bundle
    if bundle is None:
        bundle = fetch_bundle(
            req.symbols,
            days=req.days,
            requested_interval=req.interval,
            cache_dir=req.cache_dir,
            use_cache=req.use_cache,
        )
    state_dir = req.state_dir or Path(tempfile.mkdtemp(prefix="scalping-bt-"))
    tf = "5Min" if bundle.interval.minutes == 5 else "1Min"
    settings = _settings_for(
        req, bar_timeframe=tf, watchlist=bundle.symbols, state_dir=state_dir
    )
    setup_logging(req.log_level, False)
    broker = HistoricalReplayBroker(
        bundle.bars,
        cash=req.starting_equity,
        interval_minutes=bundle.interval.minutes,
        min_bars=min(80, settings.bar_lookback),
    )
    curve = _attach_equity_curve(broker)
    steps = max(broker.remaining_steps(), 1)
    engine, store = _run_engine(settings, broker, steps)
    account = broker.get_account()
    notes = list(bundle.notes)
    notes.append(
        f"Replayed {steps} bar closes through Strategy + RiskManager "
        "(same position caps, daily loss kill-switch, cooldowns as live/paper)."
    )
    return build_report(
        store=store,
        strategy=settings.strategy,
        source="historical",
        interval_label=bundle.interval.label,
        interval_minutes=bundle.interval.minutes,
        symbols=bundle.symbols,
        starting_equity=req.starting_equity,
        ending_equity=account.equity,
        equity_curve=curve,
        fees_paid=broker.ledger.fees_paid,
        slippage_paid=broker.ledger.slippage_paid,
        stock_slippage_bps=broker.ledger.slippage_bps["stock"],
        crypto_slippage_bps=broker.ledger.slippage_bps["crypto"],
        crypto_fee_bps=broker.ledger.crypto_fee_bps,
        refused=engine.refused,
        notes=notes,
    )


def run_simulator_source(req: BacktestRequest) -> BacktestReport:
    """Longer random-walk demo using the same report as historical backtests.

    The local simulator is synthetic (not market history). `--seed` applies here.
    """
    state_dir = req.state_dir or Path(tempfile.mkdtemp(prefix="scalping-bt-sim-"))
    settings = _settings_for(
        req, bar_timeframe="1Min", watchlist=req.symbols, state_dir=state_dir
    )
    setup_logging(req.log_level, False)
    seed = req.seed if req.seed is not None else settings.simulator_seed
    broker = LocalPaperBroker(
        symbols=list(req.symbols),
        cash=req.starting_equity,
        seed=seed,
    )
    # ~390 RTH minutes per equity day; crypto ticks the same clock.
    steps = req.steps if req.steps is not None else max(int(req.days) * 390, 60)
    curve = _attach_equity_curve(broker)
    engine, store = _run_engine(settings, broker, steps)
    account = broker.get_account()
    notes = [
        "Source=simulator: synthetic random-walk 1-minute bars with occasional momentum "
        "bursts (same generator as `scalping-bot demo`). This is NOT a historical backtest.",
        f"seed={seed} steps={steps} (days * 390 session minutes).",
    ]
    interval = IntervalChoice("1m", 1, "simulator 1-minute bars")
    return build_report(
        store=store,
        strategy=settings.strategy,
        source="simulator",
        interval_label=interval.label,
        interval_minutes=interval.minutes,
        symbols=list(req.symbols),
        starting_equity=req.starting_equity,
        ending_equity=account.equity,
        equity_curve=curve,
        fees_paid=broker.ledger.fees_paid,
        slippage_paid=broker.ledger.slippage_paid,
        stock_slippage_bps=broker.ledger.slippage_bps["stock"],
        crypto_slippage_bps=broker.ledger.slippage_bps["crypto"],
        crypto_fee_bps=broker.ledger.crypto_fee_bps,
        refused=engine.refused,
        notes=notes,
    )


def run_backtest(req: BacktestRequest) -> BacktestReport:
    source = req.source.strip().lower()
    if source in {"simulator", "demo", "random-walk", "random_walk"}:
        return run_simulator_source(req)
    if source in {"historical", "history", "yahoo"}:
        return run_historical(req)
    raise ValueError(f"Unknown backtest source {req.source!r} (use historical or simulator)")
