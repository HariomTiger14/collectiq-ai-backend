"""Curate coin images from Wikimedia Commons into coin_catalog_images.

Why Commons: US coin DESIGNS are federal government works, so they are
public domain -- unlike card/comic art there is no rights-holder to
licence from, and PD files may be copied and re-hosted outright. Files
are therefore mirrored into our own catalog-images bucket (same bucket
and pattern as scripts/rehost_yugioh_images.py), which removes any
hotlink-etiquette question and any dependency on Wikimedia's CDN.

Scope: coins need DESIGN-level images, not year+mint-mark ones (a 1970-D
and 1970-S Roosevelt dime are the same picture), so this imports two
views per series -- obverse and reverse -- for the 68 series in
pricecharting_set_registry, plus per-design entries for multi-design
series where SERIES_DESIGNS lists them.

Selection is conservative and auditable rather than clever:
  * only files whose licence is PD or a CC variant are eligible, and the
    licence + credit are stored per image (some Commons files are
    CC BY-SA, where attribution is a condition of use);
  * a candidate must match the series words AND the view word in its
    title, so "1 Dime (United States).jpg" cannot be picked as a
    Mercury-dime reverse;
  * everything picked is printed with its licence for review, and
    --dry-run stops before any download so the picks can be checked
    first.

Re-runnable: existing (series, design, view) rows are skipped unless
--refresh is passed, and bucket objects are never re-downloaded.

Usage:
  python3 scripts/import_coin_images.py --dry-run
  python3 scripts/import_coin_images.py [--series "Mercury Dime"] [--limit N]
"""

import argparse
import os
import re
import sys
import time
import urllib.parse
from typing import Any

import httpx

BUCKET = "catalog-images"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia requires a descriptive User-Agent identifying the app and a
# contact; anonymous/library-default agents are rejected.
USER_AGENT = "PackLoxCatalogBuilder/1.0 (https://packlox.com; dev@packlox.com)"
REQUEST_DELAY_SECONDS = 0.4
# Licence short-names we accept. Anything else (unknown, fair-use,
# non-commercial) is rejected rather than guessed at.
ACCEPTABLE_LICENCE_RE = re.compile(
    r"(public domain|^pd|cc0|cc by(?!.*nc)|cc-by(?!.*nc))", re.I
)
VIEWS = ("obverse", "reverse")

# Quality gate. Commons' coin files split cleanly by who took the photo:
# the US Mint, the grading services and the big auction houses shoot
# coins straight-on against a plain background, while individual
# contributors upload snapshots -- one otherwise-valid pick was a hand
# holding the coin in a cardboard flip with "CONGRATS!" written on it.
# Nothing about the title, licence or dimensions distinguishes those, but
# the Artist/Credit metadata does, so only photographs from these
# sources are eligible. A series with no trusted photo keeps its
# placeholder, which is better than a snapshot.
TRUSTED_PHOTOGRAPHER_RE = re.compile(
    r"(united states mint|u\.?\s?s\.?\s?mint|professional coin grading|pcgs"
    r"|numismatic guaranty|ngc|heritage auction|national numismatic collection"
    r"|smithsonian)",
    re.I,
)

# Series whose reverse changes per design: each design needs its own
# image pair. Keys are normalized series names.
SERIES_DESIGNS: dict[str, list[str]] = {
    # Seeded with the two design series most represented in our
    # catalogue; extend as coverage is reviewed.
    "state quarter": [],
    "america the beautiful quarter": [],
}


def normalize_key(value: str) -> str:
    return " ".join(re.split(r"[^a-z0-9]+", (value or "").lower())).strip()


class Commons:
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    def search_files(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 900,
            "format": "json",
        }
        try:
            response = self.client.get(COMMONS_API, params=params)
            response.raise_for_status()
            pages = (response.json().get("query") or {}).get("pages") or {}
        except Exception:
            return []
        finally:
            time.sleep(REQUEST_DELAY_SECONDS)
        return list(pages.values())


