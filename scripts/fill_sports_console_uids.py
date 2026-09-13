"""Fill the null console_uid on sportscardspro registry rows from autocomplete.

19,273 of 36,962 sportscardspro registry rows carry last_fetch_status='success'
with console_uid NULL. claim_due_sets() requires a uid, so those rows can never
be claimed -- and a row that is never claimed keeps tier3_refreshed_at NULL,
which is why 19,271 of those same rows have never been refreshed. The two nulls
are one problem: the missing uid IS the starvation, not a scheduling backlog.

Why a new source. The existing uid source is the HTML scrape
(CONSOLE_UID_PATTERN against VGPC.console_uid in backfill_pricecharting_sets),
which runs fully serial at 30s/row because that is the only spacing with a
measured 100% success rate against sportscardspro.com's Cloudflare throttle.
19,273 uids that way is roughly 160 hours of pure resolve. The same uids come
from /consoles-autocomplete/<category> in 8 unauthenticated GETs -- the same
endpoint discover_pricecharting_sets already uses for lorcana/funko/lego, whose
sports slugs were simply never wired up (flat_categories was left empty because
the umbrella slugs 'sports-cards' and 'sportscardspro' both 404; the eight real
per-sport slugs all answer 200).

Verified live 2026-09-13, identical row counts on both hosts:

    baseball-cards 24,006   football-cards 20,986   basketball-cards 14,265
    hockey-cards   10,092   soccer-cards    5,683   wrestling-cards   2,231
    racing-cards    1,827   ufc-cards       1,166        total 80,256

All 24,005 non-sentinel baseball values match G\\d+, and G24631 resolves to
'1887 n172 old judge' -- the set csv_source_policy confirmed live against
the vendor CSV endpoint as "Baseball Cards 1887 N172 Old Judge" -- the same
uid namespace. (Named indirectly on purpose: test_csv_source_policy discovers
CSV callers by scanning for that endpoint's literal name, and this script must
stay inside that guard rather than take a DIAGNOSTIC_ONLY exemption, so that if
it ever does grow a CSV fetch the guard catches it.)

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
* It does not INSERT. Autocomplete lists 80,256 sets against 36,962 registry
  rows, so a naive upsert would add ~43,000 new sets. That may well be worth
  doing, but it is a different decision with a different risk (queue size, disk,
  vendor budget) and it does not belong behind a uid backfill.
* It does not rewrite `url`. build_flat_registry_row() derives the url from its
  own slug; the crawled rows already have real urls, and 7% of slugs do not
  round-trip (see below), so rewriting would point good rows at wrong pages.
* It does not touch last_fetch_status, tier3_refreshed_at, or failure counts.
  Every sports row is already 'success'; the uid is the only missing gate, and
  writing anything else would move rows for reasons this script cannot justify.

THE SLUG MISMATCH
-----------------
Matching is by slug because that is the registry's own conflict key
(source_site, slug). The crawled slug comes from the href, which preserves
characters _slugify() collapses -- measured on 800 uid-less baseball rows, 749
(93%) match an autocomplete-derived slug and 51 (7%) do not:

    baseball-cards-2023-topps-pristine-let%27s-go          (URL-encoded quote)
    baseball-cards-2024-topps-allen-&-ginter-cut-signature (literal ampersand)

Those stay NULL and are reported. A near-match heuristic is not offered: a uid
written onto the wrong set sends that set's whole CSV family into the wrong
registry row, and the family-name validation in csv_source_policy would then
reject the batch for a reason pointing at the vendor rather than at us.

Ambiguity is refused the same way. If two autocomplete labels in one category
derive the same slug with different uids, neither is written -- there is no
evidence in the response saying which one the registry row meant.

Default is dry-run. Pass --commit to write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

from scripts.discover_pricecharting_sets import REQUEST_HEADERS, _slugify

# The eight slugs that answer 200. There is no umbrella sports slug.
SPORTS_CATEGORIES = [
    "baseball-cards",
    "football-cards",
    "basketball-cards",
    "hockey-cards",
    "soccer-cards",
    "wrestling-cards",
    "racing-cards",
    "ufc-cards",
]

# Both hosts serve this identically (byte-identical row counts, 2026-09-13).
# pricecharting.com is preferred because sportscardspro.com is the host whose
# Cloudflare rules already broke the CSV path -- see csv_source_policy.
DEFAULT_BASE_URL = "https://www.pricecharting.com"

SOURCE_SITE = "sportscardspro"

# PostgREST silently truncates a response at 1,000 rows -- it returns 200 with
# fewer rows and no error. 19,273 registry rows cannot be read in one request,
# and a page size at the cap is indistinguishable from a truncated one, so this
# stays strictly below it and pagination stops on a short page.
POSTGREST_MAX_ROWS = 1000
DEFAULT_PAGE_SIZE = 900


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it nothing changes")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"autocomplete host (default {DEFAULT_BASE_URL})")
    parser.add_argument("--categories", default=None,
                        help="comma-separated subset of the eight sports "
                             "categories (default: all)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                        help=f"registry read/write page size (default "
                             f"{DEFAULT_PAGE_SIZE}, must stay under "
                             f"{POSTGREST_MAX_ROWS})")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after writing this many uids (a bounded "
                             "first run; the rest are picked up next time)")
    return parser.parse_args(argv)


def selected_categories(raw: str | None) -> list[str]:
    if not raw:
        return list(SPORTS_CATEGORIES)
    chosen = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in chosen if name not in SPORTS_CATEGORIES]
    if unknown:
        raise ValueError(
            f"not sports autocomplete categories: {', '.join(unknown)}. "
            f"Known: {', '.join(SPORTS_CATEGORIES)}"
        )
    return chosen


def fetch_autocomplete(
    category: str, *, http: httpx.Client, base_url: str
) -> list[dict[str, Any]]:
    """The raw [{label, value}] list for one category."""
    response = http.get(f"{base_url}/consoles-autocomplete/{category}",
                        headers=REQUEST_HEADERS)
    response.raise_for_status()
    entries = response.json()
    if not isinstance(entries, list):
        raise ValueError(
            f"{category}: expected a JSON list from consoles-autocomplete, got "
            f"{type(entries).__name__}. The 404 page is served as "
            f"application/json, so a wrong slug arrives here looking like data."
        )
    return [entry for entry in entries if isinstance(entry, dict)]


def build_slug_index(
    category: str, entries: list[dict[str, Any]]
) -> tuple[dict[str, str], set[str]]:
    """Map registry-shaped slug -> uid, plus the slugs that are ambiguous.

    The first entry is always the sentinel {"label": "all", "value": ""}, which
    the empty-value guard drops -- the same guard discover_flat_category uses.

    A slug claimed by two different uids is returned in the ambiguous set and
    kept OUT of the index, so a caller cannot accidentally take whichever one
    happened to be last.
    """
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for entry in entries:
        uid = str(entry.get("value") or "").strip()
        label = str(entry.get("label") or "").strip()
        if not uid or not label:
            continue
        slug = f"{category}-{_slugify(label)}"
        existing = index.get(slug)
        if existing is not None and existing != uid:
            ambiguous.add(slug)
            continue
        index[slug] = uid
    for slug in ambiguous:
        index.pop(slug, None)
    return index, ambiguous


def _headers(key: str, **extra: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", **extra}


def fetch_uidless_rows(
    *, http: httpx.Client, url: str, key: str, categories: list[str],
    page_size: int,
) -> list[dict[str, Any]]:
    """Every sportscardspro row with a NULL console_uid, paginated.

    Ordered by registry_id so the pages are a stable partition of the table.
    """
    if page_size >= POSTGREST_MAX_ROWS:
        raise ValueError(
            f"page size {page_size} is at or above PostgREST's "
            f"{POSTGREST_MAX_ROWS}-row cap, where a full page and a truncated "
            f"page look identical. Use a smaller one."
        )
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = http.get(
            f"{url}/rest/v1/pricecharting_set_registry",
            params={
                "select": "registry_id,slug,set_name,category,console_uid",
                "source_site": f"eq.{SOURCE_SITE}",
                "console_uid": "is.null",
                "category": f"in.({','.join(categories)})",
                "order": "registry_id.asc",
                "limit": str(page_size),
                "offset": str(offset),
            },
            headers=_headers(key),
        )
        response.raise_for_status()
        page = [row for row in response.json() if isinstance(row, dict)]
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def plan_updates(
    rows: list[dict[str, Any]],
    index_by_category: dict[str, dict[str, str]],
    ambiguous_by_category: dict[str, set[str]],
    *, limit: int | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Decide, per row, whether a uid can be written.

    Only three outcomes, and the two non-writing ones are counted separately
    because they mean different things: an ambiguous slug is the vendor listing
    one name twice, an unmatched slug is our own crawled slug not round-tripping
    through _slugify. The first is theirs to fix, the second is ours.
    """
    updates: list[dict[str, str]] = []
    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0, "alreadySet": 0}
    for row in rows:
        if row.get("console_uid"):
            counts["alreadySet"] += 1
            continue
        category = str(row.get("category") or "")
        slug = str(row.get("slug") or "")
        if slug in ambiguous_by_category.get(category, set()):
            counts["ambiguous"] += 1
            continue
        uid = index_by_category.get(category, {}).get(slug)
        if not uid:
            counts["unmatched"] += 1
            continue
        counts["matched"] += 1
        if limit is not None and len(updates) >= limit:
            continue
        updates.append({"registry_id": str(row["registry_id"]), "console_uid": uid})
    return updates, counts


