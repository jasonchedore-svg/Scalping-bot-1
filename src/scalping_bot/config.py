from __future__ import annotations

from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scalping_bot.models import LiveTradingDisabledError, normalize_symbol

ET = ZoneInfo("America/New_York")

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"


def _parse_watchlist(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = [part.strip() for part in value.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        symbol = normalize_symbol(item)
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


class Settings(BaseSettings):
    """Runtime configuration. Paper mode is the default; live is hard-gated."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    paper: bool = True
    allow_live_trading: bool = False
    live_confirmed: bool = False

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = PAPER_BASE_URL
    force_simulator: bool = False

    watchlist: list[str] = Field(
        default_factory=lambda: [
            "AAPL",
            "MSFT",
            "NVDA",
            "SPY",
            "QQQ",
            "BTC/USD",
            "ETH/USD",
        ]
    )
    poll_interval_seconds: float = 10.0
    bar_timeframe: str = "1Min"
    bar_lookback: int = 120
    dry_run: bool = False
    log_level: str = "INFO"
    log_json: bool = False
    state_dir: Path = Path("data")

    initial_cash: float = 100_000.0
    simulator_seed: int = 42

    max_position_notional: float = 2_000.0
    max_position_pct: float = 0.05
    max_daily_loss_pct: float = 0.02
    max_open_positions: int = 3
    max_trades_per_day: int = 20
    cooldown_seconds: int = 90
    risk_per_trade_pct: float = 0.004
    allow_fractional_shares: bool = True
    allow_short: bool = False
    allow_margin: bool = False
    min_notional: float = 10.0

    strategy: str = "momentum_scalp"
    stop_atr_mult: float = 1.0
    take_profit_atr_mult: float = 1.5
    max_hold_minutes: int = 20
    use_5m_trend_filter: bool = True
    min_stop_pct: float = 0.0008
    max_stop_pct: float = 0.0040
    min_take_profit_pct: float = 0.0012
    max_take_profit_pct: float = 0.0060
    entry_start_et: time = time(9, 32)
    entry_cutoff_et: time = time(15, 40)
    flatten_et: time = time(15, 50)
    skip_extended_hours: bool = True
    use_broker_brackets: bool = True
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    volume_sma_period: int = 20
    volume_spike: float = 1.15
    rsi_entry_low: float = 45.0
    rsi_entry_high: float = 70.0
    rsi_fade: float = 75.0
    min_hold_before_fade_minutes: int = 5

    @field_validator("watchlist", mode="before")
    @classmethod
    def _watchlist(cls, value: str | list[str]) -> list[str]:
        return _parse_watchlist(value)

    @field_validator("alpaca_base_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator(
        "max_daily_loss_pct",
        "max_position_pct",
        "risk_per_trade_pct",
        "stop_atr_mult",
        "take_profit_atr_mult",
    )
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    def state_path(self) -> Path:
        path = self.state_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def db_path(self) -> Path:
        return self.state_path() / "bot_state.db"

    def pid_path(self) -> Path:
        return self.state_path() / "bot.pid"

    def has_alpaca_keys(self) -> bool:
        return bool(self.alpaca_api_key.strip() and self.alpaca_secret_key.strip())

    def use_simulator(self) -> bool:
        return self.force_simulator or not self.has_alpaca_keys()

    def effective_base_url(self) -> str:
        if self.paper:
            return PAPER_BASE_URL
        return self.alpaca_base_url or LIVE_BASE_URL

    def assert_trading_mode(self) -> None:
        """Refuse live routing unless every hard gate is satisfied."""
        if self.paper:
            return
        missing: list[str] = []
        if not self.allow_live_trading:
            missing.append("ALLOW_LIVE_TRADING=true")
        if not self.live_confirmed:
            missing.append("CLI flag --live")
        url = (self.alpaca_base_url or "").lower()
        if "paper-api" in url:
            missing.append("ALPACA_BASE_URL must be the live API (not paper-api.alpaca.markets)")
        if not self.has_alpaca_keys():
            missing.append("live ALPACA_API_KEY / ALPACA_SECRET_KEY")
        if missing:
            raise LiveTradingDisabledError(
                "Live trading is hard-gated and OFF by default. Refusing to start. "
                "To enable (not recommended) you must set all of: " + "; ".join(missing) + ". "
                "This software is for education and paper trading only."
            )

    def now_et(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(tz=ET)
        if current.tzinfo is None:
            current = current.replace(tzinfo=ET)
        return current.astimezone(ET)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
