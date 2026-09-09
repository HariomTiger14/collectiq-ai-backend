import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.admin_health_service import PricingHealthService

# Staleness is judged against the wall clock, so fixture load times must be
# relative — hardcoded dates rot past stale_after_hours and flip the suite red.
_FRESH_LOAD_ISO = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


class AdminPricingHealthEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_pricing_health_requires_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.get(
                "/admin/pricing/health",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_pricing_health_returns_admin_portal_payload(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_pricing.PricingHealthService",
        ) as service_factory:
            auth_settings.admin_import_token = "secret-token"
            service_factory.return_value.health.return_value = {
                "success": True,
                "status": "healthy",
                "generatedAt": "2026-07-27T00:00:00+00:00",
                "summary": {"status": "healthy"},
                "pricecharting": {"sources": []},
                "providers": [],
                "currency": {},
            }

            response = self.client.get(
                "/admin/pricing/health",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["status"], "healthy")

    def test_quick_health_returns_providers_and_currency_only(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_pricing.PricingHealthService",
        ) as service_factory:
            auth_settings.admin_import_token = "secret-token"
            service_factory.return_value.quick_health.return_value = {
                "success": True,
                "generatedAt": "2026-08-16T00:00:00+00:00",
                "providers": [{"name": "KicksDB", "configured": True}],
                "currency": {"defaultDisplayCurrency": "AUD"},
            }

            response = self.client.get(
                "/admin/pricing/health/quick",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["providers"][0]["name"], "KicksDB")
        self.assertEqual(payload["currency"]["defaultDisplayCurrency"], "AUD")
        self.assertNotIn("pricecharting", payload)
        self.assertNotIn("summary", payload)


class PricingHealthServiceTest(unittest.TestCase):
    def test_quick_health_makes_zero_network_calls(self) -> None:
        def _fail_on_any_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"quick_health() should never make a network call, got: {request.url}"
            )

        client = httpx.Client(transport=httpx.MockTransport(_fail_on_any_request))
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=client,
        )

        with patch("app.services.pricing.admin_health_service.settings") as settings:
            settings.pricecharting_api_key = ""
            settings.ebay_access_token = ""
            settings.ebay_client_id = ""
            settings.ebay_client_secret = ""
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_marketplace_insights_api_url = ""
            settings.ebay_partner_access_granted = False
            settings.tcgplayer_client_id = ""
            settings.tcgplayer_client_secret = ""
            settings.kicksdb_api_key = "kicks-key"
            settings.kicksdb_api_base = "https://api.kicks.dev"
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78

            payload = service.quick_health()

        self.assertTrue(payload["success"])
        self.assertIn("providers", payload)
        self.assertIn("currency", payload)
        kicksdb = next(p for p in payload["providers"] if p["key"] == "kicksdb")
        self.assertTrue(kicksdb["configured"])
        self.assertEqual(payload["currency"]["defaultDisplayCurrency"], "AUD")

    def test_uses_pricecharting_summary_rpc_for_large_sources(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(_supabase_summary_handler))
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=client,
            stale_after_hours=240,
        )

        with patch("app.services.pricing.admin_health_service.settings") as settings:
            settings.pricecharting_api_key = ""
            settings.ebay_access_token = ""
            settings.ebay_client_id = ""
            settings.ebay_client_secret = ""
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_marketplace_insights_api_url = ""
            settings.ebay_partner_access_granted = False
            settings.tcgplayer_client_id = ""
            settings.tcgplayer_client_secret = ""
            settings.kicksdb_api_key = "kicks-key"
            settings.kicksdb_api_base = "https://api.kicks.dev"
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78

            payload = service.health()

        self.assertEqual(payload["status"], "healthy")
        sources = {
            source["source"]: source for source in payload["pricecharting"]["sources"]
        }
        self.assertEqual(sources["magic.csv"]["currentRows"], 129605)
        self.assertEqual(sources["magic.csv"]["historyRows"], 134467)
        self.assertEqual(sources["magic.csv"]["closedHistoryRows"], 4862)
        self.assertEqual(payload["summary"]["errors"], [])

    def test_retries_pricecharting_summary_rpc_with_get(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(_supabase_post_fails_get_summary_handler)
        )
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=client,
            stale_after_hours=240,
        )

        with patch("app.services.pricing.admin_health_service.settings") as settings:
            settings.pricecharting_api_key = ""
            settings.ebay_access_token = ""
            settings.ebay_client_id = ""
            settings.ebay_client_secret = ""
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_marketplace_insights_api_url = ""
            settings.ebay_partner_access_granted = False
            settings.tcgplayer_client_id = ""
            settings.tcgplayer_client_secret = ""
            settings.kicksdb_api_key = ""
            settings.kicksdb_api_base = "https://api.kicks.dev"
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78

            payload = service.health()

        self.assertEqual(payload["status"], "healthy")
        sources = {
            source["source"]: source for source in payload["pricecharting"]["sources"]
        }
        self.assertEqual(sources["magic.csv"]["currentRows"], 129605)
        self.assertEqual(payload["summary"]["errors"], [])

    def test_reports_pricecharting_counts_and_provider_configuration(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(_supabase_handler))
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=client,
            stale_after_hours=240,
        )

        with patch("app.services.pricing.admin_health_service.settings") as settings:
            settings.pricecharting_api_key = ""
            settings.ebay_access_token = ""
            settings.ebay_client_id = ""
            settings.ebay_client_secret = ""
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_marketplace_insights_api_url = ""
            settings.ebay_partner_access_granted = False
            settings.tcgplayer_client_id = ""
            settings.tcgplayer_client_secret = ""
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78

            payload = service.health()

        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["summary"]["configuredProviderCount"], 2)
        self.assertEqual(payload["pricecharting"]["totalCurrentRows"], 5000)
        self.assertEqual(payload["pricecharting"]["totalHistoryRows"], 5500)
        self.assertEqual(payload["pricecharting"]["totalClosedHistoryRows"], 500)
        self.assertEqual(payload["pricecharting"]["sources"][0]["source"], "magic.csv")
        self.assertEqual(payload["pricecharting"]["sources"][0]["currentRows"], 1000)
        self.assertFalse(payload["pricecharting"]["sources"][0]["stale"])
        providers = {provider["key"]: provider for provider in payload["providers"]}
        self.assertTrue(providers["pricecharting_catalog"]["configured"])
        self.assertTrue(providers["kicksdb"]["configured"])
        self.assertEqual(providers["kicksdb"]["role"], "sneakers_streetwear")
        self.assertEqual(providers["ebay"]["status"], "unavailable")
        self.assertEqual(providers["ebay"]["reasonCode"], "PROVIDER_NOT_CONNECTED")
        self.assertEqual(payload["currency"]["rates"]["AUD"], 1.52)

    def test_ebay_credentials_without_partner_access_are_unavailable(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(_supabase_handler))
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=client,
            stale_after_hours=240,
        )

        with patch("app.services.pricing.admin_health_service.settings") as settings:
            settings.pricecharting_api_key = ""
            settings.ebay_access_token = "token-present"
            settings.ebay_client_id = ""
            settings.ebay_client_secret = ""
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_marketplace_insights_api_url = (
                "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
            )
            settings.ebay_partner_access_granted = False
            settings.ebay_browse_api_url = (
                "https://api.ebay.com/buy/browse/v1/item_summary/search"
            )
            settings.tcgplayer_client_id = ""
            settings.tcgplayer_client_secret = ""
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78

            payload = service.health()

        providers = {provider["key"]: provider for provider in payload["providers"]}
        self.assertFalse(providers["ebay"]["configured"])
        self.assertTrue(providers["ebay"]["credentialsPresent"])
        self.assertFalse(providers["ebay"]["partnerAccessGranted"])
        self.assertEqual(providers["ebay"]["status"], "unavailable")
        self.assertEqual(
            providers["ebay"]["reasonCode"],
            "PARTNER_ACCESS_NOT_GRANTED",
        )
        self.assertIn("partner access not granted", providers["ebay"]["message"])
        self.assertTrue(providers["ebay_metadata"]["configured"])
        self.assertEqual(providers["ebay_metadata"]["dataUse"], "metadata_only")
        self.assertEqual(
            providers["ebay_metadata"]["valuationStatus"],
            "not_valuation",
        )

    def test_missing_supabase_config_is_unhealthy_without_secret_details(self) -> None:
        service = PricingHealthService(supabase_url="", service_role_key="")

        payload = service.health()

        self.assertEqual(payload["status"], "unhealthy")
        self.assertFalse(payload["summary"]["catalogConfigured"])
        self.assertEqual(payload["pricecharting"]["sources"], [])


