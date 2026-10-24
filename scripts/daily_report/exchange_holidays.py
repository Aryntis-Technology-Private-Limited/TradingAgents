"""NSE + BSE exchange-holiday gate for the daily cron.

The trading-day calendar is maintained in two layers, no rebuild needed:

1. ``/home/appuser/.tradingagents/exchange_holidays.json`` (on the named
   volume) — the operator's list of exact holiday dates. The primary source
   of truth; edit it to add a date, the next gate process start picks it up.

2. ``holidays`` library (pinned in pyproject.toml) — a fallback for Indian
   holidays the operator did not list. ``holidays.in`` (``IN``) covers NSE's
   declared public holidays; BSE's are the same set plus a handful of
   BSE-only observances, so the operator's JSON file is where BSE-specific
   extras belong.

A date is a non-trading day if it is a weekend OR in either source. The
gate logs which source matched so a skip is explainable ("Diwali (operator
file)" vs "Diwali (holidays library)").

An empty/absent operator file and an unknown holiday name are both tolerated
— the union simply gets smaller, it never crashes the gate.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# In-container path (the named volume is mounted here); override for tests.
_DEFAULT_HOLIDAYS_PATH = os.environ.get(
    "DAILY_EXCHANGE_HOLIDAYS",
    "/home/appuser/.tradingagents/exchange_holidays.json",
)


def _operator_dates() -> dict[str, str]:
    """{iso_date: label} from the operator's JSON file on the volume."""
    path = Path(_DEFAULT_HOLIDAYS_PATH)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; treating it as empty", path)
        return {}
    entries = data.get("holidays", []) if isinstance(data, dict) else []
    dates: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("date"), str):
            label = entry.get("name") or entry.get("exchange") or "operator holiday"
            dates[entry["date"]] = f"{label} (operator file)"
        elif isinstance(entry, str):
            dates[entry] = "operator holiday"
    return dates


def _library_dates(year: int) -> dict[str, str]:
    """{iso_date: name} for Indian holidays from the holidays library."""
    try:
        import holidays
    except ImportError:
        logger.warning("holidays library not installed; operator file is the only source")
        return {}
    in_holidays = holidays.India(years=year)
    return {
        d.strftime("%Y-%m-%d"): f"{name} (holidays library)"
        for d, name in in_holidays.items()
    }


def _all_holidays(reference_date: str) -> dict[str, str]:
    """The union of both sources for the year of ``reference_date``."""
    import datetime

    year = int(reference_date[:4])
    merged = _library_dates(year)
    # Operator entries win on label overlap (they are the explicit ones).
    merged.update(_operator_dates())
    return merged


def is_trading_day(iso_date: str) -> bool:
    """True when ``iso_date`` (YYYY-MM-DD) is a weekday and not a declared
    NSE/BSE holiday (either source)."""
    import datetime

    day = datetime.datetime.strptime(iso_date, "%Y-%m-%d").date()
    if day.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return iso_date not in _all_holidays(iso_date)


def holiday_label(iso_date: str) -> str | None:
    """Why ``iso_date`` is a non-trading day, or None when it is one."""
    import datetime

    day = datetime.datetime.strptime(iso_date, "%Y-%m-%d").date()
    if day.weekday() >= 5:
        return "weekend"
    return _all_holidays(iso_date).get(iso_date)


def next_trading_day(iso_date: str) -> str:
    """First trading day on or after ``iso_date``."""
    import datetime

    day = datetime.datetime.strptime(iso_date, "%Y-%m-%d").date()
    while not is_trading_day(day.isoformat()):
        day += datetime.timedelta(days=1)
    return day.isoformat()


__all__ = ["is_trading_day", "holiday_label", "next_trading_day"]
