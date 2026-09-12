"""The download stage: claim, fetch, validate, upload — and nothing else.

PR 2 writes no catalog rows, no history and no registry stamps. Several tests
below exist only to prove that, because the failure they guard against is
silent: a batch marked refreshed from data that never landed looks identical
to a successful one until someone reads the prices.

The guards worth breaking a build over:

  * validation runs BEFORE upload, so a wrong-catalog file never enters the
    ingest queue. A mistyped filter returned HTTP 200 with 123,166 rows of
    the wrong catalog (measured 2026-09-07).
  * a 403 stops the run. Cloudflare refusing us is what killed the
    sportscardspro.com CSV path entirely; retrying hardens a temporary block.
  * backpressure. A stuck ingester must not turn into a storage bill.
  * --commit is required. Default-safe, because this job spends vendor CSV
    slots (144/day, sports wants ~106).
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.catalog_batches import (
    DOWNLOADED,
    DOWNLOADING,
    FETCH_FAILED,
    PENDING,
    VALIDATED,
    VALIDATION_FAILED,
)
from scripts.download_catalog_batch import (
    DEFAULT_BATCH_SIZE,
    BatchStore,
    main,
    parse_args,
    reap_stale_leases,
)

CSV = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Baseball Cards 1962 Bazooka,Card {i},$1.00\n" for i in range(1, 21)
)
WRONG_CATALOG = "id,console-name,product-name,loose-price\n" + "".join(
    f"{i},Console {i % 200},Game {i},$1.00\n" for i in range(1, 400)
)


class _Store:
    """Stands in for BatchStore, recording every write."""

    # Mirrors BatchStore. A fake missing a method its caller reads fails at
    # runtime and nowhere else -- the same gap that let 39 mock handlers go
    # green in #212.
    sibling_names: list[str] = []

    def sibling_set_names(self, *, source, uids):
        return list(self.sibling_names)

    def __init__(self, *, due=None, batches=None):
        self._due = due if due is not None else [
            {"registry_id": f"r{i}", "console_uid": f"G{i}", "set_name": "1962 Bazooka"}
            for i in range(3)
        ]
        self._batches = batches or {}
        self.inserted: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, Path]] = []

    def claim_due_sets(self, *, source, limit):
        return self._due[:limit]

    def batches(self, *, source, statuses):
        return [b for b in self._batches.get("all", []) if b["status"] in statuses]

    def insert(self, row):
        batch = {**row, "batch_id": "batch-1", "attempts": 0}
        self.inserted.append(batch)
        return batch

    def update(self, batch_id, patch):
        self.updates.append((batch_id, patch))

    def upload(self, key, path):
        self.uploads.append((key, path))

    # helpers
    def statuses(self):
        return [p.get("status") for _, p in self.updates if "status" in p]


def _run(store, *, body=CSV, status=200, argv=("--commit",)):
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="upstream error")
        return httpx.Response(200, content=body.encode())

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*a, **kw):
        kw.pop("transport", None)
        return real_client(*a, transport=transport, **{k: v for k, v in kw.items()})

    with patch("scripts.download_catalog_batch.BatchStore", return_value=store), \
         patch("scripts.download_catalog_batch.httpx.Client", side_effect=client_factory), \
         patch("scripts.download_catalog_batch.SharedRateLimiter") as limiter, \
         patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                   "SUPABASE_SERVICE_ROLE_KEY": "k",
                                   "PRICECHARTING_API_TOKEN": "t"}):
        limiter.return_value.acquire.return_value = True
        return main(list(argv))


class DefaultSafeTest(unittest.TestCase):
    def test_without_commit_nothing_is_claimed_or_written(self) -> None:
        """This job spends vendor CSV slots; the default must not."""
        store = _Store()
        _run(store, argv=())
        self.assertEqual(store.inserted, [])
        self.assertEqual(store.updates, [])
        self.assertEqual(store.uploads, [])

    def test_commit_is_off_by_default_in_the_parser(self) -> None:
        self.assertFalse(parse_args([]).commit)

    def test_the_default_batch_size_is_350(self) -> None:
        """500 works but sits near the vendor's 503 cliff; 1000 fails."""
        self.assertEqual(DEFAULT_BATCH_SIZE, 350)
        self.assertEqual(parse_args([]).batch_size, 350)