class TheHealthBoardDoesNotCallMetadataHistoryPriceFreshnessTest(unittest.TestCase):
    """SCD2 counts stopped meaning "prices are being refreshed" on 2026-09-09.

    Price-only changes now write a snapshot and no SCD2 version, so
    historyRows and closedHistoryRows go quiet for a pipeline that is running
    perfectly. Left unlabelled, that reads as a stall. The fields stay --
    removing a field breaks anything reading it -- but they are labelled, and
    a real price signal sits beside them.
    """

    def _payload(self, handler=None) -> dict:
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=httpx.Client(
                transport=httpx.MockTransport(handler or _supabase_handler)),
        )
        with patch("app.services.pricing.admin_health_service.settings") as settings:
            settings.pricecharting_api_key = ""
            settings.ebay_access_token = ""
            settings.ebay_client_id = ""
            settings.ebay_client_secret = ""
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_marketplace_insights_api_url = ""
            settings.ebay_partner_access_granted = False
            settings.tcgplayer_client_id = ""
            settings.tcgplayer_client_secret = ""
            settings.kicksdb_api_key = ""
            settings.kicksdb_api_base = ""
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78
            return service.health()

    def test_the_old_fields_are_still_there(self) -> None:
        source = self._payload()["pricecharting"]["sources"][0]
        self.assertIn("historyRows", source)
        self.assertIn("closedHistoryRows", source)

    def test_they_are_labelled_as_metadata_not_price(self) -> None:
        source = self._payload()["pricecharting"]["sources"][0]
        self.assertEqual(source["historyRowsMeasure"], "metadata_versions")

    def test_a_price_freshness_number_sits_beside_them(self) -> None:
        sources = self._payload()["pricecharting"]["sources"]
        by_source = {item["source"]: item for item in sources}
        self.assertEqual(by_source["pokemon.csv"]["priceSnapshotRows"], 4242)

    def test_the_total_is_aggregated_too(self) -> None:
        pricecharting = self._payload()["pricecharting"]
        self.assertEqual(
            pricecharting["totalPriceSnapshotRows"],
            sum(item["priceSnapshotRows"] for item in pricecharting["sources"]))

    def test_the_rpc_backed_path_carries_the_same_labels(self) -> None:
        """The RPC path is the one production uses; the REST path is fallback.

        A mutation that stripped the label from only the RPC path passed every
        other test here, because they all exercise the fallback.
        """
        service = PricingHealthService(
            supabase_url="https://packlox.supabase.co",
            service_role_key="service-role-key",
            client=httpx.Client(transport=httpx.MockTransport(_supabase_summary_handler)),
            stale_after_hours=240,
        )
        with patch("app.services.pricing.admin_health_service.settings") as settings:
            for attribute in ("pricecharting_api_key", "ebay_access_token",
                              "ebay_client_id", "ebay_client_secret",
                              "ebay_marketplace_insights_api_url",
                              "tcgplayer_client_id", "tcgplayer_client_secret",
                              "kicksdb_api_key", "kicksdb_api_base"):
                setattr(settings, attribute, "")
            settings.ebay_marketplace_id = "EBAY_AU"
            settings.ebay_partner_access_granted = False
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.52
            settings.fx_usd_to_cad = 1.37
            settings.fx_usd_to_gbp = 0.78
            payload = service.health()
        for source in payload["pricecharting"]["sources"]:
            with self.subTest(source=source["source"]):
                self.assertEqual(source["historyRowsMeasure"], "metadata_versions")
                self.assertIn("priceSnapshotRows", source)

    def test_the_price_signal_reads_the_snapshot_table(self) -> None:
        """Reading it from the SCD2 table is the bug this whole class is about."""
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return _supabase_handler(request)

        self._payload(handler)
        self.assertTrue(
            any(path.endswith("/pricecharting_price_history") for path in requested),
            "health never asked the snapshot table anything")