def licence_of(page: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(licence, full_credit, display_credit).

    full_credit merges Artist and Credit metadata (the photographer
    appears in only one of them for many files) and is what the
    photographer gate matches against; display_credit is the short form
    stored for the UI caption.
    """
    info = (page.get("imageinfo") or [{}])[0]
    meta = info.get("extmetadata") or {}
    licence = (meta.get("LicenseShortName") or {}).get("value")
    parts = [
        re.sub(r"<[^>]+>", " ", (meta.get(field) or {}).get("value", "") or "")
        for field in ("Artist", "Credit")
    ]
    credit = " ".join(" ".join(parts).split())
    # The photographer gate matches against the full text, but what gets
    # DISPLAYED must fit a one-line caption under the image. Commons
    # credits ramble ("X for the photograph; Y for the coin design. US
    # Mint This file was derived from: ...jpg"), so keep the first
    # clause only.
    display_credit = re.split(r"[;.]|\bThis file\b", credit)[0].strip()
    display_credit = re.sub(r"\s*https?://\S+", "", display_credit).strip(" ,-")
    return licence, (credit or None), (display_credit[:60] or None)


# Commons is worldwide, so a naive title match happily returns another
# country's coin of the same denomination. Every one of these was an
# actual wrong pick in the first dry run (Swiss commemoratives, a
# Liberian two-cent, an Iron Age British quarter-stater).
FOREIGN_MARKERS = (
    "swiss", "switzerland", "liberia", "liberian", "canada", "canadian",
    "british", "britain", "iron age", "roman", "greek", "china", "chinese",
    "japan", "japanese", "india", "indian rupee", "australia", "australian",
    "mexico", "mexican", "france", "french", "germany", "german", "russia",
    "russian", "spain", "spanish", "philippine", "hawaii", "confederate",
    "julius caesar", "dobunni", "stater",
)
# "Penny" is the collector name; Commons files say "cent".
DENOMINATION_SYNONYMS = {
    "penny": ("penny", "cent"),
    "cent": ("cent", "penny"),
    "dime": ("dime",),
    "quarter": ("quarter",),
    "nickel": ("nickel", "five cent", "5 cent"),
    "dollar": ("dollar",),
}
# Series whose names collide with a different denomination: a "Seated
# Liberty Dime" search happily returns a "seated liberty half dime", and
# a "Half Dollar" series must not match a plain "dollar" file.
DENOMINATION_EXCLUSIONS = {
    "dime": ("half dime",),
    "dollar": ("half dollar", "quarter dollar", "dollar bill"),
}


def pick_candidate(
    pages: list[dict[str, Any]], series: str, view: str
) -> dict[str, Any] | None:
    """Conservative: reject unless the title is unambiguously this exact
    US series and denomination. A miss is fine (placeholder); a wrong
    coin shown as the user's coin is not."""
    series_norm = normalize_key(series)
    series_words = [w for w in series_norm.split() if len(w) > 2]
    is_half = "half" in series_norm
    denomination = next(
        (d for d in DENOMINATION_SYNONYMS if d in series_norm), None
    )
    best = None
    best_score = 0
    for page in pages:
        title = normalize_key(page.get("title", ""))
        info = (page.get("imageinfo") or [{}])[0]
        if not info.get("url"):
            continue
        licence, credit, _ = licence_of(page)
        if not licence or not ACCEPTABLE_LICENCE_RE.search(licence):
            continue
        # Quality gate -- see TRUSTED_PHOTOGRAPHER_RE.
        if not credit or not TRUSTED_PHOTOGRAPHER_RE.search(credit):
            continue
        if view not in title:
            continue
        if any(marker in title for marker in FOREIGN_MARKERS):
            continue
        # Rules below come from visually reviewing every candidate this
        # picker produced (2026-08-30). Each rejected a real pick:
        #   * "Which side of shield nickel is obverse? (IA ...)" -- an
        #     1894 typed LETTER to the Mint Director, scanned by the
        #     Internet Archive, offered as a Shield Nickel obverse;
        #   * "2000 Washington Sacagawea dollar mule obverse" -- the
        #     famous mule error, whose obverse is a WASHINGTON QUARTER
        #     ("QUARTER DOLLAR" reads across it), offered as Sacagawea;
        #   * "1925 Medal Norse ... commemorative" -- an octagonal
        #     medal, not the US commemorative coins the series means.
        # A miss leaves a placeholder; these would have shown users the
        # wrong object entirely.
        # Checked against the RAW title, not the normalized one:
        # normalize_key() strips punctuation, so "(IA identifier)" -- the
        # Internet Archive's marker -- loses its bracket and stops being
        # distinctive. Extension check matters for the same file: the
        # 1894 letter is a .pdf, and a document is never a coin photo.
        raw_title = str(page.get("title", ""))
        if "(IA " in raw_title or "Internet Archive" in raw_title:
            continue
        if raw_title.lower().rsplit(".", 1)[-1] in {"pdf", "djvu", "svg", "webm", "ogv"}:
            continue
        if "medal" in title and "medal" not in series_norm:
            continue
        if "mule" in title:
            continue
        # Sub-400px files exist for several series and look like postage
        # stamps at detail size (a 216px 1976 Bicentennial quarter was
        # offered for the State Quarter series).
        if int(info.get("width") or 0) < 400 or int(info.get("height") or 0) < 400:
            continue
        # Composites: Commons has plenty of files showing several coins
        # side by side, or both faces in one image. Caught live --
        # "Commemorative Washington quarter obverses.png" is two coins
        # next to each other, useless as this coin's photo. Plural titles
        # and non-square proportions both give them away, since a single
        # round coin photographs roughly 1:1.
        if "obverses" in title or "reverses" in title:
            continue
        if "obverse and reverse" in title or "both sides" in title:
            continue
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        if width and height:
            ratio = width / height
            if ratio < 0.65 or ratio > 1.5:
                continue
        if denomination:
            # The denomination (or an accepted synonym) must be present...
            if not any(
                synonym in title for synonym in DENOMINATION_SYNONYMS[denomination]
            ):
                continue
            # ...and must not be a neighbouring denomination.
            if any(
                bad in title for bad in DENOMINATION_EXCLUSIONS.get(denomination, ())
            ):
                if not is_half:
                    continue
            # "Half" is part of the identity in both directions.
            if is_half and "half" not in title:
                continue
            if not is_half and "half " + denomination in title:
                continue
        # Every distinctive series word must be present -- "Franklin Half
        # Dollar" must not match "Benjamin Franklin ... Silver Dollar".
        if any(word not in title for word in series_words):
            continue
        score = int(info.get("width") or 0)
        if score > best_score:
            best, best_score = page, score
    return best


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        self.public_prefix = f"{url.rstrip('/')}/storage/v1/object/public/{BUCKET}/"
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.rest = httpx.Client(
            base_url=f"{url.rstrip('/')}/rest/v1", headers=headers, timeout=30
        )
        self.storage = httpx.Client(
            base_url=f"{url.rstrip('/')}/storage/v1", headers=headers, timeout=60
        )
        self.fetcher = httpx.Client(
            timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    def existing_keys(self) -> set[tuple[str, str, str]]:
        response = self.rest.get(
            "/coin_catalog_images",
            params={"select": "series_key,design_key,view", "limit": "5000"},
        )
        if response.status_code != 200:
            return set()
        return {
            (row["series_key"], row.get("design_key") or "", row["view"])
            for row in response.json()
        }

    def mirror(self, source_url: str, object_key: str) -> str | None:
        public_url = self.public_prefix + object_key
        if self.fetcher.head(public_url).status_code == 200:
            return public_url
        try:
            image = self.fetcher.get(source_url)
        except httpx.HTTPError:
            return None
        if image.status_code != 200 or not image.content:
            return None
        upload = self.storage.post(
            f"/object/{BUCKET}/{object_key}",
            content=image.content,
            headers={
                "Content-Type": image.headers.get("content-type", "image/jpeg"),
                "x-upsert": "true",
            },
        )
        return public_url if upload.status_code in (200, 201) else None

    def upsert(self, row: dict[str, Any]) -> bool:
        response = self.rest.post(
            "/coin_catalog_images",
            params={"on_conflict": "series_key,design_key,view"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=row,
        )
        return response.status_code in (200, 201, 204)

    def coin_series(self) -> list[str]:
        response = self.rest.get(
            "/pricecharting_set_registry",
            params={
                "select": "set_name",
                "category": "eq.coins",
                "order": "set_name.asc",
                "limit": "500",
            },
        )
        response.raise_for_status()
        return [row["set_name"] for row in response.json() if row.get("set_name")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report picks without downloading or writing.")
    parser.add_argument("--series", default="", help="Only this series.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--refresh", action="store_true",
                        help="Re-pick series that already have images.")
    args = parser.parse_args(argv)

    supabase_url = os.getenv("SUPABASE_URL", "")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    supabase = Supabase(supabase_url, service_key)
    commons = Commons()

    series_list = supabase.coin_series()
    if args.series:
        series_list = [s for s in series_list if normalize_key(s) == normalize_key(args.series)]
    if args.limit:
        series_list = series_list[: args.limit]
    existing = set() if args.refresh else supabase.existing_keys()

    print(f"[coins] {len(series_list)} series to process", flush=True)
    found = missing = written = 0
    for series in series_list:
        series_key = normalize_key(series)
        for view in VIEWS:
            if (series_key, "", view) in existing:
                continue
            # Query variants, strictest context first: the US qualifier
            # biases Commons' worldwide index toward the right country,
            # and "coin" excludes stamps/banknotes/medals of the same
            # name. Each result set still passes the same strict filter.
            page = None
            for query in (
                f"United States {series} {view} coin",
                f"{series} {view} coin United States",
                f"{series} {view} coin",
                f"{series} {view}",
            ):
                page = pick_candidate(commons.search_files(query, limit=15), series, view)
                if page is not None:
                    break
            if page is None:
                missing += 1
                print(f"  MISS  {series:34s} {view}", flush=True)
                continue
            info = (page.get("imageinfo") or [{}])[0]
            licence, _full_credit, display_credit = licence_of(page)
            found += 1
            print(
                f"  PICK  {series:34s} {view:8s} {licence!r:22s} "
                f"{info.get('width')}x{info.get('height')} {page['title'][:52]}",
                flush=True,
            )
            if args.dry_run:
                continue
            extension = os.path.splitext(urllib.parse.urlparse(info["url"]).path)[1] or ".jpg"
            object_key = f"coins/{series_key.replace(' ', '-')}-{view}{extension}"
            public_url = supabase.mirror(info["url"], object_key)
            if not public_url:
                print(f"  FAIL  mirror {series} {view}", flush=True)
                continue
            ok = supabase.upsert({
                "series_key": series_key,
                "design_key": "",
                "view": view,
                "image_url": public_url,
                "source_url": info["url"],
                "source_page": info.get("descriptionurl"),
                "license": licence,
                # Short form only -- this renders as a caption under the
                # image, not as an archive record.
                "credit": display_credit,
            })
            written += int(ok)
    print(
        f"[coins] picked {found}, missing {missing}, written {written}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
