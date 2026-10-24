"""Cron gate for the daily-report container: fire the report sweep, then the
morning digest PDF, on each trading day.

Runs as the container's entrypoint in place of the interactive CLI. Two jobs
fire per trading day (Mon-Fri; the gate skips weekends, so a trigger that
lands on Sat/Sun waits for Monday — exchange holidays are not tracked, a
holiday run just re-prices the last session's data):

1. The report sweep (``scripts.daily_report.runner``) at DAILY_REPORT_HOUR
   (default 02:00, after the previous session closed at 15:30 IST), writing
   one folder per ticker under DAILY_REPORTS_DIR/<YYYY-MM-DD>/.
2. The morning digest (``scripts.daily_report.digest``) at
   DAILY_DIGEST_HOUR (default 08:00, before the 09:15 market open),
   reading that day's sweep output and rendering the action PDF — the
   quiet "have a restful day" page when nothing is actionable.

A Python sleep loop is used instead of crontab inside the container: no
extra daemon, it restarts with the container, and the stop date is enforced
in code rather than as a cron line that could outlive the intended end
date.

Configuration (per user directive 2026-10-24: "a folder move must be a
configuration change, not a rebuild+deploy"): there is no database in this
project, so the knobs live in the repo's .env file (mounted into the
container via ``env_file``), under DAILY_REPORT_* / DAILY_DIGEST_* keys.
Edit .env to change any hour, the stop date, whether startup fires, the
reports directory, the analysts, or the digest PDF path — the change takes
effect at the next fire (a fresh process is spawned each time), with NO
image rebuild. See scripts/daily_report/config.py for the full key list.
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from scripts.daily_report import config as daily_config

ROOT = Path(__file__).resolve().parents[2]


def _hour() -> int:
    try:
        return daily_config.hour()
    except ValueError as exc:
        raise SystemExit(str(exc))


def _digest_hour() -> int:
    try:
        return daily_config.digest_hour()
    except ValueError as exc:
        raise SystemExit(str(exc))


def _until() -> datetime:
    try:
        return datetime.strptime(daily_config.until(), "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(str(exc))


def _fire_report() -> int:
    """Run the report sweep once; return its exit code."""
    print(f"[cron-gate] {datetime.now():%Y-%m-%d %H:%M:%S} firing daily report", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "scripts.daily_report.runner"],
        cwd=ROOT,
    )


def _fire_digest() -> int:
    """Run the morning digest PDF once; return its exit code."""
    print(f"[cron-gate] {datetime.now():%Y-%m-%d %H:%M:%S} firing morning digest", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "scripts.daily_report.digest"],
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


def _day_events(now: datetime, until: datetime) -> list[tuple[datetime, str]]:
    """(when, job) for each trading-day job due after ``now`` and not past
    ``until``: that day's report sweep and digest, in time order."""
    events: list[tuple[datetime, str]] = []
    report_at = _next_fire(now, _hour())
    digest_at = _next_fire(now, _digest_hour())
    # If today's report hour already passed but the digest hour has not, the
    # digest still fires today (it reads whatever the sweep wrote).
    if report_at <= until:
        events.append((report_at, "report"))
    if digest_at <= until:
        events.append((digest_at, "digest"))
    return events


def main() -> int:
    until = _until()
    run_on_startup = daily_config.run_on_startup()

    if run_on_startup:
        _fire_report()
        _fire_digest()

    last_fired: dict[str, datetime] = {}
    while True:
        now = datetime.now()
        if now.date() > until.date():
            print(f"[cron-gate] past DAILY_REPORT_UNTIL ({until.date()}); exiting", flush=True)
            return 0

        events = _day_events(now, until)
        if not events:
            print(f"[cron-gate] next fire is past DAILY_REPORT_UNTIL ({until.date()}); exiting",
                  flush=True)
            return 0

        events.sort(key=lambda item: item[0])
        next_when, job = events[0]
        wait = (next_when - now).total_seconds()
        print(f"[cron-gate] next {job} at {next_when:%Y-%m-%d %H:%M} "
              f"(in {wait/3600:.1f}h), stop after {until.date()}", flush=True)
        time.sleep(max(wait, 1.0))

        # The sleep can overshoot the boundary by seconds; only fire each job
        # once per day.
        day = datetime.now().date()
        fired_today = last_fired.get(job)
        if fired_today is None or fired_today.date() != day:
            last_fired[job] = datetime.now()
            if job == "report":
                _fire_report()
            else:
                _fire_digest()


if __name__ == "__main__":
    sys.exit(main())
