"""Ingest the visually-cleared coin image set from a manifest.

Why this exists separately from import_coin_images.py: that script SEARCHES
Commons and picks automatically, which is how we discovered the set. Automated
picking is not safe to ship on its own -- reviewing the candidates by eye caught
design *drawings* filed as coin photographs, composites with captions baked in,
a face labelled obverse that was actually a reverse, and bullion reverses whose
legend names the wrong denomination. None of those are detectable from licence
metadata.

So the human decision lives in database/seeds/coin_images_manifest.json (series
-> view -> Commons file title) and this script only executes it. Licence,
artist and source page are read LIVE from Commons at ingest time rather than
copied into the manifest, so the provenance we store can never drift from the
source it claims.

Attribution is derived, not hand-entered: CC BY / CC BY-SA images get
attribution_required=true plus the text and URL the app must display. A licence
that requires attribution and no text to show is rejected by a database
constraint rather than shipped quietly.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "PackLoxCatalogBuilder/1.0 (https://packlox.com; dev@packlox.com)"
BUCKET = "catalog-images"
MANIFEST = Path(__file__).resolve().parents[1] / "database/seeds/coin_images_manifest.json"

# Licences whose terms require the author to be credited wherever the work is
# shown. Public-domain and CC0 images carry credit for provenance, but showing
# it is our choice rather than a condition of use.
ATTRIBUTION_LICENCES = re.compile(r"^cc[ -]by", re.I)

# Provenances we refuse regardless of what tag Commons carries: a grading
# service or auction house asserts rights over its own photography, and a
# Commons uploader's "public domain" claim usually reflects the design being
# PD, which says nothing about the photograph.
BANNED_SOURCE = re.compile(
    r"grading service|numismatic guaranty|heritage|stack'?s|great collections|legend rare",
    re.I,
)


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def series_key(series: str) -> str:
    """"$5 American Gold Eagle" -> "5 american gold eagle", matching the key
    catalog_search_service.py derives from PriceCharting's console_name."""
    return " ".join(re.split(r"[^a-z0-9]+", series.lower())).strip()


class Commons:
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    def describe(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(titles), 40):
            batch = titles[start : start + 40]
            response = self.client.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "titles": "|".join(f"File:{t}" for t in batch),
                    "prop": "imageinfo",
                    "iiprop": "extmetadata|size|url",
                    "format": "json",
                },
            )
            response.raise_for_status()
            for page in (response.json().get("query") or {}).get("pages", {}).values():
                if "missing" in page or not page.get("imageinfo"):
                    continue
                info = page["imageinfo"][0]
                extra = info.get("extmetadata") or {}

                def field(key: str) -> str:
                    return strip_html((extra.get(key) or {}).get("value", ""))

                out[page["title"][5:]] = {
                    "url": info.get("url"),
                    "page": info.get("descriptionurl"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "licence": field("LicenseShortName"),
                    "artist": field("Artist"),
                    "credit": field("Credit"),
                }
            time.sleep(0.2)
        return out


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        base = url.rstrip("/")
        self.public_prefix = f"{base}/storage/v1/object/public/{BUCKET}/"
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.rest = httpx.Client(base_url=f"{base}/rest/v1", headers=headers, timeout=30)
        self.storage = httpx.Client(
            base_url=f"{base}/storage/v1", headers=headers, timeout=90
        )
        self.fetcher = httpx.Client(
            timeout=90, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    def mirror(self, source_url: str, object_key: str) -> str | None:
        """Copy the file into our own bucket. We serve our copy, never the
        original: it removes the hotlink-etiquette question and any runtime
        dependency on Wikimedia's CDN."""
        public_url = self.public_prefix + object_key
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
        if response.status_code not in (200, 201, 204):
            print(f"    upsert failed {response.status_code}: {response.text[:160]}")
            return False
        return True

    def delete(self, key: str, view: str) -> bool:
        response = self.rest.delete(
            "/coin_catalog_images",
            params={"series_key": f"eq.{key}", "view": f"eq.{view}"},
            headers={"Prefer": "return=minimal"},
        )
        return response.status_code in (200, 204)


def build_row(series: str, view: str, title: str, meta: dict[str, Any],
              image_url: str) -> dict[str, Any]:
    licence = meta["licence"] or ""
    artist = meta["artist"] or meta["credit"] or ""
    required = bool(ATTRIBUTION_LICENCES.match(licence))
    attribution_text = None
    if required:
        # What the app renders. Author first because that is what the licence
        # actually obliges us to name; the licence itself follows so a reader
        # can see the terms the reuse relies on.
        attribution_text = f"{artist or 'Unknown author'} / {licence}"
    return {
        "series_key": series_key(series),
        "design_key": "",
        "view": view,
        "image_url": image_url,
        "source_url": meta["url"],
        "source_page": meta["page"],
        "license": licence,
        "credit": artist or None,
        "attribution_required": required,
        "attribution_text": attribution_text,
        "attribution_url": meta["page"] if required else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not args.dry_run and not (url and key):
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")
        return 2

    manifest = json.loads(MANIFEST.read_text())["images"]
    wanted = [(s, v, t) for s, views in manifest.items() for v, t in sorted(views.items())]
    print(f"manifest: {len(manifest)} series, {len(wanted)} images")

    meta = Commons().describe([t for _, _, t in wanted])
    missing = [t for _, _, t in wanted if t not in meta]
    if missing:
        print(f"ABORT: {len(missing)} manifest files not found on Commons:")
        for t in missing[:10]:
            print("   ", t)
        return 1

    # Re-check provenance at ingest time. The manifest was reviewed, but the
    # check is cheap and a file's metadata can change after review.
    banned = [
        (s, v, t) for s, v, t in wanted
        if BANNED_SOURCE.search(f'{meta[t]["artist"]} {meta[t]["credit"]}')
    ]
    if banned:
        print(f"ABORT: {len(banned)} manifest files have banned provenance:")
        for s, v, t in banned:
            print(f"    {s} / {v}: {t}")
        return 1

    supabase = None if args.dry_run else Supabase(url, key)
    written = skipped = 0
    attribution_rows: list[dict[str, Any]] = []
    for series, view, title in wanted:
        info = meta[title]
        extension = (title.rsplit(".", 1)[-1] or "jpg").lower()
        object_key = f"coins/{series_key(series).replace(' ', '-')}-{view}.{extension}"
        if args.dry_run:
            image_url = supabase_url = f"(dry-run)/{object_key}"
        else:
            image_url = supabase.mirror(info["url"], object_key)
            if not image_url:
                print(f"  MIRROR FAILED {series} / {view}")
                skipped += 1
                continue
        row = build_row(series, view, title, info, image_url)
        if row["attribution_required"]:
            attribution_rows.append(row)
        if args.dry_run or supabase.upsert(row):
            written += 1
            print(f"  ok {series:32s} {view:8s} {info['width']}x{info['height']} "
                  f"{row['license']}")
        else:
            skipped += 1

    print(f"\nwritten={written} skipped={skipped}")
    print(f"attribution-required rows: {len(attribution_rows)}")
    for row in attribution_rows:
        print(f"   {row['series_key']} / {row['view']}: {row['attribution_text']}")
    return 0 if not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
