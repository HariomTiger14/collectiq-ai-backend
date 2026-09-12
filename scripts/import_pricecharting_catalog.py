import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
from functools import lru_cache
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

from scripts._ops_run_recorder import record_db_failure
from scripts._shared_rate_limiter import (
    CLASS_ESSENTIAL_CATALOG,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)


PRICE_FIELDS = {
    "loose_price_cents": ["loose-price", "loosePrice", "loose price"],
    "cib_price_cents": ["cib-price", "complete-price", "cibPrice", "complete price"],
    "new_price_cents": ["new-price", "newPrice", "new price"],
    "graded_price_cents": ["graded-price", "gradedPrice", "graded price"],
    "box_only_price_cents": ["box-only-price", "box only price"],
    "manual_only_price_cents": ["manual-only-price", "manual only price"],
}

TEXT_FIELDS = {
    "pricecharting_id": ["id", "product-id", "productId"],
    "product_name": ["product-name", "productName", "product name", "title"],
    "console_name": ["console-name", "consoleName", "console name", "platform"],
    "category": ["category", "genre"],
    "upc": ["upc", "UPC"],
    "asin": ["asin", "ASIN"],
    "epid": ["epid", "ePID", "EPID"],
    "release_date": ["release-date", "releaseDate", "release date"],
    "product_url": ["url", "product-url", "productUrl"],
}

PRICECHARTING_CSV_ENV_VARS = {
    "video_games": "PRICECHARTING_CSV_VIDEO_GAMES_URL",
    "pokemon": "PRICECHARTING_CSV_POKEMON_URL",
    "magic": "PRICECHARTING_CSV_MAGIC_URL",
    "yugioh": "PRICECHARTING_CSV_YUGIOH_URL",
    "one_piece": "PRICECHARTING_CSV_ONE_PIECE_URL",
}

CATALOG_COLUMNS = (
    "pricecharting_id",
    "product_name",
    "console_name",
    "category",
    "platform_group",
    "upc",
    "asin",
    "epid",
    "release_date",
    "loose_price_cents",
    "cib_price_cents",
    "new_price_cents",
    "graded_price_cents",
    "box_only_price_cents",
    "manual_only_price_cents",
    "currency",
    "product_url",
    "normalized_identity",
    "raw_payload",
    "source_file",
    "source_downloaded_at",
    "content_hash",
)

# Mirrors public.compute_platform_group() in
# 20260820_add_platform_group_step1_schema.sql exactly -- keep both in sync
# if this mapping changes. console_name is reused across every category
# (sports cards, comics, funko, etc all store their set name here too, not
# just video games), so this only matches real platform names; every other
# row gets None, which is correct -- they're not filterable by platform
# because they aren't one.
#
# Word-boundary matching (\b) rather than plain substring matching, so
# short platform codes (ds, wii, pc) are safe to include here -- a bare
# substring match previously had a real collision bug ("nes" matched
# inside "Finest", pulling sports cards into a video-games filter); \b
# only matches whole tokens, so that can't happen.
PLATFORM_GROUP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("playstation", re.compile(r"\b(playstation|ps1|ps2|ps3|ps4|ps5|psp|vita)\b", re.IGNORECASE)),
    ("xbox", re.compile(r"\bxbox\b", re.IGNORECASE)),
    ("nintendo", re.compile(
        r"\b(nintendo|gamecube|wii|switch|gameboy|nes|snes|n64|3ds|ds)\b", re.IGNORECASE
    )),
    ("sega", re.compile(r"\b(sega|genesis|saturn|dreamcast|32x)\b", re.IGNORECASE)),
    ("atari", re.compile(r"\b(atari|jaguar|lynx|2600|5200|7800)\b", re.IGNORECASE)),
    ("pc", re.compile(r"\b(pc|windows|commodore|amiga|msx|trs-80|apple)\b", re.IGNORECASE)),
    ("retro-other", re.compile(
        r"\b(3do|neo\s*geo|colecovision|intellivision|vectrex|turbo\s*grafx)\b", re.IGNORECASE
    )),
]


def compute_platform_group(console_name: str | None) -> str | None:
    if not console_name:
        return None
    for group, pattern in PLATFORM_GROUP_PATTERNS:
        if pattern.search(console_name):
            return group
    return None

CATALOG_HISTORY_COLUMNS = (
    "pricecharting_id",
    "product_name",
    "console_name",
    "category",
    "upc",
    "asin",
    "epid",
    "release_date",
    "loose_price_cents",
    "cib_price_cents",
    "new_price_cents",
    "graded_price_cents",
    "box_only_price_cents",
    "manual_only_price_cents",
    "currency",
    "product_url",
    "normalized_identity",
    "raw_payload",
    "source_file",
    "source_downloaded_at",
    "valid_from",
    "valid_to",
    "is_current",
    "change_hash",
)

