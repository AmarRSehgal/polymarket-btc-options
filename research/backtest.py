"""Backtest the model over settled windows against real, executed trade prints.

An earlier version of this script read the market price from the CLOB
`/prices-history` endpoint. That endpoint's finest resolution is **one minute**
(`fidelity=1` is minutes, not seconds; every smaller value returns the same 5
points per 5-minute window), so "compare the model against the market price at
time t" was really comparing a live BTC price against a market print up to 60
seconds stale. That manufactures edge out of nothing. See README.

This version uses `data-api.polymarket.com/trades`, which returns every fill
with a 1-second timestamp, and reconstructs a tradeable book from them:

    BUY  on Up at p    -> someone lifted the Up ask   -> ask_up = p
    SELL on Up at p    -> someone hit the Up bid      -> bid_up = p
    BUY  on Down at p  -> ask_down = p                -> bid_up = 1 - p
    SELL on Down at p  -> bid_down = p                -> ask_up = 1 - p

Every price the backtest trades at is therefore a price at which a real
counterparty really transacted at that second, not a midpoint or an interpolation.

    python3 research/backtest.py --windows 300
    python3 research/backtest.py --windows 300 --lag 30   # stale BTC feed

The headline test is `discrimination`, which needs no cost assumptions at all:
if the model's Brier score against the settled outcome is worse than the
market's own price, there is no edge to extract and the cost ladder is moot.
"""
from __future__ import annotations

import asyncio
import json
import math
import pathlib
import statistics
import sys
import time

import aiohttp
import click

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pricer import binary_call_price, taker_fee_per_share, twap_effective_seconds  # noqa: E402
from vol import VolEstimator  # noqa: E402

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
WINDOW_SECONDS = 300

DEFAULT_CACHE = pathlib.Path(__file__).resolve().parent / "data" / "backtest_cache.json"


async def _get(session, url, params, retries=3):
    for _ in range(retries):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def load_trades(session, condition_id, lo_ts, hi_ts):
    """Every fill on this market between lo_ts and hi_ts, paged newest-first."""
    out, offset = [], 0
    while offset < 6000:
        page = await _get(session, f"{DATA_API}/trades",
                          {"market": condition_id, "limit": 500, "offset": offset})
        if not page:
            break
        out.extend(page)
        if len(page) < 500 or min(t["timestamp"] for t in page) < lo_ts:
            break
        offset += len(page)
    return [t for t in out if lo_ts <= t["timestamp"] <= hi_ts]


def reconstruct_book(trades, up_token):
    """Per-second best bid/ask for the Up token, implied by the fills.

    Each fill pins one side of the book at that second. Where several fills
    land in the same second we keep the most aggressive quote each implies
    (highest bid, lowest ask), which is the tightest defensible reading.
    """
    bids: dict[int, float] = {}
    asks: dict[int, float] = {}
    for t in trades:
        ts = int(t["timestamp"])
        p = float(t["price"])
        is_up = t["asset"] == up_token
        buy = t["side"] == "BUY"
        # Map the fill onto the Up token's book.
        if is_up:
            up_px, is_ask = p, buy
        else:
            up_px, is_ask = 1.0 - p, not buy
        if is_ask:
            asks[ts] = min(asks.get(ts, 1.0), up_px)
        else:
            bids[ts] = max(bids.get(ts, 0.0), up_px)
    return bids, asks


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
        trades = await load_trades(session, m["conditionId"],
                                   window_ts, window_ts + WINDOW_SECONDS)
        if not trades:
            return None
        bids, asks = reconstruct_book(trades, up_token)
        cfg = m.get("cryptoMarketConfig") or {}
        return {
            "window_ts": window_ts,
            "outcome": win[0].lower(),
            "bids": bids,
            "asks": asks,
            "n_trades": len(trades),
            "twap": float(cfg.get("twapLookbackSeconds", 0.0)) if cfg.get("twapEnabled") else 0.0,
            "fee_rate": float((m.get("feeSchedule") or {}).get("rate", 0.0)) if m.get("feesEnabled") else 0.0,
        }


