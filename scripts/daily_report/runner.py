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
  ``<results_dir>/daily/<YYYY-MM-DD>/<TICKER>/``.
- Retention: date folders older than today are deleted at the start of the
  run (no archive is kept), so the volume holds at most the current day's
  reports.
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
from pathlib import Path

from tradingagents import default_config as config_module
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import safe_ticker_component

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

# Which analysts the daily sweep runs. The NSE/BSE news leg currently degrades
# to what yfinance/GDELT/AV can serve; keeping fundamentals off the sweep
# halves the LLM cost (each cell runs the trader + two debate rounds on top).
SELECTED_ANALYSTS = ["market", "news"]


def _today_dir(out_root: Path) -> tuple[str, Path]:
    today = get_current_date()
    return today, out_root / today


def _prune_old_daily(daily_root: Path, today: str) -> list[str]:
    """Delete every dated folder under ``daily_root`` that is not today's.

    No archive is kept: the volume holds at most the current day's reports.
    Only folders whose name is a valid ``YYYY-MM-DD`` are touched, so a
    stray non-date entry is never deleted. Returns the pruned folder names.
    """
    pruned: list[str] = []
    if not daily_root.is_dir():
        return pruned
    for child in daily_root.iterdir():
        if child.is_dir() and child.name != today and re.fullmatch(r"\d{4}-\d{2}-\d{2}", child.name):
            shutil.rmtree(child)
            pruned.append(child.name)
    return pruned


def run() -> int:
    config = config_module.build_default_config()
    results_dir = Path(config["results_dir"])
    today, out_dir = _today_dir(results_dir / "daily")
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

    graph = TradingAgentsGraph(SELECTED_ANALYSTS, config=config)
    failures: list[tuple[str, str]] = []
    skipped = 0

    for index, ticker in enumerate(tickers, 1):
        # A report written on an earlier run of today is final; do not re-pay
        # for it after a crash mid-sweep.
        marker = out_dir / _safe_name(ticker) / "complete_report.md"
        if marker.is_file():
            skipped += 1
            print(f"  [{index}/{total}] {ticker} {today} (already done, skipped)", flush=True)
            continue
        print(f"  [{index}/{total}] {ticker} {today} running...", flush=True)
        try:
            final_state, _signal = graph.propagate(
                ticker, today, asset_type="stock",
            )
            target = out_dir / _safe_name(ticker)
            graph.save_reports(final_state, ticker, target, html=True)
            print(f"  [{index}/{total}] {ticker} done", flush=True)
        except Exception as exc:  # one unreachable vendor must not end the sweep
            failures.append((ticker, str(exc)))
            print(f"  [{index}/{total}] {ticker} FAILED: {exc}", flush=True)

    # Settle any pending decisions against their benchmarks so their lessons
    # feed the next run; a settlement failure is not a sweep failure.
    print("\n=== daily-report summary ===")
    print(f"date: {today}")
    print(f"tickers: {total} · done {total - skipped - len(failures)} · "
          f"skipped (already run) {skipped} · failed {len(failures)}")
    print(f"reports: {out_dir}")
    for ticker, reason in failures:
        print(f"failed: {ticker}: {reason}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(run())