class HappyPathTest(unittest.TestCase):
    def test_the_batch_walks_pending_to_validated_and_uploads(self) -> None:
        store = _Store()
        self.assertEqual(_run(store), 0)
        self.assertEqual(store.statuses(), [DOWNLOADING, DOWNLOADED, VALIDATED])
        self.assertEqual(len(store.uploads), 1)

    def test_the_storage_key_is_recorded_with_the_validated_status(self) -> None:
        store = _Store()
        _run(store)
        final = [p for _, p in store.updates if p.get("status") == VALIDATED][0]
        self.assertIn("storage_key", final)
        self.assertTrue(final["storage_key"].startswith("sportscardspro/"))

    def test_the_lease_is_released_on_success(self) -> None:
        store = _Store()
        _run(store)
        final = [p for _, p in store.updates if p.get("status") == VALIDATED][0]
        self.assertIsNone(final["claimed_at"])


class NothingIsIngestedOrStampedTest(unittest.TestCase):
    """PR 2 downloads. Stamping belongs to the ingester, after a clean write."""

    def test_no_registry_write_of_any_kind(self) -> None:
        store = _Store()
        _run(store)
        for _, patch_body in store.updates:
            for key in patch_body:
                with self.subTest(key=key):
                    self.assertFalse(
                        key.startswith("tier3_") or key.startswith("tier1_"),
                        f"the downloader wrote a registry column: {key}")

    def test_the_script_never_mentions_the_catalog_write_path(self) -> None:
        source = Path("scripts/download_catalog_batch.py").read_text()
        for forbidden in ("to_catalog_row", "write_catalog_rows",
                          "mark_tier3_refreshed", "SupabaseCatalogClient"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)


class ValidationGateTest(unittest.TestCase):
    def test_a_wrong_catalog_file_is_never_uploaded(self) -> None:
        """The whole reason validation precedes upload."""
        store = _Store()
        _run(store, body=WRONG_CATALOG)
        self.assertEqual(store.uploads, [], "a wrong-catalog file reached the bucket")
        self.assertIn(VALIDATION_FAILED, store.statuses())
        self.assertNotIn(VALIDATED, store.statuses())

    def test_the_mismatch_detail_is_recorded_for_diagnosis(self) -> None:
        store = _Store()
        _run(store, body=WRONG_CATALOG)
        failed = [p for _, p in store.updates if p.get("status") == VALIDATION_FAILED][0]
        self.assertEqual(failed["last_error_class"], "validation")
        detail = json.loads(failed["last_error"])
        self.assertIn("observedFamilies", detail)
        self.assertIn("expectedSetNames", detail)


class FetchFailureTest(unittest.TestCase):
    def test_a_503_marks_fetch_failed_and_exits_zero(self) -> None:
        """Transient: the run ends quietly and the sets stay queued."""
        store = _Store()
        self.assertEqual(_run(store, status=503), 0)
        self.assertIn(FETCH_FAILED, store.statuses())
        failed = [p for _, p in store.updates if p.get("status") == FETCH_FAILED][0]
        self.assertEqual(failed["last_error_class"], "transient")

    def test_a_403_exits_non_zero_so_the_block_is_visible(self) -> None:
        """Cloudflare refusing us killed the sportscardspro.com CSV path.
        Retrying hardens a temporary block into a durable one."""
        store = _Store()
        self.assertEqual(_run(store, status=403), 1)
        failed = [p for _, p in store.updates if p.get("status") == FETCH_FAILED][0]
        self.assertEqual(failed["last_error_class"], "blocked")

    def test_no_upload_happens_on_a_failed_fetch(self) -> None:
        store = _Store()
        _run(store, status=503)
        self.assertEqual(store.uploads, [])

    def test_a_429_is_transient_not_blocked(self) -> None:
        store = _Store()
        _run(store, status=429)
        failed = [p for _, p in store.updates if p.get("status") == FETCH_FAILED][0]
        self.assertEqual(failed["last_error_class"], "transient")


class BackpressureTest(unittest.TestCase):
    """A stuck ingester must not become a storage bill."""

    def test_a_full_queue_skips_the_run_without_fetching(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": VALIDATED, "requested_count": 350},
            {"batch_id": "b", "status": VALIDATED, "requested_count": 350},
        ]})
        self.assertEqual(_run(store), 0)
        self.assertEqual(store.inserted, [], "claimed sets while the queue was full")
        self.assertEqual(store.uploads, [])

    def test_one_queued_batch_still_allows_a_download(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "a", "status": VALIDATED, "requested_count": 350},
        ]})
        _run(store)
        self.assertEqual(len(store.uploads), 1)


