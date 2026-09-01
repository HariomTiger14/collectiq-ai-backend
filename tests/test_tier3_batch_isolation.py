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

import unittest
from unittest import mock

import httpx

from scripts.refresh_sportscardspro_rotation import (
    TIER3_MAX_FAILURES,
    _isolate_failed_batch,
    _ThrottleAbort,
)
from scripts.backfill_pricecharting_sets import _Counter


def _rows(*uids):
    return [{"registry_id": f"id-{u}", "console_uid": u, "set_name": f"Set {u}"} for u in uids]


def _isolate(rows, dead_uids, *, status=503, budget=32):
    """Run isolation against a fake endpoint that 503s any request whose
    console_uid list contains a dead uid -- the real failure semantics."""
    calls = []

    def fake_fetch(http, *, base_url, token, console_uids, rate_limit_counter,
                   blocked_counter, status_sink=None):
        calls.append(list(console_uids))
        if any(u in dead_uids for u in console_uids):
            if status_sink is not None:
                status_sink.append(status)
            return None
        return "csv:" + ",".join(console_uids)

    with mock.patch(
        "scripts.refresh_sportscardspro_rotation.fetch_batch_csv", fake_fetch
    ):
        texts, dead = _isolate_failed_batch(
            mock.MagicMock(spec=httpx.Client),
            base_url="https://example.test",
            token="t",
            rows=rows,
            sleep_seconds=0,
            rate_limit_counter=_Counter(),
            blocked_counter=_Counter(),
            budget=[budget],
        )
    return texts, dead, calls


class Tier3BatchIsolationTest(unittest.TestCase):
    def test_single_dead_set_is_pinpointed_and_the_rest_salvaged(self):
        rows = _rows("A", "B", "C", "D", "E", "F", "G", "H")
        texts, dead, _ = _isolate(rows, {"E"})

        self.assertEqual([r["console_uid"] for r in dead], ["E"])
        recovered = {u for t in texts for u in t.removeprefix("csv:").split(",")}
        # Every healthy set comes back; previously all 8 were discarded.
        self.assertEqual(recovered, {"A", "B", "C", "D", "F", "G", "H"})

    def test_isolation_is_cheaper_than_probing_every_set(self):
        rows = _rows(*"ABCDEFGHIJKLMNOP")  # 16 sets
        _, dead, calls = _isolate(rows, {"K"})

        self.assertEqual([r["console_uid"] for r in dead], ["K"])
        # Halving finds one offender in ~2*log2(n); linear probing would be 16.
        self.assertLess(len(calls), len(rows))

    def test_multiple_dead_sets_all_found(self):
        rows = _rows(*"ABCDEFGH")
        texts, dead, _ = _isolate(rows, {"B", "G"})

        self.assertEqual(sorted(r["console_uid"] for r in dead), ["B", "G"])
        recovered = {u for t in texts for u in t.removeprefix("csv:").split(",")}
        self.assertEqual(recovered, {"A", "C", "D", "E", "F", "H"})

    def test_all_dead_returns_no_csv_and_every_row_dead(self):
        rows = _rows("A", "B")
        texts, dead, _ = _isolate(rows, {"A", "B"})

        self.assertEqual(texts, [])
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
        _, _, calls = _isolate(rows, {"A", "C", "E", "G"}, budget=3)
        self.assertLessEqual(len(calls), 3)

    def test_max_failures_threshold_is_positive(self):
        # The queue filter is `tier3_failure_count lt TIER3_MAX_FAILURES`;
        # a zero threshold would silently empty the rotation.
        self.assertGreater(TIER3_MAX_FAILURES, 0)


if __name__ == "__main__":
    unittest.main()
