"""Tests for the paper maker: edge curve, placement, queue model, anchor, tape mapping."""
import math

import pytest

from clob_ws import TapeTrade, UpBook, up_equivalent
from fairvalue import Anchor, logit, prob_sigma_c, sigmoid
from maker import MakerParams, desired_quotes, edge_c, gate
from paper_venue import PaperVenue

P = MakerParams()


def book(bid=49, ask=51, bid_sz=100.0, ask_sz=100.0, t=0.0):
    b = UpBook()
    b.apply_snapshot([{"price": bid / 100, "size": bid_sz}], [{"price": ask / 100, "size": ask_sz}], t)
    return b


def test_params_check_rejects_curve_that_cannot_flatten():
    MakerParams().check()
    with pytest.raises(ValueError):
        MakerParams(k_base_c=3.0, k_skew_c=2.0).check()
    with pytest.raises(ValueError):
        MakerParams(q_max=4.0, order_size=5.0).check()


def test_edge_skews_against_inventory():
    assert edge_c(0.0, 0.0, P) == P.k_base_c
    assert edge_c(1.0, 0.0, P) == P.k_base_c + P.k_skew_c
    assert edge_c(-1.0, 0.0, P) < 0          # reducing side quotes through FV
    assert edge_c(5.0, 0.0, P) == edge_c(1.0, 0.0, P)


def test_flat_quotes_improve_by_one_tick_not_to_the_limit():
    bid, ask = desired_quotes(50.0, 0.0, 0.0, 45, 55, P)
    assert (bid.price_c, ask.price_c) == (46, 54)
    assert bid.tactic == ask.tactic == "improve"


def test_rounding_moves_away_from_fair_value():
    bid, ask = desired_quotes(50.4, 0.0, 0.0, 40, 60, P)
    assert bid.price_c <= 50.4 - P.k_base_c
    assert ask.price_c >= 50.4 + P.k_base_c


def test_never_crosses_the_book():
    for pos in (-10.0, -5.0, 0.0, 5.0, 10.0):
        bid, ask = desired_quotes(50.0, 0.0, pos, 49, 50, P)
        if bid:
            assert bid.price_c <= 49
        if ask:
            assert ask.price_c >= 50


@pytest.mark.parametrize("fv,pos,bb,ba,sig", [(50.0, 0.0, 47, 53, 0.3), (61.7, 5.0, 58, 64, 1.1),
                                              (38.2, -5.0, 30, 45, 0.0), (50.0, 5.0, 49, 50, 0.5)])
def test_sell_side_mirrors_buy_side(fv, pos, bb, ba, sig):
    bid, ask = desired_quotes(fv, sig, pos, bb, ba, P)
    mbid, mask = desired_quotes(100 - fv, sig, -pos, 100 - ba, 100 - bb, P)
    for q, m in ((bid, mask), (ask, mbid)):
        assert (q is None) == (m is None)
        if q:
            assert q.price_c == 100 - m.price_c and q.tactic == m.tactic
            assert math.isclose(q.edge_c, m.edge_c)


def test_full_inventory_stops_adding():
    bid, ask = desired_quotes(50.0, 0.0, P.q_max, 45, 55, P)
    assert bid is None and ask is not None


def test_gate_order_and_pull_before_close():
    kw = dict(strike_ok=True, vol_ready=True, fv_c=50.0, book_bid=49, book_ask=51,
              binance_updated=1000.0, uses_binance=True, p=P)
    assert gate(now=1000.0, window_ts=900, book_updated=1000.0, **kw) is None
    assert gate(now=1000.0, window_ts=995, book_updated=1000.0, **kw) == "early_window"
    assert gate(now=1000.0, window_ts=1000 - 300 + 30, book_updated=1000.0, **kw) == "late_window"
    assert gate(now=1000.0, window_ts=900, book_updated=990.0, **kw) == "book_stale"
    kw["fv_c"] = 70.0
    assert gate(now=1000.0, window_ts=900, book_updated=1000.0, **kw) == "fv_gap"


