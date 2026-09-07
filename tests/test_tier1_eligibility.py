"""Sets that /api/products cannot serve should stop being asked about.

small-sets-refresh refreshes by TEXT SEARCH, which only works when the search
returns one complete, unambiguous set under the vendor's hard 100-result cap.
Sets that fail that test are skipped correctly today -- and then re-checked
every single run: one API call, one 1.2s pace wait and one alarming log line
per set per hour, forever.

Measured live 2026-09-07: searching "2023 Panini Certified 2023" returned
exactly 100 products whose console-name was "Football Cards 2023 Panini
Select" -- a different set, truncated at the cap.

The load-bearing distinction here is that tier-1 ineligible is NOT a failure
marker. Tier-3 fetches by console_uid + CSV, does not care about the search
cap, and remains the correct owner of exactly these sets. Several tests below
exist only to prove nothing leaks from the tier-1 signal into tier-3 state.
"""

import unittest
from datetime import datetime, timedelta, timezone

from scripts.tier1_eligibility import (
    API_SEARCH_RESULT_CAP,
    MISS_THRESHOLD,
    OK,
    REASON_404,
    REASON_CAPPED,
    REASON_EMPTY,
    REASON_WRONG_FAMILY,
    RECHECK_DAYS,
    classify_search_result,
    plan_registry_update,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _products(n, console="Baseball Cards 1962 Bazooka"):
    return [{"id": str(i), "product-name": f"Card {i}", "console-name": console} for i in range(n)]


class ClassificationTest(unittest.TestCase):
    def test_a_failed_call_is_a_404_class_miss(self) -> None:
        self.assertEqual(
            classify_search_result(None, set_name="1962 Bazooka", http_status=404), REASON_404
        )

    def test_an_empty_result_is_its_own_reason(self) -> None:
        self.assertEqual(classify_search_result([], set_name="1962 Bazooka"), REASON_EMPTY)

    def test_exactly_the_cap_is_treated_as_truncated(self) -> None:
        """At the cap the result is incomplete even if it looks right.

        Writing it would silently drop everything past the 100th item, so the
        boundary belongs on the reject side.
        """
        products = _products(API_SEARCH_RESULT_CAP)
        self.assertEqual(classify_search_result(products, set_name="1962 Bazooka"), REASON_CAPPED)

    def test_just_under_the_cap_is_accepted(self) -> None:
        products = _products(API_SEARCH_RESULT_CAP - 1)
        self.assertEqual(classify_search_result(products, set_name="1962 Bazooka"), OK)

    def test_the_real_failing_query_is_classified_capped(self) -> None:
        """The measured case: 100 results, all from Panini Select."""
        products = _products(100, console="Football Cards 2023 Panini Select")
        self.assertEqual(
            classify_search_result(products, set_name="2023 Panini Certified"), REASON_CAPPED
        )

    def test_a_result_entirely_from_another_set_is_wrong_family(self) -> None:
        products = _products(5, console="Football Cards 2023 Panini Select")
        self.assertEqual(
            classify_search_result(products, set_name="2023 Panini Certified"),
            REASON_WRONG_FAMILY,
        )

    def test_a_partial_overlap_is_still_accepted(self) -> None:
        """Fuzzy search returning one crossover item is normal, not a miss.

        Only a TOTAL miss means the query resolves elsewhere; dedupe already
        handles the crossover case, and rejecting here would throw away good
        refreshes.
        """
        products = _products(3, console="Comic Books Creepshow")
        products.append(
            {"id": "999", "product-name": "crossover", "console-name": "Comic Books Stray Dogs"}
        )
        self.assertEqual(classify_search_result(products, set_name="Creepshow"), OK)

    def test_the_family_check_uses_a_word_boundary(self) -> None:
        """"Panini Select" must not match "Panini Select Draft"."""
        products = _products(3, console="Football Cards 2023 Panini Select Draft")
        self.assertEqual(
            classify_search_result(products, set_name="2023 Panini Select"), REASON_WRONG_FAMILY
        )

    def test_a_missing_set_name_disables_only_the_family_check(self) -> None:
        self.assertEqual(classify_search_result(_products(3), set_name=None), OK)
        self.assertEqual(classify_search_result([], set_name=None), REASON_EMPTY)


class DeterministicReasonsMarkImmediatelyTest(unittest.TestCase):
    """Tomorrow's answer is today's answer, so waiting learns nothing."""

    def test_capped_marks_ineligible_on_the_first_miss(self) -> None:
        update = plan_registry_update(REASON_CAPPED, current_miss_count=0, now=NOW)
        self.assertIs(update["tier1_refresh_eligible"], False)
        self.assertEqual(update["tier1_ineligible_reason"], REASON_CAPPED)
        self.assertEqual(update["tier1_ineligible_at"], NOW.isoformat())

    def test_wrong_family_marks_ineligible_on_the_first_miss(self) -> None:
        update = plan_registry_update(REASON_WRONG_FAMILY, current_miss_count=0, now=NOW)
        self.assertIs(update["tier1_refresh_eligible"], False)

    def test_a_recheck_date_is_always_set(self) -> None:
        """No set is excluded permanently -- vendor catalogs change."""
        update = plan_registry_update(REASON_CAPPED, now=NOW)
        self.assertEqual(
            update["tier1_recheck_after"], (NOW + timedelta(days=RECHECK_DAYS)).isoformat()
        )


class TransientReasonsNeedRepetitionTest(unittest.TestCase):
    """A vendor blip must not exclude a good set for a month."""

    def test_a_first_404_only_counts(self) -> None:
        update = plan_registry_update(REASON_404, current_miss_count=0, now=NOW)
        self.assertEqual(update, {"tier1_miss_count": 1})
        self.assertNotIn("tier1_refresh_eligible", update)

    def test_a_second_miss_still_only_counts(self) -> None:
        update = plan_registry_update(REASON_EMPTY, current_miss_count=1, now=NOW)
        self.assertEqual(update, {"tier1_miss_count": 2})

    def test_the_threshold_miss_marks_ineligible(self) -> None:
        update = plan_registry_update(REASON_404, current_miss_count=MISS_THRESHOLD - 1, now=NOW)
        self.assertIs(update["tier1_refresh_eligible"], False)
        self.assertEqual(update["tier1_miss_count"], MISS_THRESHOLD)

    def test_success_resets_the_counter_and_restores_eligibility(self) -> None:
        update = plan_registry_update(OK, current_miss_count=2, now=NOW)
        self.assertEqual(update["tier1_miss_count"], 0)
        self.assertIs(update["tier1_refresh_eligible"], True)
        self.assertIsNone(update["tier1_ineligible_reason"])
        self.assertIsNone(update["tier1_recheck_after"])

    def test_a_clean_success_writes_nothing(self) -> None:
        """No patch for the overwhelmingly common case."""
        self.assertIsNone(plan_registry_update(OK, current_miss_count=0, now=NOW))


class NothingLeaksIntoTier3Test(unittest.TestCase):
    """The whole point: tier-1 ineligible is not a failure marker.

    These sets are exactly the ones tier-3 should own -- it fetches by
    console_uid + CSV and the search cap does not apply to it. If a tier-1
    miss ever wrote tier3_failure_count, three of them would park a set out
    of the tier-3 rotation permanently, which is the opposite of intended.
    """

    def test_no_plan_ever_touches_a_tier3_column(self) -> None:
        for reason in (OK, REASON_404, REASON_EMPTY, REASON_CAPPED, REASON_WRONG_FAMILY):
            for misses in (0, 1, MISS_THRESHOLD - 1, MISS_THRESHOLD):
                with self.subTest(reason=reason, misses=misses):
                    update = plan_registry_update(reason, current_miss_count=misses, now=NOW) or {}
                    offenders = [k for k in update if k.startswith("tier3")]
                    self.assertEqual(offenders, [], f"tier-3 state written by a tier-1 miss: {offenders}")

    def test_no_plan_writes_a_global_failure_marker(self) -> None:
        for reason in (REASON_404, REASON_EMPTY, REASON_CAPPED, REASON_WRONG_FAMILY):
            with self.subTest(reason=reason):
                update = plan_registry_update(reason, current_miss_count=MISS_THRESHOLD, now=NOW)
                for forbidden in ("last_fetch_status", "failure_count", "tier3_failure_count"):
                    self.assertNotIn(forbidden, update)

    def test_every_written_key_is_a_tier1_key(self) -> None:
        for reason in (OK, REASON_404, REASON_CAPPED):
            update = plan_registry_update(reason, current_miss_count=MISS_THRESHOLD, now=NOW) or {}
            for key in update:
                self.assertTrue(key.startswith("tier1_"), f"unexpected column written: {key}")


class MigrationMatchesTheCodeTest(unittest.TestCase):
    def test_every_column_the_planner_writes_exists_in_the_migration(self) -> None:
        import pathlib

        sql = pathlib.Path(
            "database/migrations/20260908_tier1_refresh_eligibility.sql"
        ).read_text()
        written = set()
        for reason in (OK, REASON_404, REASON_CAPPED, REASON_WRONG_FAMILY):
            for misses in (0, MISS_THRESHOLD):
                written |= set((plan_registry_update(reason, current_miss_count=misses, now=NOW) or {}))
        for column in written:
            with self.subTest(column=column):
                self.assertIn(column, sql, f"{column} is written but never added by the migration")


if __name__ == "__main__":
    unittest.main()
