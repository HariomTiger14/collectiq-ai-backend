import argparse
import concurrent.futures
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


# A sustained run of 429s means the remote site is actively throttling this
# connection right now -- continuing to send more requests after that point
# only digs the hole deeper (and risks the relationship with the data
# provider, not just wasting this run). This was an explicit design
# requirement from day one ("don't build the backfill worker to hammer at
# zero delay... with automatic backoff/circuit-breaker if any response ever
# looks like a block") that was never actually implemented -- a live run
# hit 20+ consecutive 429s before this existed. 3 is deliberately low: a
# single 429 could be a transient blip, but 3 in a row is a real signal.
CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD = 3


class _RateLimitCircuitBreaker:
    """Shared across every concurrent resolve lane so one lane detecting a
    sustained block stops every lane immediately, instead of each lane
    independently burning through its own quota of wasted requests before
    noticing the same block."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def record_rate_limited(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._tripped = True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0


class _Counter:
    """Thread-safe accumulator shared across concurrent lanes for a single
    run-wide tally (e.g. total 429s seen), where a plain int would race."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def increment(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class _StartRateLimiter:
    """Gates request START times across every worker sharing one instance,
    independent of how many workers are running concurrently.

    This is the piece the round-robin-lane concurrency used elsewhere in
    this file doesn't have: each lane there paces itself independently, so
    N lanes' timers can drift into alignment and burst well above the
    intended per-lane rate -- live-confirmed on 2026-08-09 to trip a real
    Cloudflare 429 cascade (see --sportscardspro-api-search-concurrency's
    own history). A single shared gate can't drift like that: every caller
    blocks on the same lock, so successive calls to wait_for_slot() -- from
    however many threads -- are always spaced at least min_interval_seconds
    apart, measured from the previous call's START, not its finish. That's
    exactly what lets slow response latency overlap without ever raising
    how often a new request is allowed to begin.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_start: float | None = None

    def wait_for_slot(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next_allowed_start is not None:
                wait = self._next_allowed_start - now
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
            self._next_allowed_start = now + self._min_interval_seconds


from scripts._shared_rate_limiter import (
    BULK_MAX_SLOT_WAIT_SECONDS,
    CLASS_BACKFILL,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)
from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts.import_pricecharting_catalog import (
    TEXT_FIELDS,
    PartialCatalogWriteError,
    SupabaseCatalogClient,
    load_rows_from_text,
    pick_text,
    to_catalog_row,
    to_catalog_row_from_api_product,
)


REQUEST_HEADERS = {
    "User-Agent": "PackLoxSetDiscoveryBot/1.0 (+https://packlox.com; Legendary subscriber)",
}

CONSOLE_UID_PATTERN = re.compile(r'VGPC\.console_uid = "([^"]+)"')

SOURCE_SITE_BASE_URLS = {
    "pricecharting": "https://www.pricecharting.com",
    "sportscardspro": "https://www.sportscardspro.com",
}

# /api/products (search) is confirmed NOT blocked by sportscardspro.com's
# Cloudflare protection (unlike /console/* pages and the CSV download), and
# with a real subscriber token it returns full price data per item -- same
# column names as the CSV (see TEXT_FIELDS/PRICE_FIELDS in
# import_pricecharting_catalog.py), so results can feed straight into
# to_catalog_row() unchanged. But it's a ranked full-text search with a hard
# cap, confirmed live at exactly 100 results regardless of page/offset/
# cursor/limit params (all silently ignored, no pagination exists). A set
# under this cap is reliably complete (verified against a real set's known
# checklist size); a set at or over the cap is truncated/ambiguous and must
# fall back to the console_uid+CSV path instead.
API_SEARCH_RESULT_CAP = 100

# sportscardspro.com's console_uid pages and CSV download return a
# Cloudflare "Managed Challenge" (429) unpredictably at fast pacing, but
# this is rate-based throttling, not a hard bot-fingerprint block -- an
# early test (from a non-Render IP) found ~13% success at 2s spacing, 80%
# at 15s, 100% (10/10) at 30s, which is where this was originally set.
# Re-tested 2026-08-09 from Render's actual Ohio outbound IP at 10s
# spacing: a short live test (8 requests) succeeded cleanly, but after
# running at 10s (plus a raised --sportscardspro-slow-path-limit and a
# faster --sportscardspro-api-search-sleep-seconds) for roughly an hour of
# sustained cron cycles, 429s started recurring -- short bursts-only tests
# can't see a longer-window/aggregate volume limit, only sustained real
# traffic reveals it. Backed off to 15s as a middle ground.
#
# 2026-08-31: reverted to the original 30s. 15s never actually held. Its
# own measured success rate above is ~80%, and the tier-3 rotation lost
# 13-20% of its batches every run for weeks -- matching that 80% almost
# exactly, so the "middle ground" was really a standing 1-in-5 failure
# rate that had been normalised. Tonight it escalated: sportscardspro.com
# began returning 403 Forbidden alongside the 429s (both on identical
# console-uids across retries), a run refreshed 0 of 360 sets, and the
# breaker tripped. 403 is a block, not a "slow down".
#
# 30s is the only spacing here with a measured 100% success rate, so the
# throughput lost is mostly the throughput that was being wasted on
# retries anyway. pricecharting.com has no such throttle and stays on the
# caller's normal --sleep-between-requests-seconds pacing.
# sportscardspro.com/pricecharting.com publish their limits at
# /api-documentation:
#
#   "The API is limited to 1 call every second. Any more than that and your
#    calls will be blocked and your account permissions revoked if it persists."
#   "CSV calls are limited to one every 10 minutes."
#
# The CSV limit is the one that matters here: /price-guide/download-custom is a
# CSV call, not an API call. Everything in this repo paced it at 2s
# (pricecharting) or 30s (sportscardspro) -- 300x and 20x over the published
# limit respectively -- until 2026-09-02, which is the most likely explanation
# for the Cloudflare challenge that started blocking the download path from
# Render around 31 August while /api/products (comfortably inside the 1/sec
# API limit) kept working from the same host.
#
# The throughput lost is far less than it looks: download-custom accepts ~100
# console_uids per request, so 144 compliant calls/day still covers ~14,400
# sets -- a faster full cycle than the old 30s pacing achieved at batch 3.
# The fix for slow bulk transfer is more sets per request, not more requests.
CSV_DOWNLOAD_MIN_INTERVAL_SECONDS = 600.0

SPORTSCARDSPRO_DEFAULT_SLEEP_SECONDS = 30.0
SPORTSCARDSPRO_DEFAULT_MAX_ATTEMPTS = 3

# The console_uid resolve step is fully serial at SPORTSCARDSPRO_DEFAULT_SLEEP_
# SECONDS per row (deliberately not concurrent -- parallel lanes would send
# requests faster in aggregate and risk re-triggering the throttle this
# pacing exists to avoid). Left unbounded, a claimed batch where every row
# needs this slow path (worst case) would take limit x 30s -- at the cron's
# default --limit 150 that's 75 minutes, far past the 15-minute schedule
# and risking two runs claiming overlapping rows once a lease expires
# mid-run. Capping how many rows take the slow path per run bounds a
# single run's worst-case wall-clock time regardless of --limit; any
# excess rows are simply left claimed and age out their lease for a later
# run to pick up -- not touched, no failure_count penalty.
SPORTSCARDSPRO_DEFAULT_SLOW_PATH_LIMIT = 20

# Live-confirmed: a single CSV batch of 20 sportscardspro sets returned
# 26,450+ rows and OOM-killed a starter (512Mi) instance -- some sports-card
# sets run far larger than any pricecharting.com category ever has. Keep
# sportscardspro's CSV batches much smaller than pricecharting.com's
# (--batch-size, default 150) regardless of instance size, so no single
# batch's parsed rows (each held in memory, plus a duplicated raw_payload
# per row) can blow past the memory limit.
# Raised from 3 once pacing moved to the published 10-minute CSV interval:
# with 144 calls a day, sets-per-call is the only throughput lever left. 100 is
# the largest size confirmed working live (URL ~1,005 chars, well inside
# limits). Memory, which is what forced 3 on a 512Mi Render instance, is not a
# constraint at 1 call per 10 minutes -- and the COPY writer streams rather
# than accumulating.
SPORTSCARDSPRO_DEFAULT_BATCH_SIZE = 100

# Hard ceiling on how many /api/products searches the controlled-overlap
# scheduler (see _resolve_via_api_for_small_sets_overlapped) allows in
# flight at once, regardless of how high --sportscardspro-api-search-
# concurrency is set. This is a deliberately conservative first step after
# the 2026-08-09 concurrency incident: overlap enough to hide slow-response
# latency behind the existing request-start pacing, not a general-purpose
# concurrency knob. Raising it needs its own live-traffic validation, same
# as every other pacing lever in this file -- do not wire this to the CLI
# flag's raw value.
SPORTSCARDSPRO_API_SEARCH_MAX_IN_FLIGHT = 2


def _summarize_catalog_write_events(
    events: list[dict[str, Any]], *, top_n: int = 10
) -> dict[str, list[dict[str, Any]]]:
    """Reduces the full list of per-write-event records down to the top N
    slowest and top N largest, for surfacing outliers in the final JSON
    without dumping every write event into it. One event is recorded per
    write_catalog_rows() call: exactly one set for the sportscardspro
    API-search path, but possibly several sets bundled into one chunk for
    the console_uid+CSV path -- setNames reflects that (a list, not a
    single value) so a slow/large CSV batch is still attributable to the
    sets that made it up, even though true per-set write timing isn't
    measurable once multiple sets' rows are written together."""
    top_slowest = sorted(events, key=lambda event: event["elapsedSeconds"], reverse=True)[:top_n]
    top_largest = sorted(events, key=lambda event: event["rowCount"], reverse=True)[:top_n]
    return {
        "topSlowestCatalogWrites": [
            {**event, "elapsedSeconds": round(event["elapsedSeconds"], 3)} for event in top_slowest
        ],
        "topLargestCatalogWrites": [
            {**event, "elapsedSeconds": round(event["elapsedSeconds"], 3)} for event in top_largest
        ],
    }


