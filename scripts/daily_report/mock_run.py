"""Mock daily run: 5 random NSE tickers through the full pipeline + digest.

Times every layer so the operator can see where the wall-clock goes and
tune the sweep budget:
  1. bootstrap   - build config + TradingAgentsGraph
  2. sweep       - N tickers, per-ticker + avg
  3. digest      - read reports, select interesting, render PDF/HTML

Writes to a mock reports dir under DAILY_REPORTS_DIR/mock/<today>/ and the
digest to DAILY_REPORTS_DIR/mock/<today>_digest.pdf. Run inside the
container:

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


def main() -> int:
    t0 = time.time()
    config = config_module.build_default_config()
    today = get_current_date()
    reports_root = daily_config.reports_dir()
    mock_root = reports_root / "mock"
    out_dir = mock_root / today
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = random.sample(POOL, N_TICKERS)
    print(f"[mock] {today} · {len(tickers)} random tickers: {tickers}", flush=True)
    print(f"[mock] reports -> {out_dir}", flush=True)

    t1 = time.time()
    graph = TradingAgentsGraph(daily_config.analysts(), config=config)
    t_bootstrap = t1 - t0
    print(f"[time] bootstrap (config+graph): {t_bootstrap:.1f}s", flush=True)

    t_sweep_start = time.time()
    per_ticker: list[tuple[str, float]] = []
    failures: list[tuple[str, str]] = []
    for i, ticker in enumerate(tickers, 1):
        ts = time.time()
        try:
            final_state, _ = graph.propagate(ticker, today, asset_type="stock")
            target = out_dir / daily_runner._safe_name(ticker)
            graph.save_reports(final_state, ticker, target, html=True)
            print(f"  [{i}/{len(tickers)}] {ticker} done ({time.time() - ts:.1f}s)", flush=True)
            per_ticker.append((ticker, time.time() - ts))
        except Exception as exc:
            failures.append((ticker, str(exc)))
            print(f"  [{i}/{len(tickers)}] {ticker} FAILED ({time.time() - ts:.1f}s): {exc}",
                  flush=True)
    t_sweep = time.time() - t_sweep_start
    done = [dt for _tk, dt in per_ticker]
    print(f"[time] sweep total: {t_sweep:.1f}s · {len(per_ticker)} ok, "
          f"{len(failures)} failed", flush=True)
    if done:
        print(f"[time] per-ticker min/avg/max: "
              f"{min(done):.1f}s / {sum(done) / len(done):.1f}s / {max(done):.1f}s", flush=True)
    for tk, dt in per_ticker:
        print(f"       {tk}: {dt:.1f}s", flush=True)

    # Digest: reads directly from out_dir, PDF written to mock_root
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
        "seconds": {
            "bootstrap": round(t_bootstrap, 1),
            "sweep_total": round(t_sweep, 1),
            "sweep_avg_per_ticker": round(t_sweep / max(len(tickers), 1), 1),
            "digest": round(t_digest, 1),
            "grand_total": round(total, 1),
        },
        "per_ticker_seconds": {tk: round(dt, 1) for tk, dt in per_ticker},
        "failures": [f"{tk}: {msg[:80]}" for tk, msg in failures],
        "interesting": [d.ticker for d in interesting],
        "pdf": str(out_pdf),
    }, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
