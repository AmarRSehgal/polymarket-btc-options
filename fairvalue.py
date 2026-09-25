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


class CompositeSpot:
    """Weighted mid across spot venues: w_i ~ 1 / max(spread_bps_i, floor)^2.

    Weights use an EW average of each venue's spread (half-life minutes), so the
    composite does not jitter as spreads flicker. The floor matters: Binance
    quotes a $0.01 tick and OKX $0.10, so raw inverse-spread-squared would hand
    OKX ~1% of the weight for its tick size alone. Floored at 0.5bp, two tight
    books weigh equally and a venue loses weight only when it genuinely widens.

    A venue older than `stale_s` drops out and the rest renormalise. If the
    venues disagree by more than `max_gap_bps`, one of them is wrong and there
    is no telling which from two, so the composite reports None and the arm pulls.
    """

    def __init__(self, names: tuple[str, ...], spread_half_life_s: float = 300.0,
                 floor_bps: float = 0.5, stale_s: float = 2.0, max_gap_bps: float = 50.0):
        self.names = names
        self.h, self.floor, self.stale_s, self.max_gap = spread_half_life_s, floor_bps, stale_s, max_gap_bps
        self.ew_spread: dict[str, float] = {}
        self._t: dict[str, float] = {}
        self.weights: dict[str, float] = {}
        self.dropped: dict[str, int] = {n: 0 for n in names}
        self.gaps = 0

    def value(self, now: float, quotes: dict[str, tuple[float, float, float]]) -> float | None:
        """quotes: name -> (mid, spread, updated). Returns the composite mid or None."""
        live = {}
        for n in self.names:
            mid, spread, upd = quotes[n]
            if mid <= 0 or now - upd > self.stale_s:
                self.dropped[n] += 1
                continue
            bps = spread / mid * 1e4
            if n in self.ew_spread:
                a = 1 - 0.5 ** (max(now - self._t[n], 0.0) / self.h)
                self.ew_spread[n] += a * (bps - self.ew_spread[n])
            else:
                self.ew_spread[n] = bps
            self._t[n] = now
            live[n] = mid
        if not live:
            return None
        mids = list(live.values())
        if (max(mids) - min(mids)) / min(mids) * 1e4 > self.max_gap:
            self.gaps += 1
            return None
        raw = {n: 1.0 / max(self.ew_spread[n], self.floor) ** 2 for n in live}
        tot = sum(raw.values())
        self.weights = {n: w / tot for n, w in raw.items()}
        return sum(self.weights[n] * live[n] for n in live)
