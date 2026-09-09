"""The admin catalog console must not rank by a frozen price.

PR 4 stops the writers maintaining pricecharting_catalog's cents. Anything
still sorting or filtering on that column then ranks by whatever it froze at --
the console looks right and is wrong, which is worse than an error.

Measured before writing this (EXPLAIN, no ANALYZE, so it cost nothing): the
catalog's loose_price_cents index was never serving this query anyway --
`DESC NULLS LAST` does not match a plain ascending index, so today's plan is
already Seq Scan + Sort at cost 2,465,755. The same query against
pricecharting_current_price costs 524,094, because that table is 2 GB rather
than 25 GB. Moving is 4.7x cheaper, not a trade.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.admin_catalog_service import (
    SupabaseAdminCatalogRepository,
    _price_drives_the_query,
)

CATALOG_ROW = {
    "pricecharting_id": "1",
    "product_name": "Charizard",
    "console_name": "Pokemon Cards",
    "loose_price_cents": 11100,          # frozen; must never be displayed
    "cib_price_cents": 11200,
    "new_price_cents": None,
    "graded_price_cents": None,
    "box_only_price_cents": None,
    "manual_only_price_cents": None,
    "currency": "USD",
}
CURRENT_PRICE_ROW = {
    "pricecharting_id": "1",
    "loose_price_cents": 16100,
    "cib_price_cents": 20000,
    "new_price_cents": None,
    "graded_price_cents": None,
    "box_only_price_cents": None,
    "manual_only_price_cents": None,
    "currency": "USD",
}


class _Service(SupabaseAdminCatalogRepository):
    def __init__(self, price_rows=(CURRENT_PRICE_ROW,), catalog_rows=(CATALOG_ROW,)):
        self.requests: list[tuple[str, dict]] = []
        self._price_rows = list(price_rows)
        self._catalog_rows = list(catalog_rows)

    def _request(self, method, path, params=None, **kwargs):
        self.requests.append((path, params or {}))
        if path.endswith("/pricecharting_current_price"):
            return self._price_rows
        return self._catalog_rows

    def _paths(self) -> list[str]:
        return [path for path, _ in self.requests]


class WhichTableLeadsTest(unittest.TestCase):
    def test_a_price_sort_is_driven_from_current_price(self) -> None:
        service = _Service()
        service.list_catalog_rows(
            source="pricecharting", limit=50, offset=0, sort="price_desc")
        self.assertTrue(service._paths()[0].endswith("/pricecharting_current_price"),
                        f"the price sort did not lead with current_price: {service._paths()}")

    def test_a_price_filter_is_driven_from_current_price(self) -> None:
        service = _Service()
        service.list_catalog_rows(
            source="pricecharting", limit=50, offset=0, min_price=10.0)
        self.assertTrue(service._paths()[0].endswith("/pricecharting_current_price"))

    def test_an_ordinary_listing_still_leads_with_the_catalog(self) -> None:
        """The catalog carries the identity columns the console shows."""
        service = _Service()
        service.list_catalog_rows(source="pricecharting", limit=50, offset=0)
        self.assertTrue(service._paths()[0].endswith("/pricecharting_catalog"))

    def test_kicksdb_is_untouched(self) -> None:
        """Sneakers have their own table and their own avg_price_cents index."""
        service = _Service(catalog_rows=[{"slug": "x", "avg_price_cents": 1}])
        service.list_catalog_rows(
            source="kicksdb", limit=50, offset=0, sort="price_desc")
        self.assertTrue(all("pricecharting" not in path for path in service._paths()),
                        service._paths())

    def test_the_helper_says_when_price_drives(self) -> None:
        self.assertTrue(_price_drives_the_query(sort="price_asc", min_price=None, max_price=None))
        self.assertTrue(_price_drives_the_query(sort="price_desc", min_price=None, max_price=None))
        self.assertTrue(_price_drives_the_query(sort=None, min_price=1.0, max_price=None))
        self.assertTrue(_price_drives_the_query(sort=None, min_price=None, max_price=9.0))
        self.assertFalse(_price_drives_the_query(sort=None, min_price=None, max_price=None))
        self.assertFalse(_price_drives_the_query(sort="", min_price=None, max_price=None))


class TheDisplayedPriceIsTheCurrentOneTest(unittest.TestCase):
    """The catalog fixture's cents differ deliberately, so a no-op overlay fails."""

    def test_an_ordinary_listing_still_shows_current_cents(self) -> None:
        rows = _Service().list_catalog_rows(
            source="pricecharting", limit=50, offset=0)
        self.assertEqual(rows[0]["loose_price_cents"], 16100)
        self.assertEqual(rows[0]["cib_price_cents"], 20000)

    def test_the_frozen_catalog_cents_never_reach_the_console(self) -> None:
        rows = _Service().list_catalog_rows(
            source="pricecharting", limit=50, offset=0)
        self.assertNotEqual(rows[0]["loose_price_cents"], 11100)

    def test_a_missing_current_price_row_shows_null_not_a_stale_number(self) -> None:
        rows = _Service(price_rows=[]).list_catalog_rows(
            source="pricecharting", limit=50, offset=0)
        self.assertIsNone(rows[0]["loose_price_cents"])
        self.assertEqual(rows[0]["currency"], "USD")

    def test_identity_columns_still_come_from_the_catalog(self) -> None:
        rows = _Service().list_catalog_rows(
            source="pricecharting", limit=50, offset=0)
        self.assertEqual(rows[0]["product_name"], "Charizard")
        self.assertEqual(rows[0]["console_name"], "Pokemon Cards")


class ThePriceOrderedPageKeepsItsOrderTest(unittest.TestCase):
    """A PostgREST `in.()` hydration returns rows in whatever order it likes,
    and the page was ordered by price on purpose."""

    def test_the_hydrated_page_follows_the_price_order(self) -> None:
        price_rows = [{"pricecharting_id": "3"}, {"pricecharting_id": "1"},
                      {"pricecharting_id": "2"}]
        catalog_rows = [{"pricecharting_id": "1"}, {"pricecharting_id": "2"},
                        {"pricecharting_id": "3"}]
        service = _Service(price_rows=price_rows, catalog_rows=catalog_rows)
        hydrated = service._hydrate_catalog_rows(price_rows)
        self.assertEqual([row["pricecharting_id"] for row in hydrated], ["3", "1", "2"])

    def test_an_id_with_no_catalog_row_is_dropped_not_faked(self) -> None:
        service = _Service(price_rows=[{"pricecharting_id": "9"}], catalog_rows=[])
        self.assertEqual(service._hydrate_catalog_rows([{"pricecharting_id": "9"}]), [])

    def test_an_empty_page_makes_no_request(self) -> None:
        service = _Service()
        self.assertEqual(service._hydrate_catalog_rows([]), [])
        self.assertEqual(service.requests, [])


if __name__ == "__main__":
    unittest.main()
