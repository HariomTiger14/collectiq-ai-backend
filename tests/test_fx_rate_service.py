import json
import unittest

import httpx

from app.services.pricing.fx_rate_service import FxRateService, FxRateServiceError


def _service(handler) -> FxRateService:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return FxRateService(
        supabase_url="https://supabase.example",
        service_role_key="service-role-key",
        client=client,
    )


class RefreshLatestTest(unittest.TestCase):
    def test_upserts_todays_rates_from_frankfurter(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if "frankfurter" in str(request.url.host):
                self.assertEqual(request.url.params["base"], "USD")
                return httpx.Response(
                    200,
                    json={
                        "amount": 1.0,
                        "base": "USD",
                        "date": "2026-08-22",
                        "rates": {"AUD": 1.53, "CAD": 1.38, "GBP": 0.79},
                    },
                )
            self.assertEqual(request.method, "POST")
            self.assertIn("fx_rates_daily", str(request.url))
            self.assertEqual(
                request.headers.get("prefer"), "resolution=merge-duplicates,return=minimal"
            )
            body = json.loads(request.content)
            self.assertEqual(len(body), 3)
            self.assertEqual(body[0]["rate_date"], "2026-08-22")
            return httpx.Response(201)

        rows_written = _service(handler).refresh_latest()

        self.assertEqual(rows_written, 3)
        self.assertEqual(len(captured), 2)

    def test_raises_when_frankfurter_response_missing_date(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rates": {"AUD": 1.5}})

        with self.assertRaises(FxRateServiceError):
            _service(handler).refresh_latest()

    def test_raises_when_frankfurter_errors(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        with self.assertRaises(FxRateServiceError):
            _service(handler).refresh_latest()


class BackfillHistoricalTest(unittest.TestCase):
    def test_upserts_every_day_in_the_range(self) -> None:
        posted_bodies: list[list[dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "frankfurter" in str(request.url.host):
                return httpx.Response(
                    200,
                    json={
                        "amount": 1.0,
                        "base": "USD",
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-02",
                        "rates": {
                            "2026-08-01": {"AUD": 1.50, "CAD": 1.36, "GBP": 0.77},
                            "2026-08-02": {"AUD": 1.51, "CAD": 1.37, "GBP": 0.78},
                        },
                    },
                )
            posted_bodies.append(json.loads(request.content))
            return httpx.Response(201)

        rows_written = _service(handler).backfill_historical(
            start_date="2026-08-01", end_date="2026-08-02"
        )

        self.assertEqual(rows_written, 6)
        self.assertEqual(len(posted_bodies[0]), 6)


class GetRateTest(unittest.TestCase):
    def test_usd_is_always_one(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not query Supabase for USD")

        rate = _service(handler).get_rate("usd", "2026-08-22")

        self.assertEqual(rate, 1.0)

    def test_returns_the_stored_rate_for_that_date(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["currency"], "eq.AUD")
            self.assertEqual(request.url.params["rate_date"], "lte.2026-08-22")
            self.assertEqual(request.url.params["order"], "rate_date.desc")
            return httpx.Response(200, json=[{"usd_rate": 1.52}])

        rate = _service(handler).get_rate("AUD", "2026-08-22")

        self.assertEqual(rate, 1.52)

    def test_falls_back_to_the_most_recent_earlier_date_automatically(self) -> None:
        # The mock just returns whatever the (lte + desc-order + limit-1)
        # query would return for a weekend/holiday with no rate of its own
        # -- the "most recent earlier date" behavior is expressed entirely
        # by the query shape asserted above, not extra client-side logic.
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"usd_rate": 1.49}])

        rate = _service(handler).get_rate("AUD", "2026-08-23")

        self.assertEqual(rate, 1.49)

    def test_returns_none_when_nothing_is_stored_yet(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        rate = _service(handler).get_rate("AUD", "2020-01-01")

        self.assertIsNone(rate)

    def test_returns_none_for_an_unsupported_currency(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not query Supabase for an unsupported currency")

        rate = _service(handler).get_rate("JPY", "2026-08-22")

        self.assertIsNone(rate)


class RatesForRangeTest(unittest.TestCase):
    def test_filters_to_supported_currencies_and_the_date_range(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["currency"], "in.(AUD,GBP)")
            self.assertEqual(
                request.url.params.get_list("rate_date"),
                ["gte.2026-08-01", "lte.2026-08-22"],
            )
            return httpx.Response(
                200,
                json=[{"rate_date": "2026-08-01", "currency": "AUD", "usd_rate": 1.5}],
            )

        rows = _service(handler).rates_for_range(
            currencies=["AUD", "GBP", "JPY"],
            from_date="2026-08-01",
            to_date="2026-08-22",
        )

        self.assertEqual(len(rows), 1)

    def test_returns_empty_when_no_supported_currency_was_requested(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not query Supabase with no valid currency")

        rows = _service(handler).rates_for_range(
            currencies=["JPY"], from_date="2026-08-01", to_date="2026-08-22"
        )

        self.assertEqual(rows, [])


class CurrentRatesTest(unittest.TestCase):
    def test_includes_usd_and_the_latest_stored_rate_per_currency(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            currency = request.url.params["currency"]
            rates = {"eq.AUD": 1.52, "eq.CAD": 1.37, "eq.GBP": 0.78}
            return httpx.Response(200, json=[{"usd_rate": rates[currency]}])

        rates = _service(handler).current_rates()

        self.assertEqual(rates, {"USD": 1.0, "AUD": 1.52, "CAD": 1.37, "GBP": 0.78})

    def test_omits_a_currency_with_no_stored_row_yet(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params["currency"] == "eq.AUD":
                return httpx.Response(200, json=[{"usd_rate": 1.52}])
            return httpx.Response(200, json=[])

        rates = _service(handler).current_rates()

        self.assertEqual(rates, {"USD": 1.0, "AUD": 1.52})


class NotConfiguredTest(unittest.TestCase):
    def test_supabase_calls_raise_when_credentials_are_missing(self) -> None:
        service = FxRateService(supabase_url="", service_role_key="")

        self.assertFalse(service.is_configured)
        with self.assertRaises(FxRateServiceError):
            service.get_rate("AUD", "2026-08-22")


if __name__ == "__main__":
    unittest.main()
