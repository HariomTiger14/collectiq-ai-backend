"""One vendor set registered twice must not jam the rotation.

G9157 is registered as both '2015 Panini Donrus' and '2015 Panini Donruss' --
the first is the vendor's own typo'd slug, not ours. Both rows are claimable,
both carry the same console_uid, and the vendor answers that uid with ITS
canonical family name.

Two distinct failures follow, and they are not the same bug:

  * JAM. If one of the pair is claimed and the vendor answers with the other's
    name, the validator sees a family matching no requested set and refuses the
    whole 350-set batch. The same sets lead the rotation next run, so it
    repeats forever -- one vendor slot every cycle, producing nothing. Observed
    2026-09-09 on the now-retired G86996 pair, twice in two minutes.

  * SILENT STALENESS. If both are claimed, the uid is requested twice, one
    family comes back, validation passes, and stamp_registry_refreshed marks
    BOTH rows refreshed. The duplicate rotates to the back having never been
    refreshed at all -- no error, no jam, just a set that quietly stops
    updating.

The validator only ever flags UNEXPECTED OBSERVED families; it never complains
that an expected name went unseen. That asymmetry is why the second failure is
invisible.
"""

from __future__ import annotations

import unittest

from scripts.catalog_batch_store import dedupe_by_console_uid
from scripts.csv_source_policy import CsvFamilyMismatch, validate_csv_families

PAIR = [
    {"registry_id": "r1", "console_uid": "G9157", "set_name": "2015 Panini Donrus"},
    {"registry_id": "r2", "console_uid": "G9157", "set_name": "2015 Panini Donruss"},
]


class TheUidIsRequestedOnceTest(unittest.TestCase):
    def test_a_duplicate_uid_is_dropped_from_the_claim(self) -> None:
        kept = dedupe_by_console_uid(PAIR)
        self.assertEqual([r["registry_id"] for r in kept], ["r1"])

    def test_the_first_row_wins_which_is_the_oldest_refresh(self) -> None:
        """claim_due_sets orders by tier3_refreshed_at NULLS FIRST, so the
        row that leads the rotation is the one kept."""
        kept = dedupe_by_console_uid(list(reversed(PAIR)))
        self.assertEqual(kept[0]["registry_id"], "r2")

    def test_distinct_uids_are_all_kept(self) -> None:
        rows = [{"registry_id": f"r{i}", "console_uid": f"G{i}", "set_name": f"s{i}"}
                for i in range(5)]
        self.assertEqual(len(dedupe_by_console_uid(rows)), 5)

    def test_a_row_with_no_uid_is_not_treated_as_a_duplicate(self) -> None:
        """Absent is not equal to absent. Collapsing them would silently drop
        every unregistered set but one."""
        rows = [{"registry_id": "a", "console_uid": None, "set_name": "x"},
                {"registry_id": "b", "console_uid": "", "set_name": "y"}]
        self.assertEqual(len(dedupe_by_console_uid(rows)), 2)

    def test_an_empty_claim_is_fine(self) -> None:
        self.assertEqual(dedupe_by_console_uid([]), [])


