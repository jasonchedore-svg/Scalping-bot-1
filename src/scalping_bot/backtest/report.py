from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from scalping_bot.state.store import StateStore


@dataclass
class SymbolStats:
    symbol: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    fees_hint: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.trades <= 0:
            return 0.0
        return self.wins / self.trades

    @property
    def avg_win(self) -> float:
        return self.gross_wins / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return self.gross_losses / self.losses if self.losses else 0.0


@dataclass
class BacktestReport:
    strategy: str
    source: str
    interval_label: str
    interval_minutes: int
    symbols: list[str]
    starting_equity: float
    ending_equity: float
    realized_pnl: float
    trades: int
    wins: int
    losses: int
    max_drawdown_pct: float
    max_drawdown_abs: float
    fees_paid: float
    slippage_paid: float
    stock_slippage_bps: float
    crypto_slippage_bps: float
    crypto_fee_bps: float
    refused: int
    kill_switch: bool
    by_symbol: dict[str, SymbolStats] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    baseline_name: str | None = None
    baseline_return_pct: float | None = None
    baseline_pnl: float | None = None

    @property
    def total_pnl(self) -> float:
        return self.ending_equity - self.starting_equity

    @property
    def win_rate(self) -> float:
        if self.trades <= 0:
            return 0.0
        return self.wins / self.trades

    @property
    def avg_win(self) -> float:
        wins = [r["pnl"] for r in self.rounds if r["pnl"] > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [r["pnl"] for r in self.rounds if r["pnl"] < 0]
        return sum(losses) / len(losses) if losses else 0.0

    def render(self) -> str:
        lines = [
            "=== Backtest summary ===",
            f"source={self.source} strategy={self.strategy} interval={self.interval_label}",
            f"symbols={','.join(self.symbols)}",
            f"starting_equity={self.starting_equity:.2f}",
            f"ending_equity={self.ending_equity:.2f}",
            f"total_pnl={self.total_pnl:.2f}  realized_pnl={self.realized_pnl:.2f}",
            f"trades={self.trades} wins={self.wins} losses={self.losses} "
            f"win_rate={self.win_rate:.1%}",
            f"avg_win={self.avg_win:.2f}  avg_loss={self.avg_loss:.2f}",
            f"max_drawdown={self.max_drawdown_abs:.2f} ({self.max_drawdown_pct:.2%})",
            (
                "costs: stocks "
                f"{self.stock_slippage_bps:.0f}bps slippage, crypto "
                f"{self.crypto_slippage_bps:.0f}bps slippage + {self.crypto_fee_bps:.0f}bps fee "
                f"(fees_paid={self.fees_paid:.2f}, slippage_paid={self.slippage_paid:.2f})"
            ),
            f"orders_refused={self.refused} kill_switch={self.kill_switch}",
            "",
            "Per-symbol:",
        ]
        if self.baseline_name and self.baseline_return_pct is not None:
            pnl = self.baseline_pnl if self.baseline_pnl is not None else 0.0
            lines.insert(
                -2,
                (
                    f"baseline {self.baseline_name} buy-and-hold: "
                    f"return={self.baseline_return_pct:.2%} pnl={pnl:.2f} "
                    f"on ${self.starting_equity:,.0f}"
                ),
            )
        if not self.by_symbol:
            lines.append("  (no closed trades)")
        for symbol in sorted(self.by_symbol):
            row = self.by_symbol[symbol]
            lines.append(
                f"  {symbol}: trades={row.trades} win_rate={row.win_rate:.1%} "
                f"pnl={row.realized_pnl:.2f} avg_win={row.avg_win:.2f} avg_loss={row.avg_loss:.2f}"
            )
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            for note in self.notes:
                lines.append(f"  - {note}")
        lines.append("Not financial advice. Historical replay with assumed slippage/fees.")
        return "\n".join(lines)


def max_drawdown(equity_curve: list[tuple[datetime, float]]) -> tuple[float, float]:
    if not equity_curve:
        return 0.0, 0.0
    peak = equity_curve[0][1]
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    for _ts, equity in equity_curve:
        if equity > peak:
            peak = equity
        dd_abs = peak - equity
        dd_pct = dd_abs / peak if peak > 0 else 0.0
        if dd_abs > max_dd_abs:
            max_dd_abs = dd_abs
            max_dd_pct = dd_pct
    return max_dd_abs, max_dd_pct


def pair_round_trips(events: list[dict]) -> list[dict]:
    open_qty: dict[str, list[dict]] = defaultdict(list)
    rounds: list[dict] = []
    for event in events:
        symbol = str(event["symbol"])
        side = str(event["side"])
        if side == "buy":
            open_qty[symbol].append(event)
            continue
        if side != "sell":
            continue
        entry = open_qty[symbol].pop(0) if open_qty[symbol] else None
        rounds.append(
            {
                "symbol": symbol,
                "entry_ts": entry["ts"] if entry else None,
                "exit_ts": event["ts"],
                "qty": float(event["qty"]),
                "entry_price": float(entry["price"]) if entry else None,
                "exit_price": float(event["price"]),
                "pnl": float(event.get("realized_pl") or 0.0),
                "reason": str(event.get("reason") or ""),
            }
        )
    return rounds


def summarize_rounds(rounds: list[dict]) -> tuple[dict[str, SymbolStats], int, int, float]:
    by_symbol: dict[str, SymbolStats] = {}
    wins = 0
    losses = 0
    realized = 0.0
    for item in rounds:
        symbol = item["symbol"]
        stats = by_symbol.setdefault(symbol, SymbolStats(symbol=symbol))
        pnl = float(item["pnl"])
        stats.trades += 1
        stats.realized_pnl += pnl
        realized += pnl
        if pnl > 0:
            stats.wins += 1
            stats.gross_wins += pnl
            wins += 1
        elif pnl < 0:
            stats.losses += 1
            stats.gross_losses += pnl
            losses += 1
    return by_symbol, wins, losses, realized


def build_report(
    *,
    store: StateStore,
    strategy: str,
    source: str,
    interval_label: str,
    interval_minutes: int,
    symbols: list[str],
    starting_equity: float,
    ending_equity: float,
    equity_curve: list[tuple[datetime, float]],
    fees_paid: float,
    slippage_paid: float,
    stock_slippage_bps: float,
    crypto_slippage_bps: float,
    crypto_fee_bps: float,
    refused: int,
    notes: list[str],
    baseline_name: str | None = None,
    baseline_return_pct: float | None = None,
    baseline_pnl: float | None = None,
) -> BacktestReport:
    events = store.all_trade_events()
    rounds = pair_round_trips(events)
    by_symbol, wins, losses, realized = summarize_rounds(rounds)
    dd_abs, dd_pct = max_drawdown(equity_curve)
    return BacktestReport(
        strategy=strategy,
        source=source,
        interval_label=interval_label,
        interval_minutes=interval_minutes,
        symbols=list(symbols),
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        realized_pnl=realized,
        trades=len(rounds),
        wins=wins,
        losses=losses,
        max_drawdown_pct=dd_pct,
        max_drawdown_abs=dd_abs,
        fees_paid=fees_paid,
        slippage_paid=slippage_paid,
        stock_slippage_bps=stock_slippage_bps,
        crypto_slippage_bps=crypto_slippage_bps,
        crypto_fee_bps=crypto_fee_bps,
        refused=refused,
        kill_switch=store.kill_switch_active(),
        by_symbol=by_symbol,
        notes=notes,
        rounds=rounds,
        baseline_name=baseline_name,
        baseline_return_pct=baseline_return_pct,
        baseline_pnl=baseline_pnl,
    )
