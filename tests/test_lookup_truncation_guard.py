"""PostgREST caps a response at 1,000 rows and says nothing about it.

Measured live 2026-09-08. Raising the ingest write batch to 2,000 made
_fetch_current_history_rows ask about 2,000 ids; it received 1,000. No error,
no header, no partial-content status -- just a complete-looking answer with
half the rows missing. Those 1,000 ids therefore looked like they had NO
current history row, so the writer inserted a second "current" row beside the
existing one and hit 23505 on
pricecharting_catalog_history_current_unique_idx.

    asked for   500 ids -> got  500
    asked for 1,000 ids -> got 1000
    asked for 1,500 ids -> got 1000   <- 500 silently missing
    asked for 2,000 ids -> got 1000   <- 1000 silently missing

This is the same failure shape as the wrong-catalog CSV: a well-formed
response that is quietly incomplete. The lesson both times is that a
"successful" reply is not evidence of a complete one.

The guard is a hard failure, not a fallback, because the batch size is OURS
to choose -- asking for more than the cap is a programming error, and
degrading gracefully would just hide it again.
"""

import unittest
from unittest.mock import patch

import httpx

from scripts.import_pricecharting_catalog import (
    POSTGREST_MAX_LOOKUP_ROWS,
    SupabaseCatalogClient,
    _assert_lookup_complete,
    _assert_lookup_fits,
)


def _rows(n, start=0):
    return [{"pricecharting_id": str(start + i), "product_name": f"Item {i}"} for i in range(n)]


def _service() -> SupabaseCatalogClient:
    with patch("scripts.import_pricecharting_catalog._supabase_jwt_role",
               return_value="service_role"):
        return SupabaseCatalogClient(
            supabase_url="https://x.test", service_role_key="k", timeout_seconds=5)


class TheCapIsWhatWeMeasuredTest(unittest.TestCase):
    def test_the_constant_matches_the_observed_cap(self) -> None:
        self.assertEqual(POSTGREST_MAX_LOOKUP_ROWS, 1000)


class LookupSizeGuardTest(unittest.TestCase):
    def test_a_lookup_within_the_cap_is_allowed(self) -> None:
        _assert_lookup_fits([str(i) for i in range(POSTGREST_MAX_LOOKUP_ROWS)], what="x")

    def test_a_lookup_over_the_cap_raises_before_any_request(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            _assert_lookup_fits([str(i) for i in range(POSTGREST_MAX_LOOKUP_ROWS + 1)], what="x")
        message = str(caught.exception)
        self.assertIn("silently truncated", message)
        self.assertIn(str(POSTGREST_MAX_LOOKUP_ROWS), message)

    def test_the_exact_failing_size_is_rejected(self) -> None:
        """2,000 is the size that actually produced the 23505."""
        with self.assertRaises(SystemExit):
            _assert_lookup_fits([str(i) for i in range(2000)], what="catalog history")


class AmbiguousFullCapResponseTest(unittest.TestCase):
    """A cap-sized reply to a cap-sized request cannot be told apart from a
    truncated one, so it is refused rather than guessed at."""

    def test_a_full_cap_response_to_a_full_cap_request_raises(self) -> None:
        ids = [str(i) for i in range(POSTGREST_MAX_LOOKUP_ROWS)]
        with self.assertRaises(SystemExit) as caught:
            _assert_lookup_complete(ids, [{}] * POSTGREST_MAX_LOOKUP_ROWS, what="catalog history")
        self.assertIn("cannot tell a complete answer", str(caught.exception))

    def test_a_short_response_is_unambiguous_and_allowed(self) -> None:
        ids = [str(i) for i in range(POSTGREST_MAX_LOOKUP_ROWS)]
        _assert_lookup_complete(ids, [{}] * 999, what="catalog history")

    def test_a_small_request_is_never_ambiguous(self) -> None:
        _assert_lookup_complete([str(i) for i in range(10)], [{}] * 10, what="catalog history")


class TheWriteBatchCannotOverflowTheLookupTest(unittest.TestCase):
    """The write batch and the lookup batch are the same number, so a batch
    size over the cap must fail loudly rather than corrupt history."""

    def setUp(self) -> None:
        self.calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            return httpx.Response(200, json=[])

        self.transport = httpx.MockTransport(handler)

    def _client_patch(self):
        transport = self.transport
        real = httpx.Client
        return patch("scripts.import_pricecharting_catalog.httpx.Client",
                     side_effect=lambda *a, **kw: real(*a, transport=transport,
                                                       **{k: v for k, v in kw.items()}))

    def test_history_sync_refuses_a_batch_over_the_cap(self) -> None:
        service = _service()
        with self._client_patch():
            with self.assertRaises(ValueError) as caught:
                service.sync_scd2_history_rows(
                    _rows(POSTGREST_MAX_LOOKUP_ROWS + 500),
                    batch_size=POSTGREST_MAX_LOOKUP_ROWS + 500)
        # ValueError, not SystemExit: the per-sub-batch handler catches
        # SystemExit and reports "continuing", which would downgrade a
        # programming error into silently failed rows.
        self.assertIn("lookup cap", str(caught.exception))
        self.assertIn("23505", str(caught.exception))

    def test_hash_lookup_refuses_a_batch_over_the_cap(self) -> None:
        """Truncation here is waste, not corruption -- but the same cap."""
        service = _service()
        with self._client_patch():
            with self.assertRaises(ValueError) as caught:
                service.upsert_rows(_rows(1500), batch_size=1500)
        self.assertIn("lookup cap", str(caught.exception))

    def test_a_batch_AT_the_cap_is_also_rejected(self) -> None:
        """Found by running it: a 1,000-id request that finds 1,000 rows is
        indistinguishable from a truncated one, so the cap itself is unusable.
        The ceiling is strictly below it."""
        service = _service()
        with self._client_patch():
            with self.assertRaises(ValueError) as caught:
                service.upsert_rows(_rows(POSTGREST_MAX_LOOKUP_ROWS),
                                    batch_size=POSTGREST_MAX_LOOKUP_ROWS)
        self.assertIn("strictly smaller", str(caught.exception))

    def test_a_batch_just_under_the_cap_is_accepted(self) -> None:
        service = _service()
        with self._client_patch():
            service.upsert_rows(_rows(POSTGREST_MAX_LOOKUP_ROWS - 1),
                                batch_size=POSTGREST_MAX_LOOKUP_ROWS - 1)
        self.assertTrue(self.calls, "no request was made at the allowed size")


class TheIngesterStaysUnderTheCapTest(unittest.TestCase):
    def test_the_default_catalog_batch_size_is_within_the_cap(self) -> None:
        """This is the specific regression: the default was briefly 2,000."""
        from scripts.ingest_catalog_batch import parse_args

        self.assertLess(parse_args([]).catalog_batch_size, POSTGREST_MAX_LOOKUP_ROWS)

    def test_the_ingester_records_why_the_ceiling_exists(self) -> None:
        """A bare number invites someone to raise it again."""
        import pathlib

        source = pathlib.Path("scripts/ingest_catalog_batch.py").read_text()
        self.assertIn("1,000", source)
        self.assertIn("23505", source)


if __name__ == "__main__":
    unittest.main()
