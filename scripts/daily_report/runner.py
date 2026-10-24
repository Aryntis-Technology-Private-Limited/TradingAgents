"""Headless daily-report runner: Nifty 50 + SENSEX for today.

Designed to be invoked from Docker or a cron with no TTY:

    python -m scripts.daily_report.runner

Behavior:
- Uses the pinned Nifty 50 (NSE) + SENSEX (BSE) constituent lists from
  ``scripts.daily_report.nifty_sensex``.
- Runs the full TradingAgents pipeline for each ticker on today's date;
  a ticker whose report was already written on a previous crashed run is
  skipped (idempotent re-runs pick up where the last one stopped).
- Writes per-ticker complete_report.md + complete_report.html under
  the reports root, default ``<results_dir>/reports/<YYYY-MM-DD>/<TICKER>/``
  (override with ``DAILY_REPORTS_DIR``; a relative value resolves against
  ``results_dir``).
- Retention: dated folders older than the retention window (DAILY_REPORT_
  RETENTION_DAYS, default 30 days) are deleted at the start of each run.
- Parallelism: up to DAILY_REPORT_SWEEP_WORKERS (default 5) tickers are
  analyzed at a time - one LLM slot per worker, matching the 5-slot API
  quota. Set it to 1 for the legacy serial sweep.
- One ticker failure never stops the sweep (same contract as the backtest
  sweep in tradingagents/backtest.py).

Exit codes:
    0 — every ticker produced a report
    1 — one or more tickers failed; the failure list is printed at the end
"""

from __future__ import annotations

import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tradingagents import default_config as config_module
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import safe_ticker_component

from scripts.daily_report import config as daily_config
from scripts.daily_report.nifty_sensex import both

# Tickers whose NSE/BSE spelling carries a character ``safe_ticker_component``
# refuses (``&`` in M&M.NS, ``-`` is fine) still need a directory to save
# into; the guard is for user-supplied values, not our own pinned lists. A
# filesystem-safe rendering keeps the ticker readable but never enables
# path traversal (the same character set, minus the rejected one).
_PATH_SAFE_RE = re.compile(r"[^A-Za-z0-9._\-+^=]")


def _safe_name(ticker: str) -> str:
    """A directory name for ``ticker``: safe against traversal, readable."""
    return _PATH_SAFE_RE.sub("_", ticker.upper()).replace(" ", "")

def _selected_analysts() -> list[str]:
    """Which analysts the daily sweep runs: DAILY_REPORT_ANALYSTS in .env,
    else market + news. The NSE/BSE news leg currently degrades to what
    yfinance/GDELT/AV can serve; keeping fundamentals off the sweep halves
    the LLM cost (each cell runs the trader + two debate rounds on top)."""
    return daily_config.analysts()


def _sweep_workers() -> int:
    """Parallel LLM slots for the sweep: DAILY_REPORT_SWEEP_WORKERS in .env,
    default 5 - matching the operator's 5-slot API quota, so 5 tickers are
    analyzed at a time. Each slot owns its own graph instance (a
    LangGraph graph is stateful across one propagate() call and must not
    be shared across threads). Setting 1 restores the legacy serial
    sweep."""
    import os

    raw = os.environ.get("DAILY_REPORT_SWEEP_WORKERS", "").strip()
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return 5


def _today_dir(out_root: Path) -> tuple[str, Path]:
    today = get_current_date()
    return today, out_root / today


def _reports_root(results_dir: Path) -> Path:
    """Where dated report folders live: DAILY_REPORTS_DIR in .env, else
    <results_dir>/reports."""
    return daily_config.reports_dir()


def _prune_old_daily(daily_root: Path, today: str) -> list[str]:
    """Delete dated folders under ``daily_root`` older than the retention
    window (DAILY_REPORT_RETENTION_DAYS, default 30 days, counted back from
    ``today``). Only folders whose name is a valid ``YYYY-MM-DD`` are
    touched, so a stray non-date entry is never deleted. Returns the pruned
    folder names.
    """
    import datetime

    keep_from = (
        datetime.date.fromisoformat(today)
        - datetime.timedelta(days=daily_config.retention_days() - 1)
    ).isoformat()
    pruned: list[str] = []
    if not daily_root.is_dir():
        return pruned
    for child in daily_root.iterdir():
        if child.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", child.name) and child.name < keep_from:
            shutil.rmtree(child)
            pruned.append(child.name)
    return pruned


def run() -> int:
    config = config_module.build_default_config()
    results_dir = Path(config["results_dir"])
    reports_root = _reports_root(results_dir)
    today, out_dir = _today_dir(reports_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    pruned = _prune_old_daily(out_dir.parent, today)
    if pruned:
        print(f"[daily-report] pruned old daily folders (no archive): {', '.join(pruned)}", flush=True)

    lists = both()
    tickers = lists["nifty50"] + lists["sensex"]
    total = len(tickers)
    print(f"[daily-report] {today}: {len(lists['nifty50'])} Nifty 50 + "
          f"{len(lists['sensex'])} SENSEX = {total} tickers -> {out_dir}",
          flush=True)

    graph = TradingAgentsGraph(_selected_analysts(), config=config)
    workers = _sweep_workers()
    # One graph per worker slot: a LangGraph graph is stateful across one
    # propagate() call and must not be shared across threads. With 5 slots
    # (the API quota), 5 tickers run at a time.
    graphs = [TradingAgentsGraph(_selected_analysts(), config=config)
              for _ in range(workers)]

    def _process_one(slot: int, index: int, ticker: str) -> tuple[str, str, str | None]:
        """Analyze one ticker on its slot's graph. Returns
        (ticker, 'done'|'skipped'|'failed', reason)."""
        marker = out_dir / _safe_name(ticker) / "complete_report.md"
        if marker.is_file():
            return ticker, "skipped", None
        print(f"  [{index}/{total}] {ticker} {today} running...", flush=True)
        try:
            g = graphs[slot % len(graphs)]
            final_state, _signal = g.propagate(ticker, today, asset_type="stock")
            target = out_dir / _safe_name(ticker)
            g.save_reports(final_state, ticker, target, html=True)
            print(f"  [{index}/{total}] {ticker} done", flush=True)
            return ticker, "done", None
        except Exception as exc:  # one unreachable vendor must not end the sweep
            print(f"  [{index}/{total}] {ticker} FAILED: {exc}", flush=True)
            return ticker, "failed", str(exc)

    failures: list[tuple[str, str]] = []
    skipped = 0
    done_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_one, i, i, ticker): i
            for i, ticker in enumerate(tickers, 1)
        }
        for fut in as_completed(futures):
            ticker, status, reason = fut.result()
            if status == "skipped":
                skipped += 1
                print(f"  {ticker} {today} (already done, skipped)", flush=True)
            elif status == "done":
                done_count += 1
            else:
                failures.append((ticker, reason))

    # Settle any pending decisions against their benchmarks so their lessons
    # feed the next run; a settlement failure is not a sweep failure.
    print("\n=== daily-report summary ===")
    print(f"date: {today}")
    print(f"tickers: {total} · done {done_count} · "
          f"skipped (already run) {skipped} · failed {len(failures)}")
    print(f"sweep parallelism: {workers} slot(s)")
    print(f"reports: {out_dir}")
    for ticker, reason in failures:
        print(f"failed: {ticker}: {reason}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(run())
