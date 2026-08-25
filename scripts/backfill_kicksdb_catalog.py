"""Backfill KicksDB's StockX sneaker catalog into kicksdb_catalog +
kicksdb_catalog_history.

Unlike the PriceCharting pipeline, this doesn't need a discovery+registry
step: KicksDB's /v3/stockx/products list endpoint supports real pagination
(page/limit, confirmed live), sort=rank (true StockX popularity), and --
confirmed live -- returns full per-size pricing for every item in the page
when display[variants]=true&display[prices]=true is set, not just a
single-product detail call. So one paginated query IS the whole discovery
+backfill step for that slice of the catalog.

Each query is capped at 1,000 results (Meilisearch's default maxTotalHits),
so multiple SEGMENTS (e.g. one per brand) are needed to cover more than a
single top-1,000-by-rank slice. Segments are a small, hand-maintained list
here, not a crawled registry -- KicksDB's catalog doesn't have the "tens of
thousands of enumerable sets" problem PriceCharting's did, so there's
nothing to discover, only a handful of filters to choose.

Safety: KicksDB's Standard API is billed by MONTHLY request volume, shared
with this app's live scan-time lookups (see kicksdb_pricing_provider.py) --
not a per-second rate a script can treat as its own. --max-requests caps
how many HTTP requests a single run can make, so a misconfigured segment
list can't silently burn through the account's monthly quota.
"""

import argparse
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx


DEFAULT_API_BASE = "https://api.kicks.dev"
DEFAULT_PAGE_SIZE = 100
# 10 segments x 10 requests/segment (1,000 results / 100 page size) = 100
# requests per full run, plus headroom for a slow/retried segment.
DEFAULT_MAX_REQUESTS = 120
# Meilisearch's default cap on total results for a single filtered query --
# confirmed live (a broad product_type="sneakers" query reported
# meta.total=1000 regardless of how many pages were requested).
API_RESULT_CAP = 1000

# A handful of live cron runs have seen isolated 500s from kicks.dev mid-
# segment (e.g. New Balance, Reebok) that succeed again on the next page --
# retry transient server errors once before giving up on the segment.
PAGE_FETCH_ATTEMPTS = 2
PAGE_FETCH_RETRY_DELAY_SECONDS = 2.0

# The general "sneakers-by-rank" segment plus the 9 brands confirmed live to
# each independently hit the 1,000-result cap on their own (i.e. each has
# 1,000+ sneakers in KicksDB's catalog) -- this is the real, full segment
# list the initial 9,062-item catalog was built from (live-run once as a
# one-off `--segments-json` override; persisted here so both manual re-runs
# and the daily refresh cron use the same list by default, instead of
# silently shrinking back to just the general segment). This script is
# idempotent either way (write_catalog_rows only touches rows whose
# content_hash actually changed), so running it daily against this same
# list is exactly what keeps the catalog and its price history current.
# Brands big enough that a single brand query still hits the 1,000-result
# cap (each was confirmed live reporting total=1000, i.e. "at least 1,000").
# One query can therefore never enumerate them, so each is additionally
# split by gender: the four gender values are disjoint, so their windows
# sum to well past what the brand-only query alone could reach. Jordan for
# example reports 1,000+ in EACH of men/women/kids on its own.
_CAPPED_BRANDS = [
    "Nike",
    "Jordan",
    "Adidas",
    "New Balance",
    "Puma",
    "Reebok",
    "Asics",
    "Converse",
    "Vans",
    "Salomon",
    "Saucony",
    "Crocs",
    "UGG",
    "On",
    "Under Armour",
    "Balenciaga",
    "Alexander McQueen",
]

# Every gender value present in KicksDB's sneaker data. Splitting on an
# incomplete list would silently drop products, so this is enumerated from
# the live catalog rather than assumed -- "kids" and "unisex" together
# account for ~1,000 rows that a naive men/women split would lose. (Note
# the value is "kids", not "youth"; "youth" returns zero.)
_GENDERS = ["men", "women", "kids", "unisex"]

