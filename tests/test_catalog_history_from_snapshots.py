"""The catalog price chart reads the table that was built for it.

pricecharting_price_history is append-only, 12 columns, 398 MB, indexed on
(pricecharting_id, observed_at) -- and until now it was written by the ingest
pipeline and read by no application code at all. The chart instead selected
twelve price columns out of pricecharting_catalog_history: 26 columns, 24 GB,
1,427 bytes per version against 188 per snapshot.

Both tables hold the same prices. Only one of them is shaped like a chart.

The response shape is deliberately unchanged, because two clients depend on
it: the admin portal reads `pricing.marketValue` and `validFrom ||
sourceDownloadedAt`, and the mobile app charts `validFrom` and merges
consecutive same-price points assuming newest-first ordering. Several tests
below exist only to pin that contract.
"""

import unittest
from unittest.mock import patch

from app.services.pricing.catalog_search_service import (
    CatalogSearchService,
    _history_row_to_point,
)

SNAPSHOTS = [
    {"observed_at": "2026-09-08T00:00:00+00:00", "source_file": "sportscardspro-tier3-refresh",
     "currency": "USD", "loose_price_cents": 1500, "cib_price_cents": 1800,
     "new_price_cents": None, "graded_price_cents": 5000,
     "box_only_price_cents": None, "manual_only_price_cents": None},
    {"observed_at": "2026-09-07T00:00:00+00:00", "source_file": "sportscardspro-tier3-refresh",
     "currency": "USD", "loose_price_cents": 1450, "cib_price_cents": 1750,
     "new_price_cents": None, "graded_price_cents": 4900,
     "box_only_price_cents": None, "manual_only_price_cents": None},
    {"observed_at": "2026-09-06T00:00:00+00:00", "source_file": "sportscardspro-tier3-refresh",
     "currency": "USD", "loose_price_cents": 1400, "cib_price_cents": None,
     "new_price_cents": None, "graded_price_cents": None,
     "box_only_price_cents": None, "manual_only_price_cents": None},
]


class _Service(CatalogSearchService):
    """Captures the request instead of making it."""

    def __init__(self, payload):
        self.requests = []
        self._payload = payload

    def _request(self, method, path, params=None, **kwargs):
        self.requests.append((method, path, params or {}))
        return self._payload


def params_of(service) -> list[str]:
    _, _, params = service.requests[0]
    return [column.strip() for column in params["select"].split(",")]


class ItReadsTheSnapshotTableTest(unittest.TestCase):
    def test_the_query_targets_price_history_not_catalog_history(self) -> None:
        service = _Service(SNAPSHOTS)
        service._fetch_history_rows("12345", 90)
        _, path, _ = service.requests[0]
        self.assertIn("pricecharting_price_history", path)
        self.assertNotIn("pricecharting_catalog_history", path)

    def test_it_orders_newest_first(self) -> None:
        """The mobile client's same-price merge assumes newest-first."""
        service = _Service(SNAPSHOTS)
        service._fetch_history_rows("12345", 90)
        _, _, params = service.requests[0]
        self.assertEqual(params["order"], "observed_at.desc")

    def test_it_filters_to_the_requested_item_and_honours_the_limit(self) -> None:
        service = _Service(SNAPSHOTS)
        service._fetch_history_rows("12345", 30)
        _, _, params = service.requests[0]
        self.assertEqual(params["pricecharting_id"], "eq.12345")
        self.assertEqual(params["limit"], "30")

    def test_it_selects_the_column_the_whole_chart_hangs_on(self) -> None:
        """observed_at becomes validFrom AND sourceDownloadedAt. Dropping it
        from the select leaves every point undated, and the mapper does not
        raise -- it just returns nulls, so nothing here would notice."""
        service = _Service(SNAPSHOTS)
        service._fetch_history_rows("12345", 90)
        _, _, params = service.requests[0]
        self.assertIn("observed_at", params["select"].split(","))

    def test_it_selects_every_price_column_the_mapper_reads(self) -> None:
        service = _Service(SNAPSHOTS)
        service._fetch_history_rows("12345", 90)
        selected = set(params_of(service))
        for column in ("loose_price_cents", "cib_price_cents", "new_price_cents",
                       "graded_price_cents", "box_only_price_cents",
                       "manual_only_price_cents", "currency", "source_file"):
            with self.subTest(column=column):
                self.assertIn(column, selected)

    def test_it_selects_only_columns_the_snapshot_table_has(self) -> None:
        """A column the table lacks would 400 at runtime, not at import."""
        service = _Service(SNAPSHOTS)
        service._fetch_history_rows("12345", 90)
        _, _, params = service.requests[0]
        available = {
            "price_history_id", "pricecharting_id", "observed_at",
            "loose_price_cents", "cib_price_cents", "new_price_cents",
            "graded_price_cents", "box_only_price_cents", "manual_only_price_cents",
            "currency", "source_file", "recorded_at",
        }
        for column in params["select"].split(","):
            with self.subTest(column=column):
                self.assertIn(column.strip(), available)


