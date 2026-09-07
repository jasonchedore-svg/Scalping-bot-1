# Trading bot (swing recommended; scalp educational)

**Educational software and paper-trading toolkit. Not financial advice, not an offer to trade, and not a regulated product.** Past or simulated results do not predict live results. You can lose money. Default mode is **paper trading**. Live trading is **off** unless you explicitly unlock every safety gate.

**Recommended path:** `trend_swing` on **daily bars** (hold days to ~2–3 weeks). Long-only, no margin, no shorts, no options.

**Legacy / educational default:** `momentum_scalp` (minutes-long 1-minute scalps). Historical replay on recent Yahoo data showed **no edge** after costs. The scalp/demo/backtest code is kept; do not use it as a live system.

One engine loop handles US stocks/ETFs and crypto via a `Broker` interface.

## Canada / no Alpaca

**Alpaca does not onboard Canadian residents.** If you are in Canada (or anywhere Alpaca is unavailable):

| What to use | What it is |
| --- | --- |
| `scalping-bot swing-backtest` | **Recommended.** Daily-bar historical replay (Yahoo + public crypto). No broker keys. |
| `scalping-bot backtest` | Historical replay (1m / 5m / 1d). No broker keys. |
| `scalping-bot demo` | Local **synthetic** random-walk (not a backtest). No keys. |
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
# Recommended: ~12 months of daily bars, no keys
scalping-bot swing-backtest --days 365 --symbols SPY,QQQ,AAPL,BTC/USD,ETH/USD
# Legacy educational scalp demo / short-window backtest
scalping-bot demo --minutes 90 --symbols AAPL,BTC/USD
scalping-bot backtest --days 7 --symbols SPY,QQQ,AAPL,BTC/USD,ETH/USD
```

`swing-backtest` downloads free **daily** history (no Alpaca/IBKR keys), warms SMA(200) with extra lookback, then **replays through the same Strategy + RiskManager** as paper/live, and prints a **SPY buy-and-hold** baseline for the same window.

## Paper-first checklist

Still paper-first. Live stays hard-gated. How you verify a paper run depends on style:

| | Scalp (legacy) | Swing (recommended) |
| --- | --- | --- |
| Bar size | 1-minute (5-minute if the window is longer than ~7 days) | **Daily** |
| Typical hold | minutes (flatten before the cash close) | **days to ~2–3 weeks** |
| Trades to study | many per session | **few** (often a handful per month on a 5-symbol book) |
| Calendar time | one session can fill a checklist | **weeks to months** of paper before you have a useful sample |
| Overnight | stocks flattened | **held overnight** (`hold_overnight`) |
| Config | `MODE=scalp` (default) | `MODE=swing` or `scalping-bot start --mode swing` |

A scalp-style “20 trades today” checklist **does not apply** to swing. If you paper `trend_swing`, expect long quiet stretches and judge it over a multi-month window, not a single session.

## Historical backtest (no broker keys)

```bash
# Swing (recommended)
scalping-bot swing-backtest --days 365 --starting-equity 100000

# Same thing via the generic command
scalping-bot backtest --mode swing --strategy trend_swing --interval 1d --days 365

