# Scalping / day-trading bot (crypto + US stocks)

**Educational software and paper-trading toolkit. Not financial advice, not an offer to trade, and not a regulated product.** Past or simulated results do not predict live results. You can lose money. Default mode is **paper trading**. Live trading is **off** unless you explicitly unlock every safety gate.

This repo is a runnable Python bot aimed at **short-hold scalps** (minutes, typically under 20) and **same-day** trades on:

- **US stocks** (regular session, flattened before the close)
- **Crypto** pairs such as `BTC/USD` (24/7)

One engine loop handles both via a `Broker` interface.

## Canada / no Alpaca

**Alpaca does not onboard Canadian residents.** If you are in Canada (or anywhere Alpaca is unavailable):

| What to use | What it is |
| --- | --- |
| `scalping-bot demo` | Local **synthetic** random-walk (not a backtest). No keys. |
| `scalping-bot backtest` | **Historical** replay from Yahoo Finance + public crypto candles. No broker keys. |
| Alpaca paper/live | Not an option. Do not expect a Canadian Alpaca account. |
| Interactive Brokers | **Not implemented yet.** A future IBKR adapter could paper/live-trade; live gates stay hard-locked until then. |

Paper/live safety gates are unchanged. The local demo remains. Backtests never send broker orders.

## Quick start (no API keys)

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
scalping-bot demo --minutes 90 --symbols AAPL,BTC/USD
scalping-bot backtest --days 7 --symbols SPY,QQQ,AAPL,BTC/USD,ETH/USD
```

The demo uses an in-process paper simulator (random-walk 1-minute bars, slippage, crypto fees). It never contacts a broker.

The backtest CLI downloads free historical bars (no Alpaca/IBKR keys), then **replays them through the same Strategy + RiskManager** as paper/live.

Default backtest window: **7 calendar days**, **1-minute** bars, stocks+ETFs+crypto, `$100,000` starting equity.

```bash
# Longer window: Yahoo 1-minute US equity history is ~7 days, so this falls back to 5-minute bars
scalping-bot backtest --days 30
scalping-bot backtest --days 7 --strategy momentum_reclaim
scalping-bot backtest --source simulator --days 5 --seed 7   # synthetic; --seed applies here only
```

## Historical backtest (no broker keys)

```bash
scalping-bot backtest \
  --symbols SPY,QQQ,AAPL,BTC/USD,ETH/USD \
  --days 7 \
  --starting-equity 100000 \
  --interval auto \
  --strategy momentum_scalp
