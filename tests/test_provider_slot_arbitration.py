"""Acceptance tests for the slot arbiter, run against real Postgres.

The arbitration lives in PL/pgSQL, so mocking it proves nothing. These build
the objects in a throwaway schema, exercise the policy, and drop it -- public
is never touched. Time is advanced by rewriting last_acquired_at /
last_request_at rather than moving the clock, since now() is transaction time.

Skipped unless RUN_DB_TESTS=1, because it needs a live connection. Run it
before enabling the tier-3 rotation:

    RUN_DB_TESTS=1 .venv/bin/python -m pytest -q tests/test_provider_slot_arbitration.py -s
"""

import os
import pathlib
import re
import unittest
import uuid

if os.getenv("RUN_DB_TESTS") == "1":
    import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = "pricecharting:csv"


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DATABASE_URL"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _ddl(schema: str) -> str:
    sql = "\n".join(
        (ROOT / "database/migrations" / name).read_text()
        for name in ("20260902_provider_rate_limits.sql", "20260905_provider_slot_classes.sql")
    )
    sql = re.sub(r"\bpublic\.", f"{schema}.", sql)
    sql = re.sub(r"set search_path = public", f"set search_path = {schema}", sql)
    return re.sub(r"(revoke all|grant \w+).*?;", "", sql, flags=re.S | re.I)


@unittest.skipUnless(os.getenv("RUN_DB_TESTS") == "1", "needs a live Postgres")
class ProviderSlotArbitrationTest(unittest.TestCase):
    def setUp(self):
        self.schema = f"rl_test_{uuid.uuid4().hex[:8]}"
        self.url = _database_url()
        self.assertTrue(self.url, "DATABASE_URL is required for this test")
        self.conn = psycopg.connect(self.url, autocommit=True)
        self.cur = self.conn.cursor()
        self.cur.execute(f"create schema {self.schema}")
        self.cur.execute(_ddl(self.schema))
        self.addCleanup(self.conn.close)
        self.addCleanup(
            lambda: self.cur.execute(f"drop schema if exists {self.schema} cascade")
        )

    # -- helpers -------------------------------------------------------
    def ask(self, cls, cur=None):
        (cur or self.cur).execute(
            f"select {self.schema}.acquire_provider_slot(%s, %s)", (KEY, cls)
        )
        return (cur or self.cur).fetchone()[0]

    def open_slot(self, seconds=700):
        """Pretend the last grant was long enough ago that a slot is free."""
        self.cur.execute(
            f"update {self.schema}.provider_rate_limits set last_acquired_at = "
            f"now() - make_interval(secs => %s) where limit_key = %s",
            (seconds, KEY),
        )

    def set_class(self, cls, **cols):
        """Seeded rows have quota_date NULL, so the first ask of the day
        legitimately zeroes used_today. Stamp today's date alongside any
        counter a test sets, or the policy under test never sees it."""
        assigns = ", ".join(f"{k} = %s" for k in cols)
        self.cur.execute(
            f"update {self.schema}.provider_slot_classes set {assigns}, "
            f"quota_date = (now() at time zone 'utc')::date where class = %s",
            (*cols.values(), cls),
        )

    def age_request(self, cls, seconds):
        self.cur.execute(
            f"update {self.schema}.provider_slot_classes set last_request_at = "
            f"now() - make_interval(secs => %s) where class = %s",
            (seconds, cls),
        )

    # -- acceptance criteria -------------------------------------------
    def test_essential_wins_the_slot_a_bulk_job_is_also_asking_for(self):
        self.open_slot()
        self.age_request("essential_categories", 10)
        blocked = self.ask("tier3")
        self.assertFalse(blocked["granted"])
        self.assertEqual(blocked["reason"], "POLICY_BLOCKED")
        self.assertTrue(self.ask("essential_categories")["granted"])

    def test_an_essential_job_waiting_out_the_interval_still_holds_bulk_off(self):
        """The liveness signal is recorded on a REFUSED ask too. Without that
        the essential job would go stale while sleeping and lose the slot it
        was waiting for."""
        self.open_slot(100)
        self.assertFalse(self.ask("essential_categories")["granted"])  # rate limited
        self.assertEqual(self.ask("tier3")["reason"], "RATE_LIMITED")
        self.open_slot(700)
        self.assertEqual(self.ask("tier3")["reason"], "POLICY_BLOCKED")

    def test_a_dead_essential_container_stops_blocking_on_its_own(self):
        """No reaping: the timestamp simply goes stale."""
        self.open_slot()
        self.age_request("essential_categories", 901)
        self.assertTrue(self.ask("tier3")["granted"])

    def test_an_essential_job_at_its_entitlement_stops_blocking_bulk(self):
        self.open_slot()
        self.set_class("essential_categories", used_today=23)
        self.cur.execute(
            f"update {self.schema}.provider_slot_classes set last_request_at = now() "
            f"where class = 'essential_categories'"
        )
        self.assertTrue(self.ask("tier3")["granted"])

    def test_an_essential_job_is_not_denied_past_its_entitlement(self):
        """The reservation is a priority floor, not a cap. Denying the 24th
        call because a set count grew would break the daily refresh."""
        self.open_slot()
        self.set_class("essential_categories", used_today=23)
        self.assertTrue(self.ask("essential_categories")["granted"])

    def test_backfill_is_hard_capped_while_tier3_keeps_going(self):
        self.open_slot()
        self.set_class("backfill", used_today=30)
        self.assertEqual(self.ask("backfill")["reason"], "QUOTA_EXHAUSTED")
        self.assertTrue(self.ask("tier3")["granted"])

    def test_counters_reset_on_the_utc_day_rollover(self):
        self.open_slot()
        self.cur.execute(
            f"update {self.schema}.provider_slot_classes set used_today = 30, "
            f"quota_date = (now() at time zone 'utc')::date - 1 where class = 'backfill'"
        )
        self.assertTrue(self.ask("backfill")["granted"])

    def test_two_concurrent_callers_cannot_both_be_granted(self):
        self.open_slot()
        with psycopg.connect(self.url) as c1, psycopg.connect(self.url) as c2:
            k1, k2 = c1.cursor(), c2.cursor()
            first = self.ask("tier3", cur=k1)
            c1.commit()
            second = self.ask("tier3", cur=k2)
            c2.commit()
        self.assertTrue(first["granted"])
        self.assertFalse(second["granted"])

    def test_an_unregistered_class_raises_rather_than_being_allowed(self):
        """Silently allowing an unknown caller is the failure mode this
        whole mechanism exists to prevent."""
        self.open_slot()
        with self.assertRaises(psycopg.errors.RaiseException):
            self.ask("not_a_real_class")

    def test_a_key_with_no_classes_still_gets_the_safety_gate(self):
        """kicksdb has no allocation policy, only an interval."""
        self.cur.execute(
            f"select {self.schema}.acquire_provider_slot('kicksdb:api', null)"
        )
        self.assertTrue(self.cur.fetchone()[0]["granted"])
        self.cur.execute(
            f"select {self.schema}.acquire_provider_slot('kicksdb:api', null)"
        )
        self.assertEqual(self.cur.fetchone()[0]["reason"], "RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()
