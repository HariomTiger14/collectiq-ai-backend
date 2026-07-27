import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    MarketComparableSale,
    PricingProviderUnavailableError,
    PricingResult,
    utc_timestamp,
)


class RepriceEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_reprice_returns_available_pricing_for_corrected_identity(self) -> None:
        with patch(
            "app.services.pricing.reprice_service.get_pricing_provider",
            return_value=_FakePricingProvider(),
        ):
            response = self.client.post("/api/pricing/reprice", json=_payload())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["itemId"], "portfolio-1")
        self.assertEqual(body["identity"]["title"], "1999 Pokemon Charizard Holo")
        pricing = body["pricing"]
        self.assertEqual(pricing["status"], "available")
        self.assertEqual(pricing["estimatedMarketValue"], 420)
        self.assertEqual(pricing["currency"], "AUD")
        self.assertEqual(pricing["displayString"], "AUD $420.00")
        self.assertEqual(pricing["pricingSource"]["name"], "PriceCharting")
        self.assertEqual(pricing["pricingSource"]["attributionText"], "Pricing data powered by PriceCharting")
        self.assertEqual(pricing["matchMetadata"]["comparableCount"], 2)
        self.assertEqual(len(pricing["comparableSales"]), 2)

    def test_reprice_returns_unavailable_when_provider_has_no_market_match(self) -> None:
        with patch(
            "app.services.pricing.reprice_service.get_pricing_provider",
            return_value=_FailingPricingProvider(EmptyMarketDataError("no trusted comps")),
        ):
            response = self.client.post("/api/pricing/reprice", json=_payload())

        self.assertEqual(response.status_code, 200)
        pricing = response.json()["pricing"]
        self.assertEqual(pricing["status"], "unavailable")
        self.assertEqual(pricing["reasonCode"], "NO_MARKET_MATCH")
        self.assertIn("no trusted comps", pricing["displayMessage"])
        self.assertIsNone(pricing["estimatedMarketValue"])

    def test_reprice_returns_unavailable_when_provider_not_configured(self) -> None:
        with patch(
            "app.services.pricing.reprice_service.get_pricing_provider",
            return_value=_FailingPricingProvider(
                PricingProviderUnavailableError("provider missing")
            ),
        ):
            response = self.client.post("/api/pricing/reprice", json=_payload())

        self.assertEqual(response.status_code, 200)
        pricing = response.json()["pricing"]
        self.assertEqual(pricing["status"], "unavailable")
        self.assertEqual(pricing["reasonCode"], "PROVIDER_NOT_CONFIGURED")
        self.assertIn("provider missing", pricing["displayMessage"])

    def test_reprice_converts_provider_usd_to_display_currency(self) -> None:
        payload = _payload()
        payload["displayCurrency"] = "AUD"
        with patch(
            "app.services.pricing.currency_conversion.settings",
        ) as currency_settings, patch(
            "app.services.pricing.reprice_service.get_pricing_provider",
            return_value=_UsdPricingProvider(),
        ):
            currency_settings.default_display_currency = "AUD"
            currency_settings.fx_usd_to_aud = 1.5
            currency_settings.fx_usd_to_cad = 1.35
            currency_settings.fx_usd_to_gbp = 0.8

            response = self.client.post("/api/pricing/reprice", json=payload)

        self.assertEqual(response.status_code, 200)
        pricing = response.json()["pricing"]
        self.assertEqual(pricing["status"], "available")
        self.assertEqual(pricing["estimatedMarketValue"], 150)
        self.assertEqual(pricing["lowEstimate"], 120)
        self.assertEqual(pricing["highEstimate"], 180)
        self.assertEqual(pricing["currency"], "AUD")
        self.assertEqual(pricing["displayString"], "AUD $150.00")
        self.assertEqual(pricing["originalMarketPayload"]["price"], 100)
        self.assertEqual(pricing["originalMarketPayload"]["currency"], "USD")
        self.assertEqual(pricing["originalMarketPayload"]["displayCurrency"], "AUD")
        self.assertEqual(pricing["originalMarketPayload"]["exchangeRateUsed"], 1.5)
        self.assertEqual(pricing["comparableSales"][0]["soldPrice"], 120)
        self.assertEqual(pricing["comparableSales"][0]["currency"], "AUD")

    def test_reprice_rejects_missing_title(self) -> None:
        payload = _payload()
        payload["identity"]["title"] = " "

        response = self.client.post("/api/pricing/reprice", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_reprice_identity")


class _FakePricingProvider:
    provider_name = "fake"

    def price(self, recognition) -> PricingResult:
        self.last_recognition = recognition
        return PricingResult(
            estimatedMarketValue=420,
            lowEstimate=390,
            highEstimate=450,
            currency="AUD",
            pricingSource="PriceCharting",
            pricingConfidence=86,
            lastUpdated=utc_timestamp(),
            valuationStatus="market_estimated",
            valuationSource="PriceCharting",
            marketTrend="Stable",
            sourceCount=1,
            pricingAge="fresh",
            comparableSales=[
                _sale("Loose guide", 390),
                _sale("Graded guide", 450),
            ],
            providerDiagnostics={
                "providers": "PriceCharting",
                "providerCount": "1",
                "comparableCount": "2",
                "valuationStrategy": "catalog_guide",
                "attributionText": "Pricing data powered by PriceCharting",
                "priceExplanation": "Matched by title, set, number and condition.",
                "originalPrice": "280",
                "originalCurrency": "USD",
                "exchangeRateUsed": "1.5",
            },
        )


class _FailingPricingProvider:
    provider_name = "failing"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def price(self, recognition):
        raise self._error


class _UsdPricingProvider:
    provider_name = "usd"

    def price(self, recognition) -> PricingResult:
        return PricingResult(
            estimatedMarketValue=100,
            lowEstimate=80,
            highEstimate=120,
            currency="USD",
            pricingSource="PriceCharting API",
            pricingConfidence=80,
            lastUpdated=utc_timestamp(),
            valuationStatus="market_estimated",
            valuationSource="PriceCharting API",
            marketTrend="Stable",
            sourceCount=1,
            pricingAge="fresh",
            comparableSales=[
                MarketComparableSale(
                    source="PriceCharting API",
                    title="Charizard guide",
                    soldPrice=80,
                    currency="USD",
                    soldDate="2026-07-26T00:00:00Z",
                    condition="Ungraded",
                ),
                MarketComparableSale(
                    source="PriceCharting API",
                    title="Charizard graded",
                    soldPrice=120,
                    currency="USD",
                    soldDate="2026-07-26T00:00:00Z",
                    condition="Graded",
                ),
            ],
            providerDiagnostics={
                "providers": "PriceCharting API",
                "providerCount": "1",
                "comparableCount": "2",
                "valuationStrategy": "catalog_guide",
                "priceExplanation": "Matched by corrected identity.",
            },
        )


def _sale(title: str, price: int) -> MarketComparableSale:
    return MarketComparableSale(
        source="PriceCharting",
        title=title,
        soldPrice=price,
        currency="AUD",
        soldDate="2026-07-26T00:00:00Z",
        condition="Near Mint",
    )


def _payload() -> dict:
    return {
        "itemId": "portfolio-1",
        "previousValue": 300,
        "previousCurrency": "AUD",
        "correctionSource": "manual",
        "identity": {
            "title": "1999 Pokemon Charizard Holo",
            "category": "Pokemon Card",
            "brand": "Pokemon",
            "setName": "Base Set",
            "cardNumber": "4/102",
            "condition": "Near Mint",
            "year": "1999",
        },
    }


if __name__ == "__main__":
    unittest.main()
