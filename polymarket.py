"""Polymarket BTC 5-minute market discovery and live pricing.

Uses the Gamma API for market discovery (deterministic slug) and the
CLOB API for live bid/ask/midpoint prices. All read-only, no auth needed.
"""
from __future__ import annotations

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

WINDOW_SECONDS = 300

# Fallbacks used only if Gamma stops reporting the market's own config.
DEFAULT_TWAP_LOOKBACK = 60.0
DEFAULT_FEE_RATE = 0.07


class Market:
    __slots__ = (
        "slug", "question", "condition_id", "up_token", "down_token",
        "window_ts", "twap_lookback", "fee_rate", "min_order_size",
    )

    def __init__(self, slug: str, question: str, condition_id: str,
                 up_token: str, down_token: str, window_ts: int,
                 twap_lookback: float, fee_rate: float, min_order_size: float):
        self.slug = slug
        self.question = question
        self.condition_id = condition_id
        self.up_token = up_token
        self.down_token = down_token
        self.window_ts = window_ts
        self.twap_lookback = twap_lookback
        self.fee_rate = fee_rate
        self.min_order_size = min_order_size


class Prices:
    __slots__ = ("up_bid", "up_ask", "down_bid", "down_ask")

    def __init__(self, up_bid: float, up_ask: float,
                 down_bid: float, down_ask: float):
        self.up_bid = up_bid
        self.up_ask = up_ask
        self.down_bid = down_bid
        self.down_ask = down_ask


def _twap_lookback(market: dict) -> float:
    """Seconds of trailing TWAP the market settles on, 0 if point-in-time.

    Polymarket moved these markets onto Chainlink's 60s-TWAP stream: the Gamma
    payload carries cryptoMarketConfig {"id": "btc-5m-twap-60", "twapEnabled":
    true, "twapLookbackSeconds": 60}. Older markets have no config at all.
    """
    cfg = market.get("cryptoMarketConfig") or {}
    if not cfg.get("twapEnabled"):
        return 0.0
    return float(cfg.get("twapLookbackSeconds", DEFAULT_TWAP_LOOKBACK))


def _fee_rate(market: dict) -> float:
    """Taker fee rate from the market's own feeSchedule, 0 if fees are off."""
    if not market.get("feesEnabled"):
        return 0.0
    return float((market.get("feeSchedule") or {}).get("rate", DEFAULT_FEE_RATE))


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

            market = Market(
                slug=slug,
                question=m.get("question", ""),
                condition_id=m.get("conditionId", ""),
                up_token=token_ids[0],
                down_token=token_ids[1],
                window_ts=window_ts,
                twap_lookback=_twap_lookback(m),
                fee_rate=_fee_rate(m),
                min_order_size=float(m.get("orderMinSize", 0) or 0),
            )
            self._cached_market = market
            self._cached_window_ts = window_ts
            return market

        except Exception as e:
            logger.error("Failed to fetch market: %s", e)
            return None

    def get_prices(self, market: Market) -> Prices | None:
        """Best bid and ask for both tokens.

        CLOB `/price?side=BUY` returns the best bid and `side=SELL` the best
        ask (the side of the book, not the side you are taking). Verified
        against `/book`. These are four sequential REST calls and dominate the
        poll's ~2.3s latency -- see the README on why that latency is the whole
        story for this market.
        """
        try:
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
                up_bid=float(up_buy_r.json().get("price", 0)),
                up_ask=float(up_sell_r.json().get("price", 0)),
                down_bid=float(down_buy_r.json().get("price", 0)),
                down_ask=float(down_sell_r.json().get("price", 0)),
            )

        except Exception as e:
            logger.error("Failed to fetch prices: %s", e)
            return None

    def get_strike(self, window_ts: int) -> float:
        """BTC price at the window open, from the Binance 1s kline close.

        The market's real strike is the Chainlink BTC/USD TWAP print at the
        window open, which has no free feed. Binance spot is the closest public
        proxy: across the 2,010 windows in the whale-tracker capture the two
        agree on direction 95.3% of the time overall, but only 65.8% of the time
        when the window's total move is under $5 -- which is exactly the
        near-the-money regime this tool trades. Treat model output in that
        regime as carrying a large, unmodelled basis error.
        """
        try:
            resp = self._session.get(
                f"{BINANCE_API}/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1s",
                        "startTime": window_ts * 1000, "endTime": (window_ts + 5) * 1000,
                        "limit": 1},
                timeout=5,
            )
            resp.raise_for_status()
            klines = resp.json()
            return float(klines[0][4]) if klines else 0.0
        except Exception as e:
            logger.error("Failed to fetch strike: %s", e)
            return 0.0

    def get_settled_outcome(self, window_ts: int) -> str | None:
        """'up'/'down' once Polymarket has resolved the window, else None.

        Grading against Binance close-vs-open instead of this is wrong on ~1 in
        20 windows overall and ~1 in 3 of the near-the-money ones.
        """
        try:
            resp = self._session.get(
                f"{GAMMA_API}/events", params={"slug": f"btc-updown-5m-{window_ts}"}, timeout=5
            )
            resp.raise_for_status()
            events = resp.json()
            if not events or not events[0].get("markets"):
                return None
            m = events[0]["markets"][0]
            if not m.get("closed"):
                return None
            names = json.loads(m.get("outcomes", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))
            winners = [n for n, p in zip(names, prices) if float(p) > 0.95]
            return winners[0].lower() if len(winners) == 1 else None
        except Exception as e:
            logger.error("Failed to fetch outcome: %s", e)
            return None
