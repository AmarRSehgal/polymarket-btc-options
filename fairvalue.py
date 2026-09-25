"""Fair value for the paper maker: three definitions, one per arm.

The backtest's verdict is that the market's own price out-forecasts the model
(Brier 0.1549 vs 0.1557) while BTC feed freshness is worth a smooth 3+ points of
ROI. The standard answer to that shape is to keep the slow venue's level and move
it by the fast venue's change.

    mid       quote around the Polymarket mid. No outside information -- the
              "quoting around the slow venue's own mid" mistake, kept as the
              control that says how much the Binance lead is worth.
    model     quote around N(d2) alone -- the original tool's fair value.
    anchored  FV = sigmoid(EW[logit mid] + logit(model now) - EW[logit model])

Anchoring is done in logit space so the moved level stays inside (0, 1) and a
given BTC move shifts a 50c contract more than a 90c one, as N(d2) does.
"""
from __future__ import annotations

import math

from pricer import binary_call_price

P_CLAMP = 0.01


def logit(p: float) -> float:
    p = min(max(p, P_CLAMP), 1.0 - P_CLAMP)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class Anchor:
    """EW means of logit(market mid) and logit(model), same half-life.

    Seeded, not zero-initialised: until `seed_s` of observations have arrived
    the anchor reports unseeded and the arm does not quote.
    """

    def __init__(self, half_life_s: float = 20.0, seed_s: float = 10.0):
        self.half_life_s = half_life_s
        self.seed_s = seed_s
        self.reset()

    def reset(self):
        self.a_mkt: float | None = None
        self.a_model: float | None = None
        self._first_t: float | None = None
        self._last_t: float | None = None

    def update(self, t: float, mid_p: float, model_p: float):
        lm, lo = logit(mid_p), logit(model_p)
        if self.a_mkt is None:
            self.a_mkt, self.a_model = lm, lo
            self._first_t = self._last_t = t
            return
        dt = max(t - self._last_t, 0.0)
        alpha = 1.0 - 0.5 ** (dt / self.half_life_s)
        self.a_mkt += alpha * (lm - self.a_mkt)
        self.a_model += alpha * (lo - self.a_model)
        self._last_t = t

    @property
    def seeded(self) -> bool:
        return (self._first_t is not None and self._last_t is not None
                and self._last_t - self._first_t >= self.seed_s)

    def value(self, model_p: float) -> float | None:
        if not self.seeded:
            return None
        return sigmoid(self.a_mkt + logit(model_p) - self.a_model)


def prob_sigma_c(S: float, K: float, t_eff: float, sigma: float, horizon_s: float) -> float:
    """Typical move of P(Up), in cents, over `horizon_s` of BTC movement.

    sigma_p = |dp/dS| * S * sigma_S, done as a symmetric finite
    difference so it stays right where N(d2) is steep near expiry.
    """
    if S <= 0 or K <= 0 or t_eff <= 0 or sigma <= 0:
        return 0.0
    h = sigma * math.sqrt(horizon_s / (365.25 * 24 * 3600))
    up = binary_call_price(S * math.exp(h), K, t_eff, sigma)
    dn = binary_call_price(S * math.exp(-h), K, t_eff, sigma)
    return abs(up - dn) / 2.0 * 100.0
