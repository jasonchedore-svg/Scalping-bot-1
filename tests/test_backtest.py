from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from scalping_bot.backtest.broker import HistoricalReplayBroker
from scalping_bot.backtest.fetch import (
    HistoryBundle,
    IntervalChoice,
    SymbolHistory,
    as_daily_session_ts,
    choose_interval,
    parse_binance_klines,
    parse_coinbase_candles,
    rows_to_bars,
    yahoo_ticker,
)
from scalping_bot.backtest.report import max_drawdown, pair_round_trips, summarize_rounds
from scalping_bot.backtest.runner import BacktestRequest, run_backtest
from scalping_bot.cli import app
from scalping_bot.models import Bar, OrderRequest, Side
from tests.helpers import momentum_long_setup, session_start, trend_swing_entry_setup

ET = ZoneInfo("America/New_York")
_ANSI = re.compile(r"\x1b\[[0-9;]*[mK]|\x1b\]8;;.*?\x1b\\")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def test_choose_interval_auto_and_fallback() -> None:
    seven = choose_interval(7, "auto")
    assert seven.minutes == 1
    thirty = choose_interval(30, "auto")
    assert thirty.minutes == 5
    assert thirty.fallback_from_1m is True
    forced = choose_interval(30, "1m")
    assert forced.minutes == 5
    assert forced.fallback_from_1m is True
    five = choose_interval(3, "5m")
    assert five.minutes == 5
    yearly = choose_interval(180, "auto")
    assert yearly.minutes == 1440
    assert yearly.label == "1d"
    forced_daily = choose_interval(7, "1d")
    assert forced_daily.minutes == 1440
    thirty = choose_interval(30, "auto")
    assert thirty.minutes == 5


def test_yahoo_and_binance_symbol_mapping() -> None:
    assert yahoo_ticker("AAPL") == "AAPL"
    assert yahoo_ticker("BTC/USD") == "BTC-USD"
    raw = [
        [1_718_280_000_000, "100", "101", "99", "100.5", "12.0", 0, "0", 0, 0, "0", "0"],
        [1_718_280_060_000, "100.5", "102", "100", "101", "8.0", 0, "0", 0, 0, "0", "0"],
    ]
    bars = parse_binance_klines("BTC/USD", raw)
    assert len(bars) == 2
    assert bars[0].open == 100.0
    assert bars[1].close == 101.0
    assert bars[0].symbol == "BTC/USD"
    cb = parse_coinbase_candles(
        "ETH/USD",
        [
            [1_718_280_060, 99.0, 102.0, 100.5, 101.0, 8.0],
            [1_718_280_000, 99.0, 101.0, 100.0, 100.5, 12.0],
        ],
    )
    assert len(cb) == 2
    assert cb[0].open == 100.0
    assert cb[1].close == 101.0
    assert cb[0].timestamp < cb[1].timestamp


def test_rows_to_bars_sorts_and_zones() -> None:
    start = datetime(2024, 6, 13, 14, 30)  # naive, treated as UTC -> ET
    rows = [
        (start + timedelta(minutes=1), 2, 2.1, 1.9, 2.0, 10),
        (start, 1, 1.1, 0.9, 1.0, 10),
    ]
    bars = rows_to_bars("MSFT", rows)
    assert bars[0].close == 1.0
    assert bars[1].close == 2.0
    assert bars[0].timestamp.tzinfo is not None


def test_report_drawdown_and_round_trips() -> None:
    start = datetime(2024, 6, 13, 10, 0, tzinfo=ET)
    curve = [
        (start, 100_000.0),
        (start + timedelta(minutes=1), 101_000.0),
        (start + timedelta(minutes=2), 98_000.0),
        (start + timedelta(minutes=3), 99_000.0),
    ]
    dd_abs, dd_pct = max_drawdown(curve)
    assert dd_abs == 3_000.0
    assert abs(dd_pct - 3000 / 101_000) < 1e-9
    events = [
        {
            "symbol": "AAPL",
            "side": "buy",
            "ts": start.isoformat(),
            "qty": 10,
            "price": 100,
            "realized_pl": 0,
            "reason": "entry",
        },
        {
            "symbol": "AAPL",
            "side": "sell",
            "ts": (start + timedelta(minutes=5)).isoformat(),
            "qty": 10,
            "price": 101,
            "realized_pl": 8.5,
            "reason": "take_profit",
        },
        {
            "symbol": "AAPL",
            "side": "buy",
            "ts": (start + timedelta(minutes=10)).isoformat(),
            "qty": 10,
            "price": 100,
            "realized_pl": 0,
            "reason": "entry",
        },
        {
            "symbol": "AAPL",
            "side": "sell",
            "ts": (start + timedelta(minutes=12)).isoformat(),
            "qty": 10,
            "price": 99,
            "realized_pl": -12.0,
            "reason": "stop_loss",
        },
    ]
    rounds = pair_round_trips(events)
    by_symbol, wins, losses, realized = summarize_rounds(rounds)
    assert len(rounds) == 2
    assert wins == 1 and losses == 1
    assert realized == -3.5
    assert by_symbol["AAPL"].trades == 2
    assert by_symbol["AAPL"].avg_win == 8.5
    assert by_symbol["AAPL"].avg_loss == -12.0


