"""Vocabulary and rules for staged catalog refresh batches.

The pipeline this serves does not exist yet -- PR 2 adds the downloader, PR 3
the ingester. This module exists first so both are written against one
definition of what a batch's states mean, rather than two scripts each
half-remembering the same string constants.

WHY A STATE MACHINE AND NOT A BOOLEAN
-------------------------------------
"It failed" has meant four genuinely different things in this system, and
conflating them is what made the failures hard to act on:

  * transient  -- the vendor was unwell (503/429/reset). Retry; the sets are
                  fine. 3 of 9 measured multi-uid fetches failed this way.
  * validation -- the CSV came back well-formed and WRONG. A mistyped filter
                  returned 200 with 123,166 rows of the wrong catalog
                  (measured 2026-09-07). Never write it, keep the file.
  * write      -- the database refused or timed out. Rows may be partially
                  written; retry is safe because the content-hash gate makes
                  re-ingest a no-op.
  * blocked    -- Cloudflare 403. Stop and alert; this is what killed the
                  sportscardspro.com CSV path entirely.

RETENTION FOLLOWS THE SAME LOGIC
--------------------------------
A successfully ingested file is disposable within a day. A FAILED file is the
evidence -- deleting it destroys the only record of what actually arrived --
so failures are kept a week.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# --- states -----------------------------------------------------------------

PENDING = "pending"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
VALIDATED = "validated"
INGESTING = "ingesting"
INGESTED = "ingested"

FETCH_FAILED = "fetch_failed"
VALIDATION_FAILED = "validation_failed"
INGEST_FAILED = "ingest_failed"

ALL_STATUSES = frozenset({
    PENDING, DOWNLOADING, DOWNLOADED, VALIDATED, INGESTING, INGESTED,
    FETCH_FAILED, VALIDATION_FAILED, INGEST_FAILED,
})

# Terminal for THIS attempt. fetch_failed and ingest_failed are retryable by a
# later run, but neither holds a claim, so the in-flight guard ignores them.
TERMINAL_STATUSES = frozenset({INGESTED, FETCH_FAILED, VALIDATION_FAILED, INGEST_FAILED})
IN_FLIGHT_STATUSES = frozenset(ALL_STATUSES - TERMINAL_STATUSES)

# States that hold a lease and can therefore go stale if a run dies.
LEASED_STATUSES = frozenset({DOWNLOADING, INGESTING})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({DOWNLOADING, FETCH_FAILED}),
    DOWNLOADING: frozenset({DOWNLOADED, FETCH_FAILED, PENDING}),
    DOWNLOADED: frozenset({VALIDATED, VALIDATION_FAILED}),
    VALIDATED: frozenset({INGESTING, INGEST_FAILED}),
    # Back to VALIDATED is the reaper returning an abandoned lease, not a
    # retreat: the file is still in storage and still valid.
    INGESTING: frozenset({INGESTED, INGEST_FAILED, VALIDATED}),
    INGESTED: frozenset(),
    # A failed fetch is retried as a NEW batch rather than resurrected, so the
    # attempt history stays readable.
    FETCH_FAILED: frozenset(),
    VALIDATION_FAILED: frozenset(),
    # An ingest can be retried in place: the object is still in storage, so
    # retrying costs no vendor CSV slot.
    INGEST_FAILED: frozenset({INGESTING}),
}

# --- error classes ----------------------------------------------------------

CLASS_TRANSIENT = "transient"
CLASS_VALIDATION = "validation"
CLASS_WRITE = "write"
CLASS_BLOCKED = "blocked"
ALL_ERROR_CLASSES = frozenset({CLASS_TRANSIENT, CLASS_VALIDATION, CLASS_WRITE, CLASS_BLOCKED})

# HTTP statuses that describe the VENDOR's health, never the batch's validity.
TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
# Cloudflare refusing us outright. Not a throttle -- do not keep trying.
BLOCKED_HTTP = frozenset({403})

# --- write paths ------------------------------------------------------------

WRITE_REST = "rest"
WRITE_COPY = "copy"
WRITE_NONE = "none"

# --- storage ----------------------------------------------------------------

BUCKET = "catalog-refresh-batches"

# Ingested files are disposable within a day; failed ones are the evidence.
RETENTION_DAYS = {
    INGESTED: 1,
    INGEST_FAILED: 7,
    VALIDATION_FAILED: 7,
}

# A run that dies mid-claim would otherwise hold its batch forever.
LEASE_TIMEOUT_MINUTES = 30


def storage_key(source: str, batch_id: str, *, now: datetime | None = None) -> str:
    """Object key for a batch's CSV.

    Date-partitioned so a prefix listing bounds the cleanup sweep, and named
    by batch_id so any object traces back to its row in one lookup -- an
    object with no row is an orphan worth reporting, not silently deleting.
    """
    moment = now or datetime.now(timezone.utc)
    return f"{source}/{moment:%Y/%m/%d}/{batch_id}.csv"


def classify_http_failure(status: int | None) -> str:
    """Map a failed HTTP response to an error class.

    Unknown statuses are transient by design. The wrong default here retries
    something unretryable, which is cheap; the opposite abandons a healthy
    batch, which is not.
    """
    if status in BLOCKED_HTTP:
        return CLASS_BLOCKED
    return CLASS_TRANSIENT


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise ValueError(
            f"illegal batch transition {current!r} -> {target!r}; "
            f"allowed: {sorted(ALLOWED_TRANSITIONS.get(current, []))}"
        )


def is_lease_stale(
    status: str, claimed_at: datetime | None, *, now: datetime | None = None
) -> bool:
    """Has a leased batch been held past the reaper window?"""
    if status not in LEASED_STATUSES or claimed_at is None:
        return False
    moment = now or datetime.now(timezone.utc)
    return (moment - claimed_at) > timedelta(minutes=LEASE_TIMEOUT_MINUTES)


def recovery_status(status: str) -> str | None:
    """Where a stale lease returns to.

    An abandoned ingest goes back to VALIDATED because the file is still in
    storage and still valid -- retrying costs no vendor CSV slot. An abandoned
    download goes back to PENDING, where it will cost one.
    """
    if status == INGESTING:
        return VALIDATED
    if status == DOWNLOADING:
        return PENDING
    return None


def retention_expires_at(status: str, updated_at: datetime) -> datetime | None:
    """When this batch's stored object may be deleted, or None to keep it."""
    days = RETENTION_DAYS.get(status)
    return None if days is None else updated_at + timedelta(days=days)


def is_object_expired(
    status: str, updated_at: datetime, *, now: datetime | None = None
) -> bool:
    expires = retention_expires_at(status, updated_at)
    if expires is None:
        return False
    return (now or datetime.now(timezone.utc)) >= expires


def new_batch_row(
    *, source: str, console_uids: list[str], registry_ids: list[str]
) -> dict[str, Any]:
    """The insert payload for a freshly claimed batch."""
    return {
        "source": source,
        "status": PENDING,
        "console_uids": console_uids,
        "registry_ids": registry_ids,
        "requested_count": len(console_uids),
        "attempts": 0,
    }