# Legacy scalp window (Yahoo 1-minute US equity history is ~7 days)
scalping-bot backtest --days 7 --strategy momentum_scalp
scalping-bot backtest --days 30 --interval 5m
scalping-bot backtest --source simulator --days 5 --seed 7   # synthetic; --seed applies here only
```

`--seed` is **ignored** for historical replay (fills are deterministic given the bars). It only seeds `--source simulator`.

### Data sources

| Asset | Source | Notes |
| --- | --- | --- |
| US stocks/ETFs | [yfinance](https://pypi.org/project/yfinance/) (Yahoo Finance) | No API key. Regular-session / daily bars (`prepost=False`). |
| Crypto (`BTC/USD`, `ETH/USD`, …) | Yahoo `BTC-USD` / `ETH-USD` first | No API key. |
| Crypto fallback | Coinbase public USD candles | No API key. Used if Yahoo is empty. |
| Crypto last resort | Binance public klines (`BTCUSDT` as a USD proxy) | Often **HTTP 451** / geo-blocked. |

Bars are cached under `data/cache/` (gitignored). Pass `--no-cache` to refetch.

Daily swing fetches **~400 extra calendar days** of warmup so SMA(200) is live at the start of `--days`.

### Interval honesty

| `--days` | `--interval auto` | What you actually test |
| --- | --- | --- |
| 1–7 | **1-minute** | Same bar size the legacy scalp rules assume. |
| 8–59 | **5-minute** | Coarser scalp test. EMA/RSI/ATR span more clock time. |
| 60+ | **1-day** | Swing / multi-month. Use `swing-backtest` for the full swing risk profile. |

`--interval 1m` with `--days 30` **falls back to 5-minute** so stocks and crypto share one clock. `--interval 1d` is always daily.

Do not treat `scalping-bot demo` as a backtest. Its bars are a **seeded random walk with occasional momentum bursts** so the demo can fire.

### Report

Printed after every run:

- trades, wins/losses, win rate
- average win / average loss
- max drawdown (from the mark-to-market equity curve)
- total P&L and realized P&L
- fee/slippage assumptions and amounts actually charged
- per-symbol breakdown
- **SPY buy-and-hold** over the same window (honest baseline)

**Cost model (same as the local demo):** stocks **2 bps** slippage per fill; crypto **4 bps** slippage + **15 bps** fee per fill. No Alpaca commission model.

Replay uses the same position caps, daily-loss kill-switch, and cooldowns as paper/live. Swing mode uses a **wider stop profile**; it does **not** reuse the scalp 0.5% stop-width reject. Live safety gates are **not** loosened.

## Strategy: `trend_swing` (recommended)

Long-only swing on **daily** bars. Hold **days to ~15 trading days** (~2–3 weeks). No shorts, no margin, no options.

Set `MODE=swing` (or `scalping-bot start --mode swing` / `scalping-bot swing-backtest`). That applies `trend_swing`, `1Day` bars, overnight holds, and the swing risk profile.

### Entry (all must be true)

1. Enough history for SMA(50), SMA(200), ATR(14).
2. **Uptrend:** close > SMA50 and SMA50 > SMA200.
3. **Pullback:** in the last 10 bars a low tagged SMA50 (within 0.3%).
4. **Reclaim:** last bar bullish and close back above SMA50.
5. RSI(14) < 70 (not a climax).
6. ATR > 0; stop/target from ATR clamps.
7. Flat in that symbol (no pyramid).

Stocks: weekdays only (daily bars are treated as a cash session, not a 09:30–16:00 clock). No end-of-day flatten.

### Exit (first match)

| Rule | Default |
| --- | --- |
| Stop-loss | Last low/close at or below stop (~**2.0 × ATR**, clamped 2–12%) |
| Take-profit | Last high/close at or above target (~**3.0 × ATR**, clamped 3–30%) |
| Time stop | **15 trading days** |
| Trend break | After 3 days, close < SMA50 |
| Trailing | +1R → breakeven; +2R → lock +1R |
| Kill-switch | Daily loss cap → flatten everything |

### Swing risk profile (`MODE=swing`)

| Limit | Swing | Scalp (legacy default) |
| --- | --- | --- |
| Max position notional | `$15,000` | `$2,000` |
| Max position % of equity | 15% | 5% |
| Risk per trade | 0.75% of equity | 0.40% |
| Max daily loss | 3% | 2% |
| Max open positions | 4 | 3 |
| Max trades / day | **2** | 20 |
| Per-symbol cooldown | 1 day | 90s |
| Stop width reject | ≤ ~15% (1.25 × 12% clamp) | ≤ ~0.50% |
| Session flatten | off | 15:50 ET stocks |
| Overnight stock state | **kept** | dropped at the session boundary |

## Strategy: `momentum_scalp` (legacy educational default)

Kept so existing demos and short-window backtests still run. **Long-only.** No short selling, no margin, no options.

**Timeframe:** 1-minute bars. Optional 5-minute EMA trend filter (on by default).

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
11. **Take-profit distance covers 1.1× modeled round-trip costs** (stocks ~4 bps; crypto ~38 bps). Crypto 1-minute scalps with 12–40 bps targets usually fail this gate.

Stocks only: regular session, after 09:32 America/New_York, and **no new entries after 15:40 ET**. Flattened at **15:50 ET**.

### Scalp stop / target clamps

```
raw_stop = ATR(14) × 1.0
stop     = clamp(raw_stop, 0.08% of price, 0.40% of price)
raw_tp   = ATR(14) × 1.5
target   = clamp(raw_tp,  0.12% of price, 0.60% of price)
```

Those bands are for **minutes-long** scalps. Swing mode does **not** reuse them.

### Variant: `momentum_reclaim`

Use `--strategy momentum_reclaim`. Same scalp stops and cost gate; EMA tag + reclaim instead of a 3-bar chase. On the 7-day 1-minute window it did **not** outperform after the cost gate.

## What the backtests showed (actual runs)

Same universe `SPY,QQQ,AAPL,BTC/USD,ETH/USD`, starting equity `$100,000`. Historical bars from Yahoo (cached after the first download). **Not a prediction of live results.**

### Swing: `trend_swing` daily (recommended path)

Numbers from `scalping-bot swing-backtest --days 365` are recorded in the pull request summary after the run on this branch (and updated in this section when that run completes). Compare every swing result to **SPY buy-and-hold** over the same window — that is the honest baseline.

### Legacy scalp (why this repo pivoted)

#### Before the cost gate (3-bar chase, crypto allowed)

| Run | Trades | Win rate | Total P&L | Fees paid | Max DD |
| --- | --- | --- | --- | --- | --- |
| Simulator 5d, seed=7, `momentum_scalp` | 40 | 62.5% | **−$151** | $174 | 0.15% |
| Historical 7d **1m**, `momentum_scalp` | 127 | 20.5% | **−$839** | $618 | 0.84% |
| Historical 30d **5m**, `momentum_scalp` | 328 | 30.5% | **−$2,132** | $1,710 | 2.13% |

Crypto dominated trade count. Round-trip crypto cost (~38 bps) often exceeded the 12–60 bps scalp target.

#### After: skip entries whose target cannot cover 1.1× modeled round-trip costs

| Run | Trades | Win rate | Total P&L | Fees paid | Max DD |
| --- | --- | --- | --- | --- | --- |
| Simulator 5d, seed=7, `momentum_scalp` | 40 | 57.5% | **+$19** | $0 | 0.01% |
| Historical 7d **1m**, `momentum_scalp` | 44 | 20.5% | **−$37** | $0 | 0.04% |
| Historical 7d **1m**, `momentum_reclaim` | 32 | 21.9% | **−$37** | $0 | 0.04% |
| Historical 30d **5m**, `momentum_scalp` | 81 | 37.0% | **−$295** | $222 | 0.30% |

On 7-day 1-minute data the cost gate **eliminated crypto entries**. Equities still had no edge in that week (−$37 on $100k).

**Conclusion:** the starter scalp is **not** a proven edge on recent Yahoo history. Use it to study the engine. Prefer `trend_swing` + `swing-backtest` going forward.

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
MODE=swing
```

