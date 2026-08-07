import argparse
import base64
import json
import os
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup


REQUEST_HEADERS = {
    "User-Agent": "PackLoxSetDiscoveryBot/1.0 (+https://packlox.com; Legendary subscriber)",
}

SITE_CONFIGS = {
    "pricecharting": {
        "source_site": "pricecharting",
        "base_url": "https://www.pricecharting.com",
        "seed_path": "/",
        # Video games / TCG categories already covered by the bulk CSV pipeline
        # (scripts/refresh_pricecharting_catalog.py) -- only crawl the categories
        # that pipeline doesn't reach.
        "categories": {"comic-books", "coins"},
    },
    "sportscardspro": {
        "source_site": "sportscardspro",
        "base_url": "https://www.sportscardspro.com",
        "seed_path": "/",
        # None = accept every /brand/<category>/<brand> link found; this whole
        # site is sports cards, and the set of sports is discovered dynamically.
        "categories": None,
    },
}


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
        json.dumps(
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

    inserted = 0
    if not dry_run and rows:
        assert client is not None
        inserted = client.insert_new_rows(rows, batch_size=batch_size)
    return {
        "sourceSite": source_site,
        "brandPages": len(brand_links),
        "setsFound": len(rows),
        "setsInserted": inserted,
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
    raise SystemExit(main())