def _price_snapshot_count(source: str) -> int:
    """Snapshots written for a source: the number that now means "prices are
    still being refreshed", where closedHistoryRows used to."""
    return {"pokemon.csv": 4242}.get(source, 17)


def _supabase_handler(request: httpx.Request) -> httpx.Response:
    source = str(request.url.params.get("source_file") or "").replace("eq.", "")
    path = request.url.path
    if path.endswith("/pricecharting_catalog") and request.headers.get("range") == "0-0":
        return httpx.Response(
            200,
            json=[],
            headers={"content-range": f"0-0/{_current_count(source)}"},
        )
    if (
        path.endswith("/pricecharting_catalog_history")
        and request.headers.get("range") == "0-0"
    ):
        count = (
            _closed_history_count(source)
            if request.url.params.get("is_current")
            else _history_count(source)
        )
        return httpx.Response(
            200,
            json=[],
            headers={"content-range": f"0-0/{count}"},
        )
    if (
        path.endswith("/pricecharting_price_history")
        and request.headers.get("range") == "0-0"
    ):
        # The price-freshness probe. Added 2026-09-09: SCD2 history stopped
        # counting price refreshes, so a handler that 404s here would have
        # reported the whole board unhealthy -- which is how this fake caught
        # the change in the first place.
        return httpx.Response(
            200,
            json=[],
            headers={"content-range": f"0-0/{_price_snapshot_count(source)}"},
        )
    if path.endswith("/pricecharting_catalog"):
        return httpx.Response(
            200,
            json=[
                {
                    "source_downloaded_at": _FRESH_LOAD_ISO,
                    "imported_at": _FRESH_LOAD_ISO,
                    "updated_at": _FRESH_LOAD_ISO,
                }
            ],
        )
    return httpx.Response(404, json={"message": "not found"})


