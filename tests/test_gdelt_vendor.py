"""GDELT keyless news vendor: entity mapping, windowing, and error taxonomy.

All HTTP access is mocked, so these run without a network connection or a key.
"""

import copy
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import gdelt

# ---------------------------------------------------------------------------
# Entity mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class GdeltEntityMappingTests(unittest.TestCase):
    def test_known_nse_symbol_maps_to_english_company_name(self):
        query, country = gdelt._build_gdelt_query("RELIANCE.NS")
        self.assertEqual(query, "Reliance Industries India")
        self.assertEqual(country, "India")

    def test_known_bse_symbol_maps_to_english_company_name(self):
        # .BO is BSE; the same Indian company should resolve to the same query.
        query, country = gdelt._build_gdelt_query("RELIANCE.BO")
        self.assertEqual(country, "India")

    def test_unknown_nse_symbol_falls_back_to_bare_stem(self):
        # A symbol not in the table drops the .NS suffix and gets the country hint.
        query, country = gdelt._build_gdelt_query("SOMEUNKNOWNSYM.NS")
        self.assertEqual(query, "SOMEUNKNOWNSYM India")
        self.assertEqual(country, "India")

    def test_non_indian_symbol_has_no_country_hint(self):
        query, country = gdelt._build_gdelt_query("AAPL")
        self.assertEqual(query, "AAPL")
        self.assertIsNone(country)

    def test_nse_bse_both_get_india_hint(self):
        for sym in ("INFY.NS", "INFY.BO", "TCS.NS"):
            query, country = gdelt._build_gdelt_query(sym)
            self.assertEqual(country, "India", sym)


# ---------------------------------------------------------------------------
# get_news
# ---------------------------------------------------------------------------


def _articles(dates: list[str]) -> list[dict]:
    """Build GDELT-shaped article dicts with seendate in YYYYMMDDTHHMMSSZ form."""
    out = []
    for i, day in enumerate(dates):
        out.append(
            {
                "seendate": day.replace("-", "") + "T120000Z",
                "title": f"Article {i}",
                "domain": f"domain{i}.com",
                "url": f"https://example.com/{i}",
            }
        )
    return out


