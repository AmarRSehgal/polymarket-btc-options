# Polymarket BTC 5-Minute Binary Options Edge Finder

Live edge detection for Polymarket's BTC 5-minute Up/Down markets using Black-Scholes binary option pricing against a real-time Binance volatility estimate.

> **Finding (2026-08-31): the model has no edge. What looks like edge is latency.**
> Measured over 300 settled windows, the strategy's return is a monotonic
> function of how fresh the Binance price is relative to the Polymarket quote --
> +19.9% ROI reading both at the same second, +10.7% with the BTC feed delayed
> 15s, and worse than simply believing the market price by 30s. The
> Black-Scholes machinery contributes nothing; the market's own price is better
> calibrated than the model (1.8pt mean error vs 3.2pt). See
> [Does the edge exist?](#does-the-edge-exist) before trusting any number this
> tool prints. It remains a simulator and places no real orders.

## How It Works

Every 5 minutes, Polymarket creates a new binary market: "Will BTC be higher or lower than the opening price at window close?" The Up token pays $1 if BTC finishes above the opening price, $0 otherwise. Down is the inverse.

**The markets still exist and deterministic slug discovery still works** --
`btc-updown-5m-{unix_ts}` with `ts % 300 == 0` resolves against the Gamma API,
verified 2026-08-31. Two things about them changed since this tool was written,
and both matter:

1. **Settlement is now a 60-second trailing TWAP**, not a point-in-time print.
   Markets carry `cryptoMarketConfig: {"id": "btc-5m-twap-60", "twapEnabled":
   true, "twapLookbackSeconds": 60}` and resolve off Chainlink's
   `btc-usd-twap-60s` stream. Markets from June 2026 have no such config, so
   this is new. Averaging the final minute cuts the variance of the settlement
   value, which makes the outcome *more* determined late in the window than a
   point-in-time model assumes.
2. **Taker fees are live**: `feeSchedule {"rate": 0.07, "exponent": 1,
   "takerOnly": true}`, charged as `shares * 0.07 * p * (1-p)` -- up to
   **1.75c/share** at the money, against a book that quotes a **1c** spread.
   The fee is 3.5x the half-spread and is the dominant cost of taking.

The market question text also no longer contains the strike (it is now just
"Bitcoin Up or Down - September 1, 1:55AM-2:00AM ET"), so the original
`$`-regex strike parse returned 0.0 on every market.

This tool prices these contracts independently using the Black-Scholes formula for cash-or-nothing binary options:

```
P(Up) = N(d2)

where d2 = [ln(S/K) + (r - sigma^2/2) * T] / (sigma * sqrt(T))

S = current BTC price (Binance spot)
K = strike price (BTC price at window open)
T = time remaining in window (in years)
sigma = annualized volatility (EWMA from Binance trades)
r = 0 (negligible over 5 minutes)
```

It then compares the model's fair value against Polymarket's live ask prices and simulates buying any contract priced below fair value, tracking P&L as each window resolves.

## Architecture

```
Binance BTCUSDT ──> feed.py ──> vol.py (EWMA vol) ──> pricer.py (N(d2))
                                                            |
Polymarket API  ──> polymarket.py (market discovery) ───────┤
                                                            v
                                                     simulator.py (P&L)
                                                            |
                                                     main.py (Rich display)
```

| File | Purpose |
|------|---------|
| `feed.py` | Binance BTCUSDT spot aggTrade websocket stream |
| `vol.py` | EWMA volatility estimator from 1-minute log returns |
| `pricer.py` | Black-Scholes binary option pricing (N(d2)) |
| `polymarket.py` | Market discovery via deterministic slug, CLOB prices, strike, settlement |
| `research/collect.py` | Record live books to JSONL |
| `research/evaluate.py` | Grade a recording under a cost ladder |
| `research/backtest.py` | Backtest over hundreds of settled windows |
| `test_pricing.py` | Unit tests (16) |
| `simulator.py` | Position tracking, resolution, bankroll management |
| `main.py` | CLI entry point with Rich live terminal display |

## Usage

```bash
pip install click requests rich websockets

python main.py
```

### Options

```
--bankroll FLOAT        Starting bankroll in dollars (default: 100)
--max-per-market FLOAT  Max exposure per 5-min window (default: 5)
--max-loss FLOAT        Stop-loss per window in dollars (default: 2)
--bar-interval FLOAT    Vol bar interval in seconds (default: 60)
--ewma-halflife INT     EWMA half-life in bars (default: 30)
--debug                 Enable debug logging
```

### Examples

```bash
python main.py                                    # defaults
python main.py --bankroll 500 --max-per-market 10  # bigger size
python main.py --max-loss 1                        # tighter stop
python main.py --bar-interval 30 --ewma-halflife 15  # more responsive vol
```

## Simulator Behavior

The simulator polls Polymarket every 3 seconds. On each poll:

1. Computes model fair value for Up and Down using current BTC price, strike, time remaining, and EWMA vol
2. If the market ask for either side is below the model's fair value, buys a contract at the ask
3. Continues buying each poll until the per-window exposure cap ($5) or stop-loss ($2) is hit

When a 5-minute window closes, all positions are resolved against the Binance close price vs the strike. Winning contracts pay $1, losers pay $0.

### Risk Controls

- **Exposure cap**: Max $5 total outlay per 5-minute window (configurable)
- **Stop-loss**: Max $2 realized loss per window before halting trades (configurable)
- **No late trades**: No new positions in the last 30 seconds of a window
- **No longshots**: Rejects trades where the model probability is below 20% (filters out fat-tail noise where Black-Scholes is least reliable)
- **Bankroll enforcement**: Cannot trade beyond available bankroll

## Data Sources

- **Binance**: Spot BTCUSDT aggTrade websocket (`wss://stream.binance.com:9443/ws/btcusdt@aggTrade`). Free, no auth.
- **Polymarket Gamma API**: Market discovery via deterministic slug (`btc-updown-5m-{unix_ts}`). Free, no auth.
- **Polymarket CLOB API**: Live bid/ask/midpoint prices. Free, no auth.

## Does the edge exist?

Short answer: no, not a model edge. `research/backtest.py` grades the model over
300 settled windows, reading the market price from the CLOB price-history
endpoint and BTC from Binance 1s klines **at the same second**, then charging a
0.5c half-spread and the real taker fee.

The decisive test is to degrade the BTC feed and watch what happens:

| BTC feed lag | trades | ROI | win% | Brier |
|---|---|---|---|---|
| 0s | 1191 | +19.9% | 66.1% | 0.1550 |
| 5s | 1148 | +18.6% | 64.2% | 0.1580 |
| 10s | 1139 | +16.5% | 61.9% | 0.1622 |
| 15s | 1170 | +10.7% | 57.9% | 0.1671 |
| 30s | 1215 | +8.9% | 54.7% | 0.1807 |
| 60s | 1219 | +6.6% | 50.1% | 0.2047 |

The market's own Brier score on the same sample is **0.1749**. So the model only
beats the market's price when it is allowed to see BTC fresher than that price;
delay it 30 seconds and it is worse than doing nothing. **The return is a
function of feed freshness, not of the pricing model.** That is latency
arbitrage, and Black-Scholes plays no part in it.

Two corroborating measurements:

- **The market is better calibrated than the model.** Bucketing 1,498
  observations, the market price's weighted mean calibration error is **0.0179**;
  the model's is 0.0431 point-in-time and 0.0319 TWAP-aware. The model is
  systematically under-confident -- it says 0.70 where reality is 0.83 -- which
  points it at the *losing* side of correctly-priced markets.
- **The "edge" is not the spread, it is the fee.** The book quotes a median 1c
  spread with ~239 shares at the touch at every point in the window, so the half
  spread is 0.5c. The taker fee is up to 1.75c/share. Both together mean ~2.25c
  of true edge is needed at the money just to break even -- while the model's own
  calibration error is 3-9 *cents*. The model will keep finding "edge" that is
  its own error.

### Why this tool cannot capture the latency edge it found

- `PolymarketClient.get_prices()` issues **six sequential REST calls** and takes
  a measured **~2.3s**; with the 3s poll sleep the decision cadence is **~5.3s**.
  The edge is already halved by 15s of staleness.
- The **no-trades-in-the-last-30s** rule excludes exactly the window where the
  information advantage is worth most.
- `orderMinSize` is **5 shares**, which at 0.50 is $2.50 -- half the entire $5
  per-window exposure cap. The simulator's one-share-at-a-time granularity is not
  executable.

Capturing this would mean a websocket book feed, a colocated BTC feed, and
trading *into* the last 30 seconds -- a different program, competing with
participants who are already doing it well (see the sibling
`polymarket-whale-tracker` repo).

## Known Limitations

- **Strike is a proxy.** The market's strike is the Chainlink BTC/USD print at
  the window open. `get_strike()` substitutes the Binance 1s kline close.
  Measured over 2,010 windows these agree on direction **95.3%** of the time
  overall but only **65.8%** when the window's total move is under $5 -- which is
  precisely the near-the-money regime where the model claims the most edge.
- **The settlement model is approximate.** `twap_effective_seconds()` uses
  `T_eff = (time until averaging starts) + L/3`. Once inside the averaging
  window this ignores the already-realised part of the average, which we cannot
  observe without the Chainlink stream. It improves calibration (0.0431 ->
  0.0319) but does not fix it.
- **Fat tails.** Standardised 5-minute returns over 14 days have kurtosis
  **29.2** against 3.0 for a normal, and 1.4% of moves exceed 3 sigma against
  0.27% expected. Black-Scholes is the wrong distribution; the 20% probability
  floor is a crude patch on it.
- **No book depth.** The simulator lifts the best ask at unlimited size.
- **Maker rebates ignored.** `rebateRate` is 0.2 and `takerOnly` is true, so
  quoting rather than taking avoids the fee entirely. That is a different and
  probably better strategy, and this tool does not model it.

## Volatility Estimator## Volatility Estimator

The vol estimator (`vol.py`) uses exponentially weighted moving average (EWMA) of squared log returns:

- Aggregates trades into 1-minute bars (configurable)
- Computes log returns between consecutive bar closes
- Maintains EWMA variance with configurable half-life (default: 30 bars)
- Annualizes: `sigma_annual = sigma_bar * sqrt(bars_per_year)`
- Blends from 50% default toward estimated vol over the first 30 bars
- Includes regime shift detection (flags when a return exceeds 3x the current vol estimate)

### Is a 1-minute EWMA appropriate for a 5-minute horizon?

Mostly yes on scale, badly no on shape. Over 14 days (4,025 windows), taking the
EWMA estimate at each window open and standardising the realised 5-minute return
by it:

| statistic | measured | normal |
|---|---|---|
| std(z) | 1.116 | 1.000 |
| mean abs(z) | 0.775 | 0.798 |
| kurtosis(z) | **29.19** | 3.00 |
| P(abs(z) > 2) | 5.9% | 4.6% |
| P(abs(z) > 3) | **1.4%** | 0.27% |

The level is roughly right -- an 11.6% under-estimate -- so scaling a 1-minute
estimate to 5 minutes is not the problem. The *shape* is: kurtosis of 29 means
the body of the distribution is too wide (median predicted 1-sigma move $84 vs
median realised move $46) while the tails are five times fatter than modelled.
A Gaussian binary price built on it is under-confident in the middle and
over-confident in the wings, which is exactly the calibration error measured
above.

**The estimator is not the binding constraint, though.** Even a perfect vol
number would not create edge: the market price is already better calibrated than
the model, and the only thing that pays is seeing BTC before the quote moves.

## Testing

```bash
env -u PYTHONPATH /opt/local/bin/python3.13 -m pytest test_pricing.py   # 16 tests
```

## Research tooling

```bash
# Record live books, then grade them under a cost ladder
python3 research/collect.py --minutes 30 --out research/data/run.jsonl
python3 research/evaluate.py research/data/run.jsonl

# Backtest over settled windows (this is the one that matters)
python3 research/backtest.py --windows 300
```
