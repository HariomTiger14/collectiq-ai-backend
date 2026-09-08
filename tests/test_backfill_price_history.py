"""The one-time SCD2 -> snapshot backfill is a 15M-row production write.

It is additive and duplicate-safe, but it is still large, so the properties
that make it safe are pinned here rather than left to a careful reading of the
script: it writes exactly one table, reads exactly one other, every insert ends
in ON CONFLICT DO NOTHING, every row carries a rollback marker, and the chunk
plan covers the id space with no gap and no overlap.

The plan guard is not theoretical. While preparing this, a second exploratory
query overwrote the plan file with a chunk 1 bounded below at '1' instead of
unbounded. Ids sorting below that would have been skipped silently -- the run
refused to start instead. That is the failure this file exists to keep catching.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.backfill_price_history_from_scd2 import (
    COUNT_SQL,
    INSERT_SQL,
    PLAN_PATH,
    SOURCE_FILE_MARKER,
    load_checkpoint,
    load_plan,
    parse_args,
    record_checkpoint,
)

SOURCE_TABLE = "pricecharting_catalog_history"
TARGET_TABLE = "pricecharting_price_history"


class TheWriteIsNarrowTest(unittest.TestCase):
    """What this script is allowed to touch."""

    def test_it_inserts_into_exactly_one_table(self) -> None:
        source = pathlib.Path("scripts/backfill_price_history_from_scd2.py").read_text()
        statements = [line for line in source.splitlines()
                      if "insert into" in line.lower()]
        self.assertEqual(len(statements), 1, f"more than one insert: {statements}")
        self.assertIn(TARGET_TABLE, statements[0])

    def test_it_never_writes_the_catalog_or_the_scd2_table(self) -> None:
        source = pathlib.Path("scripts/backfill_price_history_from_scd2.py").read_text().lower()
        for verb in ("update ", "delete from", "truncate", "drop "):
            with self.subTest(verb=verb.strip()):
                self.assertNotIn(verb, source)

    def test_the_source_table_is_only_ever_read(self) -> None:
        self.assertIn(f"from public.{SOURCE_TABLE}", INSERT_SQL)
        self.assertNotIn(f"into public.{SOURCE_TABLE}", INSERT_SQL)

    def test_every_insert_is_duplicate_safe(self) -> None:
        """Without this an interrupted run cannot simply be re-run."""
        self.assertIn("on conflict (pricecharting_id, observed_at) do nothing",
                      INSERT_SQL.lower())

    def test_every_backfilled_row_carries_the_rollback_marker(self) -> None:
        """Rollback is `delete ... where source_file = marker`; it only works
        if the marker is on the rows and on nothing else."""
        self.assertEqual(SOURCE_FILE_MARKER, "backfill-from-scd2")
        self.assertIn(f"'{SOURCE_FILE_MARKER}'", INSERT_SQL)

    def test_writing_requires_an_explicit_flag(self) -> None:
        self.assertFalse(parse_args([]).commit)
        self.assertTrue(parse_args(["--commit"]).commit)


class TheDefinitionOfAPricePointTest(unittest.TestCase):
    """A version counts only if its price tuple moved. Metadata-only versions
    (4.4% of the sampled window) carry no new price and are skipped."""

    def test_all_seven_price_fields_take_part_in_the_comparison(self) -> None:
        for field in ("loose_price_cents", "cib_price_cents", "new_price_cents",
                      "graded_price_cents", "box_only_price_cents",
                      "manual_only_price_cents", "currency"):
            with self.subTest(field=field):
                self.assertGreaterEqual(
                    INSERT_SQL.count(field), 3,
                    f"{field} must appear in the lag(), the comparison and the insert")

    def test_the_first_version_of_an_item_always_counts(self) -> None:
        self.assertIn("p.prev is null", INSERT_SQL)

    def test_nulls_compare_as_values_not_as_unknown(self) -> None:
        """`!=` would drop every point where a price went null or came back."""
        self.assertIn("is distinct from", INSERT_SQL)
        self.assertNotIn("p.prev !=", INSERT_SQL)

    def test_ordering_is_by_valid_from_within_an_item(self) -> None:
        self.assertIn("partition by h.pricecharting_id order by h.valid_from",
                      INSERT_SQL)

    def test_the_counting_query_uses_the_same_definition(self) -> None:
        """A dry run that counted differently from the write would be a lie."""
        for fragment in ("p.prev is null", "is distinct from",
                         "partition by h.pricecharting_id order by h.valid_from"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, COUNT_SQL)


class TheChunkPlanCoversEveryIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = load_plan(PLAN_PATH)

    def test_the_shipped_plan_validates(self) -> None:
        self.assertEqual(len(self.plan), 104)

    def test_it_is_unbounded_at_both_ends(self) -> None:
        """The regression: a chunk 1 bounded at '1' skips anything below it."""
        self.assertIsNone(self.plan[0]["lo"])
        self.assertIsNone(self.plan[-1]["hi"])

    def test_bounds_are_half_open_and_contiguous(self) -> None:
        for earlier, later in zip(self.plan, self.plan[1:]):
            with self.subTest(seq=earlier["seq"]):
                self.assertEqual(earlier["hi"], later["lo"])

    def test_a_gap_is_refused(self) -> None:
        broken = [dict(chunk) for chunk in self.plan]
        broken[3]["hi"] = "ZZZ"
        with self.assertRaises(SystemExit) as caught:
            load_plan(self._written(broken))
        self.assertIn("gap or overlap", str(caught.exception))

    def test_a_bounded_first_chunk_is_refused(self) -> None:
        broken = [dict(chunk) for chunk in self.plan]
        broken[0]["lo"] = "1"
        with self.assertRaises(SystemExit):
            load_plan(self._written(broken))

    def test_a_truncated_plan_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            load_plan(self._written([dict(chunk) for chunk in self.plan[:5]]))

    def test_the_query_uses_half_open_bounds_in_both_directions(self) -> None:
        self.assertIn(">= %(lo)s", INSERT_SQL)
        self.assertIn("<  %(hi)s", INSERT_SQL)

    def test_an_absent_bound_means_unbounded_not_no_rows(self) -> None:
        """`lo is null` must widen the range, not exclude everything."""
        self.assertIn("%(lo)s::text is null or", INSERT_SQL)
        self.assertIn("%(hi)s::text is null or", INSERT_SQL)

    def _written(self, plan) -> pathlib.Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(plan, handle)
        handle.close()
        return pathlib.Path(handle.name)


class ResumeAfterInterruptionTest(unittest.TestCase):
    def test_a_missing_checkpoint_starts_from_the_beginning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_checkpoint(pathlib.Path(directory) / "absent"), set())

    def test_recorded_chunks_are_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "checkpoint"
            for seq in (1, 2, 5):
                record_checkpoint(path, seq)
            self.assertEqual(load_checkpoint(path), {1, 2, 5})

    def test_the_checkpoint_is_appended_not_rewritten(self) -> None:
        """A rewrite loses everything if the process dies mid-write."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "checkpoint"
            record_checkpoint(path, 1)
            record_checkpoint(path, 2)
            self.assertEqual(path.read_text().split(), ["1", "2"])

    def test_a_bounded_run_can_be_asked_for(self) -> None:
        """The first production run is deliberately three chunks."""
        self.assertEqual(parse_args(["--limit-chunks", "3"]).limit_chunks, 3)
        self.assertIsNone(parse_args([]).limit_chunks)


if __name__ == "__main__":
    unittest.main()
