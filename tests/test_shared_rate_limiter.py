"""The shared limiter's job is to stop CONCURRENT callers breaching a limit
that applies to the account, not to any one job.

Measured 2026-09-02: tier3 128 + categories 23 + backfill ~4 = 155 CSV
calls/day against a published 144/day, and those jobs overlap for most of the
day, which doubles the effective rate. Per-script sleeps cannot see that.
"""

import unittest
from unittest import mock

from scripts._shared_rate_limiter import (
    KICKSDB_API,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)


def _limiter(**kw):
    kw.setdefault("supabase_url", "https://db.test")
    kw.setdefault("service_role_key", "k")
    kw.setdefault("fallback_interval_seconds", 600.0)
    return SharedRateLimiter(PRICECHARTING_CSV, **kw)


class SharedRateLimiterTest(unittest.TestCase):
    def test_proceeds_immediately_when_a_slot_is_granted(self):
        slept = []
        rl = _limiter(sleep=slept.append)
        with mock.patch.object(rl, "_ask", return_value=0.0):
            rl.acquire()
        self.assertEqual(slept, [])

    def test_waits_the_amount_the_server_asks_for(self):
        slept = []
        rl = _limiter(sleep=slept.append)
        with mock.patch.object(rl, "_ask", side_effect=[12.5, 0.0]):
            rl.acquire()
        self.assertEqual(slept, [12.5])

    def test_long_waits_are_chunked_rather_than_one_huge_sleep(self):
        # A wrong interval must not hang a run in a single un-interruptible
        # sleep; the loop re-asks instead.
        slept = []
        rl = _limiter(sleep=slept.append)
        with mock.patch.object(rl, "_ask", side_effect=[600.0, 300.0, 0.0]):
            rl.acquire()
        self.assertTrue(all(s <= 60.0 for s in slept), slept)
        self.assertEqual(len(slept), 2)

    def test_database_failure_degrades_to_local_pacing_not_to_no_pacing(self):
        """Failing open would call the provider without a slot -- the exact
        breach this exists to prevent."""
        slept, clock = [], [0.0]
        rl = _limiter(sleep=slept.append, monotonic=lambda: clock[0])
        with mock.patch.object(rl, "_ask", side_effect=RuntimeError("db down")):
            rl.acquire()          # first call: nothing to wait for
            self.assertEqual(slept, [])
            clock[0] = 10.0
            rl.acquire()          # 10s later, interval is 600s -> wait 590s
        self.assertTrue(rl.degraded_to_local)
        self.assertEqual(slept, [590.0])

    def test_missing_credentials_fall_back_locally_without_calling_out(self):
        slept, clock = [], [0.0]
        rl = SharedRateLimiter(
            KICKSDB_API,
            fallback_interval_seconds=1.0,
            supabase_url="",
            service_role_key="",
            sleep=slept.append,
            monotonic=lambda: clock[0],
        )
        with mock.patch.object(rl, "_ask", side_effect=AssertionError("must not be called")):
            rl.acquire()
            clock[0] = 0.25
            rl.acquire()
        self.assertEqual(slept, [0.75])

    def test_the_registered_keys_are_the_ones_the_migration_creates(self):
        self.assertEqual(PRICECHARTING_CSV, "pricecharting:csv")
        self.assertEqual(KICKSDB_API, "kicksdb:api")


if __name__ == "__main__":
    unittest.main()
