"""Bulk-import video game cover art from the RAWG API into rawg_video_game_catalog.

Source: api.rawg.io -- the only image source in this project with explicit
written commercial-use permission (their Terms of Service: free tier is
free for commercial use under 100k MAU/500k page views, in exchange for
attribution -- see the mobile app's About screen "Legal" section).

RAWG's full catalog is 500,000+ games -- far too large to bulk-import
wholesale. Instead this imports every game across the ~24 mainstream
platforms PriceCharting's console_name values map to (see
_VIDEO_GAME_PLATFORM_RAWG_MAP in app/services/pricing/catalog_search_service.py),
using RAWG's list endpoint filtered by platform id (not their search
endpoint). Confirmed live before writing this script: PlayStation 4 alone
has ~7,000 games, so importing all mapped platforms fits comfortably
inside RAWG's 20,000 requests/month free-tier budget (roughly 1,000-2,000
requests at 40 games/page).

One row per (game, platform) pair -- the same title released on multiple
platforms gets one row per platform, since PriceCharting prices each
platform's release as a separate catalog row and matching happens on
(normalized_name, rawg_platform) together.

Supports --limit (cap total rows written, for a small test run) and
--platforms (restrict to specific platform names, comma-separated) so this
can be run small first before a full import.

Re-running this script UPSERTs on content_hash (rawg_id + platform +
image_url), so it's safe to re-run periodically to pick up newly released
games -- RAWG's own catalog changes over time.
"""

import argparse
import hashlib
import os
import re
import time
from typing import Any

import httpx


DEFAULT_API_BASE = "https://api.rawg.io/api"
DEFAULT_BATCH_SIZE = 200
DEFAULT_PAGE_SIZE = 40
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
DEFAULT_PLATFORM_DELAY_SECONDS = 10.0

# Must stay in sync with _VIDEO_GAME_PLATFORM_RAWG_MAP's *values* in
# app/services/pricing/catalog_search_service.py -- these are RAWG's own
# platform ids (confirmed live via GET /api/platforms), keyed by the same
# RAWG platform name string used there.
RAWG_PLATFORM_IDS: dict[str, int] = {
    "PlayStation 5": 187,
    "PlayStation 4": 18,
    "PlayStation 3": 16,
    "PlayStation 2": 15,
    "PlayStation Vita": 19,
    "PlayStation": 27,
    "Xbox Series S/X": 186,
    "Xbox One": 1,
    "Xbox 360": 14,
    "Xbox": 80,
    "Nintendo Switch": 7,
    "Wii U": 10,
    "Wii": 11,
    "GameCube": 105,
    "Nintendo 64": 83,
    "SNES": 79,
    "NES": 49,
    "3DS": 8,
    "DS": 9,
    "Game Boy Advance": 24,
    "Game Boy Color": 43,
    "Game Boy": 26,
    "PSP": 17,
    "PC": 4,
}

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    return _WHITESPACE_RE.sub(" ", name).strip().lower()


def content_hash_for(rawg_id: int, platform: str, image_url: str) -> str:
    raw = f"{rawg_id}|{platform}|{image_url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def to_catalog_row(game: dict[str, Any], platform_name: str) -> dict[str, Any] | None:
    rawg_id = game.get("id")
    name = str(game.get("name") or "").strip()
    slug = str(game.get("slug") or "").strip()
    image_url = str(game.get("background_image") or "").strip()
    if not isinstance(rawg_id, int) or not name or not slug or not image_url:
        return None
    released = str(game.get("released") or "").strip()
    # PostgREST's bulk-insert endpoint requires every object in a batch to
    # have the exact same set of keys (confirmed live: PGRST102 "All object
    # keys must match" on any batch mixing games with/without a release
    # date) -- always include the key, null when there's no date, rather
    # than omitting it conditionally.
    row: dict[str, Any] = {
        "rawg_id": rawg_id,
        "rawg_slug": slug,
        "name": name,
        "normalized_name": normalize_name(name),
        "rawg_platform": platform_name,
        "image_url": image_url,
        "content_hash": content_hash_for(rawg_id, platform_name, image_url),
        "released": released or None,
    }
    return row


