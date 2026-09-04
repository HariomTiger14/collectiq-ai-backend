"""The shared limiter's job is to stop CONCURRENT callers breaching a limit
that applies to the account, not to any one job.

Measured 2026-09-02: tier3 128 + categories 23 + backfill ~4 = 155 CSV
calls/day against a published 144/day, and those jobs overlap for most of the
day, which doubles the effective rate. Per-script sleeps cannot see that.
"""

import unittest
from unittest import mock

from scripts._shared_rate_limiter import (
    BULK_MAX_SLOT_WAIT_SECONDS,
    CLASS_BACKFILL,
    CLASS_ESSENTIAL_CATEGORIES,
    CLASS_TIER3,
    KICKSDB_API,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)

GRANTED = (True, "GRANTED", 0.0)


def _limited(seconds):
    return (False, "RATE_LIMITED", seconds)


def _blocked(seconds):
    return (False, "POLICY_BLOCKED", seconds)


def _exhausted():
    return (False, "QUOTA_EXHAUSTED", 40_000.0)


def _limiter(**kw):
    kw.setdefault("slot_class", CLASS_TIER3)
    kw.setdefault("supabase_url", "https://db.test")
    kw.setdefault("service_role_key", "k")
    kw.setdefault("fallback_interval_seconds", 600.0)
    return SharedRateLimiter(PRICECHARTING_CSV, **kw)


class SharedRateLimiterTest(unittest.TestCase):
    def test_proceeds_immediately_when_a_slot_is_granted(self):
        slept = []
        rl = _limiter(sleep=slept.append)
        with mock.patch.object(rl, "_ask", return_value=GRANTED):
            rl.acquire()
        self.assertEqual(slept, [])

    def test_waits_the_amount_the_server_asks_for(self):
        slept = []
        rl = _limiter(sleep=slept.append)
        with mock.patch.object(rl, "_ask", side_effect=[_limited(12.5), GRANTED]):
            rl.acquire()
        self.assertEqual(slept, [12.5])

    def test_long_waits_are_chunked_rather_than_one_huge_sleep(self):
        # A wrong interval must not hang a run in a single un-interruptible
        # sleep; the loop re-asks instead.
        slept = []
        rl = _limiter(sleep=slept.append)
        with mock.patch.object(rl, "_ask", side_effect=[_limited(600.0), _limited(300.0), GRANTED]):
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


    def test_quota_exhausted_tells_the_caller_to_skip_rather_than_sleep(self):
        """A bulk class out of daily budget must not sleep until midnight --
        it should give the run back so the container stops billing."""
        slept = []
        rl = _limiter(sleep=slept.append, slot_class=CLASS_BACKFILL)
        with mock.patch.object(rl, "_ask", return_value=_exhausted()):
            self.assertFalse(rl.acquire())
        self.assertEqual(slept, [])

    def test_bulk_gives_up_rather_than_hold_a_container_open(self):
        """Parked behind a 3h40m essential run, waiting costs Render
        wall-clock and buys nothing: both bulk jobs resume from a queue."""
        slept = []
        rl = _limiter(sleep=slept.append, slot_class=CLASS_TIER3)
        with mock.patch.object(rl, "_ask", return_value=_blocked(900.0)):
            self.assertFalse(rl.acquire(max_wait_seconds=1000.0))
        self.assertLessEqual(sum(slept), 1000.0)

    def test_a_policy_block_is_waited_out_when_no_deadline_is_given(self):
        slept = []
        rl = _limiter(sleep=slept.append, slot_class=CLASS_TIER3)
        with mock.patch.object(rl, "_ask", side_effect=[_blocked(90.0), GRANTED]):
            self.assertTrue(rl.acquire())
        self.assertEqual(slept, [60.0])

    def test_essential_callers_wait_indefinitely_by_default(self):
        """Essential jobs have a daily deadline; they wait, they don't bail."""
        slept = []
        rl = _limiter(sleep=slept.append, slot_class=CLASS_ESSENTIAL_CATEGORIES)
        with mock.patch.object(
            rl, "_ask", side_effect=[_limited(610.0)] * 3 + [GRANTED]
        ):
            self.assertTrue(rl.acquire())
        self.assertEqual(len(slept), 3)

    def test_the_class_is_sent_to_the_server(self):
        rl = _limiter(slot_class=CLASS_TIER3)
        captured = {}

        class _Resp:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"granted": True, "reason": "GRANTED", "retry_after_seconds": 0}

        class _Client:
            def __init__(self_inner, *args, **kwargs):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, json, headers):
                captured["url"] = url
                captured["json"] = json
                return _Resp()

        with mock.patch("scripts._shared_rate_limiter.httpx.Client", _Client):
            self.assertTrue(rl.acquire())
        self.assertTrue(captured["url"].endswith("/rpc/acquire_provider_slot"))
        self.assertEqual(captured["json"]["p_class"], CLASS_TIER3)
        self.assertEqual(captured["json"]["p_key"], PRICECHARTING_CSV)

    def test_bulk_wait_ceiling_exceeds_the_normal_interval(self):
        """A ceiling below the interval would abort every ordinary wait."""
        self.assertGreater(BULK_MAX_SLOT_WAIT_SECONDS, 610.0)

    def test_the_registered_keys_are_the_ones_the_migration_creates(self):
        self.assertEqual(PRICECHARTING_CSV, "pricecharting:csv")
        self.assertEqual(KICKSDB_API, "kicksdb:api")


if __name__ == "__main__":
    unittest.main()
