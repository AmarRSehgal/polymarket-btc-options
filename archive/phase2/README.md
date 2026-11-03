# Paper A/B, phase 2 (2026-10-30 to 2026-11-02)

The original taker against three makers and their latency ladders on Polymarket's BTC
5-minute binaries, with an aggro close at all times: any maker position rested in full at the
touch on the closing side, kept through fair-value and late-window gates, never crossing.
Stopped after 277 windows: every maker tripped both kill criteria (PnL per window below zero
at 95% confidence after 200 windows; negative 5s markout after 500 fills). The taker lost at
roughly its phase-1 rate; makers lost 3-6x more per window, because fills per window tripled
(about 4 to 12) on entries that were already adversely selected, and the closes joined the
touch exactly when informed flow wanted the other side. Phase 3 (from 2026-11-03) quotes
normally and unwinds only at the end of the window.

| file | what it is |
|---|---|
| `report.json` | the final scored report, as `ab_report.py` produced it on 2026-11-02 |
| `report.txt` | that run's console table |
| `data.tar.xz` | the raw record: `paper_data/windows.jsonl`, `fills.jsonl`, `settlements.jsonl`, `taker_v1.jsonl`, `status.json` |

To re-score it:

    mkdir -p /tmp/pm_phase2 && tar -xJf archive/phase2/data.tar.xz -C /tmp/pm_phase2
    PAPER_DATA=/tmp/pm_phase2/paper_data env -u PYTHONPATH /opt/local/bin/python3.13 ab_report.py \
        --output /tmp/pm_phase2/report.json

Later code carries its own phase labels; the numbers it computes from these files are phase 2's.
