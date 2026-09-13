"""The 2026-09-13 rename jam, and what now prevents it.

G9533's registry name was '2015 Topps Platinum Autograph Rookies'. The vendor
renamed the set and answered the CSV with 'football cards 2015 topps platinum
autographed rookie refractor'. One unmatched family refuses the whole file
(correctly), the sets are never stamped, and claim_due_sets orders
tier3_refreshed_at NULLS FIRST -- so the identical 350 uids came back every ten
minutes for 90 minutes, spending twelve account-wide CSV slots.

Sibling widening (#225) could not help: it unions the set_names of OTHER
registry rows sharing the uid, and G9533 has only one row. vendor_label is that
missing second name.
"""

import unittest

import httpx

from scripts.catalog_batch_store import BatchStore
from scripts.fill_sports_vendor_labels import build_label_index, plan_updates
from scripts.csv_source_policy import (
    CsvFamilyMismatch,
    normalize_family,
    validate_csv_families,
)

# Exactly as observed in batch b75696f6.
G9533_REGISTRY_NAME = "2015 Topps Platinum Autograph Rookies"
G9533_VENDOR_LABEL = "2015 topps platinum autographed rookie refractor"
G9533_CSV_FAMILY = ("football cards 2015 topps platinum autographed "
                    "rookie refractor")

# The other survivor of the diff: our name is SHORTER than the vendor's, and a
# bare '1993 classic' exists in three other sports but not in hockey.
G58581_REGISTRY_NAME = "1993 Classic"
G58581_VENDOR_LABEL = "1993 classic four sport"
G58581_CSV_FAMILY = "hockey cards 1993 classic four sport"


class TheRenameIsAcceptedOnlyWithTheVendorLabelTest(unittest.TestCase):
    def test_the_registry_name_alone_still_fails(self) -> None:
        """The guard is not being loosened. Without the vendor's label this is
        still an unrecognised family, and still refuses the file."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families([G9533_CSV_FAMILY],
                                  expected_set_names=[G9533_REGISTRY_NAME],
                                  requested_uid_count=350)

    def test_the_vendor_label_accepts_it(self) -> None:
        validate_csv_families(
            [G9533_CSV_FAMILY],
            expected_set_names=[G9533_REGISTRY_NAME, G9533_VENDOR_LABEL],
            requested_uid_count=350)

    def test_a_shorter_registry_name_also_works(self) -> None:
        """G58581. Our '1993 Classic' is less specific than the vendor's label,
        not wrong -- and set_name is deliberately not overwritten, because a
        bare '1993 classic' really does exist in other sports."""
        validate_csv_families(
            [G58581_CSV_FAMILY],
            expected_set_names=[G58581_REGISTRY_NAME, G58581_VENDOR_LABEL],
            requested_uid_count=350)

    def test_a_genuinely_wrong_family_still_fails_closed(self) -> None:
        """Widening must not become fail-open. A family matching neither our
        name nor the vendor's label for a requested uid is still refused."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                ["video games nintendo 64"],
                expected_set_names=[G9533_REGISTRY_NAME, G9533_VENDOR_LABEL],
                requested_uid_count=350)

    def test_the_count_check_is_untouched(self) -> None:
        """The 123,166-row video-game dump: 229 families for 3 uids. No amount
        of name widening may let that through, because it is caught on count
        before names are considered at all."""
        with self.assertRaises(CsvFamilyMismatch) as ctx:
            validate_csv_families([f"family {i}" for i in range(229)],
                                  expected_set_names=[G9533_VENDOR_LABEL],
                                  requested_uid_count=3)
        self.assertIn("cannot widen", str(ctx.exception))

    def test_a_trailing_number_is_still_not_a_prefix_match(self) -> None:
        """The reason _matches_requested demands a word boundary: set names
        really do differ only by a trailing digit."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(["baseball cards 2023 panini prizm 10"],
                                  expected_set_names=["2023 Panini Prizm 1"],
                                  requested_uid_count=5)

    def test_normalisation_handles_the_case_difference(self) -> None:
        """Our names are title-case, the vendor's labels are lowercase."""
        self.assertEqual(normalize_family(G9533_VENDOR_LABEL),
                         normalize_family(G9533_VENDOR_LABEL.title()))


