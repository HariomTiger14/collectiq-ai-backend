"""Store the vendor's own name for each sports set, matched by console_uid.

WHY

2026-09-13, 20:50-22:20 UTC: the sportscardspro rotation re-downloaded the same
350 console_uids every ten minutes, eleven times, spending twelve of the day's
141 account-wide CSV slots on a file already known to be refused. One family in
it matched no requested set:

    registry set_name  : '2015 Topps Platinum Autograph Rookies'    (G9533)
    vendor console-name: 'football cards 2015 topps platinum autographed
                          rookie refractor'

The vendor renamed the set; our name came from an HTML crawl on 2026-08-15.
validate_csv_families is fail-closed -- one unmatched family refuses the whole
file -- so the sets were never stamped, and claim_due_sets orders
tier3_refreshed_at NULLS FIRST, which hands back the identical 350 next tick.

#225 already widens the expected names with SIBLING set_names, which covers a
uid registered twice under two spellings (G9157: 'Donrus' and 'Donruss'). It
cannot cover a rename where we hold a single row with the old name. This fills
that gap: /consoles-autocomplete returns the vendor's current label for every
uid, and storing it lets the downloader accept either name.

Measured 2026-09-14 across 35,649 claimable sportscardspro uids, comparing the
real _matches_requested against sibling-widened names:

    would fail on set_name alone                  5
    would fail WITH sibling widening              2   (G9533, G58581)
    uids absent from autocomplete                 1

So this is not a large repair. It is a cheap one that removes a recurring class
of 90-minute jams rather than the two rows currently in it.

WHAT THIS DOES NOT DO

  * It does not touch set_name. G58581 is the reason: our row reads
    '1993 Classic', the vendor says '1993 classic four sport'. A bare
    '1993 classic' genuinely exists -- G218 basketball, G15788 football,
    G53812 baseball -- just not in hockey, where our row sits. Our name is
    less specific, not wrong, and search and display already use it. Both
    names are true and the guard must accept either, so both are kept.
  * It does not INSERT. Autocomplete lists 80,270 sets against ~36,000
    registry rows; adding the difference is a separate decision.
  * It does not touch url, last_fetch_status, tier3_refreshed_at or any
    failure counter. A label is not a refresh.

Matched on console_uid, not on slug: the uid is the identity the vendor and the
registry already agree on, so no name normalisation is involved in deciding
which row a label belongs to.

Default is dry-run. Pass --commit to write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

from scripts.fill_sports_console_uids import (
    DEFAULT_BASE_URL,
    DEFAULT_PAGE_SIZE,
    POSTGREST_MAX_ROWS,
    SOURCE_SITE,
    SPORTS_CATEGORIES,
    _headers,
    fetch_autocomplete,
    selected_categories,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it nothing changes")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--categories", default=None,
                        help="comma-separated subset (default: all eight)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after writing this many labels")
    return parser.parse_args(argv)


def build_label_index(
    category: str, entries: list[dict[str, Any]]
) -> dict[str, str]:
    """uid -> vendor label, for one category.

    Keyed by uid, so unlike the slug index in fill_sports_console_uids there is
    no ambiguity to resolve: two labels cannot claim the same uid, and two uids
    with the same label are simply two sets.
    """
    index: dict[str, str] = {}
    for entry in entries:
        uid = str(entry.get("value") or "").strip()
        label = str(entry.get("label") or "").strip()
        if uid and label:
            index[uid] = label
    return index


def fetch_registry_rows(
    *, http: httpx.Client, url: str, key: str, categories: list[str],
    page_size: int,
) -> list[dict[str, Any]]:
    """Every sportscardspro row with a uid, paginated.

    Unlike the uid fill this does NOT filter on vendor_label being null: a
    vendor can rename a set twice, and a label already stored can itself be
    stale. plan_updates skips rows whose label is already correct.
    """
    if page_size >= POSTGREST_MAX_ROWS:
        raise ValueError(
            f"page size {page_size} is at or above PostgREST's "
            f"{POSTGREST_MAX_ROWS}-row cap, where a full page and a truncated "
            f"page look identical.")
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = http.get(
            f"{url}/rest/v1/pricecharting_set_registry",
            params={
                "select": "registry_id,console_uid,set_name,category,vendor_label",
                "source_site": f"eq.{SOURCE_SITE}",
                "console_uid": "not.is.null",
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
    *, limit: int | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """One update per row whose stored label differs from the vendor's.

    A uid is looked up in EVERY category's index, not just its own. The registry
    category and the vendor's are not guaranteed to agree -- G9533 sits in
    football-cards on both sides, but nothing enforces that -- and the uid is
    unique across the vendor's whole catalog, so a category mismatch would
    otherwise silently drop the label for exactly the rows most likely to be
    mislabelled.
    """
    updates: list[dict[str, str]] = []
    counts = {"matched": 0, "unchanged": 0, "notInAutocomplete": 0}
    for row in rows:
        uid = str(row.get("console_uid") or "")
        label = None
        for index in index_by_category.values():
            if uid in index:
                label = index[uid]
                break
        if not label:
            counts["notInAutocomplete"] += 1
            continue
        if (row.get("vendor_label") or "") == label:
            counts["unchanged"] += 1
            continue
        counts["matched"] += 1
        if limit is not None and len(updates) >= limit:
            continue
        updates.append({"registry_id": str(row["registry_id"]),
                        "vendor_label": label})
    return updates, counts


def write_updates(
    updates: list[dict[str, str]], *, http: httpx.Client, url: str, key: str,
) -> int:
    """PATCH one row at a time, writing vendor_label and nothing else.

    Not filtered on vendor_label being null, unlike the uid fill: a rename is
    exactly the case where an existing value must be replaced. The filter that
    does matter is source_site, so a registry_id collision cannot reach a
    pricecharting row.
    """
    written = 0
    for update in updates:
        response = http.patch(
            f"{url}/rest/v1/pricecharting_set_registry",
            params={"registry_id": f"eq.{update['registry_id']}",
                    "source_site": f"eq.{SOURCE_SITE}"},
            headers=_headers(key, **{"Content-Type": "application/json",
                                     "Prefer": "return=minimal"}),
            json={"vendor_label": update["vendor_label"]},
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
    with httpx.Client(timeout=90.0, follow_redirects=True) as http:
        for category in categories:
            index = build_label_index(
                category,
                fetch_autocomplete(category, http=http, base_url=args.base_url))
            index_by_category[category] = index
            print(f"  {category:18} {len(index):>6} labels", flush=True)

        rows = fetch_registry_rows(http=http, url=url, key=key,
                                   categories=categories,
                                   page_size=args.page_size)
        print(f"\n{len(rows)} registry rows with a console_uid", flush=True)

        updates, counts = plan_updates(rows, index_by_category, limit=args.limit)
        print(f"  label to write    {counts['matched']:>6}")
        print(f"  already correct   {counts['unchanged']:>6}")
        print(f"  not in vendor     {counts['notInAutocomplete']:>6}")

        if not updates:
            print("\nnothing to write")
            return 0
        if not args.commit:
            print(f"\ndry run: would write {len(updates)} labels. "
                  f"Pass --commit to write.")
            for update in updates[:5]:
                print(f"    {update['registry_id']} -> "
                      f"{update['vendor_label']!r}")
            return 0

        written = write_updates(updates, http=http, url=url, key=key)

    print(f"\nwrote {written} vendor labels")
    print(json.dumps({**counts, "written": written}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
