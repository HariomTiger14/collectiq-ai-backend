"""Stage the five bulk CSVs through Supabase Storage before ingesting them.

Today refresh_pricecharting_catalog downloads a CSV to a temp file, imports it,
and deletes it in a `finally`. If the import dies after the download -- a
statement timeout, a crash, a deploy mid-run -- the file is gone and the only
way to retry is another vendor download. The CSV endpoint allows one request
per ten minutes account-wide, so a retry is not free: it costs a slot the
tier-3 rotation and the sets backfill are also drawing on.

This is the same staging the sports pipeline uses (#207-#210): the object lands
in Storage and a row in catalog_download_batches records it, so a failed ingest
is retried from the object. That table was built source-agnostic --
console_uids and registry_ids default to empty arrays -- so a CSV batch needs
no schema change.

Two deliberate differences from the sports pipeline, both because the shape of
the problem differs:

  * One process, not two crons. The sports split exists because 350 sets are
    downloaded on a 10-minute vendor budget while ingests take ~10 minutes
    each, so they must run independently. Five CSVs in one nightly job have no
    such pressure, and splitting them would mean two new Render services to
    get wrong. The durability property -- retry without re-downloading -- does
    not come from the split; it comes from the object existing.

  * No validation gate. The sports downloader validates console-name families
    because its per-set vendor endpoint can return the wrong catalog entirely.
    A bulk CSV is the whole category by definition, so there is nothing to
    compare it against.

(Deliberately no literal vendor endpoint name anywhere in this file:
tests/test_csv_source_policy.py discovers CSV callers by scanning for one, and
a module that merely stages files should stay inside that net rather than be
excused from it by an exclusion list.)

Opt-in. Nothing here runs unless --use-storage is passed, so the cron behaves
exactly as it does today until that flag is added to its start command.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from datetime import datetime, timezone

from scripts.catalog_batches import (
    BUCKET,
    INGEST_FAILED,
    INGESTED,
    INGESTING,
    VALIDATED,
    assert_transition,
    is_lease_stale,
    recovery_status,
    storage_key,
)
from scripts.catalog_batch_store import BatchStore

# A CSV batch is one whole source file, so the "source" on its row is the
# file name -- video_games.csv, not video_games. That keeps it distinct from
# the sports rows (sportscardspro) in the same table at a glance and matches
# the source_file the rows themselves are tagged with.
RESUMABLE_STATUSES = (VALIDATED, INGEST_FAILED)


def csv_batch_source(source_name: str) -> str:
    return source_name


def stage_csv(
    store: BatchStore,
    *,
    source_name: str,
    path: Path,
    row_count: int | None = None,
    fetch_ms: int | None = None,
) -> dict[str, Any]:
    """Put a freshly downloaded CSV in Storage and record it.

    Uploaded BEFORE the row is marked ready, never after: a row pointing at an
    object that does not exist is worse than an object with no row. The orphan
    is visible and disposable; the dangling row would be claimed by the next
    run and fail on a 404 it cannot fix.
    """
    batch_id = str(uuid.uuid4())
    key = storage_key(csv_batch_source(source_name), batch_id)
    store.upload(key, path)
    row = store.insert({
        "batch_id": batch_id,
        "source": csv_batch_source(source_name),
        "status": VALIDATED,
        "storage_key": key,
        "bytes": path.stat().st_size,
        "row_count": row_count,
        "fetch_ms": fetch_ms,
    })
    return row


def resumable_batches(store: BatchStore, *, source_name: str) -> list[dict[str, Any]]:
    """Batches for this source whose file is staged but uningested.

    The whole point of staging: these cost no vendor slot to retry, because
    the object is already in Storage.
    """
    return [
        batch for batch in store.batches(
            source=csv_batch_source(source_name), statuses=list(RESUMABLE_STATUSES))
        if batch.get("storage_key")
    ]


def mark_ingesting(store: BatchStore, batch: dict[str, Any], *, claimed_by: str) -> None:
    """Claim a staged file for import.

    claimed_at is written, not just claimed_by, and that is load-bearing: a
    process killed mid-import leaves the row INGESTING, and without a claim
    timestamp nothing can tell an import running now from one abandoned by a
    deploy last night. The reaper needs a clock, not a name.
    """
    store.update(batch["batch_id"], {
        "status": INGESTING,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "claimed_by": claimed_by,
        "attempts": int(batch.get("attempts") or 0) + 1,
    })


def reap_stale_ingesting(store: BatchStore, *, source_name: str) -> int:
    """Return abandoned imports to VALIDATED so the next run can resume them.

    Without this, a crash or deploy mid-import parks the file in INGESTING
    forever: it is not in RESUMABLE_STATUSES, so the next run downloads again
    -- spending the vendor slot this whole change exists to save. Exactly the
    shape of the orphaned 'running' ledger rows sitting since 2026-09-03.

    Back to VALIDATED rather than PENDING because the object is still in
    storage; recovery_status() already encodes that choice for the sports
    pipeline and is reused rather than restated.
    """
    reaped = 0
    for batch in store.batches(source=csv_batch_source(source_name),
                               statuses=[INGESTING]):
        claimed_at = batch.get("claimed_at")
        moment = (datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
                  if claimed_at else None)
        if not is_lease_stale(INGESTING, moment):
            continue
        target = recovery_status(INGESTING)
        assert_transition(INGESTING, target)
        store.update(batch["batch_id"], {
            "status": target,
            "claimed_at": None,
            "claimed_by": None,
            "last_error": "ingest lease expired",
            "last_error_class": "transient",
        })
        reaped += 1
    return reaped


def staged_today(batch: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Whether this staged object is already today's download.

    Decides whether draining a leftover also satisfies today's refresh. A file
    staged yesterday does not: ingesting it finishes yesterday's work, and
    today still needs its own download.
    """
    created = batch.get("created_at")
    if not created:
        return False
    moment = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    today = (now or datetime.now(timezone.utc)).date()
    return moment.astimezone(timezone.utc).date() == today


def mark_ingested(store: BatchStore, batch: dict[str, Any], *, stats: dict[str, Any]) -> None:
    store.update(batch["batch_id"], {
        "status": INGESTED,
        "claimed_at": None,
        "claimed_by": None,
        "last_error": None,
        **stats,
    })


def mark_ingest_failed(
    store: BatchStore, batch: dict[str, Any], *, error: str, error_class: str
) -> None:
    """Leave the object in place. That is what makes the retry free."""
    store.update(batch["batch_id"], {
        "status": INGEST_FAILED,
        "claimed_at": None,
        "claimed_by": None,
        "last_error": error[:2000],
        "last_error_class": error_class,
    })
