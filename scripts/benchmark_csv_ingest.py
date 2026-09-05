"""Measure peak RSS across the WHOLE bulk-CSV pipeline, not just the fetch.

A streamed download that is then transformed into one big list has simply
moved the spike downstream, so this samples RSS continuously and reports a
true peak per phase: download, parse+transform, and database write.

Run it in a Render shell on the same 256 MB class of container the cron uses:

    python -m scripts.benchmark_csv_ingest --sets 100
    # wait out the CSV interval, then
    python -m scripts.benchmark_csv_ingest --sets 300

Each run takes ONE CSV slot through the shared limiter, so it cannot breach
the published one-call-per-10-minutes limit even if run back to back.
"""

import argparse
import os
import threading
import time
from pathlib import Path

import httpx

from scripts._shared_rate_limiter import (
    CLASS_TIER3,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)
from scripts.backfill_pricecharting_sets import (
    CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
    _Counter,
    REQUEST_HEADERS,
    cleanup_csv_downloads,
    fetch_batch_csv_file,
    write_catalog_rows_with_retry,
)
from scripts.refresh_sportscardspro_rotation import TIER3_MAX_FAILURES
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    chunked_iter,
    iter_rows_from_file,
    to_catalog_row,
)

CSV_BASE_URL = "https://www.pricecharting.com"

# Refresh-age ordering says nothing about set SIZE, and sizes vary by two
# orders of magnitude -- a 300-set sample drawn that way could easily be a
# friendly one and report a peak the real rotation never sees. These are
# console_uids whose row counts were measured directly from download-custom
# responses on 2026-09-04, seeded into every sample so the benchmark always
# carries some genuinely heavy sets:
#
#   G47162  2021 Panini Mosaic (baseball)      10,589 rows
#   G66750  2023 Topps Chrome Update            6,289 rows
#   G66421  2022 Topps Simplicidad UEFA           640 rows
#   G63100  2021 Topps Chrome F1 Autographs       318 rows
#
# Not a row-count feature, just four known-heavy ids.
KNOWN_LARGE_UIDS = ["G47162", "G66750", "G66421", "G63100"]


