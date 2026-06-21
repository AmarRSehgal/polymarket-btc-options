# Polymarket BTC 5-Minute Binary Options Edge Finder

Live edge detection for Polymarket's BTC 5-minute Up/Down markets using Black-Scholes binary option pricing against a real-time Binance volatility estimate.

## How It Works

Every 5 minutes, Polymarket creates a new binary market: "Will BTC be higher or lower than the opening price at window close?" The Up token pays $1 if BTC finishes above the opening price, $0 otherwise. Down is the inverse.

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
| `polymarket.py` | Market discovery via deterministic slug, CLOB price fetching |
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

## Known Limitations

- **Strike price approximation**: The actual market resolves against the Chainlink BTC/USD data stream price at window open. We approximate this with the Binance spot price at the 5-minute boundary. The basis is typically <$5 against a ~$16 expected 5-min move, so the impact on N(d2) is small.
- **Vol warm-up**: The EWMA estimator needs ~30 bars (30 minutes at default settings) to fully converge from the 50% default. Early-session pricing reflects this blend.
- **Fat tails**: Black-Scholes assumes lognormal returns. BTC at 5-minute horizons has fatter tails, meaning extreme moves are more likely than the model predicts. The 20% floor on model probability partially mitigates this.
- **No book depth**: The simulator assumes infinite liquidity at the best ask. Real execution would face size constraints.
- **No fees**: Polymarket taker fees are not modeled. These would reduce realized edge.
- **Chainlink vs Binance resolution**: We resolve positions using Binance spot close price, but Polymarket resolves against Chainlink. Small discrepancies are possible.

## Volatility Estimator

The vol estimator (`vol.py`) uses exponentially weighted moving average (EWMA) of squared log returns:

- Aggregates trades into 1-minute bars (configurable)
- Computes log returns between consecutive bar closes
- Maintains EWMA variance with configurable half-life (default: 30 bars)
- Annualizes: `sigma_annual = sigma_bar * sqrt(bars_per_year)`
- Blends from 50% default toward estimated vol over the first 30 bars
- Includes regime shift detection (flags when a return exceeds 3x the current vol estimate)
