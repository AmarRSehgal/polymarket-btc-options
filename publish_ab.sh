#!/bin/bash
set -u
# Score the paper A/B and push it to the website. Run by session.py when a day's
# session ends. Mechanism: ~/personal/automation/LAUNCHD.md

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${JOB_PYTHON:-/opt/local/bin/python3.13}"
notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"polymarket-btc\"" 2>/dev/null; }

{
    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') publish ==="
    SITE="${WEBSITE_DIR:-$HOME/personal/website}"
    AB="$PROJECT_DIR/web/pm_btc_paper.json"
    if env -u PYTHONPATH "$PY" "$PROJECT_DIR/ab_report.py" --output "$AB" \
        && env -u PYTHONPATH "$PY" "$SITE/.github/scripts/validate_predictions.py" pm_btc_paper "$AB"; then
        cp "$AB" "$SITE/predictions/pm_btc_paper.json"
        git -C "$SITE" add predictions/pm_btc_paper.json
        if git -C "$SITE" diff --cached --quiet -- predictions/pm_btc_paper.json; then
            echo "paper A/B: no change"
        elif git -C "$SITE" commit -q -m "polymarket paper A/B: $(date -u +%Y-%m-%d)" -- predictions/pm_btc_paper.json \
            && git -C "$SITE" push -q origin HEAD; then
            echo "paper A/B: published"
        else
            echo "paper A/B: publish FAILED"; notify "paper A/B publish failed - see logs/run.log"
        fi
    else
        echo "paper A/B: report FAILED"; notify "paper A/B report failed - see logs/run.log"
    fi
} >> "$PROJECT_DIR/logs/session.log" 2>&1
exit 0
