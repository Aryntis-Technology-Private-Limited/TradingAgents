"""Cron gate for the daily-report container: fire the report sweep, then run
the digest immediately after it completes, on each trading day.

Runs as the container's entrypoint in place of the interactive CLI. One
scheduled fire per trading day (Mon-Fri minus exchange holidays); the two
jobs are chained, not independently scheduled:

1. The report sweep (``scripts.daily_report.runner``) at DAILY_REPORT_HOUR
   (default 02:00, after the previous session closed at 15:30 IST), writing
   one folder per ticker under DAILY_REPORTS_DIR/<YYYY-MM-DD>/.
2. The digest (``scripts.daily_report.digest``) runs AS SOON AS the sweep
   finishes — the summarizer is not a separately scheduled job; it reads the
   just-completed day's reports and renders the action PDF (the quiet
   "have a restful day" page when nothing is actionable).

This design means the digest always reflects a complete sweep: no 08:00
timer that could read a half-written folder, and no re-run of the sweep to
"catch up" a missed digest.

Trading days = Mon-Fri, EXCEPT dates in the exchange-holiday file
(``/home/appuser/.tradingagents/exchange_holidays.json``, see
scripts/daily_report/exchange_holidays.py). A fire that lands on a weekend
or a declared exchange holiday waits for the next eligible day.

The gate process runs as the unprivileged container user (appuser, uid 1000 —
a developer user, never root), per the non-root directive: the Dockerfile's
``USER appuser`` sets it, and no step in the gate drops to root.

A Python sleep loop is used instead of crontab inside the container: no extra
daemon, it restarts with the container, and the stop date is enforced in code
rather than as a cron line that could outlive the intended end date.

Configuration (per user directive 2026-10-24: "a folder move must be a
configuration change, not a rebuild+deploy"): there is no database in this
project, so the knobs live in the repo's .env file (mounted into the
container via ``env_file``), under DAILY_REPORT_* keys. Edit .env to change
the fire hour, the stop date, whether startup fires, the reports directory,
the analysts, or the digest PDF path — the change takes effect at the next
fire (a fresh process is spawned each time), with NO image rebuild. Exchange
holidays are maintained in the JSON file above, also without a rebuild. See
scripts/daily_report/config.py for the full key list.
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from scripts.daily_report import config as daily_config
from scripts.daily_report import exchange_holidays

ROOT = Path(__file__).resolve().parents[2]


def _trading_day(iso_date: str) -> bool:
    """Weekday and not a declared NSE/BSE exchange holiday."""
    import datetime as _dt

    day = _dt.datetime.strptime(iso_date, "%Y-%m-%d").date()
    if day.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return not exchange_holidays.is_exchange_holiday(iso_date)


def _hour() -> int:
    try:
        return daily_config.hour()
    except ValueError as exc:
        raise SystemExit(str(exc))


def _until() -> datetime:
    try:
        return datetime.strptime(daily_config.until(), "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(str(exc))


def _fire_sweep() -> int:
    """Run the report sweep once; return its exit code."""
    print(f"[cron-gate] {datetime.now():%Y-%m-%d %H:%M:%S} firing daily report sweep", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "scripts.daily_report.runner"],
        cwd=ROOT,
    )


def _fire_digest() -> int:
    """Run the digest PDF once; return its exit code. Called immediately
    after the sweep so it reads a just-completed day's reports."""
    print(f"[cron-gate] {datetime.now():%Y-%m-%d %H:%M:%S} firing digest "
          f"(sweep just completed)", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "scripts.daily_report.digest"],
        cwd=ROOT,
    )


def _fire() -> int:
    """One trading day's work: the sweep, then the digest as soon as the
    sweep returns (the digest is chained, not independently scheduled).

    A non-zero sweep exit (some tickers failed) does not skip the digest —
    the digest summarizes whatever completed, and logs the failure count.
    """
    sweep_rc = _fire_sweep()
    digest_rc = _fire_digest()
    return 0 if (sweep_rc == 0 and digest_rc == 0) else 1


def _next_fire(now: datetime, hour: int) -> datetime:
    """The next trading day (weekend- and holiday-skipped) at ``hour:00``
    strictly after ``now``."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    day = candidate.date()
    while not _trading_day(day.isoformat()):
        label = exchange_holidays.holiday_label(day.isoformat())
        reason = f"declared holiday ({label})" if label else "weekend"
        print(f"[cron-gate] {day.isoformat()} is not a trading day ({reason}); skipping",
              flush=True)
        day += timedelta(days=1)
        candidate = datetime(day.year, day.month, day.day, hour, 0, 0)
    return candidate


def main() -> int:
    hour = _hour()
    until = _until()
    run_on_startup = daily_config.run_on_startup()

    if run_on_startup:
        _fire()

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
        print(f"[cron-gate] next fire (sweep+digest) at {next_fire:%Y-%m-%d %H:%M} "
              f"(in {wait/3600:.1f}h), stop after {until.date()}", flush=True)
        time.sleep(max(wait, 1.0))

        # The sleep can overshoot the boundary by seconds; only fire if the
        # day is still a valid trading day (a holiday declared while the
        # gate was sleeping is re-checked here) and has not already fired.
        day = datetime.now().date()
        iso = day.isoformat()
        if not _trading_day(iso):
            label = exchange_holidays.holiday_label(iso)
            reason = f"declared holiday ({label})" if label else "weekend"
            print(f"[cron-gate] {iso} is not a trading day ({reason}); "
                  f"waiting for the next one", flush=True)
            continue
        _fire()


if __name__ == "__main__":
    sys.exit(main())
