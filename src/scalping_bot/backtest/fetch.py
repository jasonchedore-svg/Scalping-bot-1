from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scalping_bot.models import AssetClass, Bar, infer_asset_class, normalize_symbol, round_price

ET = ZoneInfo("America/New_York")

YAHOO_1M_MAX_CALENDAR_DAYS = 7
YAHOO_5M_MAX_CALENDAR_DAYS = 59
SWING_AUTO_MIN_DAYS = 60
SMA200_WARMUP_CALENDAR_DAYS = 400
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"
DEFAULT_CACHE_DIR = Path("data/cache")
COINBASE_GRANULARITY = {
    "1m": 60,
    "1min": 60,
    "5m": 300,
    "5min": 300,
    "1d": 86400,
    "1day": 86400,
    "day": 86400,
    "daily": 86400,
}

CRYPTO_BINANCE: dict[str, str] = {
    "BTC/USD": "BTCUSDT",
    "ETH/USD": "ETHUSDT",
    "SOL/USD": "SOLUSDT",
    "DOGE/USD": "DOGEUSDT",
    "LTC/USD": "LTCUSDT",
    "AVAX/USD": "AVAXUSDT",
    "LINK/USD": "LINKUSDT",
    "UNI/USD": "UNIUSDT",
    "AAVE/USD": "AAVEUSDT",
    "BCH/USD": "BCHUSDT",
}


@dataclass
class IntervalChoice:
    label: str
    minutes: int
    note: str
    fallback_from_1m: bool = False


@dataclass
class SymbolHistory:
    symbol: str
    bars: list[Bar]
    source: str
    interval_minutes: int
    note: str = ""


@dataclass
class HistoryBundle:
    symbols: list[str]
    by_symbol: dict[str, SymbolHistory]
    interval: IntervalChoice
    notes: list[str] = field(default_factory=list)

    @property
    def bars(self) -> dict[str, list[Bar]]:
        return {sym: hist.bars for sym, hist in self.by_symbol.items()}


def yf_interval_for(minutes: int) -> str:
    if minutes >= 1440:
        return "1d"
    if minutes == 1:
        return "1m"
    return "5m"


def warmup_calendar_days(interval: IntervalChoice) -> int:
    """Extra history so SMA(200) (or scalp EMAs) is live at the start of --days."""
    if interval.minutes >= 1440:
        return SMA200_WARMUP_CALENDAR_DAYS
    if interval.minutes == 1:
        return 1
    return 2


def choose_interval(days: int, requested: str = "auto") -> IntervalChoice:
    """Pick 1m, 5m, or 1d. Yahoo 1-minute US equity history is about 7 calendar days."""
    req = requested.strip().lower()
    daily_note = (
        "Daily bars (recommended for swing). Fetch includes ~400 extra calendar days "
        "so SMA(50)/SMA(200) are warm at the start of the --days window. "
        "Crypto uses Yahoo `BTC-USD` then Coinbase public USD candles; Binance is last resort."
    )
    if req in {"1d", "1day", "day", "daily", "d"}:
        return IntervalChoice("1d", 1440, daily_note)
    if req in {"5m", "5min", "5minute", "5"}:
        return IntervalChoice(
            "5m",
            5,
            "Requested 5-minute bars. Indicator periods (EMA/RSI/ATR) span more "
            "calendar time than on 1-minute bars; this is a coarser test, not a 1-minute scalp.",
        )
    yahoo_cap_note = (
        f"Yahoo Finance typically keeps only ~{YAHOO_1M_MAX_CALENDAR_DAYS} calendar days "
        "of 1-minute US stock/ETF bars. Crypto uses Coinbase public USD candles (no keys), "
        "then Yahoo `BTC-USD` tickers; Binance public klines are a last resort (often geo-blocked)."
    )
    if req in {"1m", "1min", "1minute", "1"}:
        if days > YAHOO_1M_MAX_CALENDAR_DAYS:
            return IntervalChoice(
                "5m",
                5,
                f"Requested 1-minute bars for {days} days, but {yahoo_cap_note} "
                "Falling back to 5-minute bars for a consistent stock+crypto clock.",
                fallback_from_1m=True,
            )
        return IntervalChoice("1m", 1, yahoo_cap_note)
    if days <= YAHOO_1M_MAX_CALENDAR_DAYS:
        return IntervalChoice(
            "1m",
            1,
            f"Auto interval: 1-minute bars because --days={days} <= "
            f"{YAHOO_1M_MAX_CALENDAR_DAYS}. {yahoo_cap_note}",
        )
    if days < SWING_AUTO_MIN_DAYS:
        return IntervalChoice(
            "5m",
            5,
            f"Auto interval: 5-minute bars because --days={days} is between "
            f"{YAHOO_1M_MAX_CALENDAR_DAYS + 1} and {SWING_AUTO_MIN_DAYS - 1}. {yahoo_cap_note} "
            "On 5-minute bars the same EMA/RSI periods cover more time; holds are still "
            "measured in clock minutes (default max hold 20 minutes = four 5-minute bars).",
            fallback_from_1m=True,
        )
    return IntervalChoice(
        "1d",
        1440,
        f"Auto interval: daily bars because --days={days} >= {SWING_AUTO_MIN_DAYS}. {daily_note}",
    )


