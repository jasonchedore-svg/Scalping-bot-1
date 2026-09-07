from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scalping_bot.models import DailyStats, OpenTrade, Side, infer_asset_class

ET = ZoneInfo("America/New_York")


class StateStore:
    """SQLite persistence for daily P&amp;L, open trades, and kill-switch."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                day TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                reason TEXT NOT NULL,
                realized_pl REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS open_trades (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                stop_price REAL NOT NULL,
                take_profit_price REAL NOT NULL,
                high_water REAL NOT NULL,
                client_order_id TEXT
            );
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY,
                starting_equity REAL NOT NULL,
                trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def trading_day(self, now: datetime) -> str:
        local = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
        return local.date().isoformat()

    def ensure_trading_day(self, now: datetime, equity: float) -> str:
        day = self.trading_day(now)
        current = self.get_meta("current_day")
        if current != day:
            self._conn.execute(
                "INSERT INTO daily(day, starting_equity) VALUES(?, ?) "
                "ON CONFLICT(day) DO NOTHING",
                (day, equity),
            )
            self.set_meta("current_day", day)
            self.set_meta("kill_switch", "0")
            self.set_meta("kill_reason", "")
            # Drop leftover stock scalp state across sessions; crypto may persist.
            self._conn.execute(
                "DELETE FROM open_trades WHERE symbol NOT LIKE '%/%'",
            )
            self._conn.commit()
        else:
            self._conn.execute(
                "INSERT INTO daily(day, starting_equity) VALUES(?, ?) "
                "ON CONFLICT(day) DO NOTHING",
                (day, equity),
            )
            self._conn.commit()
        return day

    def starting_equity(self) -> float:
        day = self.get_meta("current_day")
        if not day:
            return 0.0
        row = self._conn.execute(
            "SELECT starting_equity FROM daily WHERE day = ?", (day,)
        ).fetchone()
        return float(row["starting_equity"]) if row else 0.0

    def trades_today(self, day: str | None = None) -> int:
        day = day or self.get_meta("current_day")
        if not day:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE day = ? AND side = 'buy'", (day,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def last_trade_time(self, symbol: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT ts FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["ts"])

    def kill_switch_active(self) -> bool:
        return self.get_meta("kill_switch", "0") == "1"

    def set_kill_switch(self, active: bool, reason: str = "") -> None:
        self.set_meta("kill_switch", "1" if active else "0")
        self.set_meta("kill_reason", reason)

    def record_trade_event(
        self,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
        ts: datetime,
        reason: str,
        realized_pl: float = 0.0,
    ) -> None:
        day = self.trading_day(ts)
        self._conn.execute(
            "INSERT INTO trades(ts, day, symbol, side, qty, price, reason, realized_pl) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ts.isoformat(), day, symbol, side.value, qty, price, reason, realized_pl),
        )
        if side is Side.BUY:
            self._conn.execute(
                "UPDATE daily SET trades = trades + 1 WHERE day = ?",
                (day,),
            )
        else:
            if realized_pl > 0:
                self._conn.execute(
                    """
                    UPDATE daily
                    SET wins = wins + 1, realized_pnl = realized_pnl + ?
                    WHERE day = ?
                    """,
                    (realized_pl, day),
                )
            elif realized_pl < 0:
                self._conn.execute(
                    """
                    UPDATE daily
                    SET losses = losses + 1, realized_pnl = realized_pnl + ?
                    WHERE day = ?
                    """,
                    (realized_pl, day),
                )
            else:
                self._conn.execute(
                    "UPDATE daily SET realized_pnl = realized_pnl + ? WHERE day = ?",
                    (realized_pl, day),
                )
        self._conn.commit()

    def upsert_open_trade(self, trade: OpenTrade) -> None:
        self._conn.execute(
            "INSERT INTO open_trades(symbol, qty, entry_price, entry_time, stop_price, "
            "take_profit_price, high_water, client_order_id) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, entry_price=excluded.entry_price, "
            "entry_time=excluded.entry_time, stop_price=excluded.stop_price, "
            "take_profit_price=excluded.take_profit_price, high_water=excluded.high_water, "
            "client_order_id=excluded.client_order_id",
            (
                trade.symbol,
                trade.qty,
                trade.entry_price,
                trade.entry_time.isoformat(),
                trade.stop_price,
                trade.take_profit_price,
                trade.high_water,
                trade.client_order_id,
            ),
        )
        self._conn.commit()

    def get_open_trade(self, symbol: str) -> OpenTrade | None:
        row = self._conn.execute(
            "SELECT * FROM open_trades WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return OpenTrade(
            symbol=row["symbol"],
            qty=float(row["qty"]),
            entry_price=float(row["entry_price"]),
            entry_time=datetime.fromisoformat(row["entry_time"]),
            stop_price=float(row["stop_price"]),
            take_profit_price=float(row["take_profit_price"]),
            asset_class=infer_asset_class(row["symbol"]),
            high_water=float(row["high_water"]),
            client_order_id=row["client_order_id"],
        )

    def all_open_trades(self) -> list[OpenTrade]:
        rows = self._conn.execute("SELECT symbol FROM open_trades").fetchall()
        trades = [self.get_open_trade(row["symbol"]) for row in rows]
        return [t for t in trades if t is not None]

    def delete_open_trade(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM open_trades WHERE symbol = ?", (symbol,))
        self._conn.commit()

    def update_high_water_and_stop(self, symbol: str, high_water: float, stop_price: float) -> None:
        self._conn.execute(
            "UPDATE open_trades SET high_water = ?, stop_price = ? WHERE symbol = ?",
            (high_water, stop_price, symbol),
        )
        self._conn.commit()

    def daily_stats(self, equity: float, unrealized: float) -> DailyStats:
        day = self.get_meta("current_day") or ""
        row = self._conn.execute("SELECT * FROM daily WHERE day = ?", (day,)).fetchone()
        if row is None:
            return DailyStats(
                day=day,
                starting_equity=equity,
                equity=equity,
                realized_pnl=0.0,
                unrealized_pnl=unrealized,
                trades=0,
                wins=0,
                losses=0,
                kill_switch=self.kill_switch_active(),
            )
        return DailyStats(
            day=day,
            starting_equity=float(row["starting_equity"]),
            equity=equity,
            realized_pnl=float(row["realized_pnl"]),
            unrealized_pnl=unrealized,
            trades=int(row["trades"]),
            wins=int(row["wins"]),
            losses=int(row["losses"]),
            kill_switch=self.kill_switch_active(),
        )

    def recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def all_trade_events(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]

    def export_json(self, equity: float, unrealized: float) -> str:
        stats = self.daily_stats(equity, unrealized)
        payload = {
            "day": stats.day,
            "starting_equity": stats.starting_equity,
            "equity": stats.equity,
            "realized_pnl": stats.realized_pnl,
            "unrealized_pnl": stats.unrealized_pnl,
            "day_pnl": stats.day_pnl,
            "trades": stats.trades,
            "wins": stats.wins,
            "losses": stats.losses,
            "kill_switch": stats.kill_switch,
            "kill_reason": self.get_meta("kill_reason", ""),
            "open_trades": [
                {
                    "symbol": t.symbol,
                    "qty": t.qty,
                    "entry_price": t.entry_price,
                    "stop_price": t.stop_price,
                    "take_profit_price": t.take_profit_price,
                }
                for t in self.all_open_trades()
            ],
        }
        return json.dumps(payload, indent=2)