@pytest.mark.unit
class GdeltGetNewsTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_happy_path_renders_markdown_with_title_source_date(self):
        dates = ["2026-01-05", "2026-01-06"]
        body = {"articles": _articles(dates)}
        with mock.patch.object(gdelt, "_request", return_value=body):
            out = gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")
        self.assertIn("## GDELT news for RELIANCE.NS", out)
        self.assertIn("### Article 0", out)
        self.assertIn("(source: domain0.com, 2026-01-05)", out)
        self.assertIn("Link: https://example.com/0", out)
        self.assertIn("### Article 1", out)

    def test_empty_article_list_raises_no_market_data(self):
        with (
            mock.patch.object(gdelt, "_request", return_value={"articles": []}),
            self.assertRaises(NoMarketDataError),
        ):
            gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")

    def test_429_raises_vendor_unavailable(self):
        resp = mock.Mock(status_code=429)
        resp.json.return_value = {}
        with (
            mock.patch.object(gdelt.requests, "get", return_value=resp),
            self.assertRaises(VendorUnavailableError) as ctx,
        ):
            gdelt._request("doc/doc", {})
        self.assertIn("429", str(ctx.exception))

    def test_network_error_raises_vendor_unavailable(self):
        with (
            mock.patch.object(gdelt.requests, "get", side_effect=requests.ConnectionError("boom")),
            self.assertRaises(VendorUnavailableError),
        ):
            gdelt._request("doc/doc", {})

    def test_unparseable_seendate_articles_excluded(self):
        # Articles with no seendate are dropped; if all are undated -> NoMarketData.
        body = {"articles": [{"title": "X", "domain": "d", "url": "u", "seendate": ""}]}
        with (
            mock.patch.object(gdelt, "_request", return_value=body),
            self.assertRaises(NoMarketDataError),
        ):
            gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")

    def test_lookahead_no_article_after_end_date(self):
        # An article stamped after the window end must not be served.
        body = {"articles": _articles(["2026-01-05", "2026-01-20"])}
        with mock.patch.object(gdelt, "_request", return_value=body):
            out = gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")
        self.assertIn("Article 0", out)
        self.assertNotIn("Article 1", out)  # 2026-01-20 is after end_date

    def test_chunks_called_multiple_times_for_long_window(self):
        # A 400-day window exceeds the 365-day DOC-API slab and must be
        # chunked into two calls; a 30-day window is a single call.
        body = {"articles": []}
        with (
            mock.patch.object(gdelt, "_request", return_value=body) as req,
            self.assertRaises(NoMarketDataError),
        ):
            gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-03-30")
        self.assertEqual(req.call_count, 1)  # 89 days fits one slab
        with (
            mock.patch.object(gdelt, "_request", return_value=body) as req,
            self.assertRaises(NoMarketDataError),
        ):
            gdelt.get_news("RELIANCE.NS", "2025-01-01", "2026-01-01")
        self.assertEqual(req.call_count, 2)  # 366 days -> 365-day slab + 1-day
        # tail, so two calls

    def test_absolute_date_bounds_sent_per_chunk(self):
        # Each chunk must carry GDELT absolute bounds addressing that slab,
        # not a rolling timespan measured from now.
        seen = []

        def _capture(endpoint, params):
            seen.append(dict(params))
            return {"articles": _articles(["2026-01-05"])}

        with mock.patch.object(gdelt, "_request", side_effect=_capture):
            gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")
        self.assertNotIn("timespan", seen[0])
        self.assertEqual(seen[0]["startdatetime"], "20260101000000")
        self.assertEqual(seen[0]["enddatetime"], "20260110235959")

    def test_sourcecountry_param_passed_for_indian_symbols(self):
        seen = {}

        def _capture(endpoint, params):
            seen.update(params)
            return {"articles": _articles(["2026-01-05"])}

        with mock.patch.object(gdelt, "_request", side_effect=_capture):
            gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")
        self.assertEqual(seen.get("sourcecountry"), "India")
        self.assertEqual(seen.get("query"), "Reliance Industries India")

    def test_sourcecountry_not_set_for_non_indian_symbols(self):
        seen = {}

        def _capture(endpoint, params):
            seen.update(params)
            return {"articles": _articles(["2026-01-05"])}

        with mock.patch.object(gdelt, "_request", side_effect=_capture):
            gdelt.get_news("AAPL", "2026-01-01", "2026-01-10")
        self.assertNotIn("sourcecountry", seen)


# ---------------------------------------------------------------------------
# get_global_news
# ---------------------------------------------------------------------------