class TheResponseShapeIsUnchangedTest(unittest.TestCase):
    """Two clients depend on this contract; it must not shift underneath."""

    def _rows(self):
        return _Service(SNAPSHOTS)._fetch_history_rows("12345", 90)

    def test_every_field_the_point_mapper_reads_is_present(self) -> None:
        for row in self._rows():
            with self.subTest(row=row["valid_from"]):
                for field in ("valid_from", "valid_to", "is_current", "source_file",
                              "source_downloaded_at", "currency"):
                    self.assertIn(field, row)

    def test_valid_from_carries_the_observation_time(self) -> None:
        """The portal charts `validFrom || sourceDownloadedAt`."""
        rows = self._rows()
        self.assertEqual(rows[0]["valid_from"], "2026-09-08T00:00:00+00:00")
        self.assertEqual(rows[0]["source_downloaded_at"], "2026-09-08T00:00:00+00:00")

    def test_only_the_newest_point_is_current(self) -> None:
        rows = self._rows()
        self.assertTrue(rows[0]["is_current"])
        self.assertFalse(rows[1]["is_current"])
        self.assertFalse(rows[2]["is_current"])

    def test_valid_to_is_null_because_a_point_has_no_window(self) -> None:
        self.assertTrue(all(row["valid_to"] is None for row in self._rows()))

    def test_the_point_mapper_still_works_unchanged(self) -> None:
        """The mapper was not touched; this proves it did not need to be."""
        points = [_history_row_to_point(row) for row in self._rows()]
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].validFrom, "2026-09-08T00:00:00+00:00")
        self.assertTrue(points[0].isCurrent)
        self.assertIsNone(points[0].validTo)

    def test_prices_survive_the_mapping(self) -> None:
        points = [_history_row_to_point(row) for row in self._rows()]
        values = [p.pricing.marketValue for p in points]
        self.assertEqual(values, [15.0, 14.5, 14.0])
        self.assertTrue(all(p.pricing.currency == "USD" for p in points))

    def test_a_missing_price_column_does_not_break_the_point(self) -> None:
        """The oldest fixture row has null cib/graded, as real rows do."""
        point = _history_row_to_point(self._rows()[2])
        self.assertEqual(point.pricing.marketValue, 14.0)

    def test_an_empty_history_returns_an_empty_list(self) -> None:
        self.assertEqual(_Service([])._fetch_history_rows("12345", 90), [])

    def test_a_non_list_payload_is_handled(self) -> None:
        self.assertEqual(_Service({"error": "x"})._fetch_history_rows("12345", 90), [])


class TheWindowIsNoLongerATradeOffTest(unittest.TestCase):
    """Switching tables used to mean losing history. It no longer does.

    The pipeline only wrote snapshots from 2026-08-28. Before the backfill,
    89 of 150 sampled items had price points in the SCD2 table with no
    snapshot -- item 3796419, for instance, had a 125 point on 2026-08-14 that
    would simply have vanished from its chart. That is why this switch was
    stopped the first time it was attempted.

    The backfill on 2026-09-09 closed the gap. These tests exist so nobody
    reintroduces the caveat, or the union that was rejected in favour of it.
    """

    def _source(self) -> str:
        import pathlib

        source = pathlib.Path(
            "app/services/pricing/catalog_search_service.py").read_text()
        body = source[source.index("def _fetch_history_rows"):]
        return body[: body.index("payload = self._request")]

    def test_the_source_records_that_the_backfill_closed_the_gap(self) -> None:
        body = self._source()
        self.assertIn("2026-08-28", body)
        self.assertIn("backfill", body.lower())

    def test_it_points_at_the_script_that_did_it(self) -> None:
        """The claim is checkable only if the reader can find the evidence."""
        self.assertIn("backfill_price_history_from_scd2", self._source())

    def test_the_backfilled_rows_are_ordinary_history(self) -> None:
        """They carry a distinguishing source_file but need no special case."""
        rows = _Service([{**SNAPSHOTS[0], "source_file": "backfill-from-scd2"}]
                        )._fetch_history_rows("12345", 90)
        point = _history_row_to_point(rows[0])
        self.assertEqual(point.pricing.marketValue, 15.0)
        self.assertTrue(point.isCurrent)


if __name__ == "__main__":
    unittest.main()
