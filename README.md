# Polymarket BTC 5-Minute Binary Options Edge Finder

Live edge detection for Polymarket's BTC 5-minute Up/Down markets using Black-Scholes binary option pricing against a real-time Binance volatility estimate.

> **Finding (2026-09-01): the model has no edge. Returns are a function of how
> fresh the BTC feed is, and nothing else that can be measured here.**
> Over 230 settled windows and 460,266 real fills:
>
> 1. The market's own price predicts the settlement **better than the model**
>    (Brier 0.1549 vs 0.1557, and better on log-loss and calibration too). That
>    test assumes no costs at all, so nothing downstream of it can rescue the
>    strategy.
> 2. Post-cost ROI is a smooth, monotonic function of BTC feed lag, and
>    **break-even is about 5 seconds of staleness** -- against a tool whose own
>    decision cadence is ~3.5-4s. A permutation test puts the freshness gradient
>    at 8.6 sd and the profit level at 3.3 sd, so the honest summary is "latency
>    pays, this model does not."
> 3. In the last 30 seconds the model wins **4.4%** of its trades. The
>    no-trades-in-last-30s rule is the most protective line in the file.
> 4. The biggest single improvement available was not in the model at all: using
>    a 60-second TWAP for the strike instead of a spot print took settlement
>    reconstruction from 90.9% to **98.3%** accurate.
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
| `test_pricing.py` | Unit tests (22) |

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
2. Computes model fair value for Up and Down using current BTC price, a 60-second TWAP strike, time remaining, and EWMA vol
3. If the market ask for either side is below the model's fair value **net of the taker fee**, buys a contract at the ask
4. Continues buying each poll until the per-window exposure cap ($5) or stop-loss ($2) is hit

When a 5-minute window closes, positions are resolved against Polymarket's own settlement.

### Two things that surprise you on a first run

- **It will not trade for the first ~30 minutes.** The simulator refuses to
  trade while the EWMA is still the hardcoded 50% placeholder, and the default
  `--ewma-halflife 30` over 60-second bars means 30 bars, so a 15-minute run
  displays a live model and never places a single simulated trade. That is
  correct behaviour, not a hang. `--bar-interval 15 --ewma-halflife 10` warms
  in ~2.5 minutes if you just want to watch it work.
- **Settlement lags the close by four to nine minutes.** Gamma does not flag a
  window `closed` when it ends -- measured 2026-09-01, a window 213s past its
  close was still open while one 513s past was resolved. So one to two windows'
  worth of positions are always outstanding, which is why the portfolio cap
  below exists and why `Open positions` routinely shows more than the
  per-window limit.

### Risk Controls

- **Exposure cap**: Max $5 total outlay per 5-minute window (configurable)
- **Portfolio cap**: Max $15 open across *all* unsettled windows. The per-window
  cap is not a portfolio cap -- a window holds its positions until Gamma reports
  it settled, which lags the close by the better part of a minute, so two or
  three windows are routinely open at once. A live 36-minute run held **$14.57**
  against a headline "$5 per market" limit.
