"""Mock daily run: 5 random NSE tickers through the full pipeline + digest.

Times every layer so the operator can see where the wall-clock goes and
tune the sweep budget:
  1. bootstrap   - build config + TradingAgentsGraph
  2. sweep       - N tickers, per-ticker + avg
  3. digest      - read reports, select interesting, render PDF/HTML

Writes to a mock reports dir under DAILY_REPORTS_DIR/mock/<today>/ and the
digest to DAILY_REPORTS_DIR/mock/<today>_digest.pdf. The sweep is parallel
(DAILY_REPORT_SWEEP_WORKERS, default 5) - one LLM slot per worker, matching
the operator's 5-slot quota - so a 5-ticker mock run and a 78-ticker daily
sweep share the same throughput profile.

Run inside the container:

  python -m scripts.daily_report.mock_run

Exit codes:
    0 - every ticker produced a report
    1 - one or more tickers failed; the failure list is printed
"""

import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tradingagents import default_config as config_module
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.date_window import get_current_date

from scripts.daily_report import config as daily_config
from scripts.daily_report import digest
from scripts.daily_report import runner as daily_runner

N_TICKERS = 5
# NSE large-caps, safe_ticker_component-friendly
POOL = ["TCS.NS", "HDFCBANK.NS", "INFY.NS", "RELIANCE.NS", "SBIN.NS",
        "ADANIENT.NS", "WIPRO.NS", "LT.NS", "ITC.NS", "MARUTI.NS"]


def _sweep_workers() -> int:
    """Parallel LLM slots for the sweep (one report at a time per slot),
    mirroring the operator's 5-slot quota. Falls back to 5."""
    raw = os.environ.get("DAILY_REPORT_SWEEP_WORKERS", "").strip()
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return 5


def main() -> int:
    t0 = time.time()
    config = config_module.build_default_config()
    today = get_current_date()
    reports_root = daily_config.reports_dir()
    mock_root = reports_root / "mock"
    out_dir = mock_root / today
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = random.sample(POOL, N_TICKERS)
    workers = _sweep_workers()
    print(f"[mock] {today} · {len(tickers)} random tickers: {tickers}", flush=True)
    print(f"[mock] reports -> {out_dir} · sweep parallelism: {workers}", flush=True)

    t1 = time.time()
    graph = TradingAgentsGraph(daily_config.analysts(), config=config)
    t_bootstrap = t1 - t0
    print(f"[time] bootstrap (config+graph): {t_bootstrap:.1f}s", flush=True)

    # One graph per worker thread - the LangGraph graph is stateful across a
    # single propagate() call, so sharing it across threads races.
    graphs = [TradingAgentsGraph(daily_config.analysts(), config=config)
              for _ in range(workers)]

    def _one(slot: int, ticker: str) -> tuple[str, float, str | None]:
        ts = time.time()
        try:
            final_state, _ = graphs[slot].propagate(ticker, today, asset_type="stock")
            target = out_dir / daily_runner._safe_name(ticker)
            graphs[slot].save_reports(final_state, ticker, target, html=True)
            return ticker, time.time() - ts, None
        except Exception as exc:
            return ticker, time.time() - ts, str(exc)

    t_sweep_start = time.time()
    per_ticker: list[tuple[str, float]] = []
    failures: list[tuple[str, str]] = []
    slot_next = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, slot_next % workers, tk) for tk in tickers]
        slot_next += 1
        for fut in as_completed(futures):
            ticker, dt, err = fut.result()
            if err:
                failures.append((ticker, err))
                print(f"  {ticker} FAILED ({dt:.1f}s): {err[:100]}", flush=True)
            else:
                per_ticker.append((ticker, dt))
                print(f"  {ticker} done ({dt:.1f}s)", flush=True)
    t_sweep = time.time() - t_sweep_start
    done = [dt for _tk, dt in per_ticker]
    print(f"[time] sweep total (parallel {workers}): {t_sweep:.1f}s · "
          f"{len(per_ticker)} ok, {len(failures)} failed", flush=True)
    if done:
        print(f"[time] per-ticker min/avg/max (single-LLM-slot time): "
              f"{min(done):.1f}s / {sum(done) / len(done):.1f}s / {max(done):.1f}s", flush=True)
    for tk, dt in sorted(per_ticker, key=lambda x: x[0]):
        print(f"       {tk}: {dt:.1f}s", flush=True)

    # Digest: reads directly from out_dir, PDF written to mock_root.
    os.environ["DAILY_DIGEST_PDF"] = str(mock_root / f"{today}_digest.pdf")

    t_dig_start = time.time()
    decisions = digest.collect_decisions(out_dir)
    scanned = len(decisions)
    interesting = [d for d in decisions if d.is_interesting()]
    interesting.sort(key=lambda d: d.ticker)
    out_pdf = digest.render_pdf(interesting, today, scanned)
    t_digest = time.time() - t_dig_start
    print(f"[time] digest (extract+select+render): {t_digest:.1f}s", flush=True)
    print(f"[digest] {scanned} scanned, {len(interesting)} interesting", flush=True)
    print(f"[digest] output: {out_pdf}", flush=True)

    total = time.time() - t0
    print("\n=== MOCK DAILY RUN SUMMARY ===")
    print(json.dumps({
        "date": today,
        "tickers": tickers,
        "sweep_workers": workers,
        "seconds": {
            "bootstrap": round(t_bootstrap, 1),
            "sweep_total_wall": round(t_sweep, 1),
            "sweep_avg_per_ticker_llm_time": round(sum(done) / len(done), 1) if done else None,
            "digest": round(t_digest, 1),
            "grand_total_wall": round(total, 1),
        },
        "per_ticker_seconds": {tk: round(dt, 1) for tk, dt in sorted(per_ticker)},
        "failures": [f"{tk}: {msg[:80]}" for tk, msg in failures],
        "interesting": [d.ticker for d in interesting],
        "pdf": str(out_pdf),
    }, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