def _rss_mb() -> float:
    """Linux VmRSS. Falls back to ru_maxrss (a high-water mark, so phase
    peaks will read high) where /proc is unavailable, e.g. macOS."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss / (1024 * 1024) if maxrss > 1 << 20 else maxrss / 1024


class Sampler:
    """Background RSS sampler. ru_maxrss only ever rises, so it cannot show
    that memory was released between phases -- this can."""

    def __init__(self, interval: float = 0.05):
        self.interval, self.peak, self._stop = interval, 0.0, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            self.peak = max(self.peak, _rss_mb())

    def __enter__(self):
        self.peak = _rss_mb()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1)
        self.peak = max(self.peak, _rss_mb())
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sets", type=int, default=100)
    parser.add_argument("--ingest-chunk-rows", type=int, default=10_000)
    parser.add_argument("--catalog-batch-size", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the database write. Only for checking the fetch/parse "
        "half; a run with this set does NOT validate a batch size.",
    )
    args = parser.parse_args(argv)

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    token = os.getenv("PRICECHARTING_API_TOKEN") or os.getenv("PRICECHARTING_API_KEY", "")
    if not (supabase_url and key and token):
        raise SystemExit("SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and a token are required.")

    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    registry = httpx.get(
        f"{supabase_url}/rest/v1/pricecharting_set_registry",
        params={
            "select": "console_uid,set_name",
            "source_site": "eq.sportscardspro",
            "console_uid": "not.is.null",
            "last_fetch_status": "eq.success",
            # Must match the rotation's own filter. Sets confirmed dead
            # upstream keep tier3_refreshed_at NULL forever, so under
            # nullsfirst they sort to the very front of every sample -- and
            # download-custom fails the ENTIRE request if one uid in it
            # cannot be served, so omitting this filter 503s every batch.
            "tier3_failure_count": f"lt.{TIER3_MAX_FAILURES}",
            "order": (
                "tier3_failure_count.asc,"
                "tier3_refreshed_at.asc.nullsfirst,"
                "registry_id.asc"
            ),
            "limit": str(args.sets),
        },
        headers=headers,
        timeout=60,
    ).json()
    sampled = [row["console_uid"] for row in registry]
    seeds = [u for u in KNOWN_LARGE_UIDS if u not in sampled]
    # Seeds replace the tail rather than extending it, so --sets is exact.
    uids = (seeds + sampled)[: args.sets]
    print(f"sets requested        : {len(uids)}  "
          f"({len(seeds)} known-large seeded, {len(uids) - len(seeds)} by refresh age)")

    baseline = _rss_mb()
    print(f"baseline RSS          : {baseline:.0f} MB")

    limiter = SharedRateLimiter(
        PRICECHARTING_CSV,
        slot_class=CLASS_TIER3,
        fallback_interval_seconds=CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
    )
    # acquire() blocks until the shared 610s gate allows a call, so running
    # these back to back is safe -- do NOT space them by hand.
    print("waiting for a CSV slot (shared limiter enforces the 610s gate)...", flush=True)
    if not limiter.acquire():
        raise SystemExit("no CSV slot available (class out of daily budget)")

    status_sink: list[int] = []
    rate_counter, blocked_counter = _Counter(), _Counter()
    started = time.perf_counter()
    with httpx.Client(
        timeout=args.timeout_seconds, follow_redirects=True, headers=REQUEST_HEADERS
    ) as http:
        with Sampler() as download_sampler:
            fetch_started = time.perf_counter()
            download = fetch_batch_csv_file(
                http,
                base_url=CSV_BASE_URL,
                token=token,
                console_uids=uids,
                status_sink=status_sink,
                rate_limit_counter=rate_counter,
                blocked_counter=blocked_counter,
            )
            fetch_seconds = time.perf_counter() - fetch_started
        if download is None:
            raise SystemExit(
                f"download failed -- http status {status_sink or 'transport error'}, "
                f"429s={rate_counter.value} 403s={blocked_counter.value}"
            )

        size_mb = download.path.stat().st_size / (1024 * 1024)
        rss_after_download = _rss_mb()
        print(f"downloaded            : {size_mb:.1f} MB in {fetch_seconds:.1f}s")
        print(f"peak RSS (download)   : {download_sampler.peak:.0f} MB")

        stamp = "benchmark"
        catalog_client = None if args.dry_run else SupabaseCatalogClient(
            supabase_url=supabase_url, service_role_key=key,
            timeout_seconds=args.timeout_seconds,
        )
        rows = written = 0
        parse_peak = write_peak = 0.0
        parse_seconds = write_seconds = 0.0
        errors = 0
        try:
            def _iter_rows():
                for raw in iter_rows_from_file(
                    download.path, encoding=download.encoding
                ):
                    row = to_catalog_row(raw, stamp, "1970-01-01T00:00:00+00:00")
                    if row is not None:
                        yield row

            # One sampler spans parse+transform+write so nothing between
            # phases is missed, and a second isolates the write calls. The
            # difference attributes the spike to parsing or to the writer.
            ingest_started = time.perf_counter()
            with Sampler() as ingest_sampler:
                for chunk in chunked_iter(_iter_rows(), args.ingest_chunk_rows):
                    rows += len(chunk)
                    if catalog_client is None:
                        continue
                    with Sampler() as write_sampler:
                        write_started = time.perf_counter()
                        ok, _retried = write_catalog_rows_with_retry(
                            catalog_client, chunk,
                            batch_size=args.catalog_batch_size,
                            attempts=2, backoff_seconds=2.0,
                        )
                        write_seconds += time.perf_counter() - write_started
                    write_peak = max(write_peak, write_sampler.peak)
                    if not ok:
                        errors += 1
                        break
            parse_peak = ingest_sampler.peak
            parse_seconds = (time.perf_counter() - ingest_started) - write_seconds
        finally:
            cleanup_csv_downloads([download])
        rss_after_ingest = _rss_mb()

    stats = getattr(catalog_client, "catalog_write_stats", None) or {}
    history = getattr(catalog_client, "price_history_stats", None) or {}
    total_seconds = time.perf_counter() - started
    overall = max(download_sampler.peak, parse_peak, write_peak)
    print(f"csv rows parsed       : {rows:,}")
    print(f"peak RSS (parse+write): {parse_peak:.0f} MB  ({parse_seconds:.1f}s parsing)")
    print(f"peak RSS (db write)   : {write_peak:.0f} MB  ({write_seconds:.1f}s writing)")
    print(f"catalog rows written  : {stats.get('written', 0):,}")
    print(f"rows skipped unchanged: {stats.get('skippedUnchanged', 0):,}")
    print(f"scd2 versions created : {history.get('inserted', 0):,}")
    print(f"scd2 duplicates skipped: {history.get('duplicateSkipped', 0):,}")
    print(f"scd2 write failures   : {history.get('failed', 0):,}")
    print(f"write failures        : {errors}")
    print("sets stamped          : 0 (deliberate -- stamping would mutate the "
          "rotation; its cost is one PATCH body)")
    print(f"http duration         : {fetch_seconds:.1f}s")
    print(f"end-to-end duration   : {total_seconds:.1f}s")
    print(f"http status           : 200  (429s={rate_counter.value} "
          f"403s={blocked_counter.value})")
    print(f"temp file removed     : {not download.path.exists()}")
    # Settled RSS between phases separates a transient spike from retained
    # structures: parsing that reaches 120 MB and falls back to 75 is a very
    # different diagnosis from one that reaches 120 MB and holds it.
    print(f"RSS settled (baseline) : {baseline:.0f} MB")
    print(f"RSS settled (post-dl)  : {rss_after_download:.0f} MB")
    print(f"RSS settled (post-ingest): {rss_after_ingest:.0f} MB")
    growth = rss_after_ingest - baseline
    print(f"retained above baseline: {growth:+.0f} MB")
    print(f"OVERALL PEAK RSS      : {overall:.0f} MB   (of 256 MB)")
    if overall < 150:
        verdict = "EXCELLENT -- safe to escalate to the next batch size"
    elif overall < 170:
        verdict = "ACCEPTABLE -- usable, but do not escalate further"
    elif overall < 190:
        verdict = "TOO CLOSE for a 256 MB container -- step back a size"
    else:
        verdict = "REJECT this configuration"
    print(f"VERDICT               : {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
