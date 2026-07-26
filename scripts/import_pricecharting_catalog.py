import argparse
import base64
import csv
import hashlib
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

CATALOG_COLUMNS = (
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
)

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
    handle = io.StringIO(csv_text)
    return [dict(row) for row in csv.DictReader(handle)]


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
    return normalize_catalog_row(catalog_row)


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
        role = _supabase_jwt_role(self.service_role_key)
        if role and role != "service_role":
            raise SystemExit(
                "SUPABASE_SERVICE_ROLE_KEY must be the Supabase service_role key "
                f"for catalog imports, but the configured key has role '{role}'."
            )

    def upsert_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        return self._upsert(
            table="pricecharting_catalog",
            rows=rows,
            batch_size=batch_size,
            on_conflict="pricecharting_id",
            label="catalog",
        )

    def sync_scd2_history_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        batch_size: int,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        inserted = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                current_by_id = self._fetch_current_history_rows(client, batch)
                rows_to_insert = []
                changed_ids = []
                for row in batch:
                    product_id = str(row.get("pricecharting_id") or "").strip()
                    if not product_id:
                        continue
                    history_row = to_catalog_history_row(row)
                    current = current_by_id.get(product_id)
                    if current and current.get("change_hash") == history_row["change_hash"]:
                        continue
                    if current:
                        changed_ids.append(product_id)
                    rows_to_insert.append(history_row)

                if changed_ids:
                    self._close_current_history_rows(
                        client,
                        pricecharting_ids=changed_ids,
                        valid_to=source_timestamp(batch[0].get("source_downloaded_at")),
                    )
                if rows_to_insert:
                    inserted += self._insert_history_rows(
                        client,
                        rows_to_insert,
                        batch_offset=index,
                    )
                print(
                    f"Recorded {inserted} / {len(rows)} SCD2 history versions...",
                    flush=True,
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
        response = client.get(
            f"{self.supabase_url}/rest/v1/pricecharting_catalog_history",
            params={
                "select": "pricecharting_id,change_hash",
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
        response = client.post(
            f"{self.supabase_url}/rest/v1/pricecharting_catalog_history",
            headers={**self._headers(), "Prefer": "return=minimal"},
            json=rows,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SystemExit(
                "Supabase catalog history insert failed "
                f"at rows {batch_offset + 1}-{batch_offset + len(rows)} "
                f"with HTTP {response.status_code}: {response.text}"
            ) from exc
        return len(rows)

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
        total = 0
        headers = {**self._headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/{table}",
                    params={"on_conflict": on_conflict},
                    headers=headers,
                    json=batch,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SystemExit(
                        f"Supabase {label} import failed "
                        f"at rows {index + 1}-{index + len(batch)} "
                        f"with HTTP {response.status_code}: {response.text}"
                    ) from exc
                total += len(batch)
                print(f"Imported {total} / {len(rows)} {label} rows...", flush=True)
        return total

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
