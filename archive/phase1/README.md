# Paper A/B, phase 1 (2026-09-25 to 2026-10-29)

The original taker (`taker_v1`) against three passive makers and their latency ladders on
Polymarket's BTC 5-minute binaries, before the aggro close. In this phase a maker quoted its
reducing side only as far as its edge curve allowed, so a position could sit off the touch
and was held into resolution once quoting stopped in the last 60 seconds. Phase 2 (from
2026-10-30) adds the aggro close to every maker and starts from flat.

| file | what it is |
|---|---|
| `report.json` | the final scored report, as `ab_report.py` produced it on 2026-10-29 |
| `report.txt` | that run's console table |
| `data.tar.xz` | the raw record: `paper_data/windows.jsonl`, `fills.jsonl`, `settlements.jsonl`, `taker_v1.jsonl`, `status.json` |

Windows on 2026-10-02..24 are declared downtime (`~/personal/automation/downtime.json`) and
not scored; 2026-10-29 is scored, because these arms ran normally that day.

To re-score it, or score it a different way:

    mkdir -p /tmp/pm_phase1 && tar -xJf archive/phase1/data.tar.xz -C /tmp/pm_phase1
    PAPER_DATA=/tmp/pm_phase1/paper_data env -u PYTHONPATH /opt/local/bin/python3.13 ab_report.py \
        --output /tmp/pm_phase1/report.json

Phase 2's code carries phase-2 labels (`PHASE`, `started`, the aggro-close note in the arm
descriptions); the numbers it computes from these files are phase 1's.