def yahoo_ticker(symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    if infer_asset_class(symbol) is AssetClass.CRYPTO:
        base, quote = symbol.split("/", 1)
        return f"{base}-{quote}"
    return symbol


def binance_symbol(symbol: str) -> str | None:
    symbol = normalize_symbol(symbol)
    return CRYPTO_BINANCE.get(symbol)


def coinbase_product(symbol: str) -> str:
    return yahoo_ticker(symbol)


def _ensure_tz(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(ET)


def as_daily_session_ts(ts: datetime) -> datetime:
    """Map a daily bar to 15:30 America/New_York on its session date.

    Yahoo/Coinbase/Binance often stamp daily candles at midnight UTC, which is the
    previous evening in ET and would fail a regular-hours tradable check.
    """
    if ts.tzinfo is None:
        d = ts.date()
    else:
        utc = ts.astimezone(UTC)
        if utc.hour == 0 and utc.minute == 0:
            d = utc.date()
        else:
            d = ts.astimezone(ET).date()
    return datetime(d.year, d.month, d.day, 15, 30, tzinfo=ET)


def rebase_daily_bars(bars: list[Bar]) -> list[Bar]:
    uniq: dict[datetime, Bar] = {}
    for bar in bars:
        ts = as_daily_session_ts(bar.timestamp)
        uniq[ts] = Bar(
            symbol=bar.symbol,
            timestamp=ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            vwap=bar.vwap,
        )
    return [uniq[k] for k in sorted(uniq)]


def rows_to_bars(
    symbol: str,
    rows: list[tuple[datetime, float, float, float, float, float]],
) -> list[Bar]:
    asset = infer_asset_class(symbol)
    bars: list[Bar] = []
    for ts, open_, high, low, close, volume in rows:
        ts = _ensure_tz(ts)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=round_price(float(open_), asset),
                high=round_price(float(high), asset),
                low=round_price(float(low), asset),
                close=round_price(float(close), asset),
                volume=max(float(volume), 0.0),
            )
        )
    bars.sort(key=lambda b: b.timestamp)
    return bars


def parse_coinbase_candles(symbol: str, payload: list) -> list[Bar]:
    """Coinbase returns [time, low, high, open, close, volume] (newest first)."""
    rows: list[tuple[datetime, float, float, float, float, float]] = []
    for item in payload:
        ts = datetime.fromtimestamp(int(item[0]), tz=UTC)
        low, high, open_, close, volume = (
            float(item[1]),
            float(item[2]),
            float(item[3]),
            float(item[4]),
            float(item[5]),
        )
        rows.append((ts, open_, high, low, close, volume))
    return rows_to_bars(symbol, rows)


def parse_binance_klines(symbol: str, payload: list) -> list[Bar]:
    rows: list[tuple[datetime, float, float, float, float, float]] = []
    for item in payload:
        open_ms = int(item[0])
        ts = datetime.fromtimestamp(open_ms / 1000.0, tz=UTC)
        rows.append(
            (
                ts,
                float(item[1]),
                float(item[2]),
                float(item[3]),
                float(item[4]),
                float(item[5]),
            )
        )
    return rows_to_bars(symbol, rows)


