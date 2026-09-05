"""Account-wide rate limiting for external providers.

A provider's limit applies to the ACCOUNT -- the API token identifies the
subscription, not the caller -- but pacing used to live inside each script.
Three jobs each sleeping the full interval still breach the limit whenever
they overlap, and they do: completed-categories runs ~3.8h from 04:45 while
the tier-3 rotation runs 2.7h out of every 3.

acquire() asks Postgres for a slot. The RPC serialises callers with FOR
UPDATE on a single row, so two jobs asking at the same instant cannot both be
granted. Callers sleep and re-ask rather than being queued server-side, so a
caller that dies mid-wait holds nothing.

Serialising was not enough on its own. The RPC is a race, not a queue:
whoever polls first after the interval expires wins. Two jobs that must
finish daily were competing against ~120 bulk runs and winning about 2% of
races, which left the 23-call categories refresh completing 4.5 calls a day.
So each caller now declares a CLASS, and the transaction decides which class
may take the next slot (see 20260905_provider_slot_classes.sql).

An essential job announces itself by ASKING -- there is no reservation to
take out and none to clean up. Its liveness timestamp is updated even when
the ask is refused, so a job waiting out the interval still holds bulk off
rather than losing the slot it is waiting for. If it dies, the timestamp
goes stale on its own and bulk resumes.

Failure policy is deliberately the opposite of the ops recorder's. That module
must never break a job, so it swallows errors and carries on. Here, carrying
on would mean calling the provider without a slot -- the exact breach this
exists to prevent. So a database problem degrades to LOCAL pacing at the full
interval: slower than necessary if another job is idle, never faster than the
published limit. Wrong in the safe direction.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

# Registered in provider_rate_limits by the 20260902 migration.
PRICECHARTING_CSV = "pricecharting:csv"
KICKSDB_API = "kicksdb:api"

# Allocation classes, registered in provider_slot_classes by 20260905. The
# class a job declares decides its priority; see that migration for what
# each one is entitled to.
CLASS_ESSENTIAL_CATEGORIES = "essential_categories"
CLASS_ESSENTIAL_CATALOG = "essential_catalog"
CLASS_BACKFILL = "backfill"
CLASS_TIER3 = "tier3"

# A single wait is capped so a wildly wrong interval can't hang a run for
# hours in one sleep; the loop simply re-asks. The server returns the true
# remaining wait each time, so the chunks converge exactly on the boundary
# rather than overshooting it -- 610s becomes 60x10 then 10, and the caller
# is asking at the moment the slot opens. No tight polling is needed for
# that: priority is decided in the transaction, not by who wakes first.
_MAX_SINGLE_SLEEP_SECONDS = 60.0

# How long a BULK caller will wait for a slot before ending its run instead.
# Render crons bill wall-clock, so a container parked behind the 3h40m
# categories refresh is paying to do nothing. Both bulk jobs resume from a
# persistent queue, so stopping early costs nothing but a little latency.
# Must comfortably exceed the interval or normal waits would abort.
BULK_MAX_SLOT_WAIT_SECONDS = 1800.0


class SharedRateLimiter:
    def __init__(
        self,
        limit_key: str,
        *,
        fallback_interval_seconds: float,
        slot_class: str | None = None,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 15.0,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.limit_key = limit_key
        self.slot_class = slot_class
        self.fallback_interval_seconds = fallback_interval_seconds
        self.supabase_url = (supabase_url or os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
        self.service_role_key = (
            service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        ).strip()
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_local_acquire: float | None = None
        self.degraded_to_local = False
        self.quota_exhausted = False

    def acquire(self, *, max_wait_seconds: float | None = None) -> bool:
        """Block until this caller may make one request.

        Returns True when a slot was granted. Returns False when the caller
        should SKIP its work rather than wait: the class is out of daily
        budget, or waiting would exceed max_wait_seconds.

        max_wait_seconds matters because these are Render cron containers
        billed by wall-clock. A bulk job parked behind a 3h40m essential run
        is burning money to do nothing; it is cheaper to end the run and let
        the next scheduled one pick up where the queue left off.
        """
        if not self.supabase_url or not self.service_role_key:
            self._acquire_locally()
            return True
        waited = 0.0
        while True:
            try:
                granted, reason, retry_after = self._ask()
            except Exception as exc:  # noqa: BLE001 -- see module docstring
                if not self.degraded_to_local:
                    print(
                        f"  Rate limiter unavailable ({exc}); falling back to local "
                        f"pacing at {self.fallback_interval_seconds}s. This is slower "
                        f"than the shared limiter, never faster.",
                        flush=True,
                    )
                    self.degraded_to_local = True
                self._acquire_locally()
                return True
            if granted:
                self._last_local_acquire = self._monotonic()
                return True
            if reason == "QUOTA_EXHAUSTED":
                if not self.quota_exhausted:
                    print(
                        f"  {self.slot_class} has used its daily {self.limit_key} "
                        f"budget; skipping the rest of this run's calls.",
                        flush=True,
                    )
                    self.quota_exhausted = True
                return False
            if max_wait_seconds is not None and waited + retry_after > max_wait_seconds:
                print(
                    f"  {self.slot_class} would wait {retry_after:.0f}s more for a "
                    f"{self.limit_key} slot ({reason}); ending this run instead of "
                    f"holding a container open.",
                    flush=True,
                )
                return False
            nap = min(retry_after, _MAX_SINGLE_SLEEP_SECONDS)
            waited += nap
            self._sleep(nap)

    def _ask(self) -> tuple[bool, str, float]:
        """Returns (granted, reason, retry_after_seconds).

        The server distinguishes "the interval has not elapsed" from "another
        class has priority right now" from "you are out of budget for the
        day", because the right response differs: wait a known amount, wait
        and re-ask, or stop.
        """
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                f"{self.supabase_url}/rest/v1/rpc/acquire_provider_slot",
                json={"p_key": self.limit_key, "p_class": self.slot_class},
                headers={
                    "apikey": self.service_role_key,
                    "Authorization": f"Bearer {self.service_role_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        return (
            bool(payload["granted"]),
            str(payload.get("reason", "")),
            float(payload.get("retry_after_seconds") or 0.0),
        )

    def _acquire_locally(self) -> None:
        last = self._last_local_acquire
        if last is not None:
            elapsed = self._monotonic() - last
            if elapsed < self.fallback_interval_seconds:
                self._sleep(self.fallback_interval_seconds - elapsed)
        self._last_local_acquire = self._monotonic()
