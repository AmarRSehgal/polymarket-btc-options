#!/usr/bin/env python3
"""Daily paper-session controller: SESSION_HOURS (default 6) of AWAKE running time.

launchd starts this every 10 minutes; a second copy never runs (launchd will not
start a job that is still running). It keeps the arm alive, counts only time the
machine was awake -- time.monotonic() stops while the lid is closed -- and when
the day's budget is spent it stops the arm and publishes. A lid closed mid-session
just pauses the count: on wake the arm handles the gap itself (paper_ab._on_wake)
and the controller carries on from where it was. A day that ends before its
budget is spent is published the next time this starts.

State: logs/session_state.json  {"date", "used_s", "published"}
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
STATE = LOGS / "session_state.json"
PY = os.environ.get("JOB_PYTHON", "/opt/local/bin/python3.13")
BUDGET_S = float(os.environ.get("SESSION_HOURS", "6")) * 3600
TICK_S = 10.0
ARMS = {"paper_ab": [PY, str(HERE / "paper_ab.py"), "--log-file", str(LOGS / "paper_ab.log")]}
PUBLISH = ["/bin/bash", str(HERE / "publish_ab.sh")]


def log(msg: str):
    with open(LOGS / "session.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"date": None, "used_s": 0.0, "published": True}


def save(st: dict):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


class Arms:
    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.started: dict[str, float] = {}
        self.env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    def ensure(self):
        for name, cmd in ARMS.items():
            p = self.procs.get(name)
            if p is not None and p.poll() is None:
                continue
            if p is not None:
                log(f"{name} exited {p.returncode}; restarting")
            if time.monotonic() - self.started.get(name, -1e9) < 30:
                continue          # crash-loop throttle
            self.procs[name] = subprocess.Popen(cmd, cwd=HERE, env=self.env,
                                                stdout=subprocess.DEVNULL, stderr=open(LOGS / f"{name}.err", "a"))
            self.started[name] = time.monotonic()
            log(f"started {name} pid {self.procs[name].pid}")

    def stop(self):
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 60
        for p in self.procs.values():
            try:
                p.wait(max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs.clear()


def publish(day: str):
    r = subprocess.run(PUBLISH, cwd=HERE, env={**os.environ, "AB_DATE": day}, capture_output=True, text=True)
    log(f"publish {day}: exit {r.returncode} {r.stdout.strip()[-200:]}")


def main() -> int:
    LOGS.mkdir(exist_ok=True)
    arms = Arms()
    stopping = False

    def on_term(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    st = load()
    last = time.monotonic()
    while not stopping:
        today = date.today().isoformat()
        if st["date"] != today:
            if st["date"] and not st["published"]:
                arms.stop()
                publish(st["date"])
            st = {"date": today, "used_s": 0.0, "published": False}
            save(st)
        if st["used_s"] >= BUDGET_S:
            arms.stop()
            if not st["published"]:
                publish(today)
                st["published"] = True
                save(st)
            log(f"budget spent for {today}; exiting")
            return 0
        arms.ensure()
        time.sleep(TICK_S)
        now = time.monotonic()
        st["used_s"] += min(now - last, 3 * TICK_S)   # monotonic: asleep time never counts
        last = now
        save(st)
    arms.stop()
    save(st)
    log("stopped by signal; session resumes on the next start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
