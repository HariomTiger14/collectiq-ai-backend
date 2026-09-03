"""Tier-3 rotation: keep every sports card's price moving through history.

The sportscardspro first pass is complete -- all ~36,700 sets fetched once,
~10.5M catalog rows -- but backfill_pricecharting_sets.py's claim_rows()
permanently excludes a set once last_fetch_status='success' (deliberate:
initial completeness, not recurring freshness). The two existing refresh
tiers only cover slices of it: tier 1 (refresh_small_sets.py) re-fetches
sets that fit under the /api/products 100-result cap, and tier 2
(refresh_tracked_catalog_items.py) covers individually-tracked items. A
sample showed ~0.03% of the bulk sports rows touched within 2 days --
for most sports cards, price "history" was a single backfill-time point.

This script closes that gap by rotating through EVERY completed
sportscardspro set, oldest-refreshed first, via the same console_uid +
download-custom CSV path the backfill used. No claim/lease machinery: the
population is fixed and known, ordering by tier3_refreshed_at (nulls
first) IS the queue, and each set is stamped after a successful write, so
concurrent runs at worst re-fetch a few sets whose writes are
content-hash-diffed no-ops (see SupabaseCatalogClient.upsert_rows).

Small sets are rotated too, not filtered out. Tier 1's
tier1_refreshed_at can't distinguish "refreshed" from "checked and
skipped as too large" (it stamps every attempted candidate), so there is
no cheap size signal in the registry -- and a small set's CSV is small,
so including them costs little and keeps this script's logic trivial.

Pacing is the hard constraint, inherited from the backfill's live
findings (see SPORTSCARDSPRO_DEFAULT_* there): sportscardspro.com's
Cloudflare throttling tolerates ~15s spacing sustained, batches must stay
tiny (a 20-set batch once OOM-killed a 512Mi instance -- sports sets run
huge), and a 429 breaker stops the run rather than digging in. At the
default --max-requests 220 x 3 sets, one run covers ~660 sets in ~55
minutes; hourly runs cycle the full registry in ~2.3 days, i.e. every
sports card gets a fresh price point (and a history row when it moved)
every 2-3 days.

The 15-minute backfill cron shares this throttle budget but has been a
no-op since the first pass completed; it only wakes when the weekly
discover job registers new sets. Both scripts carry the breaker, so a
collision degrades to a skipped run, not a ban.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts._shared_rate_limiter import PRICECHARTING_CSV, SharedRateLimiter
from scripts.backfill_pricecharting_sets import (
    REQUEST_HEADERS,
    SOURCE_SITE_BASE_URLS,
    CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
    SPORTSCARDSPRO_DEFAULT_BATCH_SIZE,
    SPORTSCARDSPRO_DEFAULT_MAX_ATTEMPTS,
    _Counter,
    _RateLimitCircuitBreaker,
    fetch_batch_csv,
    fetch_batch_csv_with_retry,
    write_catalog_rows,
    write_catalog_rows_with_retry,
)
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    chunked_iter,
    iter_rows_from_text,
    to_catalog_row,
)

SOURCE_FILE_TAG = "sportscardspro-tier3-refresh"
REGISTRY_PAGE_SIZE = 1000

# Sets that have failed this many times in a row stop being queued. They are
# not deleted -- tier3_last_error keeps the reason, and tier3_rotation_status
# surfaces them as "poisoned" -- but they no longer occupy the head of a
# NULLS FIRST queue forever. See the 20260901 migration for the G37119 case
# that motivated this.
TIER3_MAX_FAILURES = 3

# Extra requests allowed to pinpoint the bad set inside ONE failed batch.
#
# Sized against the CSV budget, not against how neatly bisection converges.
# At the published 1-call-per-10-minutes limit the account gets ~144 CSV calls
# a day, so a full bisection of a 100-set batch (~14 probes) would cost 2.3
# hours and ~10% of the day's entire budget to quarantine one dead set. That
# was cheap at 30s pacing; it is not now.
#
# 4 probes narrows a 100-set batch to ~6 sets, which is enough: those sets stop
# being stamped, their tier3_failure_count climbs, and TIER3_MAX_FAILURES
# retires them within a few cycles. Slightly blunter than pinpointing one
# offender, and roughly a quarter of the cost.
DEFAULT_MAX_ISOLATION_REQUESTS = 4
# Trip after this many consecutive rate-limited batches. Low on purpose:
# the backfill's live testing showed 429s recur in clusters once the
# sustained-volume limit is hit, and continuing just extends the cluster.
RATE_LIMIT_BREAKER_THRESHOLD = 3


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "PRICECHARTING_API_TOKEN is required (or --api-token) -- even for "
            "--dry-run, since this worker makes real download-custom requests "
            "and only skips writing results."
        )

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    registry = Tier3RegistryClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    copy_writer = None
    if args.copy_writer and not args.dry_run:
        from scripts.tier3_copy_writer import CopyCatalogWriter

        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            raise SystemExit("--copy-writer requires DATABASE_URL.")
        copy_writer = CopyCatalogWriter(database_url)
        print("Using COPY writer (direct Postgres) for catalog writes.", flush=True)

    catalog_client = (
        None
        if args.dry_run or copy_writer is not None
        else SupabaseCatalogClient(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            timeout_seconds=args.timeout_seconds,
        )
    )

    set_budget = args.max_requests * args.batch_size
    rows = registry.fetch_rotation_rows(limit=set_budget)
    print(
        f"Rotating {len(rows)} of the oldest-refreshed sportscardspro set(s) "
        f"(budget: {args.max_requests} requests x {args.batch_size} sets).",
        flush=True,
    )
    if not rows:
        print(dump_and_report({"success": True, "setsConsidered": 0}, indent=2), flush=True)
        return 0

    base_url = SOURCE_SITE_BASE_URLS["sportscardspro"]
    csv_limiter = SharedRateLimiter(
        PRICECHARTING_CSV,
        fallback_interval_seconds=args.sleep_between_requests_seconds,
    )
    breaker = _RateLimitCircuitBreaker(RATE_LIMIT_BREAKER_THRESHOLD)
    rate_limit_counter = _Counter()
    # 403s are tracked separately from 429s: "blocked" and "slow down" are
    # different states, and only one of them is fixed by waiting a bit
    # longer between requests.
    blocked_counter = _Counter()
    total_catalog_rows = 0
    failed_batches = 0
    # failed_batches lumped fetch failures and write failures into one
    # number, so the ledger could not say whether a run's ~13% loss was
    # sportscardspro throttling us or Postgres timing out on a 12M-row
    # table -- opposite problems with opposite fixes. Count them apart.
    failed_fetches = 0
    failed_writes = 0
    write_retries = 0
    # Sets the endpoint refused individually, and healthy sets recovered from
    # a batch that would previously have been discarded whole.
    dead_sets = 0
    salvaged_sets = 0
    isolation_requests = 0
    refreshed_ids: list[str] = []

    with httpx.Client(
        timeout=args.timeout_seconds,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as http:
        for index in range(0, len(rows), args.batch_size):
            if breaker.tripped:
                print(
                    "Rate-limit breaker tripped -- stopping this run; the "
                    "unstamped sets stay at the front of the rotation.",
                    flush=True,
                )
                break
            chunk = rows[index : index + args.batch_size]
            # Account-wide, not per-run: the categories refresh and the sets
            # backfill draw on the same published CSV limit and overlap this
            # job for most of the day.
            csv_limiter.acquire()
            console_uids = [row["console_uid"] for row in chunk]
            before_429s = rate_limit_counter.value
            before_403s = blocked_counter.value
            status_sink: list[int] = []
            csv_text = fetch_batch_csv_with_retry(
                http,
                base_url=base_url,
                token=token,
                console_uids=console_uids,
                max_attempts=args.max_attempts,
                retry_sleep_seconds=args.sleep_between_requests_seconds,
                rate_limit_counter=rate_limit_counter,
                blocked_counter=blocked_counter,
                status_sink=status_sink,
            )

            csv_texts: list[str] = []
            live_chunk = chunk
            if csv_text is not None:
                csv_texts = [csv_text]
                breaker.record_success()
            else:
                # Trip on EITHER signal. Previously only 429 counted, so a
                # run taking pure 403s never tripped and would spend all
                # 120 throttled requests on an endpoint already refusing
                # us -- exactly the behaviour most likely to harden a
                # temporary block.
                throttled = (
                    rate_limit_counter.value > before_429s
                    or blocked_counter.value > before_403s
                )
                status = status_sink[-1] if status_sink else 0
                if throttled or not status:
                    # Endpoint-wide refusal, or a transport error that never
                    # produced a status. Either way the batch says nothing
                    # about any individual set, so there is nothing to isolate.
                    failed_batches += 1
                    failed_fetches += 1
                    if throttled:
                        breaker.record_rate_limited()
                    continue

                # The endpoint answered and refused THIS combination of
                # console_uids -- historically one set removed upstream
                # taking its whole batch down with it (see the 20260901
                # migration). Salvage the healthy sets instead of discarding
                # the batch and re-fetching all of them next run.
                isolation_budget = [args.max_isolation_requests]
                try:
                    csv_texts, dead_rows = _isolate_failed_batch(
                        http,
                        base_url=base_url,
                        token=token,
                        rows=chunk,
                        sleep_seconds=args.sleep_between_requests_seconds,
                        rate_limit_counter=rate_limit_counter,
                        blocked_counter=blocked_counter,
                        budget=isolation_budget,
                    )
                except _ThrottleAbort as exc:
                    print(f"  Isolation abandoned: {exc}", flush=True)
                    failed_batches += 1
                    failed_fetches += 1
                    breaker.record_rate_limited()
                    continue
                finally:
                    isolation_requests += (
                        args.max_isolation_requests - isolation_budget[0]
                    )

                if dead_rows:
                    dead_ids = [row["registry_id"] for row in dead_rows]
                    dead_sets += len(dead_ids)
                    if not args.dry_run:
                        registry.record_tier3_failures(
                            dead_ids,
                            error=f"download-custom refused this set (HTTP {status})",
                        )
                    dead_id_set = {row["registry_id"] for row in dead_rows}
                    live_chunk = [
                        row for row in chunk if row["registry_id"] not in dead_id_set
                    ]

                if not csv_texts:
                    # Every set in the batch was refused individually.
                    failed_batches += 1
                    failed_fetches += 1
                    continue
                salvaged_sets += len(live_chunk)
                breaker.record_success()

            # Stamped per batch, not once per run. It becomes the history
            # row's valid_from and the value used to close the previous
            # current row, so a run-wide stamp goes stale as the run gets
            # longer: a multi-hour catch-up would try to close a row that
            # tier-1 (hourly, same table) wrote AFTER this run started,
            # producing valid_to < valid_from and a 23514 violation against
            # pricecharting_catalog_history_valid_window_check. Measured
            # 2026-09-01: 30 such failures in a 5.5-hour run.
            source_downloaded_at = datetime.now(timezone.utc).isoformat()
            # Parse and write in chunks rather than materialising the batch.
            # A 100-set batch is ~63,600 rows; holding those as dicts is what
            # forced batch sizes down to single digits -- a 20-set batch
            # (~12,700 rows) already OOM-killed a 512Mi instance. Peak memory
            # here is one chunk, so batch size is bounded by the fetch limit
            # (one CSV call per 10 minutes) rather than by RAM.
            #
            # Splitting a batch across writes is safe: the history gate
            # compares against the CURRENT history row, so if the same
            # pricecharting_id appears in two chunks the second sees the
            # version the first just wrote and skips it instead of creating a
            # duplicate. A failed chunk fails the batch and the set is never
            # stamped, so the retry re-fetches and the already-written chunks
            # become content-hash no-ops.
            def _iter_catalog_rows():
                for text in csv_texts:
                    for raw in iter_rows_from_text(text):
                        row = to_catalog_row(raw, SOURCE_FILE_TAG, source_downloaded_at)
                        if row is not None:
                            yield row

            wrote, retried = True, 0
            if args.dry_run:
                total_catalog_rows += sum(1 for _ in _iter_catalog_rows())
            else:
                for chunk in chunked_iter(_iter_catalog_rows(), args.ingest_chunk_rows):
                    total_catalog_rows += len(chunk)
                    if copy_writer is not None:
                        # One transaction per chunk: it either lands or it
                        # does not, so there is no partial state to reason
                        # about on retry.
                        chunk_wrote, chunk_retried = copy_writer.write(chunk), 0
                    else:
                        assert catalog_client is not None
                        # Retrying a write costs no sportscardspro request;
                        # giving up costs a full re-fetch of bytes we hold.
                        chunk_wrote, chunk_retried = write_catalog_rows_with_retry(
                            catalog_client,
                            chunk,
                            batch_size=args.catalog_batch_size,
                            attempts=args.write_attempts,
                            backoff_seconds=args.write_retry_seconds,
                        )
                    retried += chunk_retried
                    if not chunk_wrote:
                        wrote = False
                        break

                write_retries += retried
                if not wrote:
                    failed_batches += 1
                    failed_writes += 1
                    continue
            # Stamp each batch as soon as its write lands, not in one
            # PATCH at the end of the run: a run is ~55 minutes long, and
            # end-of-run stamping means a killed run (deploy, timeout,
            # OOM) loses every stamp -- the next run would re-spend all
            # 220 throttled requests re-fetching sets whose data already
            # landed. A failed batch is simply never stamped, keeping its
            # sets at the front of the rotation.
            batch_ids = [row["registry_id"] for row in live_chunk]
            if not args.dry_run:
                registry.mark_tier3_refreshed(batch_ids)
            refreshed_ids.extend(batch_ids)

    writer_with_stats = copy_writer or catalog_client
    catalog_write_stats = (
        writer_with_stats.catalog_write_stats
        if writer_with_stats is not None
        else {"written": 0, "skippedUnchanged": 0, "failed": 0}
    )
    if copy_writer is not None:
        copy_writer.close()
    print(
        dump_and_report(
            {
                "success": True,
                "dryRun": args.dry_run,
                "setsConsidered": len(rows),
                "setsRefreshed": len(refreshed_ids),
                "failedBatches": failed_batches,
                "failedFetches": failed_fetches,
                "failedWrites": failed_writes,
                "writeRetries": write_retries,
                "rateLimited429s": rate_limit_counter.value,
                "blocked403s": blocked_counter.value,
                "deadSetsIsolated": dead_sets,
                "setsSalvagedFromFailedBatches": salvaged_sets,
                "isolationRequests": isolation_requests,
                "breakerTripped": breaker.tripped,
                "catalogRowsParsed": total_catalog_rows,
                # Real write/skip split from the client's accumulator, not
                # a placeholder echo of rowsParsed. Unchanged rows cost a
                # hash read; only changed rows cost a write -- which is the
                # number that matters when judging database load, and the
                # one the ledger previously could not show.
                "catalogRowsWritten": (
                    0 if args.dry_run else catalog_write_stats["written"]
                ),
                "catalogRowsSkippedUnchanged": (
                    0 if args.dry_run else catalog_write_stats["skippedUnchanged"]
                ),
                "catalogRowsFailed": (
                    0 if args.dry_run else catalog_write_stats["failed"]
                ),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


class _ThrottleAbort(Exception):
    """Raised when batch isolation runs into a 429/403.

    Those two mean the endpoint is refusing us generally, not that one set
    is bad. Probing individual console_uids in that state burns throttled
    requests against a door already shut -- the surest way to turn a
    temporary block into a durable one -- so isolation unwinds and lets the
    breaker handle it.
    """


def _isolate_failed_batch(
    http: httpx.Client,
    *,
    base_url: str,
    token: str,
    rows: list[dict[str, Any]],
    sleep_seconds: float,
    rate_limit_counter: _Counter,
    blocked_counter: _Counter,
    budget: list[int],
    limiter: Any = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Split a failed batch to salvage its healthy sets and pinpoint the
    set(s) the endpoint cannot serve.

    download-custom fails the ENTIRE request, so one dead console_uid
    destroys every set batched with it -- at --batch-size 25 that is 24
    healthy sets discarded per dead one, re-fetched from scratch next run.
    Halving costs ~2*log2(n) requests to find one offender instead of n,
    and hands back CSV for everything else.

    Returns (csv_texts, dead_rows). ``budget`` is a one-element list used as
    a shared mutable countdown so the recursion cannot exceed its allowance.
    """
    if not rows or budget[0] <= 0:
        return [], []
    budget[0] -= 1
    if limiter is not None:
        limiter.acquire()
    elif sleep_seconds > 0:
        time.sleep(sleep_seconds)
    status_sink: list[int] = []
    csv_text = fetch_batch_csv(
        http,
        base_url=base_url,
        token=token,
        console_uids=[row["console_uid"] for row in rows],
        rate_limit_counter=rate_limit_counter,
        blocked_counter=blocked_counter,
        status_sink=status_sink,
    )
    if csv_text is not None:
        return [csv_text], []

    status = status_sink[-1] if status_sink else 0
    if status in (429, 403):
        raise _ThrottleAbort(f"HTTP {status} during isolation")
    if len(rows) == 1:
        row = rows[0]
        print(
            f"  Dead set isolated: {row['console_uid']} "
            f"({row.get('set_name') or '?'}) -- HTTP {status or 'error'}.",
            flush=True,
        )
        return [], [row]

    mid = len(rows) // 2
    left_csv, left_dead = _isolate_failed_batch(
        http,
        base_url=base_url,
        token=token,
        rows=rows[:mid],
        sleep_seconds=sleep_seconds,
        rate_limit_counter=rate_limit_counter,
        blocked_counter=blocked_counter,
        budget=budget,
        limiter=limiter,
    )
    right_csv, right_dead = _isolate_failed_batch(
        http,
        base_url=base_url,
        token=token,
        rows=rows[mid:],
        sleep_seconds=sleep_seconds,
        rate_limit_counter=rate_limit_counter,
        blocked_counter=blocked_counter,
        budget=budget,
        limiter=limiter,
    )
    return left_csv + right_csv, left_dead + right_dead


