"""Re-host Yu-Gi-Oh card images into our own Supabase Storage bucket.

Why: YGOPRODeck's API guide states verbatim "Do not continually hotlink
images directly from this site. Please download and re-host the images
yourself. Failure to do so will result in an IP blacklist." (verified
live 2026-08-30). yugioh_catalog rows hotlinked images.ygoprodeck.com
(36,930 rows at migration time) and tcgplayer-cdn.tcgplayer.com (1,635
rows, the TCGCSV fallback source) -- this script mirrors BOTH into the
public `catalog-images` bucket and rewrites image_url to our copy,
preserving the original in source_image_url (see
database/migrations/20260830_add_yugioh_source_image_url.sql).

Politeness: their other rule is "only pull an image once and then store
it locally" and no high per-second volume. So: rows are deduped by
source URL before downloading (reprints share one image across many
set_codes), downloads are globally throttled (~5/sec), and re-runs skip
any source URL whose object already exists in the bucket -- crash-safe
resumability without ever re-pulling an image.

Run AFTER the catalog imports (import_tcgcsv_yugioh_catalog.py then
import_ygoprodeck_catalog.py): new rows arrive with provider URLs and a
re-run mirrors only those.

Usage:
  python3 scripts/rehost_yugioh_images.py --env-file ../collectiq-ai-backend/.env [--limit N] [--dry-run]
"""

import argparse
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

BUCKET = "catalog-images"
DOWNLOADS_PER_SECOND = 5.0
WORKERS = 6
PAGE_SIZE = 1000


def load_env(path: Path) -> dict[str, str]:
    # Parsed by hand instead of shell-sourcing: the file's DATABASE_URL
    # contains shell-hostile characters.
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class RateLimiter:
    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wake_at = max(now, self._next_at)
            self._next_at = wake_at + self._interval
        delay = wake_at - now
        if delay > 0:
            time.sleep(delay)


def object_key(source_url: str) -> str:
    parts = urlsplit(source_url)
    name = parts.path.rsplit("/", 1)[-1]
    host_tag = "ygo" if "ygoprodeck" in parts.netloc else "tcg"
    return f"yugioh/{host_tag}/{name}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="../collectiq-ai-backend/.env")
    parser.add_argument("--limit", type=int, default=0, help="mirror at most N distinct images (smoke test)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    env = load_env(Path(args.env_file).expanduser())
    supabase_url = env["SUPABASE_URL"].rstrip("/")
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    public_prefix = f"{supabase_url}/storage/v1/object/public/{BUCKET}/"

    rest = httpx.Client(base_url=f"{supabase_url}/rest/v1", headers=headers, timeout=30)
    storage = httpx.Client(base_url=f"{supabase_url}/storage/v1", headers=headers, timeout=60)
    fetcher = httpx.Client(timeout=30, follow_redirects=True)

    # 1. Collect every row still pointing at a provider URL.
    rows_by_source: dict[str, int] = defaultdict(int)
    offset = 0
    while True:
        response = rest.get(
            "/yugioh_catalog",
            params={
                "select": "set_code,image_url",
                "image_url": f"not.like.{public_prefix}*",
                "order": "set_code.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
        )
        response.raise_for_status()
        page = response.json()
        for row in page:
            url = (row.get("image_url") or "").strip()
            if url:
                rows_by_source[url] += 1
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    sources = sorted(rows_by_source)
    if args.limit:
        sources = sources[: args.limit]
    total_rows = sum(rows_by_source[s] for s in sources)
    print(f"[rehost] {len(sources)} distinct images across {total_rows} rows to mirror")
    if args.dry_run:
        return 0

    limiter = RateLimiter(DOWNLOADS_PER_SECOND)
    done = 0
    failed: list[str] = []
    lock = threading.Lock()

    def mirror(source_url: str) -> tuple[str, str | None]:
        key_path = object_key(source_url)
        public_url = public_prefix + key_path
        # Resumability: if the object already exists we never re-pull the
        # image (their "only pull an image once" rule) -- just re-point rows.
        head = fetcher.head(public_url)
        if head.status_code != 200:
            limiter.wait()
            for attempt in range(3):
                try:
                    image = fetcher.get(source_url)
                    if image.status_code == 200 and image.content:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(2 * (attempt + 1))
            else:
                return source_url, None
            if image.status_code != 200 or not image.content:
                return source_url, None
            upload = storage.post(
                f"/object/{BUCKET}/{key_path}",
                content=image.content,
                headers={
                    "Content-Type": image.headers.get("content-type", "image/jpeg"),
                    "x-upsert": "true",
                },
            )
            if upload.status_code not in (200, 201):
                return source_url, None
        patch = rest.patch(
            "/yugioh_catalog",
            params={"image_url": f"eq.{source_url}"},
            headers={"Prefer": "return=minimal"},
            json={"image_url": public_url, "source_image_url": source_url},
        )
        if patch.status_code not in (200, 204):
            return source_url, None
        return source_url, public_url

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(mirror, s): s for s in sources}
        for future in as_completed(futures):
            source_url, public_url = future.result()
            with lock:
                if public_url is None:
                    failed.append(source_url)
                done += 1
                if done % 200 == 0 or done == len(sources):
                    print(f"[rehost] {done}/{len(sources)} done, {len(failed)} failed", flush=True)

    if failed:
        print(f"[rehost] FAILED sources ({len(failed)}):")
        for url in failed[:20]:
            print(f"  {url}")
        return 1
    print("[rehost] complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
