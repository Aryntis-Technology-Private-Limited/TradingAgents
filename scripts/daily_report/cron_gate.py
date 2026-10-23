"""Cron gate for the daily-report container: fire the report at a fixed time each day.

Runs as the container's entrypoint in place of the interactive CLI. Each loop
iteration sleeps until the next DAILY_REPORT_HOUR boundary (server-local time),
runs the report via ``python -m scripts.daily_report.runner``, and checks the
stop date: once today has passed DAILY_REPORT_UNTIL the gate exits 0 and the
container stops, so it does not keep firing past the requested end date.

A Python sleep loop is used instead of crontab inside the container: no extra
daemon, it restarts with the container, and the stop date is enforced in code
rather than as a cron line that could outlive the intended end date.

Environment (all optional, defaults shown):
    DAILY_REPORT_HOUR         hour in 0-23, server-local (default 2)
    DAILY_REPORT_UNTIL        YYYY-MM-DD inclusive stop date (default 2027-01-01)
    DAILY_REPORT_RUN_ON_STARTUP   "1"/"0": fire once immediately on start (default 1)

Only weekdays fire: the gate skips Sat/Sun dates at the fire boundary, so a
08:00 trigger that lands on a weekend waits for Monday.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _hour() -> int:
    raw = os.environ.get("DAILY_REPORT_HOUR", "2").strip()
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"DAILY_REPORT_HOUR must be an int 0-23, got {raw!r}")
    if not 0 <= value <= 23:
        raise SystemExit(f"DAILY_REPORT_HOUR must be 0-23, got {value}")
    return value


def _until() -> datetime:
    raw = os.environ.get("DAILY_REPORT_UNTIL", "2027-01-01").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"DAILY_REPORT_UNTIL must be YYYY-MM-DD, got {raw!r}")


def _fire() -> int:
    """Run the daily report once; return its exit code."""
    print(f"[cron-gate] {datetime.now():%Y-%m-%d %H:%M:%S} firing daily report", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "scripts.daily_report.runner"],
        cwd=ROOT,
    )


def _next_fire(now: datetime, hour: int) -> datetime:
    """The next weekday at ``hour:00`` strictly after ``now``."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate += timedelta(days=1)
    return candidate


def main() -> int:
    hour = _hour()
    until = _until()
    run_on_startup = os.environ.get("DAILY_REPORT_RUN_ON_STARTUP", "1") not in ("0", "false", "False")

    if run_on_startup:
        _fire()

    last_fired_day: datetime | None = None
    while True:
        now = datetime.now()
        if now.date() > until.date():
            print(f"[cron-gate] past DAILY_REPORT_UNTIL ({until.date()}); exiting", flush=True)
            return 0

        next_fire = _next_fire(now, hour)
        if next_fire.date() > until.date():
            print(f"[cron-gate] next fire {next_fire:%Y-%m-%d} is past "
                  f"DAILY_REPORT_UNTIL ({until.date()}); exiting", flush=True)
            return 0

        wait = (next_fire - now).total_seconds()
        print(f"[cron-gate] next fire at {next_fire:%Y-%m-%d %H:%M} "
              f"(in {wait/3600:.1f}h), stop after {until.date()}", flush=True)
        time.sleep(max(wait, 1.0))

        # The sleep can overshoot the boundary by seconds; only fire once per day.
        day = datetime.now().date()
        if day != last_fired_day:
            last_fired_day = day
            _fire()


if __name__ == "__main__":
    sys.exit(main())
