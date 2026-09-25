"""Lightweight Binance BTCUSDT spot trade stream (futures optional).

Connects to the aggTrade websocket and dispatches Trade objects to callbacks.
Reconnects automatically on disconnect.
"""
import asyncio
import json
import logging
from dataclasses import dataclass

import websockets

logger = logging.getLogger(__name__)

BINANCE_WS_SPOT = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_WS_FUTURES = "wss://fstream.binance.com/ws/btcusdt@aggTrade"


@dataclass
class Trade:
    price: float
    qty: float
    ts: float
    is_buyer_maker: bool


class BinanceFeed:
    def __init__(self, use_futures: bool = False):
        self._url = BINANCE_WS_FUTURES if use_futures else BINANCE_WS_SPOT
        self._callbacks: list = []
        self._running = False

    def on_trade(self, callback):
        self._callbacks.append(callback)

    async def run(self):
        self._running = True
        reconnect_delay = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self._url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logger.info("Connected to %s", self._url)
                    reconnect_delay = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        trade = Trade(
                            price=float(msg["p"]),
                            qty=float(msg["q"]),
                            ts=msg["T"] / 1000.0,
                            is_buyer_maker=msg["m"],
                        )
                        for cb in self._callbacks:
                            cb(trade)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Feed disconnected: %s. Reconnect in %.1fs", e, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def stop(self):
        self._running = False


BINANCE_WS_BOOK = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"


class BinanceBookTicker:
    """Best bid/offer mid, pushed. The paper maker's fast reference.

    The mid is used rather than the last trade: an aggTrade price bounces between
    bid and ask, which reads as volatility and as fair-value jitter that is not there.
    The spot bookTicker stream carries no event time, so `updated` is local receipt.
    """

    def __init__(self, url: str = BINANCE_WS_BOOK):
        self._url = url
        self._callbacks: list = []
        self._running = False
        self.mid = 0.0
        self.spread = 0.0
        self.updated = 0.0

    def on_mid(self, callback):
        self._callbacks.append(callback)

    async def run(self):
        import time
        self._running = True
        reconnect_delay = 1.0
        while self._running:
            try:
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=10, max_queue=None) as ws:
                    logger.info("Connected to %s", self._url)
                    reconnect_delay = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        bid, ask = float(msg["b"]), float(msg["a"])
                        self.mid, self.spread = (bid + ask) / 2.0, ask - bid
                        self.updated = time.time()
                        for cb in self._callbacks:
                            cb(self.mid, self.updated)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Book feed disconnected: %s. Reconnect in %.1fs", e, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def stop(self):
        self._running = False


OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"


class OkxBBO:
    """OKX BTC-USDT top of book (`bbo-tbt`, tick by tick), same shape as BinanceBookTicker."""

    def __init__(self, inst: str = "BTC-USDT"):
        self.inst = inst
        self.mid = 0.0
        self.spread = 0.0
        self.updated = 0.0
        self._running = False

    async def run(self):
        import time
        self._running = True
        reconnect_delay = 1.0
        while self._running:
            try:
                async with websockets.connect(OKX_WS, ping_interval=20, ping_timeout=10, max_queue=None) as ws:
                    await ws.send(json.dumps({"op": "subscribe",
                                              "args": [{"channel": "bbo-tbt", "instId": self.inst}]}))
                    logger.info("Connected to %s %s", OKX_WS, self.inst)
                    reconnect_delay = 1.0
                    async for raw in ws:
                        for d in json.loads(raw).get("data") or ():
                            if not d.get("bids") or not d.get("asks"):
                                continue
                            bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
                            self.mid, self.spread = (bid + ask) / 2.0, ask - bid
                            self.updated = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("OKX feed disconnected: %s. Reconnect in %.1fs", e, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def stop(self):
        self._running = False
