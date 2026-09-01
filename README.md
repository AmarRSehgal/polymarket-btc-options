# Polymarket BTC 5-Minute Binary Options Edge Finder

Live edge detection for Polymarket's BTC 5-minute Up/Down markets using Black-Scholes binary option pricing against a real-time Binance volatility estimate.

> **Finding (2026-09-01): the model has no edge. Returns are a function of how
> fresh the BTC feed is, and nothing else that can be measured here.**
> Over 230 settled windows and 460,266 real fills:
>
> 1. The market's own price predicts the settlement **better than the model**
>    (Brier 0.155 vs 0.162, and better on log-loss and calibration too). That
>    test assumes no costs at all, so nothing downstream of it can rescue the
>    strategy.
> 2. Post-cost ROI is a smooth, monotonic function of BTC feed lag -- and an
>    outcome-permutation test says the **lag gradient is real (7.2 sd) while the
>    profit level at any single lag is not (0.9 sd)**. The honest summary is
>    "latency pays, this model does not", not "+4.8% ROI".
> 3. In the last 30 seconds the model wins **10%** of its trades. The
>    no-trades-in-last-30s rule is the most protective line in the file.
>
> See [Does the edge exist?](#does-the-edge-exist) before trusting any number
> this tool prints. It remains a simulator and places no real orders.

## How It Works

Every 5 minutes, Polymarket creates a new binary market: "Will BTC be higher or lower than the opening price at window close?" The Up token pays $1 if BTC finishes above the opening price, $0 otherwise. Down is the inverse.

**The markets still exist and deterministic slug discovery still works** --
`btc-updown-5m-{unix_ts}` with `ts % 300 == 0` resolves against the Gamma API,
verified live 2026-09-01. Two things about them changed since this tool was
written, and both matter:

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
"Bitcoin Up or Down - September 1, 2:55AM-3:00AM ET"), so the original
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
| `polymarket.py` | Market discovery via deterministic slug, batched book fetch, strike, settlement |
| `simulator.py` | Position tracking, resolution, bankroll management |
| `main.py` | CLI entry point with Rich live terminal display |
| `research/backtest.py` | Backtest over settled windows using real fills, with a lag ladder and a permutation null. **The one that matters.** |
| `research/voldiag.py` | Scale/shape diagnostics for the vol estimator |
| `research/collect.py` | Record live books to JSONL |
| `research/evaluate.py` | Grade a recording under a cost ladder |
| `test_pricing.py` | Unit tests (19) |

## Usage

```bash
pip install -r requirements.txt

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
python main.py                                     # defaults
python main.py --bankroll 500 --max-per-market 10  # bigger size
python main.py --max-loss 1                        # tighter stop
python main.py --bar-interval 30 --ewma-halflife 15  # more responsive vol
```

## Simulator Behavior

The simulator polls Polymarket every 3 seconds. On each poll:

1. Fetches both order books in a single batched `POST /books` call, so the Up and Down quotes share one timestamp
2. Computes model fair value for Up and Down using current BTC price, strike, time remaining, and EWMA vol
3. If the market ask for either side is below the model's fair value **net of the taker fee**, buys a contract at the ask
4. Continues buying each poll until the per-window exposure cap ($5) or stop-loss ($2) is hit

When a 5-minute window closes, positions are resolved against Polymarket's own settlement.

### Risk Controls

