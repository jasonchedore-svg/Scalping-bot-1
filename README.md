# Scalping / day-trading bot (crypto + US stocks)

**Educational software and paper-trading toolkit. Not financial advice, not an offer to trade, and not a regulated product.** Past or simulated results do not predict live results. You can lose money. Default mode is **paper trading**. Live trading is **off** unless you explicitly unlock every safety gate.

This repo is a runnable Python bot aimed at **short-hold scalps** (minutes, typically under 20) and **same-day** trades on:

- **US stocks** (regular session, flattened before the close)
- **Crypto** on Alpaca pairs such as `BTC/USD` (24/7)

One engine loop handles both via a `Broker` interface.

## Quick start (no API keys)

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
scalping-bot demo --minutes 90 --symbols AAPL,BTC/USD
```

The demo uses an in-process paper simulator (random-walk 1-minute bars, slippage, crypto fees). It never contacts a broker.

## Alpaca paper account (free)

1. Create a free account at [Alpaca](https://app.alpaca.markets/signup).
2. Open the **Paper** dashboard: [paper overview](https://app.alpaca.markets/paper/dashboard/overview).
3. Generate **paper** API keys (not live keys).
4. Copy `.env.example` to `.env` and fill:

```bash
cp .env.example .env
```

```
PAPER=true
ALLOW_LIVE_TRADING=false
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
WATCHLIST=AAPL,MSFT,NVDA,SPY,QQQ,BTC/USD,ETH/USD
```

5. Run against Alpaca **paper** (still simulated fills at the broker; no real money):

```bash
scalping-bot start
# or
scalping-bot start --dry-run
```

Paper keys on the **live** URL (or live keys on the paper URL) will not do what you expect. Keep `PAPER=true` and `ALPACA_BASE_URL=https://paper-api.alpaca.markets`.

Free paper accounts typically receive **IEX** stock data. Crypto data is available on the same Alpaca account.

## CLI

| Command | What it does |
| --- | --- |
| `scalping-bot demo` | Local simulator, no keys |
| `scalping-bot start` | Foreground bot loop (paper default) |
| `scalping-bot start --dry-run` | Signals + risk, no orders |
| `scalping-bot start --simulator` | Force local simulator even if keys exist |
| `scalping-bot stop` | SIGTERM via `data/bot.pid` |
| `scalping-bot status` | Mode, kill-switch, trade counts |
| `scalping-bot positions` | Tracked scalps (+ broker positions if keyed) |
| `scalping-bot pnl` | Persisted daily P&L JSON |
| `scalping-bot watchlist` | Configured symbols |
| `scalping-bot dry-run` | Short dry-run loop |