def _http_get_json(url: str, timeout: float = 30.0) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "scalping-bot-backtest/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def fetch_coinbase_candles(
    symbol: str,
    *,
    interval: str,
    start: datetime,
    end: datetime,
    pause_s: float = 0.12,
) -> list[Bar]:
    product = coinbase_product(symbol)
    key = interval.strip().lower()
    granularity = COINBASE_GRANULARITY.get(key)
    if granularity is None:
        raise ValueError(f"Unsupported Coinbase interval {interval!r}")
    # Public endpoint caps each response (~300 candles). Page forward in time.
    window = timedelta(seconds=granularity * 280)
    cursor = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    out: list[Bar] = []
    while cursor < end_utc:
        chunk_end = min(cursor + window, end_utc)
        params = urllib.parse.urlencode(
            {
                "granularity": granularity,
                "start": cursor.isoformat().replace("+00:00", "Z"),
                "end": chunk_end.isoformat().replace("+00:00", "Z"),
            }
        )
        url = COINBASE_CANDLES.format(product=product) + f"?{params}"
        try:
            payload = _http_get_json(url)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Coinbase HTTP {exc.code} for {product}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Coinbase network error for {product}: {exc.reason}") from exc
        if not isinstance(payload, list) or not payload:
            cursor = chunk_end + timedelta(seconds=granularity)
            continue
        out.extend(parse_coinbase_candles(symbol, payload))
        cursor = chunk_end + timedelta(seconds=granularity)
        time.sleep(pause_s)
    uniq: dict[datetime, Bar] = {}
    for bar in out:
        uniq[bar.timestamp] = bar
    return [uniq[k] for k in sorted(uniq)]


def fetch_binance_klines(
    symbol: str,
    *,
    interval: str,
    start: datetime,
    end: datetime,
    pause_s: float = 0.15,
) -> list[Bar]:
    pair = binance_symbol(symbol)
    if pair is None:
        raise ValueError(f"No Binance mapping for {symbol}")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out: list[Bar] = []
    cursor = start_ms
    while cursor < end_ms:
        params = urllib.parse.urlencode(
            {
                "symbol": pair,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            }
        )
        url = f"{BINANCE_KLINES}?{params}"
        try:
            payload = _http_get_json(url)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Binance HTTP {exc.code} for {pair}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Binance network error for {pair}: {exc.reason}") from exc
        if not isinstance(payload, list) or not payload:
            break
        chunk = parse_binance_klines(symbol, payload)
        if not chunk:
            break
        out.extend(chunk)
        last_open_ms = int(payload[-1][0])
        nxt = last_open_ms + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(payload) < 1000:
            break
        time.sleep(pause_s)
    # De-duplicate timestamps (page overlap).
    uniq: dict[datetime, Bar] = {}
    for bar in out:
        uniq[bar.timestamp] = bar
    return [uniq[k] for k in sorted(uniq)]


def _as_date_str(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def _yahoo_download(
    ticker: str,
    interval: str,
    *,
    period: str | None = None,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
):
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            "yfinance is required for stock/ETF history. Install with pip install -e ."
        ) from exc
    kwargs: dict = {
        "interval": interval,
        "auto_adjust": True,
        "prepost": False,
        "progress": False,
        "threads": False,
    }
    if start is not None:
        kwargs["start"] = _as_date_str(start)
        if end is not None:
            kwargs["end"] = _as_date_str(end)
    else:
        kwargs["period"] = period or "1mo"
    return yf.download(ticker, **kwargs)


def _frame_to_rows(frame) -> list[tuple[datetime, float, float, float, float, float]]:
    import pandas as pd

    if frame is None or getattr(frame, "empty", True):
        return []
    df = frame
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        try:
            df.columns = df.columns.droplevel(-1)
        except (ValueError, KeyError):
            df.columns = [
                str(col[0]).title() if isinstance(col, tuple) else str(col) for col in df.columns
            ]
        df.columns = [str(c).title() for c in df.columns]
    cols = {str(c).title(): c for c in df.columns}
    need = ["Open", "High", "Low", "Close"]
    if not all(name in cols or name in df.columns for name in need):
        # yfinance sometimes returns lowercase
        cols = {str(c).capitalize(): c for c in df.columns}
    rows: list[tuple[datetime, float, float, float, float, float]] = []
    for idx, rec in df.iterrows():
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if not isinstance(ts, datetime):
            continue
        try:
            open_ = float(rec[cols.get("Open", "Open")])
            high = float(rec[cols.get("High", "High")])
            low = float(rec[cols.get("Low", "Low")])
            close = float(rec[cols.get("Close", "Close")])
            vol_col = cols.get("Volume", "Volume")
            if "Volume" in cols or "Volume" in rec.index:
                volume = float(rec[vol_col])
            else:
                volume = 0.0
        except (TypeError, ValueError, KeyError):
            continue
        if not (open_ > 0 and high > 0 and low > 0 and close > 0):
            continue
        rows.append((ts, open_, high, low, close, volume))
    return rows