class TheVendorsOwnLabelIsAccepted(unittest.TestCase):
    """The jam, and the fix for it.

    Widening expected names to the uid's siblings is not a weakening: a family
    matching a sibling of a REQUESTED uid is a requested set under the vendor's
    preferred label.
    """

    def test_the_sibling_name_alone_is_refused(self) -> None:
        """What happens today: claim 'Donrus', get answered 'Donruss'."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                ["Football Cards 2015 Panini Donruss"],
                expected_set_names=["2015 Panini Donrus"],
                requested_uid_count=1)

    def test_with_the_sibling_included_it_passes(self) -> None:
        validate_csv_families(
            ["Football Cards 2015 Panini Donruss"],
            expected_set_names=["2015 Panini Donrus", "2015 Panini Donruss"],
            requested_uid_count=1)

    def test_either_direction_works(self) -> None:
        """The vendor may answer with either registered spelling."""
        validate_csv_families(
            ["Football Cards 2015 Panini Donrus"],
            expected_set_names=["2015 Panini Donrus", "2015 Panini Donruss"],
            requested_uid_count=1)

    def test_a_genuinely_wrong_catalog_is_still_refused(self) -> None:
        """The guard that matters: widening must not admit a family belonging
        to no requested uid at all."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                ["Baseball Cards 1962 Topps"],
                expected_set_names=["2015 Panini Donrus", "2015 Panini Donruss"],
                requested_uid_count=1)

    def test_a_widened_batch_cannot_exceed_its_uid_count(self) -> None:
        """The count check is independent of names and still bites: a filtered
        download cannot widen, however many names we are willing to accept."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                ["Football Cards 2015 Panini Donrus",
                 "Football Cards 2015 Panini Donruss"],
                expected_set_names=["2015 Panini Donrus", "2015 Panini Donruss"],
                requested_uid_count=1)


class WhatWeDeliberatelyDidNotDoTest(unittest.TestCase):
    """Recorded so the next reader knows these were choices.

    Not merging or deleting a registry row: both are real vendor pages with
    distinct slugs, and G86996 was retired only after the failing CSV proved
    which name that uid answers to. There is no such evidence for G9157, and
    retiring the wrong half would silently stop refreshing the live set.

    Not ingesting a family that matches nothing: that is the wrong-catalog
    failure the validator exists to prevent, and it would write another
    category's prices into these sets.
    """

    def test_the_dedupe_drops_a_claim_not_a_row(self) -> None:
        """The skipped row stays claimable; it leads the rotation next time."""
        import inspect

        from scripts import catalog_batch_store

        source = inspect.getsource(catalog_batch_store.dedupe_by_console_uid)
        for destructive in ("delete", "update", "patch"):
            with self.subTest(verb=destructive):
                self.assertNotIn(destructive, source.lower())


if __name__ == "__main__":
    unittest.main()


class TheRealSiblingLookupTest(unittest.TestCase):
    """Against BatchStore itself, not the test double.

    Every test above uses a fake store, so a mutation emptying the real
    sibling_set_names passed all of them: the downloader would have widened
    its expected names with nothing, and the jam would be back with a full
    green suite.
    """

    def _store(self, handler):
        import httpx

        from scripts.catalog_batch_store import BatchStore

        store = BatchStore(supabase_url="https://x.test",
                           service_role_key="k", timeout_seconds=5)
        real = httpx.Client
        transport = httpx.MockTransport(handler)
        store._client = lambda: real(transport=transport)
        return store

    def test_it_returns_the_names_it_finds(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"set_name": "2015 Panini Donrus"},
                                             {"set_name": "2015 Panini Donruss"}])

        names = self._store(handler).sibling_set_names(
            source="sportscardspro", uids=["G9157"])
        self.assertEqual(sorted(names), ["2015 Panini Donrus", "2015 Panini Donruss"])

    def test_it_filters_by_uid_and_source(self) -> None:
        """Without the source filter it would pull a pricecharting.com row
        with a colliding uid into a sportscardspro batch's expected names."""
        import httpx

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        self._store(handler).sibling_set_names(
            source="sportscardspro", uids=["G9157", "G1"])
        params = captured[0].url.params
        self.assertEqual(params["source_site"], "eq.sportscardspro")
        self.assertEqual(params["console_uid"], "in.(G1,G9157)")

    def test_no_uids_makes_no_request(self) -> None:
        import httpx

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        self.assertEqual(self._store(handler).sibling_set_names(
            source="sportscardspro", uids=[]), [])
        self.assertEqual(captured, [])

    def test_a_row_without_a_name_is_skipped(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"set_name": None},
                                             {"set_name": "2015 Panini Donruss"}])

        self.assertEqual(self._store(handler).sibling_set_names(
            source="sportscardspro", uids=["G9157"]), ["2015 Panini Donruss"])
