"""Runtime configuration for the daily-report runner, kept in a JSON file.

Design rule: parameters that an operator wants to change WITHOUT a rebuild
or a container recreation live in ``~/.tradingagents/daily_report.json``
(inside the named volume the service mounts) and are read at process start.
Every daily fire is a fresh process (the cron gate spawns the runner as a
subprocess), so an edit to the JSON takes effect at the next fire — no
``docker compose up``, no image rebuild.

The JSON file is optional; absent keys fall back to the DAILY_REPORT_*
environment variables set in docker-compose.yml, which themselves fall back
to the defaults below. Precedence: JSON > env var > default.

Current parameters (all optional in the file — omit any to inherit the
next level down):

- ``reports_dir`` — absolute host path the per-day report folders are created
  under. Must be a directory the container can write to: today that means
  a path inside the mounted ``/home/appuser/.tradingagents`` tree (the
  named volume) OR a bind mount of a host dir that exists with correct
  ownership (uid 1000 inside the container). Point it at e.g.
  ``/home/appuser/.tradingagents/reports`` to keep reports next to the
  other logs.
- ``hour`` (int, 0-23) — which hour the cron gate fires at; overrides
  DAILY_REPORT_HOUR.
- ``until`` (``YYYY-MM-DD``) — the last date the gate will fire; overrides
  DAILY_REPORT_UNTIL.
- ``run_on_startup`` (bool) — whether a fresh process fires immediately
  before sleeping to the next scheduled hour; overrides
  DAILY_REPORT_RUN_ON_STARTUP.
- ``analysts`` (list of str) — which TradingAgents analysts the runner
  uses; overrides the hardcoded SELECTED_ANALYSTS in runner.py.

Example file (create it on the host at
``/var/lib/docker/volumes/daily_report_daily_reports/_data/.tradingagents/
daily_report.json`` — the same files the container sees under
``/home/appuser/.tradingagents/daily_report.json``)::

    {
      "reports_dir": "/home/appuser/.tradingagents/reports",
      "hour": 2,
      "until": "2027-01-01"
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# In-container path of the config file (the named volume is mounted here).
_CONFIG_PATH = Path(os.environ.get(
    "DAILY_REPORT_CONFIG",
    "/home/appuser/.tradingagents/daily_report.json",
))


def load() -> dict:
    """Read the JSON config, tolerating a missing/blank file (all defaults).

    A malformed file raises — a silently-broken operator config is worse
    than a loud startup error, and the cron gate will surface it in the
    container log instead of hiding behind default values.
    """
    if not _CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{_CONFIG_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{_CONFIG_PATH} must be a JSON object, got {type(data).__name__}")
    return data


def get(key: str, default=None):
    """A single runtime parameter: JSON value if present and non-null, else the
    DAILY_REPORT_* env var if set, else the given default."""
    value = load().get(key)
    if value is not None:
        return value
    env = os.environ.get(f"DAILY_REPORT_{key.upper()}")
    if env:
        return env
    return default


def reports_dir() -> Path:
    """The reports root: JSON ``reports_dir`` > ``DAILY_REPORTS_DIR`` env > the
    runner's built-in default (``<results_dir>/reports``)."""
    import tradingagents.default_config as config_module  # local: heavy module

    value = get("reports_dir")
    if value is not None:
        raw = os.environ.get("DAILY_REPORTS_DIR", "").strip()
        # JSON wins over the env var when both are present, but a relative
        # JSON path still resolves against results_dir.
        path = Path(value)
        return path if path.is_absolute() else Path(config_module.build_default_config()["results_dir"]) / path
    raw = os.environ.get("DAILY_REPORTS_DIR", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else Path(config_module.build_default_config()["results_dir"]) / path
    return Path(config_module.build_default_config()["results_dir"]) / "reports"


def hour() -> int:
    raw = get("hour", 2)
    if isinstance(raw, str):
        raw = int(raw)
    if not 0 <= int(raw) <= 23:
        raise ValueError(f"daily_report.json 'hour' must be 0-23, got {raw}")
    return int(raw)


def until() -> str:
    import datetime

    raw = get("until", "2027-01-01")
    datetime.datetime.strptime(str(raw), "%Y-%m-%d")  # validate, raises if bad
    return str(raw)


def run_on_startup() -> bool:
    raw = get("run_on_startup", "1")
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() not in ("0", "false", "no")


def analysts() -> list[str] | None:
    """The analyst list to run, or None to let the runner use its default."""
    raw = get("analysts")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [a.strip() for a in raw.split(",") if a.strip()]
    if isinstance(raw, list) and all(isinstance(a, str) for a in raw):
        return [a.strip().upper() if False else a.strip() for a in raw]
    raise ValueError(f"daily_report.json 'analysts' must be a list of strings or a comma-separated string, got {type(raw).__name__}")


__all__ = ["load", "get", "reports_dir", "hour", "until", "run_on_startup", "analysts"]
