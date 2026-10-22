"""GDELT DOC 2.0 news vendor.

Keyless access to the GDELT global news index (https://api.gdeltproject.org/api/v2/doc/doc).
Serves both ticker news (via entity keyword search) and global/macro news.

No API key required — GDELT is fully open. The vendor degrades cleanly:
  - HTTP 429 (rate limited)  -> VendorUnavailableError (router skips to next vendor)
  - Empty article list        -> NoMarketDataError (router skips to next vendor)
  - Network/timeout errors    -> VendorUnavailableError

Rate limit: GDELT's DOC 2.0 API enforces 1 request per 5 seconds. The module
maintains a simple in-memory last-request timestamp and sleeps to respect it,
rather than blocking the whole agent. The sleep is capped so a cold start
never adds more than 5.5 seconds of latency.

Output shape matches what the other news vendors produce: a header line plus
a list of articles (title / source / date / link).
"""
import logging
import time
from datetime import UTC, datetime, timedelta

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

API_BASE = "https://api.gdeltproject.org/api/v2"

# GDELT's documented rate limit is 1 request per 5 seconds. A 0.5 s margin
# accounts for clock skew and bursty multi-vendor runs where yfinance, GDELT
# and Alpha Vantage each issue a request in the same tool call.
REQUEST_TIMEOUT: float = 30
_RATE_LIMIT_SECONDS: float = 5.5
_last_request_time: float = 0.0


# ---------------------------------------------------------------------------
# Entity mapping
# ---------------------------------------------------------------------------

# Yahoo-canonical symbol -> GDELT entity query term. GDELT has no ticker
# resolution; it is a free-text keyword search over a global news index.
# This table covers the most-traded NSE/BSE names; anything not listed falls
# back to the bare ticker stem plus the country hint (see _COUNTRY_HINTS).
_GDELT_COMPANY_NAMES: dict[str, str] = {
    "RELIANCE.NS": "Reliance Industries",
    "TCS.NS": "Tata Consultancy Services",
    "TATAMOTORS.NS": "Tata Motors",
    "INFY.NS": "Infosys",
    "WIPRO.NS": "Wipro",
    "HDFCBANK.NS": "HDFC Bank",
    "ICICIBANK.NS": "ICICI Bank",
    "SBIN.NS": "State Bank of India",
    "ADANIENT.NS": "Adani Enterprises",
    "BAJFINANCE.NS": "Bajaj Finance",
    "SUNPHARMA.NS": "Sun Pharma",
    "MARUTI.NS": "Maruti Suzuki",
}

# Exchange suffix -> country name appended to disambiguate entities. GDELT's
# default search covers ALL countries, so an NSE/BSE ticker without a country
# hint can match unrelated entities in other countries (e.g. "TATA" in the
# UK). The hint is soft — GDELT's sourcecountry filter is unreliable (see the
# scout notes), so this is a query-term disambiguator, not a hard filter.
_COUNTRY_HINTS: dict[str, str] = {
    ".NS": "India",
    ".BO": "India",
}


