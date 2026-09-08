"""Shared PostgREST/Storage access for staged catalog refresh batches.

Extracted from the downloader when the ingester needed the same access. Two
copies of this would drift, and this pipeline already has enough places where
two things have to agree -- the migration and the status constants, the
downloader and the ingester, the run summary and the batch row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from scripts.catalog_batches import BUCKET, INGEST_FAILED, INGESTING, VALIDATED


class BatchStore:
    """PostgREST access to catalog_download_batches and the registry."""

    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float):
        self.base = supabase_url.rstrip("/")
        self.key = service_role_key
        self.timeout = timeout_seconds
        if not self.base or not self.key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}", **extra}

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout)

    def claim_due_sets(self, *, source: str, limit: int) -> list[dict[str, Any]]:
        """The rotation's own queue predicate, unchanged.

        Ordering by tier3_refreshed_at NULLS FIRST is what makes the rotation
        a rotation: never-refreshed sets lead, then oldest.
        """
        with self._client() as client:
            response = client.get(
                f"{self.base}/rest/v1/pricecharting_set_registry",
                params={
                    "select": "registry_id,console_uid,set_name",
                    "source_site": f"eq.{source}",
                    "console_uid": "not.is.null",
                    "last_fetch_status": "eq.success",
                    "tier3_failure_count": "lt.3",
                    "order": "tier3_refreshed_at.asc.nullsfirst,registry_id.asc",
                    "limit": str(limit),
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return [row for row in response.json() if isinstance(row, dict)]

    def batches(self, *, source: str, statuses: list[str]) -> list[dict[str, Any]]:
        with self._client() as client:
            response = client.get(
                f"{self.base}/rest/v1/catalog_download_batches",
                params={
                    "select": "*",
                    "source": f"eq.{source}",
                    "status": f"in.({','.join(statuses)})",
                    "order": "created_at.asc",
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    def insert(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"{self.base}/rest/v1/catalog_download_batches",
                headers=self._headers(**{"Content-Type": "application/json",
                                         "Prefer": "return=representation"}),
                json=row,
            )
            response.raise_for_status()
            return response.json()[0]

    def update(self, batch_id: str, patch: dict[str, Any]) -> None:
        patch = {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}
        with self._client() as client:
            response = client.patch(
                f"{self.base}/rest/v1/catalog_download_batches",
                params={"batch_id": f"eq.{batch_id}"},
                headers=self._headers(**{"Content-Type": "application/json",
                                         "Prefer": "return=minimal"}),
                json=patch,
            )
            response.raise_for_status()

    def upload(self, key: str, path: Path) -> None:
        """Upload the CSV to the private bucket.

        Same service-role storage API the codebase already uses for coin
        images and data-request exports.
        """
        with open(path, "rb") as handle:
            with httpx.Client(timeout=max(self.timeout, 300)) as client:
                response = client.post(
                    f"{self.base}/storage/v1/object/{BUCKET}/{key}",
                    headers=self._headers(**{"Content-Type": "text/csv",
                                             "x-upsert": "true"}),
                    content=handle.read(),
                )
                response.raise_for_status()

    # --- ingester-side access ------------------------------------------------

    def claim_ingestable_batch(
        self, *, source: str, claimed_by: str, max_attempts: int
    ) -> dict[str, Any] | None:
        """Take the oldest batch that can be ingested, or None.

        Claims VALIDATED batches and also INGEST_FAILED ones, which is the
        whole point of staging the file: the object is still in storage, so
        a retry costs no vendor CSV slot. Without this a single failed ingest
        parks its file forever -- found the hard way on 2026-09-08, when a
        failed batch had to be reset by hand twice.

        VALIDATION_FAILED is deliberately NOT retryable: that file is a
        wrong-catalog response, and retrying it would just write the wrong
        catalog later instead of now.

        A compare-and-swap rather than a plain read-then-write: the PATCH
        filters on the status the row was read with, so if another worker
        claimed it first this updates zero rows and we look again. PostgREST
        cannot express FOR UPDATE SKIP LOCKED, and an unconditional write
        would let two ingesters process one batch -- double-writing the
        catalog and double-stamping the registry.
        """
        candidates = self.batches(source=source, statuses=[VALIDATED, INGEST_FAILED])
        for candidate in candidates:
            attempts = int(candidate.get("attempts") or 0)
            if attempts >= max_attempts:
                # Left in place rather than hidden: a batch that keeps failing
                # is a thing to look at, not to quietly drop.
                continue
            batch_id = candidate["batch_id"]
            with self._client() as client:
                response = client.patch(
                    f"{self.base}/rest/v1/catalog_download_batches",
                    params={"batch_id": f"eq.{batch_id}",
                            "status": f"eq.{candidate['status']}"},
                    headers=self._headers(**{"Content-Type": "application/json",
                                             "Prefer": "return=representation"}),
                    json={
                        "status": INGESTING,
                        "claimed_at": datetime.now(timezone.utc).isoformat(),
                        "claimed_by": claimed_by,
                        "ingest_started_at": datetime.now(timezone.utc).isoformat(),
                        "attempts": attempts + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                response.raise_for_status()
                claimed = response.json()
            if claimed:
                return claimed[0]
            # Lost the race; try the next one.
        return None

    def download_object(self, key: str, destination: Path) -> int:
        """Stream a stored CSV to disk. Returns bytes written.

        Streamed, never held: a 350-set batch is ~37 MB and materialising one
        of these cost 2,028 MB of peak memory when measured.
        """
        written = 0
        with httpx.Client(timeout=max(self.timeout, 300)) as client:
            with client.stream("GET", f"{self.base}/storage/v1/object/{BUCKET}/{key}",
                               headers=self._headers()) as response:
                response.raise_for_status()
                with destination.open("wb") as out:
                    for chunk in response.iter_bytes():
                        out.write(chunk)
                        written += len(chunk)
        return written

    def stamp_registry_refreshed(self, registry_ids: list[str]) -> None:
        """Mark exactly these sets refreshed. Only ever after a clean write."""
        if not registry_ids:
            return
        with self._client() as client:
            response = client.patch(
                f"{self.base}/rest/v1/pricecharting_set_registry",
                params={"registry_id": f"in.({','.join(registry_ids)})"},
                headers=self._headers(**{"Content-Type": "application/json",
                                         "Prefer": "return=minimal"}),
                json={"tier3_refreshed_at": datetime.now(timezone.utc).isoformat()},
            )
            response.raise_for_status()
