"""Polymarket BTC 5-minute market discovery and live pricing.

Uses the Gamma API for market discovery (deterministic slug) and the
CLOB API for live bid/ask/midpoint prices. All read-only, no auth needed.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

WINDOW_SECONDS = 300


class Market:
    __slots__ = (
        "slug", "question", "condition_id", "up_token", "down_token",
        "window_ts", "opening_price",
    )

    def __init__(self, slug: str, question: str, condition_id: str,
                 up_token: str, down_token: str, window_ts: int,
                 opening_price: float):
        self.slug = slug
        self.question = question
        self.condition_id = condition_id
        self.up_token = up_token
        self.down_token = down_token
        self.window_ts = window_ts
        self.opening_price = opening_price


class Prices:
    __slots__ = ("up_mid", "down_mid", "up_bid", "up_ask", "down_bid", "down_ask")

    def __init__(self, up_mid: float, down_mid: float,
                 up_bid: float, up_ask: float,
                 down_bid: float, down_ask: float):
        self.up_mid = up_mid
        self.down_mid = down_mid
        self.up_bid = up_bid
        self.up_ask = up_ask
        self.down_bid = down_bid
        self.down_ask = down_ask


def _parse_opening_price(question: str) -> float:
    match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", question)
    if match:
        return float(match.group(1).replace(",", ""))
    return 0.0


class PolymarketClient:
    def __init__(self):
        self._session = requests.Session()
        self._cached_market: Market | None = None
        self._cached_window_ts = 0

    @staticmethod
    def current_window_ts() -> int:
        now = int(time.time())
        return now - (now % WINDOW_SECONDS)

    @staticmethod
    def time_remaining() -> float:
        now = time.time()
        window_end = (int(now) - int(now) % WINDOW_SECONDS) + WINDOW_SECONDS
        return max(0.0, window_end - now)

    def get_market(self, window_ts: int | None = None) -> Market | None:
        if window_ts is None:
            window_ts = self.current_window_ts()

        if window_ts == self._cached_window_ts and self._cached_market:
            return self._cached_market

        slug = f"btc-updown-5m-{window_ts}"
        try:
            resp = self._session.get(
                f"{GAMMA_API}/events", params={"slug": slug}, timeout=5
            )
            resp.raise_for_status()
            events = resp.json()

            if not events or not events[0].get("markets"):
                logger.debug("No market for slug %s", slug)
                return None

            m = events[0]["markets"][0]
            token_ids = json.loads(m["clobTokenIds"])
            question = m.get("question", "")
            opening_price = _parse_opening_price(question)

            market = Market(
                slug=slug,
                question=question,
                condition_id=m.get("conditionId", ""),
                up_token=token_ids[0],
                down_token=token_ids[1],
                window_ts=window_ts,
                opening_price=opening_price,
            )
            self._cached_market = market
            self._cached_window_ts = window_ts
            return market

        except Exception as e:
            logger.error("Failed to fetch market: %s", e)
            return None

    def get_prices(self, market: Market) -> Prices | None:
        try:
            up_mid_r = self._session.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": market.up_token},
                timeout=5,
            )
            down_mid_r = self._session.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": market.down_token},
                timeout=5,
            )
            up_buy_r = self._session.get(
                f"{CLOB_API}/price",
                params={"token_id": market.up_token, "side": "BUY"},
                timeout=5,
            )
            up_sell_r = self._session.get(
                f"{CLOB_API}/price",
                params={"token_id": market.up_token, "side": "SELL"},
                timeout=5,
            )
            down_buy_r = self._session.get(
                f"{CLOB_API}/price",
                params={"token_id": market.down_token, "side": "BUY"},
                timeout=5,
            )
            down_sell_r = self._session.get(
                f"{CLOB_API}/price",
                params={"token_id": market.down_token, "side": "SELL"},
                timeout=5,
            )

            return Prices(
                up_mid=float(up_mid_r.json().get("mid", 0)),
                down_mid=float(down_mid_r.json().get("mid", 0)),
                up_bid=float(up_buy_r.json().get("price", 0)),
                up_ask=float(up_sell_r.json().get("price", 0)),
                down_bid=float(down_buy_r.json().get("price", 0)),
                down_ask=float(down_sell_r.json().get("price", 0)),
            )

        except Exception as e:
            logger.error("Failed to fetch prices: %s", e)
            return None