CATALOG_HISTORY_SIGNATURE_COLUMNS = (
    "product_name",
    "console_name",
    "category",
    "upc",
    "asin",
    "epid",
    "release_date",
    "loose_price_cents",
    "cib_price_cents",
    "new_price_cents",
    "graded_price_cents",
    "box_only_price_cents",
    "manual_only_price_cents",
    "currency",
    "product_url",
    "normalized_identity",
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = load_sources(args)
    imported_rows: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    for source in sources:
        rows = source.rows
        print(f"Preparing {source.name}: {len(rows)} input rows...", flush=True)
        source_rows = [
            to_catalog_row(row, source.name, args.source_downloaded_at)
            for row in rows
        ]
        source_rows = [row for row in source_rows if row is not None]
        imported_rows.extend(source_rows)
        source_summaries.append(
            {
                "source": source.name,
                "inputRows": len(rows),
                "validRows": len(source_rows),
            }
        )

    imported_rows = dedupe_catalog_rows(imported_rows)
    print(f"Prepared {len(imported_rows)} unique catalog rows.", flush=True)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "sources": source_summaries,
                    "inputRows": sum(source["inputRows"] for source in source_summaries),
                    "validRows": len(imported_rows),
                    "firstRow": imported_rows[0] if imported_rows else None,
                },
                indent=2,
                default=str,
            )
        )
        return 0

    client = SupabaseCatalogClient(
        supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
        service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        timeout_seconds=args.timeout_seconds,
    )
    print(f"Starting Supabase import with batch size {args.batch_size}...", flush=True)
    history_total = client.sync_scd2_history_rows(
        imported_rows,
        batch_size=args.batch_size,
    )
    total = client.upsert_rows(imported_rows, batch_size=args.batch_size)
    print(f"Imported {total} PriceCharting catalog rows into Supabase.")
    print(f"Recorded {history_total} PriceCharting catalog history versions into Supabase.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a PriceCharting Legendary CSV download into PackLox pricing catalog."
    )
    parser.add_argument(
        "csv",
        type=Path,
        nargs="?",
        help="Path to a downloaded PriceCharting CSV file.",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="Download and import all configured PRICECHARTING_CSV_*_URL env vars.",
    )
    parser.add_argument(
        "--source",
        choices=sorted(PRICECHARTING_CSV_ENV_VARS),
        help="When used with --from-env, import only one configured source.",
    )
    parser.add_argument("--supabase-url", default="", help="Supabase project URL. Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key",
        default="",
        help="Supabase service role key. Defaults to SUPABASE_SERVICE_ROLE_KEY.",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument(
        "--source-downloaded-at",
        default=datetime.now(timezone.utc).isoformat(),
        help="ISO timestamp for when the CSV was downloaded.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write to Supabase.")
    return parser.parse_args(argv)


class CatalogSource:
    def __init__(self, *, name: str, rows: list[dict[str, str]]) -> None:
        self.name = name
        self.rows = rows


def load_sources(args: argparse.Namespace) -> list[CatalogSource]:
    sources: list[CatalogSource] = []
    if args.csv is not None:
        sources.append(CatalogSource(name=args.csv.name, rows=load_rows(args.csv)))
    if args.from_env:
        sources.extend(
            download_env_sources(
                timeout_seconds=args.timeout_seconds,
                source_filter=args.source,
            )
        )
    if not sources:
        raise SystemExit("Provide a CSV path or use --from-env.")
    return sources


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def download_env_sources(
    *,
    timeout_seconds: float,
    source_filter: str | None = None,
    csv_limiter: "SharedRateLimiter | None" = None,
) -> list[CatalogSource]:
    # Imported lazily: backfill_pricecharting_sets imports this module at
    # module level, so a top-level import here would be circular.
    from scripts.backfill_pricecharting_sets import (
        CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
        REQUEST_HEADERS,
    )

    sources: list[CatalogSource] = []
    if source_filter is not None and source_filter not in PRICECHARTING_CSV_ENV_VARS:
        allowed_sources = ", ".join(sorted(PRICECHARTING_CSV_ENV_VARS))
        raise SystemExit(f"Unsupported source. Use one of: {allowed_sources}.")

    # These are download-custom URLs like every other CSV call, so they are
    # bound by the same published limit of one CSV call per 10 minutes, and
    # they share it with the crons. Downloading all five categories back to
    # back -- which is what the documented --from-env command used to do --
    # breaches that limit five times over in a few seconds.
    # Injectable so a test can supply one that does not really sleep. When
    # the limiter cannot reach the database it falls back to LOCAL pacing and
    # sleeps the full interval for real -- correct in production, but it
    # turned this function's test into a 600-second one.
    if csv_limiter is None:
        csv_limiter = SharedRateLimiter(
            PRICECHARTING_CSV,
            slot_class=CLASS_ESSENTIAL_CATALOG,
            fallback_interval_seconds=CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
        )

    with httpx.Client(
        timeout=timeout_seconds, follow_redirects=True, headers=REQUEST_HEADERS
    ) as client:
        for category, env_name in PRICECHARTING_CSV_ENV_VARS.items():
            if source_filter is not None and category != source_filter:
                continue
            url = os.getenv(env_name, "").strip()
            if not url:
                continue
            csv_limiter.acquire()
            print(f"Downloading {category} CSV...", flush=True)
            response = client.get(url, headers={"Accept": "text/csv,*/*"})
            response.raise_for_status()
            rows = load_rows_from_text(response.text)
            print(f"Downloaded {category}.csv with {len(rows)} rows.", flush=True)
            sources.append(CatalogSource(name=f"{category}.csv", rows=rows))
    if not sources:
        if source_filter:
            env_name = PRICECHARTING_CSV_ENV_VARS[source_filter]
            raise SystemExit(f"{env_name} is not configured.")
        raise SystemExit("No PRICECHARTING_CSV_*_URL environment variables were configured.")
    return sources


def load_rows_from_text(csv_text: str) -> list[dict[str, str]]:
    """Whole-file parse. Fine for the small per-category CSVs; for a sports
    batch use iter_rows_from_text() instead -- see the note there."""
    handle = io.StringIO(csv_text)
    return [dict(row) for row in csv.DictReader(handle)]


def iter_rows_from_text(csv_text: str) -> "Iterator[dict[str, str]]":
    """Row-at-a-time parse, so a caller can convert and write in chunks
    without ever holding the whole file as dicts.

    Sports sets average ~636 rows, so a 100-set download-custom batch parses
    to ~63,600 dicts. Materialising those at once is what forced the tier-3
    rotation down to tiny batch sizes: a 20-set batch (~12,700 rows) already
    OOM-killed a 512Mi Render instance. Peak memory here is one chunk rather
    than one batch, which is what makes batch 100 affordable on the small
    plan -- the fetch limit (one CSV call per 10 minutes) then becomes the
    only constraint, which is the one we cannot engineer around."""
    for row in csv.DictReader(io.StringIO(csv_text)):
        yield dict(row)


def iter_rows_from_file(
    path: "Path", *, encoding: str = "utf-8"
) -> "Iterator[dict[str, str]]":
    """Row-at-a-time parse straight off disk.

    iter_rows_from_text() still holds the whole CSV as a str, and copies it
    again into a StringIO. For a 300-set download-custom batch that is ~28 MB
    twice over, on top of the bytes httpx already buffered -- measured at a
    229 MB peak against a 256 MB container. Reading from the file keeps the
    body out of the heap entirely, so peak memory tracks the ingest chunk
    size rather than the size of the download."""
    with open(path, "r", encoding=encoding, errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            yield dict(row)


def chunked_iter(rows: "Iterable[Any]", size: int) -> "Iterator[list[Any]]":
    """Group an iterator into lists of at most `size`, without buffering the
    whole input."""
    if size <= 0:
        raise ValueError("size must be greater than zero")
    chunk: list[Any] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def dedupe_catalog_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        product_id = str(row.get("pricecharting_id", "")).strip()
        if not product_id:
            continue
        deduped[product_id] = row
    return list(deduped.values())


def to_catalog_row(
    row: dict[str, Any],
    source_file: str,
    source_downloaded_at: str,
) -> dict[str, Any] | None:
    normalized_row = normalize_row_keys(row)
    product_id = pick_normalized(normalized_row, TEXT_FIELDS["pricecharting_id"])
    product_name = pick_normalized(normalized_row, TEXT_FIELDS["product_name"])
    if not product_id or not product_name:
        return None

    console_name = pick_normalized(normalized_row, TEXT_FIELDS["console_name"])
    catalog_row: dict[str, Any] = {
        "pricecharting_id": product_id,
        "product_name": product_name,
        "console_name": console_name,
        "category": pick_normalized(normalized_row, TEXT_FIELDS["category"]) or console_name,
        "platform_group": compute_platform_group(console_name),
        "upc": pick_normalized(normalized_row, TEXT_FIELDS["upc"]),
        "asin": pick_normalized(normalized_row, TEXT_FIELDS["asin"]),
        "epid": pick_normalized(normalized_row, TEXT_FIELDS["epid"]),
        "release_date": parse_date(pick_normalized(normalized_row, TEXT_FIELDS["release_date"])),
        "currency": "USD",
        "product_url": pick_normalized(normalized_row, TEXT_FIELDS["product_url"]),
        "normalized_identity": normalized_identity(product_name, console_name),
        "raw_payload": row,
        "source_file": source_file,
        "source_downloaded_at": source_downloaded_at,
    }
    for target, aliases in PRICE_FIELDS.items():
        catalog_row[target] = parse_price_cents(pick_normalized(normalized_row, aliases))
    normalized_row = normalize_catalog_row(catalog_row)
    normalized_row["content_hash"] = catalog_history_change_hash(normalized_row)
    return normalized_row


def to_catalog_row_from_api_product(
    product: dict[str, Any],
    source_file: str,
    source_downloaded_at: str,
) -> dict[str, Any] | None:
    """to_catalog_row() for /api/product(s) payloads -- canonicalizes
    category BEFORE the SCD2 hash is computed.

    The API attaches a short `genre` ("Baseball Card") that the category
    alias picks up, while the CSV/set paths have no category column at all
    and fall back to console_name -- the long form ("Baseball Cards 2020
    Topps Chrome ..."), which ~95% of stored rows already hold and which
    the 2026-08-29 SCD2 audit fixed as canonical. Feeding the API's short
    form into the shared hash made every card that alternated between an
    API path (tier-1/tier-2/api-search) and a CSV path (backfill/tier-3/
    completed-categories) mint a fake "metadata changed" SCD2 version on
    each crossing -- ~17% of all history versions were this flap.

    Stripping the API's category aliases here makes every ingestion path
    resolve category through the same console_name fallback inside
    to_catalog_row(), which also computes the hash -- so canonicalization
    is strictly hash-first by construction. raw_payload keeps the original
    untouched API product (restored after conversion; the hash signature
    never includes raw_payload, so this cannot affect change detection).

    Only strips when the product actually carries a console name to fall
    back to -- a payload without one keeps its genre rather than ending up
    with no category at all.
    """
    has_console_name = any(
        str(product.get(alias) or "").strip()
        for alias in TEXT_FIELDS["console_name"]
    )
    if not has_console_name:
        return to_catalog_row(product, source_file, source_downloaded_at)
    stripped = {
        key: value
        for key, value in product.items()
        if key not in TEXT_FIELDS["category"]
    }
    row = to_catalog_row(stripped, source_file, source_downloaded_at)
    if row is not None:
        row["raw_payload"] = product
    return row


def normalize_catalog_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        column: row.get(column) if row.get(column) != "" else None
        for column in CATALOG_COLUMNS
    }


def to_catalog_history_row(catalog_row: dict[str, Any]) -> dict[str, Any]:
    valid_from = source_timestamp(catalog_row.get("source_downloaded_at"))
    row = {
        **catalog_row,
        "valid_from": valid_from,
        "valid_to": None,
        "is_current": True,
        "change_hash": catalog_history_change_hash(catalog_row),
    }
    return {
        column: row.get(column) if row.get(column) != "" else None
        for column in CATALOG_HISTORY_COLUMNS
    }


PRICE_OBSERVATION_COLUMNS = (
    "loose_price_cents",
    "cib_price_cents",
    "new_price_cents",
    "graded_price_cents",
    "box_only_price_cents",
    "manual_only_price_cents",
)


def to_price_observation_row(catalog_row: dict[str, Any]) -> dict[str, Any]:
    """Compact pricecharting_price_history row (Step-2 shadow write).

    Carries exactly the reader contract (_fetch_history_rows): the six
    price columns, currency, source, and the observation timestamp --
    observed_at is the provider-download timestamp, which also forms the
    (pricecharting_id, observed_at) idempotency key, so a retried
    ingestion of the same input conflicts instead of duplicating."""
    return {
        "pricecharting_id": catalog_row.get("pricecharting_id"),
        "observed_at": source_timestamp(catalog_row.get("source_downloaded_at")),
        **{column: catalog_row.get(column) for column in PRICE_OBSERVATION_COLUMNS},
        "currency": catalog_row.get("currency") or "USD",
        "source_file": catalog_row.get("source_file"),
    }


def prices_differ(catalog_row: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether any of the six price columns (or currency) differs between
    the incoming row and the stored current version. Metadata, category
    canonicalization, raw_payload, timestamps, and ingestion path play no
    part -- a version whose prices are unchanged must NOT produce a price
    observation."""
    if (catalog_row.get("currency") or "USD") != (current.get("currency") or "USD"):
        return True
    return any(
        catalog_row.get(column) != current.get(column)
        for column in PRICE_OBSERVATION_COLUMNS
    )


CATALOG_METADATA_SIGNATURE_COLUMNS = tuple(
    column
    for column in CATALOG_HISTORY_SIGNATURE_COLUMNS
    if column not in PRICE_OBSERVATION_COLUMNS and column != "currency"
)


def catalog_metadata_hash(row: dict[str, Any]) -> str:
    """Hash of the non-price signature columns only.

    Used as the gate for writing pricecharting_catalog. content_hash cannot
    serve: it covers prices, so once the catalog stops carrying daily cents it
    would differ on every price move and rewrite the 25 GB search document --
    the exact write this PR removes.

    Cached per id rather than the columns themselves: a full CSV refresh runs
    millions of rows through one process, and holding ~9 text columns for each
    measured in the hundreds of MB where a hash is ~150 bytes.
    """
    payload = {column: row.get(column) for column in CATALOG_METADATA_SIGNATURE_COLUMNS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


BROWSE_KEY_COLUMNS = ("category", "platform_group")


def browse_keys_differ(catalog_row: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether the Discover browse keys on the current-price row are stale.

    pricecharting_current_price carries category and platform_group so the
    browse indexes can live there. A metadata-only rename that leaves prices
    untouched still has to reach that copy, or browse serves the old key until
    the next price move -- which for a stable item may be never.
    """
    return any(
        (catalog_row.get(column) or None) != (current.get(column) or None)
        for column in BROWSE_KEY_COLUMNS
    )


def to_current_price_row(catalog_row: dict[str, Any]) -> dict[str, Any]:
    """The pricecharting_current_price shape for one parsed CSV/API row."""
    return {
        "pricecharting_id": catalog_row.get("pricecharting_id"),
        **{column: catalog_row.get(column) for column in PRICE_OBSERVATION_COLUMNS},
        "currency": catalog_row.get("currency") or "USD",
        "observed_at": source_timestamp(catalog_row.get("source_downloaded_at")),
        "source_file": catalog_row.get("source_file"),
        "category": catalog_row.get("category"),
        "platform_group": catalog_row.get("platform_group"),
    }


def metadata_differs(catalog_row: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether anything OTHER than price differs from the stored SCD2 version.

    This is what now decides whether a full 1,427-byte SCD2 version is written.
    Prices and currency are excluded deliberately: a price move is recorded as
    a 188-byte snapshot in pricecharting_price_history instead, which is the
    table the chart reads (#212).
    """
    return any(
        catalog_row.get(column) != current.get(column)
        for column in CATALOG_METADATA_SIGNATURE_COLUMNS
    )


def source_timestamp(source_downloaded_at: Any) -> str:
    if isinstance(source_downloaded_at, datetime):
        return source_downloaded_at.astimezone(timezone.utc).isoformat()
    value = str(source_downloaded_at or "").strip()
    if not value:
        return datetime.now(timezone.utc).isoformat()
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def catalog_history_change_hash(catalog_row: dict[str, Any]) -> str:
    payload = {
        column: catalog_row.get(column)
        for column in CATALOG_HISTORY_SIGNATURE_COLUMNS
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_row_keys(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw CSV row's keys ONCE.

    pick_text used to rebuild this dict on every field lookup, so a 15-column
    row with 11 lookups ran normalize_key 246 times. Profiled over the real
    292,687-row batch that was 4.92M regex substitutions and 72% of
    to_catalog_row's runtime -- 257 seconds of a 20-minute ingest.
    """
    return {normalize_key(key): value for key, value in row.items()}


def pick_normalized(normalized: dict[str, Any], aliases: list[str]) -> str:
    """Alias lookup against an already-normalised row."""
    for alias in aliases:
        value = normalized.get(normalize_key(alias))
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def pick_text(row: dict[str, Any], aliases: list[str]) -> str:
    """Convenience wrapper for callers holding a raw row.

    Kept so the handful of one-off callers (diagnostics, the API-search path)
    are unchanged. Hot loops should normalise once and use pick_normalized.
    """
    return pick_normalized(normalize_row_keys(row), aliases)


# The same ~15 CSV headers and ~30 aliases recur for every row in a 292k-row
# file, so this turns millions of regex substitutions into dict lookups. The
# cache is bounded because a malformed file could otherwise present unbounded
# distinct keys.
@lru_cache(maxsize=4096)
def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def normalized_identity(product_name: str, console_name: str = "") -> str:
    return " ".join(f"{product_name} {console_name}".lower().split())


def parse_date(value: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# Generous upper bound for a single collectible's price ($1,000,000). Real
# source rows never approach this; it exists to catch malformed CSV values
# (e.g. a UPC or id sitting in a price column) before they reach the
# database, where they would overflow the `integer` price_cents columns
# and fail the whole write batch.
MAX_PLAUSIBLE_PRICE_CENTS = 100_000_000


def parse_price_cents(value: str) -> int | None:
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    if not cleaned or cleaned in {"-", "N/A", "n/a"}:
        return None
    if cleaned.startswith("$") or "." in cleaned:
        cleaned = cleaned.replace("$", "")
        try:
            cents = max(0, round(float(cleaned) * 100))
        except ValueError:
            return None
    else:
        try:
            cents = int(float(cleaned))
        except ValueError:
            return None
    if cents <= 0 or cents > MAX_PLAUSIBLE_PRICE_CENTS:
        return None
    return cents


# Why this hardens the REST writer rather than moving everything to COPY.
#
# scripts/tier3_copy_writer.py already exists and remains the preferred
# tier-3 path: it writes through a direct DATABASE_URL connection, so it
# never meets the PostgREST role's 8s statement_timeout at all, and one
# COPY plus a server-side merge replaces ~375 independent statements.
#
# Expanding it to the other scheduled jobs is a genuine migration, not a
# switch: a different connection (psycopg + DATABASE_URL, which Render's
# cron path does not currently carry), a different write mechanism, and
# different failure semantics -- one transaction that lands or does not,
# rather than sub-batches that partially succeed. Every caller's error
# handling is written against the latter.
#
# The bleeding is live and measured (300 rows lost in one run), so this
# stops it where the rows are actually being lost, safely and reversibly.
# Whether a broader COPY migration is worth it is a decision better made
# from the statement-timeout counters this change starts recording than
# from the guess we would be making today.

# Statement-timeout recovery. Three attempts because the timeouts are
# transient contention against a fixed 8s cap, not a capacity wall: the same
# rows usually write on the next try. Bounded tightly on purpose -- retrying
# a contended write many times adds to the contention it is waiting on.
MAX_TIMEOUT_ATTEMPTS = 3
TIMEOUT_RETRY_BACKOFF_SECONDS = 1.5


class _StatementTimeoutExhausted(Exception):
    """A sub-batch still timed out after every retry.

    Raised by the inner frames and caught by the outermost one, so the
    ledger records ONE event carrying the row count the caller actually
    asked to write. Recording at the leaf instead would report the size
    of the last halved fragment -- a 39-row batch would appear in
    ops_error_events as a 9-row failure, quietly corrupting the very
    metric the 57014 diagnosis was built on (batch size vs duration).
    """

    def __init__(self, rows_abandoned: int) -> None:
        super().__init__(f"{rows_abandoned} rows abandoned after timeout retries")
        self.rows_abandoned = rows_abandoned


def timeout_retry_summary(writer: Any) -> dict[str, Any]:
    """The statement-timeout counters, shaped for a run summary.

    Every scheduled catalog writer reports these so the retry can be judged
    from the ops ledger rather than inferred. Without them a run that
    recovered 300 rows and one that never hit a timeout look identical, and
    the next decision -- whether raising the 8s cap or dropping the
    HOT-blocking indexes is actually needed -- depends on telling those
    apart.

    `writePath` is included because zeros mean different things. The tier-3
    COPY writer talks to Postgres directly over DATABASE_URL and never meets
    the PostgREST role's 8s statement_timeout, so its zeros mean "not
    applicable", not "no timeouts occurred". Reporting bare zeros for it
    would read as a clean REST run and quietly overstate how well the retry
    is doing.
    """
    if writer is None:
        # No writer at all -- a dry run. Distinct from "copy": nothing was
        # written, so the zeros describe an absence of writes rather than a
        # write path that cannot time out.
        return {
            "writePath": "none",
            "statementTimeouts": 0,
            "statementTimeoutRetries": 0,
            "statementTimeoutRowsRecovered": 0,
            "statementTimeoutRowsAbandoned": 0,
        }
    stats = getattr(writer, "timeout_retry_stats", None)
    if stats is None:
        return {
            "writePath": "copy",
            "statementTimeouts": 0,
            "statementTimeoutRetries": 0,
            "statementTimeoutRowsRecovered": 0,
            "statementTimeoutRowsAbandoned": 0,
        }
    return {
        "writePath": "rest",
        "statementTimeouts": stats["timeouts"],
        "statementTimeoutRetries": stats["retries"],
        "statementTimeoutRowsRecovered": stats["rowsRecovered"],
        "statementTimeoutRowsAbandoned": stats["rowsAbandoned"],
    }


# PostgREST caps a response at 1,000 rows and says nothing about it -- no
# error, no header, no partial-content status. A lookup that asks about more
# ids than this comes back TRUNCATED and looks like a complete answer.
#
# Measured live 2026-09-08: at a 2,000-row write batch,
# _fetch_current_history_rows asked about 2,000 ids and received 1,000. The
# missing 1,000 looked like they had no current history row, so the writer
# inserted a second "current" row beside the existing one and hit 23505 on
# pricecharting_catalog_history_current_unique_idx. Nothing about the
# response suggested anything was wrong.
#
# Asking for more than this is therefore a programming error, not a runtime
# condition to handle: the batch size is ours to choose.
POSTGREST_MAX_LOOKUP_ROWS = 1000


def _assert_lookup_fits(ids: list[str], *, what: str) -> None:
    if len(ids) > POSTGREST_MAX_LOOKUP_ROWS:
        raise SystemExit(
            f"{what} lookup asked about {len(ids)} ids, over PostgREST's "
            f"{POSTGREST_MAX_LOOKUP_ROWS}-row response cap. The reply would be "
            "silently truncated and the missing ids would be treated as absent. "
            "Lower the write batch size, or chunk the lookup."
        )


def _assert_lookup_complete(ids: list[str], payload: list, *, what: str) -> None:
    """Defence in depth: a full-cap response is indistinguishable from a
    truncated one, so refuse to act on the ambiguous case."""
    if len(payload) >= POSTGREST_MAX_LOOKUP_ROWS and len(ids) >= POSTGREST_MAX_LOOKUP_ROWS:
        raise SystemExit(
            f"{what} lookup returned exactly the {POSTGREST_MAX_LOOKUP_ROWS}-row cap "
            f"for {len(ids)} requested ids -- cannot tell a complete answer from a "
            "truncated one, so refusing to write."
        )


class PartialCatalogWriteError(Exception):
    """Raised by upsert_rows()/sync_scd2_history_rows() when at least one
    sub-batch failed but every sub-batch was still attempted (unlike a bare
    exception escaping mid-loop, which would abort every later sub-batch
    too -- live-confirmed: a single 50-row Postgres statement timeout
    aborted a run with thousands of still-unwritten rows that would
    otherwise have succeeded). Callers that only care about full success
    can let this propagate like any other exception; callers that want to
    know how much actually landed can inspect succeeded_count/failed_ids."""

    def __init__(self, message: str, *, succeeded_count: int, failed_ids: list[str]) -> None:
        super().__init__(message)
        self.succeeded_count = succeeded_count
        self.failed_ids = failed_ids


class SupabaseCatalogClient:
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
                f"for catalog imports, but the configured key has role '{role}'."
            )
        # Accumulates across every upsert_rows()/sync_scd2_history_rows()
        # call made through this instance (one instance is reused for a
        # whole backfill run) -- lets the caller see WHERE catalog-write
        # wall clock actually goes (e.g. investigating the 2026-08-10
        # ~30 rows/sec slowdown) without instrumenting every call site
        # individually. A sub-batch that raises mid-call loses whatever
        # partial time it spent on the call that failed (the timer only
        # records on success) -- acceptable since PartialCatalogWriteError
        # is already a rare, logged path, not the common case this is for.
        self.phase_seconds: dict[str, float] = {
            "unchanged_detection": 0.0,
            "catalog_upsert": 0.0,
            "scd2_comparison": 0.0,
            "scd2_history_lookup": 0.0,
            "catalog_metadata_lookup": 0.0,
            "current_price_lookup": 0.0,
            # Split apart 2026-09-09. These three ran under one timer called
            # "scd2_insert", which was fine while they were one decision.
            # They are not: the snapshot insert is the write that stays, the
            # close+insert pair is the write that mostly went away in #213,
            # and a single number cannot show that.
            "current_price_upsert": 0.0,
            "price_snapshot_insert": 0.0,
            "scd2_close": 0.0,
            "scd2_insert": 0.0,
        }
        # sync_scd2_history_rows and upsert_rows each fetched the SAME current
        # catalog rows for the SAME batch -- two round trips over the wire for
        # one answer. Measured 2026-09-09 on a 350-set batch: the duplicate
        # cost ~81s of a 595s ingest, 14%, for nothing. The second lookup was
        # added in #213 for the price baseline and the two calls live 140
        # lines apart in different methods, which is why it was not obvious.
        #
        # Only the content_hash is kept, not the row: 169k ids at ~150 bytes
        # is ~25 MB, where caching whole rows would be hundreds. The covered
        # set is separate from the hashes because "looked up and absent" and
        # "never looked up" mean different things -- the first is a new item,
        # the second means the cache cannot answer and a fetch must happen.
        self.catalog_lookup_stats: dict[str, int] = {"fetched": 0, "reused": 0}
        self.current_price_stats: dict[str, int] = {
            "upserted": 0, "priceChanged": 0, "browseKeysOnly": 0}
        self._catalog_hash_cache: dict[str, Any] = {}
        self._catalog_lookup_covered: set[str] = set()
        self.price_history_stats: dict[str, int] = {
            "attempted": 0,
            "inserted": 0,
            "duplicateSkipped": 0,
            "failed": 0,
        }
        # Same accumulator pattern as price_history_stats, for the catalog
        # write path. upsert_rows() already computes these three numbers to
        # decide what to send, but only returned the write count -- so the
        # cheap/expensive split (unchanged rows cost a hash READ, changed
        # rows cost a WRITE) was visible in stdout and nowhere else. The
        # tier-3 rotation summary consequently reported rowsWritten as a
        # placeholder equal to rowsParsed, which made the run ledger look
        # like every row was rewritten every cycle. Accumulating here lets
        # any caller record the real split without changing upsert_rows()'
        # int return type, which ~15 import scripts depend on.
        # Statement-timeout recovery, reported per run so the cost and
        # the benefit of retrying are both visible rather than inferred.
        self.timeout_retry_stats: dict[str, int] = {
            "timeouts": 0,
            "retries": 0,
            "rowsRecovered": 0,
            "rowsAbandoned": 0,
        }
        self.catalog_write_stats: dict[str, int] = {
            "written": 0,
            "skippedUnchanged": 0,
            "failed": 0,
        }

    def upsert_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        # Checked HERE rather than at the lookup: the per-sub-batch handler
        # catches everything and reports "continuing", which would downgrade
        # a programming error into silently failed rows.
        if batch_size >= POSTGREST_MAX_LOOKUP_ROWS:
            raise ValueError(
                f"batch_size {batch_size} reaches PostgREST's "
                f"{POSTGREST_MAX_LOOKUP_ROWS}-row lookup cap; a full-cap reply "
                "cannot be told apart from a truncated one, so the batch must "
                "be strictly smaller (see the 23505 on "
                "pricecharting_catalog_history_current_unique_idx, 2026-09-08)."
            )
        total = 0
        skipped = 0
        failed_ids: list[str] = []
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                try:
                    detection_started_at = time.perf_counter()
                    current_by_id = self._current_catalog_hashes(client, batch)
                    self.phase_seconds["unchanged_detection"] += (
                        time.perf_counter() - detection_started_at
                    )
                    changed_rows = []
                    for row in batch:
                        product_id = str(row.get("pricecharting_id") or "").strip()
                        current = current_by_id.get(product_id)
                        # Metadata, not content_hash. content_hash covers the
                        # six price columns, so gating on it rewrote the search
                        # document -- 15 indexes, ~7 GB of GIN -- every time a
                        # price moved, although no searchable text had changed.
                        # Measured at 49.7% of a 350-set ingest. Prices now go
                        # to pricecharting_current_price and never touch this
                        # table.
                        incoming = catalog_metadata_hash(row)
                        if current is not None and current.get("metadata_hash") == incoming:
                            skipped += 1
                            continue
                        changed_rows.append(row)
                    if changed_rows:
                        upsert_started_at = time.perf_counter()
                        total += self._upsert(
                            table="pricecharting_catalog",
                            rows=changed_rows,
                            batch_size=batch_size,
                            on_conflict="pricecharting_id",
                            label="catalog",
                        )
                        self.phase_seconds["catalog_upsert"] += (
                            time.perf_counter() - upsert_started_at
                        )
                except (SystemExit, Exception) as exc:
                    # This sub-batch failed (e.g. a statement timeout on a
                    # large/loaded table) -- log and move on to the next
                    # sub-batch rather than letting the exception abort the
                    # whole call. Every row in a failed sub-batch is treated
                    # as failed, even if some would have been skipped as
                    # unchanged, since _fetch_current_catalog_hashes may be
                    # what failed.
                    print(f"  Catalog upsert sub-batch failed, continuing: {exc}", flush=True)
                    failed_ids.extend(
                        str(row.get("pricecharting_id") or "").strip()
                        for row in batch
                        if row.get("pricecharting_id")
                    )
                    continue
                print(
                    f"Skipped {skipped} unchanged catalog rows; upserted {total} changed/new rows...",
                    flush=True,
                )
        self.catalog_write_stats["written"] += total
        self.catalog_write_stats["skippedUnchanged"] += skipped
        self.catalog_write_stats["failed"] += len(failed_ids)
        if failed_ids:
            raise PartialCatalogWriteError(
                f"{len(failed_ids)} catalog row(s) failed to upsert; {total} succeeded.",
                succeeded_count=total,
                failed_ids=failed_ids,
            )
        return total

    def _fetch_current_prices(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """The batch's current prices and browse keys.

        The price baseline is pricecharting_current_price itself, not the
        catalog. Once the catalog stops being written for price changes its
        cents freeze, and comparing against a frozen value reports "changed"
        on every run forever -- the same stale-baseline trap #213 hit when the
        SCD2 row stopped advancing, one table along.

        category and platform_group come back too so a metadata-only rename can
        be detected and pushed into the browse keys without a price move.
        """
        ids = [
            str(row.get("pricecharting_id") or "").strip()
            for row in rows
            if str(row.get("pricecharting_id") or "").strip()
        ]
        if not ids:
            return {}
        _assert_lookup_fits(ids, what="current price")
        response = client.get(
            f"{self.supabase_url}/rest/v1/pricecharting_current_price",
            params={
                "select": (
                    "pricecharting_id,currency,category,platform_group,"
                    + ",".join(PRICE_OBSERVATION_COLUMNS)
                ),
                "pricecharting_id": f"in.({','.join(ids)})",
            },
            headers=self._headers(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SystemExit(
                "Supabase current price lookup failed "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        rows_payload = response.json()
        if not isinstance(rows_payload, list):
            raise SystemExit("Supabase current price lookup returned invalid data.")
        _assert_lookup_complete(ids, rows_payload, what="current price")
        return {
            str(row.get("pricecharting_id")): row
            for row in rows_payload
            if isinstance(row, dict) and row.get("pricecharting_id")
        }

    def _upsert_current_price_rows(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
    ) -> int:
        """Upsert into pricecharting_current_price. One row per id, always."""
        if not rows:
            return 0
        response = client.post(
            f"{self.supabase_url}/rest/v1/pricecharting_current_price",
            params={"on_conflict": "pricecharting_id"},
            json=rows,
            headers={**self._headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SystemExit(
                "Supabase current price upsert failed "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        self.current_price_stats["upserted"] += len(rows)
        return len(rows)

    def _remember_catalog_hashes(
        self,
        rows: list[dict[str, Any]],
        fetched: dict[str, dict[str, Any]],
    ) -> None:
        """Record what a catalog lookup found, so the next pass need not ask.

        Every id in `rows` joins the covered set, including ids the lookup did
        not return -- absence is itself the answer ("no catalog row yet"), and
        losing that distinction would send new items down the fetch path.
        """
        for row in rows:
            product_id = str(row.get("pricecharting_id") or "").strip()
            if not product_id:
                continue
            self._catalog_lookup_covered.add(product_id)
            found = fetched.get(product_id)
            if found is not None:
                self._catalog_hash_cache[product_id] = catalog_metadata_hash(found)

    def _current_catalog_hashes(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """The batch's current content_hashes, from the cache when it can
        answer for EVERY id and from Supabase otherwise.

        All-or-nothing on purpose. A partial hit would mean a fetch anyway,
        and mixing cached and fetched answers within a batch is how a stale
        hash quietly turns a changed row into a skipped one. Nothing writes
        pricecharting_catalog between the two passes, so a cached hash is the
        same value the second fetch would have returned.
        """
        ids = [
            str(row.get("pricecharting_id") or "").strip()
            for row in rows
            if str(row.get("pricecharting_id") or "").strip()
        ]
        if not ids or not self._catalog_lookup_covered.issuperset(ids):
            # Normalised to the same shape the cached branch returns. They used
            # to differ -- the fetch path handed back raw rows while the cache
            # handed back a hash -- so the gate silently compared against None
            # and rewrote every row it was meant to skip.
            fetched = self._fetch_current_catalog_hashes(client, rows)
            return {
                product_id: {"metadata_hash": catalog_metadata_hash(found)}
                for product_id, found in fetched.items()
            }
        self.catalog_lookup_stats["reused"] += len(ids)
        return {
            product_id: {"metadata_hash": self._catalog_hash_cache[product_id]}
            for product_id in ids
            if product_id in self._catalog_hash_cache
        }

    def _fetch_current_catalog_hashes(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        ids = [
            str(row.get("pricecharting_id") or "").strip()
            for row in rows
            if str(row.get("pricecharting_id") or "").strip()
        ]
        if not ids:
            return {}
        # Truncation here is waste rather than corruption -- a missing hash
        # reads as "changed" and the row is rewritten -- but it is the same
        # silent cap, and a batch size that overflows one lookup overflows
        # both.
        _assert_lookup_fits(ids, what="catalog hash")
        self.catalog_lookup_stats["fetched"] += len(ids)
        response = client.get(
            f"{self.supabase_url}/rest/v1/pricecharting_catalog",
            params={
                # METADATA, not prices. The catalog stopped being the price
                # baseline in this PR -- pricecharting_current_price is, and it
                # has its own lookup. What the catalog is still authoritative
                # for is its own metadata, which is what decides whether the
                # 25 GB search document gets rewritten at all.
                "select": (
                    "pricecharting_id,"
                    + ",".join(CATALOG_METADATA_SIGNATURE_COLUMNS)
                ),
                "pricecharting_id": f"in.({','.join(ids)})",
            },
            headers=self._headers(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SystemExit(
                "Supabase catalog hash lookup failed "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        rows_payload = response.json()
        if not isinstance(rows_payload, list):
            raise SystemExit("Supabase catalog hash lookup returned invalid data.")
        _assert_lookup_complete(ids, rows_payload, what="catalog hash")
        return {
            str(row.get("pricecharting_id")): row
            for row in rows_payload
            if isinstance(row, dict) and row.get("pricecharting_id")
        }

    def sync_scd2_history_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        batch_size: int,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        # Checked HERE rather than at the lookup: the per-sub-batch handler
        # catches everything and reports "continuing", which would downgrade
        # a programming error into silently failed rows.
        if batch_size >= POSTGREST_MAX_LOOKUP_ROWS:
            raise ValueError(
                f"batch_size {batch_size} reaches PostgREST's "
                f"{POSTGREST_MAX_LOOKUP_ROWS}-row lookup cap; a full-cap reply "
                "cannot be told apart from a truncated one, so the batch must "
                "be strictly smaller (see the 23505 on "
                "pricecharting_catalog_history_current_unique_idx, 2026-09-08)."
            )
        inserted = 0
        failed_ids: list[str] = []
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                try:
                    # Timed apart. "scd2_comparison" wrapped all three of
                    # these, so the 65% it reported was read as the SCD2 GET
                    # when it is in fact three lookups against a 24 GB table, a
                    # 25 GB table and a 2 GB one. Optimising on that number
                    # would have been guessing which of the three to attack.
                    comparison_started_at = time.perf_counter()
                    current_by_id = self._fetch_current_history_rows(client, batch)
                    self.phase_seconds["scd2_history_lookup"] += (
                        time.perf_counter() - comparison_started_at
                    )
                    catalog_lookup_started_at = time.perf_counter()
                    # Each gate reads the table it is about to write. The SCD2
                    # row decides whether a version is written, the catalog row
                    # whether the search document is rewritten, the current
                    # price row whether a snapshot and a price upsert happen.
                    # Inferring one from another is how a partial failure turns
                    # into a permanently missed write: if the catalog write
                    # fails after the SCD2 version lands, a gate reading SCD2
                    # would conclude the catalog is up to date forever.
                    catalog_by_id = self._fetch_current_catalog_hashes(client, batch)
                    self._remember_catalog_hashes(batch, catalog_by_id)
                    self.phase_seconds["catalog_metadata_lookup"] += (
                        time.perf_counter() - catalog_lookup_started_at
                    )
                    price_lookup_started_at = time.perf_counter()
                    price_by_id = self._fetch_current_prices(client, batch)
                    self.phase_seconds["current_price_lookup"] += (
                        time.perf_counter() - price_lookup_started_at
                    )
                    # Kept as the sum so a run can still be compared against
                    # every measurement taken before this split.
                    self.phase_seconds["scd2_comparison"] += (
                        time.perf_counter() - comparison_started_at
                    )
                    rows_to_insert = []
                    changed_ids = []
                    price_observations = []
                    current_price_rows: list[dict[str, Any]] = []
                    for row in batch:
                        product_id = str(row.get("pricecharting_id") or "").strip()
                        if not product_id:
                            continue
                        current = current_by_id.get(product_id)
                        price_current = price_by_id.get(product_id)
                        # Two independent decisions, deliberately not chained.
                        # An earlier version of this short-circuited on
                        # change_hash equality before classifying, which is
                        # unsafe now that SCD2 prices freeze: a price that
                        # moved and came back to the frozen value matches the
                        # hash, and its genuine change would go unrecorded.
                        if current is None or metadata_differs(row, current):
                            history_row = to_catalog_history_row(row)
                            if current:
                                changed_ids.append(product_id)
                            rows_to_insert.append(history_row)
                        # A price move is a snapshot plus a current-price
                        # upsert, never a catalog write and never a version.
                        price_moved = (price_current is None
                                       or prices_differ(row, price_current))
                        if price_moved:
                            price_observations.append(to_price_observation_row(row))
                            self.current_price_stats["priceChanged"] += 1
                            current_price_rows.append(to_current_price_row(row))
                        elif price_current is not None and browse_keys_differ(row, price_current):
                            # Bullet 2: a rename that leaves the price alone
                            # still has to reach the browse keys, or Discover
                            # browses the old category until the next price
                            # move -- which for a stable item may be never.
                            self.current_price_stats["browseKeysOnly"] += 1
                            current_price_rows.append(to_current_price_row(row))

                    insert_started_at = time.perf_counter()
                    # Price observations are written BEFORE the legacy
                    # close/insert on purpose: PostgREST offers no
                    # cross-request transaction (the legacy close+insert
                    # pair is already non-atomic today), so consistency
                    # comes from ordering + idempotency instead. If this
                    # insert fails, the whole sub-batch fails BEFORE any
                    # legacy write -- the change hash still differs, so
                    # the job's normal retry redoes both sides; if the
                    # legacy write fails after this succeeded, the retry
                    # re-runs and this insert conflict-skips on the
                    # (pricecharting_id, observed_at) unique key. Either
                    # order of failure converges; neither can produce a
                    # legacy version whose price observation is silently
                    # unrecoverable.
                    if current_price_rows:
                        current_price_started_at = time.perf_counter()
                        self._upsert_current_price_rows(client, current_price_rows)
                        self.phase_seconds["current_price_upsert"] += (
                            time.perf_counter() - current_price_started_at
                        )
                    if price_observations:
                        self._insert_price_observation_rows(client, price_observations)
                        self.phase_seconds["price_snapshot_insert"] += (
                            time.perf_counter() - insert_started_at
                        )
                    close_started_at = time.perf_counter()
                    if changed_ids:
                        self._close_current_history_rows(
                            client,
                            pricecharting_ids=changed_ids,
                            valid_to=source_timestamp(batch[0].get("source_downloaded_at")),
                        )
                        self.phase_seconds["scd2_close"] += (
                            time.perf_counter() - close_started_at
                        )
                    version_started_at = time.perf_counter()
                    if rows_to_insert:
                        inserted += self._insert_history_rows(
                            client,
                            rows_to_insert,
                            batch_offset=index,
                        )
                        self.phase_seconds["scd2_insert"] += (
                            time.perf_counter() - version_started_at
                        )
                except (SystemExit, Exception) as exc:
                    # Same reasoning as upsert_rows(): don't let one
                    # sub-batch's failure abort every later sub-batch.
                    print(f"  Catalog history sub-batch failed, continuing: {exc}", flush=True)
                    failed_ids.extend(
                        str(row.get("pricecharting_id") or "").strip()
                        for row in batch
                        if row.get("pricecharting_id")
                    )
                    continue
                print(
                    f"Recorded {inserted} / {len(rows)} SCD2 history versions...",
                    flush=True,
                )
        if failed_ids:
            raise PartialCatalogWriteError(
                f"{len(failed_ids)} catalog history row(s) failed to sync; {inserted} succeeded.",
                succeeded_count=inserted,
                failed_ids=failed_ids,
            )
        return inserted

    def _fetch_current_history_rows(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        ids = [
            str(row.get("pricecharting_id") or "").strip()
            for row in rows
            if str(row.get("pricecharting_id") or "").strip()
        ]
        if not ids:
            return {}
        _assert_lookup_fits(ids, what="catalog history")
        response = client.get(
            f"{self.supabase_url}/rest/v1/pricecharting_catalog_history",
            params={
                # The METADATA columns decide whether a version is written
                # at all. change_hash cannot: it covers prices too, and once
                # price-only changes stop writing versions the stored row's
                # prices freeze, so its hash would differ on every price move
                # and mint exactly the versions this is meant to avoid.
                # Compared field by field against CATALOG_METADATA_SIGNATURE_
                # COLUMNS instead, which needs no migration and no rewrite of
                # the 18M hashes already stored.
                # change_hash is NOT selected. PR 4 replaced the hash
                # comparison with metadata_differs(), and the column stayed in
                # this SELECT reading nothing -- 64 bytes per row across the
                # batch, fetched from a 24 GB table, for a value no code
                # touches. The gate needs the metadata columns and nothing else.
                "select": (
                    "pricecharting_id,"
                    + ",".join(CATALOG_METADATA_SIGNATURE_COLUMNS)
                ),
                "is_current": "eq.true",
                "pricecharting_id": f"in.({','.join(ids)})",
            },
            headers=self._headers(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SystemExit(
                "Supabase catalog history lookup failed "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        rows_payload = response.json()
        if not isinstance(rows_payload, list):
            raise SystemExit("Supabase catalog history lookup returned invalid data.")
        _assert_lookup_complete(ids, rows_payload, what="catalog history")
        return {
            str(row.get("pricecharting_id")): row
            for row in rows_payload
            if isinstance(row, dict) and row.get("pricecharting_id")
        }

    def _close_current_history_rows(
        self,
        client: httpx.Client,
        *,
        pricecharting_ids: list[str],
        valid_to: str,
    ) -> None:
        request_started_at = time.perf_counter()
        response = client.patch(
            f"{self.supabase_url}/rest/v1/pricecharting_catalog_history",
            params={
                "pricecharting_id": f"in.({','.join(pricecharting_ids)})",
                "is_current": "eq.true",
            },
            headers={**self._headers(), "Prefer": "return=minimal"},
            json={"valid_to": valid_to, "is_current": False},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            record_db_failure(
                duration_seconds=time.perf_counter() - request_started_at,
                operation="history_close",
                row_count=len(pricecharting_ids),
                status_code=response.status_code,
                body=response.text,
                context={"table": "pricecharting_catalog_history"},
            )
            raise SystemExit(
                "Supabase catalog history close-current failed "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc

    def _insert_history_rows(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
        *,
        batch_offset: int,
    ) -> int:
        request_started_at = time.perf_counter()
        response = client.post(
            f"{self.supabase_url}/rest/v1/pricecharting_catalog_history",
            headers={**self._headers(), "Prefer": "return=minimal"},
            json=rows,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            record_db_failure(
                duration_seconds=time.perf_counter() - request_started_at,
                operation="history_insert",
                row_count=len(rows),
                status_code=response.status_code,
                body=response.text,
                context={"table": "pricecharting_catalog_history"},
            )
            raise SystemExit(
                "Supabase catalog history insert failed "
                f"at rows {batch_offset + 1}-{batch_offset + len(rows)} "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        return len(rows)

    def _insert_price_observation_rows(
        self,
        client: httpx.Client,
        rows: list[dict[str, Any]],
    ) -> None:
        # resolution=ignore-duplicates + return=representation: retried
        # observations conflict on (pricecharting_id, observed_at) and are
        # silently skipped by the DATABASE (not application memory); the
        # representation contains only the genuinely inserted rows, which
        # is what makes inserted-vs-duplicate observable.
        self.price_history_stats["attempted"] += len(rows)
        request_started_at = time.perf_counter()
        response = client.post(
            f"{self.supabase_url}/rest/v1/pricecharting_price_history",
            params={"on_conflict": "pricecharting_id,observed_at"},
            headers={
                **self._headers(),
                "Prefer": "resolution=ignore-duplicates,return=representation",
            },
            json=rows,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self.price_history_stats["failed"] += len(rows)
            record_db_failure(
                duration_seconds=time.perf_counter() - request_started_at,
                operation="price_observation_insert",
                row_count=len(rows),
                status_code=response.status_code,
                body=response.text,
                context={"table": "pricecharting_price_history"},
            )
            raise SystemExit(
                "Supabase price-history insert failed "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        try:
            inserted = len(response.json())
        except ValueError:
            inserted = len(rows)
        self.price_history_stats["inserted"] += inserted
        self.price_history_stats["duplicateSkipped"] += len(rows) - inserted
        # Batch-level observability only -- one aggregate line per write,
        # never per row.
        print(
            f"  price_history: attempted {len(rows)}, inserted {inserted}, "
            f"duplicate-skipped {len(rows) - inserted} "
            f"(run totals: {self.price_history_stats})",
            flush=True,
        )

    def _upsert(
        self,
        *,
        table: str,
        rows: list[dict[str, Any]],
        batch_size: int,
        on_conflict: str,
        label: str,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        # Checked HERE rather than at the lookup: the per-sub-batch handler
        # catches everything and reports "continuing", which would downgrade
        # a programming error into silently failed rows.
        if batch_size >= POSTGREST_MAX_LOOKUP_ROWS:
            raise ValueError(
                f"batch_size {batch_size} reaches PostgREST's "
                f"{POSTGREST_MAX_LOOKUP_ROWS}-row lookup cap; a full-cap reply "
                "cannot be told apart from a truncated one, so the batch must "
                "be strictly smaller (see the 23505 on "
                "pricecharting_catalog_history_current_unique_idx, 2026-09-08)."
            )
        total = 0
        headers = {**self._headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                total += self._post_batch(
                    client,
                    table=table,
                    batch=batch,
                    on_conflict=on_conflict,
                    label=label,
                    headers=headers,
                    batch_size=batch_size,
                    first_row_number=index + 1,
                )
                print(f"Imported {total} / {len(rows)} {label} rows...", flush=True)
        return total

    def _post_batch(
        self,
        client: "httpx.Client",
        *,
        table: str,
        batch: list[dict[str, Any]],
        on_conflict: str,
        label: str,
        headers: dict[str, str],
        batch_size: int,
        first_row_number: int,
        attempt: int = 1,
    ) -> int:
        """POST one batch, recovering from Postgres statement timeouts.

        A 57014 used to abandon the whole sub-batch: measured 2026-09-07,
        one `small-sets-refresh` run wrote 1,445 rows and lost 300 to three
        timeouts -- 17% of what it attempted, silently, every run.

        Two recovery levers, because the evidence supports both and neither
        alone is enough:

        * TIME. The timeouts are a fixed 8s cap (the PostgREST role's
          statement_timeout) hit by a latency tail, not by volume -- batches
          of 5, 6, 8 and 13 rows have timed out at 8.2s while batches of
          1,000 succeed. So the same rows usually write fine a moment later,
          and simply trying again is the lever that actually recovers rows.
        * SIZE. Halving is still worth doing because a smaller statement is
          a smaller target for whatever it was queued behind, and it costs
          nothing when the first lever is what works. It is NOT the primary
          fix, and this deliberately does not shrink the caller's batch_size
          for subsequent batches: that would be a permanent throughput cut
          in response to a transient condition.

        Gives up after MAX_TIMEOUT_ATTEMPTS and records the failure then, so
        one slow moment produces one ledger event rather than a cascade.
        """
        request_started_at = time.perf_counter()
        response = client.post(
            f"{self.supabase_url}/rest/v1/{table}",
            params={"on_conflict": on_conflict},
            headers=headers,
            json=batch,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            timed_out = "57014" in (response.text or "")
            can_retry = timed_out and attempt < MAX_TIMEOUT_ATTEMPTS
            if timed_out and attempt == 1:
                self.timeout_retry_stats["timeouts"] += 1
            if can_retry:
                self.timeout_retry_stats["retries"] += 1
                # Backoff before AND halving: the pause is what usually
                # works, the split is insurance.
                time.sleep(TIMEOUT_RETRY_BACKOFF_SECONDS * attempt)
                if len(batch) > 1:
                    middle = len(batch) // 2
                    halves = (batch[:middle], batch[middle:])
                else:
                    halves = (batch,)
                written = 0
                try:
                    for offset, half in enumerate(halves):
                        written += self._post_batch(
                            client,
                            table=table,
                            batch=half,
                            on_conflict=on_conflict,
                            label=label,
                            headers=headers,
                            batch_size=batch_size,
                            first_row_number=(
                                first_row_number + (len(halves[0]) if offset else 0)
                            ),
                            attempt=attempt + 1,
                        )
                except _StatementTimeoutExhausted:
                    if attempt > 1:
                        raise
                    record_db_failure(
                        duration_seconds=time.perf_counter() - request_started_at,
                        operation=f"{label}_upsert",
                        row_count=len(batch),
                        status_code=response.status_code,
                        body=response.text,
                        context={
                            "table": table,
                            "writeBatchSize": batch_size,
                            "attempts": MAX_TIMEOUT_ATTEMPTS,
                            "statementTimeout": True,
                            "rowsRecoveredBeforeGivingUp": written,
                        },
                    )
                    raise SystemExit(
                        f"Supabase {label} import failed at rows "
                        f"{first_row_number}-{first_row_number + len(batch) - 1} "
                        f"after {MAX_TIMEOUT_ATTEMPTS} statement-timeout attempts"
                    ) from exc
                self.timeout_retry_stats["rowsRecovered"] += written
                return written
            if timed_out:
                # Do not record here: the outermost frame does, with the
                # caller's real batch size. See _StatementTimeoutExhausted.
                self.timeout_retry_stats["rowsAbandoned"] += len(batch)
                raise _StatementTimeoutExhausted(len(batch)) from exc
            # Recorded HERE, at the request that actually failed, so the
            # event carries the real row count and operation -- and so no
            # outer layer records it again as it becomes a
            # PartialCatalogWriteError and then a False return.
            record_db_failure(
                duration_seconds=time.perf_counter() - request_started_at,
                operation=f"{label}_upsert",
                row_count=len(batch),
                status_code=response.status_code,
                body=response.text,
                context={
                    "table": table,
                    "writeBatchSize": batch_size,
                    "attempt": attempt,
                    "statementTimeout": timed_out,
                },
            )
            raise SystemExit(
                f"Supabase {label} import failed "
                f"at rows {first_row_number}-{first_row_number + len(batch) - 1} "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        return len(batch)

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


if __name__ == "__main__":
    raise SystemExit(main())
