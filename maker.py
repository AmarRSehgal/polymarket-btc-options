"""Passive quoting around a fair value, for one 5-minute window.

Taking is what the original tool does and what its backtest killed: the 1.75c
taker fee at the money is 3.5x the half-spread. Resting orders pay no fee
(`feeSchedule.takerOnly`), so the maker's cost is adverse selection only, and
that is what the markouts measure.

Everything is in Up-token cents. Inventory is signed Up shares; a short Up
position is economically a long Down position at 1 - p.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class MakerParams:
    order_size: float = 5.0         # Polymarket orderMinSize
    q_max: float = 10.0             # |inventory| limit in shares; ~$5 at risk, like the taker's cap
    k_base_c: float = 1.0           # edge at flat inventory: no maker fee, so this is all adverse selection
    k_vol: float = 1.0              # one typical sigma_p of extra edge
    k_skew_c: float = 2.0           # edge shift at |q| = 1; reducing side crosses FV at q = k_base / k_skew
    sigma_horizon_s: float = 5.0    # exposure horizon for sigma_p
    start_after_open_s: float = 15.0
    pull_before_close_s: float = 60.0   # the 60s settlement TWAP starts here; the model cannot see it
    band_lo_c: int = 10
    band_hi_c: int = 90
    max_fv_gap_c: float = 8.0       # FV and the book disagree by more than this: pull
    binance_stale_s: float = 2.0
    book_stale_s: float = 5.0
    replace_ticks: int = 1
    min_order_life_s: float = 0.5

    def check(self):
        """Pre-flight sanity check. Misconfiguration is fatal."""
        if self.order_size <= 0 or self.q_max < self.order_size:
            raise ValueError("q_max must hold at least one order")
        if self.k_base_c < 0 or self.k_skew_c <= 0:
            raise ValueError("k_base_c must be >= 0 and k_skew_c > 0")
        cross = self.k_base_c / self.k_skew_c
        if not 0 < cross < 1:
            raise ValueError(f"reducing side crosses FV at q={cross:.2f}; must be inside (0, 1)")
        if not 0 < self.band_lo_c < self.band_hi_c < 100:
            raise ValueError("price band must sit inside (0, 100)")


@dataclass(frozen=True)
class Quote:
    side: str          # "buy" | "sell"
    price_c: int
    size: float
    tactic: str        # "improve" | "join" | "rest"
    edge_c: float      # distance from FV at placement, positive = passive side


def edge_c(q_side: float, sigma_c: float, p: MakerParams) -> float:
    """Required edge for one side. q_side > 0 means this side adds to the position."""
    x = max(-1.0, min(1.0, q_side))
    return p.k_base_c + p.k_vol * sigma_c + p.k_skew_c * x


def gate(*, now: float, window_ts: int, strike_ok: bool, vol_ready: bool, fv_c: float | None,
         book_bid: int | None, book_ask: int | None, book_updated: float,
         binance_updated: float, uses_binance: bool, p: MakerParams) -> str | None:
    """Name of the first check that blocks quoting, or None. Order matters: the
    name is the first thing to read when an arm is quiet."""
    elapsed = now - window_ts
    remaining = window_ts + 300 - now
    if elapsed < p.start_after_open_s:
        return "early_window"
    if remaining < p.pull_before_close_s:
        return "late_window"
    if now - book_updated > p.book_stale_s:
        return "book_stale"
    if book_bid is None or book_ask is None or book_bid >= book_ask:
        return "book_one_sided"
    if uses_binance:
        if not strike_ok:
            return "no_strike"
        if not vol_ready:
            return "vol_warmup"
        if now - binance_updated > p.binance_stale_s:
            return "binance_stale"
    if fv_c is None:
        return "fv_unseeded"
    if abs(fv_c - (book_bid + book_ask) / 2.0) > p.max_fv_gap_c:
        return "fv_gap"
    return None


def desired_quotes(fv_c: float, sigma_c: float, position: float, best_bid: int, best_ask: int,
                   p: MakerParams) -> tuple[Quote | None, Quote | None]:
    """The two passive quotes for this instant. Either may be None.

    Placement: compute the most aggressive admissible
    price from the edge curve, round AWAY from FV, then take the least
    aggressive of {that, one tick inside the touch, one tick off the far side}.
    Improving by exactly one tick buys first place in the queue without giving
    away edge the curve did not ask for.
    """
    q = position / p.q_max
    out: list[Quote | None] = []
    for side, s in (("buy", 1), ("sell", -1)):
        e = edge_c(s * q, sigma_c, p)
        if side == "buy":
            limit = math.floor(fv_c - e + 1e-9)
            price = min(limit, best_bid + 1, best_ask - 1)
            room = p.q_max - position
        else:
            limit = math.ceil(fv_c + e - 1e-9)
            price = max(limit, best_ask - 1, best_bid + 1)
            room = p.q_max + position
        size = min(p.order_size, room)
        if size < p.order_size or not (p.band_lo_c <= price <= p.band_hi_c):
            out.append(None)
            continue
        touch = best_bid if side == "buy" else best_ask
        tactic = "join" if price == touch else ("improve" if s * (price - touch) > 0 else "rest")
        out.append(Quote(side=side, price_c=price, size=size, tactic=tactic,
                         edge_c=s * (fv_c - price)))
    return out[0], out[1]