def _build_gdelt_query(canonical: str) -> tuple[str, str | None]:
    """Return ``(query_string, sourcecountry_or_None)`` for a canonical symbol.

    The query string is the entity term GDELT searches on. For .NS/.BO
    symbols the country hint is appended (e.g. ``Reliance Industries India``)
    and ``sourcecountry`` is returned as a soft filter param.
    """
    stem = canonical.upper()
    country = None
    for suffix, hint in _COUNTRY_HINTS.items():
        if stem.endswith(suffix):
            country = hint
            break
    base = _GDELT_COMPANY_NAMES.get(stem)
    if base is None:
        # Not in the table: strip any exchange suffix to get the bare stem.
        bare = stem
        for suffix in _COUNTRY_HINTS:
            if bare.endswith(suffix):
                bare = bare[: -len(suffix)]
        base = bare
    query = f"{base} {country}" if country else base
    return query, country


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _request(endpoint: str, params: dict) -> dict:
    """Call a GDELT endpoint, respecting the 5-second rate limit.

    Args:
        endpoint: URL path segment after ``/api/v2/`` (e.g. ``"doc/doc"``).
        params: Query parameters; keys are passed verbatim to GDELT.

    Returns:
        Parsed JSON body (a dict).

    Raises:
        VendorUnavailableError: HTTP 429, network error, or unexpected status.
    """
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    wait = _RATE_LIMIT_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)

    url = f"{API_BASE}/{endpoint}"
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"GDELT unreachable: {exc}") from exc

    _last_request_time = time.monotonic()

    if response.status_code == 429:
        raise VendorUnavailableError("GDELT rate limit exceeded (HTTP 429)")
    if response.status_code >= 400:
        raise VendorUnavailableError(f"GDELT returned HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError:
        raise VendorUnavailableError(
            f"GDELT returned non-JSON body: {response.text[:120]!r}"
        )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_seendate(value: str) -> datetime | None:
    """Parse GDELT's ``seendate`` format (``YYYYMMDDTHHMMSSZ``) to a UTC-aware datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        logger.warning("GDELT: could not parse seendate %r", value)
        return None


def _timespan_str(days: int) -> str:
    """GDELT ``timespan`` value; the API is confirmed to accept up to ``14d``."""
    return f"{max(1, min(days, 14))}d"


def _chunk_days(start_date: str, end_date: str, chunk_days: int = 14) -> list[tuple[str, str]]:
    """Split ``[start_date, end_date]`` into non-overlapping 14-day slabs.

    GDELT's ``timespan`` param is capped at 14 days per call; a longer window
    must be chunked. The last slab may be shorter.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        return []
    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        slab_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor.strftime("%Y-%m-%d"), slab_end.strftime("%Y-%m-%d")))
        cursor = slab_end + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_articles(articles: list[dict], header: str) -> str:
    """Render a list of GDELT articles in the same markdown shape as the other vendors."""
    lines: list[str] = []
    for article in articles:
        seendate = _parse_seendate(article.get("seendate", ""))
        pub_date = seendate.strftime("%Y-%m-%d") if seendate else ""
        domain = article.get("domain", "")
        title = article.get("title", "")
        url = article.get("url", "")
        lines.append(f"### {title} (source: {domain}, {pub_date})\nLink: {url}\n")
    if not lines:
        return header + "No GDELT articles found in this window.\n"
    return header + "\n".join(lines)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Fetch GDELT news about ``ticker`` published in ``[start_date, end_date]``.

    Args:
        ticker: The instrument ticker as the caller has it (Yahoo-canonical form).
        start_date: ``yyyy-mm-dd``.
        end_date: ``yyyy-mm-dd``.

    Returns:
        A markdown string in the same shape the other news vendors produce:
        a header line, then one entry per article with title / source / date / link.

    Raises:
        VendorUnavailableError: GDELT is rate-limited (429) or unreachable.
        NoMarketDataError: GDELT returned no articles for the query + window.
    """
    canonical = normalize_symbol(ticker)
    query, sourcecountry = _build_gdelt_query(canonical)

    # GDELT's timespan is capped at 14 days per call; chunk the requested
    # window into 14-day slabs and merge the results.
    chunks = _chunk_days(start_date, end_date)
    if not chunks:
        raise NoMarketDataError(ticker, canonical, "empty date window")

    config = get_config()
    max_records = config["news_article_limit"]
    all_articles: list[dict] = []

    for chunk_start, chunk_end in chunks:
        params: dict = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": _timespan_str(
                (datetime.strptime(chunk_end, "%Y-%m-%d") - datetime.strptime(chunk_start, "%Y-%m-%d")).days + 1
            ),
            "maxrecords": max_records,
        }
        if sourcecountry:
            # Soft hint — GDELT does not reliably restrict by sourcecountry,
            # but passing it still reduces cross-country noise in the ranking.
            params["sourcecountry"] = sourcecountry

        body = _request("doc/doc", params)
        articles = body.get("articles", [])
        all_articles.extend(articles)

    # Filter to the requested window on seendate (the only date field GDELT
    # returns in artlist JSON; GKG per-document tone/publish date is not
    # accessible via the DOC 2.0 API).
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=UTC)
    in_window = [
        a for a in all_articles
        if (seendate := _parse_seendate(a.get("seendate", ""))) is not None
        and start_dt <= seendate <= end_dt
    ]

    if not in_window:
        raise NoMarketDataError(ticker, canonical, "no GDELT articles in window")

    resolved_note = "" if canonical == ticker else f" (resolved to {canonical})"
    header = f"## GDELT news for {ticker}{resolved_note}, from {start_date} to {end_date}:\n"
    return _render_articles(in_window, header)


def get_global_news(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Fetch GDELT global/macro news for the configured query topics.

    Mirrors the signature of the yfinance and Alpha Vantage global news vendors.

    Args:
        as_of_date: ``yyyy-mm-dd``.
        look_back_days: Window; ``None`` -> config ``global_news_lookback_days`` (default 7).
        limit: Max articles; ``None`` -> config ``global_news_article_limit`` (default 10).

    Returns:
        Markdown string in the same shape as the other news vendors.

    Raises:
        VendorUnavailableError: GDELT is rate-limited or unreachable.
        NoMarketDataError: No GDELT articles matched any configured query.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    search_queries = config["global_news_queries"]
    if not search_queries:
        raise NoMarketDataError("", "", "no global_news_queries configured")

    timespan = _timespan_str(look_back_days)

    all_articles: list[dict] = []
    for query in search_queries:
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": timespan,
            "maxrecords": limit,
        }
        body = _request("doc/doc", params)
        all_articles.extend(body.get("articles", []))

    # Deduplicate by URL and filter to the requested window.
    start_dt = (
        datetime.strptime(as_of_date, "%Y-%m-%d")
        - timedelta(days=look_back_days)
    ).replace(tzinfo=UTC)
    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=UTC)

    seen: set[str] = set()
    in_window: list[dict] = []
    for article in all_articles:
        url = article.get("url", "")
        if url and url in seen:
            continue
        seen.add(url)
        seendate = _parse_seendate(article.get("seendate", ""))
        if seendate is None or not (start_dt <= seendate <= end_dt):
            continue
        in_window.append(article)

    if not in_window:
        raise NoMarketDataError("", "", "no GDELT global news in window")

    start_date = start_dt.strftime("%Y-%m-%d")
    header = f"## Global Market News (GDELT), from {start_date} to {as_of_date}:\n"
    return _render_articles(in_window[:limit], header)
