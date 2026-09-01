"""Backtest the model over many settled windows using Polymarket price history.

Answers the only question that matters: after paying the spread and the taker
fee, does model-vs-market disagreement predict the settlement?

For each settled 5-minute window it takes three decision points (4, 3 and 2
minutes to go), reads the market price from the CLOB price-history endpoint,
prices the contract off an EWMA vol estimated from Binance 1-minute bars, and
grades the resulting trades against Polymarket's own resolution.

    python3 research/backtest.py --windows 600

Costs are explicit: `--half-spread` is added to the market price to approximate
lifting the offer (the book is a 1c tick wide in practice) and the crypto_fees_v2
taker fee is charged on top.
"""
from __future__ import annotations

import asyncio
import json
import math
import pathlib
import sys
import time

import aiohttp
import click

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pricer import binary_call_price, taker_fee_per_share, twap_effective_seconds  # noqa: E402
from vol import VolEstimator, MINUTES_PER_YEAR  # noqa: E402

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
WINDOW_SECONDS = 300


async def _get(session, url, params, retries=3):
    for _ in range(retries):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def load_window(session, sem, window_ts):
    async with sem:
        ev = await _get(session, f"{GAMMA_API}/events",
                        {"slug": f"btc-updown-5m-{window_ts}"})
        if not ev or not ev[0].get("markets"):
            return None
        m = ev[0]["markets"][0]
        if not m.get("closed"):
            return None
        names = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        win = [n for n, p in zip(names, prices) if float(p) > 0.95]
        if len(win) != 1:
            return None
        up_token = json.loads(m["clobTokenIds"])[0]
        hist = await _get(session, f"{CLOB_API}/prices-history",
                          {"market": up_token, "startTs": window_ts - 30,
                           "endTs": window_ts + WINDOW_SECONDS, "fidelity": 1})
        pts = (hist or {}).get("history") or []
        if not pts:
            return None
        cfg = m.get("cryptoMarketConfig") or {}
        return {
            "window_ts": window_ts,
            "outcome": win[0].lower(),
            "history": [(int(p["t"]), float(p["p"])) for p in pts],
            "twap": float(cfg.get("twapLookbackSeconds", 0.0)) if cfg.get("twapEnabled") else 0.0,
            "fee_rate": float((m.get("feeSchedule") or {}).get("rate", 0.0)) if m.get("feesEnabled") else 0.0,
        }


