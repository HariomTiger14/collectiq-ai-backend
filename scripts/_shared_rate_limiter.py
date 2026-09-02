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

# A single wait is capped so a wildly wrong interval can't hang a run for
# hours in one sleep; the loop simply re-asks.
_MAX_SINGLE_SLEEP_SECONDS = 60.0


class SharedRateLimiter:
    def __init__(
        self,
        limit_key: str,
        *,
        fallback_interval_seconds: float,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 15.0,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.limit_key = limit_key
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

    def acquire(self) -> None:
        """Block until this caller may make one request."""
        if not self.supabase_url or not self.service_role_key:
            self._acquire_locally()
            return
        while True:
            try:
                wait = self._ask()
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
                return
            if wait <= 0:
                self._last_local_acquire = self._monotonic()
                return
            self._sleep(min(wait, _MAX_SINGLE_SLEEP_SECONDS))

    def _ask(self) -> float:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                f"{self.supabase_url}/rest/v1/rpc/acquire_rate_limit_slot",
                json={"p_key": self.limit_key},
                headers={
                    "apikey": self.service_role_key,
                    "Authorization": f"Bearer {self.service_role_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            return float(response.json())

    def _acquire_locally(self) -> None:
        last = self._last_local_acquire
        if last is not None:
            elapsed = self._monotonic() - last
            if elapsed < self.fallback_interval_seconds:
                self._sleep(self.fallback_interval_seconds - elapsed)
        self._last_local_acquire = self._monotonic()