def fetch_yahoo_bars(
    symbol: str,
    *,
    interval: str,
    period: str | None = None,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
) -> list[Bar]:
    ticker = yahoo_ticker(symbol)
    frame = _yahoo_download(ticker, interval, period=period, start=start, end=end)
    rows = _frame_to_rows(frame)
    return rows_to_bars(symbol, rows)


def _cache_path(cache_dir: Path, symbol: str, interval: str, days: int) -> Path:
    safe = normalize_symbol(symbol).replace("/", "-")
    return cache_dir / f"{safe}_{interval}_{days}d.json"


def _bars_to_json(bars: list[Bar]) -> list[dict]:
    return [
        {
            "timestamp": bar.timestamp.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]


def _bars_from_json(symbol: str, payload: list[dict]) -> list[Bar]:
    rows = []
    for item in payload:
        ts = datetime.fromisoformat(item["timestamp"])
        rows.append(
            (
                ts,
                float(item["open"]),
                float(item["high"]),
                float(item["low"]),
                float(item["close"]),
                float(item["volume"]),
            )
        )
    return rows_to_bars(symbol, rows)


def load_cached(cache_dir: Path, symbol: str, interval: str, days: int) -> list[Bar] | None:
    path = _cache_path(cache_dir, symbol, interval, days)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _bars_from_json(symbol, payload["bars"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def save_cached(
    cache_dir: Path, symbol: str, interval: str, days: int, bars: list[Bar]
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, symbol, interval, days)
    path.write_text(
        json.dumps({"symbol": symbol, "interval": interval, "bars": _bars_to_json(bars)}),
        encoding="utf-8",
    )


def _crypto_yahoo_period(days: int, interval: str) -> str:
    if interval == "1m":
        return f"{min(days + 1, YAHOO_1M_MAX_CALENDAR_DAYS)}d"
    if interval == "1d":
        return "5y" if days > 400 else "2y"
    return f"{min(days + 2, YAHOO_5M_MAX_CALENDAR_DAYS)}d"


def _fetch_crypto_bars(
    symbol: str,
    *,
    yf_interval: str,
    days: int,
    start: datetime,
    end: datetime,
) -> tuple[list[Bar], str, str]:
    errors: list[str] = []
    try:
        if yf_interval == "1d":
            bars = fetch_yahoo_bars(symbol, interval=yf_interval, start=start, end=end)
        else:
            period = _crypto_yahoo_period(days, yf_interval)
            bars = fetch_yahoo_bars(symbol, period=period, interval=yf_interval)
        if len(bars) >= 30:
            return (
                bars,
                "yfinance",
                f"Yahoo {yahoo_ticker(symbol)} {yf_interval} ({len(bars)} bars).",
            )
        errors.append(f"yfinance too few bars ({len(bars)})")
    except Exception as exc:  # noqa: BLE001 - provider fallback
        errors.append(f"yfinance: {exc}")

    try:
        bars = fetch_coinbase_candles(symbol, interval=yf_interval, start=start, end=end)
        if len(bars) >= 30:
            return (
                bars,
                "coinbase",
                f"Coinbase public {yf_interval} USD candles for {coinbase_product(symbol)} "
                f"({len(bars)} bars). Yahoo skipped ({'; '.join(errors)}).",
            )
        errors.append(f"coinbase too few bars ({len(bars)})")
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"coinbase: {exc}")

    try:
        bars = fetch_binance_klines(symbol, interval=yf_interval, start=start, end=end)
        return (
            bars,
            "binance",
            f"Binance public {yf_interval} klines for {binance_symbol(symbol)} "
            f"(USDT as USD proxy; {len(bars)} bars). Earlier sources: {'; '.join(errors)}.",
        )
    except (RuntimeError, ValueError) as exc:
        errors.append(f"binance: {exc}")
        raise RuntimeError(
            f"Could not fetch crypto history for {symbol}: " + "; ".join(errors)
        ) from exc


def fetch_symbol_history(
    symbol: str,
    *,
    days: int,
    interval: IntervalChoice,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    now: datetime | None = None,
) -> SymbolHistory:
    symbol = normalize_symbol(symbol)
    asset = infer_asset_class(symbol)
    yf_interval = yf_interval_for(interval.minutes)
    warmup_days = warmup_calendar_days(interval)
    cache_interval = f"{yf_interval}_w{warmup_days}"
    if cache_dir is not None and use_cache:
        cached = load_cached(cache_dir, symbol, cache_interval, days)
        if cached:
            return SymbolHistory(
                symbol=symbol,
                bars=cached,
                source="cache",
                interval_minutes=interval.minutes,
                note=f"Loaded {len(cached)} bars from cache.",
            )

    end = now or datetime.now(tz=UTC)
    start = end - timedelta(days=days + warmup_days)

    if asset is AssetClass.CRYPTO:
        bars, source, note = _fetch_crypto_bars(
            symbol,
            yf_interval=yf_interval,
            days=days + warmup_days,
            start=start,
            end=end,
        )
    elif yf_interval == "1d":
        # yfinance `end` is exclusive.
        bars = fetch_yahoo_bars(
            symbol,
            interval=yf_interval,
            start=start,
            end=end + timedelta(days=1),
        )
        source = "yfinance"
        note = f"Yahoo Finance {yahoo_ticker(symbol)} {yf_interval} ({len(bars)} bars)."
    else:
        if interval.minutes == 1:
            period = f"{min(days + warmup_days, YAHOO_1M_MAX_CALENDAR_DAYS)}d"
        else:
            period = f"{min(days + warmup_days, YAHOO_5M_MAX_CALENDAR_DAYS)}d"
        bars = fetch_yahoo_bars(symbol, period=period, interval=yf_interval)
        source = "yfinance"
        note = f"Yahoo Finance {yahoo_ticker(symbol)} {yf_interval} ({len(bars)} bars, RTH)."

    if interval.minutes >= 1440:
        bars = rebase_daily_bars(bars)
    cutoff = (end - timedelta(days=days + warmup_days)).astimezone(ET)
    bars = [b for b in bars if b.timestamp >= cutoff]
    note = (
        f"{note} Kept {len(bars)} bars after window cutoff "
        f"(includes {warmup_days}d indicator warmup)."
    )
    if cache_dir is not None and bars:
        save_cached(cache_dir, symbol, cache_interval, days, bars)
    return SymbolHistory(
        symbol=symbol,
        bars=bars,
        source=source,
        interval_minutes=interval.minutes,
        note=note,
    )


def fetch_bundle(
    symbols: list[str],
    *,
    days: int,
    requested_interval: str = "auto",
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
    use_cache: bool = True,
    now: datetime | None = None,
) -> HistoryBundle:
    interval = choose_interval(days, requested_interval)
    notes = [interval.note]
    by_symbol: dict[str, SymbolHistory] = {}
    dropped: list[str] = []
    for raw in symbols:
        symbol = normalize_symbol(raw)
        hist = fetch_symbol_history(
            symbol,
            days=days,
            interval=interval,
            cache_dir=cache_dir,
            use_cache=use_cache,
            now=now,
        )
        min_keep = 80 if interval.minutes >= 1440 else 30
        if len(hist.bars) < min_keep:
            dropped.append(symbol)
            notes.append(
                f"{symbol}: skipped ({hist.source}: {len(hist.bars)} bars, "
                f"need >= {min_keep}). {hist.note}"
            )
            continue
        by_symbol[symbol] = hist
        notes.append(f"{symbol}: {hist.note}")
    if not by_symbol:
        raise RuntimeError(
            "No symbols returned enough historical bars. Check network access to "
            "Yahoo Finance / Binance, or pass a shorter --days window."
        )
    if dropped:
        notes.append("Dropped: " + ", ".join(dropped))
    return HistoryBundle(
        symbols=list(by_symbol),
        by_symbol=by_symbol,
        interval=interval,
        notes=notes,
    )
