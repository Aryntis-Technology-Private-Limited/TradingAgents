"""Runtime configuration for the daily-report runner.

Design rule: parameters that an operator wants to change WITHOUT a rebuild
or a container recreation live in the repo's ``.env`` file (the service
already mounts it via ``env_file``) under DAILY_REPORT_* keys, and are
read at process start. Every daily fire is a fresh process (the cron gate
spawns the runner as a subprocess), so an edit to .env takes effect at the
next fire — no image rebuild.

Precedence per key (highest first):
    1. The process environment (i.e. whatever docker compose put in from
       .env plus any DAILY_REPORT_* keys there) — a key set in .env wins
       over the built-in default below.
    2. The built-in default.

There is no database in this project (it is a CLI + Docker app, no ORM
layer), so .env is the persistence layer for these knobs — the same
mechanism the app already uses for LLM provider, results dir, etc.

Current parameters (set the matching key in .env; omit it to inherit the
default):

- DAILY_REPORTS_DIR      absolute path the per-day report folders are created
  under. Must be writable by the container's user (uid 1000 inside the
  container): today that means a path inside the mounted
  /home/appuser/.tradingagents tree (the named volume), e.g.
  /home/appuser/.tradingagents/reports. Default: <results_dir>/reports.
- DAILY_REPORT_HOUR      hour 0-23, server-local, the gate fires (default 2)
- DAILY_REPORT_UNTIL     YYYY-MM-DD, last day the gate fires (default 2027-01-01)
- DAILY_REPORT_RUN_ON_STARTUP   1/0, fire immediately on container start (default 1)
- DAILY_REPORT_ANALYSTS  comma list, e.g. "market,news" (default: market,news)

Digest module (scripts/daily_report/digest.py):
- DAILY_DIGEST_HOUR      hour 0-23, server-local, the 08:00 digest PDF fire
  (default 8). Runs AFTER the report sweep's hour so it reads a completed day.
- DAILY_DIGEST_PDF       output path for the morning digest PDF (default:
  <reports_dir>/digest/<YYYY-MM-DD>.pdf)
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULTS: dict[str, str] = {
    "hour": "2",
    "until": "2027-01-01",
    "run_on_startup": "1",
    "digest_hour": "8",
}


def get(key: str, default: str | None = None) -> str | None:
    """The value of DAILY_REPORT_<KEY> from the process environment, else the
    built-in default for known keys, else ``default``.

    Reading os.environ at call time (not at import) is deliberate: every
    fire is a fresh process, so the value reflects whatever is in .env at
    that moment.
    """
    value = os.environ.get(f"DAILY_REPORT_{key.upper()}")
    if value:
        return value
    return _DEFAULTS.get(key, default)


def reports_dir() -> Path:
    """The reports root: DAILY_REPORTS_DIR if set (absolute, or relative to
    results_dir), else <results_dir>/reports."""
    import tradingagents.default_config as config_module  # local: heavy module

    raw = os.environ.get("DAILY_REPORTS_DIR", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else Path(config_module.build_default_config()["results_dir"]) / path
    return Path(config_module.build_default_config()["results_dir"]) / "reports"


def reports_dir_str() -> str:
    """reports_dir() as a plain string, for display/logging."""
    return str(reports_dir())


def hour() -> int:
    raw = get("hour", "2")
    value = int(raw)
    if not 0 <= value <= 23:
        raise ValueError(f"DAILY_REPORT_HOUR must be 0-23, got {raw!r}")
    return value


def until() -> str:
    import datetime

    raw = get("until", "2027-01-01")
    datetime.datetime.strptime(str(raw), "%Y-%m-%d")  # validate, raises if bad
    return str(raw)


def run_on_startup() -> bool:
    raw = get("run_on_startup", "1")
    return str(raw).strip().lower() not in ("0", "false", "no")


def analysts() -> list[str]:
    """The analyst list: DAILY_REPORT_ANALYSTS (comma-separated) else the
    default of market + news."""
    raw = os.environ.get("DAILY_REPORT_ANALYSTS", "").strip()
    if raw:
        return [a.strip() for a in raw.split(",") if a.strip()]
    return ["market", "news"]


def digest_hour() -> int:
    """The hour (0-23, server-local) the digest PDF fires: DAILY_DIGEST_HOUR,
    default 8 — after the report sweep's hour, so it reads a completed day."""
    raw = get("digest_hour", "8")
    value = int(raw)
    if not 0 <= value <= 23:
        raise ValueError(f"DAILY_DIGEST_HOUR must be 0-23, got {raw!r}")
    return value


def retention_days() -> int:
    """How many calendar days of dated report folders to keep, including
    today: DAILY_REPORT_RETENTION_DAYS, default 30 — history before the
    retention window is pruned at every sweep (per user directive
    2026-10-24: store only recent history, older folders can be dropped).
    Must be >= 1."""
    raw = os.environ.get("DAILY_REPORT_RETENTION_DAYS", "").strip() or "30"
    value = int(raw)
    if value < 1:
        raise ValueError(f"DAILY_REPORT_RETENTION_DAYS must be >= 1, got {raw!r}")
    return value


def digest_pdf_path() -> str:
    """Output path for the digest PDF: DAILY_DIGEST_PDF, else
    <reports_dir>/digest/<today>.pdf."""
    import datetime

    raw = os.environ.get("DAILY_DIGEST_PDF", "").strip()
    if raw:
        return raw
    today = datetime.date.today().strftime("%Y-%m-%d")
    return str(reports_dir() / "digest" / f"{today}.pdf")


__all__ = [
    "get", "reports_dir", "hour", "until", "run_on_startup", "analysts",
    "retention_days", "digest_hour", "digest_pdf_path",
]