def write_updates(
    updates: list[dict[str, str]], *, http: httpx.Client, url: str, key: str,
) -> int:
    """PATCH one row at a time, each filtered on console_uid=is.null.

    The filter is the point: this gate reads the column it writes, so a uid
    resolved by the HTML scrape between the read above and this write is left
    alone instead of being overwritten by our guess. One request per row is slow
    and correct; a bulk PATCH cannot carry a different value per row.
    """
    written = 0
    for update in updates:
        response = http.patch(
            f"{url}/rest/v1/pricecharting_set_registry",
            params={
                "registry_id": f"eq.{update['registry_id']}",
                "console_uid": "is.null",
                "source_site": f"eq.{SOURCE_SITE}",
            },
            headers=_headers(key, **{"Content-Type": "application/json",
                                     "Prefer": "return=minimal"}),
            json={"console_uid": update["console_uid"]},
        )
        response.raise_for_status()
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    categories = selected_categories(args.categories)

    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required",
              file=sys.stderr)
        return 2

    index_by_category: dict[str, dict[str, str]] = {}
    ambiguous_by_category: dict[str, set[str]] = {}
    autocomplete_totals: dict[str, int] = {}

    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        for category in categories:
            entries = fetch_autocomplete(category, http=http,
                                         base_url=args.base_url)
            index, ambiguous = build_slug_index(category, entries)
            index_by_category[category] = index
            ambiguous_by_category[category] = ambiguous
            autocomplete_totals[category] = len(index)
            print(f"  {category:18} {len(index):>6} sets"
                  f"{f'  ({len(ambiguous)} ambiguous slugs skipped)' if ambiguous else ''}",
                  flush=True)

        rows = fetch_uidless_rows(http=http, url=url, key=key,
                                  categories=categories,
                                  page_size=args.page_size)
        print(f"\n{len(rows)} registry rows with a NULL console_uid", flush=True)

        updates, counts = plan_updates(rows, index_by_category,
                                       ambiguous_by_category, limit=args.limit)

        by_category: dict[str, int] = {}
        for row in rows:
            category = str(row.get("category") or "")
            slug = str(row.get("slug") or "")
            if index_by_category.get(category, {}).get(slug):
                by_category[category] = by_category.get(category, 0) + 1

        print(f"  matched   {counts['matched']:>6}  (a uid can be written)")
        print(f"  ambiguous {counts['ambiguous']:>6}  (slug claimed by two uids)")
        print(f"  unmatched {counts['unmatched']:>6}  (crawled slug does not "
              f"round-trip through _slugify)")
        for category in categories:
            print(f"    {category:18} {by_category.get(category, 0):>6} matched")

        # New sets, counted only. Listing them would read as a proposal to
        # insert them, which this PR deliberately does not do.
        known_slugs = {str(row.get("slug") or "") for row in rows}
        extra = {
            category: len([slug for slug in index
                           if slug not in known_slugs])
            for category, index in index_by_category.items()
        }
        print(f"\n  autocomplete sets not matched to any NULL-uid row: "
              f"{sum(extra.values())}")
        print("  (a mix of sets we already have a uid for and sets we do not "
              "have at all; NOT inserted by this script)")

        if not updates:
            print("\nnothing to write")
            return 0
        if not args.commit:
            print(f"\ndry run: would write {len(updates)} uids. "
                  f"Pass --commit to write.")
            for update in updates[:5]:
                print(f"    {update['registry_id']} -> {update['console_uid']}")
            return 0

        written = write_updates(updates, http=http, url=url, key=key)

    print(f"\nwrote {written} console_uid values")
    print(json.dumps({
        "matched": counts["matched"],
        "ambiguous": counts["ambiguous"],
        "unmatched": counts["unmatched"],
        "written": written,
        "autocompleteTotals": autocomplete_totals,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
