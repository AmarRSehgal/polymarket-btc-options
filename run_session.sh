#!/bin/bash
set -u
# One paper A/B session a day: paper_ab.py for SESSION_S (default 2h) while the Mac
# is awake, then score and publish. launchd fires this hourly; it no-ops once
# today's session is done. Windows still inside Gamma's settlement lag at the end
# are settled by the next session. Mechanism: ~/personal/automation/LAUNCHD.md

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${JOB_PYTHON:-/opt/local/bin/python3.13}"
SESSION_S="${SESSION_S:-7200}"
MARK="$PROJECT_DIR/logs/session_$(date +%F).done"
LOG="$PROJECT_DIR/logs/session.log"
mkdir -p "$PROJECT_DIR/logs"
[ -f "$MARK" ] && exit 0

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"polymarket-btc\"" 2>/dev/null; }

{
    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') session start (${SESSION_S}s) ==="
    env -u PYTHONPATH "$PY" "$PROJECT_DIR/paper_ab.py" --log-file "$PROJECT_DIR/logs/paper_ab.log" &
    PID=$!
    trap 'kill -TERM $PID 2>/dev/null' TERM INT
    sleep "$SESSION_S"
    kill -TERM $PID 2>/dev/null
    for _ in $(seq 60); do kill -0 $PID 2>/dev/null || break; sleep 1; done
    kill -9 $PID 2>/dev/null
    wait 2>/dev/null
    touch "$MARK"
    find "$PROJECT_DIR/logs" -name 'session_*.done' -mtime +14 -delete

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
    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') session end ==="
} >> "$LOG" 2>&1
exit 0
