"""Binary option pricing via Black-Scholes.

Prices cash-or-nothing binary calls/puts as N(d2) -- the risk-neutral
probability of finishing in-the-money.
"""
import math

SECONDS_PER_YEAR = 365.25 * 24 * 3600


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def binary_call_price(
    S: float, K: float, T_seconds: float, sigma: float, r: float = 0.0
) -> float:
    """Price of a binary call (pays $1 if S >= K at expiry).

    S: current price
    K: strike price
    T_seconds: time to expiry in seconds
    sigma: annualized volatility
    r: risk-free rate (negligible for short expiries)
    """
    if T_seconds <= 0:
        return 1.0 if S >= K else 0.0
    if sigma <= 0:
        return 1.0 if S >= K else 0.0

    T = T_seconds / SECONDS_PER_YEAR
    d2 = (math.log(S / K) + (r - 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d2)


def binary_put_price(
    S: float, K: float, T_seconds: float, sigma: float, r: float = 0.0
) -> float:
    return 1.0 - binary_call_price(S, K, T_seconds, sigma, r)


def edge_cents(model_price: float, market_price: float) -> float:
    """Theoretical edge in cents. Positive = market is cheap (buy)."""
    return (model_price - market_price) * 100


def twap_effective_seconds(remaining_seconds: float, lookback_seconds: float) -> float:
    """Time-to-expiry to use when the contract settles on a trailing TWAP.

    Polymarket's BTC 5-min markets now resolve on Chainlink's 60s-TWAP stream
    (cryptoMarketConfig `btc-5m-twap-60`), not a point-in-time print. Averaging
    the final `lookback_seconds` cuts the variance of the settlement value: for
    driftless GBM the variance of the average over a window of length L is
    sigma^2 * L/3, so an option expiring into it behaves like one with

        T_eff = (time until averaging starts) + L/3

    Once inside the averaging window this ignores the already-realised part of
    the average, which we cannot see without the Chainlink stream itself. That
    makes the estimate conservative (too much residual uncertainty) near expiry.
    """
    if lookback_seconds <= 0:
        return remaining_seconds
    before = max(remaining_seconds - lookback_seconds, 0.0)
    inside = min(remaining_seconds, lookback_seconds)
    return before + inside / 3.0


def taker_fee_per_share(price: float, rate: float = 0.07) -> float:
    """Polymarket crypto_fees_v2 taker fee, in dollars per share.

    fee = rate * p * (1 - p), so it peaks at 1.75c/share at p = 0.50 and falls
    towards the wings. Against a 1c tick and a 1-2c book this is the dominant
    cost of taking, and it is charged on top of the ask.
    """
    return rate * price * (1.0 - price)
