#!/bin/bash
set -u

# Daily BTC 5-minute market-monitor payload for the website.
# Mechanism and gotchas: ~/personal/automation/LAUNCHD.md
#
# This publishes an OBSERVATION, not picks. The repo's own measurement is that
# the model has no edge (see README "Does the edge exist?"), so the job re-runs
# that measurement daily and publishes the scorecard. If the verdict ever flips,
# this is where it shows up.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG="$LOG_DIR/run.log"
PY="${JOB_PYTHON:-/opt/local/bin/python3.13}"
OUT="$PROJECT_DIR/web/btc_monitor.json"

# Optional: also drop the payload into a checkout of the website repo so it is
# ready to commit. Unset by default -- a scheduled job should not write into
# another repo's tree, and must never git-push a public site on its own.
# Export POLYMARKET_WEB_DEST to enable. Matches funding-drift's convention.
WEB_DEST="${POLYMARKET_WEB_DEST:-}"

mkdir -p "$LOG_DIR" "$(dirname "$OUT")"

notify() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"polymarket-btc\"" 2>/dev/null
}

{
    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') run start ==="

    # Never inherit the work-repo PYTHONPATH: bare python3 on this Mac is the
    # work trading venv. See LAUNCHD.md "The interpreter".
    if env -u PYTHONPATH "$PY" "$PROJECT_DIR/generate_web_monitor.py" --output "$OUT"; then
        if [ -n "$WEB_DEST" ]; then
            if [ -d "$(dirname "$WEB_DEST")" ]; then
                cp "$OUT" "$WEB_DEST" && echo "mirrored to $WEB_DEST"
            else
                echo "WARN: POLYMARKET_WEB_DEST parent missing: $WEB_DEST"
                notify "web mirror path missing - see logs/run.log"
            fi
        fi
    else
        echo "FAILED (exit $?)"
        notify "scan FAILED - see logs/run.log"
    fi

    # Alarm on silence, not just on errors: a job that quietly stops running
    # produces no error at all, and looks identical to a healthy one. Check the
    # payload's own age after each run.
    env -u PYTHONPATH "$PY" "$PROJECT_DIR/generate_web_monitor.py" \
        --status --fail-if-stale --output "$OUT"
    if [ $? -ne 0 ]; then
        notify "payload is stale - the daily scan may have stopped running"
    fi

    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') run end ==="
} >> "$LOG" 2>&1

# Keep the log from growing forever.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2000000 ]; then
    tail -c 500000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
