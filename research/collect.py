"""Record the live Polymarket BTC 5-min book to JSONL for offline evaluation.

Records only the order book and timestamps. BTC spot, the strike and the
realised outcome are backfilled afterwards from Binance klines and the Gamma
API, so a run is fully reproducible and does not depend on this process having
had a good websocket at the time.

    python3 research/collect.py --minutes 30 --out research/data/run.jsonl
"""
from __future__ import annotations

import json
import pathlib
import time

import click
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WINDOW_SECONDS = 300


def _top(levels: list[dict], best_is_max: bool) -> list[tuple[float, float]]:
    lv = sorted(((float(x["price"]), float(x["size"])) for x in levels), reverse=best_is_max)
    return lv[:5]


def collect(minutes: float, out_path: pathlib.Path, interval: float) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    deadline = time.time() + minutes * 60
    market_cache: dict[int, dict] = {}
    n = 0

    with out_path.open("a") as fh:
        while time.time() < deadline:
            loop_start = time.time()
            now = time.time()
            window_ts = int(now) - (int(now) % WINDOW_SECONDS)
            try:
                meta = market_cache.get(window_ts)
                if meta is None:
                    r = session.get(f"{GAMMA_API}/events",
                                    params={"slug": f"btc-updown-5m-{window_ts}"}, timeout=8)
                    events = r.json()
                    if not events or not events[0].get("markets"):
                        time.sleep(interval)
                        continue
                    m = events[0]["markets"][0]
                    up_tok, down_tok = json.loads(m["clobTokenIds"])
                    meta = market_cache[window_ts] = {
                        "slug": m["slug"], "question": m["question"],
                        "up_token": up_tok, "down_token": down_tok,
                        "fee_schedule": m.get("feeSchedule"),
                        "crypto_config": m.get("cryptoMarketConfig"),
                        "order_min_size": m.get("orderMinSize"),
                        "tick": m.get("orderPriceMinTickSize"),
                    }

                books = {}
                for name, tok in (("up", meta["up_token"]), ("down", meta["down_token"])):
                    b = session.get(f"{CLOB_API}/book", params={"token_id": tok}, timeout=8).json()
                    books[name] = {
                        "bids": _top(b.get("bids", []), True),
                        "asks": _top(b.get("asks", []), False),
                    }

                fh.write(json.dumps({
                    "ts": now, "window_ts": window_ts, "slug": meta["slug"],
                    "remaining": window_ts + WINDOW_SECONDS - now,
                    "books": books, "meta": meta,
                }) + "\n")
                fh.flush()
                n += 1
            except Exception as e:  # transient API/network errors are expected
                print(f"sample error: {e}", flush=True)

            time.sleep(max(0.0, interval - (time.time() - loop_start)))
    return n


@click.command()
@click.option("--minutes", default=30.0, help="How long to record for")
@click.option("--interval", default=2.0, help="Seconds between samples")
@click.option("--out", "out", default="research/data/run.jsonl", help="Output JSONL path")
def main(minutes: float, interval: float, out: str):
    """Record live Polymarket BTC 5-min books for offline evaluation."""
    n = collect(minutes, pathlib.Path(out), interval)
    print(f"wrote {n} samples to {out}")


if __name__ == "__main__":
    main()
