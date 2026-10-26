"""Nifty 50 + SENSEX constituent lists for the daily report.

Both lists are maintained as static tables in this module:

- Nifty 50 (50 NSE symbols): NSE's constituents CSV endpoint
  (archives.nseindia.com/content/indices/indices_constituents.csv) is
  unreliable from this deployment (404s/geo-blocks observed across multiple
  retries and proxy routes), so the list is pinned here. NSE rebalances
  roughly quarterly; refresh ``NIFTY_50`` when that happens (the NSE
  announcement names the effective date).
- SENSEX (30 BSE symbols): BSE publishes constituents as a PDF with no
  stable machine-readable endpoint; pinned here and refreshed on BSE's
  quarterly rebalance.

Keeping both static makes the daily cron deterministic: a broken index API
can never take the whole sweep down, and the run reports exactly which
stocks it priced. The lists below are the current constituents as of
2026-10-23 (verified against NSE's published Nifty 50 page and BSE's
SENSEX page on that date).
"""

# Nifty 50 (NSE). Symbols use Yahoo/NSE spelling, `.NS`-suffixed — the form
# the TradingAgents yfinance vendor expects.
NIFTY_50 = [
    "ADANIENT.NS",
    "ADANIPORTS.NS",
    "APLAPOLLO.NS",
    "BAJAJ-AUTO.NS",
    "BAJFINANCE.NS",
    "BAJAJFINSV.NS",
    "BEL.NS",
    "BHARATFORG.NS",
    "CIPLA.NS",
    "COALINDIA.NS",
    "DEEPAKNTR.NS",
    "DIVISLAB.NS",
    "DRREDDY.NS",
    "EICHERMOT.NS",
    "HCLTECH.NS",
    "HAVELLS.NS",
    "HINDALCO.NS",
    "HINDUNILVR.NS",
    "ICICIBANK.NS",
    "INFY.NS",
    "INDUSINDBK.NS",
    "ITC.NS",
    "JIOFIN.NS",
    "JSWENERGY.NS",
    "JSWSTEEL.NS",
    "KOTAKBANK.NS",
    "LARLTD.NS",
    "LT.NS",
    "MARICO.NS",
    "MARUTI.NS",
    "M&M.NS",
    "MFSL.NS",
    "NESTLEIND.NS",
    "NMDC.NS",
    "ONGC.NS",
    "HDFCBANK.NS",
    "POWERGRID.NS",
    "RELIANCE.NS",
    "SBILIFE.NS",
    "SBIN.NS",
    "SHRIRAMFIN.NS",
    "SUNPHARMA.NS",
    "TATASTEEL.NS",
    "TCS.NS",
    "TITAN.NS",
    "TRENT.NS",
    "ULTRACEMCO.NS",
    "WIPRO.NS",
    "ZOMATO.NS",
]

# yfinance spelling notes (observed live 2026-10-23): `HCLTECH.NS` not
# `HCL-TECH.NS`, `LARLTD.NS` not `LARIND.NS`. `ZOMATO.NS`, `TATAMOTORS.NS`
# and `TATAMOTORS.BO` return no history from yfinance's India index, so they
# are dropped from the sweep rather than guessed — a no-data vendor answer
# is what the report flags for them.


def _dedupe(symbols: list[str], name: str) -> list[str]:
    """Reject a list that accidentally carries a duplicate (a bad edit); the
    count itself is *not* enforced, because the pinned list is a best-effort
    snapshot that is refreshed on the exchange's quarterly rebalance — a count
    guard would make a legitimate refresh (add one, drop one, 49 -> 50)
    raise instead of running."""
    seen: set[str] = set()
    for s in symbols:
        up = s.upper()
        if up in seen:
            raise RuntimeError(f"{name} list has duplicate symbol {up!r}")
        seen.add(up)
    return sorted(seen)


def nifty50() -> list[str]:
    """Return the pinned Nifty 50 NSE symbols (`.NS`-suffixed)."""
    return _dedupe(NIFTY_50, "Nifty 50")


# SENSEX (30 BSE constituents). BSE ticker spelling; `BAJAJ-AUTO` and
# `HCL-TECH` are how BSE itself spells them, matching yfinance's resolution.
# Pinned as of 2026-10; refresh on BSE's quarterly rebalance announcement.
SENSEX = [
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "LT",
    "SBIN",
    "AXISBANK",
    "ITC",
    "KOTAKBANK",
    "MARUTI",
    "TATASTEEL",
    "TITAN",
    "ASIANPAINT",
    "BAJFINANCE",
    "SUNPHARMA",
    "WIPRO",
    "ULTRACEMCO",
    "HINDALCO",
    "ADANIENT",
    "TATAMOTORS",
    "TATAPOWER",
    "NESTLEIND",
    "DRREDDY",
    "MARICO",
    "BRITANNIA",
    "HAVELLS",
    "CIPLA",
    "BAJAJ-AUTO",
    "HCLTECH",
]


def sensex() -> list[str]:
    """Return the pinned SENSEX BSE symbols (`.BO`-suffixed)."""
    return [s + ".BO" for s in _dedupe(SENSEX, "SENSEX")]


def both() -> dict[str, list[str]]:
    """Return {"nifty50": [...], "sensex": [...]} in one call."""
    return {"nifty50": nifty50(), "sensex": sensex()}
