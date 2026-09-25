"""Polymarket CLOB market channel: the Up-token book and the trade tape, pushed.

The REST poller in main.py sees the book every ~3.5-4s, which the backtest puts
inside break-even staleness with no headroom. A maker has to know its queue and
see every print, so the paper maker reads the websocket instead.

Two facts about the feed, measured 2026-09-25 against live windows:

* The two token books are one book. Every `price_change` carries the Up level
  and its mirrored Down level (Up BUY 0.21 x 495.92 arrives with Down SELL 0.79
  x 495.92), so the Up book alone is complete. Down updates are ignored.
* `last_trade_price` is published once, on the taker's token, with `side` the
  taker's side. A Down BUY at q is therefore a taker SELLING Up at 1 - q. No
  transaction hash appeared on both tokens in a 40s sample; dedup anyway.

Prices are held as integer cents: the tick is 1c in the 0.04-0.96 band this
trades in, and float dict keys drift.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def to_cents(price: str | float) -> int:
    return int(round(float(price) * 100))


class UpBook:
    """L2 book for the Up token, in cents."""

    def __init__(self):
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}
        self.best_bid: int | None = None
        self.best_ask: int | None = None
        self.updated = 0.0

    def _refresh_top(self):
        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None

    def apply_snapshot(self, bids: list[dict], asks: list[dict], t: float):
        self.bids = {to_cents(x["price"]): float(x["size"]) for x in bids if float(x["size"]) > 0}
        self.asks = {to_cents(x["price"]): float(x["size"]) for x in asks if float(x["size"]) > 0}
        self._refresh_top()
        self.updated = t

    def apply_change(self, side: str, price: str, size: str, t: float):
        book = self.bids if side == "BUY" else self.asks
        c, s = to_cents(price), float(size)
        if s > 0:
            book[c] = s
        else:
            book.pop(c, None)
        self._refresh_top()
        self.updated = t

    def level_size(self, side: str, price_c: int) -> float:
        """Visible size resting at `price_c` on the side a `side` order joins."""
        return (self.bids if side == "buy" else self.asks).get(price_c, 0.0)

    @property
    def mid_c(self) -> float | None:
        if self.best_bid is None or self.best_ask is None or self.best_bid >= self.best_ask:
            return None
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class TapeTrade:
    t: float
    taker_side: str     # "buy" | "sell", in Up terms
    price_c: int        # Up price
    size: float
    tx: str


def up_equivalent(is_up: bool, side: str, price: str) -> tuple[str, int]:
    """Map a print on either token to the Up taker side and Up price."""
    c = to_cents(price)
    if is_up:
        return ("buy" if side == "BUY" else "sell"), c
    return ("sell" if side == "BUY" else "buy"), 100 - c


class MarketStream:
    """One subscription per window. `run()` returns at `until` or on stop()."""

    def __init__(self, up_token: str, down_token: str,
                 on_trade: Callable[[TapeTrade], None]):
        self.up_token = up_token
        self.down_token = down_token
        self.on_trade = on_trade
        self.book = UpBook()
        self.messages = 0
        self._seen_tx: OrderedDict[str, None] = OrderedDict()
        self._stop = False

    def stop(self):
        self._stop = True

    def _handle(self, x: dict, now: float):
        et = x.get("event_type")
        if et == "book":
            if x.get("asset_id") == self.up_token:
                self.book.apply_snapshot(x.get("bids") or [], x.get("asks") or [], now)
        elif et == "price_change":
            for ch in x.get("price_changes") or ():
                if ch.get("asset_id") == self.up_token:
                    self.book.apply_change(ch["side"], ch["price"], ch["size"], now)
        elif et == "last_trade_price":
            aid = x.get("asset_id")
            if aid not in (self.up_token, self.down_token):
                return
            tx = x.get("transaction_hash") or f"{aid}:{x.get('timestamp')}:{x.get('price')}:{x.get('size')}"
            if tx in self._seen_tx:
                return
            self._seen_tx[tx] = None
            if len(self._seen_tx) > 5000:
                self._seen_tx.popitem(last=False)
            side, price_c = up_equivalent(aid == self.up_token, x["side"], x["price"])
            self.on_trade(TapeTrade(t=now, taker_side=side, price_c=price_c,
                                    size=float(x["size"]), tx=tx))

    async def run(self, until: float):
        backoff = 1.0
        while not self._stop and time.time() < until:
            try:
                async with websockets.connect(MARKET_WS, ping_interval=10, ping_timeout=10,
                                              max_size=None, open_timeout=10) as ws:
                    await ws.send(json.dumps({"assets_ids": [self.up_token, self.down_token],
                                              "type": "market"}))
                    backoff = 1.0
                    while not self._stop:
                        left = until - time.time()
                        if left <= 0:
                            return
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(left, 15.0))
                        except asyncio.TimeoutError:
                            continue
                        now = time.time()
                        msgs = json.loads(raw)
                        for x in (msgs if isinstance(msgs, list) else [msgs]):
                            self.messages += 1
                            self._handle(x, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("market ws error: %s; reconnect in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