def _build_result_summary(
    *,
    dry_run: bool,
    claimed_count: int,
    succeeded_count: int,
    api_search_succeeded: int,
    deferred_count: int,
    failed_count: int,
    catalog_rows_written: int,
    catalog_rows_parsed: int,
    api_search_counts: dict[str, int],
    slow_path_attempted: int,
    rate_limit_429_count: int,
    phase_seconds: dict[str, float],
    catalog_write_phase_seconds: dict[str, float],
    catalog_write_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assembles the final JSON printed to stdout -- pulled out of main() so
    the counters/timings it reports can be unit tested without wiring up a
    full httpx/Supabase-mocked run."""
    return {
        "success": True,
        "dryRun": dry_run,
        "claimed": claimed_count,
        "succeeded": succeeded_count,
        "succeededViaApiSearch": api_search_succeeded,
        "deferredToLaterRun": deferred_count,
        "failed": failed_count,
        "catalogRowsWritten": catalog_rows_written,
        "catalogRowsParsed": catalog_rows_parsed,
        "sportscardsproApiAttempted": api_search_counts["attempted"],
        "sportscardsproApiSucceeded": api_search_counts["succeeded"],
        "sportscardsproApiRejectedAmbiguous": api_search_counts["rejected_ambiguous"],
        "sportscardsproApiHitCap": api_search_counts["hit_cap"],
        "sportscardsproApiEmpty": api_search_counts["empty"],
        "sportscardsproSlowPathAttempted": slow_path_attempted,
        "sportscardsproDeferred": deferred_count,
        "sportscardspro429Count": rate_limit_429_count,
        "claimSeconds": round(phase_seconds.get("claim", 0.0), 3),
        "apiSearchSeconds": round(phase_seconds.get("api_search", 0.0), 3),
        "consoleResolveSeconds": round(phase_seconds.get("console_resolve", 0.0), 3),
        "csvFetchSeconds": round(phase_seconds.get("csv_fetch", 0.0), 3),
        "catalogWriteSeconds": round(phase_seconds.get("catalog_write", 0.0), 3),
        # Sub-phase breakdown of catalogWriteSeconds -- these won't sum
        # exactly to it (Python-side list/dict work between HTTP calls
        # isn't captured by any of the four), but they identify which
        # phase actually dominates catalog-write wall clock.
        "unchangedDetectionSeconds": round(
            catalog_write_phase_seconds.get("unchanged_detection", 0.0), 3
        ),
        "catalogUpsertSeconds": round(catalog_write_phase_seconds.get("catalog_upsert", 0.0), 3),
        "scd2ComparisonSeconds": round(catalog_write_phase_seconds.get("scd2_comparison", 0.0), 3),
        "scd2InsertSeconds": round(catalog_write_phase_seconds.get("scd2_insert", 0.0), 3),
        **_summarize_catalog_write_events(catalog_write_events),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "PRICECHARTING_API_TOKEN is required (or --api-token) -- even for "
            "--dry-run, since this worker makes real download-custom requests "
            "and only skips writing results."
        )

    registry_client = SupabaseRegistryOpsClient(
        supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
        service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        timeout_seconds=args.timeout_seconds,
    )
    catalog_client = (
        None
        if args.dry_run
        else SupabaseCatalogClient(
            supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
            service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            timeout_seconds=args.timeout_seconds,
        )
    )

    # Dry runs never write to the catalog/history tables this lock protects
    # -- skip acquiring it entirely rather than adding an unrelated Supabase
    # dependency to a --dry-run invocation.
    run_lock_client = (
        None
        if args.dry_run or args.skip_run_lock
        else SupabaseRunLockClient(
            supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
            service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            timeout_seconds=args.timeout_seconds,
        )
    )
    if run_lock_client is not None:
        acquired, held_by = run_lock_client.acquire(
            lock_name=args.run_lock_name,
            worker_id=args.worker_id,
            lease_seconds=args.run_lock_lease_seconds,
        )
        if not acquired:
            print(
                f"Run lock {args.run_lock_name!r} is currently held by {held_by!r} -- "
                f"skipping this run to avoid overlapping catalog writes.",
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "success": True,
                        "claimed": 0,
                        "runLockHeld": True,
                        "runLockHeldBy": held_by,
                    },
                    indent=2,
                ),
                flush=True,
            )
            return 0

    try:
        return _run_backfill(
            args, token=token, registry_client=registry_client, catalog_client=catalog_client
        )
    finally:
        if run_lock_client is not None:
            run_lock_client.release(lock_name=args.run_lock_name, worker_id=args.worker_id)


def _run_backfill(
    args: argparse.Namespace,
    *,
    token: str,
    registry_client: "SupabaseRegistryOpsClient",
    catalog_client: "SupabaseCatalogClient | None",
) -> int:
    phase_seconds = {
        "claim": 0.0,
        "api_search": 0.0,
        "console_resolve": 0.0,
        "csv_fetch": 0.0,
        "catalog_write": 0.0,
    }

    claim_started_at = time.perf_counter()
    claimed_rows = registry_client.claim_rows(
        limit=args.limit,
        lease_minutes=args.lease_minutes,
        worker_id=args.worker_id,
    )
    phase_seconds["claim"] = time.perf_counter() - claim_started_at
    print(f"Claimed {len(claimed_rows)} registry rows.", flush=True)
    if not claimed_rows:
        print(dump_and_report({"success": True, "claimed": 0}, indent=2), flush=True)
        return 0

    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    succeeded_ids: list[str] = []
    failed_rows: list[dict[str, Any]] = []
    total_catalog_rows = 0
    api_search_succeeded = 0
    api_search_counts = _new_api_search_counts()
    deferred_count = 0
    sportscardspro_slow_path_rows: list[dict[str, Any]] = []
    sportscardspro_429_counter = _Counter()
    catalog_write_events: list[dict[str, Any]] = []

    with httpx.Client(
        timeout=args.timeout_seconds,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as http:
        # Try the cheap, non-blocked /api/products search first for
        # sportscardspro sets -- complete for anything under the 100-result
        # cap, which skips console_uid resolution and the CSV download
        # entirely for those rows. Only sets that come back empty or
        # truncated fall through to the console_uid+CSV path below.
        sportscardspro_rows = [
            row for row in claimed_rows if row["source_site"] == "sportscardspro"
        ]
        remaining_rows = [
            row for row in claimed_rows if row["source_site"] != "sportscardspro"
        ]
        api_remaining: list[dict[str, Any]] = []
        if sportscardspro_rows:
            api_search_started_at = time.perf_counter()
            api_succeeded, api_remaining, api_search_counts = resolve_via_api_for_small_sets(
                sportscardspro_rows,
                http=http,
                token=token,
                sleep_seconds=args.sportscardspro_api_search_sleep_seconds,
                source_downloaded_at=source_downloaded_at,
                max_concurrency=args.sportscardspro_api_search_concurrency,
                rate_limit_counter=sportscardspro_429_counter,
            )
            phase_seconds["api_search"] += time.perf_counter() - api_search_started_at
            for row, catalog_rows in api_succeeded:
                total_catalog_rows += len(catalog_rows)
                if not args.dry_run and catalog_rows:
                    assert catalog_client is not None
                    write_started_at = time.perf_counter()
                    write_ok = write_catalog_rows(
                        catalog_client, catalog_rows, batch_size=args.catalog_batch_size
                    )
                    write_elapsed = time.perf_counter() - write_started_at
                    phase_seconds["catalog_write"] += write_elapsed
                    catalog_write_events.append(
                        {
                            "setNames": [row.get("set_name") or row.get("registry_id")],
                            "sourceSite": "sportscardspro",
                            "rowCount": len(catalog_rows),
                            "elapsedSeconds": write_elapsed,
                        }
                    )
                    if not write_ok:
                        failed_rows.append(row)
                        continue
                succeeded_ids.append(row["registry_id"])
                api_search_succeeded += 1
        # remaining_rows stays pricecharting-only; api_remaining (the
        # sportscardspro rows that didn't clear via API search) is handled
        # as its own group below, with its own pacing.

        # console_uid resolution and CSV download run with SEPARATE pacing
        # per site: pricecharting.com is unthrottled and keeps the caller's
        # normal pace; sportscardspro.com's large sets (whatever didn't
        # clear via the API search above) need the much slower, empirically
        # -confirmed-reliable pace to avoid the Cloudflare throttle. Each
        # call gets its own circuit breaker instance too, so a sportscardspro
        # anomaly never affects pricecharting processing or vice versa.
        #
        # Only the first --sportscardspro-slow-path-limit of api_remaining
        # actually go through this run's slow resolve step, bounding this
        # run's worst-case wall-clock time -- any beyond that are simply
        # left claimed (not touched, no failure_count penalty) to age out
        # their lease and get picked up by a later run instead.
        sportscardspro_slow_path_rows, deferred_count = cap_slow_path_rows(
            api_remaining, limit=args.sportscardspro_slow_path_limit
        )
        if deferred_count:
            print(
                f"  Deferring {deferred_count} sportscardspro set(s) to a later run "
                f"(slow-path cap {args.sportscardspro_slow_path_limit} reached).",
                flush=True,
            )
        console_resolve_started_at = time.perf_counter()
        resolved_pricecharting, resolve_failed_pricecharting = resolve_console_uids(
            remaining_rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=args.sleep_between_requests_seconds,
            dry_run=args.dry_run,
            max_concurrency=args.console_resolve_concurrency,
        )
        resolved_sportscardspro, resolve_failed_sportscardspro = resolve_console_uids(
            sportscardspro_slow_path_rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=args.sportscardspro_sleep_seconds,
            dry_run=args.dry_run,
            max_concurrency=1,
            rate_limit_counter=sportscardspro_429_counter,
        )
        phase_seconds["console_resolve"] += time.perf_counter() - console_resolve_started_at
        resolved = resolved_pricecharting + resolved_sportscardspro
        failed_rows.extend(resolve_failed_pricecharting)
        failed_rows.extend(resolve_failed_sportscardspro)

        for source_site, rows in group_by_site(resolved).items():
            base_url = SOURCE_SITE_BASE_URLS[source_site]
            is_sportscardspro = source_site == "sportscardspro"
            # CSV pacing is deliberately NOT the page-resolve pacing. Set
            # pages are ordinary HTML fetches; download-custom is a CSV call
            # and carries the published 1-per-10-minutes limit. Sharing one
            # flag between them is how the CSV limit came to be exceeded by
            # 300x on pricecharting and 20x on sportscardspro.
            site_sleep_seconds = args.csv_sleep_seconds
            csv_limiter = SharedRateLimiter(
                PRICECHARTING_CSV,
                slot_class=CLASS_BACKFILL,
                fallback_interval_seconds=site_sleep_seconds,
            )
            site_batch_size = (
                args.sportscardspro_batch_size if is_sportscardspro else args.batch_size
            )
            for index, chunk in enumerate(chunked(rows, site_batch_size)):
                if not csv_limiter.acquire(max_wait_seconds=BULK_MAX_SLOT_WAIT_SECONDS):
                    # Out of daily CSV budget, or parked behind an essential
                    # job. The API-search pre-pass above already ran, so the
                    # run is not wasted; the rest of these sets stay claimed
                    # for the next one.
                    break
                console_uids = [row["console_uid"] for row in chunk]
                print(
                    f"Fetching {source_site} batch of {len(chunk)} sets...",
                    flush=True,
                )
                csv_fetch_started_at = time.perf_counter()
                csv_text = (
                    fetch_batch_csv_with_retry(
                        http,
                        base_url=base_url,
                        token=token,
                        console_uids=console_uids,
                        max_attempts=args.sportscardspro_max_attempts,
                        # A retry is another CSV call and counts against the
                        # same published limit as the original.
                        retry_sleep_seconds=args.csv_sleep_seconds,
                        rate_limit_counter=sportscardspro_429_counter,
                    )
                    if is_sportscardspro
                    else fetch_batch_csv(
                        http, base_url=base_url, token=token, console_uids=console_uids
                    )
                )
                phase_seconds["csv_fetch"] += time.perf_counter() - csv_fetch_started_at
                if csv_text is None:
                    failed_rows.extend(chunk)
                    continue

                catalog_rows = [
                    to_catalog_row(row, f"{source_site}-set-backfill", source_downloaded_at)
                    for row in load_rows_from_text(csv_text)
                ]
                catalog_rows = [row for row in catalog_rows if row is not None]
                total_catalog_rows += len(catalog_rows)
                print(
                    f"  Parsed {len(catalog_rows)} catalog rows from this batch.",
                    flush=True,
                )
                if not args.dry_run and catalog_rows:
                    assert catalog_client is not None
                    write_started_at = time.perf_counter()
                    write_ok = write_catalog_rows(
                        catalog_client,
                        catalog_rows,
                        batch_size=args.catalog_batch_size,
                    )
                    write_elapsed = time.perf_counter() - write_started_at
                    phase_seconds["catalog_write"] += write_elapsed
                    catalog_write_events.append(
                        {
                            "setNames": [
                                row.get("set_name") or row.get("console_uid") for row in chunk
                            ],
                            "sourceSite": source_site,
                            "rowCount": len(catalog_rows),
                            "elapsedSeconds": write_elapsed,
                        }
                    )
                    if not write_ok:
                        failed_rows.extend(chunk)
                        continue

                succeeded_ids.extend(row["registry_id"] for row in chunk)

    if not args.dry_run:
        if succeeded_ids:
            registry_client.mark_success(succeeded_ids)
        if failed_rows:
            registry_client.mark_failure(failed_rows)

    print(
        dump_and_report(
            _build_result_summary(
                dry_run=args.dry_run,
                claimed_count=len(claimed_rows),
                succeeded_count=len(succeeded_ids),
                api_search_succeeded=api_search_succeeded,
                deferred_count=deferred_count,
                failed_count=len(failed_rows),
                catalog_rows_written=0 if args.dry_run else total_catalog_rows,
                catalog_rows_parsed=total_catalog_rows,
                api_search_counts=api_search_counts,
                slow_path_attempted=len(sportscardspro_slow_path_rows),
                rate_limit_429_count=sportscardspro_429_counter.value,
                phase_seconds=phase_seconds,
                catalog_write_phase_seconds=(
                    {} if catalog_client is None else catalog_client.phase_seconds
                ),
                catalog_write_events=catalog_write_events,
            ),
            indent=2,
        ),
        flush=True,
    )
    return 0


_MATCH_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")


def _normalize_for_match(value: str) -> str:
    return _MATCH_NORMALIZE_PATTERN.sub(" ", (value or "").lower()).strip()


def _console_name_strongly_matches_set_name(console_name: str, set_name: str) -> bool:
    """A "strong" match requires every word of the registry row's set_name to
    appear in the product's own console-name -- e.g. set_name "1962 Bazooka"
    matches console-name "Baseball Cards 1962 Bazooka". This is deliberately
    conservative (word-subset, not similarity-scored) because a false
    accept here silently writes a wrong/incomplete checklist to the
    catalog, while a false reject only costs an extra trip through the
    slower but always-correct console_uid+CSV path."""
    set_tokens = _normalize_for_match(set_name).split()
    console_tokens = set(_normalize_for_match(console_name).split())
    if not set_tokens or not console_tokens:
        return False
    return all(token in console_tokens for token in set_tokens)


def _products_match_set_name(products: list[dict[str, Any]], set_name: str) -> bool:
    """/api/products is a fuzzy full-text search -- live-confirmed to
    sometimes bleed in items from a different, similarly-named set (see
    refresh_small_sets.py's "Creepshow" example). Every returned product's
    own console-name must strongly match the set_name being searched for,
    or the whole result is untrustworthy as this set's complete checklist
    and must fall back to the console_uid+CSV path instead."""
    if not products:
        return False
    return all(
        _console_name_strongly_matches_set_name(
            pick_text(product, TEXT_FIELDS["console_name"]), set_name
        )
        for product in products
    )


def _new_api_search_counts() -> dict[str, int]:
    return {
        "attempted": 0,
        "succeeded": 0,
        "rejected_ambiguous": 0,
        "hit_cap": 0,
        "empty": 0,
    }


def _merge_api_search_counts(counts_list: list[dict[str, int]]) -> dict[str, int]:
    merged = _new_api_search_counts()
    for counts in counts_list:
        for key in merged:
            merged[key] += counts.get(key, 0)
    return merged


def _evaluate_api_search_products(
    products: list[dict[str, Any]] | None,
    set_name: str,
    source_downloaded_at: str,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Classifies one row's /api/products search result into exactly one
    outcome bucket -- "succeeded", "empty", "hit_cap", or "rejected_ambiguous"
    -- shared by every scheduling strategy (serial, and the controlled-
    overlap scheduler below) so the acceptance rules themselves (cap check,
    then strong-match validation) can never drift between them. catalog_rows
    is only non-None for "succeeded"."""
    if products is None or not products:
        return "empty", None
    if len(products) >= API_SEARCH_RESULT_CAP:
        return "hit_cap", None
    if not _products_match_set_name(products, set_name):
        return "rejected_ambiguous", None
    catalog_rows = [
        to_catalog_row_from_api_product(product, "sportscardspro-api-search", source_downloaded_at)
        for product in products
    ]
    catalog_rows = [catalog_row for catalog_row in catalog_rows if catalog_row is not None]
    if not catalog_rows:
        return "empty", None
    return "succeeded", catalog_rows


def resolve_via_api_for_small_sets(
    rows: list[dict[str, Any]],
    *,
    http: httpx.Client,
    token: str,
    sleep_seconds: float,
    source_downloaded_at: str,
    max_concurrency: int = 1,
    rate_limit_counter: "_Counter | None" = None,
) -> tuple[
    list[tuple[dict[str, Any], list[dict[str, Any]]]],
    list[dict[str, Any]],
    dict[str, int],
]:
    """Try the sanctioned /api/products search for each row's set name --
    cheap, not Cloudflare-blocked, and a complete result for any set with
    fewer than API_SEARCH_RESULT_CAP cards. Sets that come back empty or hit
    the cap are ambiguous/incomplete and are returned in `remaining` for the
    caller to fall back to the console_uid+CSV path instead.

    Only meaningful for sportscardspro.com rows -- pricecharting.com's CSV
    path already works directly via plain HTTP, so there's no reason to
    spend extra per-set API calls there.

    max_concurrency=1 (the default) runs the exact original single-lane
    loop, unchanged -- this default is preserved deliberately; nothing about
    the currently-deployed pacing changes unless this is explicitly raised.
    max_concurrency>1 switches to a controlled-overlap scheduler (see
    _resolve_via_api_for_small_sets_overlapped) instead of independently
    -paced lanes: individual searches vary a lot in latency (live-confirmed
    some broad/ambiguous queries take several seconds server-side even
    though the endpoint itself isn't Cloudflare-throttled), and this
    overlaps that latency without ever raising how often a new request is
    allowed to start. A dedicated circuit breaker (independent from any
    other phase's) still aborts every in-flight worker on repeated 429s.
    """
    breaker = _RateLimitCircuitBreaker(CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD)
    if max_concurrency <= 1 or len(rows) <= 1:
        return _resolve_via_api_for_small_sets_lane(
            rows,
            http=http,
            token=token,
            sleep_seconds=sleep_seconds,
            source_downloaded_at=source_downloaded_at,
            breaker=breaker,
            rate_limit_counter=rate_limit_counter,
        )

    return _resolve_via_api_for_small_sets_overlapped(
        rows,
        http=http,
        token=token,
        sleep_seconds=sleep_seconds,
        source_downloaded_at=source_downloaded_at,
        breaker=breaker,
        rate_limit_counter=rate_limit_counter,
        max_in_flight=min(max_concurrency, SPORTSCARDSPRO_API_SEARCH_MAX_IN_FLIGHT),
    )


def _resolve_via_api_for_small_sets_lane(
    rows: list[dict[str, Any]],
    *,
    http: httpx.Client,
    token: str,
    sleep_seconds: float,
    source_downloaded_at: str,
    breaker: "_RateLimitCircuitBreaker",
    rate_limit_counter: "_Counter | None" = None,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], list[dict[str, Any]], dict[str, int]]:
    succeeded: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    remaining: list[dict[str, Any]] = []
    counts = _new_api_search_counts()
    base_url = SOURCE_SITE_BASE_URLS["sportscardspro"]

    for index, row in enumerate(rows):
        if breaker.tripped:
            print(
                f"  Circuit breaker tripped (repeated 429s) -- skipping "
                f"{row.get('set_name')!r} without attempting.",
                flush=True,
            )
            remaining.append(row)
            continue
        if index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        set_name = row.get("set_name") or ""
        counts["attempted"] += 1
        products = _search_products(
            http,
            base_url=base_url,
            token=token,
            query=set_name,
            breaker=breaker,
            rate_limit_counter=rate_limit_counter,
        )
        outcome, catalog_rows = _evaluate_api_search_products(
            products, set_name, source_downloaded_at
        )
        counts[outcome] += 1
        if outcome == "succeeded":
            print(
                f"  API search complete for {set_name!r}: "
                f"{len(catalog_rows)} cards.",
                flush=True,
            )
            succeeded.append((row, catalog_rows))
        else:
            if outcome == "rejected_ambiguous":
                print(
                    f"  API search for {set_name!r} returned ambiguous/mismatched "
                    f"console names -- falling back to slow path.",
                    flush=True,
                )
            remaining.append(row)

    return succeeded, remaining, counts


def _resolve_via_api_for_small_sets_overlapped(
    rows: list[dict[str, Any]],
    *,
    http: httpx.Client,
    token: str,
    sleep_seconds: float,
    source_downloaded_at: str,
    breaker: "_RateLimitCircuitBreaker",
    max_in_flight: int,
    rate_limit_counter: "_Counter | None" = None,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], list[dict[str, Any]], dict[str, int]]:
    """Controlled-overlap scheduler: unlike the round-robin-lane concurrency
    used elsewhere in this file (independent per-lane pacing -- already
    proven on 2026-08-09 to burst when lanes' timers align), every worker
    here shares one _StartRateLimiter gating request starts to no more than
    one per sleep_seconds, globally. The worker COUNT itself (capped at
    max_in_flight, not a separate semaphore) is what bounds how many
    requests can genuinely be in flight at once -- a slow response from one
    worker just means the next worker's already-scheduled request overlaps
    it instead of queueing serially behind it; the request-start cadence is
    unchanged either way. Workers pull from a shared queue rather than a
    static round-robin split so no worker can race ahead of the others."""
    work_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
    for row in rows:
        work_queue.put(row)

    rate_limiter = _StartRateLimiter(sleep_seconds)
    base_url = SOURCE_SITE_BASE_URLS["sportscardspro"]
    results_lock = threading.Lock()
    succeeded: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    remaining: list[dict[str, Any]] = []
    counts = _new_api_search_counts()

    def worker() -> None:
        while True:
            try:
                row = work_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if breaker.tripped:
                    print(
                        f"  Circuit breaker tripped (repeated 429s) -- skipping "
                        f"{row.get('set_name')!r} without attempting.",
                        flush=True,
                    )
                    with results_lock:
                        remaining.append(row)
                    continue

                # The rate gate -- not a per-worker sleep -- is what keeps
                # request starts at the same cadence as the original serial
                # loop regardless of worker count.
                rate_limiter.wait_for_slot()

                set_name = row.get("set_name") or ""
                with results_lock:
                    counts["attempted"] += 1
                products = _search_products(
                    http,
                    base_url=base_url,
                    token=token,
                    query=set_name,
                    breaker=breaker,
                    rate_limit_counter=rate_limit_counter,
                )
                outcome, catalog_rows = _evaluate_api_search_products(
                    products, set_name, source_downloaded_at
                )
                with results_lock:
                    counts[outcome] += 1
                    if outcome == "succeeded":
                        succeeded.append((row, catalog_rows))
                    else:
                        remaining.append(row)
                if outcome == "succeeded":
                    print(
                        f"  API search complete for {set_name!r}: "
                        f"{len(catalog_rows)} cards.",
                        flush=True,
                    )
                elif outcome == "rejected_ambiguous":
                    print(
                        f"  API search for {set_name!r} returned ambiguous/mismatched "
                        f"console names -- falling back to slow path.",
                        flush=True,
                    )
            finally:
                work_queue.task_done()

    worker_count = max(1, min(max_in_flight, len(rows)))
    threads = [threading.Thread(target=worker) for _ in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return succeeded, remaining, counts


def _redact_token(text: str, token: str) -> str:
    """PRICECHARTING_API_TOKEN travels as a URL query param (?t=...), and
    httpx's default exception message embeds the full request URL -- an
    unredacted print() of that exception would put a live secret straight
    into Render's cron log output. Plain string replacement (not URL
    parsing) so it catches the token wherever it shows up in the message,
    not just in a query string."""
    if not token:
        return text
    return text.replace(token, "***REDACTED***")


def _search_products(
    http: httpx.Client,
    *,
    base_url: str,
    token: str,
    query: str,
    breaker: "_RateLimitCircuitBreaker | None" = None,
    rate_limit_counter: "_Counter | None" = None,
) -> list[dict[str, Any]] | None:
    if not query:
        return None
    try:
        response = http.get(
            f"{base_url}/api/products",
            params={"t": token, "q": query},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        print(f"  API search failed for {query!r}: {_redact_token(str(exc), token)}", flush=True)
        if exc.response.status_code == 429:
            if breaker is not None:
                breaker.record_rate_limited()
            if rate_limit_counter is not None:
                rate_limit_counter.increment()
        return None
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  API search failed for {query!r}: {_redact_token(str(exc), token)}", flush=True)
        return None
    if breaker is not None:
        breaker.record_success()
    if payload.get("status") != "success":
        return None
    return payload.get("products") or []


def resolve_console_uids(
    rows: list[dict[str, Any]],
    *,
    http: httpx.Client,
    registry_client: "SupabaseRegistryOpsClient",
    sleep_seconds: float,
    dry_run: bool,
    max_concurrency: int = 1,
    rate_limit_counter: "_Counter | None" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # This resolve phase (one plain page fetch per unresolved set, looking
    # for the VGPC.console_uid script tag) dominates a backfill run's wall
    # clock -- confirmed live, ~5min of pure sleep()/request time for 150
    # sets at the default 2s serial pace, before any CSV download or catalog
    # write happens. max_concurrency>1 shards rows round-robin across N
    # independent lanes run in a thread pool; each lane keeps the exact same
    # per-request sleep_seconds pacing as before, so this scales wall-clock
    # throughput without changing how "gentle" any single lane looks to
    # PriceCharting/SportsCardsPro -- N lanes each requesting at the old
    # pace, not one lane requesting N times faster. max_concurrency=1 (the
    # default) runs the exact original single-lane loop, unchanged.
    breaker = _RateLimitCircuitBreaker(CONSECUTIVE_RATE_LIMIT_ABORT_THRESHOLD)
    if max_concurrency <= 1 or len(rows) <= 1:
        resolved, failed, newly_resolved = _resolve_console_uids_lane(
            rows,
            http=http,
            sleep_seconds=sleep_seconds,
            breaker=breaker,
            rate_limit_counter=rate_limit_counter,
        )
    else:
        lanes = _shard_round_robin(rows, max_concurrency)
        resolved, failed, newly_resolved = [], [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            lane_results = pool.map(
                lambda lane: _resolve_console_uids_lane(
                    lane,
                    http=http,
                    sleep_seconds=sleep_seconds,
                    breaker=breaker,
                    rate_limit_counter=rate_limit_counter,
                ),
                lanes,
            )
            for lane_resolved, lane_failed, lane_newly_resolved in lane_results:
                resolved.extend(lane_resolved)
                failed.extend(lane_failed)
                newly_resolved.extend(lane_newly_resolved)

    if newly_resolved and not dry_run:
        registry_client.update_console_uids(newly_resolved)

    return resolved, failed


def _resolve_console_uids_lane(
    rows: list[dict[str, Any]],
    *,
    http: httpx.Client,
    sleep_seconds: float,
    breaker: "_RateLimitCircuitBreaker",
    rate_limit_counter: "_Counter | None" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    newly_resolved: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        if row.get("console_uid"):
            resolved.append(row)
            continue
        if breaker.tripped:
            print(
                f"  Circuit breaker tripped (repeated 429s) -- skipping "
                f"{row['url']} without attempting.",
                flush=True,
            )
            failed.append(row)
            continue
        if index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        try:
            response = http.get(row["url"])
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"  Failed to resolve console_uid for {row['url']}: {exc}", flush=True)
            failed.append(row)
            if exc.response.status_code == 429:
                breaker.record_rate_limited()
                if rate_limit_counter is not None:
                    rate_limit_counter.increment()
            continue
        except httpx.HTTPError as exc:
            print(f"  Failed to resolve console_uid for {row['url']}: {exc}", flush=True)
            failed.append(row)
            continue
        match = CONSOLE_UID_PATTERN.search(response.text)
        if not match:
            print(f"  No console_uid found on {row['url']}.", flush=True)
            failed.append(row)
            breaker.record_success()
            continue
        row["console_uid"] = match.group(1)
        resolved.append(row)
        newly_resolved.append(row)
        breaker.record_success()

    return resolved, failed, newly_resolved


def _shard_round_robin(items: list[Any], shard_count: int) -> list[list[Any]]:
    shards: list[list[Any]] = [[] for _ in range(shard_count)]
    for index, item in enumerate(items):
        shards[index % shard_count].append(item)
    return [shard for shard in shards if shard]


def cap_slow_path_rows(
    rows: list[dict[str, Any]], *, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Bounds how many rows enter the slow (30s/row) resolve path in a
    single run. Returns (rows_to_process, deferred_count) -- rows beyond
    the limit are simply not included; they stay claimed and age out their
    lease for a later run to pick up, no other action needed here."""
    if limit <= 0:
        return [], len(rows)
    capped = rows[:limit]
    return capped, max(0, len(rows) - len(capped))


def group_by_site(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["source_site"], []).append(row)
    return grouped


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("size must be greater than zero")
    return [items[i : i + size] for i in range(0, len(items), size)]


def write_catalog_rows_with_retry(
    catalog_client: SupabaseCatalogClient,
    catalog_rows: list[dict[str, Any]],
    *,
    batch_size: int,
    attempts: int,
    backoff_seconds: float,
    sleep: Any = time.sleep,
) -> tuple[bool, int]:
    """write_catalog_rows() with bounded retries. Returns (wrote, retries).

    The CSV behind these rows already cost a throttled sportscardspro
    request. Giving up on the first write failure means re-fetching
    identical bytes next cycle -- paying that budget twice for data we
    already hold. The failure this guards is a Postgres statement timeout
    under load (see write_catalog_rows), which is transient, and upserts
    are idempotent: rows that already landed are skipped by the
    content-hash diff, so a retry redoes almost nothing.

    Backoff is deliberately NOT the sportscardspro pacing interval. That
    delay exists to protect their servers; this contention is our own
    database, and coupling the two would make write pressure cost
    throttle budget it has nothing to do with.
    """
    # "failed" and "skippedUnchanged" are cumulative counters on the client,
    # so a retry re-counts rows the previous attempt already tallied: the
    # failed sub-batch is counted again, and every unchanged row it re-reads
    # is skipped again. Left alone that makes the ledger self-contradictory
    # -- a 2026-09-01 tier-3 run reported catalogRowsFailed 31,766 with
    # failedWrites 0, and skippedUnchanged (3,023,794) larger than
    # catalogRowsParsed (1,908,562), which cannot describe real rows.
    #
    # "written" is genuinely cumulative (each attempt writes DIFFERENT rows --
    # what already landed is skipped by the content-hash diff), so only the
    # two re-counted fields are rebased, and only once the batch succeeds.
    stats = getattr(catalog_client, "catalog_write_stats", None)
    baseline = dict(stats) if stats is not None else None

    retries = 0
    for attempt in range(1, max(1, attempts) + 1):
        before_attempt = dict(stats) if stats is not None else None
        if write_catalog_rows(catalog_client, catalog_rows, batch_size=batch_size):
            if stats is not None and baseline is not None and before_attempt is not None:
                for key in ("failed", "skippedUnchanged"):
                    if key in stats:
                        this_attempt = stats[key] - before_attempt.get(key, 0)
                        stats[key] = baseline.get(key, 0) + this_attempt
            return True, retries
        if attempt < attempts:
            retries += 1
            wait = backoff_seconds * attempt
            print(
                f"  Catalog write failed; retrying ({attempt + 1}/{attempts}) "
                f"in {wait:.0f}s without re-fetching...",
                flush=True,
            )
            sleep(wait)
    return False, retries


def write_catalog_rows(
    catalog_client: SupabaseCatalogClient,
    catalog_rows: list[dict[str, Any]],
    *,
    batch_size: int,
) -> bool:
    # A single slow/failing catalog write (e.g. a Postgres statement timeout
    # on a large table) must not crash the whole run and cost every other
    # already-processed chunk its mark_success. Whatever landed before the
    # failure is already committed (idempotent upserts), so the only cost of
    # treating this chunk as failed is a retry next cycle, not lost data.
    #
    # sync_scd2_history_rows()/upsert_rows() each attempt every sub-batch
    # regardless of earlier sub-batch failures (see PartialCatalogWriteError)
    # -- so even on a partial failure here, only the genuinely-failed rows'
    # worth of work is left to redo next cycle, not the whole chunk from
    # scratch. This whole registry-row chunk still gets marked failed either
    # way (there's no cheap way to know which specific registry rows/sets a
    # failed catalog row belonged to once batched into one CSV parse), but
    # the retry converges fast since almost everything already landed.
    try:
        catalog_client.sync_scd2_history_rows(catalog_rows, batch_size=batch_size)
        catalog_client.upsert_rows(catalog_rows, batch_size=batch_size)
    except PartialCatalogWriteError as exc:
        print(
            f"  Catalog write partially failed for this batch "
            f"({exc.succeeded_count} succeeded, {len(exc.failed_ids)} failed) "
            f"-- will retry next cycle: {exc}",
            flush=True,
        )
        return False
    except (SystemExit, Exception) as exc:
        print(f"  Catalog write failed for this batch, will retry next cycle: {exc}", flush=True)
        return False
    return True


def fetch_batch_csv(
    http: httpx.Client,
    *,
    base_url: str,
    token: str,
    console_uids: list[str],
    rate_limit_counter: "_Counter | None" = None,
    blocked_counter: "_Counter | None" = None,
    status_sink: list[int] | None = None,
) -> str | None:
    """status_sink, when given, receives the HTTP status of a failed
    response. Callers need it to tell the three failure modes apart: 429
    (slow down), 403 (Cloudflare refusing us), and anything else -- which
    for this endpoint means a specific console_uid its backend cannot
    serve, and is worth isolating rather than abandoning the batch."""
    try:
        response = http.get(
            f"{base_url}/price-guide/download-custom",
            params={"t": token, "console-uids": ",".join(console_uids)},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"  Batch download failed for {len(console_uids)} sets: "
            f"{_redact_token(str(exc), token)}",
            flush=True,
        )
        status = exc.response.status_code
        if status_sink is not None:
            status_sink.append(status)
        if rate_limit_counter is not None and status == 429:
            rate_limit_counter.increment()
        # 403 is Cloudflare refusing us outright, not asking us to slow
        # down. It has to feed the breaker too: on 2026-08-31 a run took
        # 403s on every batch, and only tripped because a few 429s
        # happened to appear alongside them. A pure-403 run would have
        # burned all 120 throttled requests against a blocked endpoint --
        # the surest way to turn a temporary block into a durable one.
        if blocked_counter is not None and status == 403:
            blocked_counter.increment()
        return None
    except httpx.HTTPError as exc:
        print(
            f"  Batch download failed for {len(console_uids)} sets: "
            f"{_redact_token(str(exc), token)}",
            flush=True,
        )
        return None
    return response.text


def fetch_batch_csv_with_retry(
    http: httpx.Client,
    *,
    base_url: str,
    token: str,
    console_uids: list[str],
    max_attempts: int,
    retry_sleep_seconds: float,
    rate_limit_counter: "_Counter | None" = None,
    blocked_counter: "_Counter | None" = None,
    status_sink: list[int] | None = None,
) -> str | None:
    # Retries are a safety margin for occasional misses, recovering inside the
    # same run rather than waiting for the next cron cycle. retry_sleep_seconds
    # must be the CSV interval, not a shorter "we're just retrying" pause: a
    # retry is a CSV call and counts against the published 1-per-10-minutes
    # limit exactly like the attempt it follows.
    for attempt in range(1, max_attempts + 1):
        csv_text = fetch_batch_csv(
            http,
            base_url=base_url,
            token=token,
            console_uids=console_uids,
            rate_limit_counter=rate_limit_counter,
            blocked_counter=blocked_counter,
            status_sink=status_sink,
        )
        if csv_text is not None:
            return csv_text
        if attempt < max_attempts:
            print(
                f"  Retrying batch download (attempt {attempt + 1}/{max_attempts}) "
                f"after {retry_sleep_seconds:.0f}s...",
                flush=True,
            )
            time.sleep(retry_sleep_seconds)
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill PriceCharting/SportsCardsPro comics/coins/sports-card sets "
            "from pricecharting_set_registry into the shared pricecharting_catalog tables."
        )
    )
    parser.add_argument("--limit", type=int, default=200, help="Registry rows to claim this run.")
    parser.add_argument("--batch-size", type=int, default=150, help="Sets per download-custom request.")
    parser.add_argument(
        "--catalog-batch-size",
        type=int,
        default=100,
        help=(
            "Rows per pricecharting_catalog upsert request. Kept smaller than "
            "the history-insert batches: the upsert does a hash lookup plus a "
            "conflict-resolving write per row, so it hits Postgres's statement "
            "timeout at a much lower row count than a plain insert does."
        ),
    )
    parser.add_argument("--lease-minutes", type=float, default=30)
    parser.add_argument("--worker-id", default=os.getenv("RENDER_INSTANCE_ID", "backfill-worker"))
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--sleep-between-requests-seconds",
        type=float,
        default=2.0,
        help="Pace between set-page (console_uid) HTML resolves. NOT used for "
        "CSV downloads -- see --csv-sleep-seconds.",
    )
    parser.add_argument(
        "--csv-sleep-seconds",
        type=float,
        default=CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
        help="Pace between /price-guide/download-custom calls. Defaults to the "
        "published limit of one CSV call every 10 minutes. Do not lower this "
        "without a written exception -- the vendor documents revoking account "
        "permissions for persistent limit violations.",
    )
    parser.add_argument(
        "--console-resolve-concurrency",
        type=int,
        default=1,
        help=(
            "Number of parallel lanes resolving console_uids. Each lane keeps "
            "the same --sleep-between-requests-seconds pacing independently, "
            "so this multiplies wall-clock throughput without any single lane "
            "requesting faster than before. Default 1 preserves the original "
            "fully-serial behavior."
        ),
    )
    parser.add_argument(
        "--sportscardspro-sleep-seconds",
        type=float,
        default=SPORTSCARDSPRO_DEFAULT_SLEEP_SECONDS,
        help=(
            "Pace for sportscardspro.com console_uid resolution and CSV "
            "requests -- separate from --sleep-between-requests-seconds "
            "since sportscardspro.com throttles much more aggressively "
            "(confirmed live: ~13%% success at 2s, 80%% at 15s, 100%% at 30s). "
            "pricecharting.com is unaffected and keeps the normal pace."
        ),
    )
    parser.add_argument(
        "--sportscardspro-max-attempts",
        type=int,
        default=SPORTSCARDSPRO_DEFAULT_MAX_ATTEMPTS,
        help="In-run retry attempts for a sportscardspro CSV batch before giving up.",
    )
    parser.add_argument(
        "--sportscardspro-batch-size",
        type=int,
        default=SPORTSCARDSPRO_DEFAULT_BATCH_SIZE,
        help=(
            "Sets per sportscardspro.com download-custom request -- kept far "
            "smaller than --batch-size (150, used for pricecharting.com) "
            "since some sports-card sets are large enough that a batch of "
            "20 produced 26,450+ rows and OOM-killed a 512Mi instance."
        ),
    )
    parser.add_argument(
        "--sportscardspro-slow-path-limit",
        type=int,
        default=SPORTSCARDSPRO_DEFAULT_SLOW_PATH_LIMIT,
        help=(
            "Max sportscardspro rows resolved via the slow (30s/row) "
            "console_uid+CSV path per run, bounding worst-case wall-clock "
            "time regardless of --limit. Excess rows are left claimed and "
            "picked up by a later run once their lease expires."
        ),
    )
    parser.add_argument(
        "--sportscardspro-api-search-sleep-seconds",
        type=float,
        default=0.5,
        help=(
            "Pace for the /api/products search pre-pass over sportscardspro "
            "rows -- separate from --sleep-between-requests-seconds since "
            "this endpoint is confirmed NOT Cloudflare-throttled (unlike "
            "console_uid pages/CSV downloads), live-confirmed reliable at "
            "0.5s from Render's own outbound IP."
        ),
    )
    parser.add_argument(
        "--sportscardspro-api-search-concurrency",
        type=int,
        default=1,
        help=(
            "Parallel lanes for the /api/products search pre-pass. Individual "
            "searches vary widely in latency (some broad/ambiguous queries "
            "take several seconds server-side even though the endpoint isn't "
            "throttled), so concurrency overlaps those slow searches instead "
            "of serializing them -- same round-robin-lane design as "
            "--console-resolve-concurrency. Default 1 preserves the original "
            "fully-serial behavior."
        ),
    )
    parser.add_argument("--api-token", default="", help="Defaults to PRICECHARTING_API_TOKEN.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument("--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY.")
    parser.add_argument(
        "--run-lock-name",
        default="pricecharting_backfill",
        help=(
            "Name of the row in backfill_run_lock this run acquires before "
            "claiming/writing anything, so two overlapping invocations of this "
            "script never write to pricecharting_catalog/_history at the same "
            "time (Render cron jobs can genuinely overlap -- a manually "
            "triggered run is not blocked from starting while the previous "
            "scheduled run is still executing)."
        ),
    )
    parser.add_argument(
        "--run-lock-lease-seconds",
        type=float,
        default=10800,
        help=(
            "Dead-man's-switch TTL on the run lock, not the primary release "
            "mechanism -- this run releases explicitly in a finally block on "
            "every normal exit (including on error). Only matters if the "
            "process is killed outright (e.g. an OOM kill) before that finally "
            "block runs. 10800s (3h) is set with margin above a live-observed "
            "catalogWriteSeconds of ~7324s (~2.03h) alone for one run (219,628 "
            "rows) -- if the lease is shorter than a legitimate slow run, a "
            "second run could acquire the lock while the first is still "
            "writing, which is exactly the failure mode this exists to "
            "prevent. Revisit downward once the write-path instrumentation "
            "added alongside this lock (unchangedDetectionSeconds/"
            "catalogUpsertSeconds/scd2ComparisonSeconds/scd2InsertSeconds/"
            "topSlowestCatalogWrites) shows what's actually driving that time "
            "and it's been brought down."
        ),
    )
    parser.add_argument(
        "--skip-run-lock",
        action="store_true",
        help="Skip acquiring the run lock -- for local testing only.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


class SupabaseRunLockClient:
    """Prevents two backfill_pricecharting_sets.py runs from writing to the
    catalog/history tables at the same time. Render cron jobs can genuinely
    overlap -- a manually triggered run starting while the previous
    scheduled run is still executing is not prevented by Render itself
    (live-observed 2026-08-10) -- and two runs both hammering the same
    upsert/SCD2 write paths concurrently was a suspected contributor to a
    live-observed ~30 rows/sec catalog-write slowdown.

    Uses the same acquire-throttle RPC pattern already established by
    pricing_provider_throttle (see database/migrations/20260728_create_
    pricing_provider_throttle.sql and app/services/pricing/cache.py): a
    Postgres function wraps a pg_advisory_xact_lock around a real row's
    read-check-write, so the acquire is atomic even though this codebase
    never holds a persistent DB connection across HTTP calls -- see
    database/migrations/20260811_create_backfill_run_lock.sql.
    """

    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds

    def acquire(
        self, *, lock_name: str, worker_id: str, lease_seconds: float
    ) -> tuple[bool, str | None]:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                f"{self.supabase_url}/rest/v1/rpc/acquire_backfill_run_lock",
                headers=self._headers(),
                json={
                    "lock_name_arg": lock_name,
                    "worker_id_arg": worker_id,
                    "lease_seconds_arg": int(lease_seconds),
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase run-lock acquire failed with HTTP {response.status_code}: "
                    f"{response.text}"
                ) from exc
            payload = response.json()
        row = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(row, dict):
            return False, None
        return bool(row.get("acquired")), row.get("locked_by")

    def release(self, *, lock_name: str, worker_id: str) -> None:
        # Best-effort: a failure here must not crash a run that already
        # completed its real work -- the lease TTL is the backstop for a
        # lock that never gets released.
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.supabase_url}/rest/v1/rpc/release_backfill_run_lock",
                    headers=self._headers(),
                    json={"lock_name_arg": lock_name, "worker_id_arg": worker_id},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"  Warning: failed to release run lock: {exc}", flush=True)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


class SupabaseRegistryOpsClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def claim_rows(self, *, limit: int, lease_minutes: float, worker_id: str) -> list[dict[str, Any]]:
        lease_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lease_minutes)).isoformat()
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={
                    "select": "registry_id,source_site,url,console_uid,failure_count,set_name",
                    # Both conditions matter: the claimed_at OR-group picks
                    # never-claimed/lease-expired rows; the last_fetch_status
                    # OR-group excludes rows already marked "success" so they
                    # stop re-entering the batch forever (mark_success() nulls
                    # claimed_at on completion, which otherwise makes a done
                    # row indistinguishable from a never-attempted one -- with
                    # priority_tier ordering, that let coins/comics rows
                    # perpetually re-fill every batch and starve every lower
                    # -tier category of a single run). Failed ("error") and
                    # never-attempted (null) rows still pass through to retry.
                    "and": (
                        f"(or(claimed_at.is.null,claimed_at.lt.{lease_cutoff}),"
                        "or(last_fetch_status.is.null,last_fetch_status.neq.success))"
                    ),
                    "order": "priority_tier.asc,last_fetched_at.asc.nullsfirst",
                    "limit": str(limit),
                },
                headers=self._headers(),
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase registry claim query failed with HTTP {response.status_code}: {response.text}"
                ) from exc
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                return []

            registry_ids = [row["registry_id"] for row in rows]
            patch_response = client.patch(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={"registry_id": f"in.({','.join(registry_ids)})"},
                headers={**self._headers(), "Prefer": "return=minimal"},
                json={"claimed_at": datetime.now(timezone.utc).isoformat(), "claimed_by": worker_id},
            )
            try:
                patch_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase registry claim patch failed with HTTP {patch_response.status_code}: {patch_response.text}"
                ) from exc
            return rows

    def update_console_uids(self, rows: list[dict[str, Any]]) -> None:
        # PATCH per row, not a batched upsert: PostgREST's upsert builds a full
        # candidate row to check NOT NULL constraints before it even looks at
        # the conflict, so a partial-column payload like {registry_id,
        # console_uid} fails against this table's NOT NULL columns even though
        # the row already exists. PATCH only ever touches the columns sent.
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for row in rows:
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"eq.{row['registry_id']}"},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                    json={"console_uid": row["console_uid"]},
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SystemExit(
                        f"Supabase console_uid update failed for {row['registry_id']} "
                        f"with HTTP {response.status_code}: {response.text}"
                    ) from exc

    def mark_success(self, registry_ids: list[str]) -> None:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.patch(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={"registry_id": f"in.({','.join(registry_ids)})"},
                headers={**self._headers(), "Prefer": "return=minimal"},
                json={
                    "last_fetched_at": datetime.now(timezone.utc).isoformat(),
                    "last_fetch_status": "success",
                    "failure_count": 0,
                    "claimed_at": None,
                    "claimed_by": None,
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase mark-success failed with HTTP {response.status_code}: {response.text}"
                ) from exc

    def mark_failure(self, rows: list[dict[str, Any]]) -> None:
        # PATCH per row (see update_console_uids for why a batched upsert
        # can't be used here) -- each row needs its own failure_count value.
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for row in rows:
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"eq.{row['registry_id']}"},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                    json={
                        "last_fetched_at": datetime.now(timezone.utc).isoformat(),
                        "last_fetch_status": "error",
                        "failure_count": int(row.get("failure_count") or 0) + 1,
                        "claimed_at": None,
                        "claimed_by": None,
                    },
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SystemExit(
                        f"Supabase mark-failure update failed for {row['registry_id']} "
                        f"with HTTP {response.status_code}: {response.text}"
                    ) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


if __name__ == "__main__":
    raise SystemExit(run_with_recorder("pricecharting-sets-backfill", main))
