"""Is a 1-minute EWMA an appropriate vol input for a 5-minute binary?

Walks `vol.VolEstimator` over Binance 1-minute closes exactly as the live tool
does, then at every 5-minute window open takes the estimate as of that moment
and standardises the window's realised return by it:

    z = log(close_{t+5m} / close_t) / (sigma_annual * sqrt(5 / minutes_per_year))

If the estimator were right and returns were Gaussian, z would be standard
normal. Two failure modes matter and they are different questions:

  * SCALE  -- is std(z) near 1? A binary priced with sigma too low or too high
    is wrong everywhere, and this is the part a better estimator can fix.
  * SHAPE  -- is z actually normal? N(d2) assumes it is. Excess kurtosis means
    the body is too wide and the tails too fat, so the model is under-confident
    near the money and over-confident in the wings no matter how good sigma is.

    python3 research/voldiag.py --days 14
"""
from __future__ import annotations

import asyncio
import math
import pathlib
import statistics
import sys
import time

import aiohttp
import click

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from vol import MINUTES_PER_YEAR, VolEstimator  # noqa: E402

BINANCE_API = "https://api.binance.com"
WINDOW_MINUTES = 5


async def load_1m(session, start_ms, end_ms):
    """1-minute closes keyed by the second the bar's close becomes public."""
    out, cur = {}, start_ms
    while cur < end_ms:
        try:
            async with session.get(f"{BINANCE_API}/api/v3/klines",
                                   params={"symbol": "BTCUSDT", "interval": "1m",
                                           "startTime": cur, "endTime": end_ms,
                                           "limit": 1000},
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                ks = await r.json()
        except Exception:
            break
        if not ks:
            break
        for k in ks:
            out[int(k[6]) // 1000 + 1] = float(k[4])
        nxt = int(ks[-1][0]) + 1
        if nxt <= cur:
            break
        cur = nxt
    return out


def kurtosis(xs):
    """Non-excess kurtosis; a normal sample sits at 3."""
    mu = statistics.fmean(xs)
    var = statistics.fmean((x - mu) ** 2 for x in xs)
    if var <= 0:
        return float("nan")
    return statistics.fmean((x - mu) ** 4 for x in xs) / var ** 2


def diagnose(closes, halflife, bar_interval):
    vol = VolEstimator(bar_interval=bar_interval, ewma_halflife=halflife)
    sigma_at = {}
    for t in sorted(closes):
        vol.on_trade(closes[t], float(t) + bar_interval - 1)
        sigma_at[t] = vol.annual_vol if vol.bar_count >= halflife else None

    step = WINDOW_MINUTES * 60
    horizon = math.sqrt(WINDOW_MINUTES / MINUTES_PER_YEAR)
    zs, predicted, realised = [], [], []
    for t in sorted(closes):
        if t % step != 0:
            continue
        sigma = sigma_at.get(t)
        nxt = closes.get(t + step)
        if sigma is None or not nxt or sigma <= 0:
            continue
        S = closes[t]
        ret = math.log(nxt / S)
        zs.append(ret / (sigma * horizon))
        predicted.append(S * sigma * horizon)
        realised.append(abs(nxt - S))
    return zs, predicted, realised, vol


@click.command()
@click.option("--days", default=14, help="How many days of 1-minute bars to walk")
@click.option("--ewma-halflife", default=30)
@click.option("--bar-interval", default=60.0)
def main(days, ewma_halflife, bar_interval):
    """Report scale and shape diagnostics for the live vol estimator."""
    end = int(time.time())
    start = end - days * 86400

    async def pull():
        async with aiohttp.ClientSession() as s:
            return await load_1m(s, start * 1000, end * 1000)

    closes = asyncio.run(pull())
    zs, predicted, realised, vol = diagnose(closes, ewma_halflife, bar_interval)
    if len(zs) < 100:
        print(f"only {len(zs)} usable windows -- widen --days")
        return

    n = len(zs)
    print(f"{len(closes):,} 1m bars over {days}d -> {n:,} five-minute windows")
    print(f"EWMA annual vol at end of sample: {vol.annual_vol*100:.1f}%\n")

    print(f"{'statistic':<20} {'measured':>10} {'normal':>10}")
    print(f"{'std(z)':<20} {statistics.stdev(zs):>10.3f} {1.0:>10.3f}")
    print(f"{'mean abs(z)':<20} {statistics.fmean(abs(z) for z in zs):>10.3f} "
          f"{math.sqrt(2/math.pi):>10.3f}")
    print(f"{'kurtosis(z)':<20} {kurtosis(zs):>10.2f} {3.0:>10.2f}")
    for k, ref in ((2, 0.0455), (3, 0.0027), (4, 0.0000633)):
        hit = sum(1 for z in zs if abs(z) > k) / n
        print(f"{f'P(abs(z) > {k})':<20} {hit*100:>9.2f}% {ref*100:>9.2f}%")

    predicted.sort()
    realised.sort()
    print(f"\nmedian predicted 1-sigma 5m move: ${predicted[n//2]:,.0f}")
    print(f"median realised 5m move:          ${realised[n//2]:,.0f}")
    print("\nSCALE is the fixable part; SHAPE (kurtosis) is a property of the")
    print("returns, and no vol estimate makes a Gaussian binary right about it.")


if __name__ == "__main__":
    main()