@pytest.mark.unit
class GdeltGlobalNewsTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_look_back_days_honored(self):
        # Explicit look_back_days must bound the absolute GDELT search window,
        # not a rolling timespan that ignores the requested dates.
        seen = {}

        def _capture(endpoint, params):
            seen.update(params)
            return {"articles": _articles(["2026-01-05"])}

        with mock.patch.object(gdelt, "_request", side_effect=_capture):
            gdelt.get_global_news("2026-01-10", look_back_days=14, limit=5)
        self.assertNotIn("timespan", seen)
        self.assertEqual(seen.get("startdatetime"), "20251227000000")
        self.assertEqual(seen.get("enddatetime"), "20260110235959")
        self.assertEqual(seen.get("maxrecords"), 5)

    def test_limit_caps_articles_returned(self):
        # GDELT may return more than `limit`; the renderer must cap it.
        body = {
            "articles": _articles(
                [
                    "2026-01-01",
                    "2026-01-02",
                    "2026-01-03",
                    "2026-01-04",
                    "2026-01-05",
                ]
            )
        }
        with mock.patch.object(gdelt, "_request", return_value=body):
            out = gdelt.get_global_news("2026-01-10", look_back_days=14, limit=3)
        # limit=3 caps the rendered article count to 3
        self.assertEqual(out.count("### Article"), 3)

    def test_global_news_no_articles_raises_no_market_data(self):
        with (
            mock.patch.object(gdelt, "_request", return_value={"articles": []}),
            self.assertRaises(NoMarketDataError),
        ):
            gdelt.get_global_news("2026-01-10", look_back_days=14, limit=5)

    def test_global_news_empty_queries_config_raises_no_market_data(self):
        set_config({"global_news_queries": []})
        with self.assertRaises(NoMarketDataError):
            gdelt.get_global_news("2026-01-10", look_back_days=14, limit=5)

    def test_global_news_deduplicates_by_url(self):
        # Same URL returned by two queries must appear only once.
        set_config({"global_news_queries": ["queryA", "queryB"]})  # exactly 2 -> 2 GDELT calls
        dup = {
            "seendate": "20260105T120000Z",
            "title": "Dup",
            "domain": "d.com",
            "url": "https://example.com/dup",
        }
        other = {
            "seendate": "20260105T120000Z",
            "title": "Other",
            "domain": "e.com",
            "url": "https://example.com/other",
        }
        calls = {"n": 0}
        bodies = [
            {"articles": [dup, other]},
            {"articles": [dict(dup)]},  # duplicate on second query
        ]

        def _dispatch(endpoint, params):
            body = bodies[calls["n"]]
            calls["n"] += 1
            return body

        with mock.patch.object(gdelt, "_request", side_effect=_dispatch):
            out = gdelt.get_global_news("2026-01-10", look_back_days=14, limit=10)
        self.assertEqual(calls["n"], 2)  # one GDELT call per query
        self.assertEqual(out.count("https://example.com/dup"), 1)
        self.assertIn("https://example.com/other", out)

    def test_global_news_future_articles_excluded(self):
        # An article after as_of_date must not leak into the output.
        future = {
            "seendate": "20260201T000000Z",
            "title": "FUTURE",
            "domain": "d.com",
            "url": "https://example.com/future",
        }
        past = {
            "seendate": "20260105T120000Z",
            "title": "PAST",
            "domain": "d.com",
            "url": "https://example.com/past",
        }
        with mock.patch.object(gdelt, "_request", return_value={"articles": [past, future]}):
            out = gdelt.get_global_news("2026-01-10", look_back_days=14, limit=10)
        self.assertIn("PAST", out)
        self.assertNotIn("FUTURE", out)


# ---------------------------------------------------------------------------
# Router: gdelt in the chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
class GdeltRouterTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_gdelt_no_market_data_falls_through_to_next_vendor(self):
        """With gdelt in the chain, a gdelt NoMarketDataError skips to the next
        configured vendor rather than terminating the call."""
        from tradingagents.dataflows import router

        set_config({"data_vendors": {"news_data": "gdelt,alpha_vantage"}})

        def _gdelt_no_data(*a, **k):
            raise NoMarketDataError("RELIANCE.NS", "RELIANCE.NS", "no GDELT articles")

        def _alpha_returns(*a, **k):
            return "ALPHA_VANTAGE_NEWS"

        with mock.patch.dict(
            router.VENDOR_METHODS,
            {"get_news": {"gdelt": _gdelt_no_data, "alpha_vantage": _alpha_returns}},
            clear=False,
        ):
            out = router.route_to_vendor("get_news", "RELIANCE.NS", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "ALPHA_VANTAGE_NEWS")

    def test_keyless_gdelt_not_treated_as_not_configured(self):
        """GDELT needs no API key; a keyless gdelt must not raise
        VendorNotConfiguredError — it is always 'configured'. With an empty
        article list it raises NoMarketDataError instead, which the router
        treats as 'skip to next vendor'."""
        from tradingagents.dataflows.errors import VendorNotConfiguredError

        # Directly: the keyless vendor raises NoMarketDataError (not a
        # VendorNotConfiguredError) when it has no articles.
        with (
            mock.patch.object(gdelt, "_request", return_value={"articles": []}),
            self.assertRaises(NoMarketDataError),
        ):
            gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")

        # And that error must NOT be a VendorNotConfiguredError, so the router
        # will fall through rather than treat gdelt as unconfigured.
        with mock.patch.object(gdelt, "_request", return_value={"articles": []}):
            try:
                gdelt.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")
            except NoMarketDataError as e:
                self.assertNotIsInstance(e, VendorNotConfiguredError)
            else:
                self.fail("expected NoMarketDataError")

    def test_gdelt_unavailable_falls_to_next_vendor(self):
        from tradingagents.dataflows import router

        set_config({"data_vendors": {"news_data": "gdelt,yfinance"}})

        def _gdelt_unavailable(*a, **k):
            raise VendorUnavailableError("GDELT rate limit exceeded (HTTP 429)")

        def _yfinance_returns(*a, **k):
            return "YAHOO_NEWS"

        with mock.patch.dict(
            router.VENDOR_METHODS,
            {"get_news": {"gdelt": _gdelt_unavailable, "yfinance": _yfinance_returns}},
            clear=False,
        ):
            out = router.route_to_vendor("get_news", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "YAHOO_NEWS")