class Tier3RegistryClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def fetch_rotation_rows(self, *, limit: int) -> list[dict[str, Any]]:
        """Oldest-refreshed completed sportscardspro sets, never-refreshed
        first. nullsfirst makes the initial full cycle drain every
        never-stamped set before any second visit happens.

        Ordered by tier3_failure_count first, and sets at or above
        TIER3_MAX_FAILURES are dropped entirely. Without that, a set the
        endpoint can never serve keeps tier3_refreshed_at NULL forever and
        so parks permanently at the head of a NULLS FIRST queue -- retried
        first on every run, taking its whole batch down each time."""
        rows: list[dict[str, Any]] = []
        offset = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            while len(rows) < limit:
                page_limit = min(REGISTRY_PAGE_SIZE, limit - len(rows))
                response = client.get(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={
                        "select": "registry_id,console_uid,set_name",
                        "source_site": "eq.sportscardspro",
                        "last_fetch_status": "eq.success",
                        "console_uid": "not.is.null",
                        "tier3_failure_count": f"lt.{TIER3_MAX_FAILURES}",
                        "order": (
                            "tier3_failure_count.asc,"
                            "tier3_refreshed_at.asc.nullsfirst,"
                            "registry_id.asc"
                        ),
                        "limit": str(page_limit),
                        "offset": str(offset),
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list) or not page:
                    break
                rows.extend(page)
                if len(page) < page_limit:
                    break
                offset += len(page)
        return rows

    def mark_tier3_refreshed(self, registry_ids: list[str]) -> None:
        stamped_at = datetime.now(timezone.utc).isoformat()
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(registry_ids), REGISTRY_PAGE_SIZE):
                chunk = registry_ids[index : index + REGISTRY_PAGE_SIZE]
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"in.({','.join(chunk)})"},
                    # Reset the failure counter: it counts CONSECUTIVE
                    # failures, so a set that recovers must not stay one
                    # strike from being dropped from the queue for good.
                    json={
                        "tier3_refreshed_at": stamped_at,
                        "tier3_attempted_at": stamped_at,
                        "tier3_failure_count": 0,
                        "tier3_last_error": None,
                    },
                    headers={**self._headers(), "Prefer": "return=minimal"},
                )
                response.raise_for_status()

    def record_tier3_failures(self, registry_ids: list[str], *, error: str) -> None:
        """Increment the failure counter for sets the endpoint refused.

        Goes through an RPC rather than a PATCH because the increment has to
        be atomic: the backfill cron shares this throttle budget and can be
        mid-run, and a read-modify-write would drop failures exactly when
        they cluster.
        """
        if not registry_ids:
            return
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(registry_ids), REGISTRY_PAGE_SIZE):
                chunk = registry_ids[index : index + REGISTRY_PAGE_SIZE]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/rpc/tier3_record_failure",
                    json={"p_registry_ids": chunk, "p_error": error},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                )
                response.raise_for_status()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Tier-3 rotation: re-download every completed sportscardspro set "
            "oldest-refreshed-first via console_uid + download-custom CSV, so "
            "all sports cards accumulate price history (full cycle ~2-3 days)."
        )
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=220,
        help="download-custom requests per run; x batch-size = sets per run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=SPORTSCARDSPRO_DEFAULT_BATCH_SIZE,
        help="Sets per download-custom request. Keep tiny: a 20-set batch has "
        "OOM-killed a 512Mi instance before (sports sets run huge).",
    )
    parser.add_argument(
        "--catalog-batch-size",
        type=int,
        default=40,
        help="Rows per catalog upsert. 500 (the completed-categories "
        "default) was observed hitting the database statement timeout "
        "(57014) here -- each upserted row updates five GIN trigram "
        "indexes plus the browse btrees, and sports-card batches skew "
        "large. 150 was not low enough either: measured 2026-09-01, it "
        "still lost ~15%% of writes to 57014 (one failure was on a "
        "59-row sub-batch), while 40 wrote 1,065 rows with zero failures "
        "and zero retries.",
    )
    parser.add_argument(
        "--copy-writer",
        action="store_true",
        help="Write via COPY + a single server-side merge over a direct "
        "DATABASE_URL connection instead of PostgREST. Measured 2026-09-01: "
        "~2.5-3x faster end to end, and the write becomes one transaction "
        "rather than ~375 statements racing the hourly tier-1 job (which is "
        "what produces the 57014 timeouts). Needs psycopg and DATABASE_URL, "
        "so it is opt-in and laptop-only -- Render's cron path has neither.",
    )
    parser.add_argument(
        "--ingest-chunk-rows",
        type=int,
        default=10_000,
        help="Rows held in memory before writing. Bounds peak memory "
        "independently of --batch-size, so a 100-set batch (~63,600 rows) "
        "fits on a 512Mi instance. 10,000 is ~7 chunks per batch, against "
        "the 375 round trips the old per-sub-batch REST path made.",
    )
    parser.add_argument(
        "--max-isolation-requests",
        type=int,
        default=DEFAULT_MAX_ISOLATION_REQUESTS,
        help="Extra requests allowed to pinpoint the bad set inside one "
        "failed batch. download-custom fails the whole request, so without "
        "isolation a single set removed upstream discards every set "
        "batched with it.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--sleep-between-requests-seconds",
        type=float,
        default=CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
        help="Pace between download-custom calls. Defaults to the vendor's "
        "published limit of one CSV call every 10 minutes (see "
        "backfill_pricecharting_sets.py). The old 30s default was 20x over it.",
    )
    parser.add_argument("--max-attempts", type=int, default=SPORTSCARDSPRO_DEFAULT_MAX_ATTEMPTS)
    parser.add_argument(
        "--write-attempts",
        type=int,
        default=3,
        help=(
            "Catalog write attempts per already-fetched batch before giving "
            "up. Retrying costs no sportscardspro request; giving up costs a "
            "full re-fetch next cycle."
        ),
    )
    parser.add_argument(
        "--write-retry-seconds",
        type=float,
        default=5.0,
        help="Base backoff between catalog write retries (multiplied by attempt).",
    )
    parser.add_argument("--api-token", default="", help="Defaults to PRICECHARTING_API_TOKEN.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run_with_recorder("tier3-sportscardspro-rotation", main))
