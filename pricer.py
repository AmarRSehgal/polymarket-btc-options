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
