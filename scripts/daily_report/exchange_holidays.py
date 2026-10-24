"""NSE + BSE exchange-holiday gate for the daily cron.

The trading-day calendar is the operator's JSON file on the named volume,
``/home/appuser/.tradingagents/exchange_holidays.json`` — the single source
of truth (the 2026-27 NSE/BSE holiday list is maintained there, not in
source, so a new holiday or a special session is a one-line file edit, no
rebuild). The file shape:

    {
      "holidays": [
        {"date": "2026-10-20", "name": "Dussehra", "exchange": "NSE+BSE"},
        {"date": "2026-11-08",
         "name": "Diwali (Laxmi Pujan) - Special Muhurat Trading Session, exchanges OPEN",
         "open": true}
      ]
    }

Weekends are always non-trading days; a date in the file is a non-trading
day UNLESS it carries ``"open": true`` — that flag is for the Diwali
muhurat session, a declared holiday on which the exchanges still run a
special trading session, so it must not be skipped. Entries without the
flag (or bare date strings) are closed holidays and are skipped.

An absent or malformed file degrades to "weekends only" — it never blocks
the gate, and the gate logs every skip it makes so a missed holiday is
visible rather than silent.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# In-container path (the named volume is mounted here); override for tests
# via the DAILY_EXCHANGE_HOLIDAYS environment variable.
_DEFAULT_HOLIDAYS_PATH = os.environ.get(
    "DAILY_EXCHANGE_HOLIDAYS",
    "/home/appuser/.tradingagents/exchange_holidays.json",
)


def _entries() -> dict[str, tuple[str, bool]]:
    """{iso_date: (label, is_open_day)} from the operator's JSON file.

    ``is_open_day`` is True only when the entry carries ``"open": true``
    (a declared open muhurat session). An absent/malformed file yields an
    empty dict — the gate then skips only weekends.
    """
    path = Path(_DEFAULT_HOLIDAYS_PATH)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("%s is not valid JSON (%s); treating it as empty", path, exc)
        return {}
    entries = data.get("holidays", []) if isinstance(data, dict) else []
    result: dict[str, tuple[str, bool]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("date"), str):
            label = entry.get("name") or entry.get("exchange") or "exchange day"
            open_day = bool(entry.get("open"))
            result[entry["date"]] = (str(label), open_day)
        elif isinstance(entry, str):
            result[entry] = ("exchange holiday", False)
    return result


def is_exchange_holiday(iso_date: str) -> bool:
    """True when ``iso_date`` (YYYY-MM-DD) is a declared CLOSED exchange
    holiday. Entries flagged ``"open": true`` (e.g. the Diwali muhurat
    session) are not holidays — the exchanges trade that day, so it must
    not be skipped."""
    entry = _entries().get(iso_date)
    if entry is None:
        return False
    return not entry[1]


def holiday_label(iso_date: str) -> str | None:
    """A human-readable reason string for gate logging: the closed
    holiday's name, an open-session note, or None when the date is an
    ordinary weekday with no declared entry."""
    entry = _entries().get(iso_date)
    if entry is None:
        return None
    label, open_day = entry
    if open_day:
        return f"declared open day ({label}) — the exchanges trade, not skipped"
    return label


def is_trading_day(iso_date: str) -> bool:
    """True when ``iso_date`` is a weekday and not a declared closed
    exchange holiday (open muhurat days count as trading days)."""
    import datetime

    day = datetime.datetime.strptime(iso_date, "%Y-%m-%d").date()
    if day.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return not is_exchange_holiday(iso_date)


__all__ = ["is_exchange_holiday", "holiday_label", "is_trading_day"]