class ExpectedFamilyNamesReadsBothColumnsTest(unittest.TestCase):
    """The store's side: one request, both names."""

    def _store(self, handler):
        store = BatchStore(supabase_url="https://x.test", service_role_key="k",
                           timeout_seconds=5)
        store._client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
        return store

    def test_it_returns_set_names_and_vendor_labels(self) -> None:
        def handler(request):
            self.assertIn("set_name", request.url.params["select"])
            self.assertIn("vendor_label", request.url.params["select"])
            return httpx.Response(200, json=[
                {"set_name": G9533_REGISTRY_NAME,
                 "vendor_label": G9533_VENDOR_LABEL}])
        names = self._store(handler).expected_family_names(
            source="sportscardspro", uids=["G9533"])
        self.assertEqual(sorted(names),
                         sorted([G9533_REGISTRY_NAME, G9533_VENDOR_LABEL]))

    def test_a_null_vendor_label_contributes_nothing(self) -> None:
        """The column is nullable and starts empty everywhere. A row without a
        label must behave exactly as before this change."""
        def handler(request):
            return httpx.Response(200, json=[
                {"set_name": G9533_REGISTRY_NAME, "vendor_label": None}])
        names = self._store(handler).expected_family_names(
            source="sportscardspro", uids=["G9533"])
        self.assertEqual(names, [G9533_REGISTRY_NAME])

    def test_siblings_and_labels_are_unioned(self) -> None:
        """G9157 keeps working: two rows, two spellings, plus the vendor's."""
        def handler(request):
            return httpx.Response(200, json=[
                {"set_name": "2015 Panini Donruss", "vendor_label": None},
                {"set_name": "2015 Panini Donrus", "vendor_label": None}])
        names = self._store(handler).expected_family_names(
            source="sportscardspro", uids=["G9157"])
        self.assertEqual(sorted(names),
                         ["2015 Panini Donrus", "2015 Panini Donruss"])

    def test_no_uids_makes_no_request(self) -> None:
        def handler(request):
            raise AssertionError("queried the registry for an empty uid list")
        self.assertEqual(
            self._store(handler).expected_family_names(
                source="sportscardspro", uids=[]),
            [])

    def test_it_filters_by_source_site(self) -> None:
        """A registry_id or uid collision must not reach a pricecharting row."""
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return httpx.Response(200, json=[])
        self._store(handler).expected_family_names(
            source="sportscardspro", uids=["G1"])
        self.assertEqual(seen["source_site"], "eq.sportscardspro")


class LabelFillPlanTest(unittest.TestCase):

    def test_a_uid_is_found_in_any_category_index(self) -> None:
        """The registry's category and the vendor's need not agree, and the uid
        is unique across the vendor's whole catalog. Looking only in the row's
        own category would drop the label for exactly the rows most likely to be
        mislabelled."""
        index = {"football-cards": {"G9533": G9533_VENDOR_LABEL},
                 "hockey-cards": {}}
        rows = [{"registry_id": "r1", "console_uid": "G9533",
                 "category": "hockey-cards", "vendor_label": None}]
        updates, counts = plan_updates(rows, index)
        self.assertEqual(updates,
                         [{"registry_id": "r1",
                           "vendor_label": G9533_VENDOR_LABEL}])
        self.assertEqual(counts["matched"], 1)

    def test_an_unchanged_label_is_not_rewritten(self) -> None:
        index = {"football-cards": {"G9533": G9533_VENDOR_LABEL}}
        rows = [{"registry_id": "r1", "console_uid": "G9533",
                 "category": "football-cards",
                 "vendor_label": G9533_VENDOR_LABEL}]
        updates, counts = plan_updates(rows, index)
        self.assertEqual(updates, [])
        self.assertEqual(counts["unchanged"], 1)

    def test_a_second_rename_overwrites_the_stored_label(self) -> None:
        """A vendor can rename twice; a stored label can itself go stale."""
        index = {"football-cards": {"G9533": "a newer vendor name"}}
        rows = [{"registry_id": "r1", "console_uid": "G9533",
                 "category": "football-cards",
                 "vendor_label": G9533_VENDOR_LABEL}]
        updates, _ = plan_updates(rows, index)
        self.assertEqual(updates,
                         [{"registry_id": "r1",
                           "vendor_label": "a newer vendor name"}])

    def test_a_uid_absent_from_autocomplete_is_counted_not_guessed(self) -> None:
        rows = [{"registry_id": "r1", "console_uid": "G404",
                 "category": "football-cards", "vendor_label": None}]
        updates, counts = plan_updates(rows, {"football-cards": {}})
        self.assertEqual(updates, [])
        self.assertEqual(counts["notInAutocomplete"], 1)

    def test_the_sentinel_all_row_is_dropped(self) -> None:
        index = build_label_index(
            "football-cards", [{"label": "all", "value": ""},
                               {"label": G9533_VENDOR_LABEL, "value": "G9533"}])
        self.assertEqual(index, {"G9533": G9533_VENDOR_LABEL})


if __name__ == "__main__":
    unittest.main()
