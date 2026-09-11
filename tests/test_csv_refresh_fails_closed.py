"""The daily CSV refresh must not report success while writing nothing.

From 2026-09-08 to 2026-09-11 it did exactly that. render.yaml passed
--batch-size 1000; upsert_rows refuses anything at or above PostgREST's
1,000-row lookup cap; import_batch caught the ValueError and returned zeros;
and `success` was computed from uncaught source exceptions only. Five green
runs in the ops ledger, ~41 minutes each -- four 600-second inter-source sleeps
with every write dying instantly -- and five CSV catalogs went stale for five
days while the board said healthy.

Two independent holes, so two independent sets of tests:

  * the batch size the cron actually passes, which the argparse default does
    not control -- Render runs the startCommand, not the default
  * a write failure that cannot be told apart from "nothing had changed"

tests/test_lookup_truncation_guard.py already pins the staged ingester's
batch size. This job had no equivalent, which is why the same class of bug
landed here and not there.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from scripts.import_pricecharting_catalog import POSTGREST_MAX_LOOKUP_ROWS
from scripts.refresh_pricecharting_catalog import import_batch, parse_args

RENDER_YAML = pathlib.Path("render.yaml")


class TheBatchSizeTheCronActuallyPassesTest(unittest.TestCase):
    """Render runs the startCommand. The argparse default never applies in
    production, so pinning only the default would have caught nothing."""

    def _start_command(self) -> str:
        text = RENDER_YAML.read_text()
        match = re.search(
            r"startCommand: python -m scripts\.refresh_pricecharting_catalog[^\n]*", text)
        self.assertIsNotNone(match, "refresh_pricecharting_catalog startCommand not found")
        return match.group(0)

    def _yaml_batch_size(self) -> int:
        match = re.search(r"--batch-size (\d+)", self._start_command())
        self.assertIsNotNone(match, "startCommand passes no --batch-size")
        return int(match.group(1))

    def test_the_start_command_is_below_the_lookup_cap(self) -> None:
        size = self._yaml_batch_size()
        self.assertLess(
            size, POSTGREST_MAX_LOOKUP_ROWS,
            f"render.yaml passes --batch-size {size}; upsert_rows refuses "
            f">= {POSTGREST_MAX_LOOKUP_ROWS} and the run writes nothing")

    def test_the_exact_value_that_broke_production_is_rejected(self) -> None:
        self.assertNotEqual(self._yaml_batch_size(), 1000)

    def test_the_argparse_default_is_below_the_cap_too(self) -> None:
        """Not what production uses, but a hand-run without --batch-size
        should not silently do nothing either."""
        self.assertLess(parse_args([]).batch_size, POSTGREST_MAX_LOOKUP_ROWS)

    def test_the_two_agree(self) -> None:
        """Divergence is how the default looks safe while the cron is not."""
        self.assertEqual(parse_args([]).batch_size, self._yaml_batch_size())


class _Client:
    """Stands in for SupabaseCatalogClient."""

    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.calls = 0

    def sync_scd2_history_rows(self, rows, *, batch_size):
        if self.error is not None:
            raise self.error
        return len(rows)

    def upsert_rows(self, rows, *, batch_size):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return len(rows)


ROWS = [{"pricecharting_id": "1", "product_name": "Charizard"}]


class AProgrammingErrorFailsClosedTest(unittest.TestCase):
    """A ValueError from the writer means the batch size is wrong. No number
    of retries fixes that, and a zero hides it."""

    def test_the_cap_valueerror_is_not_swallowed(self) -> None:
        client = _Client(ValueError(
            f"batch_size 1000 reaches PostgREST's {POSTGREST_MAX_LOOKUP_ROWS}-row lookup cap"))
        with self.assertRaises(ValueError):
            import_batch(batch=ROWS, batch_size=1000, dry_run=False,
                         client=client, imported_rows=0)

    def test_it_does_not_return_a_zero_that_reads_as_unchanged(self) -> None:
        client = _Client(ValueError("batch_size must be greater than zero"))
        try:
            result = import_batch(batch=ROWS, batch_size=0, dry_run=False,
                                  client=client, imported_rows=0)
        except ValueError:
            return
        self.fail(f"returned {result} instead of raising")


class ATransientFailureIsToleratedButReportedTest(unittest.TestCase):
    """SystemExit here is an HTTP 500 / 57014 statement timeout, not a bug.

    Aborting the whole run on one slow write is the failure fixed on
    2026-08-29, where a single 503 on the first CSV cost every later category
    its daily refresh. So it is still caught -- but the caller now learns it
    happened rather than reading the zero as success.
    """

    def test_a_statement_timeout_does_not_abort_the_run(self) -> None:
        client = _Client(SystemExit("HTTP 500: 57014 canceling statement"))
        result = import_batch(batch=ROWS, batch_size=900, dry_run=False,
                              client=client, imported_rows=0)
        self.assertEqual(result["importedRows"], 0)

    def test_but_the_batch_is_flagged_as_failed(self) -> None:
        client = _Client(SystemExit("HTTP 500: 57014 canceling statement"))
        result = import_batch(batch=ROWS, batch_size=900, dry_run=False,
                              client=client, imported_rows=0)
        self.assertTrue(result["batchFailed"],
                        "a failed write returned a zero indistinguishable from 'unchanged'")

    def test_a_clean_write_is_not_flagged(self) -> None:
        result = import_batch(batch=ROWS, batch_size=900, dry_run=False,
                              client=_Client(), imported_rows=0)
        self.assertFalse(result["batchFailed"])
        self.assertEqual(result["importedRows"], 1)

    def test_a_dry_run_is_not_a_failure(self) -> None:
        result = import_batch(batch=ROWS, batch_size=900, dry_run=True,
                              client=None, imported_rows=0)
        self.assertFalse(result["batchFailed"])


class WhatCountsAsAFailedSourceTest(unittest.TestCase):
    """A write that was attempted and did not land -- and only that.

    Not "wrote no catalog rows": after #213/#216 that is what a healthy
    price-only night looks like.
    """

    def _summary(self, client, text=None, batch_size=900):
        import tempfile

        from scripts.refresh_pricecharting_catalog import import_source_file

        csv_text = text or (
            "id,product-name,console-name,loose-price\n"
            "1,Charizard,Pokemon Cards,79000\n"
            "2,Pikachu,Pokemon Cards,1200\n")
        handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv")
        with handle:
            handle.write(csv_text)
        path = pathlib.Path(handle.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return import_source_file(
            source_name="pokemon.csv", path=path,
            source_downloaded_at="2026-09-12T00:00:00Z",
            batch_size=batch_size, dry_run=False, client=client)

    def test_every_batch_failing_marks_the_source_failed(self) -> None:
        summary = self._summary(_Client(SystemExit("HTTP 500")))
        self.assertTrue(summary["failed"])
        self.assertEqual(summary["failureReason"], "batch_write_failed")

    def test_a_quiet_night_writing_no_catalog_or_scd2_rows_is_NOT_a_failure(self) -> None:
        """The correction that matters, and an earlier draft of this fix got
        it backwards.

        importedRows counts CATALOG writes; historyRows counts SCD2 version
        inserts. Since #213/#216 a healthy night writes neither -- prices go to
        pricecharting_current_price, the catalog is touched only for metadata
        and new items. Treating that as failure would have exited 1 on the
        quiet night after a catch-up, while Discover updated correctly.

        The five silent nights are covered elsewhere: the cap ValueError is no
        longer swallowed, so it cannot return a quiet zero.
        """

        class _WritesNothing(_Client):
            def sync_scd2_history_rows(self, rows, *, batch_size):
                return 0

            def upsert_rows(self, rows, *, batch_size):
                return 0

        summary = self._summary(_WritesNothing())
        self.assertGreater(summary["validRows"], 0)
        self.assertEqual(summary["importedRows"], 0)
        self.assertEqual(summary["historyRows"], 0)
        self.assertFalse(summary["failed"],
                         "a price-only night was reported as a failed source")
        self.assertIsNone(summary["failureReason"])

    def test_a_normal_source_is_not_flagged(self) -> None:
        summary = self._summary(_Client())
        self.assertEqual(summary["importedRows"], 2)
        self.assertFalse(summary["failed"])

    def test_an_empty_csv_is_not_a_failure(self) -> None:
        """No rows to write is not the same as failing to write rows."""
        summary = self._summary(_Client(), text="id,product-name,console-name,loose-price\n")
        self.assertEqual(summary["validRows"], 0)
        self.assertFalse(summary["failed"])


if __name__ == "__main__":
    unittest.main()


class TheRunItselfFailsTest(unittest.TestCase):
    """The run-level assertion, and the one the first pass missed.

    `success` used to be `not failures`, where failures held only UNCAUGHT
    source exceptions. A source that returned cleanly having written nothing
    never reached that list, so the ledger recorded five green runs while five
    catalogs went stale. Testing import_source_file alone does not catch this;
    the aggregation in main() is where the zero became a success.
    """

    def _run(self, summary):
        import contextlib
        import io
        import json
        from unittest.mock import patch

        import scripts.refresh_pricecharting_catalog as module

        buffer = io.StringIO()
        with patch.object(module, "SupabaseCatalogClient", lambda **kw: object()), \
             patch.object(module, "SharedRateLimiter", lambda *a, **kw: None), \
             patch.object(module, "timeout_retry_summary", lambda client: {}), \
             patch.object(module, "refresh_source", lambda **kw: dict(summary)), \
             contextlib.redirect_stdout(buffer):
            code = module.main(["--sources", "pokemon", "--batch-size", "900"])
        printed = buffer.getvalue()
        # The summary is the last top-level JSON object printed; prose lines
        # may or may not precede it depending on whether anything failed.
        starts = [i for i, line in enumerate(printed.splitlines()) if line.startswith("{")]
        self.assertTrue(starts, f"no JSON summary printed:\n{printed}")
        body = "\n".join(printed.splitlines()[starts[-1]:])
        return code, json.loads(body[: body.rindex("}") + 1])

    _CLEAN = {"source": "pokemon.csv", "inputRows": 100, "validRows": 100,
              "importedRows": 100, "historyRows": 100, "archivePath": None,
              "failed": False, "failureReason": None}

    def test_a_failed_batch_makes_the_run_fail(self) -> None:
        code, payload = self._run({**self._CLEAN, "importedRows": 0, "historyRows": 0,
                                   "failed": True,
                                   "failureReason": "batch_write_failed"})
        self.assertFalse(payload["success"], "run reported success after a failed write")
        self.assertEqual(code, 1, "exit code was zero on a run whose writes failed")

    def test_a_price_only_night_still_succeeds(self) -> None:
        """millions parsed, zero catalog writes, zero SCD2 versions -- healthy."""
        code, payload = self._run({**self._CLEAN, "importedRows": 0, "historyRows": 0,
                                   "failed": False, "failureReason": None})
        self.assertTrue(payload["success"])
        self.assertEqual(code, 0)

    def test_the_reason_reaches_the_ledger(self) -> None:
        """`success: false` with no reason sends the next person to the logs."""
        _, payload = self._run({**self._CLEAN, "importedRows": 0, "historyRows": 0,
                                "failed": True, "failureReason": "batch_write_failed"})
        self.assertEqual(payload["failedSources"],
                         [{"source": "pokemon", "error": "batch_write_failed"}])

    def test_a_healthy_run_still_succeeds(self) -> None:
        code, payload = self._run(self._CLEAN)
        self.assertTrue(payload["success"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["failedSources"], [])