5. Run against Alpaca **paper** (still simulated fills at the broker; no real money):

```bash
scalping-bot start --mode swing
# or
scalping-bot start --dry-run --mode swing
```

Paper keys on the **live** URL (or live keys on the paper URL) will not do what you expect. Keep `PAPER=true` and `ALPACA_BASE_URL=https://paper-api.alpaca.markets`.

## CLI

| Command | What it does |
| --- | --- |
| `scalping-bot swing-backtest` | **Recommended.** Daily swing replay, no keys, SPY buy-and-hold baseline |
| `scalping-bot backtest` | Historical replay, no keys (Yahoo/public crypto) |
| `scalping-bot demo` | Local simulator, no keys (synthetic bars) |
| `scalping-bot start` | Foreground bot loop (paper default; `--mode swing` recommended) |
| `scalping-bot start --dry-run` | Signals + risk, no orders |
| `scalping-bot start --simulator` | Force local simulator even if keys exist |
| `scalping-bot stop` | SIGTERM via `data/bot.pid` |
| `scalping-bot status` | Mode, kill-switch, trade counts |
| `scalping-bot positions` | Tracked trades (+ broker positions if keyed) |
| `scalping-bot pnl` | Persisted daily P&L JSON |
| `scalping-bot watchlist` | Configured symbols |
| `scalping-bot dry-run` | Short dry-run loop |

