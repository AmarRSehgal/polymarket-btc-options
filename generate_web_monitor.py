"""Emit the daily BTC 5-minute market monitor payload for the website.

Deliberately NOT a picks or opportunities feed. This repo's own measurement
(research/backtest.py, README "Does the edge exist?") is that the model has no
edge: Polymarket's own price forecasts the settlement better than the model
does, and what survives costs is a latency effect that reaches break-even within
a few seconds of feed staleness -- against a tool whose decision cadence is
~3.75s. Publishing ranked "opportunities" off that would be publishing
noise, so the payload is framed as a market OBSERVATION and carries the verdict
with it, in `methodology` and `scorecard`, where the front end cannot lose it.

What it actually publishes is the audit's core measurement, re-run daily on the
last 24 hours of real settled windows: does the model out-forecast the market,
and what does the latency curve look like today. If that ever flips, this is
where it shows up. `track_record` is null by the house rule in the website's
predictions/README.md -- the model's skill does not clear "real, with a CI", so
it does not get to quote one.

All probabilities and returns are DECIMAL FRACTIONS, never percent: 0.032 means
+3.2%. One convention, stated once, enforced by --validate.

Usage:
    python generate_web_monitor.py --output /tmp/btc_monitor.json
    python generate_web_monitor.py --validate /tmp/btc_monitor.json
    python generate_web_monitor.py --status --fail-if-stale
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from datetime import datetime, timezone

import click

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from research.backtest import (  # noqa: E402
    brier, build_sigma, gather_windows, observations, permutation_test,
    trade_ladder, two_sided,
)

# Matches the website validator's publish gate; a payload older than this is
# refused at publish time rather than shown as current.
FRESH_LIMIT_HOURS = 6
CLOCK_SKEW_HOURS = 1
MAX_AGE_HOURS = 30          # daily job: alarm if the published payload is older

# The lag ladder. 0 is the honest best case (both marks read at the same second);
# the negative rung is a placebo that hands the model BTC from the future, and is
# published precisely so the curve cannot be read as model skill.
LAG_LADDER = (-30, 0, 5, 10, 15, 30, 60)
PLACEBO_LAGS = frozenset({-30})

METHODOLOGY = (
    "Re-runs this repo's own backtest on the last 24h of settled Polymarket BTC "
    "5-minute windows, using real fills from data-api /trades (1s timestamps) to "
    "reconstruct a tradeable book, and grading against Polymarket's own "
    "resolution. The headline is a forecasting comparison that assumes no costs: "
    "Brier score of the Black-Scholes model against Brier of the market's own "
    "mid. The market has consistently won it. What survives costs is a latency "
    "effect -- ROI falls monotonically as the BTC feed is made staler, crossing "
    "zero at the measured `breakeven_lag_seconds`, and the negative-lag rung "
    "(future BTC) is a placebo showing what pure direction-knowledge is worth. "
    "Not a trade recommendation, and no orders are ever placed."
)


def _breakeven_lag(curve):
    """Seconds of BTC staleness at which post-cost ROI crosses zero.

    Linear interpolation between the two real (non-placebo) rungs that straddle
    zero. None if the curve never crosses inside the ladder -- better an absent
    number than one extrapolated past the data.
    """
    real = [p for p in curve if not p["placebo"]]
    real.sort(key=lambda p: p["lag_seconds"])
    for lo, hi in zip(real, real[1:]):
        if lo["roi"] >= 0.0 > hi["roi"]:
            span = lo["roi"] - hi["roi"]
            if span <= 0:
                return float(lo["lag_seconds"])
            frac = lo["roi"] / span
            return round(lo["lag_seconds"] + frac *
                         (hi["lag_seconds"] - lo["lag_seconds"]), 1)
    return None


def build_payload(windows, skip_recent, permute_rounds, halflife):
    ws, closes, spot = asyncio.run(gather_windows(windows, skip_recent))
    if not ws:
        raise RuntimeError("no settled windows returned -- Gamma or Binance unreachable")

    sigma_at, vol = build_sigma(closes, halflife)

    curve = []
    for lag in LAG_LADDER:
        obs = observations(ws, sigma_at, spot, halflife, lag, use_twap=True)
        both = two_sided(obs)
        if not both:
            continue
        _name, n, stake, pnl, wins = trade_ladder(obs, 0.0, 0.0)[2]
        curve.append({
            "lag_seconds": lag,
            "placebo": lag in PLACEBO_LAGS,
            "brier_model": round(brier([(m, w) for m, _d, _a, _b, _f, w, _r in both]), 4),
            "trades": n,
            "roi": round(pnl / stake, 4) if stake else 0.0,
            "win_rate": round(wins / n, 4) if n else 0.0,
        })
    if not curve:
        raise RuntimeError("no observable seconds in the sample")

    obs0 = observations(ws, sigma_at, spot, halflife, 0, use_twap=True)
    both0 = two_sided(obs0)
    brier_model = brier([(m, w) for m, _d, _a, _b, _f, w, _r in both0])
    brier_market = brier([(d, w) for _m, d, _a, _b, _f, w, _r in both0])
    spreads = sorted(a - b for _m, _d, a, b, _f, _w, _r in both0)

    sig = permutation_test(ws, sigma_at, spot, halflife, 0.0, 0.0, 0, 30, permute_rounds)
    roi_mean, roi_sd = sig["null_roi"]
    grad_mean, grad_sd = sig["null_grad"]

    at_zero = next(p for p in curve if p["lag_seconds"] == 0)
    ups = sum(1 for w in ws if w["outcome"] == "up")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": METHODOLOGY,
        "sample": {
            "windows": len(ws),
            "hours": round(len(ws) * 5 / 60.0, 1),
            "fills": sum(w["n_trades"] for w in ws),
            "outcomes_up": ups,
            "outcomes_down": len(ws) - ups,
            "realized_vol_annual": round(vol.annual_vol, 4),
            "median_spread": round(spreads[len(spreads) // 2], 4) if spreads else None,
        },
        "scorecard": {
            "brier_model": round(brier_model, 4),
            "brier_market": round(brier_market, 4),
            # The whole finding, as one boolean the front end cannot misread.
            "model_beats_market": bool(brier_model < brier_market),
            "roi_at_zero_lag": at_zero["roi"],
            "breakeven_lag_seconds": _breakeven_lag(curve),
            "tool_decision_cadence_seconds": 3.75,
            "roi_sd_above_null": (round((at_zero["roi"] * 100 - roi_mean) / roi_sd, 1)
                                  if roi_sd else None),
            "latency_gradient_sd_above_null": (round((sig["real_grad"] - grad_mean) / grad_sd, 1)
                                               if grad_sd else None),
            "permutation_rounds": permute_rounds,
        },
        "lag_curve": curve,
        # House rule (website predictions/README.md): quote a track record only
        # if it is real. A 3-sigma ROI on ~230 windows of a single vol regime,
        # from a model the market out-forecasts, is not one.
        "track_record": None,
        "simulation_only": True,
    }


def validate(data, now=None):
    """Raise ValueError on anything that would let the page state a falsehood."""
    now = now or datetime.now(timezone.utc)

    for key in ("generated_at", "methodology", "sample", "scorecard",
                "lag_curve", "simulation_only"):
        if key not in data:
            raise ValueError(f"missing required key {key!r}")

    stamp = data["generated_at"]
    if not isinstance(stamp, str):
        raise ValueError("generated_at must be an ISO 8601 string, never null -- "
                         "null means the generator never ran")
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        raise ValueError("generated_at has no UTC offset; browsers would parse it "
                         "as the viewer's local time")
    age = (now - parsed).total_seconds() / 3600.0
    if age > FRESH_LIMIT_HOURS:
        raise ValueError(f"generated_at is {age:.1f}h old, past the {FRESH_LIMIT_HOURS}h "
                         f"publish gate")
    if age < -CLOCK_SKEW_HOURS:
        raise ValueError(f"generated_at is {-age:.1f}h in the future")

    if data["simulation_only"] is not True:
        raise ValueError("simulation_only must be true -- this repo places no orders "
                         "and the page must not imply otherwise")

    if data.get("track_record") is not None:
        raise ValueError("track_record must be null: the model does not out-forecast "
                         "the market, so it has no skill record to quote")

    sc = data["scorecard"]
    for key in ("brier_model", "brier_market", "model_beats_market", "roi_at_zero_lag"):
        if key not in sc:
            raise ValueError(f"scorecard missing {key!r}")
    for key in ("brier_model", "brier_market", "roi_at_zero_lag"):
        if not isinstance(sc[key], (int, float)) or isinstance(sc[key], bool):
            raise ValueError(f"scorecard.{key} must be a JSON number")
    for key in ("brier_model", "brier_market"):
        if not 0.0 <= sc[key] <= 1.0:
            raise ValueError(f"scorecard.{key}={sc[key]} is not a Brier score in [0,1]")
    # The boolean and the numbers have to agree, or the page says one thing and
    # its own evidence says the other.
    if sc["model_beats_market"] != (sc["brier_model"] < sc["brier_market"]):
        raise ValueError("scorecard.model_beats_market contradicts the Brier scores")
    if abs(sc["roi_at_zero_lag"]) > 1.0:
        raise ValueError(f"roi_at_zero_lag={sc['roi_at_zero_lag']} looks like percent; "
                         f"emit a fraction (0.032), not 3.2")

    curve = data["lag_curve"]
    if not isinstance(curve, list) or not curve:
        raise ValueError("lag_curve must be a non-empty list")
    seen = set()
    for i, p in enumerate(curve):
        for key in ("lag_seconds", "placebo", "roi", "brier_model"):
            if key not in p:
                raise ValueError(f"lag_curve[{i}] missing {key!r}")
        if not isinstance(p["placebo"], bool):
            raise ValueError(f"lag_curve[{i}].placebo must be a boolean")
        # A negative lag feeds the model BTC from the future. If that were ever
        # published unflagged it would read as the strongest result on the page.
        if p["lag_seconds"] < 0 and not p["placebo"]:
            raise ValueError(f"lag_curve[{i}] has negative lag {p['lag_seconds']} but is "
                             f"not flagged placebo -- that is future information")
        if abs(p["roi"]) > 1.0:
            raise ValueError(f"lag_curve[{i}].roi={p['roi']} looks like percent")
        seen.add(p["lag_seconds"])
    if 0 not in seen:
        raise ValueError("lag_curve must include the 0s rung; it is the honest case")

    sample = data["sample"]
    if not isinstance(sample.get("windows"), int) or sample["windows"] < 1:
        raise ValueError("sample.windows must be a positive int")
    return data


@click.command()
@click.option("--output", type=click.Path(), help="Write the payload here")
@click.option("--validate", "validate_path", type=click.Path(exists=True),
              help="Validate an existing payload and exit")
@click.option("--status", is_flag=True, help="Report the age of --output and exit")
@click.option("--fail-if-stale", is_flag=True,
              help="With --status, exit 2 when the payload is older than 30h")
@click.option("--windows", default=288, help="Windows to sample (288 = 24h)")
@click.option("--skip-recent", default=12, help="Skip N recent windows (settlement lag)")
@click.option("--permute", default=100, help="Permutation rounds for the null")
@click.option("--ewma-halflife", default=30)
def main(output, validate_path, status, fail_if_stale, windows, skip_recent,
         permute, ewma_halflife):
    """Generate, validate or status-check the BTC market monitor payload."""
    if validate_path:
        with open(validate_path) as fh:
            data = json.load(fh)
        try:
            validate(data)
        except ValueError as exc:
            click.echo(f"INVALID {validate_path}: {exc}", err=True)
            sys.exit(1)
        click.echo(f"{validate_path}: OK (generated {data['generated_at']})")
        return

    if status:
        if not output:
            click.echo("--status needs --output", err=True)
            sys.exit(1)
        path = pathlib.Path(output)
        if not path.exists():
            click.echo(f"MISSING {output} -- the generator has never produced a payload")
            sys.exit(2 if fail_if_stale else 0)
        data = json.loads(path.read_text())
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(data["generated_at"])).total_seconds() / 3600.0
        click.echo(f"{output}: {age:.1f}h old ({data['sample']['windows']} windows)")
        # A job that stops running produces no error at all -- staleness is the
        # only signal that it died, so it gets its own exit code.
        if fail_if_stale and age > MAX_AGE_HOURS:
            click.echo(f"STALE: older than {MAX_AGE_HOURS}h", err=True)
            sys.exit(2)
        return

    payload = build_payload(windows, skip_recent, permute, ewma_halflife)
    validate(payload)

    text = json.dumps(payload, indent=2) + "\n"
    if output:
        pathlib.Path(output).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(output).write_text(text)
        sc = payload["scorecard"]
        click.echo(f"wrote {output}: {payload['sample']['windows']} windows, "
                   f"model Brier {sc['brier_model']} vs market {sc['brier_market']}, "
                   f"break-even {sc['breakeven_lag_seconds']}s")
    else:
        click.echo(text)


if __name__ == "__main__":
    main()