def test_joining_waits_behind_visible_queue():
    v, b = PaperVenue(ack_s=0.0), book(49, 51, bid_sz=30.0)
    v.place(0.0, "buy", 49, 5.0, "join", 50.0)
    v.advance(0.0, b)
    assert v.on_trade(TapeTrade(0.1, "sell", 49, 20.0, "a")) == []
    fills = v.on_trade(TapeTrade(0.2, "sell", 49, 12.0, "b"))
    assert sum(f.size for f in fills) == 2.0


def test_improving_is_first_in_queue_and_print_through_fills():
    v, b = PaperVenue(ack_s=0.0), book(49, 52)
    v.place(0.0, "sell", 51, 5.0, "improve", 50.0)
    v.advance(0.0, b)
    assert sum(f.size for f in v.on_trade(TapeTrade(0.1, "buy", 52, 3.0, "a"))) == 3.0
    assert v.on_trade(TapeTrade(0.2, "sell", 49, 50.0, "b")) == []


def test_not_live_until_acked_and_fills_during_cancel():
    v, b = PaperVenue(ack_s=0.4, cancel_s=0.4), book(49, 52)
    oid = v.place(0.0, "buy", 50, 5.0, "improve", 51.0)
    v.advance(0.1, b)
    assert v.on_trade(TapeTrade(0.2, "sell", 50, 5.0, "a")) == []
    v.advance(0.5, b)
    v.cancel(0.6, oid)
    v.advance(0.7, b)
    assert sum(f.size for f in v.on_trade(TapeTrade(0.8, "sell", 50, 5.0, "b"))) == 5.0
    assert v.pending_size("buy") == 0.0


def test_down_prints_map_to_up():
    assert up_equivalent(False, "BUY", "0.30") == ("sell", 70)
    assert up_equivalent(False, "SELL", "0.30") == ("buy", 70)
    assert up_equivalent(True, "BUY", "0.30") == ("buy", 30)


def test_anchor_keeps_market_level_and_moves_with_model():
    a = Anchor(half_life_s=20.0, seed_s=5.0)
    for t in range(10):
        a.update(float(t), 0.60, 0.50)
    assert a.value(0.50) == pytest.approx(0.60, abs=1e-6)
    moved = a.value(0.55)
    assert moved == pytest.approx(sigmoid(logit(0.60) + logit(0.55) - logit(0.50)))
    assert 0.60 < moved < 0.70


def test_anchor_unseeded_until_seed_window():
    a = Anchor(seed_s=10.0)
    a.update(0.0, 0.5, 0.5)
    a.update(5.0, 0.5, 0.5)
    assert a.value(0.5) is None


def test_prob_sigma_peaks_at_the_money():
    atm = prob_sigma_c(100_000, 100_000, 120, 0.4, 5)
    otm = prob_sigma_c(100_300, 100_000, 120, 0.4, 5)
    assert atm > otm > 0


from fairvalue import CompositeSpot


def test_composite_ignores_tick_size_difference():
    c = CompositeSpot(("binance", "okx"))
    v = c.value(10.0, {"binance": (84_000.00, 0.01, 10.0), "okx": (84_010.0, 0.10, 10.0)})
    assert v == pytest.approx(84_005.0)
    assert c.weights["binance"] == pytest.approx(0.5)


def test_composite_downweights_a_wide_venue():
    c = CompositeSpot(("binance", "okx"))
    c.value(10.0, {"binance": (84_000.0, 0.01, 10.0), "okx": (84_000.0, 42.0, 10.0)})  # okx 5bp wide
    assert c.weights["okx"] < 0.02


def test_composite_drops_stale_and_refuses_disagreement():
    c = CompositeSpot(("binance", "okx"), stale_s=2.0, max_gap_bps=50.0)
    assert c.value(10.0, {"binance": (84_000.0, 0.01, 10.0), "okx": (90_000.0, 0.1, 5.0)}) == 84_000.0
    assert c.value(10.0, {"binance": (84_000.0, 0.01, 10.0), "okx": (85_000.0, 0.1, 10.0)}) is None
    assert c.value(10.0, {"binance": (84_000.0, 0.01, 1.0), "okx": (84_000.0, 0.1, 1.0)}) is None