def _pad_setup(symbol: str, n_prefix: int = 120) -> list[Bar]:
    setup = momentum_long_setup(symbol)
    start = session_start() - timedelta(minutes=n_prefix)
    prefix: list[Bar] = []
    px = setup[0].close * 0.995
    for i in range(n_prefix):
        nxt = px * (1.00002 if i % 2 == 0 else 0.99999)
        prefix.append(
            Bar(
                symbol=symbol,
                timestamp=start + timedelta(minutes=i),
                open=px,
                high=max(px, nxt) * 1.0002,
                low=min(px, nxt) * 0.9998,
                close=nxt,
                volume=800_000,
            )
        )
        px = nxt
    shifted: list[Bar] = []
    t0 = prefix[-1].timestamp + timedelta(minutes=1)
    for i, bar in enumerate(setup):
        shifted.append(
            Bar(
                symbol=bar.symbol,
                timestamp=t0 + timedelta(minutes=i),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        )
    return prefix + shifted


def test_historical_broker_fills_and_clock() -> None:
    bars = _pad_setup("AAPL")
    broker = HistoricalReplayBroker({"AAPL": bars}, cash=50_000, interval_minutes=1, min_bars=80)
    assert broker.name == "historical"
    assert broker.get_account().cash == 50_000
    clock = broker.get_clock()
    assert clock.timestamp.tzinfo is not None
    px = broker.last_price("AAPL")
    order = broker.submit_order(OrderRequest(symbol="AAPL", qty=1, side=Side.BUY))
    assert order.status == "filled"
    assert order.filled_avg_price is not None
    assert order.filled_avg_price > px  # 2bps stock slippage
    assert broker.get_position("AAPL") is not None
    broker.advance()
    broker.close_position("AAPL", reason="test")
    assert broker.get_position("AAPL") is None
    assert broker.ledger.slippage_paid > 0


def test_historical_backtest_reuses_engine(tmp_path: Path) -> None:
    bars = _pad_setup("AAPL")
    bundle = HistoryBundle(
        symbols=["AAPL"],
        by_symbol={
            "AAPL": SymbolHistory("AAPL", bars, "fixture", 1, "unit fixture"),
        },
        interval=IntervalChoice("1m", 1, "fixture 1m"),
        notes=["unit fixture, no network"],
    )
    report = run_backtest(
        BacktestRequest(
            symbols=["AAPL"],
            days=1,
            starting_equity=100_000,
            bundle=bundle,
            state_dir=tmp_path,
            source="historical",
        )
    )
    assert report.source == "historical"
    assert report.starting_equity == 100_000
    assert report.ending_equity > 0
    reclaim = run_backtest(
        BacktestRequest(
            symbols=["AAPL"],
            days=1,
            starting_equity=100_000,
            bundle=bundle,
            state_dir=tmp_path / "reclaim",
            source="historical",
            strategy="momentum_reclaim",
        )
    )
    assert reclaim.strategy == "momentum_reclaim"
    assert reclaim.ending_equity > 0
    text = report.render()
    assert "Backtest summary" in text
    assert "win_rate" in text
    assert "Per-symbol" in text
    assert "slippage" in text


def test_simulator_source_uses_seed(tmp_path: Path) -> None:
    kw = {
        "symbols": ["AAPL", "BTC/USD"],
        "days": 1,
        "starting_equity": 100_000.0,
        "source": "simulator",
        "steps": 80,
        "seed": 11,
    }
    a = run_backtest(BacktestRequest(**kw, state_dir=tmp_path / "a"))  # type: ignore[arg-type]
    b = run_backtest(BacktestRequest(**kw, state_dir=tmp_path / "b"))  # type: ignore[arg-type]
    assert a.source == "simulator"
    assert a.ending_equity == b.ending_equity
    assert a.trades == b.trades
    joined = " ".join(a.notes)
    assert "synthetic" in joined.lower() or "NOT a historical" in joined


def test_cli_backtest_help() -> None:
    runner = CliRunner()
    help_result = runner.invoke(
        app,
        ["backtest", "--help"],
        env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120"},
    )
    assert help_result.exit_code == 0, help_result.output
    text = _plain(help_result.output or help_result.stdout or "")
    assert "--days" in text
    assert "--symbols" in text
    assert "--starting-equity" in text
    assert "--seed" in text


def test_cli_swing_backtest_help() -> None:
    runner = CliRunner()
    help_result = runner.invoke(
        app,
        ["swing-backtest", "--help"],
        env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120"},
    )
    assert help_result.exit_code == 0, help_result.output
    text = _plain(help_result.output or help_result.stdout or "")
    assert "--days" in text
    assert "365" in text or "daily" in text.lower() or "swing" in text.lower()


def test_cli_rejects_bad_days() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["backtest", "--days", "0"])
    assert result.exit_code == 2


