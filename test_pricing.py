"""Unit tests for the pricing, fee and simulator logic.

    env -u PYTHONPATH /opt/local/bin/python3.13 -m pytest test_pricing.py
"""
import math

from polymarket import _fee_rate, _twap_lookback
from pricer import (binary_call_price, binary_put_price, taker_fee_per_share,
                    twap_effective_seconds)
from simulator import Simulator
from vol import VolEstimator

TWAP_MARKET = {
    "cryptoMarketConfig": {"id": "btc-5m-twap-60", "twapEnabled": True,
                           "twapLookbackSeconds": 60},
    "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True},
}


def test_atm_binary_is_a_coin_flip_less_the_variance_drag():
    """N(d2) sits just under 0.5 at the money: the -sigma^2/2 term in d2.

    Worth 3bp over 5 minutes, so it is real but nowhere near the 1c tick.
    """
    p = binary_call_price(100.0, 100.0, 300, 0.5)
    assert 0.499 < p < 0.5
    assert math.isclose(p + binary_put_price(100.0, 100.0, 300, 0.5), 1.0)


def test_call_and_put_are_complements():
    p = binary_call_price(101.0, 100.0, 300, 0.5)
    assert math.isclose(p + binary_put_price(101.0, 100.0, 300, 0.5), 1.0)


def test_expired_binary_pays_on_moneyness():
    assert binary_call_price(101.0, 100.0, 0, 0.5) == 1.0
    assert binary_call_price(99.0, 100.0, 0, 0.5) == 0.0


def test_twap_shortens_effective_expiry():
    # Outside the averaging window: full time up to it, then a third of it.
    assert twap_effective_seconds(120, 60) == 60 + 20
    # Inside it: only a third of what is left still matters.
    assert twap_effective_seconds(30, 60) == 10
    # No TWAP configured leaves the horizon untouched.
    assert twap_effective_seconds(120, 0) == 120


def test_twap_pushes_probabilities_away_from_even():
    """A TWAP settle is more determined than a point-in-time one.

    Ignoring it makes the model quote nearer 0.50 than fair, so it reads a
    correctly-priced market as offering edge on the losing side.
    """
    rem, s, k, sigma = 60.0, 100.05, 100.0, 0.5
    point = binary_call_price(s, k, rem, sigma)
    twap = binary_call_price(s, k, twap_effective_seconds(rem, 60), sigma)
    assert twap > point > 0.5


def test_taker_fee_peaks_at_the_money():
    assert math.isclose(taker_fee_per_share(0.5), 0.0175)
    assert taker_fee_per_share(0.5) > taker_fee_per_share(0.9)
    assert taker_fee_per_share(0.5, rate=0.0) == 0.0


def test_market_config_parsing():
    assert _twap_lookback(TWAP_MARKET) == 60.0
    assert _fee_rate(TWAP_MARKET) == 0.07
    assert _twap_lookback({}) == 0.0
    assert _fee_rate({"feesEnabled": False}) == 0.0


def test_fee_can_erase_a_thin_edge():
    """A 1c gross edge at the money does not survive a 1.75c fee."""
    sim = Simulator()
    assert sim.try_trade(1, "up", 0.50, 0.51, 120, fee_rate=0.0) is not None
    sim2 = Simulator()
    assert sim2.try_trade(1, "up", 0.50, 0.51, 120, fee_rate=0.07) is None


def test_exposure_cap_counts_the_fee():
    sim = Simulator(max_exposure_per_market=1.0)
    for _ in range(10):
        sim.try_trade(1, "up", 0.30, 0.90, 120, fee_rate=0.07)
    assert sim.window_exposure(1) <= 1.0


def test_no_trades_in_final_30s():
    sim = Simulator()
    assert sim.try_trade(1, "up", 0.10, 0.90, 29) is None
    assert sim.try_trade(1, "up", 0.10, 0.90, 31) is not None


def test_longshot_floor():
    sim = Simulator()
    assert sim.try_trade(1, "down", 0.05, 0.19, 120) is None


def test_pnl_is_net_of_fees():
    sim = Simulator(bankroll=10.0)
    pos = sim.try_trade(1, "up", 0.50, 0.90, 120, fee_rate=0.07)
    assert pos is not None and pos.fee > 0
    sim.resolve_window(1, "up")
    assert math.isclose(sim.total_pnl, 1.0 - 0.50 - pos.fee)
    assert math.isclose(sim.total_fees, pos.fee)


def test_losing_trade_pays_the_fee_too():
    sim = Simulator(bankroll=10.0)
    pos = sim.try_trade(1, "up", 0.50, 0.90, 120, fee_rate=0.07)
    sim.resolve_window(1, "down")
    assert math.isclose(sim.total_pnl, -(0.50 + pos.fee))


def test_vol_estimator_recovers_a_known_sigma():
    est = VolEstimator(bar_interval=60, ewma_halflife=5, default_annual_vol=0.5)
    price, ts = 100.0, 0.0
    for i in range(400):
        ts += 60
        price *= math.exp(0.001 if i % 2 else -0.001)
        est.on_trade(price, ts)
    expected = 0.001 * math.sqrt(525960)
    assert est.annual_vol == 0.5 * 0 + expected or abs(est.annual_vol - expected) < 0.05


def test_bankroll_cannot_go_negative():
    sim = Simulator(bankroll=0.4)
    assert sim.try_trade(1, "up", 0.50, 0.90, 120) is None


def test_no_trading_before_the_vol_estimator_warms_up():
    sim = Simulator()
    assert sim.try_trade(1, "up", 0.10, 0.90, 120, vol_ready=False) is None
    assert sim.try_trade(1, "up", 0.10, 0.90, 120, vol_ready=True) is not None
