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


class AnAdminEditReachesTheBrowseKeysTest(unittest.TestCase):
    """Bullet 2, console half -- and the half that was missed first time.

    The ingest writer rewrites category/platform_group on current_price
    whenever a CSV rename arrives. An admin edit never goes through ingest, so
    without this an item recategorised in the console browses under its old
    category until its price happens to move -- which for a stable item may be
    never. The failure is invisible: the console shows the new category and
    Discover shows the old one.
    """

    class _Repo(SupabaseAdminCatalogRepository):
        _table_name = "pricecharting_catalog"

        def __init__(self, updated_row):
            self.calls: list[tuple[str, str, dict]] = []
            self._updated_row = updated_row

        @property
        def is_configured(self) -> bool:
            return True

        def _request(self, method, path, params=None, json_payload=None, **kwargs):
            self.calls.append((method, path, json_payload or {}))
            return [self._updated_row]

    def _service(self, updated_row):
        from app.services.admin_catalog_service import AdminCatalogService

        service = AdminCatalogService.__new__(AdminCatalogService)
        service._repository = self._Repo(updated_row)
        return service, service._repository

    def _patches_to_current_price(self, repo):
        return [(method, payload) for method, path, payload in repo.calls
                if path.endswith("/pricecharting_current_price")]

    def test_a_category_edit_reaches_current_price(self) -> None:
        service, repo = self._service(
            {"pricecharting_id": "1", "category": "Baseball Cards",
             "platform_group": None})
        service.update_item("1", {"category": "Baseball Cards"})
        patches = self._patches_to_current_price(repo)
        self.assertEqual(len(patches), 1, f"browse keys never synced: {repo.calls}")
        self.assertEqual(patches[0][0], "PATCH")
        self.assertEqual(patches[0][1]["category"], "Baseball Cards")

    def test_platform_group_is_not_an_editable_admin_field(self) -> None:
        """Recorded rather than fixed.

        _catalog_update_payload accepts title/category/console/upc/productUrl/
        note/active -- platform_group is not among them, so the console cannot
        set it directly. Editing `console` does not recompute it either, on the
        catalog or here; that gap predates this change and is not made worse by
        it. If platform_group ever becomes editable, it is already in
        BROWSE_KEY_COLUMNS and will sync without further work.
        """
        from app.services.admin_catalog_service import _catalog_update_payload

        self.assertEqual(_catalog_update_payload({"platform_group": "nintendo"}), {})
        self.assertEqual(_catalog_update_payload({"console": "Nintendo 64"}),
                         {"console_name": "Nintendo 64"})

    def test_an_edit_that_touches_no_browse_key_syncs_nothing(self) -> None:
        """An admin note is not a browse key; a write per edit is waste."""
        # The row carries category because the real PATCH uses select=* and
        # gets the whole row back. An earlier fixture omitted it, which let a
        # "sync on every edit" mutation pass -- the sync found nothing to write
        # for the wrong reason.
        service, repo = self._service(
            {"pricecharting_id": "1", "admin_note": "hi", "category": "Coins",
             "platform_group": None})
        service.update_item("1", {"note": "hi"})
        self.assertEqual(self._patches_to_current_price(repo), [],
                         "an admin-note edit wrote to current_price")

    def test_the_sync_never_restates_a_price(self) -> None:
        """A metadata edit has no business rewriting cents it did not change."""
        service, repo = self._service(
            {"pricecharting_id": "1", "category": "Coins",
             "loose_price_cents": 999, "currency": "USD"})
        service.update_item("1", {"category": "Coins"})
        payload = self._patches_to_current_price(repo)[0][1]
        self.assertEqual(set(payload), {"category"})
        self.assertNotIn("loose_price_cents", payload)

    def test_the_mirrored_columns_match_the_ingest_writer(self) -> None:
        """Two places mirror these keys; drift means one of them goes stale."""
        from app.services.admin_catalog_service import BROWSE_KEY_COLUMNS as admin_keys
        from scripts.import_pricecharting_catalog import BROWSE_KEY_COLUMNS as ingest_keys

        self.assertEqual(tuple(admin_keys), tuple(ingest_keys))
