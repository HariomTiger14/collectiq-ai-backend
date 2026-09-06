"""Currency safety in pricing aggregation.

Two separate concerns, both about not stating a price we cannot back:

1. A comparable sale with a missing currency must not end up unlabelled. The
   previous normalization, `(sale.currency or "AUD").strip().upper()`, looked
   like it defaulted -- but a whitespace-only currency is truthy, so the
   fallback never fired and the expression returned "".

2. Comparable sales from different providers must not be medianed together.
   Aggregation pools every provider's sales and takes a median of raw
   soldPrice values with no notion of currency, so the moment two providers
   quote different currencies that median is arithmetic on unlike units. This
   cannot happen today only because credential filtering leaves one provider
   per route; enabling eBay (genuinely AUD, EBAY_MARKETPLACE_ID=EBAY_AU)
   alongside PriceCharting or KicksDB (USD) would make it live.

Note what is deliberately NOT asserted here: that everything is USD. eBay is
an AUD source and stays one. The rule is "never guess, never mix", not
"assume USD".
"""

import unittest

from app.services.pricing.aggregation_service import (
    PricingAggregationService,
    _normalize_currency,
)
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    MarketComparableSale,
    PricingResult,
    utc_timestamp,
)


def _sale(price: int, currency, *, source: str = "eBay sold comps") -> MarketComparableSale:
    return MarketComparableSale(
        source=source,
        title="Charizard Holo",
        soldPrice=price,
        currency=currency,
        soldDate="2026-09-01T00:00:00Z",
        condition="Near Mint",
    )


def _result(sales: list[MarketComparableSale], *, currency: str = "USD") -> PricingResult:
    return PricingResult(
        estimatedMarketValue=max((s.soldPrice for s in sales), default=0),
        lowEstimate=min((s.soldPrice for s in sales), default=0),
        highEstimate=max((s.soldPrice for s in sales), default=0),
        currency=currency,
        pricingSource="test provider",
        pricingConfidence=80,
        lastUpdated=utc_timestamp(),
        valuationStatus="market_estimated",
        valuationSource="test",
        marketTrend="Stable",
        sourceCount=1,
        pricingAge="fresh",
        comparableSales=sales,
        fallbackUsed=False,
        cacheStatus="miss",
        providerDiagnostics={},
    )


class _StubProvider:
    def __init__(self, result: PricingResult, name: str = "stub"):
        self.provider_name = name
        self._result = result

    def price(self, recognition):  # noqa: ARG002 - signature match only
        return self._result


class _Recognition:
    title = "Charizard Holo"
    category = "Pokemon Card"
    brand = "Pokemon"
    estimatedValue = 0
    setName = ""
    cardNumber = ""
    condition = "Near Mint"
    edition = ""
    year = ""
    series = ""
    detectedObjects: list = []
    confidence = 90


class NormalizeCurrencyTest(unittest.TestCase):
    """The helper itself -- the whitespace case is the one that regressed."""

    def test_missing_values_default_to_usd(self) -> None:
        for raw in (None, "", "   ", "\t\n"):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_currency(raw), "USD")

    def test_values_are_stripped_and_uppercased(self) -> None:
        self.assertEqual(_normalize_currency("usd"), "USD")
        self.assertEqual(_normalize_currency(" aud "), "AUD")
        self.assertEqual(_normalize_currency("Gbp"), "GBP")

    def test_whitespace_only_does_not_produce_an_empty_string(self) -> None:
        """The exact defect: `("   " or "AUD").strip().upper()` returned ""."""
        self.assertNotEqual(_normalize_currency("   "), "")


class SaleNormalizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PricingAggregationService([])

    def test_sale_currencies_are_normalized(self) -> None:
        cases = {None: "USD", "": "USD", "   ": "USD", "usd": "USD", " aud ": "AUD"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                normalized = self.service._normalize_sales([_sale(100, raw)])
                self.assertEqual(len(normalized), 1)
                self.assertEqual(normalized[0].currency, expected)

    def test_no_normalized_sale_is_ever_unlabelled(self) -> None:
        normalized = self.service._normalize_sales(
            [_sale(100, "   "), _sale(120, None), _sale(140, "usd")]
        )
        self.assertEqual(len(normalized), 3)
        for sale in normalized:
            self.assertTrue(sale.currency, "a sale was left with no currency")


class MixedCurrencyGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PricingAggregationService([])

    def test_single_currency_comps_are_allowed(self) -> None:
        comps = self.service._normalize_sales([_sale(100, "USD"), _sale(120, "usd")])
        self.service._reject_mixed_currency_comps(comps)  # must not raise

    def test_all_aud_comps_are_allowed(self) -> None:
        """eBay-only results are legitimately AUD and must still price."""
        comps = self.service._normalize_sales([_sale(150, "AUD"), _sale(170, " aud ")])
        self.service._reject_mixed_currency_comps(comps)  # must not raise

    def test_empty_comps_are_allowed(self) -> None:
        self.service._reject_mixed_currency_comps([])  # must not raise

    def test_mixed_currencies_are_rejected(self) -> None:
        comps = self.service._normalize_sales([_sale(100, "USD"), _sale(150, "AUD")])
        with self.assertRaises(EmptyMarketDataError) as caught:
            self.service._reject_mixed_currency_comps(comps)
        message = str(caught.exception)
        self.assertIn("Mixed comparable sale currencies", message)
        self.assertIn("AUD", message)
        self.assertIn("USD", message)


class AggregateEndToEndTest(unittest.TestCase):
    """Through the real price() path, which is what actually protects users."""

    def test_two_providers_in_different_currencies_produce_no_price(self) -> None:
        usd_provider = _StubProvider(
            _result([_sale(100, "USD"), _sale(110, "USD")], currency="USD"), "pricecharting"
        )
        aud_provider = _StubProvider(
            _result([_sale(150, "AUD"), _sale(160, "AUD")], currency="AUD"), "ebay"
        )
        service = PricingAggregationService([usd_provider, aud_provider])

        with self.assertRaises(EmptyMarketDataError):
            service.price(_Recognition())

    def test_two_providers_in_the_same_currency_aggregate_normally(self) -> None:
        first = _StubProvider(_result([_sale(100, "USD")], currency="USD"), "pricecharting")
        second = _StubProvider(_result([_sale(120, "usd")], currency="USD"), "tcgplayer")
        service = PricingAggregationService([first, second])

        result = service.price(_Recognition())

        self.assertEqual(result.valuationStatus, "market_estimated")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(len(result.comparableSales), 2)

    def test_single_aud_provider_still_prices_in_aud(self) -> None:
        """The guard must not turn a legitimate AUD-only result into no price."""
        provider = _StubProvider(
            _result([_sale(150, "AUD"), _sale(170, "AUD")], currency="AUD"), "ebay"
        )
        service = PricingAggregationService([provider])

        result = service.price(_Recognition())

        self.assertEqual(result.valuationStatus, "market_estimated")
        self.assertEqual(result.currency, "AUD")

    def test_aggregate_currency_falls_back_to_usd_when_a_provider_omits_it(self) -> None:
        """Defensive: not reachable today, since currency is a required field."""
        provider = _StubProvider(_result([_sale(100, "USD")], currency="   "), "odd")
        service = PricingAggregationService([provider])

        result = service.price(_Recognition())

        self.assertEqual(result.currency, "USD")


if __name__ == "__main__":
    unittest.main()
