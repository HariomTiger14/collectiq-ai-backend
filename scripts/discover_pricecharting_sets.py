import argparse
import base64
import json
import os
import re
import time
from typing import Any

import httpx

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from bs4 import BeautifulSoup


REQUEST_HEADERS = {
    "User-Agent": "PackLoxSetDiscoveryBot/1.0 (+https://packlox.com; Legendary subscriber)",
}

# Lorcana, Funko Pops, and LEGO Sets have no /brand/<category>/<brand> index
# pages (unlike comic-books/coins) -- their category page lists sets
# directly. pricecharting.com's own "QUICK JUMP" search box on those pages
# is backed by this JSON endpoint, confirmed live to return every set in the
# category (not just the "popular" subset shown in the page HTML) along with
# its console_uid, so discovery skips both the brand-page crawl and the
# per-set console_uid resolve step entirely for these categories.
FLAT_CATEGORY_CONFIGS = [
    {"category": "lorcana-cards", "autocomplete_path": "/consoles-autocomplete/lorcana-cards"},
    {"category": "funko-pops", "autocomplete_path": "/consoles-autocomplete/funko-pops"},
    {"category": "lego-sets", "autocomplete_path": "/consoles-autocomplete/lego-sets"},
]

SITE_CONFIGS = {
    "pricecharting": {
        "source_site": "pricecharting",
        "base_url": "https://www.pricecharting.com",
        "seed_path": "/",
        # Video games / TCG categories already covered by the bulk CSV pipeline
        # (scripts/refresh_pricecharting_catalog.py) -- only crawl the categories
        # that pipeline doesn't reach.
        "categories": {"comic-books", "coins"},
        "flat_categories": FLAT_CATEGORY_CONFIGS,
    },
    "sportscardspro": {
        "source_site": "sportscardspro",
        "base_url": "https://www.sportscardspro.com",
        "seed_path": "/",
        # None = accept every /brand/<category>/<brand> link found; this whole
        # site is sports cards, and the set of sports is discovered dynamically.
        "categories": None,
        "flat_categories": [],
    },
}

# Coins (~68 sets) finishes almost immediately; comics (~6,200) is still
# manageable; Lorcana/Funko Pops/LEGO Sets are all small (dozens to low
# hundreds of sets) and skip the console_uid resolve step entirely, so they
# clear fast regardless of tier -- tier 1 just keeps them ahead of everything
# else.
#
# Sports cards (sportscardspro.com, ~36,000 sets across 8 dynamically-
# discovered categories) used to all share one flat default tier, which let
# whichever category happened to be inserted first (baseball) dominate every
# claim batch via tie-break order, starving the other 7 almost entirely --
# smallest-set-count-first avoids that: small categories clear fast and stop
# competing for cron slots, instead of the biggest category holding up
# everyone else indefinitely. Tiers below are ordered strictly by each
# category's real set count (confirmed live via pricecharting_set_registry).
PRIORITY_TIER_BY_CATEGORY = {
    "coins": 1,
    "lorcana-cards": 1,
    "funko-pops": 1,
    "lego-sets": 1,
    "comic-books": 2,
    "ufc-cards": 3,  # ~724 sets
    "wrestling-cards": 3,  # ~979 sets
    "racing-cards": 3,  # ~962 sets
    "soccer-cards": 4,  # ~2,982 sets
    "hockey-cards": 4,  # ~4,108 sets
    "basketball-cards": 5,  # ~6,990 sets
    "football-cards": 5,  # ~8,700 sets
    "baseball-cards": 5,  # ~10,995 sets, the largest category
}
DEFAULT_PRIORITY_TIER = 5

