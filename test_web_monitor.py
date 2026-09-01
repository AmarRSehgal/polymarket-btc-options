"""Tests for the published monitor payload's validator.

    env -u PYTHONPATH /opt/local/bin/python3.13 -m pytest test_web_monitor.py

Every case here is a way the public page could state something false. The
validator exists to make each of them fail the job instead of publishing.
"""
from datetime import datetime, timedelta, timezone

import pytest

from generate_web_monitor import _breakeven_lag, validate


def _payload(**over):
    p = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": "...",
        "sample": {"windows": 219, "hours": 18.2, "fills": 435501,
                   "outcomes_up": 103, "outcomes_down": 116,
                   "realized_vol_annual": 0.2553, "median_spread": 0.01},
        "scorecard": {
            "brier_model": 0.1554,
            "brier_market": 0.1542,
            "model_beats_market": False,
            "roi_at_zero_lag": 0.0212,
            "breakeven_lag_seconds": 3.7,
            "tool_decision_cadence_seconds": 3.75,
            "roi_sd_above_null": 2.8,
            "latency_gradient_sd_above_null": 8.2,
            "permutation_rounds": 100,
        },
        "lag_curve": [
            {"lag_seconds": -30, "placebo": True, "roi": 0.1681, "brier_model": 0.1368},
            {"lag_seconds": 0, "placebo": False, "roi": 0.0212, "brier_model": 0.1554},
            {"lag_seconds": 30, "placebo": False, "roi": -0.0211, "brier_model": 0.1759},
        ],
        "track_record": None,
        "simulation_only": True,
    }
    p.update(over)
    return p


def test_the_real_payload_shape_validates():
    assert validate(_payload()) is not None


def test_null_generated_at_is_refused():
    """null means the generator never ran; publishing it fakes an empty result."""
    with pytest.raises(ValueError, match="never ran"):
        validate(_payload(generated_at=None))


def test_naive_timestamp_is_refused():
    """Browsers parse a naive ISO string as the viewer's local time."""
    naive = datetime.now().isoformat()
    with pytest.raises(ValueError, match="offset"):
        validate(_payload(generated_at=naive))


def test_stale_payload_is_refused_at_publish_time():
    old = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    with pytest.raises(ValueError, match="publish gate"):
        validate(_payload(generated_at=old))


def test_a_track_record_may_not_be_quoted():
    """The model does not out-forecast the market, so it has no skill to claim."""
    tr = {"resolved": 219, "spearman": 0.4, "ci_low": 0.1, "ci_high": 0.7}
    with pytest.raises(ValueError, match="track_record must be null"):
        validate(_payload(track_record=tr))


def test_the_verdict_boolean_must_match_its_own_evidence():
    """A flipped boolean would invert the entire finding while looking fine."""
    p = _payload()
    p["scorecard"]["model_beats_market"] = True
    with pytest.raises(ValueError, match="contradicts"):
        validate(p)


def test_brier_scores_must_be_scores():
    p = _payload()
    p["scorecard"]["brier_model"] = 15.54
    with pytest.raises(ValueError, match="Brier score"):
        validate(p)


def test_roi_emitted_as_percent_is_refused():
    p = _payload()
    p["scorecard"]["roi_at_zero_lag"] = 2.12
    with pytest.raises(ValueError, match="percent"):
        validate(p)


def test_an_unflagged_negative_lag_is_refused():
    """Negative lag feeds the model BTC from the future.

    Unflagged it would render as the strongest row on the page -- an 17% ROI
    that is pure lookahead. It must always carry placebo: true.
    """
    p = _payload()
    p["lag_curve"][0]["placebo"] = False
    with pytest.raises(ValueError, match="placebo"):
        validate(p)


def test_the_zero_lag_rung_is_mandatory():
    """It is the honest case; a curve without it is all placebo and staleness."""
    p = _payload()
    p["lag_curve"] = [r for r in p["lag_curve"] if r["lag_seconds"] != 0]
    with pytest.raises(ValueError, match="0s rung"):
        validate(p)


def test_simulation_only_cannot_be_dropped():
    with pytest.raises(ValueError, match="simulation_only"):
        validate(_payload(simulation_only=False))


def test_breakeven_interpolates_between_the_straddling_rungs():
    curve = [{"lag_seconds": 0, "placebo": False, "roi": 0.02},
             {"lag_seconds": 10, "placebo": False, "roi": -0.02}]
    assert _breakeven_lag(curve) == 5.0


def test_breakeven_ignores_the_placebo_rung():
    """The placebo is always positive; including it would move the crossing."""
    curve = [{"lag_seconds": -30, "placebo": True, "roi": 0.17},
             {"lag_seconds": 0, "placebo": False, "roi": 0.02},
             {"lag_seconds": 10, "placebo": False, "roi": -0.02}]
    assert _breakeven_lag(curve) == 5.0


def test_breakeven_is_none_when_the_curve_never_crosses():
    """Better an absent number than one extrapolated past the ladder."""
    curve = [{"lag_seconds": 0, "placebo": False, "roi": 0.02},
             {"lag_seconds": 60, "placebo": False, "roi": 0.01}]
    assert _breakeven_lag(curve) is None
