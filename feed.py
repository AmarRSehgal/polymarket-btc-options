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
