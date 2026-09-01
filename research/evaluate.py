"""Grade the strategy against a recorded book, honestly.

Takes a JSONL capture from collect.py, backfills BTC spot and the strike from
Binance 1s klines and the realised outcome from Gamma, then reports what the
model would have made under a ladder of increasingly realistic assumptions:

    mid, no fee      -- what the original tool displayed as "edge"
    ask, no fee      -- pay the spread
    ask + fee        -- what you would actually be charged
    ask + fee + TWAP -- and price the contract the market actually settles on

    python3 research/evaluate.py research/data/run1.jsonl
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

import click
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pricer import binary_call_price, taker_fee_per_share, twap_effective_seconds  # noqa: E402
from vol import VolEstimator  # noqa: E402

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
WINDOW_SECONDS = 300


def load_klines(session, start_ms: int, end_ms: int, interval: str) -> dict[int, float]:
    """Binance kline closes keyed by second. Paged to cover the whole span."""
    out: dict[int, float] = {}
    cur = start_ms
    while cur < end_ms:
        r = session.get(f"{BINANCE_API}/api/v3/klines",
                        params={"symbol": "BTCUSDT", "interval": interval,
                                "startTime": cur, "endTime": end_ms, "limit": 1000},
                        timeout=20)
        ks = r.json()
        if not ks:
            break
        for k in ks:
            out[int(k[0]) // 1000] = float(k[4])
        nxt = int(ks[-1][0]) + 1
        if nxt <= cur:
            break
        cur = nxt
    return out


def nearest(price_map: dict[int, float], ts: int, tol: int = 10) -> float | None:
    for d in range(tol):
        if ts - d in price_map:
            return price_map[ts - d]
        if ts + d in price_map:
            return price_map[ts + d]
    return None


def fetch_outcomes(session, window_tss) -> dict[int, str]:
    out = {}
    for wts in sorted(window_tss):
        try:
            ev = session.get(f"{GAMMA_API}/events",
                             params={"slug": f"btc-updown-5m-{wts}"}, timeout=15).json()
            if not ev or not ev[0].get("markets"):
                continue
            m = ev[0]["markets"][0]
            if not m.get("closed"):
                continue
            names = json.loads(m.get("outcomes", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))
            win = [n for n, p in zip(names, prices) if float(p) > 0.95]
            if len(win) == 1:
                out[wts] = win[0].lower()
        except Exception as e:
            print(f"  outcome fetch failed for {wts}: {e}")
    return out


def best(levels, side: str) -> float:
    if not levels:
        return 0.0
    return float(levels[0][0])


def run(samples, spot, strikes, outcomes, halflife: int, bar_interval: float):
    # Warm the vol estimator on the 1-minute closes leading into the capture.
    vol = VolEstimator(bar_interval=bar_interval, ewma_halflife=halflife)
    for ts in sorted(spot):
        vol.on_trade(spot[ts], float(ts))

    variants = {
        "mid, no fee":            dict(use_ask=False, fee=False, twap=False),
        "ask, no fee":            dict(use_ask=True,  fee=False, twap=False),
        "ask + fee":              dict(use_ask=True,  fee=True,  twap=False),
        "ask + fee + TWAP model": dict(use_ask=True,  fee=True,  twap=True),
    }
    results = {k: {"n": 0, "stake": 0.0, "pnl": 0.0, "wins": 0} for k in variants}
    spreads, model_vs_mid = [], []

    for s in samples:
        wts = s["window_ts"]
        if wts not in outcomes or wts not in strikes:
            continue
        S = nearest(spot, int(s["ts"]))
        K = strikes[wts]
        if not S or not K:
            continue
        remaining = s["remaining"]
        if remaining < 30:
            continue

        fee_rate = float((s["meta"].get("fee_schedule") or {}).get("rate", 0.0))
        lookback = float((s["meta"].get("crypto_config") or {}).get("twapLookbackSeconds", 0.0))

        up_bid = best(s["books"]["up"]["bids"], "b")
        up_ask = best(s["books"]["up"]["asks"], "a")
        dn_bid = best(s["books"]["down"]["bids"], "b")
        dn_ask = best(s["books"]["down"]["asks"], "a")
        if not (up_bid and up_ask and dn_bid and dn_ask):
            continue
        spreads.append(up_ask - up_bid)

        for name, cfg in variants.items():
            t_eff = twap_effective_seconds(remaining, lookback) if cfg["twap"] else remaining
            m_up = binary_call_price(S, K, t_eff, vol.annual_vol)
            m_dn = 1.0 - m_up
            if name == "mid, no fee":
                model_vs_mid.append(m_up - (up_bid + up_ask) / 2)

            for side, model, bid, ask in (("up", m_up, up_bid, up_ask),
                                          ("down", m_dn, dn_bid, dn_ask)):
                px = ask if cfg["use_ask"] else (bid + ask) / 2
                cost = px + (taker_fee_per_share(px, fee_rate) if cfg["fee"] else 0.0)
                if model < 0.20 or cost <= 0 or cost >= model:
                    continue
                r = results[name]
                r["n"] += 1
                r["stake"] += cost
                won = outcomes[wts] == side
                r["pnl"] += (1.0 if won else 0.0) - cost
                r["wins"] += int(won)

    return results, spreads, model_vs_mid, vol


@click.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--ewma-halflife", default=30)
@click.option("--bar-interval", default=60.0)
def main(path, ewma_halflife, bar_interval):
    """Grade a recorded capture under progressively realistic cost assumptions."""
    samples = [json.loads(l) for l in open(path) if l.strip()]
    print(f"{len(samples)} samples, {len({s['window_ts'] for s in samples})} windows")
    session = requests.Session()

    lo = int(min(s["ts"] for s in samples)) - 3600
    hi = int(max(s["ts"] for s in samples)) + 400
    print("loading Binance 1s klines...")
    spot = load_klines(session, lo * 1000, hi * 1000, "1s")
    print(f"  {len(spot)} 1s closes")

    wtss = {s["window_ts"] for s in samples}
    strikes = {w: nearest(spot, w) for w in wtss}
    strikes = {w: v for w, v in strikes.items() if v}
    print("fetching settled outcomes...")
    outcomes = fetch_outcomes(session, wtss)
    print(f"  {len(outcomes)}/{len(wtss)} windows settled")

    results, spreads, mvm, vol = run(samples, spot, strikes, outcomes,
                                     ewma_halflife, bar_interval)

    print(f"\nEWMA annual vol at end of capture: {vol.annual_vol*100:.1f}% "
          f"({vol.bar_count} bars)")
    if spreads:
        spreads.sort()
        print(f"Up-token spread: median {spreads[len(spreads)//2]*100:.1f}c  "
              f"mean {sum(spreads)/len(spreads)*100:.1f}c")
    if mvm:
        mvm.sort()
        print(f"|model - mid| median: {abs(mvm[len(mvm)//2])*100:.2f}c   "
              f"mean abs: {sum(abs(x) for x in mvm)/len(mvm)*100:.2f}c")

    print(f"\n{'variant':<24} {'trades':>7} {'stake':>10} {'pnl':>10} {'roi':>8} {'win%':>7}")
    for name, r in results.items():
        roi = (r["pnl"] / r["stake"] * 100) if r["stake"] else 0.0
        wr = (r["wins"] / r["n"] * 100) if r["n"] else 0.0
        print(f"{name:<24} {r['n']:>7} {r['stake']:>10.2f} {r['pnl']:>+10.2f} "
              f"{roi:>7.1f}% {wr:>6.1f}%")


if __name__ == "__main__":
    main()