_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_NON_ALNUM_RE.sub("-", text.lower()).strip("-")
    return slug or "set"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sites = _selected_sites(args.sites)

    client = None
    if not args.dry_run:
        client = SupabaseRegistryClient(
            supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
            service_role_key=args.service_role_key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            timeout_seconds=args.timeout_seconds,
        )

    summaries: list[dict[str, Any]] = []
    with httpx.Client(
        timeout=args.timeout_seconds,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as http:
        for site in sites:
            summary = discover_site(
                site=site,
                http=http,
                client=client,
                dry_run=args.dry_run,
                batch_size=args.batch_size,
                sleep_seconds=args.sleep_between_requests_seconds,
            )
            summaries.append(summary)

    print(
        dump_and_report(
            {
                "success": True,
                "dryRun": args.dry_run,
                "sites": summaries,
                "brandPagesCrawled": sum(s["brandPages"] for s in summaries),
                "setsFound": sum(s["setsFound"] for s in summaries),
                "setsInserted": sum(s["setsInserted"] for s in summaries),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


def discover_site(
    *,
    site: dict[str, Any],
    http: httpx.Client,
    client: "SupabaseRegistryClient | None",
    dry_run: bool,
    batch_size: int,
    sleep_seconds: float,
) -> dict[str, Any]:
    source_site = site["source_site"]
    print(f"Discovering brand pages for {source_site}...", flush=True)
    seed_url = site["base_url"] + site["seed_path"]
    seed_html = _get_text(http, seed_url)
    brand_links = parse_brand_links(seed_html, allowed_categories=site["categories"])
    print(f"Found {len(brand_links)} brand pages on {source_site}.", flush=True)

    rows: list[dict[str, Any]] = []
    for index, brand in enumerate(brand_links):
        if sleep_seconds > 0 and index > 0:
            time.sleep(sleep_seconds)
        brand_url = site["base_url"] + brand["path"]
        try:
            brand_html = _get_text(http, brand_url)
        except httpx.HTTPError as exc:
            print(f"  Skipping {brand_url}: {exc}", flush=True)
            continue

        set_links = parse_set_links(brand_html, category=brand["category"])
        print(
            f"  {brand['category']}/{brand['brand']}: {len(set_links)} sets",
            flush=True,
        )
        for set_link in set_links:
            rows.append(
                build_registry_row(
                    source_site=source_site,
                    category=brand["category"],
                    brand=brand["brand"],
                    slug=set_link["slug"],
                    set_name=set_link["set_name"],
                    base_url=site["base_url"],
                )
            )

    flat_categories = site.get("flat_categories") or []
    for index, flat in enumerate(flat_categories):
        if sleep_seconds > 0 and (brand_links or index > 0):
            time.sleep(sleep_seconds)
        flat_rows = discover_flat_category(
            flat,
            http=http,
            base_url=site["base_url"],
            source_site=source_site,
        )
        print(
            f"  {flat['category']}: {len(flat_rows)} sets (via consoles-autocomplete)",
            flush=True,
        )
        rows.extend(flat_rows)

    inserted = 0
    if not dry_run and rows:
        assert client is not None
        inserted = client.insert_new_rows(rows, batch_size=batch_size)
    return {
        "sourceSite": source_site,
        "brandPages": len(brand_links),
        "flatCategories": len(flat_categories),
        "setsFound": len(rows),
        "setsInserted": inserted,
    }


def discover_flat_category(
    flat: dict[str, str],
    *,
    http: httpx.Client,
    base_url: str,
    source_site: str,
) -> list[dict[str, Any]]:
    """Discover every set in a category whose page lists sets directly
    (no /brand/<category>/<brand> indirection), via the same JSON endpoint
    that powers pricecharting.com's own "QUICK JUMP" set search."""
    category = flat["category"]
    url = base_url + flat["autocomplete_path"]
    try:
        response = http.get(url)
        response.raise_for_status()
        entries = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  Skipping flat category {category}: {exc}", flush=True)
        return []

    rows: list[dict[str, Any]] = []
    for entry in entries:
        console_uid = (entry or {}).get("value") or ""
        set_name = (entry or {}).get("label") or ""
        if not console_uid or not set_name:
            continue
        rows.append(
            build_flat_registry_row(
                source_site=source_site,
                category=category,
                set_name=set_name,
                console_uid=console_uid,
                base_url=base_url,
            )
        )
    return rows


def build_flat_registry_row(
    *,
    source_site: str,
    category: str,
    set_name: str,
    console_uid: str,
    base_url: str,
) -> dict[str, Any]:
    slug = f"{category}-{_slugify(set_name)}"
    return {
        "source_site": source_site,
        "category": category,
        "brand": category,
        "slug": slug,
        "set_name": set_name,
        "url": f"{base_url}/console/{slug}",
        "console_uid": console_uid,
        "priority_tier": PRIORITY_TIER_BY_CATEGORY.get(category, DEFAULT_PRIORITY_TIER),
    }


def parse_brand_links(
    html: str,
    *,
    allowed_categories: set[str] | None,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[tuple[str, str], str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        parts = href.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "brand":
            continue
        category, brand = parts[1], parts[2]
        if allowed_categories is not None and category not in allowed_categories:
            continue
        found[(category, brand)] = f"/brand/{category}/{brand}"
    return [
        {"category": category, "brand": brand, "path": path}
        for (category, brand), path in found.items()
    ]


def parse_set_links(html: str, *, category: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    prefix = f"{category}-"
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith("/console/"):
            continue
        slug = href[len("/console/") :]
        if not slug.startswith(prefix):
            continue
        set_name = anchor.get_text(strip=True)
        if not set_name:
            continue
        found[slug] = set_name
    return [{"slug": slug, "set_name": set_name} for slug, set_name in found.items()]


def build_registry_row(
    *,
    source_site: str,
    category: str,
    brand: str,
    slug: str,
    set_name: str,
    base_url: str,
) -> dict[str, Any]:
    return {
        "source_site": source_site,
        "category": category,
        "brand": brand,
        "slug": slug,
        "set_name": set_name,
        "url": f"{base_url}/console/{slug}",
        # Always present (even if null) so every row in a batch shares the
        # same key set -- PostgREST's bulk insert rejects a batch where
        # objects have different keys ("All object keys must match", seen
        # live when a batch straddled these brand-crawled rows and the
        # flat-category rows below, which do set console_uid).
        "console_uid": None,
        "priority_tier": PRIORITY_TIER_BY_CATEGORY.get(category, DEFAULT_PRIORITY_TIER),
    }


def _get_text(http: httpx.Client, url: str) -> str:
    response = http.get(url)
    response.raise_for_status()
    return response.text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover PriceCharting/SportsCardsPro set pages (comics, coins, "
            "sports cards) and register new ones in pricecharting_set_registry."
        )
    )
    parser.add_argument(
        "--sites",
        default=",".join(SITE_CONFIGS),
        help="Comma-separated site keys to crawl.",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument(
        "--sleep-between-requests-seconds",
        type=float,
        default=1.0,
        help="Politeness delay between brand-page fetches (plain page views, not the rate-limited CSV/API endpoints).",
    )
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key",
        default="",
        help="Defaults to SUPABASE_SERVICE_ROLE_KEY.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _selected_sites(raw_sites: str) -> list[dict[str, Any]]:
    selected = [site.strip() for site in raw_sites.split(",") if site.strip()]
    if not selected:
        raise SystemExit("At least one site is required.")
    unsupported = [site for site in selected if site not in SITE_CONFIGS]
    if unsupported:
        allowed = ", ".join(sorted(SITE_CONFIGS))
        raise SystemExit(
            f"Unsupported site(s): {', '.join(unsupported)}. Use one of: {allowed}."
        )
    return [SITE_CONFIGS[site] for site in selected]


class SupabaseRegistryClient:
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
                f"for registry writes, but the configured key has role '{role}'."
            )

    def insert_new_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        inserted = 0
        headers = {
            **self._headers(),
            "Prefer": "resolution=ignore-duplicates,return=representation",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"on_conflict": "source_site,slug"},
                    headers=headers,
                    json=batch,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SystemExit(
                        "Supabase set registry insert failed "
                        f"at rows {index + 1}-{index + len(batch)} "
                        f"with HTTP {response.status_code}: {response.text}"
                    ) from exc
                payload = response.json()
                new_count = len(payload) if isinstance(payload, list) else 0
                inserted += new_count
                print(
                    f"Inserted {inserted} newly discovered sets so far "
                    f"(checked {index + len(batch)} / {len(rows)})...",
                    flush=True,
                )
        return inserted

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
    raise SystemExit(run_with_recorder("pricecharting-sets-discover", main))