async def load_klines(session, start_ms, end_ms, interval="1m"):
    """Binance closes keyed by the FIRST second at which the close is known.

    k[6] is the bar's close time in ms (end of the bar minus 1ms), so
    `k[6]//1000 + 1` is the first second at which that close is public
    information. Keying by open time instead would leak a full bar of
    lookahead into every comparison.
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
    sem = asyncio.Semaphore(8)

    async def one(a, b):
        async with sem:
            return await load_klines(session, a * 1000, b * 1000, "1s")

    out = {}
    for p in await asyncio.gather(*[one(a, b) for a, b in spans]):
        out.update(p)
    return out


def spot_at(spot, ts, tol=4):
    for d in range(tol):
        if ts - d in spot:
            return spot[ts - d]
        if ts + d in spot:
            return spot[ts + d]
    return None


def build_sigma(closes, halflife):
    """EWMA annual vol as of each 1-minute bar close."""
    vol = VolEstimator(bar_interval=60.0, ewma_halflife=halflife)
    sigma_at = {}
    for t in sorted(closes):
        vol.on_trade(closes[t], float(t) + 59)
        sigma_at[t] = vol.annual_vol if vol.bar_count >= halflife else None
    return sigma_at, vol


def observations(windows, sigma_at, spot, halflife, lag, use_twap):
    """Every second where the book is observable, with model and market marks.

    Yields (model_up, market_mid_up, market_ask_up, fee_rate, won_up, remaining).
    `lag` staleness is applied to the BTC price only -- the market marks are
    always read at the true second -- so the lag ladder isolates feed freshness.
    """
    out = []
    for w in windows:
        wts = w["window_ts"]
        K = spot_at(spot, wts)
        sigma = sigma_at.get(wts)
        if K is None or sigma is None:
            continue
        won_up = w["outcome"] == "up"
        for t in sorted(set(w["asks"]) | set(w["bids"])):
            if not (wts < t < wts + WINDOW_SECONDS):
                continue
            S = spot_at(spot, t - lag)
            if S is None:
                continue
            bid, ask = w["bids"].get(t), w["asks"].get(t)
            # Crossed readings mean the book moved inside the second; drop them.
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None
            if bid is None and ask is None:
                continue
            remaining = float(wts + WINDOW_SECONDS - t)
            t_eff = twap_effective_seconds(remaining, w["twap"]) if use_twap else remaining
            mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
            out.append((binary_call_price(S, K, t_eff, sigma), mid,
                        ask, bid, w["fee_rate"], won_up, remaining))
    return out


def two_sided(obs):
    """Observations where both sides printed in the same second (mid is real)."""
    return [o for o in obs if o[1] is not None]


def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def log_loss(pairs):
    eps = 1e-6
    return -sum(math.log(max(min(p, 1 - eps), eps)) if y else
                math.log(max(min(1 - p, 1 - eps), eps)) for p, y in pairs) / len(pairs)


def calibration(pairs, nbins=10):
    """Weighted mean |predicted - realised| across probability buckets."""
    buckets: dict[int, list] = {}
    for p, y in pairs:
        buckets.setdefault(min(int(p * nbins), nbins - 1), []).append((p, y))
    tot = err = 0
    rows = []
    for b in sorted(buckets):
        vals = buckets[b]
        pred = sum(p for p, _ in vals) / len(vals)
        real = sum(y for _, y in vals) / len(vals)
        rows.append((b / nbins, (b + 1) / nbins, len(vals), pred, real))
        err += abs(pred - real) * len(vals)
        tot += len(vals)
    return (err / tot if tot else float("nan")), rows


TIME_BUCKETS = ((0, 30), (30, 60), (60, 120), (120, 180), (180, 240), (240, 300))


def by_time_remaining(obs, min_edge, extra_cost):
    """Cost-ladder result split by how much of the window is left.

    The live tool refuses to trade with under 30s to go. The sibling
    whale-tracker repo found the profitable actors concentrated in the last
    2-17 seconds, so this is the bucket that decides whether that rule is
    protecting the strategy or excluding the only part that works.
    """
    rows = []
    for lo, hi in TIME_BUCKETS:
        sub = [o for o in obs if lo <= o[6] < hi]
        if not sub:
            rows.append((lo, hi, 0, 0, 0.0, 0.0, float("nan"), float("nan")))
            continue
        both = two_sided(sub)
        pairs_mkt = [(mid, won) for _m, mid, _a, _b, _f, won, _r in both]
        pairs_mdl = [(m, won) for m, _mid, _a, _b, _f, won, _r in both]
        _n, n, stake, pnl, wins = trade_ladder(sub, min_edge, extra_cost)[2]
        rows.append((lo, hi, len(sub), n, stake, pnl,
                     brier(pairs_mdl), brier(pairs_mkt), wins))
    return rows


def trade_ladder(obs, min_edge, extra_cost):
    """Take whenever the model beats the observed executable price, net of costs."""
    variants = [
        ("mid, no fee   (as displayed)", False, False),
        ("ask, no fee   (pay spread)", True, False),
        ("ask + taker fee", True, True),
    ]
    rows = []
    for name, use_ask, fee_on in variants:
        n = wins = 0
        stake = pnl = 0.0
        for model_up, mid, ask, bid, fee_rate, won_up, _rem in obs:
            # Taking Up needs an observed ask; taking Down needs an observed Up
            # bid (ask_down = 1 - bid_up). A one-sided second offers one of them.
            if use_ask:
                candidates = (("up", model_up, ask),
                              ("down", 1.0 - model_up, None if bid is None else 1.0 - bid))
            else:
                candidates = (("up", model_up, mid),
                              ("down", 1.0 - model_up, None if mid is None else 1.0 - mid))
            for side, model, px in candidates:
                if px is None:
                    continue
                px = min(px + extra_cost, 0.999)
                cost = px + (taker_fee_per_share(px, fee_rate) if fee_on else 0.0)
                if model < 0.20 or cost <= 0.001 or model - cost < min_edge:
                    continue
                won = won_up if side == "up" else not won_up
                n += 1
                stake += cost
                wins += won
                pnl += (1.0 if won else 0.0) - cost
        rows.append((name, n, stake, pnl, wins))
    return rows


async def gather_windows(count, skip_recent):
    now = int(time.time())
    latest = now - (now % WINDOW_SECONDS) - skip_recent * WINDOW_SECONDS
    wtss = [latest - i * WINDOW_SECONDS for i in range(count)]
    sem = asyncio.Semaphore(10)
    async with aiohttp.ClientSession() as s:
        closes = await load_klines(s, (min(wtss) - 5000) * 1000, (max(wtss) + 600) * 1000)
        spot = await load_klines_1s(s, [(w - 70, w + WINDOW_SECONDS + 10) for w in wtss])
        got = await asyncio.gather(*[load_window(s, sem, w) for w in wtss])
    return [g for g in got if g], closes, spot


def _save(path, ws, closes, spot):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ws": [{**w, "bids": {str(k): v for k, v in w["bids"].items()},
                "asks": {str(k): v for k, v in w["asks"].items()}} for w in ws],
        "closes": {str(k): v for k, v in closes.items()},
        "spot": {str(k): v for k, v in spot.items()},
    }))


def _load(path):
    d = json.loads(path.read_text())
    ws = [{**w, "bids": {int(k): v for k, v in w["bids"].items()},
           "asks": {int(k): v for k, v in w["asks"].items()}} for w in d["ws"]]
    return (ws, {int(k): v for k, v in d["closes"].items()},
            {int(k): v for k, v in d["spot"].items()})


@click.command()
@click.option("--windows", default=300, help="How many 5-min windows to test")
@click.option("--skip-recent", default=12, help="Skip N most recent windows (settlement lag)")
@click.option("--lag", default=0, help="Seconds of staleness to impose on the BTC feed")
@click.option("--extra-cost", default=0.0, help="Extra cents/share of slippage beyond the print")
@click.option("--min-edge", default=0.0, help="Require at least this much edge net of costs")
@click.option("--ewma-halflife", default=30)
@click.option("--cache", default=str(DEFAULT_CACHE), help="Where to cache the pulled data")
@click.option("--refresh", is_flag=True, help="Re-pull even if the cache exists")
def main(windows, skip_recent, lag, extra_cost, min_edge, ewma_halflife, cache, refresh):
    """Backtest the model against settled Polymarket windows."""
    cache_path = pathlib.Path(cache)
    if cache_path.exists() and not refresh:
        ws, closes, spot = _load(cache_path)
        print(f"loaded cache {cache_path} ({len(ws)} windows)")
    else:
        ws, closes, spot = asyncio.run(gather_windows(windows, skip_recent))
        _save(cache_path, ws, closes, spot)
        print(f"pulled {len(ws)}/{windows} settled windows -> {cache_path}")
    if not ws:
        return

    ups = sum(1 for w in ws if w["outcome"] == "up")
    fills = sum(w["n_trades"] for w in ws)
    print(f"{len(ws)} windows ({ups} up / {len(ws)-ups} down), {fills:,} fills, "
          f"{len(closes)} 1m bars, {len(spot):,} 1s bars")

    sigma_at, vol = build_sigma(closes, ewma_halflife)
    obs = observations(ws, sigma_at, spot, ewma_halflife, lag, use_twap=True)
    obs_pit = observations(ws, sigma_at, spot, ewma_halflife, lag, use_twap=False)
    if not obs:
        print("no observable seconds -- check the cache")
        return

    both, both_pit = two_sided(obs), two_sided(obs_pit)
    spreads = sorted(a - b for _m, _mid, a, b, _f, _w, _r in both)
    print(f"{len(obs):,} observable seconds ({len(both):,} two-sided), implied spread "
          f"median {spreads[len(spreads)//2]*100:.1f}c mean {statistics.mean(spreads)*100:.1f}c")
    print(f"EWMA annual vol at end of sample: {vol.annual_vol*100:.1f}%   "
          f"BTC feed lag applied: {lag}s\n")

    market = [(mid, won) for _m, mid, _a, _b, _f, won, _r in both]
    model = [(m, won) for m, _mid, _a, _b, _f, won, _r in both]
    model_pit = [(m, won) for m, _mid, _a, _b, _f, won, _r in both_pit]

    print("DISCRIMINATION (no cost assumptions -- who predicts the settlement better?)")
    print(f"{'series':<28} {'Brier':>8} {'logloss':>9} {'cal.err':>9}")
    for name, pairs in (("market mid", market),
                        ("model (TWAP-aware)", model),
                        ("model (point-in-time)", model_pit)):
        cal, _ = calibration(pairs)
        print(f"{name:<28} {brier(pairs):>8.4f} {log_loss(pairs):>9.4f} {cal:>9.4f}")
    print("  lower is better on all three; the market is the benchmark to beat\n")

    _, rows = calibration(model)
    print("MODEL CALIBRATION (TWAP-aware)")
    print(f"{'bucket':<14} {'n':>7} {'predicted':>10} {'realised':>10}")
    for lo, hi, n, pred, real in rows:
        print(f"{lo:.1f}-{hi:.1f}      {n:>7} {pred:>10.3f} {real:>10.3f}")
    print()

    print("COST LADDER (take whenever the model beats the price)")
    print(f"{'variant':<32} {'trades':>7} {'stake':>11} {'pnl':>10} {'roi':>8} {'win%':>7}")
    for name, n, stake, pnl, wins in trade_ladder(obs, min_edge, extra_cost):
        roi = pnl / stake * 100 if stake else 0.0
        wr = wins / n * 100 if n else 0.0
        print(f"{name:<32} {n:>7} {stake:>11.2f} {pnl:>+10.2f} {roi:>7.1f}% {wr:>6.1f}%")

    print("\nBY TIME REMAINING (ask + taker fee; the live tool skips the 0-30s bucket)")
    print(f"{'remaining':<12} {'obs':>7} {'trades':>7} {'stake':>10} {'pnl':>9} "
          f"{'roi':>8} {'win%':>7} {'B(model)':>9} {'B(mkt)':>8}")
    for lo, hi, nobs, n, stake, pnl, bm, bk, wins in by_time_remaining(obs, min_edge, extra_cost):
        roi = pnl / stake * 100 if stake else 0.0
        wr = wins / n * 100 if n else 0.0
        print(f"{f'{lo}-{hi}s':<12} {nobs:>7} {n:>7} {stake:>10.2f} {pnl:>+9.2f} "
              f"{roi:>7.1f}% {wr:>6.1f}% {bm:>9.4f} {bk:>8.4f}")


if __name__ == "__main__":
    main()