def _supabase_summary_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/rpc/pricecharting_catalog_health_summary"):
        return httpx.Response(
            200,
            json=[
                {
                    "source_file": "magic.csv",
                    "current_rows": 129605,
                    "history_rows": 134467,
                    "closed_history_rows": 4862,
                    "last_loaded_at": _FRESH_LOAD_ISO,
                },
                {
                    "source_file": "one_piece.csv",
                    "current_rows": 11847,
                    "history_rows": 12466,
                    "closed_history_rows": 619,
                    "last_loaded_at": _FRESH_LOAD_ISO,
                },
                {
                    "source_file": "pokemon.csv",
                    "current_rows": 91278,
                    "history_rows": 96424,
                    "closed_history_rows": 5146,
                    "last_loaded_at": _FRESH_LOAD_ISO,
                },
                {
                    "source_file": "video_games.csv",
                    "current_rows": 122186,
                    "history_rows": 125947,
                    "closed_history_rows": 3761,
                    "last_loaded_at": _FRESH_LOAD_ISO,
                },
                {
                    "source_file": "yugioh.csv",
                    "current_rows": 77428,
                    "history_rows": 80188,
                    "closed_history_rows": 2760,
                    "last_loaded_at": _FRESH_LOAD_ISO,
                },
            ],
        )
    return httpx.Response(500, json={"message": "unexpected slow health path"})


def _supabase_post_fails_get_summary_handler(request: httpx.Request) -> httpx.Response:
    if (
        request.url.path.endswith("/rpc/pricecharting_catalog_health_summary")
        and request.method == "POST"
    ):
        return httpx.Response(404, json={"message": "schema cache miss"})
    return _supabase_summary_handler(request)


def _current_count(source: str) -> int:
    return {
        "magic.csv": 1000,
        "one_piece.csv": 1000,
        "pokemon.csv": 1000,
        "video_games.csv": 1000,
        "yugioh.csv": 1000,
    }.get(source, 0)


def _history_count(source: str) -> int:
    return _current_count(source) + 100


def _closed_history_count(source: str) -> int:
    known_sources = {
        "magic.csv",
        "one_piece.csv",
        "pokemon.csv",
        "video_games.csv",
        "yugioh.csv",
    }
    return 100 if source in known_sources else 0


if __name__ == "__main__":
    unittest.main()