# Brands whose entire catalog fits comfortably under the 1,000 cap, so one
# query each is enough -- no gender split needed. Sizes confirmed live.
# Several of these were almost entirely absent before (Yeezy had 5 of its
# 122 products stored, Timberland 3 of 745) because they had no segment of
# their own and only surfaced if they happened to rank in the global
# top-1,000.
_SMALL_BRANDS = [
    "Yeezy",
    "Birkenstock",
    "Timberland",
    "Onitsuka Tiger",
    "Merrell",
    "Mizuno",
    "Brooks",
    "Rick Owens",
    "Maison Mihara Yasuhiro",
    "Diadora",
    "Fila",
    "Golden Goose",
    "Veja",
    "Lacoste",
    "Common Projects",
    "Bravest Studios",
]


def _slug(value: str) -> str:
    return value.lower().replace(" ", "-")


def _build_default_segments() -> list[dict[str, str]]:
    segments = [
        # Global top-1,000 by rank: the catch-all. Kept in front of the
        # per-brand segments so a brand that never gets its own entry (a new
        # one, or a gender value not in _GENDERS) still has a route in.
        {"label": "sneakers-by-rank", "filters": 'product_type="sneakers"', "sort": "rank"},
    ]
    for brand in _CAPPED_BRANDS:
        # The unsplit brand query is kept alongside its gender splits. It is
        # partly redundant, but it is the safety net if KicksDB ever adds a
        # gender value we don't enumerate -- without it those products would
        # vanish from the catalog silently.
        segments.append(
            {
                "label": f"{_slug(brand)}-by-rank",
                "filters": f'product_type="sneakers" AND brand="{brand}"',
                "sort": "rank",
            }
        )
        segments.extend(
            {
                "label": f"{_slug(brand)}-{gender}-by-rank",
                "filters": (
                    f'product_type="sneakers" AND brand="{brand}" AND gender="{gender}"'
                ),
                "sort": "rank",
            }
            for gender in _GENDERS
        )
    segments.extend(
        {
            "label": f"{_slug(brand)}-by-rank",
            "filters": f'product_type="sneakers" AND brand="{brand}"',
            "sort": "rank",
        }
        for brand in _SMALL_BRANDS
    )
    return segments