`start --live` is **not** enough. See [Live trading (hard-gated)](#live-trading-hard-gated).

## Risk manager

Every **new** order is gated. Risk-reducing exits are always allowed. See the swing vs scalp table above for numeric caps.

Orders that fail a check are logged as `order_refused` and **not** sent. Short / margin / options stay disabled. Cash long-only.

## Execution

- **Alpaca** adapter when `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set: stocks + crypto, paper URL by default, IEX bars for stocks, optional broker-side bracket (take-profit + stop) with engine backup. **Not available to Canadian residents.**
- **Local paper simulator** when keys are missing or `--simulator` / `FORCE_SIMULATOR=true`: synthetic 1-minute bars, fill at last close with slippage (2 bps stocks / 4 bps crypto) and a small crypto fee.
- **Historical replay** (`scalping-bot backtest` / `swing-backtest`): recorded Yahoo/public bars, **same fill model** as the simulator.

The engine never retries order **submits** (duplicate-fill risk). Reads may fail and be retried on the next loop.

State is stored in SQLite under `data/bot_state.db` (gitignored). Swing keeps stock `open_trades` across calendar days; scalp still drops leftover stock rows at the session boundary.

## Live trading (hard-gated)

Live is **off**. All of the following are required or the process exits. **`MODE=swing` does not bypass any of this.**

1. `PAPER=false`
2. `ALLOW_LIVE_TRADING=true`
3. CLI `--live`
4. `ALPACA_BASE_URL` is the **live** API (`https://api.alpaca.markets`), not `paper-api`
5. Live API keys present

```bash
# This is refused by default (scalp or swing):
scalping-bot start --live
scalping-bot start --live --mode swing
```

Do not use this software with real money unless you understand order routing, PDT rules (~$25k for unlimited US equity day trades), overnight gap risk, crypto fees, slippage, and operational risk. **This project is not a recommendation to trade live.**

## Configuration

See `.env.example`. Important defaults:

| Variable | Default |
| --- | --- |
| `PAPER` | `true` |
| `ALLOW_LIVE_TRADING` | `false` |
| `MODE` | `scalp` (legacy educational default; set `swing` to recommend) |
| `WATCHLIST` | `AAPL,MSFT,NVDA,SPY,QQQ,BTC/USD,ETH/USD` |
| `POLL_INTERVAL_SECONDS` | `10` |
| `BAR_TIMEFRAME` | `1Min` (`1Day` when `MODE=swing`) |
| `STRATEGY` | `momentum_scalp` (`trend_swing` when `MODE=swing`) |
| `MAX_HOLD_MINUTES` | `20` (swing uses `MAX_HOLD_DAYS=15`) |
| `DRY_RUN` | `false` |

Never commit `.env` or API keys.

## Tests

```bash
pytest
pytest --cov=scalping_bot
```

Coverage includes indicator math, scalp and `trend_swing` entry/exit, swing vs scalp stop-width, overnight stock persistence, historical replay plumbing (including daily tradable hours), the live-mode lock (including `--mode swing`), a no-keys CLI demo, and CLI backtest / swing-backtest help.

## Layout

```
src/scalping_bot/
  backtest/     Yahoo/public fetch, historical broker, report, runner
  broker/       Alpaca + local simulator + shared paper ledger
  strategy/     momentum_scalp, momentum_reclaim, trend_swing
  risk/         Hard limits / kill-switch (swing-aware stop width)
  engine/       Single loop for stocks and crypto
  state/        SQLite persistence (optional overnight stock holds)
  cli.py        Typer CLI
```

## Disclaimer (read this)

This repository is for **education, research, and paper trading**. It is **not** financial, investment, tax, or legal advice. The authors and contributors are **not** brokers, advisers, or fiduciaries. Markets gap; paper fills are not live fills; historical Yahoo/Coinbase bars are not your live quotes; bugs happen. You are solely responsible for any use of this code, including losses. If you do not agree, do not run the bot.
