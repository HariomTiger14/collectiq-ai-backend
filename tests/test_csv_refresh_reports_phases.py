"""The CSV refresh must write its phase timings to the ledger.

SupabaseCatalogClient has accumulated phase_seconds since #214, and #221 split
the three lookup timers apart so the ~65% attributed to "scd2_comparison"
could be attributed properly. Neither reached this script's summary, so two
full production runs -- Saturday's manual five-file and Sunday's scheduled
night -- measured everything and discarded it at exit.

The cost was not the code. It was two nights of the SCD2-perf work having no
evidence to aim at, on a job that runs once a day.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import scripts.refresh_pricecharting_catalog as module

PHASES = {
    "scd2_comparison": 312.51,
    "scd2_history_lookup": 210.4,
    "catalog_metadata_lookup": 71.9,
    "current_price_lookup": 30.2,
    "catalog_upsert": 12.89,
    "current_price_upsert": 68.9,
    "price_snapshot_insert": 125.85,
    "scd2_insert": 0.0,
}


class _Client:
    phase_seconds = dict(PHASES)
    current_price_stats = {"upserted": 2657, "priceChanged": 2657, "browseKeysOnly": 0}


SOURCE = {"source": "pokemon.csv", "inputRows": 10, "validRows": 10,
          "importedRows": 4, "historyRows": 4, "archivePath": None,
          "failed": False, "failureReason": None}


def _summary(client=None, source=None):
    buffer = io.StringIO()
    with patch.object(module, "SupabaseCatalogClient", lambda **kw: client or _Client()), \
         patch.object(module, "SharedRateLimiter", lambda *a, **kw: None), \
         patch.object(module, "timeout_retry_summary", lambda c: {}), \
         patch.object(module, "refresh_source", lambda **kw: dict(source or SOURCE)), \
         contextlib.redirect_stdout(buffer):
        module.main(["--sources", "pokemon", "--batch-size", "900"])
    printed = buffer.getvalue()
    starts = [i for i, line in enumerate(printed.splitlines()) if line.startswith("{")]
    body = "\n".join(printed.splitlines()[starts[-1]:])
    return json.loads(body[: body.rindex("}") + 1])


class ThePhasesReachTheLedgerTest(unittest.TestCase):
    def test_the_key_exists_at_all(self) -> None:
        self.assertIn("phaseSeconds", _summary())

    def test_the_three_split_lookups_are_all_there(self) -> None:
        """These are the point. Attributing the ~65% needs all three, since a
        single combined number is what sent the earlier analysis wrong."""
        phases = _summary()["phaseSeconds"]
        for name in ("scd2_history_lookup", "catalog_metadata_lookup",
                     "current_price_lookup"):
            with self.subTest(phase=name):
                self.assertIn(name, phases)

    def test_the_combined_figure_is_kept(self) -> None:
        """Every measurement before the split reports scd2_comparison;
        dropping it would make this run incomparable with all of them."""
        self.assertIn("scd2_comparison", _summary()["phaseSeconds"])

    def test_the_write_phases_come_through_too(self) -> None:
        phases = _summary()["phaseSeconds"]
        for name in ("catalog_upsert", "current_price_upsert",
                     "price_snapshot_insert"):
            with self.subTest(phase=name):
                self.assertIn(name, phases)

    def test_the_values_are_the_client_s_own(self) -> None:
        phases = _summary()["phaseSeconds"]
        self.assertEqual(phases["scd2_history_lookup"], 210.4)
        self.assertEqual(phases["catalog_upsert"], 12.89)

    def test_a_zero_phase_is_reported_not_dropped(self) -> None:
        """An absent key reads as "not measured"; a zero is a fact."""
        self.assertEqual(_summary()["phaseSeconds"]["scd2_insert"], 0.0)

    def test_the_biggest_phase_is_listed_first(self) -> None:
        """Read by a human looking for the bottleneck."""
        phases = _summary()["phaseSeconds"]
        self.assertEqual(list(phases), sorted(phases, key=phases.get, reverse=True))

    def test_currentPrice_still_reported_alongside(self) -> None:
        """The new key must not displace the one that proves a night worked."""
        summary = _summary()
        self.assertEqual(summary["currentPrice"]["upserted"], 2657)


class ItSurvivesAClientThatHasNoTimingsTest(unittest.TestCase):
    """A dry run builds no client, and observability must never be able to
    break the job it observes."""

    def test_a_client_without_phase_seconds_is_not_an_error(self) -> None:
        class _Bare:
            current_price_stats = {}

        self.assertEqual(_summary(client=_Bare())["phaseSeconds"], {})

    def test_a_dry_run_still_produces_a_summary(self) -> None:
        buffer = io.StringIO()
        with patch.object(module, "SharedRateLimiter", lambda *a, **kw: None), \
             patch.object(module, "timeout_retry_summary", lambda c: {}), \
             patch.object(module, "refresh_source", lambda **kw: dict(SOURCE)), \
             contextlib.redirect_stdout(buffer):
            module.main(["--sources", "pokemon", "--dry-run"])
        printed = buffer.getvalue()
        starts = [i for i, line in enumerate(printed.splitlines()) if line.startswith("{")]
        body = "\n".join(printed.splitlines()[starts[-1]:])
        self.assertIn("phaseSeconds", json.loads(body[: body.rindex("}") + 1]))


if __name__ == "__main__":
    unittest.main()
