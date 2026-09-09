"""Catalog detail takes its current cents from pricecharting_current_price.

The catalog's own *_price_cents columns stop being maintained in PR 4. Until
the read path stops trusting them, that switch would silently serve prices
frozen at whatever the last catalog write happened to leave behind.

These tests are written against request URLs rather than returned values on
purpose. Every existing handler in test_catalog_search.py answers an
unrecognised URL with the catalog stub, so a read that never asks
pricecharting_current_price still gets cents back and still looks correct --
the same trap that let 39 mock handlers in #212 go green while testing nothing.
"""

from __future__ import annotations

import unittest

import httpx

from app.services.pricing.catalog_search_service import (
    CURRENT_PRICE_CENTS_COLUMNS,
    CatalogSearchService,
)

CATALOG_ROW = {
    "pricecharting_id": "999",
    "product_name": "Charizard #4 Base Set",
    "console_name": "Pokemon Cards",
    "category": "Pokemon Cards",
    "upc": "",
    # Stale on purpose: these are what the catalog would still be holding
    # after PR 4 freezes them.
    "loose_price_cents": 11100,
    "cib_price_cents": 11200,
    "new_price_cents": None,
    "graded_price_cents": 11300,
    "box_only_price_cents": None,
    "manual_only_price_cents": None,
    "currency": "USD",
    "product_url": "https://www.pricecharting.com/game/pokemon/charizard",
    "source_file": "pokemon.csv",
    "source_downloaded_at": "2026-09-09T00:00:00Z",
    "updated_at": "2026-09-09T00:00:00Z",
    "normalized_identity": "charizard 4 base set pokemon cards",
}
CURRENT_PRICE_ROW = {
    "loose_price_cents": 16100,
    "cib_price_cents": 20000,
    "new_price_cents": None,
    "graded_price_cents": 80000,
    "box_only_price_cents": None,
    "manual_only_price_cents": None,
    "currency": "USD",
}


def _service(current_price_rows=(CURRENT_PRICE_ROW,), seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if path.endswith("/pricecharting_current_price"):
            return httpx.Response(200, json=list(current_price_rows))
        if path.endswith("/pricecharting_price_history"):
            return httpx.Response(200, json=[])
        if path.endswith("/pricecharting_catalog"):
            return httpx.Response(200, json=[CATALOG_ROW])
        return httpx.Response(404, json={"message": f"unstubbed: {path}"})

    return CatalogSearchService(
        supabase_url="https://example.supabase.co",
        service_role_key="service-role",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class ItAsksTheCurrentPriceTableTest(unittest.TestCase):
    def test_the_request_is_actually_made(self) -> None:
        seen: list[str] = []
        _service(seen=seen)._fetch_catalog_row("999")
        self.assertTrue(
            any(path.endswith("/pricecharting_current_price") for path in seen),
            f"current price was never requested; paths asked: {seen}")

    def test_it_asks_for_the_one_id(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/pricecharting_current_price"):
                return httpx.Response(200, json=[CURRENT_PRICE_ROW])
            return httpx.Response(200, json=[CATALOG_ROW])

        CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )._fetch_catalog_row("999")
        price_request = next(r for r in captured
                             if r.url.path.endswith("/pricecharting_current_price"))
        self.assertEqual(price_request.url.params.get("pricecharting_id"), "eq.999")
        self.assertEqual(price_request.url.params.get("limit"), "1")

    def test_it_selects_every_column_it_overlays(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/pricecharting_current_price"):
                return httpx.Response(200, json=[CURRENT_PRICE_ROW])
            return httpx.Response(200, json=[CATALOG_ROW])

        CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )._fetch_catalog_row("999")
        selected = next(r for r in captured
                        if r.url.path.endswith("/pricecharting_current_price")
                        ).url.params["select"].split(",")
        for column in (*CURRENT_PRICE_CENTS_COLUMNS, "currency"):
            with self.subTest(column=column):
                self.assertIn(column, selected)


class TheCurrentPriceWinsTest(unittest.TestCase):
    """The catalog's cents are deliberately different in the fixture."""

    def test_cents_come_from_the_current_price_row(self) -> None:
        row = _service()._fetch_catalog_row("999")
        self.assertEqual(row["loose_price_cents"], 16100)
        self.assertEqual(row["cib_price_cents"], 20000)
        self.assertEqual(row["graded_price_cents"], 80000)

    def test_the_stale_catalog_cents_do_not_survive(self) -> None:
        row = _service()._fetch_catalog_row("999")
        for column in CURRENT_PRICE_CENTS_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(row[column], CURRENT_PRICE_ROW[column])

    def test_everything_that_is_not_a_price_still_comes_from_the_catalog(self) -> None:
        row = _service()._fetch_catalog_row("999")
        self.assertEqual(row["product_name"], "Charizard #4 Base Set")
        self.assertEqual(row["normalized_identity"], "charizard 4 base set pokemon cards")
        self.assertEqual(row["source_file"], "pokemon.csv")
        self.assertEqual(row["product_url"], CATALOG_ROW["product_url"])


class AMissingCurrentPriceRowTest(unittest.TestCase):
    """Null prices, never an error, and never the catalog's frozen ones.

    Serving a silently stale price is worse than serving none -- the whole
    reason the overlay applies even on a miss.
    """

    def test_it_does_not_raise(self) -> None:
        self.assertIsNotNone(_service(current_price_rows=())._fetch_catalog_row("999"))

    def test_prices_come_back_null(self) -> None:
        row = _service(current_price_rows=())._fetch_catalog_row("999")
        for column in CURRENT_PRICE_CENTS_COLUMNS:
            with self.subTest(column=column):
                self.assertIsNone(row[column])

    def test_the_frozen_catalog_price_is_not_used_as_a_fallback(self) -> None:
        row = _service(current_price_rows=())._fetch_catalog_row("999")
        self.assertNotEqual(row["loose_price_cents"], 11100)

    def test_currency_still_has_a_value(self) -> None:
        self.assertEqual(_service(current_price_rows=())._fetch_catalog_row("999")["currency"],
                         "USD")

    def test_a_non_list_payload_is_survivable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pricecharting_current_price"):
                return httpx.Response(200, json={"error": "boom"})
            return httpx.Response(200, json=[CATALOG_ROW])

        row = CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )._fetch_catalog_row("999")
        self.assertIsNone(row["loose_price_cents"])

    def test_a_missing_catalog_row_is_still_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        self.assertIsNone(CatalogSearchService(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )._fetch_catalog_row("999"))


if __name__ == "__main__":
    unittest.main()
