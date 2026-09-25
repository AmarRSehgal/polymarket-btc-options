"""Pessimistic paper venue for resting orders.

* An order goes live `ack_s` after it is sent, and a cancel takes effect
  `cancel_s` after it is sent. Fills can land in both gaps.
* On going live the order joins the BACK of its price level: queue ahead is the
  visible size there, or zero if it improved the touch.
* Only prints move the queue. A print at our price first consumes the queue
  ahead; a print through our price means the taker reached us first. Visible
  size shrinking caps the queue ahead (you cannot be behind more than is shown)
  but a cancellation is never assumed to have been in front of us.
* No fee: Polymarket's crypto fee schedule is taker-only. The maker rebate
  (`rebateRate` 0.2) is ignored, which understates maker PnL.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from clob_ws import TapeTrade, UpBook

_ids = itertools.count(1)


@dataclass
class RestingOrder:
    oid: int
    side: str               # "buy" | "sell" (Up)
    price_c: int
    size: float
    sent_t: float
    live_t: float
    tactic: str
    fv_c: float
    remaining: float = 0.0
    queue_ahead: float | None = None    # None until live
    cancel_t: float | None = None       # when the cancel takes effect

    def __post_init__(self):
        self.remaining = self.size


@dataclass
class PaperFill:
    t: float
    oid: int
    side: str
    price_c: int
    size: float
    tactic: str
    fv_at_place_c: float
    placed_t: float


@dataclass
class PaperVenue:
    ack_s: float = 0.4
    cancel_s: float = 0.4
    orders: dict[int, RestingOrder] = field(default_factory=dict)

    def place(self, t: float, side: str, price_c: int, size: float, tactic: str, fv_c: float) -> int:
        oid = next(_ids)
        self.orders[oid] = RestingOrder(oid=oid, side=side, price_c=price_c, size=size,
                                        sent_t=t, live_t=t + self.ack_s, tactic=tactic, fv_c=fv_c)
        return oid

    def cancel(self, t: float, oid: int):
        o = self.orders.get(oid)
        if o is not None and o.cancel_t is None:
            o.cancel_t = t + self.cancel_s

    def cancel_all(self, t: float):
        for oid in list(self.orders):
            self.cancel(t, oid)

    def working(self, side: str) -> RestingOrder | None:
        """The order on `side` not yet being cancelled (at most one per side)."""
        for o in self.orders.values():
            if o.side == side and o.cancel_t is None:
                return o
        return None

    def pending_size(self, side: str) -> float:
        """Everything that could still fill on `side`, including cancels in flight."""
        return sum(o.remaining for o in self.orders.values() if o.side == side)

    def advance(self, t: float, book: UpBook):
        """Retire cancelled orders, bring acked ones live, cap queues at visible size."""
        for oid, o in list(self.orders.items()):
            if o.cancel_t is not None and t >= o.cancel_t:
                del self.orders[oid]
                continue
            if t < o.live_t:
                continue
            visible = book.level_size(o.side, o.price_c)
            if o.queue_ahead is None:
                touch = book.best_bid if o.side == "buy" else book.best_ask
                better = touch is None or (o.price_c > touch if o.side == "buy" else o.price_c < touch)
                o.queue_ahead = 0.0 if better else visible
            else:
                o.queue_ahead = min(o.queue_ahead, visible)

    def on_trade(self, tr: TapeTrade) -> list[PaperFill]:
        fills: list[PaperFill] = []
        # A taker SELL hits bids, a taker BUY lifts asks.
        side = "buy" if tr.taker_side == "sell" else "sell"
        left = tr.size
        # Best-priced orders first: they would have been reached first.
        book = sorted((o for o in self.orders.values()
                       if o.side == side and o.queue_ahead is not None and tr.t >= o.live_t),
                      key=lambda o: -o.price_c if side == "buy" else o.price_c)
        for o in book:
            if left <= 0:
                break
            through = tr.price_c < o.price_c if side == "buy" else tr.price_c > o.price_c
            at = tr.price_c == o.price_c
            if not (through or at):
                continue
            if at:
                eat = min(o.queue_ahead, left)
                o.queue_ahead -= eat
                left -= eat
            got = min(o.remaining, left)
            if got <= 0:
                continue
            left -= got
            o.remaining -= got
            fills.append(PaperFill(t=tr.t, oid=o.oid, side=o.side, price_c=o.price_c, size=got,
                                   tactic=o.tactic, fv_at_place_c=o.fv_c, placed_t=o.sent_t))
            if o.remaining <= 1e-9:
                del self.orders[o.oid]
        return fills