async def load_klines(session, start_ms, end_ms, interval="1m"):
    """Binance closes keyed by the second the bar CLOSES.

    Keying by open time while storing the close price shifts every price one
    bar into the future -- a full minute of lookahead against a market price
    sampled at `t`, which on its own manufactures a large fake edge.
    """
    out = {}
    cur = start_ms
    while cur < end_ms:
        ks = await _get(session, f"{BINANCE_API}/api/v3/klines",
                        {"symbol": "BTCUSDT", "interval": interval, "startTime": cur,
                         "endTime": end_ms, "limit": 1000})
        if not ks:
            break
        for k in ks:
            out[int(k[6]) // 1000 + 1] = float(k[4])
        nxt = int(ks[-1][0]) + 1
        if nxt <= cur:
            break
        cur = nxt
    return out


async def load_klines_1s(session, spans):
    """1s closes covering each (start_s, end_s) span, fetched concurrently."""
    sem = asyncio.Semaphore(8)

    async def one(a, b):
        async with sem:
            return await load_klines(session, a * 1000, b * 1000, "1s")

    parts = await asyncio.gather(*[one(a, b) for a, b in spans])
    out = {}
    for p in parts:
        out.update(p)
    return out


def spot_at(spot, ts, tol=4):
    for d in range(tol):
        if ts - d in spot:
            return spot[ts - d]
        if ts + d in spot:
            return spot[ts + d]
    return None


def simulate(windows, closes, spot, half_spread, use_twap, fee_on, min_edge, halflife):
    """Grade every market print against the BTC price at that same second.

    Sampling BTC fresher than the market price is the whole game here: a market
    quote read even 60s stale against a live spot makes any model look like it
    has found tens of cents of edge. Both sides are read at `t`.
    """
    vol = VolEstimator(bar_interval=60.0, ewma_halflife=halflife)
    sigma_at = {}
    for t in sorted(closes):
        vol.on_trade(closes[t], float(t) + 59)
        sigma_at[t] = vol.annual_vol if vol.bar_count >= halflife else None

    n = wins = 0
    stake = pnl = 0.0
    by_side = {"up": [0, 0], "down": [0, 0]}
    edges = []

    for w in windows:
        wts = w["window_ts"]
        K = spot_at(spot, wts)
        sigma = sigma_at.get(wts)
        if K is None or sigma is None:
            continue
        for t, mkt_up in w["history"]:
            if not (wts < t < wts + WINDOW_SECONDS - 30):
                continue
            S = spot_at(spot, t)
            if S is None:
                continue
            remaining = float(wts + WINDOW_SECONDS - t)
            t_eff = twap_effective_seconds(remaining, w["twap"]) if use_twap else remaining
            model_up = binary_call_price(S, K, t_eff, sigma)

            for side, model, mkt in (("up", model_up, mkt_up),
                                     ("down", 1.0 - model_up, 1.0 - mkt_up)):
                ask = min(mkt + half_spread, 0.999)
                cost = ask + (taker_fee_per_share(ask, w["fee_rate"]) if fee_on else 0.0)
                if model < 0.20 or cost <= 0.001 or model - cost < min_edge:
                    continue
                n += 1
                stake += cost
                won = w["outcome"] == side
                wins += won
                pnl += (1.0 if won else 0.0) - cost
                edges.append(model - cost)
                by_side[side][0] += 1
                by_side[side][1] += won
    return dict(n=n, wins=wins, stake=stake, pnl=pnl, by_side=by_side,
                avg_edge=(sum(edges) / len(edges) if edges else 0.0),
                final_vol=vol.annual_vol)


async def gather_windows(count, skip_recent):
    now = int(time.time())
    latest = now - (now % WINDOW_SECONDS) - skip_recent * WINDOW_SECONDS
    wtss = [latest - i * WINDOW_SECONDS for i in range(count)]
    sem = asyncio.Semaphore(12)
    async with aiohttp.ClientSession() as s:
        klines = await load_klines(s, (min(wtss) - 4000) * 1000, (max(wtss) + 600) * 1000)
        spans = [(w - 10, w + WINDOW_SECONDS + 10) for w in wtss]
        spot = await load_klines_1s(s, spans)
        got = await asyncio.gather(*[load_window(s, sem, w) for w in wtss])
    return [g for g in got if g], klines, spot


@click.command()
@click.option("--windows", default=600, help="How many 5-min windows to test")
@click.option("--skip-recent", default=12, help="Skip N most recent windows (settlement lag)")
@click.option("--half-spread", default=0.005, help="Added to the market price to take the offer")
@click.option("--min-edge", default=0.0, help="Require at least this much edge net of costs")
@click.option("--ewma-halflife", default=30)
def main(windows, skip_recent, half_spread, min_edge, ewma_halflife):
    """Backtest the model against settled Polymarket windows."""
    ws, closes, spot = asyncio.run(gather_windows(windows, skip_recent))
    print(f"{len(ws)}/{windows} windows settled with price history; "
          f"{len(closes)} 1m bars, {len(spot)} 1s bars")
    if not ws:
        return
    ups = sum(1 for w in ws if w["outcome"] == "up")
    print(f"outcomes: {ups} up / {len(ws)-ups} down")
    print(f"assumptions: half-spread {half_spread*100:.1f}c, min-edge {min_edge*100:.1f}c\n")

    variants = [
        ("mid, no fee  (as displayed)", 0.0, False, False),
        ("ask, no fee  (pay spread)", half_spread, False, False),
        ("ask + fee", half_spread, False, True),
        ("ask + fee + TWAP model", half_spread, True, True),
    ]
    print(f"{'variant':<30} {'trades':>7} {'stake':>10} {'pnl':>10} {'roi':>8} "
          f"{'win%':>7} {'edge':>7}")
    for name, hs, twap, fee in variants:
        r = simulate(ws, closes, spot, hs, twap, fee, min_edge, ewma_halflife)
        roi = r["pnl"] / r["stake"] * 100 if r["stake"] else 0.0
        wr = r["wins"] / r["n"] * 100 if r["n"] else 0.0
        print(f"{name:<30} {r['n']:>7} {r['stake']:>10.2f} {r['pnl']:>+10.2f} "
              f"{roi:>7.1f}% {wr:>6.1f}% {r['avg_edge']*100:>6.1f}c")
        if name.startswith("ask + fee + TWAP"):
            for side, (cnt, w) in r["by_side"].items():
                if cnt:
                    print(f"    {side:>5}: {cnt} trades, {w/cnt*100:.1f}% win")
    print(f"\nEWMA annual vol at end of sample: "
          f"{simulate(ws, closes, spot, 0, False, False, 0, ewma_halflife)['final_vol']*100:.1f}%")


if __name__ == "__main__":
    main()