- **Exposure cap**: Max $5 total outlay per 5-minute window (configurable)
- **Stop-loss**: Max $2 realized loss per window before halting trades (configurable)
- **No late trades**: No new positions in the last 30 seconds of a window. [This rule is load-bearing](#should-it-trade-the-last-30-seconds) -- the model loses badly there.
- **No longshots**: Rejects trades where the model probability is below 20% (fat tails; N(d2) is least reliable in the wings)
- **No trading on the default vol**: refuses to trade until the EWMA has warmed up, so it never trades on the hardcoded 50% placeholder
- **Bankroll enforcement**: Cannot trade beyond available bankroll

## Data Sources

- **Binance**: Spot BTCUSDT aggTrade websocket (`wss://stream.binance.com:9443/ws/btcusdt@aggTrade`). Free, no auth.
- **Polymarket Gamma API**: Market discovery via deterministic slug (`btc-updown-5m-{unix_ts}`). Free, no auth.
- **Polymarket CLOB API**: `POST /books` for live order books. Free, no auth.
- **Polymarket Data API**: `/trades` for historical fills at 1-second resolution (research only). Free, no auth.

## Does the edge exist?

Short answer: no, not a model edge.

### A note on how an earlier version of this answer was wrong

An earlier pass read the market price from the CLOB `/prices-history` endpoint
with `fidelity=1`, believing that to be 1-second resolution, and concluded
"+19.9% ROI comparing both at the same second."

**`fidelity` is in minutes.** The endpoint returns ~5 points per 5-minute window
and every smaller value (`0`, `0.1`, `"10s"`) returns exactly the same 5 points;
there is no sub-minute price history. So that measurement compared a live
Binance price against a market print up to 60 seconds stale, which manufactures
edge out of nothing. Its "+19.9% at 0s lag" reproduces below almost exactly as
the **-30s row -- BTC from the future**. That is the size of the artifact.

`research/backtest.py` now uses `data-api.polymarket.com/trades`, which returns
every fill with a 1-second timestamp (~2,000 fills per window against 5 price
points), and reconstructs a tradeable book from them:

```
BUY  on Up at p    -> someone lifted the Up ask  -> ask_up = p
SELL on Up at p    -> someone hit the Up bid     -> bid_up = p
BUY  on Down at p  -> ask_down = p               -> bid_up = 1 - p
SELL on Down at p  -> bid_down = p               -> ask_up = 1 - p
```

Every price the backtest trades at is one where a real counterparty really
transacted at that second. Over 230 settled windows this gives 460,266 fills and
20,101 observable seconds, with a median implied spread of **1.0c** -- matching
the live book.

### The model is worse than the price it is trading against

This test needs no cost assumptions at all. If the model predicts the settlement
worse than the market price does, there is nothing to extract and the cost
ladder is moot.

| series | Brier | log-loss | calibration error |
|---|---|---|---|
| **market mid** | **0.1549** | **0.4725** | **0.0201** |
| model (TWAP-aware) | 0.1617 | 0.4931 | 0.0417 |
| model (point-in-time) | 0.1642 | 0.4987 | 0.0619 |

The market wins on all three. The model is systematically under-confident --
it says 0.75 where reality is 0.84, and 0.65 where reality is 0.71 -- which
points it at the *losing* side of correctly-priced markets. (Modelling the TWAP
settlement helps, 0.062 -> 0.042 calibration error, but does not close the gap.)

### What is left is feed latency, and it decays in seconds

Degrade the BTC feed and watch what happens. Only the Binance price is delayed;
the market prints are always read at the true second, so this isolates freshness.

| BTC feed lag | model Brier | trades | stake | P&L | ROI | win% |
|---|---|---|---|---|---|---|
| **-30s** *(future BTC -- placebo)* | 0.1425 | 11,913 | $6,288 | +$1,079.69 | **+17.2%** | 61.8% |
| -10s *(future BTC -- placebo)* | 0.1548 | 11,090 | $5,760 | +$590.85 | +10.3% | 57.3% |
| 0s | 0.1617 | 10,570 | $5,308 | +$255.09 | +4.8% | 52.6% |
| 5s | 0.1640 | 10,453 | $5,142 | +$172.08 | +3.3% | 50.8% |
| 10s | 0.1675 | 10,666 | $5,117 | +$122.02 | +2.4% | 49.1% |
| 15s | 0.1710 | 10,847 | $5,108 | +$69.46 | +1.4% | 47.7% |
| 30s | 0.1831 | 11,364 | $4,952 | +$17.93 | +0.4% | 43.7% |
| 60s | 0.2059 | 12,232 | $4,560 | +$8.75 | +0.2% | 37.4% |

Read the negative-lag rows as a placebo: handing the model BTC from 30 seconds
in the future buys 17.2% ROI, which is what pure direction-knowledge is worth
here. The return is a smooth function of feed freshness with no kink at the
model -- **it is latency, and Black-Scholes plays no part in it.** Note also
that model Brier only beats the market's 0.1549 at *negative* lag. At 0s it is
already losing, and the residual +4.8% is the tail of the same latency effect:
a fill printed at second `t` reflects a decision its taker made some seconds
earlier, while the Binance 1s close at `t` is the freshest mark obtainable.

**Break-even is around 10-15 seconds of staleness.** For comparison, `get_prices()`
now takes ~0.4-0.8s and the poll loop sleeps 3s, so the live tool's decision
cadence is ~3.5-4s -- inside break-even, but for maybe a percent or two of ROI
before slippage, size limits, and the fact that the model is the worse predictor.

### How much of this is noise?

Most of the level; almost none of the gradient. Every trade inside a window
settles on that window's single outcome, so the effective sample size is **230
windows, not 10,570 trades** -- treating trades as independent overstates
significance by more than an order of magnitude. `--permute` shuffles outcomes
across windows, which preserves the trade selection and the prices while
breaking the link to what happened:

```
SIGNIFICANCE (100 outcome permutations; n = 230 windows, NOT the trade count)
  ROI at 0s            real +4.81%   null -0.38% +/- 5.49   -> 0.9 sd
  gradient 0s vs 30s   real +4.44pp  null -15.14pp +/- 2.71 -> 7.2 sd
```

So **the +4.8% is not distinguishable from noise** (split-half confirms it:
+1.2% on the first 115 windows, +9.5% on the second). What *is* solid is the
freshness gradient, which is paired across the same windows and lands 7.2 sd
from its null. Note the null is not centred on zero -- the model selects
different trades at different lags, so the bias has to be measured rather than
assumed, which is the whole reason for the permutation rather than a t-test.

Read that as the finding: **latency pays; this model does not.** Reproduce with
`python3 research/backtest.py --permute 100`.

### Is it the spread or the fee?

The fee, by a factor of three, and neither is the real problem.

| variant | trades | stake | P&L | ROI | win% |
|---|---|---|---|---|---|
| mid, no fee *(what the original tool displayed)* | 9,790 | $4,729 | +$379.95 | +8.0% | 52.2% |
| ask, no fee *(pay the spread)* | 11,731 | $5,829 | +$435.57 | +7.5% | 53.4% |
| ask + taker fee | 10,570 | $5,308 | +$255.09 | +4.8% | 52.6% |

The book quotes a median **1c** spread, so the half-spread is 0.5c. The taker fee
peaks at **1.75c/share** at the money. Together ~2.25c of true edge is needed at
the money just to break even -- while the model's own calibration error is 2-9
*cents*. The model will keep finding "edge" that is its own error. The original
display's "edge" against an untradeable midpoint with no fee overstates the
result by 3.2 points of ROI, and the whole of that remaining 4.8% is latency.

### Should it trade the last 30 seconds?

**No.** The sibling `polymarket-whale-tracker` repo found sophisticated actors
trading 2-17 seconds before close, which raises the fair question of whether the
no-trades-in-the-last-30s rule excludes the only part of the window that pays.
It does not. It is the single most protective rule in the file:

| remaining | obs | trades | stake | P&L | ROI | win% | Brier (model) | Brier (market) |
|---|---|---|---|---|---|---|---|---|
| **0-30s** | 1,187 | 271 | $28.90 | **-$1.90** | **-6.6%** | **10.0%** | 0.2221 | **0.0338** |
| 30-60s | 1,682 | 322 | $137.52 | +$1.48 | +1.1% | 43.2% | 0.0746 | 0.0405 |
| 60-120s | 3,848 | 1,527 | $963.64 | +$38.36 | +4.0% | 65.6% | 0.1058 | 0.0946 |
| 120-180s | 4,733 | 2,489 | $1,434.45 | +$99.55 | +6.9% | 61.6% | 0.1376 | 0.1339 |
| 180-240s | 4,601 | 3,011 | $1,469.55 | +$90.45 | +6.2% | 51.8% | 0.1679 | 0.1669 |
| 240-300s | 4,050 | 2,950 | $1,273.85 | +$27.15 | +2.1% | 44.1% | 0.2279 | 0.2241 |

In the final 30 seconds this model wins **10% of its trades**. The reason is
structural, not bad luck: by then the 60-second TWAP is more than half realised,
so the settlement value is largely already determined and the market price is
close to a fact (Brier **0.034**, versus 0.155 over the window as a whole). The
model cannot observe the running average -- `twap_effective_seconds` explicitly
assumes none of it is realised -- so it is guessing (Brier 0.222) against a
price that is nearly right, and disagreement with a nearly-right price is
simply being wrong.

The late actors the whale-tracker found are trading **on the settlement value
itself** -- a faster BTC feed and, most likely, the Chainlink TWAP stream as it
accumulates. That is a different edge that this model does not have. Lifting the
30s rule would not capture it; it would hand them $1 per contract.

(Caveat on sample: the book goes one-sided near the close -- only 6% of
final-30s seconds show fills on both sides, against ~35% mid-window -- so the
271 trades in that bucket are thin. The mechanism and the Brier gap are the
load-bearing evidence, not the ROI point estimate.)

### Why this tool cannot capture the latency edge it found

- `orderMinSize` is **5 shares**, which at 0.50 is $2.50 -- half the entire $5
  per-window exposure cap. The simulator's one-share-at-a-time granularity is
  not executable.
- The poll loop is REST-and-sleep. Break-even is ~10-15s of staleness, so there
  is headroom, but the whole prize inside it is a couple of points of ROI on a
  model that is the *worse* forecaster.
- No book depth is modelled; the simulator lifts the best ask at unlimited size.

Capturing this properly would mean a websocket book feed, a colocated BTC feed,
the Chainlink TWAP stream, and quoting rather than taking -- a different program,
competing with participants who are already doing it well.

## Known Limitations

- **Strike is a proxy.** The market's strike is the Chainlink BTC/USD print at
  the window open. `get_strike()` substitutes the Binance 1s kline close.
  Measured over 2,010 windows these agree on direction **95.3%** of the time
  overall but only **65.8%** when the window's total move is under $5 -- which is
  precisely the near-the-money regime where the model claims the most edge.
- **The settlement model is approximate.** `twap_effective_seconds()` uses
  `T_eff = (time until averaging starts) + L/3`. Once inside the averaging
  window this ignores the already-realised part of the average, which we cannot
  observe without the Chainlink stream. It improves calibration (0.0619 ->
  0.0417) but does not fix it, and it is why the model is hopeless in the last
  30 seconds.
- **Fat tails.** See the vol section below: standardised 5-minute returns carry
  kurtosis far above 3, so Black-Scholes is the wrong distribution and the 20%
  probability floor is a crude patch on it.
- **Maker rebates ignored.** `feeSchedule.rebateRate` is 0.2 and `takerOnly` is
  true, so quoting rather than taking avoids the fee entirely. That is a
  different and probably better strategy, and this tool does not model it.
- **Backtest fill realism.** The reconstruction takes the most aggressive quote
  implied by the fills in each second. Where several fills land in one second at
  different prices, that is optimistic by up to a tick.

## Volatility Estimator

The vol estimator (`vol.py`) uses an exponentially weighted moving average of squared log returns:

- Aggregates trades into 1-minute bars (configurable)
- Computes log returns between consecutive bar closes
- Maintains EWMA variance with configurable half-life (default: 30 bars)
- Annualizes: `sigma_annual = sigma_bar * sqrt(bars_per_year)`
- Blends from a 50% default toward the estimate over the first 30 bars
- Includes regime shift detection (flags when a return exceeds 3x the current vol estimate)

### Is a 1-minute EWMA appropriate for a 5-minute horizon?

Mostly yes on scale, badly no on shape. `research/voldiag.py --days 14` takes the
estimate at each window open and standardises the realised 5-minute return by it
(4,025 windows):

| statistic | measured | normal |
|---|---|---|
| std(z) | 1.112 | 1.000 |
| mean abs(z) | 0.797 | 0.798 |
| kurtosis(z) | **8.88** | 3.00 |
| P(abs(z) > 2) | 6.48% | 4.55% |
| P(abs(z) > 3) | **1.94%** | 0.27% |
| P(abs(z) > 4) | **0.80%** | 0.006% |

The level is roughly right -- an 11% over-estimate -- so scaling a 1-minute
estimate to 5 minutes is not the problem. The *shape* is: the body of the
distribution is too wide (median predicted 1-sigma move $84 against a median
realised move of $47) while the tails run 7x to 100x fatter than modelled. A
Gaussian binary built on it is under-confident in the middle and over-confident
in the wings, which is exactly the calibration error measured above. (Kurtosis is
dominated by a handful of prints and is not stable across samples -- an earlier
14-day window read 29.2. Its instability is itself the point.)

**The estimator is not the binding constraint, though.** Even a perfect vol
number would not create edge: the market price is already the better forecast,
and the only thing that pays is seeing BTC before the quote moves.

## Testing

```bash
env -u PYTHONPATH /opt/local/bin/python3.13 -m pytest test_pricing.py   # 19 tests
```

## Research tooling

```bash
# Backtest over settled windows using real fills -- this is the one that matters.
# The first run pulls and caches; later runs read the cache.
python3 research/backtest.py --windows 300
python3 research/backtest.py --lag 15        # degrade the BTC feed
python3 research/backtest.py --lag -30       # placebo: BTC from the future
python3 research/backtest.py --permute 100   # significance vs an outcome-shuffle null

# Vol estimator scale and shape diagnostics
python3 research/voldiag.py --days 14

# Record live books, then grade them under a cost ladder
python3 research/collect.py --minutes 30 --out research/data/run.jsonl
python3 research/evaluate.py research/data/run.jsonl
```