def test_cli_simulator_backtest_short(monkeypatch) -> None:
    """CLI historical path is network-bound; simulator is covered via runner steps.

    Patch simulator steps by intercepting BacktestRequest construction through run_backtest.
    """
    from scalping_bot.backtest import runner as runner_mod

    original = runner_mod.run_backtest

    def _wrapped(req: BacktestRequest) -> object:
        req.steps = 40
        return original(req)

    monkeypatch.setattr(runner_mod, "run_backtest", _wrapped)
    # The CLI imports run_backtest from runner inside the command — patch the CLI binding.
    import scalping_bot.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_backtest", _wrapped, raising=False)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "backtest",
            "--source",
            "simulator",
            "--symbols",
            "AAPL,BTC/USD",
            "--days",
            "1",
            "--seed",
            "5",
            "--starting-equity",
            "100000",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Backtest summary" in result.output


def test_daily_session_timestamp_and_tradable() -> None:
    naive = datetime(2024, 6, 13, 0, 0)
    ts = as_daily_session_ts(naive)
    assert ts.tzinfo is not None
    assert ts.hour == 15 and ts.minute == 30
    utc = datetime(2024, 6, 13, 0, 0, tzinfo=UTC)
    assert as_daily_session_ts(utc).date().isoformat() == "2024-06-13"

    bars = trend_swing_entry_setup("SPY")
    broker = HistoricalReplayBroker(
        {"SPY": bars},
        cash=50_000,
        interval_minutes=1440,
        start_index=len(bars) - 5,
        min_bars=2,
    )
    assert broker.is_tradable_now("SPY") is True
    saturday = datetime(2024, 6, 15, 15, 30, tzinfo=ET)
    assert broker.is_tradable_now("SPY", saturday) is False


def test_swing_historical_backtest_fixture(tmp_path: Path) -> None:
    bars = trend_swing_entry_setup("SPY")
    last = bars[-1]
    # Two follow-through days so the engine can exit after entry.
    follow = []
    ts = last.timestamp
    px = last.close
    for _ in range(3):
        ts = ts + timedelta(days=1)
        while ts.weekday() >= 5:
            ts += timedelta(days=1)
        nxt = px * 1.04
        follow.append(
            Bar(
                symbol="SPY",
                timestamp=ts,
                open=px,
                high=nxt * 1.01,
                low=px * 0.995,
                close=nxt,
                volume=1_200_000,
            )
        )
        px = nxt
    series = bars + follow
    bundle = HistoryBundle(
        symbols=["SPY"],
        by_symbol={"SPY": SymbolHistory("SPY", series, "fixture", 1440, "swing fixture")},
        interval=IntervalChoice("1d", 1440, "fixture 1d"),
        notes=["unit fixture, no network"],
    )
    report = run_backtest(
        BacktestRequest(
            symbols=["SPY"],
            days=400,
            starting_equity=100_000,
            bundle=bundle,
            state_dir=tmp_path / "swing",
            source="historical",
            strategy="trend_swing",
            mode="swing",
            interval="1d",
        )
    )
    assert report.strategy == "trend_swing"
    assert report.interval_label == "1d"
    assert report.ending_equity > 0
    text = report.render()
    assert "Backtest summary" in text
    assert "SPY" in text