# ---------------------------------------------------------------------------
# Config: news_data default chain includes gdelt and is overridable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class GdeltConfigTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_default_news_data_chain_includes_gdelt(self):
        from tradingagents.dataflows.config import get_config

        chain = get_config()["data_vendors"]["news_data"]
        self.assertIn("gdelt", chain)

    def test_news_data_chain_is_overridable(self):
        set_config({"data_vendors": {"news_data": "gdelt"}})
        from tradingagents.dataflows.config import get_config

        chain = get_config()["data_vendors"]["news_data"]
        self.assertEqual(chain, "gdelt")

    def test_global_news_lookback_days_default(self):
        from tradingagents.dataflows.config import get_config

        self.assertEqual(get_config()["global_news_lookback_days"], 7)

    def test_global_news_article_limit_default(self):
        from tradingagents.dataflows.config import get_config

        self.assertEqual(get_config()["global_news_article_limit"], 10)


# ---------------------------------------------------------------------------
# AV alias: NSE (.NS) and BSE (.BO) map to the AV ticker, yfinance keeps .NS/.BO
# ---------------------------------------------------------------------------


@pytest.mark.unit
class AvAliasTests(unittest.TestCase):
    def test_nse_symbol_maps_to_av_bo_suffix(self):
        from tradingagents.dataflows.symbols import normalize_symbol, to_alpha_vantage_symbol

        canonical = normalize_symbol("RELIANCE.NS")
        self.assertEqual(canonical, "RELIANCE.NS")  # yfinance keeps .NS
        av_symbol = to_alpha_vantage_symbol(canonical)
        self.assertEqual(av_symbol, "RELIANCE.BO")  # AV gets .BO

    def test_bse_symbol_maps_to_av_bo_suffix(self):
        from tradingagents.dataflows.symbols import normalize_symbol, to_alpha_vantage_symbol

        canonical = normalize_symbol("RELIANCE.BO")
        self.assertEqual(canonical, "RELIANCE.BO")  # yfinance keeps .BO
        av_symbol = to_alpha_vantage_symbol(canonical)
        self.assertEqual(av_symbol, "RELIANCE.BO")  # AV keeps .BO (already BSE)

    def test_nse_symbol_yfinance_gets_ns_av_gets_bo(self):
        """The design choice: yfinance sees .NS, AV sees .BO."""
        from tradingagents.dataflows.symbols import normalize_symbol, to_alpha_vantage_symbol

        yahoo_sym = normalize_symbol("TCS.NS")
        av_sym = to_alpha_vantage_symbol(yahoo_sym)
        self.assertEqual(yahoo_sym, "TCS.NS")
        self.assertEqual(av_sym, "TCS.BO")

    def test_us_symbol_unchanged_in_av_mapping(self):
        from tradingagents.dataflows.symbols import normalize_symbol, to_alpha_vantage_symbol

        self.assertEqual(to_alpha_vantage_symbol(normalize_symbol("AAPL")), "AAPL")

    def test_av_get_news_queries_with_av_symbol_not_yahoo_symbol(self):
        """The AV get_news must pass the AV-spelled symbol, not the Yahoo one."""
        import tradingagents.dataflows.vendors.alpha_vantage.news as av_news

        seen = {}
        with mock.patch.object(
            av_news, "_make_api_request", side_effect=lambda fn, params: seen.update(params) or "{}"
        ):
            av_news.get_news("RELIANCE.NS", "2026-01-01", "2026-01-10")
        self.assertEqual(seen.get("tickers"), "RELIANCE.BO")
        # yfinance path still gets the .NS symbol
        from tradingagents.dataflows.symbols import normalize_symbol

        self.assertEqual(normalize_symbol("RELIANCE.NS"), "RELIANCE.NS")


if __name__ == "__main__":
    unittest.main()
