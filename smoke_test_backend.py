"""Smoke test a running CollectIQ backend.

The image upload check is optional. Pass --image PATH or add a local image to
one of the known sample locations. No secrets are required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_IMAGE_CANDIDATES = [
    Path("test/fixtures/persistent-gallery-card.jpg"),
    Path("test/fixtures/persistent-camera-card.jpg"),
    Path("validation/images/local_sample/sample.jpg"),
    Path("validation/images/local_sample/sample.png"),
]


def _content_type(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if extension == ".png":
        return "image/png"
    return "application/octet-stream"


def _find_image(explicit_path: str | None) -> Path | None:
    if explicit_path:
        candidate = Path(explicit_path)
        return candidate if candidate.exists() else None

    for candidate in DEFAULT_IMAGE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def check_health(client: httpx.Client, base_url: str) -> bool:
    response = client.get(f"{base_url}/health")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    required = {"status", "environment", "ai_provider", "pricing_provider", "version"}
    missing = sorted(required.difference(payload))
    if missing:
        print(f"[FAIL] /health missing fields: {', '.join(missing)}")
        return False
    print(
        "[PASS] /health "
        f"status={payload['status']} env={payload['environment']} "
        f"ai={payload['ai_provider']} pricing={payload['pricing_provider']} "
        f"version={payload['version']}"
    )
    return True


def check_scanner_analyze(
    client: httpx.Client,
    base_url: str,
    image_path: Path | None,
) -> bool:
    if image_path is None:
        print("[SKIP] /scanner/analyze image test: no sample image found.")
        print("       Pass --image PATH to test image upload explicitly.")
        return True

    with image_path.open("rb") as image_file:
        response = client.post(
            f"{base_url}/scanner/analyze",
            files={
                "image": (
                    image_path.name,
                    image_file,
                    _content_type(image_path),
                )
            },
        )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    required = {
        "success",
        "title",
        "category",
        "manufacturer",
        "series",
        "year",
        "country",
        "estimated_value_low",
        "estimated_value_high",
        "confidence",
        "description",
        "notes",
    }
    missing = sorted(required.difference(payload))
    if missing:
        print(f"[FAIL] /scanner/analyze missing fields: {', '.join(missing)}")
        return False
    print(
        "[PASS] /scanner/analyze "
        f"title={payload['title']} category={payload['category']} "
        f"confidence={payload['confidence']}"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test CollectIQ backend.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--image", help="Optional jpg/png image for /scanner/analyze")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    image_path = _find_image(args.image)

    try:
        with httpx.Client(timeout=20) as client:
            health_ok = check_health(client, base_url)
            scanner_ok = check_scanner_analyze(client, base_url, image_path)
    except httpx.HTTPError as exc:
        print(f"[FAIL] Backend smoke test HTTP error: {exc}")
        return 1
    except OSError as exc:
        print(f"[FAIL] Backend smoke test file error: {exc}")
        return 1

    return 0 if health_ok and scanner_ok else 1


if __name__ == "__main__":
    sys.exit(main())
