import argparse
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = load_sources(args)
    imported_rows: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    for source in sources:
        rows = source.rows
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
    total = client.upsert_rows(imported_rows, batch_size=args.batch_size)
    print(f"Imported {total} PriceCharting catalog rows into Supabase.")
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
) -> list[CatalogSource]:
    sources: list[CatalogSource] = []
    if source_filter is not None and source_filter not in PRICECHARTING_CSV_ENV_VARS:
        allowed_sources = ", ".join(sorted(PRICECHARTING_CSV_ENV_VARS))
        raise SystemExit(f"Unsupported source. Use one of: {allowed_sources}.")

    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        for category, env_name in PRICECHARTING_CSV_ENV_VARS.items():
            if source_filter is not None and category != source_filter:
                continue
            url = os.getenv(env_name, "").strip()
            if not url:
                continue
            response = client.get(url, headers={"Accept": "text/csv,*/*"})
            response.raise_for_status()
            rows = load_rows_from_text(response.text)
            sources.append(CatalogSource(name=f"{category}.csv", rows=rows))
    if not sources:
        if source_filter:
            env_name = PRICECHARTING_CSV_ENV_VARS[source_filter]
            raise SystemExit(f"{env_name} is not configured.")
        raise SystemExit("No PRICECHARTING_CSV_*_URL environment variables were configured.")
    return sources


def load_rows_from_text(csv_text: str) -> list[dict[str, str]]:
    handle = io.StringIO(csv_text)
    return [dict(row) for row in csv.DictReader(handle)]


def to_catalog_row(
    row: dict[str, Any],
    source_file: str,
    source_downloaded_at: str,
) -> dict[str, Any] | None:
    product_id = pick_text(row, TEXT_FIELDS["pricecharting_id"])
    product_name = pick_text(row, TEXT_FIELDS["product_name"])
    if not product_id or not product_name:
        return None

    console_name = pick_text(row, TEXT_FIELDS["console_name"])
    catalog_row: dict[str, Any] = {
        "pricecharting_id": product_id,
        "product_name": product_name,
        "console_name": console_name,
        "category": pick_text(row, TEXT_FIELDS["category"]) or console_name,
        "upc": pick_text(row, TEXT_FIELDS["upc"]),
        "asin": pick_text(row, TEXT_FIELDS["asin"]),
        "epid": pick_text(row, TEXT_FIELDS["epid"]),
        "release_date": parse_date(pick_text(row, TEXT_FIELDS["release_date"])),
        "currency": "USD",
        "product_url": pick_text(row, TEXT_FIELDS["product_url"]),
        "normalized_identity": normalized_identity(product_name, console_name),
        "raw_payload": row,
        "source_file": source_file,
        "source_downloaded_at": source_downloaded_at,
    }
    for target, aliases in PRICE_FIELDS.items():
        catalog_row[target] = parse_price_cents(pick_text(row, aliases))
    return {
        key: value
        for key, value in catalog_row.items()
        if value is not None and value != ""
    }


def pick_text(row: dict[str, Any], aliases: list[str]) -> str:
    normalized = {normalize_key(key): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(normalize_key(alias))
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


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


def parse_price_cents(value: str) -> int | None:
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    if not cleaned or cleaned in {"-", "N/A", "n/a"}:
        return None
    if cleaned.startswith("$") or "." in cleaned:
        cleaned = cleaned.replace("$", "")
        try:
            return max(0, round(float(cleaned) * 100))
        except ValueError:
            return None
    try:
        cents = int(float(cleaned))
    except ValueError:
        return None
    return cents if cents > 0 else None


class SupabaseCatalogClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def upsert_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        total = 0
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/pricecharting_catalog",
                    params={"on_conflict": "pricecharting_id"},
                    headers=headers,
                    json=batch,
                )
                response.raise_for_status()
                total += len(batch)
        return total


if __name__ == "__main__":
    raise SystemExit(main())
