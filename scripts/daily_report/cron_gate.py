"""Cron gate for the daily-report container: fire the report sweep, then the
morning digest PDF, on each trading day (Mon-Fri minus exchange holidays).

Runs as the container's entrypoint in place of the interactive CLI. Two jobs
fire per trading day:

1. The report sweep (``scripts.daily_report.runner``) at DAILY_REPORT_HOUR
   (default 02:00, after the previous session closed at 15:30 IST), writing
   one folder per ticker under DAILY_REPORTS_DIR/<YYYY-MM-DD>/.
2. The morning digest (``scripts.daily_report.digest``) at
   DAILY_DIGEST_HOUR (default 08:00, before the 09:15 market open),
   reading that day's sweep output and rendering the action PDF — the
   quiet "have a restful day" page when nothing is actionable.

Trading days = Mon-Fri, EXCEPT dates in the exchange-holiday file
(``/home/appuser/.tradingagents/exchange_holidays.json``, see
scripts/daily_report/exchange_holidays.py). A fire that lands on a weekend
or a declared exchange holiday waits for the next eligible day.

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
image rebuild. Exchange holidays are maintained in the JSON file above,
also without a rebuild. See scripts/daily_report/config.py for the full
key list.
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


def _next_trading_day(start: datetime) -> datetime:
    """First trading day (weekday, not a declared NSE/BSE holiday) at or
    after ``start``'s date, at ``start``'s hour."""
    day = start.date()
    while not _trading_day(day.isoformat()):
        label = exchange_holidays.holiday_label(day.isoformat())
        reason = f"declared holiday ({label})" if label else "weekend"
        print(f"[cron-gate] {day.isoformat()} is not a trading day ({reason}); skipping",
              flush=True)
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, start.hour, 0, 0)


def _next_fire(now: datetime, hour: int) -> datetime:
    """The next trading day (weekend- and holiday-skipped) at ``hour:00``
    strictly after ``now``."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return _next_trading_day(candidate)


def _day_events(now: datetime, until: datetime) -> list[tuple[datetime, str]]:
    """(when, job) for each trading-day job due after ``now`` and not past
    ``until``: that day's report sweep and digest, in time order."""
    events: list[tuple[datetime, str]] = []
    report_at = _next_fire(now, _hour())
    digest_at = _next_fire(now, _digest_hour())
    # If today's report hour already passed but the digest hour has not, the
    # digest still fires (it reads whatever the sweep wrote).
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

        # Wait for the earliest job that has not yet fired on its day.
        events.sort(key=lambda item: item[0])
        next_when, job = events[0]
        wait = (next_when - now).total_seconds()
        print(f"[cron-gate] next {job} at {next_when:%Y-%m-%d %H:%M} "
              f"(in {wait/3600:.1f}h), stop after {until.date()}", flush=True)
        time.sleep(max(wait, 1.0))

        # The sleep can overshoot the boundary by seconds. Fire ``job`` only
        # if that day is still a valid trading day (a holiday declared while
        # the gate was sleeping is re-checked here) and the job has not
        # already fired on it.
        day = datetime.now().date()
        iso = day.isoformat()
        if not _trading_day(iso):
            label = exchange_holidays.holiday_label(iso)
            reason = f"declared holiday ({label})" if label else "weekend"
            print(f"[cron-gate] {iso} is not a trading day ({reason}); "
                  f"waiting for the next one", flush=True)
            continue
        if job == "report":
            _fire_report()
        else:
            _fire_digest()


if __name__ == "__main__":
    sys.exit(main())