class ResumeTest(unittest.TestCase):
    """An abandoned batch is resumed, not re-claimed.

    Claiming the same sets again would double-book them and spend a second
    vendor slot on rows already spoken for.
    """

    def test_a_pending_batch_is_resumed_instead_of_claiming_new_sets(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "old-1", "status": PENDING, "console_uids": ["G1", "G2"],
             "registry_ids": ["r1", "r2"], "requested_count": 2, "attempts": 1},
        ]})
        _run(store)
        self.assertEqual(store.inserted, [], "created a new batch instead of resuming")
        self.assertTrue(all(bid == "old-1" for bid, _ in store.updates))

    def test_the_attempt_counter_advances_on_resume(self) -> None:
        store = _Store(batches={"all": [
            {"batch_id": "old-1", "status": PENDING, "console_uids": ["G1"],
             "registry_ids": ["r1"], "requested_count": 1, "attempts": 1},
        ]})
        _run(store)
        first = [p for _, p in store.updates if p.get("status") == DOWNLOADING][0]
        self.assertEqual(first["attempts"], 2)


class ReaperTest(unittest.TestCase):
    """A claim nobody releases is how a row stays 'running' for 118 hours."""

    def test_a_stale_download_lease_is_returned_to_pending(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        store = _Store(batches={"all": [
            {"batch_id": "stuck", "status": DOWNLOADING, "claimed_at": old},
        ]})
        reaped = reap_stale_leases(store, source="sportscardspro", commit=True)
        self.assertEqual(reaped, 1)
        self.assertEqual(store.updates[0][1]["status"], PENDING)
        self.assertIsNone(store.updates[0][1]["claimed_at"])

    def test_a_fresh_lease_is_left_alone(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        store = _Store(batches={"all": [
            {"batch_id": "busy", "status": DOWNLOADING, "claimed_at": recent},
        ]})
        self.assertEqual(reap_stale_leases(store, source="sportscardspro", commit=True), 0)
        self.assertEqual(store.updates, [])

    def test_the_reaper_writes_nothing_without_commit(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        store = _Store(batches={"all": [
            {"batch_id": "stuck", "status": DOWNLOADING, "claimed_at": old},
        ]})
        self.assertEqual(reap_stale_leases(store, source="sportscardspro", commit=False), 1)
        self.assertEqual(store.updates, [])


class RateLimiterTest(unittest.TestCase):
    def test_no_slot_leaves_the_batch_pending_rather_than_failed(self) -> None:
        """Not getting a turn is not a failure of the batch."""
        store = _Store()
        with patch("scripts.download_catalog_batch.BatchStore", return_value=store), \
             patch("scripts.download_catalog_batch.SharedRateLimiter") as limiter, \
             patch.dict("os.environ", {"SUPABASE_URL": "https://x.test",
                                       "SUPABASE_SERVICE_ROLE_KEY": "k",
                                       "PRICECHARTING_API_TOKEN": "t"}):
            limiter.return_value.acquire.return_value = False
            self.assertEqual(main(["--commit"]), 0)
        self.assertEqual(store.statuses()[-1], PENDING)
        self.assertEqual(store.uploads, [])

    def test_the_limiter_is_used_at_all(self) -> None:
        """Manual probes bypassed it; production must not."""
        source = Path("scripts/download_catalog_batch.py").read_text()
        self.assertIn("SharedRateLimiter", source)
        self.assertIn("acquire(", source)


if __name__ == "__main__":
    unittest.main()


class ARefusedBatchExitsNonZeroTest(unittest.TestCase):
    """Two refusals on 2026-09-09 were recorded green, two minutes apart.

    A refusal is not backpressure. The file is a wrong-catalog response, the
    same sets are claimed again next run, and it repeats until someone looks --
    so it must be loud at the Render level too, not only in the summary.

    A vendor 503 stays a quiet zero: that is expected pacing, and alerting on
    it would train the alert away.
    """

    def test_the_refusal_path_returns_one(self) -> None:
        import pathlib
        import re

        source = pathlib.Path("scripts/download_catalog_batch.py").read_text()
        block = source[source.index("except CsvFamilyMismatch"):]
        block = block[: block.index("key = storage_key")]
        self.assertRegex(block, r"return 1\b",
                         "a refused batch still exits 0; it was recorded green twice")

    def test_a_transient_fetch_failure_stays_quiet(self) -> None:
        """Only a blocked host exits non-zero on the fetch path."""
        import pathlib

        source = pathlib.Path("scripts/download_catalog_batch.py").read_text()
        self.assertIn("return 1 if error_class == CLASS_BLOCKED else 0", source)


class DuplicateConsoleUidDoesNotJamTheRotationTest(unittest.TestCase):
    """Asserted through main(), not against the helpers.

    dedupe_by_console_uid and the sibling lookup are both unit-tested in
    tests/test_duplicate_console_uid.py, and both passed while mutations that
    removed them from the DOWNLOADER entirely went unnoticed. The call site is
    where the jam actually happens.
    """

    DUPE = [
        {"registry_id": "r1", "console_uid": "G9157", "set_name": "2015 Panini Donrus"},
        {"registry_id": "r2", "console_uid": "G9157", "set_name": "2015 Panini Donruss"},
        {"registry_id": "r3", "console_uid": "G1", "set_name": "1962 Bazooka"},
    ]

    def test_the_uid_is_requested_only_once(self) -> None:
        """Sent twice, one family comes back, validation passes, and BOTH rows
        are stamped refreshed -- the duplicate having never been refreshed."""
        store = _Store(due=list(self.DUPE))
        _run(store)
        uids = store.inserted[0]["console_uids"]
        self.assertEqual(sorted(uids), ["G1", "G9157"],
                         f"the duplicate uid was requested twice: {uids}")

    def test_only_the_kept_row_is_claimed(self) -> None:
        store = _Store(due=list(self.DUPE))
        _run(store)
        self.assertEqual(sorted(store.inserted[0]["registry_ids"]), ["r1", "r3"])

    def test_the_vendors_own_label_does_not_refuse_the_batch(self) -> None:
        """The jam: claim 'Donrus', get answered 'Donruss'. Without the sibling
        widening this refuses all 350 sets and reclaims them next run."""
        store = _Store(due=[self.DUPE[0]])
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Football Cards 2015 Panini Donruss,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertNotIn("validation_failed", statuses,
                         "the vendor's own spelling was refused as a wrong catalog")
        self.assertEqual(code, 0)

    def test_a_wrong_catalog_is_still_refused_with_siblings_present(self) -> None:
        """Widening must not admit a family belonging to no requested uid."""
        store = _Store(due=[self.DUPE[0]])
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Baseball Cards 1962 Topps,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertIn("validation_failed", statuses,
                      "a wrong catalog passed once siblings were allowed")
        self.assertEqual(code, 1, "a refusal must exit non-zero")


class AResumedBatchIsValidatedToo(unittest.TestCase):
    """The resume path had no family check at all.

    Expected names were built from the CLAIMED rows, and a resumed PENDING
    batch has none -- so set_names was empty, and validate_csv_families returns
    early when it has nothing to compare against. Every retry therefore ran
    with the wrong-catalog guard switched off.

    That is worse than the G9157 jam it sits beside: a jam refuses a good batch
    loudly, an absent guard accepts a bad one quietly. Both are fixed by
    deriving the names from the batch's own console_uids, which exist on both
    paths.
    """

    PENDING_BATCH = {"batch_id": "old-1", "status": PENDING,
                     "console_uids": ["G9157"], "registry_ids": ["r1"],
                     "requested_count": 1, "attempts": 0}

    def test_a_resumed_batch_accepts_the_vendors_own_label(self) -> None:
        """Claim 'Donrus', crash, resume, get answered 'Donruss'."""
        store = _Store(batches={"all": [dict(self.PENDING_BATCH)]})
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Football Cards 2015 Panini Donruss,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertNotIn("validation_failed", statuses)
        self.assertEqual(code, 0)

    def test_a_resumed_batch_still_refuses_a_wrong_catalog(self) -> None:
        """The guard that was absent entirely before this."""
        store = _Store(batches={"all": [dict(self.PENDING_BATCH)]})
        store.sibling_names = ["2015 Panini Donrus", "2015 Panini Donruss"]
        csv = ("id,console-name,product-name,loose-price\n"
               "1,Baseball Cards 1962 Topps,Card,1.00\n")
        code = _run(store, body=csv)
        statuses = [p.get("status") for _, p in store.updates]
        self.assertIn("validation_failed", statuses,
                      "a resumed batch accepted a wrong catalog")
        self.assertEqual(code, 1)

    def test_the_resumed_batch_looks_up_siblings_for_its_own_uids(self) -> None:
        asked: list[list[str]] = []
        store = _Store(batches={"all": [dict(self.PENDING_BATCH)]})
        store.sibling_set_names = lambda *, source, uids: asked.append(list(uids)) or []
        _run(store)
        self.assertEqual(asked, [["G9157"]],
                         "resume did not widen from the batch's console_uids")