- **Stop-loss**: Max $2 realized loss per window before halting trades (configurable)
- **No late trades**: No new positions in the last 30 seconds of a window. [This rule is load-bearing](#should-it-trade-the-last-30-seconds) -- the model loses badly there.
- **No longshots**: Rejects trades where the model probability is below 20% (fat tails; N(d2) is least reliable in the wings)
- **No trading on the default vol**: refuses to trade until the EWMA has warmed up, so it never trades on the hardcoded 50% placeholder
- **Bankroll enforcement**: Cannot trade beyond available bankroll

### Simulation only, by construction

There is no order placement path in this repo and no way to add one by
accident. Placing a Polymarket order requires an EIP-712 signature from a
funded wallet plus L2 API credentials; the repo contains no private key, no
signing code, no wallet library and no credential handling of any kind. Every
outbound call is a public read -- the single `POST` in the codebase is
`CLOB /books`, which is a batched book *query*. `simulator.py` moves a number
in memory and nothing else.

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

### The strike matters more than anything in the model

Before any of that: the model needs a strike, and the market's strike is a
Chainlink `btc-usd-twap-60s` print with no free feed. Which Binance statistic
you substitute dominates everything downstream. Reconstructing settlement over
the 230 windows and checking the implied direction against Polymarket's own
resolution:

| open -> close | all | near-the-money |
|---|---|---|
| point-in-time -> point-in-time | 90.9% | 68.3% |
| point-in-time -> 60s TWAP | 95.2% | 82.9% |
| **60s TWAP -> 60s TWAP** | **98.3%** | **92.7%** |
| 60s TWAP -> point-in-time | 93.0% | 75.6% |

("near-the-money" = the 41 windows whose total move was under $20.) `get_strike()`
used to return a single 1s kline close -- the worst row. It now averages the 60
seconds before the open, matching what the oracle itself averages, which cuts
the basis error by more than half overall and by three quarters in the regime
this tool actually trades. Every number below uses the TWAP strike; on the
point-in-time strike the model's Brier was 0.1617 rather than 0.1557.

A residual basis remains -- Chainlink aggregates several venues, and 1.7% of
windows still resolve against the best Binance-only reconstruction.

### The model is still worse than the price it is trading against

This test needs no cost assumptions at all. If the model predicts the settlement
worse than the market price does, there is nothing to extract and the cost
ladder is moot.

| series | Brier | log-loss | calibration error |
|---|---|---|---|
| **market mid** | **0.1549** | **0.4725** | **0.0201** |
| model (TWAP-aware) | 0.1557 | 0.4777 | 0.0336 |
| model (point-in-time settle) | 0.1590 | 0.4870 | 0.0524 |

With a correct strike and a TWAP-aware settlement the model gets *close* --
0.1557 against 0.1549 -- but it does not get in front, and it is still half
again as badly calibrated. It remains under-confident in the upper middle: it
says 0.65 where reality is 0.71, and 0.75 where reality is 0.80, which points it
at the losing side of correctly-priced markets.

### What is left is feed latency, and break-even is about five seconds

Degrade the BTC feed and watch what happens. Only the Binance price is delayed;
the market prints are always read at the true second, so this isolates freshness.

| BTC feed lag | model Brier | trades | stake | P&L | ROI | win% |
|---|---|---|---|---|---|---|
| **-30s** *(future BTC -- placebo)* | 0.1366 | 11,874 | $6,687 | +$1,222.64 | **+18.3%** | 66.6% |
| -10s *(future BTC -- placebo)* | 0.1486 | 10,362 | $5,890 | +$658.17 | +11.2% | 63.2% |
| 0s | 0.1557 | 9,080 | $5,053 | +$162.96 | +3.2% | 57.4% |
| 5s | 0.1580 | 8,942 | $4,859 | +$2.99 | **+0.1%** | 54.4% |
| 10s | 0.1611 | 9,473 | $4,990 | -$53.16 | -1.1% | 52.1% |
| 15s | 0.1645 | 9,884 | $5,073 | -$92.65 | -1.8% | 50.4% |
| 30s | 0.1763 | 10,697 | $4,953 | -$101.68 | -2.1% | 45.3% |
| 60s | 0.1982 | 11,737 | $4,568 | -$148.39 | -3.2% | 37.7% |

Read the negative-lag rows as a placebo: handing the model BTC from 30 seconds
in the future buys 18.3% ROI, which is what pure direction-knowledge is worth
here. The return is a smooth function of feed freshness with no kink at the
model -- **it is latency, and Black-Scholes plays no part in it.**

**Break-even is about five seconds of staleness.** That is the number that
kills this design. `get_prices()` now takes ~0.4-0.8s and the poll loop sleeps
3s, so the tool's own decision cadence is **~3.5-4s** -- inside break-even, but
with essentially no headroom, before slippage, the 5-share minimum, or the fact
that the model is still the worse forecaster.

### How much of this is noise?

Some of the level; almost none of the gradient. Every trade inside a window
settles on that window's single outcome, so the effective sample size is **230
windows, not 9,080 trades** -- treating trades as independent overstates
significance by more than an order of magnitude. `--permute` shuffles outcomes
across windows, which preserves the trade selection and the prices while
breaking the link to what happened:

```
SIGNIFICANCE (100 outcome permutations; n = 230 windows, NOT the trade count)
  ROI at 0s            real +3.23%   null -9.94% +/- 3.95   -> 3.3 sd
  gradient 0s vs 30s   real +5.28pp  null -18.14pp +/- 2.71 -> 8.6 sd
```

Split-half agrees the level is now reasonably stable (+4.3% on the first 115
windows, +1.6% on the second; on the old point-in-time strike it was +1.2% then
+9.5%). Note the null is not centred on zero -- the model selects different
trades at different lags, so the bias has to be measured rather than assumed,
which is the whole reason for a permutation rather than a t-test.

So a real but small effect at zero lag, and a much stronger freshness gradient.
Read it as: **latency pays; the model does not.** Reproduce with
`python3 research/backtest.py --permute 100`.

### Is it the spread or the fee?

The fee, by a factor of three, and neither is the real problem.

| variant | trades | stake | P&L | ROI | win% |
|---|---|---|---|---|---|
| mid, no fee *(what the original tool displayed)* | 9,579 | $5,061 | +$220.04 | +4.3% | 55.1% |
| ask, no fee *(pay the spread)* | 11,249 | $6,148 | +$274.02 | +4.5% | 57.1% |
| ask + taker fee | 9,080 | $5,053 | +$162.96 | +3.2% | 57.4% |

The book quotes a median **1c** spread, so the half-spread is 0.5c. The taker fee
peaks at **1.75c/share** at the money. Together ~2.25c of true edge is needed at
the money just to break even -- while the model's own calibration error is still
3.4 *cents* on average. The model will keep finding "edge" that is its own error.
Note the spread barely registers (4.3% -> 4.5%, and it *rises* because paying up
changes which trades qualify); it is the fee that takes a third of the return.

`research/evaluate.py` corroborates this from live *recorded books* rather than
reconstructed ones -- real top-of-book with depth, captured by `collect.py`.
Over a 10-window capture (far too small for the ROI to mean anything, so read
only the spread statistics): median book spread **1.0c**, and the model's
disagreement with the midpoint runs a median of **1.8c** and a mean absolute of
**6.4c**. The model is routinely claiming several times the entire cost of
crossing, against a price it cannot out-forecast. That gap is the error, not
the edge.

### Should it trade the last 30 seconds?

**No.** The sibling `polymarket-whale-tracker` repo found sophisticated actors
trading 2-17 seconds before close, which raises the fair question of whether the
no-trades-in-the-last-30s rule excludes the only part of the window that pays.
It does not. It is the single most protective rule in the file:

| remaining | obs | trades | stake | P&L | ROI | win% | Brier (model) | Brier (market) |
|---|---|---|---|---|---|---|---|---|
| **0-30s** | 1,187 | 203 | $11.71 | **-$2.71** | **-23.1%** | **4.4%** | 0.1210 | **0.0338** |
| 30-60s | 1,682 | 189 | $80.64 | +$8.36 | +10.4% | 47.1% | 0.0431 | 0.0405 |
| 60-120s | 3,848 | 1,337 | $909.96 | +$5.04 | +0.6% | 68.4% | 0.0966 | 0.0946 |
| 120-180s | 4,733 | 2,382 | $1,471.02 | +$10.98 | +0.7% | 62.2% | 0.1347 | 0.1339 |
| 180-240s | 4,601 | 2,683 | $1,474.91 | +$57.09 | +3.9% | 57.1% | 0.1669 | 0.1669 |
| 240-300s | 4,050 | 2,286 | $1,104.79 | +$84.21 | +7.6% | 52.0% | 0.2221 | 0.2241 |

In the final 30 seconds this model wins **4.4% of its trades**. The reason is
structural, not bad luck: by then the 60-second TWAP is more than half realised,
so the settlement value is largely already determined and the market price is
close to a fact (Brier **0.034**, versus 0.155 over the window as a whole). The
model cannot observe the running average -- `twap_effective_seconds` explicitly
assumes none of it is realised -- so it is guessing against a price that is
nearly right, and disagreement with a nearly-right price is simply being wrong.

Note the profile runs the other way from intuition: the model does best with
**240-300s** left (+7.6%), when the market itself is most uncertain and the TWAP
has not started, and deteriorates monotonically into the close.

The late actors the whale-tracker found are trading **on the settlement value
itself** -- a faster BTC feed and, most likely, the Chainlink TWAP stream as it
accumulates. That is a different edge that this model does not have. Lifting the
30s rule would not capture it; it would hand them $1 per contract.

(Caveat on sample: the book goes one-sided near the close -- only 6% of
final-30s seconds show fills on both sides, against ~35% mid-window -- so the
203 trades in that bucket are thin. The mechanism and the Brier gap are the
load-bearing evidence, not the ROI point estimate.)

### Why this tool cannot capture the latency edge it found

- `orderMinSize` is **5 shares**, which at 0.50 is $2.50 -- half the entire $5
  per-window exposure cap. The simulator's one-share-at-a-time granularity is
  not executable.
- The poll loop is REST-and-sleep at ~3.5-4s, against a break-even of ~5s of
  staleness. There is essentially no headroom, and the whole prize inside it is
  ~3 points of ROI on a model that is still the *worse* forecaster.
- No book depth is modelled; the simulator lifts the best ask at unlimited size.

Capturing this properly would mean a websocket book feed, a colocated BTC feed,
the Chainlink TWAP stream, and quoting rather than taking -- a different program,
competing with participants who are already doing it well.

## Known Limitations

- **Strike is still a proxy.** `get_strike()` now returns a 60s TWAP of Binance
  spot rather than a single print, which reconstructs Polymarket's settlement
  98.3% of the time (92.7% near the money). The remaining 1.7% is real basis:
  Chainlink aggregates several venues and we only see Binance.
- **The settlement model is approximate.** `twap_effective_seconds()` uses
  `T_eff = (time until averaging starts) + L/3`. Once inside the averaging
  window this ignores the already-realised part of the average, which we cannot
  observe without the Chainlink stream. It improves calibration (0.0524 ->
  0.0336) but does not fix it, and it is why the model is hopeless in the last
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
env -u PYTHONPATH /opt/local/bin/python3.13 -m pytest test_pricing.py   # 22 tests
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