```

`--seed` is **ignored** for historical replay (fills are deterministic given the bars). It only seeds `--source simulator`.

### Data sources

| Asset | Source | Notes |
| --- | --- | --- |
| US stocks/ETFs | [yfinance](https://pypi.org/project/yfinance/) (Yahoo Finance) | No API key. Regular-session bars (`prepost=False`). |
| Crypto (`BTC/USD`, `ETH/USD`, …) | Yahoo `BTC-USD` / `ETH-USD` first | No API key. |
| Crypto fallback | Coinbase public USD candles | No API key. Used if Yahoo is empty. |
| Crypto last resort | Binance public klines (`BTCUSDT` as a USD proxy) | Often **HTTP 451** / geo-blocked. |

Bars are cached under `data/cache/` (gitignored). Pass `--no-cache` to refetch.

### 1-minute vs 5-minute (be honest)

Yahoo Finance typically keeps only **~7 calendar days** of **1-minute** US stock/ETF bars. Crypto 1-minute history from Yahoo is also short-window.

| `--days` | `--interval auto` | What you actually test |
| --- | --- | --- |
| 1–7 | **1-minute** | Same bar size the live scalp rules assume. |
| 8–30 (up to 60) | **5-minute** | Coarser. EMA(9)/RSI(14)/ATR(14) span more clock time. Max hold is still 20 **minutes** (four 5-minute bars). The 5-minute trend filter becomes a **15-minute** filter so it stays a higher timeframe. |

`--interval 1m` with `--days 30` **falls back to 5-minute** so stocks and crypto share one clock. This is not a 1-minute scalp; the report prints that note.

Do not treat `scalping-bot demo` as a backtest. Its bars are a **seeded random walk with occasional momentum bursts** so the demo can fire. `--source simulator` on `backtest` uses that same generator but prints the full trade/drawdown report.

### Report

Printed after every run:

- trades, wins/losses, win rate
- average win / average loss
- max drawdown (from the mark-to-market equity curve)
- total P&L and realized P&L
- fee/slippage assumptions and amounts actually charged
- per-symbol breakdown

**Cost model (same as the local demo):** stocks **2 bps** slippage per fill; crypto **4 bps** slippage + **15 bps** fee per fill. No Alpaca commission model.

Replay uses the same position caps, daily-loss kill-switch, cooldowns, and session flatten as paper/live. Risk gates are **not** loosened for backtests.

## Alpaca paper account (free, where Alpaca is available)

Canadian residents: skip this section; use demo/backtest (and IBKR later).

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
| `scalping-bot demo` | Local simulator, no keys (synthetic bars) |
| `scalping-bot backtest` | Historical replay, no keys (Yahoo/public crypto) |
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

**Timeframe:** 1-minute bars. Optional 5-minute EMA trend filter (on by default; 5-minute bars are resampled from 1-minute). On a 5-minute backtest the higher-timeframe filter is 15-minute.

### Entry (all must be true)

1. Enough history to compute EMA(21), RSI(14), ATR(14), volume SMA(20).
2. EMA(9) > EMA(21) on primary-bar closes.
3. Last close > session/rolling VWAP.
4. RSI(14) between **45 and 70**.
5. Last bar is bullish (`close > open`).
6. Last three closes are strictly rising.
7. Last bar volume > **1.15 ×** 20-bar volume SMA.
8. ATR > 0 so stops can be placed.
9. If the higher-timeframe filter is on: EMA(9) > EMA(21) on 5-minute (or 15-minute when primary bars are already 5-minute).
10. Flat in that symbol (no pyramid).
11. **Take-profit distance covers 1.1× modeled round-trip costs** (stocks ~4 bps; crypto ~38 bps = 15 bps fee + 4 bps slip, both sides). Crypto 1-minute scalps with 12–40 bps targets usually fail this gate.

Stocks only: regular session, after 09:32 America/New_York, and **no new entries after 15:40 ET**.

### Exit (first match)

| Rule | Default |
| --- | --- |
| Stop-loss | Last low/close at or below stop |
| Take-profit | Last high/close at or above target |
| Time stop | 20 minutes in the trade |
| Momentum fade | After 5 minutes: RSI ≥ 75 **or** close < EMA(9) |
| Session flatten | Stocks flattened at **15:50 ET** (and if the session is already closed) |
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

### Variant: `momentum_reclaim`

Use `--strategy momentum_reclaim` (or `STRATEGY=momentum_reclaim`). Same stops, VWAP, higher-timeframe filter, exits, and cost gate. Differences:

- RSI 50–65 (avoid extended pops)
- Volume spike 1.30×
- **No** 3-bar chase. Prior bar must **tag EMA(9)** (low ≤ EMA); last bar **reclaims** it (bullish close back above EMA)

Added because 7-day 1-minute history showed the 3-bar chase buying extensions into tight stops. On that window reclaim did **not** outperform after the cost gate; it is kept as a named alternative, not the default.

To add another strategy: implement `Strategy.evaluate`, decorate with `@register_strategy`, set `STRATEGY=your_name`.

## What the backtests showed (actual runs)

Same universe `SPY,QQQ,AAPL,BTC/USD,ETH/USD`, starting equity `$100,000`, unchanged risk caps. Historical bars from Yahoo (cached after the first download). **Not a prediction of live results.**

### Before the cost gate (3-bar chase, crypto allowed)

| Run | Trades | Win rate | Total P&L | Fees paid | Max DD |
| --- | --- | --- | --- | --- | --- |
| Simulator 5d, seed=7, `momentum_scalp` | 40 | 62.5% | **−$151** | $174 | 0.15% |
| Historical 7d **1m**, `momentum_scalp` | 127 | 20.5% | **−$839** | $618 | 0.84% |
| Historical 30d **5m**, `momentum_scalp` | 328 | 30.5% | **−$2,132** | $1,710 | 2.13% |

Crypto dominated trade count. Round-trip crypto cost (~38 bps) often exceeded the 12–60 bps scalp target, so “wins” still lost after fees.

### After: skip entries whose target cannot cover 1.1× modeled round-trip costs

| Run | Trades | Win rate | Total P&L | Fees paid | Max DD |
| --- | --- | --- | --- | --- | --- |
| Simulator 5d, seed=7, `momentum_scalp` | 40 | 57.5% | **+$19** | $0 | 0.01% |
| Historical 7d **1m**, `momentum_scalp` | 44 | 20.5% | **−$37** | $0 | 0.04% |
| Historical 7d **1m**, `momentum_reclaim` | 32 | 21.9% | **−$37** | $0 | 0.04% |
| Historical 30d **5m**, `momentum_scalp` | 81 | 37.0% | **−$295** | $222 | 0.30% |

On 7-day 1-minute data the cost gate **eliminated crypto entries** (targets too small). Equities still had no edge in that week (−$37 on $100k). Risk limits never fired a kill-switch in these runs.

**Conclusion:** this starter scalp is **not** a proven edge on recent Yahoo history. Use it to study the engine, not to size live risk.

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

- **Alpaca** adapter when `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set: stocks + crypto, paper URL by default, IEX bars for stocks, optional broker-side bracket (take-profit + stop) with engine backup. **Not available to Canadian residents.**
- **Local paper simulator** when keys are missing or `--simulator` / `FORCE_SIMULATOR=true`: synthetic 1-minute bars, fill at last close with slippage (2 bps stocks / 4 bps crypto) and a small crypto fee.
- **Historical replay** (`scalping-bot backtest`): recorded Yahoo/public bars, **same fill model** as the simulator.

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

Coverage includes indicator math, exact strategy entry/exit (including `momentum_reclaim` and the crypto cost gate), every major risk gate, the simulator, historical replay plumbing, the live-mode lock, a no-keys CLI demo, and CLI backtest help.

## Layout

```
src/scalping_bot/
  backtest/     Yahoo/public fetch, historical broker, report, runner
  broker/       Alpaca + local simulator + shared paper ledger
  strategy/     Pluggable strategies (momentum_scalp, momentum_reclaim)
  risk/         Hard limits / kill-switch
  engine/       Single loop for stocks and crypto
  state/        SQLite persistence
  cli.py        Typer CLI
```

## Disclaimer (read this)

This repository is for **education, research, and paper trading**. It is **not** financial, investment, tax, or legal advice. The authors and contributors are **not** brokers, advisers, or fiduciaries. Markets gap; paper fills are not live fills; historical Yahoo/Coinbase bars are not your live quotes; bugs happen. You are solely responsible for any use of this code, including losses. If you do not agree, do not run the bot.