class RawgClient:
    def __init__(self, *, api_key: str, api_base: str, timeout_seconds: float) -> None:
        self.api_key = api_key.strip()
        self.api_base = api_base.strip().rstrip("/")
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise SystemExit("RAWG_API_KEY is required.")

    def fetch_platform_games(
        self, platform_id: int, *, page_size: int, request_delay_seconds: float, limit: int | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        url: str | None = f"{self.api_base}/games"
        params: dict[str, Any] | None = {
            "platforms": platform_id,
            "page_size": page_size,
            "key": self.api_key,
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            while url:
                try:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as error:
                    # RAWG appears to cap how deep search-result pagination
                    # goes (confirmed live: a 404 on page 251 of a ~10,000+
                    # result platform, well before that platform's own
                    # reported total `count`) -- treat this the same as
                    # running out of pages, not a fatal error: keep
                    # whatever this platform already yielded rather than
                    # losing every other platform's completed work.
                    print(f"    stopped early ({error.__class__.__name__}), keeping {len(results)} so far", flush=True)
                    break
                page_results = payload.get("results") if isinstance(payload, dict) else None
                if isinstance(page_results, list):
                    results.extend(row for row in page_results if isinstance(row, dict))
                if limit is not None and len(results) >= limit:
                    return results[:limit]
                url = payload.get("next") if isinstance(payload, dict) else None
                params = None  # `next` already has all query params embedded.
                if url:
                    time.sleep(request_delay_seconds)
        return results


class RawgVideoGameCatalogWriter:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

    def upsert_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        total = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/rawg_video_game_catalog",
                    params={"on_conflict": "content_hash"},
                    headers={
                        **self._headers(),
                        "Prefer": "resolution=merge-duplicates,return=minimal",
                    },
                    json=batch,
                )
                if response.status_code >= 400:
                    print(
                        f"  Batch {index}-{index + len(batch)} failed: "
                        f"HTTP {response.status_code} {response.text[:300]}",
                        flush=True,
                    )
                    continue
                total += len(batch)
                print(f"Upserted {total}/{len(rows)} rows...", flush=True)
        return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default="", help="Defaults to RAWG_API_KEY.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument("--platform-delay-seconds", type=float, default=DEFAULT_PLATFORM_DELAY_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--platforms",
        default="",
        help="Comma-separated RAWG platform names to import (default: all mapped platforms).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap total rows written across all platforms (for a small test run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform but do not write to Supabase.",
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or os.getenv("RAWG_API_KEY", "")
    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    platform_names = (
        [p.strip() for p in args.platforms.split(",") if p.strip()]
        if args.platforms
        else list(RAWG_PLATFORM_IDS.keys())
    )
    unknown = [p for p in platform_names if p not in RAWG_PLATFORM_IDS]
    if unknown:
        raise SystemExit(f"Unknown platform name(s): {unknown}. Known: {list(RAWG_PLATFORM_IDS)}")

    rawg = RawgClient(api_key=api_key, api_base=args.api_base, timeout_seconds=args.timeout_seconds)
    writer = None
    if not args.dry_run:
        writer = RawgVideoGameCatalogWriter(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            timeout_seconds=args.timeout_seconds,
        )

    total_fetched = 0
    total_written = 0
    for platform_name in platform_names:
        remaining = None if args.limit is None else max(0, args.limit - total_fetched)
        if remaining == 0:
            break
        print(f"Fetching {platform_name} (id {RAWG_PLATFORM_IDS[platform_name]})...", flush=True)
        games = rawg.fetch_platform_games(
            RAWG_PLATFORM_IDS[platform_name],
            page_size=args.page_size,
            request_delay_seconds=args.request_delay_seconds,
            limit=remaining,
        )
        print(f"  {len(games)} games fetched.", flush=True)
        rows = [row for row in (to_catalog_row(g, platform_name) for g in games) if row is not None]
        total_fetched += len(rows)

        # Write each platform's rows immediately rather than accumulating
        # everything in memory until the very end -- a failure on a later
        # platform (RAWG's pagination cap, a network blip, etc.) must never
        # lose every earlier platform's already-fetched, already-good data.
        if args.dry_run:
            print("  Dry run: sample rows:", flush=True)
            for row in rows[:3]:
                print("   ", row, flush=True)
        elif rows and writer is not None:
            written = writer.upsert_rows(rows, batch_size=args.batch_size)
            total_written += written

        if args.limit is not None and total_fetched >= args.limit:
            break
        print(f"  Pausing {args.platform_delay_seconds:g}s before the next platform...", flush=True)
        time.sleep(args.platform_delay_seconds)

    if args.dry_run:
        print(f"Dry run complete. Would have written {total_fetched} rows total.", flush=True)
    else:
        print(f"Done. Upserted {total_written}/{total_fetched} rows total.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