`start --live` is **not** enough. See [Live trading (hard-gated)](#live-trading-hard-gated).

## Strategy: `momentum_scalp` (default)

Pluggable `Strategy` interface; this is the shipped starter. **Long-only.** No short selling, no margin, no options.

**Timeframe:** 1-minute bars. Optional 5-minute EMA trend filter (on by default; 5-minute bars are resampled from 1-minute).

### Entry (all must be true)

1. Enough history to compute EMA(21), RSI(14), ATR(14), volume SMA(20).
2. EMA(9) > EMA(21) on 1-minute closes.
3. Last close > session/rolling VWAP.
4. RSI(14) between **45 and 70**.
5. Last 1-minute bar is bullish (`close > open`).
6. Last three 1-minute closes are strictly rising.
7. Last bar volume > **1.15 ×** 20-bar volume SMA.
8. ATR > 0 so stops can be placed.
9. If the 5-minute filter is on: EMA(9) > EMA(21) on 5-minute bars.
10. Flat in that symbol (no pyramid).

Stocks only: regular session, after 09:32 America/New_York, and **no new entries after 15:40 ET**.

### Exit (first match)

| Rule | Default |
| --- | --- |
| Stop-loss | Last low/close at or below stop |
| Take-profit | Last high/close at or above target |
| Time stop | 20 minutes in the trade |
| Momentum fade | After 5 minutes: RSI ≥ 75 **or** close < EMA(9) |
| Session flatten | Stocks flattened at **15:50 ET** |
| Kill-switch | Daily loss cap → flatten everything |

After price reaches **+1R**, the engine trails the stop up to **breakeven**.

### Scalp stop / target clamps

```
raw_stop = ATR(14) × 1.0
stop     = clamp(raw_stop, 0.08% of price, 0.40% of price)
raw_tp   = ATR(14) × 1.5
target   = clamp(raw_tp,  0.12% of price, 0.60% of price)
```

Those bands are for **minutes-long** scalps, not swing trades.

To add another strategy: implement `Strategy.evaluate`, decorate with `@register_strategy`, set `STRATEGY=your_name`.

## Risk manager

Every **new** order is gated. Risk-reducing exits are always allowed.

| Limit | Default | Behavior |
| --- | --- | --- |
| Max position notional | `$2,000` | Cap per symbol |
| Max position % of equity | `5%` | Further cap |
| Risk per trade | `0.40%` of equity | Size = risk budget / stop distance |
| Max daily loss | `2%` of start-of-day equity | **Kill-switch**: flatten, block entries |
| Max open positions | `3` | Across stocks + crypto |
| Max trades / day | `20` | Counted on entries |
| Per-symbol cooldown | `90s` | After any fill on that symbol |
| Stop + take-profit | required | Missing OCO → refuse |
| Stop width | ≤ ~0.50% | Rejects wide (non-scalp) stops |
| Short / margin / options | disabled | Cash long-only |
| Cash | required | Will not size above cash |

Orders that fail a check are logged as `order_refused` and **not** sent.

## Execution

- **Alpaca** adapter when `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set: stocks + crypto, paper URL by default, IEX bars for stocks, optional broker-side bracket (take-profit + stop) with engine backup.
- **Local paper simulator** when keys are missing or `--simulator` / `FORCE_SIMULATOR=true`: synthetic 1-minute bars, fill at last close with slippage (2 bps stocks / 4 bps crypto) and a small crypto fee.

The engine never retries order **submits** (duplicate-fill risk). Reads may fail and be retried on the next loop.

State is stored in SQLite under `data/bot_state.db` (gitignored): open scalps, daily P&L, kill-switch, trade log. Structured logs via `structlog` (`LOG_JSON=true` for JSON).

## Live trading (hard-gated)

Live is **off**. All of the following are required or the process exits:

1. `PAPER=false`
2. `ALLOW_LIVE_TRADING=true`
3. CLI `--live`
4. `ALPACA_BASE_URL` is the **live** API (`https://api.alpaca.markets`), not `paper-api`
5. Live API keys present

```bash
# This is refused by default:
scalping-bot start --live
```

Do not use this software with real money unless you understand order routing, PDT rules (~$25k for unlimited US equity day trades), crypto fees, slippage, and operational risk. **This project is not a recommendation to trade live.**

## Configuration

See `.env.example`. Important defaults:

| Variable | Default |
| --- | --- |
| `PAPER` | `true` |
| `ALLOW_LIVE_TRADING` | `false` |
| `WATCHLIST` | `AAPL,MSFT,NVDA,SPY,QQQ,BTC/USD,ETH/USD` |
| `POLL_INTERVAL_SECONDS` | `10` |
| `BAR_TIMEFRAME` | `1Min` |
| `STRATEGY` | `momentum_scalp` |
| `MAX_HOLD_MINUTES` | `20` |
| `DRY_RUN` | `false` |

Never commit `.env` or API keys.

## Tests

```bash
pytest
pytest --cov=scalping_bot
```

Coverage includes indicator math, exact strategy entry/exit, every major risk gate, the simulator, the live-mode lock, and a no-keys CLI demo.

## Layout

```
src/scalping_bot/
  broker/       Alpaca + local simulator (same Broker ABC)
  strategy/     Pluggable strategies (momentum_scalp)
  risk/         Hard limits / kill-switch
  engine/       Single loop for stocks and crypto
  state/        SQLite persistence
  cli.py        Typer CLI
```

## Disclaimer (read this)

This repository is for **education, research, and paper trading**. It is **not** financial, investment, tax, or legal advice. The authors and contributors are **not** brokers, advisers, or fiduciaries. Markets gap; paper fills are not live fills; bugs happen. You are solely responsible for any use of this code, including losses. If you do not agree, do not run the bot.
