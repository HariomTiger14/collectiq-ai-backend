"""Seeding pricecharting_current_price from the catalog.

The seed has to happen while pricecharting_catalog still holds the current
cents -- once the writers stop updating it (PR 4) the copy is stale and the new
table is the only source. So this runs before that switch, and it must be safe
to run against a live, actively-written catalog.

Same chunk plan, checkpoint and idempotency machinery as the price-history
backfill, which is deliberate: that one ran 104 chunks and 14.4M rows against
production with 0 duplicates and 0 ops errors, and reusing it means reusing
guards that have already been proven rather than writing new ones.
"""

from __future__ import annotations

import pathlib
import unittest

from scripts.backfill_current_price_from_catalog import (
    COUNT_SQL,
    INSERT_SQL,
    parse_args,
)
from scripts.backfill_price_history_from_scd2 import PLAN_PATH, load_plan, select_chunks

SOURCE_TABLE = "pricecharting_catalog"
TARGET_TABLE = "pricecharting_current_price"


class TheWriteIsNarrowTest(unittest.TestCase):
    def test_it_inserts_into_exactly_one_table(self) -> None:
        source = pathlib.Path(
            "scripts/backfill_current_price_from_catalog.py").read_text()
        statements = [line for line in source.splitlines()
                      if "insert into" in line.lower()]
        self.assertEqual(len(statements), 1, f"more than one insert: {statements}")
        self.assertIn(TARGET_TABLE, statements[0])

    def test_the_catalog_is_only_ever_read(self) -> None:
        """Checked against the SQL, not the file: an earlier version of this
        matched the words "DO UPDATE" in the module docstring explaining why
        the conflict action is DO NOTHING, which is prose, not a statement."""
        for sql in (INSERT_SQL, COUNT_SQL):
            lowered = sql.lower()
            for verb in ("update ", "delete from", "truncate", "drop "):
                with self.subTest(verb=verb.strip(), sql=sql.split()[0]):
                    self.assertNotIn(verb, lowered)
        self.assertIn(f"from public.{SOURCE_TABLE}", INSERT_SQL)
        self.assertNotIn(f"into public.{SOURCE_TABLE}", INSERT_SQL)

    def test_writing_requires_an_explicit_flag(self) -> None:
        self.assertFalse(parse_args([]).commit)
        self.assertTrue(parse_args(["--commit"]).commit)


class ItNeverOverwritesALiveRowTest(unittest.TestCase):
    """DO NOTHING, not DO UPDATE.

    A row already in current_price was written by the daily pipeline and is
    newer than the catalog copy this reads. DO UPDATE would walk today's price
    backwards to whatever the catalog happened to hold.
    """

    def test_the_conflict_action_is_do_nothing(self) -> None:
        self.assertIn("on conflict (pricecharting_id) do nothing", INSERT_SQL.lower())

    def test_it_does_not_update_on_conflict(self) -> None:
        self.assertNotIn("do update", INSERT_SQL.lower())

    def test_the_dry_run_counts_only_rows_it_would_actually_insert(self) -> None:
        """Counting every catalog row would report work the insert will skip."""
        self.assertIn("not exists", COUNT_SQL.lower())
        self.assertIn(TARGET_TABLE, COUNT_SQL)


class TheCopiedShapeTest(unittest.TestCase):
    def test_every_price_column_is_carried(self) -> None:
        for column in ("loose_price_cents", "cib_price_cents", "new_price_cents",
                       "graded_price_cents", "box_only_price_cents",
                       "manual_only_price_cents"):
            with self.subTest(column=column):
                self.assertGreaterEqual(
                    INSERT_SQL.count(column), 2,
                    f"{column} must appear in both the column list and the select")

    def test_the_browse_keys_are_carried(self) -> None:
        """Without these the browse indexes cannot live on the new table."""
        for column in ("category", "platform_group"):
            with self.subTest(column=column):
                self.assertGreaterEqual(INSERT_SQL.count(column), 2)

    def test_currency_defaults_rather_than_arriving_null(self) -> None:
        self.assertIn("coalesce(c.currency, 'USD')", INSERT_SQL)

    def test_observed_at_prefers_the_vendor_timestamp(self) -> None:
        """source_downloaded_at is when the vendor observed the price;
        updated_at is when we happened to write it. Only the first is
        comparable with pricecharting_price_history.observed_at."""
        self.assertIn("coalesce(c.source_downloaded_at, c.updated_at)", INSERT_SQL)


class ItReusesTheProvenChunkMachineryTest(unittest.TestCase):
    def test_it_uses_the_same_validated_plan(self) -> None:
        plan = load_plan(PLAN_PATH)
        self.assertEqual(len(plan), 104)
        self.assertIsNone(plan[0]["lo"])
        self.assertIsNone(plan[-1]["hi"])

    def test_bounds_are_half_open_in_both_directions(self) -> None:
        self.assertIn(">= %(lo)s", INSERT_SQL)
        self.assertIn("<  %(hi)s", INSERT_SQL)

    def test_an_absent_bound_widens_rather_than_excludes(self) -> None:
        self.assertIn("%(lo)s::text is null or", INSERT_SQL)
        self.assertIn("%(hi)s::text is null or", INSERT_SQL)

    def test_it_has_the_absolute_ceiling_too(self) -> None:
        """The lesson from the price-history run: --limit-chunks alone
        advances on a re-run and silently exceeded an approved bound."""
        plan = [{"seq": seq, "lo": None, "hi": None, "versions": 0}
                for seq in range(1, 11)]
        args = parse_args(["--stop-after-chunk", "3"])
        first = [c["seq"] for c in select_chunks(plan, set(), args)]
        self.assertEqual(first, [1, 2, 3])
        self.assertEqual(select_chunks(plan, set(first), args), [])

    def test_its_checkpoint_is_its_own(self) -> None:
        """Sharing the price-history checkpoint would mark this backfill
        complete before it had written anything."""
        from scripts.backfill_current_price_from_catalog import CHECKPOINT_PATH as mine
        from scripts.backfill_price_history_from_scd2 import CHECKPOINT_PATH as theirs

        self.assertNotEqual(mine, theirs)


if __name__ == "__main__":
    unittest.main()
