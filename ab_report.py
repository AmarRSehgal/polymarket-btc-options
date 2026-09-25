"""Score the paper A/B: is any maker better than the taker it replaces?

Every window counts, including the ones an arm sat out (zero PnL), so the arms
share one denominator: the settled windows the runner observed. A window is the
unit of evidence -- every share in it settles on the same coin flip -- so every
interval here resamples windows, never fills, and every comparison is paired on
the same windows.

    env -u PYTHONPATH /opt/local/bin/python3.13 ab_report.py            # table
    env -u PYTHONPATH /opt/local/bin/python3.13 ab_report.py --output web/pm_btc_paper.json
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "paper_data"
MIN_WINDOWS = 200          # no verdict before this many settled windows, fixed before the first fill
BOOT = 2000

ARMS = {
    "taker_v1": ("control", "The original tool, unchanged: buys the ask when N(d2) beats ask + taker fee, "
                            "polling REST every 3s."),
    "maker_mid": ("treatment", "Rests quotes around the Polymarket mid. No outside information -- measures "
                               "what passive quoting alone is worth."),
    "maker_model": ("treatment", "Rests quotes around the N(d2) model price."),
    "maker_anchored": ("treatment", "Rests quotes around the Polymarket mid moved by the Binance-implied change "
                                    "since its recent average."),
    "maker_composite": ("treatment", "As maker_anchored, with BTC a spread-weighted Binance + OKX mid "
                                     "(added 2026-09-25, after the other arms; compared on the windows it ran)."),
}

KILL = [f"A maker arm whose mean PnL per window is below zero at 95% confidence after {MIN_WINDOWS} "
        "settled windows is stopped.",
        "A maker arm with a negative mean 5s markout after 500 fills is being picked off; stopped.",
        "These were written before the first paper fill (2026-09-25) and are not moved after seeing results."]

CAVEATS = [
    "Paper fills. A resting order joins the back of its price level, goes live 0.4s after it is sent, "
    "and fills only when a real print reaches it; a cancel takes 0.4s and can still be filled meanwhile.",
    "Maker rebates are ignored and the taker pays the published fee, so the maker side is understated.",
    "Only prints on the websocket move the paper queue; hidden or cross-matched liquidity is not modelled.",
    "One process, one laptop: windows lost to sleep or a network drop are lost for every arm at once.",
]


def read(name: str) -> list[dict]:
    p = DATA / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def boot_mean_ci(xs: list[float], seed: int = 7) -> tuple[float, float]:
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(BOOT))
    return means[int(0.025 * BOOT)], means[int(0.975 * BOOT)]


def max_drawdown(series: list[float]) -> float:
    peak = cum = worst = 0.0
    for x in series:
        cum += x
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def build() -> dict:
    outcomes = {r["window_ts"]: r["outcome"] for r in read("settlements.jsonl")}
    # Arms added later carry their own window set: the windows.jsonl rows written
    # before an arm existed do not list it (and rows from before the list existed
    # hold the original four).
    original = ("taker_v1", "maker_mid", "maker_model", "maker_anchored")
    ran: dict[str, set[int]] = {a: set() for a in ARMS}
    for r in read("windows.jsonl"):
        if r["window_ts"] in outcomes:
            for a in r.get("arms") or original:
                if a in ran:
                    ran[a].add(r["window_ts"])
    observed = sorted(set().union(*ran.values()))
    per_window: dict[str, dict[int, float]] = {a: defaultdict(float) for a in ARMS}
    stats: dict[str, dict] = {a: defaultdict(float) for a in ARMS}
    markouts: dict[str, dict[str, list[float]]] = {a: defaultdict(list) for a in ARMS}

    for f in read("fills.jsonl"):
        a, w = f["arm"], f["window_ts"]
        if a not in ARMS or w not in outcomes:
            continue
        s = 1 if f["side"] == "buy" else -1
        payout = 100.0 if outcomes[w] == "up" else 0.0
        pnl = s * f["size"] * (payout - f["price_c"]) / 100.0
        per_window[a][w] += pnl
        st = stats[a]
        st["fills"] += 1
        st["shares"] += f["size"]
        st["captured_c"] += s * (f["fv_at_place_c"] - f["price_c"]) * f["size"]
        st["notional"] += f["size"] * (f["price_c"] if s > 0 else 100 - f["price_c"]) / 100.0
        for h, m in (f.get("markouts") or {}).items():
            if m is not None:
                markouts[a][h].append(m)

    for t in read("taker_v1.jsonl"):
        w = t["window_ts"]
        if w not in outcomes:
            continue
        cost = t["entry"] + t["fee"]
        pnl = (1.0 if t["side"] == outcomes[w] else 0.0) - cost
        per_window["taker_v1"][w] += pnl
        st = stats["taker_v1"]
        st["fills"] += 1
        st["shares"] += 1
        st["fees"] += t["fee"]
        st["notional"] += cost

    arms = []
    for a, (role, desc) in ARMS.items():
        mine = sorted(ran[a])
        xs = [per_window[a].get(w, 0.0) for w in mine]
        st, n = stats[a], len(mine)
        total = sum(xs)
        lo, hi = boot_mean_ci(xs)
        mk = {h: round(sum(v) / len(v), 3) for h, v in sorted(markouts[a].items(), key=lambda kv: int(kv[0])) if v}
        arms.append({
            "name": a, "role": role, "description": desc,
            "units": n, "active_units": sum(1 for x in xs if x != 0.0),
            "fills": int(st["fills"]), "size": round(st["shares"], 1),
            "pnl": round(total, 2),
            "pnl_per_unit": round(total / n, 4) if n else None,
            "pnl_per_unit_ci": [round(lo, 4), round(hi, 4)] if n >= 2 else None,
            "pnl_per_size_c": round(100 * total / st["shares"], 3) if st["shares"] else None,
            "roi": round(total / st["notional"], 4) if st["notional"] else None,
            "max_drawdown": round(max_drawdown(xs), 2),
            "edge_at_fill_c": round(st["captured_c"] / st["shares"], 3) if st["shares"] and role == "treatment" else None,
            "markouts_c": mk or None,
            "fees": round(st["fees"], 2),
        })

    comparisons = []
    for a in ARMS:
        if a == "taker_v1":
            continue
        both = sorted(ran[a] & ran["taker_v1"])
        diff = [per_window[a].get(w, 0.0) - per_window["taker_v1"].get(w, 0.0) for w in both]
        lo, hi = boot_mean_ci(diff)
        n = len(diff)
        if n < MIN_WINDOWS:
            verdict = "collecting"
        elif lo > 0:
            verdict = "better"
        elif hi < 0:
            verdict = "worse"
        else:
            verdict = "indistinguishable"
        comparisons.append({"treatment": a, "control": "taker_v1", "metric": "pnl_per_window",
                            "diff": round(sum(diff) / n, 4) if n else None,
                            "ci": [round(lo, 4), round(hi, 4)] if n >= 2 else None, "verdict": verdict})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment": "Polymarket BTC 5-minute binaries: taker v1 vs passive makers",
        "venue": "Polymarket", "paper": True, "started": "2026-09-25",
        "unit": "window", "min_units_for_verdict": MIN_WINDOWS,
        "status": "verdict" if len(ran["taker_v1"]) >= MIN_WINDOWS else "collecting",
        "headline": headline(len(ran["taker_v1"]), arms, comparisons),
        "arms": arms, "comparisons": comparisons,
        "kill_criteria": KILL, "caveats": CAVEATS,
    }


def headline(n: int, arms: list[dict], comps: list[dict]) -> str:
    if n < MIN_WINDOWS:
        return (f"Collecting: {n} of {MIN_WINDOWS} settled windows before any arm is scored. "
                "Numbers below are running totals, not results.")
    best = max(comps, key=lambda c: c["diff"] if c["diff"] is not None else -1e9)
    lo, hi = best["ci"]
    word = {"better": "beats", "worse": "trails", "indistinguishable": "cannot yet be told apart from"}[best["verdict"]]
    return (f"After {n} settled windows the best maker ({best['treatment']}) {word} the original taker by "
            f"${best['diff']:+.3f} per window (95% CI ${lo:+.3f} to ${hi:+.3f}).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=None)
    a = ap.parse_args()
    doc = build()
    print(doc["headline"])
    print(f"{'arm':16} {'fills':>6} {'size':>7} {'pnl':>8} {'$/window':>9} {'95% CI':>19} {'c/share':>8} {'mk5s':>6}")
    for r in doc["arms"]:
        ci = r["pnl_per_unit_ci"] or [float("nan")] * 2
        mk = (r["markouts_c"] or {}).get("5")
        print(f"{r['name']:16} {r['fills']:>6} {r['size']:>7} {r['pnl']:>8.2f} {r['pnl_per_unit'] or 0:>9.4f} "
              f"[{ci[0]:+.4f},{ci[1]:+.4f}] {r['pnl_per_size_c'] or 0:>8.2f} {mk if mk is not None else '-':>6}")
    for c in doc["comparisons"]:
        print(f"  {c['treatment']} - taker_v1: {c['diff']} {c['ci']} -> {c['verdict']}")
    if a.output:
        out = Path(a.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
