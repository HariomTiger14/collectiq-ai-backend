"""Tier-3 batch isolation: one dead set must not destroy its whole batch.

sportscardspro's /price-guide/download-custom fails the ENTIRE request when
any single console_uid in it cannot be served, so before isolation a set
removed upstream discarded every set batched with it -- 24 healthy sets per
dead one at --batch-size 25, re-fetched from scratch on the next run.

Measured against production on 2026-09-01: console_uid G37119 ("1993 Hoops
Fifth Anniversary Gold") 404s on its console page and 503s on the CSV
endpoint. It sat at queue position 3 and, because failures were never
stamped, was retried first on every run forever.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from scripts.refresh_sportscardspro_rotation import (
    TIER3_MAX_FAILURES,
    _isolate_failed_batch,
    _ThrottleAbort,
)
from scripts.backfill_pricecharting_sets import CsvDownload, _Counter


def _rows(*uids):
    return [{"registry_id": f"id-{u}", "console_uid": u, "set_name": f"Set {u}"} for u in uids]


def _isolate(rows, dead_uids, *, status=503, budget=32):
    """Run isolation against a fake endpoint that 503s any request whose
    console_uid list contains a dead uid -- the real failure semantics."""
    calls = []
    written: list[Path] = []

    def fake_fetch(http, *, base_url, token, console_uids, rate_limit_counter,
                   blocked_counter, status_sink=None):
        calls.append(list(console_uids))
        if any(u in dead_uids for u in console_uids):
            if status_sink is not None:
                status_sink.append(status)
            return None
        # The real fetch hands back a file on disk, so the fake must too --
        # otherwise the caller's cleanup path is never exercised.
        handle, name = tempfile.mkstemp(suffix=".csv")
        path = Path(name)
        with open(handle, "w", encoding="utf-8") as out:
            out.write("csv:" + ",".join(console_uids))
        written.append(path)
        return CsvDownload(path, "utf-8")

    with mock.patch(
        "scripts.refresh_sportscardspro_rotation.fetch_batch_csv_file", fake_fetch
    ):
        downloads, dead = _isolate_failed_batch(
            mock.MagicMock(spec=httpx.Client),
            base_url="https://example.test",
            token="t",
            rows=rows,
            sleep_seconds=0,
            rate_limit_counter=_Counter(),
            blocked_counter=_Counter(),
            budget=[budget],
        )
    return downloads, dead, calls, written


class Tier3BatchIsolationTest(unittest.TestCase):
    def test_single_dead_set_is_pinpointed_and_the_rest_salvaged(self):
        rows = _rows("A", "B", "C", "D", "E", "F", "G", "H")
        downloads, dead, _, _written = _isolate(rows, {"E"})

        self.assertEqual([r["console_uid"] for r in dead], ["E"])
        recovered = {
            u
            for d in downloads
            for u in d.path.read_text().removeprefix("csv:").split(",")
        }
        # Every healthy set comes back; previously all 8 were discarded.
        self.assertEqual(recovered, {"A", "B", "C", "D", "F", "G", "H"})

    def test_isolation_is_cheaper_than_probing_every_set(self):
        rows = _rows(*"ABCDEFGHIJKLMNOP")  # 16 sets
        _downloads, dead, calls, _written = _isolate(rows, {"K"})

        self.assertEqual([r["console_uid"] for r in dead], ["K"])
        # Halving finds one offender in ~2*log2(n); linear probing would be 16.
        self.assertLess(len(calls), len(rows))

    def test_multiple_dead_sets_all_found(self):
        rows = _rows(*"ABCDEFGH")
        downloads, dead, _, _written = _isolate(rows, {"B", "G"})

        self.assertEqual(sorted(r["console_uid"] for r in dead), ["B", "G"])
        recovered = {
            u
            for d in downloads
            for u in d.path.read_text().removeprefix("csv:").split(",")
        }
        self.assertEqual(recovered, {"A", "C", "D", "E", "F", "H"})

    def test_all_dead_returns_no_csv_and_every_row_dead(self):
        rows = _rows("A", "B")
        downloads, dead, _, _written = _isolate(rows, {"A", "B"})

        self.assertEqual(downloads, [])
        self.assertEqual(sorted(r["console_uid"] for r in dead), ["A", "B"])

    def test_throttle_aborts_instead_of_probing_individual_sets(self):
        """A 429/403 means the endpoint is refusing us generally. Probing
        single sets in that state burns throttled requests against a door
        already shut -- the surest way to harden a temporary block."""
        for status in (429, 403):
            with self.subTest(status=status):
                with self.assertRaises(_ThrottleAbort):
                    _isolate(_rows("A", "B", "C", "D"), {"C"}, status=status)

    def test_budget_bounds_the_worst_case(self):
        rows = _rows(*"ABCDEFGH")
        _downloads, _dead, calls, _written = _isolate(
            rows, {"A", "C", "E", "G"}, budget=3
        )
        self.assertLessEqual(len(calls), 3)

    def test_max_failures_threshold_is_positive(self):
        # The queue filter is `tier3_failure_count lt TIER3_MAX_FAILURES`;
        # a zero threshold would silently empty the rotation.
        self.assertGreater(TIER3_MAX_FAILURES, 0)


if __name__ == "__main__":
    unittest.main()


class RetryStatsRebasingTest(unittest.TestCase):
    """A retry must not re-count rows the failed attempt already tallied.

    Observed on a real tier-3 run 2026-09-01: catalogRowsFailed 31,766 with
    failedWrites 0, and skippedUnchanged (3,023,794) exceeding
    catalogRowsParsed (1,908,562) -- impossible for real rows, and it made
    the ledger read as data loss when nothing had been lost.
    """

    def _client(self):
        client = mock.MagicMock()
        client.catalog_write_stats = {"written": 0, "skippedUnchanged": 0, "failed": 0}
        return client

    def test_failed_attempt_is_not_counted_once_a_retry_succeeds(self):
        from scripts.backfill_pricecharting_sets import write_catalog_rows_with_retry

        client = self._client()
        calls = {"n": 0}

        def fake_write(c, rows, *, batch_size):
            calls["n"] += 1
            if calls["n"] == 1:  # attempt 1: everything fails
                c.catalog_write_stats["failed"] += 100
                c.catalog_write_stats["skippedUnchanged"] += 900
                return False
            c.catalog_write_stats["written"] += 100          # attempt 2 succeeds
            c.catalog_write_stats["skippedUnchanged"] += 900
            return True

        with mock.patch(
            "scripts.backfill_pricecharting_sets.write_catalog_rows", fake_write
        ):
            wrote, retries = write_catalog_rows_with_retry(
                client, [{"pricecharting_id": "x"}],
                batch_size=10, attempts=3, backoff_seconds=0, sleep=lambda _: None,
            )

        self.assertTrue(wrote)
        self.assertEqual(retries, 1)
        self.assertEqual(client.catalog_write_stats["failed"], 0)
        self.assertEqual(client.catalog_write_stats["skippedUnchanged"], 900)
        # written stays cumulative: each attempt writes DIFFERENT rows.
        self.assertEqual(client.catalog_write_stats["written"], 100)

    def test_exhausted_attempts_keep_the_failure_visible(self):
        from scripts.backfill_pricecharting_sets import write_catalog_rows_with_retry

        client = self._client()

        def always_fail(c, rows, *, batch_size):
            c.catalog_write_stats["failed"] += 50
            return False

        with mock.patch(
            "scripts.backfill_pricecharting_sets.write_catalog_rows", always_fail
        ):
            wrote, _ = write_catalog_rows_with_retry(
                client, [{"pricecharting_id": "x"}],
                batch_size=10, attempts=3, backoff_seconds=0, sleep=lambda _: None,
            )

        self.assertFalse(wrote)
        # Nothing is rebased on failure -- a genuinely lost batch must not be
        # quietly zeroed by the same code that hides retry noise.
        self.assertGreater(client.catalog_write_stats["failed"], 0)
