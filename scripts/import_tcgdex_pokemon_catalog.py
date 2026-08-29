"""Import Pokemon card images from TCGdex into tcgdex_pokemon_catalog.

Source: api.tcgdex.net (free, no API key, MIT-licensed multilanguage
Pokemon TCG database with its own image CDN at assets.tcgdex.net).
Reviewer-approved production Pokemon image source (2026-08-29): measured
92.7% image coverage across its 23.5K English cards, flat across eras;
Japanese coverage concentrated in 2022-2024 and used opportunistically.

Imports BOTH languages:
  en -- all sets (the primary English image path)
  ja -- all sets (only hand-mapped sets are ever looked up, see
        app/services/pricing/tcgdex_pokemon_sets.py, but importing all of
        them costs nothing extra and lets the map grow without re-import)

image_url stores TCGdex's asset BASE url exactly as returned (no quality
suffix); readers append /high.webp (600x825) or /low.webp (245x337) per
surface, per TCGdex's own Assets documentation. Missing images stay null
-- TCGdex adds community images over time, so re-running this script
UPDATES rows (keyed on language+set_id+local_id).

TCGdex asks consumers to be considerate and cache rather than hammer:
one run is ~400 requests (218 en + 184 ja set fetches) with a small
delay, intended to be re-run daily/weekly, not per-request.
"""

import argparse
import os
import sys
import time
from typing import Any
from urllib.parse import quote

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pricing.tcgdex_pokemon_sets import (  # noqa: E402
    normalize_card_number,
    normalize_set_key,
)

DEFAULT_BASE_URL = "https://api.tcgdex.net/v2"
DEFAULT_BATCH_SIZE = 500
DEFAULT_REQUEST_DELAY_SECONDS = 0.1
USER_AGENT = "CollectIQCatalogBuilder/1.0 (+https://packlox.com)"
LANGUAGES = ("en", "ja")


class TCGdexClient:
    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(
            timeout=timeout_seconds, headers={"User-Agent": USER_AGENT}
        )

    def _get(self, path: str) -> Any:
        for attempt in range(3):
            try:
                response = self._client.get(f"{self.base_url}{path}")
                response.raise_for_status()
                return response.json()
            except Exception:
                if attempt == 2:
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def fetch_sets(self, language: str) -> list[dict[str, Any]]:
        payload = self._get(f"/{language}/sets")
        return [s for s in payload or [] if isinstance(s, dict)]

    def fetch_set(self, language: str, set_id: str) -> dict[str, Any] | None:
        payload = self._get(f"/{language}/sets/{quote(set_id, safe='')}")
        return payload if isinstance(payload, dict) else None


def to_rows(language: str, set_payload: dict[str, Any]) -> list[dict[str, Any]]:
    set_id = str(set_payload.get("id") or "").strip()
    set_name = str(set_payload.get("name") or "").strip()
    if not set_id or not set_name:
        return []
    set_key = normalize_set_key(set_name)
    rows: list[dict[str, Any]] = []
    for card in set_payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        local_id = str(card.get("localId") or "").strip()
        if not local_id:
            continue
        image = str(card.get("image") or "").strip() or None
        rows.append(
            {
                "language": language,
                "set_id": set_id,
                "set_name": set_name,
                "set_key": set_key,
                "local_id": local_id,
                "local_id_norm": normalize_card_number(local_id),
                "card_name": str(card.get("name") or "").strip() or None,
                "image_url": image,
            }
        )
    return rows


class SupabaseClient:
    def __init__(self, *, supabase_url: str, service_role_key: str,
                 timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required."
            )

    def upsert_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        total = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index:index + batch_size]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/tcgdex_pokemon_catalog",
                    params={"on_conflict": "language,set_id,local_id"},
                    headers={
                        "apikey": self.service_role_key,
                        "Authorization": f"Bearer {self.service_role_key}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates,return=minimal",
                    },
                    json=batch,
                )
                if response.status_code >= 400:
                    print(
                        f"  Batch {index}-{index + len(batch)} failed: HTTP "
                        f"{response.status_code} {response.text[:300]}",
                        flush=True,
                    )
                    continue
                total += len(batch)
        return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--supabase-url", default="",
                        help="Defaults to SUPABASE_URL.")
    parser.add_argument("--service-role-key", default="",
                        help="Defaults to SUPABASE_SERVICE_ROLE_KEY.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--request-delay-seconds", type=float,
                        default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument("--languages", default=",".join(LANGUAGES))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    tcgdex = TCGdexClient(base_url=args.base_url,
                          timeout_seconds=args.timeout_seconds)

    all_rows: list[dict[str, Any]] = []
    for language in [x.strip() for x in args.languages.split(",") if x.strip()]:
        sets = tcgdex.fetch_sets(language)
        print(f"[{language}] {len(sets)} sets", flush=True)
        for i, s in enumerate(sets):
            payload = tcgdex.fetch_set(language, str(s.get("id") or ""))
            time.sleep(args.request_delay_seconds)
            if payload is None:
                print(f"[{language}] failed to fetch set {s.get('id')}",
                      flush=True)
                continue
            all_rows.extend(to_rows(language, payload))
            if (i + 1) % 50 == 0:
                print(f"[{language}] fetched {i + 1}/{len(sets)} sets",
                      flush=True)

    with_image = sum(1 for r in all_rows if r["image_url"])
    print(
        f"Prepared {len(all_rows)} rows ({with_image} with image = "
        f"{100 * with_image / len(all_rows):.1f}%).",
        flush=True,
    )

    if args.dry_run:
        print("Dry run: no writes.", flush=True)
        return 0

    supabase = SupabaseClient(
        supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
        service_role_key=args.service_role_key
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        timeout_seconds=args.timeout_seconds,
    )
    total = supabase.upsert_rows(all_rows, batch_size=args.batch_size)
    print(f"Upserted {total}/{len(all_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