DEFAULT_SEGMENTS = _build_default_segments()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = args.api_key or os.getenv("KICKSDB_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "KICKSDB_API_KEY is required (or --api-key) -- even for "
            "--dry-run, since this worker makes real KicksDB requests and "
            "only skips writing results."
        )

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    client = (
        None
        if args.dry_run
        else KicksDBCatalogClient(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            timeout_seconds=args.timeout_seconds,
        )
    )

    segments = load_segments(args.segments_json)
    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    request_budget = RequestBudget(args.max_requests)

    # Rows (each carrying a full raw_payload + per-size variants) are written
    # to Supabase segment-by-segment instead of being accumulated across all
    # segments and written once at the end -- holding all ~10 segments'
    # worth of rows in memory simultaneously is what was pushing this cron
    # past its memory limit. Writing (and letting each segment's rows fall
    # out of scope) as we go keeps peak memory to one segment at a time.
    products_found = 0
    unique_ids: set[str] = set()
    catalog_rows_written = 0
    segment_summaries: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout_seconds, follow_redirects=True) as http:
        for segment in segments:
            if request_budget.exhausted:
                print(
                    f"  Skipping segment {segment['label']!r}: request budget "
                    f"({args.max_requests}) already exhausted.",
                    flush=True,
                )
                continue
            products, requests_used = fetch_segment(
                http,
                api_base=args.api_base,
                api_key=api_key,
                segment=segment,
                page_size=args.page_size,
                request_budget=request_budget,
            )
            print(
                f"  {segment['label']}: {len(products)} products "
                f"({requests_used} request(s)).",
                flush=True,
            )
            rows = [
                to_catalog_row(product, source_query=segment["filters"], source_downloaded_at=source_downloaded_at)
                for product in products
            ]
            rows = [row for row in rows if row is not None]
            rows = dedupe_catalog_rows(rows)

            products_found += len(products)
            unique_ids.update(row["kicksdb_id"] for row in rows)

            if rows:
                if args.dry_run:
                    catalog_rows_written += len(rows)
                else:
                    assert client is not None
                    if write_catalog_rows(client, rows, batch_size=args.catalog_batch_size):
                        catalog_rows_written += len(rows)

            segment_summaries.append(
                {"label": segment["label"], "products": len(products), "requests": requests_used}
            )

    print(
        json.dumps(
            {
                "success": True,
                "dryRun": args.dry_run,
                "requestsUsed": request_budget.used,
                "segments": segment_summaries,
                "productsFound": products_found,
                "uniqueProducts": len(unique_ids),
                "catalogRowsWritten": catalog_rows_written,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


class RequestBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def consume(self) -> None:
        self.used += 1


def fetch_segment(
    http: httpx.Client,
    *,
    api_base: str,
    api_key: str,
    segment: dict[str, str],
    page_size: int,
    request_budget: RequestBudget,
) -> tuple[list[dict[str, Any]], int]:
    products: list[dict[str, Any]] = []
    requests_used = 0
    max_pages = (API_RESULT_CAP + page_size - 1) // page_size
    for page in range(1, max_pages + 1):
        if request_budget.exhausted:
            print(
                f"    Stopping {segment['label']!r} early: request budget exhausted.",
                flush=True,
            )
            break
        params = {
            "filters": segment["filters"],
            "sort": segment.get("sort", "rank"),
            "limit": str(page_size),
            "page": str(page),
            "display[variants]": "true",
            "display[prices]": "true",
        }
        try:
            payload = None
            last_exc: Exception | None = None
            for attempt in range(PAGE_FETCH_ATTEMPTS):
                if attempt > 0:
                    time.sleep(PAGE_FETCH_RETRY_DELAY_SECONDS)
                try:
                    response = http.get(
                        f"{api_base}/v3/stockx/products",
                        params=params,
                        headers={"Authorization": api_key, "Accept": "application/json"},
                    )
                    request_budget.consume()
                    requests_used += 1
                    response.raise_for_status()
                    payload = response.json()
                    last_exc = None
                    break
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    # Only transient server errors are worth a retry -- a 4xx
                    # (bad filter, auth) will fail identically every time.
                    if exc.response.status_code < 500:
                        break
            if last_exc is not None:
                raise last_exc
        except (httpx.HTTPError, ValueError) as exc:
            print(f"    Page {page} failed for {segment['label']!r}: {exc}", flush=True)
            break

        page_products = payload.get("data") or []
        if not isinstance(page_products, list) or not page_products:
            break
        products.extend(item for item in page_products if isinstance(item, dict))

        total = (payload.get("meta") or {}).get("total")
        if isinstance(total, int) and page * page_size >= total:
            break
    return products, requests_used


def to_catalog_row(
    product: dict[str, Any],
    *,
    source_query: str,
    source_downloaded_at: str,
) -> dict[str, Any] | None:
    kicksdb_id = str(product.get("id") or "").strip()
    title = str(product.get("title") or "").strip()
    if not kicksdb_id or not title:
        return None

    row = {
        "kicksdb_id": kicksdb_id,
        "sku": _clean(product.get("sku")),
        "slug": _clean(product.get("slug")),
        "title": title,
        "brand": _clean(product.get("brand")),
        "model": _clean(product.get("model")),
        "gender": _clean(product.get("gender")),
        "product_type": _clean(product.get("product_type")),
        "category": _clean(product.get("category")),
        "secondary_category": _clean(product.get("secondary_category")),
        "image_url": _clean(product.get("image")),
        "rank": product.get("rank") if isinstance(product.get("rank"), int) else None,
        "weekly_orders": (
            product.get("weekly_orders") if isinstance(product.get("weekly_orders"), int) else None
        ),
        "release_date": _clean(product.get("release_date")),
        "currency": "USD",
        "min_price_cents": _price_to_cents(product.get("min_price")),
        "max_price_cents": _price_to_cents(product.get("max_price")),
        "avg_price_cents": _price_to_cents(product.get("avg_price")),
        "variants": product.get("variants") if isinstance(product.get("variants"), list) else [],
        "product_url": _clean(product.get("link")),
        "raw_payload": product,
        "source_query": source_query,
        "source_downloaded_at": source_downloaded_at,
    }
    row["content_hash"] = catalog_content_hash(row)
    return row


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _price_to_cents(value: Any) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return round(value * 100)
    return None


_HASH_SIGNATURE_COLUMNS = (
    "title",
    "brand",
    "model",
    "gender",
    "category",
    "secondary_category",
    "rank",
    "weekly_orders",
    "min_price_cents",
    "max_price_cents",
    "avg_price_cents",
    "variants",
    "product_url",
)


def catalog_content_hash(row: dict[str, Any]) -> str:
    payload = {column: row.get(column) for column in _HASH_SIGNATURE_COLUMNS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dedupe_catalog_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A shoe can appear in more than one segment (e.g. a top-ranked Nike
    # showing up in both a broad "sneakers" query and a "brand=Nike" query)
    # -- keep the last occurrence per kicksdb_id so the write batch never
    # contains the same id twice.
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        kicksdb_id = str(row.get("kicksdb_id") or "").strip()
        if not kicksdb_id:
            continue
        deduped[kicksdb_id] = row
    return list(deduped.values())


def to_catalog_history_row(catalog_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kicksdb_id": catalog_row["kicksdb_id"],
        "sku": catalog_row.get("sku"),
        "slug": catalog_row.get("slug"),
        "title": catalog_row["title"],
        "brand": catalog_row.get("brand"),
        "model": catalog_row.get("model"),
        "gender": catalog_row.get("gender"),
        "product_type": catalog_row.get("product_type"),
        "category": catalog_row.get("category"),
        "secondary_category": catalog_row.get("secondary_category"),
        "image_url": catalog_row.get("image_url"),
        "rank": catalog_row.get("rank"),
        "weekly_orders": catalog_row.get("weekly_orders"),
        "release_date": catalog_row.get("release_date"),
        "currency": catalog_row.get("currency", "USD"),
        "min_price_cents": catalog_row.get("min_price_cents"),
        "max_price_cents": catalog_row.get("max_price_cents"),
        "avg_price_cents": catalog_row.get("avg_price_cents"),
        "variants": catalog_row.get("variants") or [],
        "product_url": catalog_row.get("product_url"),
        "raw_payload": catalog_row.get("raw_payload") or {},
        "source_query": catalog_row.get("source_query"),
        "source_downloaded_at": catalog_row.get("source_downloaded_at"),
        "valid_from": catalog_row.get("source_downloaded_at") or datetime.now(timezone.utc).isoformat(),
        "valid_to": None,
        "is_current": True,
        "change_hash": catalog_row["content_hash"],
    }


class PartialKicksDBWriteError(Exception):
    """Raised when at least one sub-batch failed but every sub-batch was
    still attempted (see PartialCatalogWriteError in
    import_pricecharting_catalog.py for the same reasoning: a single
    sub-batch failure must not abort every later sub-batch in the call)."""

    def __init__(self, message: str, *, succeeded_count: int, failed_ids: list[str]) -> None:
        super().__init__(message)
        self.succeeded_count = succeeded_count
        self.failed_ids = failed_ids


def write_catalog_rows(
    client: "KicksDBCatalogClient",
    catalog_rows: list[dict[str, Any]],
    *,
    batch_size: int,
) -> bool:
    try:
        client.sync_history_rows(catalog_rows, batch_size=batch_size)
        client.upsert_catalog_rows(catalog_rows, batch_size=batch_size)
    except PartialKicksDBWriteError as exc:
        print(
            f"  KicksDB catalog write partially failed "
            f"({exc.succeeded_count} succeeded, {len(exc.failed_ids)} failed) "
            f"-- will retry next run: {exc}",
            flush=True,
        )
        return False
    except (SystemExit, Exception) as exc:
        print(f"  KicksDB catalog write failed, will retry next run: {exc}", flush=True)
        return False
    return True


class KicksDBCatalogClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")
        role = _supabase_jwt_role(self.service_role_key)
        if role and role != "service_role":
            raise SystemExit(
                "SUPABASE_SERVICE_ROLE_KEY must be the Supabase service_role key "
                f"for catalog writes, but the configured key has role '{role}'."
            )

    def upsert_catalog_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        total = 0
        skipped = 0
        failed_ids: list[str] = []
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                try:
                    current_by_id = self._fetch_current_hashes(client, batch)
                    changed_rows = []
                    for row in batch:
                        current = current_by_id.get(row["kicksdb_id"])
                        if current and current.get("content_hash") == row.get("content_hash"):
                            skipped += 1
                            continue
                        changed_rows.append(row)
                    if changed_rows:
                        response = client.post(
                            f"{self.supabase_url}/rest/v1/kicksdb_catalog",
                            params={"on_conflict": "kicksdb_id"},
                            headers={
                                **self._headers(),
                                "Prefer": "resolution=merge-duplicates,return=minimal",
                            },
                            json=changed_rows,
                        )
                        response.raise_for_status()
                        total += len(changed_rows)
                except (SystemExit, Exception) as exc:
                    print(f"  Catalog upsert sub-batch failed, continuing: {exc}", flush=True)
                    failed_ids.extend(row["kicksdb_id"] for row in batch)
                    continue
                print(
                    f"Skipped {skipped} unchanged catalog rows; upserted {total} changed/new rows...",
                    flush=True,
                )
        if failed_ids:
            raise PartialKicksDBWriteError(
                f"{len(failed_ids)} catalog row(s) failed to upsert; {total} succeeded.",
                succeeded_count=total,
                failed_ids=failed_ids,
            )
        return total

    def sync_history_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        inserted = 0
        failed_ids: list[str] = []
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                try:
                    current_by_id = self._fetch_current_history_hashes(client, batch)
                    rows_to_insert = []
                    changed_ids = []
                    for row in batch:
                        history_row = to_catalog_history_row(row)
                        current = current_by_id.get(row["kicksdb_id"])
                        if current and current.get("change_hash") == history_row["change_hash"]:
                            continue
                        if current:
                            changed_ids.append(row["kicksdb_id"])
                        rows_to_insert.append(history_row)

                    if changed_ids:
                        close_response = client.patch(
                            f"{self.supabase_url}/rest/v1/kicksdb_catalog_history",
                            params={
                                "kicksdb_id": f"in.({','.join(changed_ids)})",
                                "is_current": "eq.true",
                            },
                            headers={**self._headers(), "Prefer": "return=minimal"},
                            json={
                                "valid_to": rows_to_insert[0]["source_downloaded_at"],
                                "is_current": False,
                            },
                        )
                        close_response.raise_for_status()
                    if rows_to_insert:
                        insert_response = client.post(
                            f"{self.supabase_url}/rest/v1/kicksdb_catalog_history",
                            headers={**self._headers(), "Prefer": "return=minimal"},
                            json=rows_to_insert,
                        )
                        insert_response.raise_for_status()
                        inserted += len(rows_to_insert)
                except (SystemExit, Exception) as exc:
                    print(f"  Catalog history sub-batch failed, continuing: {exc}", flush=True)
                    failed_ids.extend(row["kicksdb_id"] for row in batch)
                    continue
                print(f"Recorded {inserted} / {len(rows)} history versions...", flush=True)
        if failed_ids:
            raise PartialKicksDBWriteError(
                f"{len(failed_ids)} history row(s) failed to sync; {inserted} succeeded.",
                succeeded_count=inserted,
                failed_ids=failed_ids,
            )
        return inserted

    def _fetch_current_hashes(self, client: httpx.Client, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        ids = [row["kicksdb_id"] for row in rows]
        response = client.get(
            f"{self.supabase_url}/rest/v1/kicksdb_catalog",
            params={"select": "kicksdb_id,content_hash", "kicksdb_id": f"in.({','.join(ids)})"},
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        return {row["kicksdb_id"]: row for row in payload if isinstance(row, dict)}

    def _fetch_current_history_hashes(
        self, client: httpx.Client, rows: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        ids = [row["kicksdb_id"] for row in rows]
        response = client.get(
            f"{self.supabase_url}/rest/v1/kicksdb_catalog_history",
            params={
                "select": "kicksdb_id,change_hash",
                "is_current": "eq.true",
                "kicksdb_id": f"in.({','.join(ids)})",
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        return {row["kicksdb_id"]: row for row in payload if isinstance(row, dict)}

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


def _supabase_jwt_role(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    role = data.get("role")
    return role if isinstance(role, str) else None


def load_segments(segments_json: str | None) -> list[dict[str, str]]:
    if not segments_json:
        return DEFAULT_SEGMENTS
    parsed = json.loads(segments_json)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise SystemExit("--segments-json must be a JSON array of {label, filters} objects.")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill KicksDB's StockX sneaker catalog into kicksdb_catalog."
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help="Hard cap on HTTP requests this run can make, across all segments.",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--catalog-batch-size", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default="", help="Defaults to KICKSDB_API_KEY.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument(
        "--segments-json",
        default="",
        help="JSON array of {label, filters, sort} objects; defaults to a single top-1000-by-rank sneakers segment.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
