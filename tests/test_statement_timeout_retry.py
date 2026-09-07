"""A statement timeout must cost a retry, not the rows.

Measured on 2026-09-07, before this: one `small-sets-refresh` run reported

    catalogRowsWritten: 1,445
    catalogRowsFailed:    300     <- three 57014s
    catalogRowsSkippedUnchanged: 4,325

so 17% of what that run attempted to write was dropped, silently, and the
same happened on every affected run. A timed-out sub-batch was marked failed
and abandoned; nothing retried it.

Retrying is the right lever because the timeouts are NOT a capacity wall.
Across 176 recorded events every single one fired between 8.15s and 8.67s --
the PostgREST `authenticator` role's fixed 8s statement_timeout -- at batch
sizes 100, 200 AND 1000, on batches as small as 5, 6, 8 and 13 rows, while
the mean upsert is 88-153ms. That is a latency tail from contention, not
volume, so the same rows usually write a moment later.

Batch halving rides along because it is nearly free, not because size is the
problem. These tests pin that distinction: recovery must work for a
single-row batch, where there is nothing left to halve.
"""

import unittest
from unittest.mock import patch

import httpx

from scripts.import_pricecharting_catalog import (
    MAX_TIMEOUT_ATTEMPTS,
    SupabaseCatalogClient,
)


class _Timeout:
    status_code = 500
    text = '{"code":"57014","message":"canceling statement due to statement timeout"}'

    def raise_for_status(self):
        raise httpx.HTTPStatusError("timeout", request=None, response=self)


class _NotATimeout:
    status_code = 400
    text = '{"code":"23514","message":"violates check constraint"}'

    def raise_for_status(self):
        raise httpx.HTTPStatusError("bad request", request=None, response=self)


class _Ok:
    status_code = 200
    text = "[]"

    def raise_for_status(self):
        return None


class _Client:
    """Records every POST and replays a scripted sequence of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts: list[list[dict]] = []

    def post(self, url, **kwargs):
        self.posts.append(kwargs.get("json", []))
        if self._responses:
            return self._responses.pop(0)
        return _Ok()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client_with(responses):
    fake = _Client(responses)
    return fake, patch(
        "scripts.import_pricecharting_catalog.httpx.Client", return_value=fake
    )


def _rows(n):
    return [{"pricecharting_id": str(i), "product_name": f"Item {i}"} for i in range(n)]


def _service() -> SupabaseCatalogClient:
    with patch(
        "scripts.import_pricecharting_catalog._supabase_jwt_role", return_value="service_role"
    ):
        return SupabaseCatalogClient(
            supabase_url="https://x.test", service_role_key="k", timeout_seconds=5
        )


class TimeoutIsRetriedTest(unittest.TestCase):
    def setUp(self):
        patcher = patch("scripts.import_pricecharting_catalog.time.sleep")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_a_transient_timeout_recovers_every_row(self) -> None:
        """The 300-lost-rows case: one blip, nothing lost."""
        fake, ctx = _client_with([_Timeout()])
        service = _service()
        with ctx:
            written = service._upsert(
                table="pricecharting_catalog", rows=_rows(10), batch_size=10,
                on_conflict="pricecharting_id", label="catalog",
            )
        self.assertEqual(written, 10, "rows were dropped instead of retried")
        self.assertEqual(service.timeout_retry_stats["timeouts"], 1)
        self.assertGreaterEqual(service.timeout_retry_stats["retries"], 1)
        self.assertEqual(service.timeout_retry_stats["rowsAbandoned"], 0)

    def test_a_single_row_batch_recovers_too(self) -> None:
        """Nothing to halve -- proves the retry, not the split, is the lever."""
        fake, ctx = _client_with([_Timeout()])
        service = _service()
        with ctx:
            written = service._upsert(
                table="pricecharting_catalog", rows=_rows(1), batch_size=1,
                on_conflict="pricecharting_id", label="catalog",
            )
        self.assertEqual(written, 1)

    def test_the_batch_is_halved_on_retry(self) -> None:
        fake, ctx = _client_with([_Timeout()])
        service = _service()
        with ctx:
            service._upsert(
                table="pricecharting_catalog", rows=_rows(10), batch_size=10,
                on_conflict="pricecharting_id", label="catalog",
            )
        self.assertEqual(len(fake.posts[0]), 10, "first attempt sends the whole batch")
        self.assertEqual(
            sorted(len(post) for post in fake.posts[1:]), [5, 5],
            "the retry should split the batch in half",
        )

    def test_a_persistent_timeout_still_gives_up(self) -> None:
        """Bounded: retrying a contended write forever adds to the contention."""
        fake, ctx = _client_with([_Timeout() for _ in range(40)])
        service = _service()
        with ctx, patch(
            "scripts.import_pricecharting_catalog.record_db_failure"
        ) as record:
            with self.assertRaises(SystemExit):
                service._upsert(
                    table="pricecharting_catalog", rows=_rows(8), batch_size=8,
                    on_conflict="pricecharting_id", label="catalog",
                )
        self.assertGreater(service.timeout_retry_stats["rowsAbandoned"], 0)
        record.assert_called_once()

    def test_the_ledger_records_the_callers_batch_size_not_a_fragment(self) -> None:
        """Halving must not corrupt the metric the diagnosis rests on.

        ops_error_events' rowCount is what showed that 5- and 9-row batches
        time out exactly like 1000-row ones. If a 39-row batch were recorded
        as its last 9-row fragment, that signal would quietly degrade.
        """
        fake, ctx = _client_with([_Timeout() for _ in range(40)])
        service = _service()
        with ctx, patch(
            "scripts.import_pricecharting_catalog.record_db_failure"
        ) as record:
            with self.assertRaises(SystemExit):
                service._upsert(
                    table="pricecharting_catalog", rows=_rows(39), batch_size=39,
                    on_conflict="pricecharting_id", label="catalog",
                )
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["row_count"], 39)
        self.assertEqual(kwargs["operation"], "catalog_upsert")
        self.assertTrue(kwargs["context"]["statementTimeout"])

    def test_attempts_are_bounded(self) -> None:
        fake, ctx = _client_with([_Timeout() for _ in range(200)])
        service = _service()
        with ctx, patch("scripts.import_pricecharting_catalog.record_db_failure"):
            with self.assertRaises(SystemExit):
                service._upsert(
                    table="pricecharting_catalog", rows=_rows(2), batch_size=2,
                    on_conflict="pricecharting_id", label="catalog",
                )
        # 1 initial + the halves it fans out to, capped by MAX_TIMEOUT_ATTEMPTS.
        self.assertLessEqual(len(fake.posts), 2 ** MAX_TIMEOUT_ATTEMPTS)


class NonTimeoutErrorsAreUnchangedTest(unittest.TestCase):
    """Only 57014 is retried. A constraint violation is not transient, and
    retrying it would turn one bad row into several identical failures."""

    def test_a_constraint_violation_is_not_retried(self) -> None:
        fake, ctx = _client_with([_NotATimeout()])
        service = _service()
        with ctx, patch(
            "scripts.import_pricecharting_catalog.record_db_failure"
        ) as record:
            with self.assertRaises(SystemExit):
                service._upsert(
                    table="pricecharting_catalog", rows=_rows(4), batch_size=4,
                    on_conflict="pricecharting_id", label="catalog",
                )
        self.assertEqual(len(fake.posts), 1, "a non-timeout error must not be retried")
        record.assert_called_once()
        self.assertFalse(record.call_args.kwargs["context"]["statementTimeout"])
        self.assertEqual(service.timeout_retry_stats["timeouts"], 0)


if __name__ == "__main__":
    unittest.main()
