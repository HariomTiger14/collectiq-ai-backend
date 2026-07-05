import argparse
import base64
import csv
import json
import mimetypes
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/analyze"
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MANIFEST_FIELDS = [
    "filename",
    "expected_name",
    "expected_category",
    "expected_brand",
    "expected_set",
    "expected_year",
    "expected_price_min",
    "expected_price_max",
    "source",
    "license",
    "notes",
]


@dataclass(frozen=True)
class ManifestRow:
    filename: str
    expected_name: str = ""
    expected_category: str = ""
    expected_brand: str = ""
    expected_set: str = ""
    expected_year: str = ""
    expected_price_min: float | None = None
    expected_price_max: float | None = None
    source: str = ""
    license: str = ""
    notes: str = ""

    @classmethod
    def from_mapping(cls, value: dict) -> "ManifestRow":
        normalized = {_canonical_key(key): item for key, item in value.items()}
        return cls(
            filename=str(
                normalized.get("filename")
                or normalized.get("image_filename")
                or ""
            ).strip(),
            expected_name=_text(
                normalized.get("expected_name")
                or normalized.get("expected_item")
            ),
            expected_category=_text(normalized.get("expected_category")),
            expected_brand=_text(
                normalized.get("expected_brand")
                or normalized.get("expected_brand_or_franchise")
            ),
            expected_set=_text(normalized.get("expected_set")),
            expected_year=_text(normalized.get("expected_year")),
            expected_price_min=_optional_float(normalized.get("expected_price_min")),
            expected_price_max=_optional_float(normalized.get("expected_price_max")),
            source=_text(normalized.get("source")),
            license=_text(normalized.get("license")),
            notes=_text(normalized.get("notes")),
        )

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "expected_name": self.expected_name,
            "expected_category": self.expected_category,
            "expected_brand": self.expected_brand,
            "expected_set": self.expected_set,
            "expected_year": self.expected_year,
            "expected_price_min": self.expected_price_min,
            "expected_price_max": self.expected_price_max,
            "source": self.source,
            "license": self.license,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ValidationResult:
    filename: str
    status: str
    expected_name: str = ""
    actual_name: str = ""
    expected_category: str = ""
    actual_category: str = ""
    expected_brand: str = ""
    actual_brand: str = ""
    expected_set: str = ""
    actual_set: str = ""
    expected_year: str = ""
    actual_year: str = ""
    expected_price_min: float | None = None
    expected_price_max: float | None = None
    actual_estimated_value: float | None = None
    confidence: int | None = None
    latency_ms: int | None = None
    fallback_reason: str = ""
    category_match: bool = False
    name_keyword_match: bool = False
    set_match: bool | None = None
    year_match: bool | None = None
    price_in_range: bool | None = None
    error: str = ""

    def to_csv_row(self) -> dict:
        return {
            "filename": self.filename,
            "status": self.status,
            "expected_name": self.expected_name,
            "actual_name": self.actual_name,
            "expected_category": self.expected_category,
            "actual_category": self.actual_category,
            "expected_brand": self.expected_brand,
            "actual_brand": self.actual_brand,
            "expected_set": self.expected_set,
            "actual_set": self.actual_set,
            "expected_year": self.expected_year,
            "actual_year": self.actual_year,
            "expected_price_min": _blank_if_none(self.expected_price_min),
            "expected_price_max": _blank_if_none(self.expected_price_max),
            "actual_estimated_value": _blank_if_none(self.actual_estimated_value),
            "confidence": _blank_if_none(self.confidence),
            "latency_ms": _blank_if_none(self.latency_ms),
            "fallback_reason": self.fallback_reason,
            "category_match": self.category_match,
            "name_keyword_match": self.name_keyword_match,
            "set_match": _blank_if_none(self.set_match),
            "year_match": _blank_if_none(self.year_match),
            "price_in_range": _blank_if_none(self.price_in_range),
            "error": self.error,
        }


def load_manifest(path: Path) -> list[ManifestRow]:
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return [ManifestRow.from_mapping(row) for row in csv.DictReader(handle)]

    parsed = json.loads(path.read_text(encoding="utf-8"))
    rows = parsed.get("items", parsed) if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError("Manifest JSON must be a list or an object with an items list.")
    return [ManifestRow.from_mapping(row) for row in rows if isinstance(row, dict)]


def write_manifest(rows: list[ManifestRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([row.to_dict() for row in rows], indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_manifest(
    *,
    output_manifest: Path,
    manifest_path: Path | None = None,
    image_folder: Path | None = None,
    kaggle_path: Path | None = None,
    hugging_face_export: Path | None = None,
    copy_images_to: Path | None = None,
) -> list[ManifestRow]:
    source_rows: list[ManifestRow] = []
    if manifest_path:
        source_rows = load_manifest(manifest_path)

    source_folder = image_folder or kaggle_path or hugging_face_export
    if not source_rows and source_folder:
        source_rows = [
            ManifestRow(
                filename=image_path.name,
                source=str(source_folder),
                license="user-provided",
                notes="Generated from local image folder; add ground-truth labels.",
            )
            for image_path in sorted(source_folder.iterdir())
            if image_path.is_file() and image_path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ]

    if copy_images_to and source_folder:
        copy_images_to.mkdir(parents=True, exist_ok=True)
        for row in source_rows:
            source_image = source_folder / row.filename
            if source_image.exists() and source_image.is_file():
                shutil.copy2(source_image, copy_images_to / row.filename)

    write_manifest(source_rows, output_manifest)
    return source_rows


def run_validation(
    *,
    manifest_path: Path,
    image_dir: Path,
    reports_dir: Path,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: float = 120,
    dry_run: bool = False,
) -> dict:
    rows = load_manifest(manifest_path)
    results: list[ValidationResult] = []
    reports_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        image_path = image_dir / row.filename
        if not image_path.exists() or not image_path.is_file():
            results.append(
                ValidationResult(
                    filename=row.filename,
                    status="missing_image",
                    expected_name=row.expected_name,
                    expected_category=row.expected_category,
                    expected_brand=row.expected_brand,
                    expected_set=row.expected_set,
                    expected_year=row.expected_year,
                    expected_price_min=row.expected_price_min,
                    expected_price_max=row.expected_price_max,
                    error="Image file is missing from validation/images.",
                )
            )
            continue
        if dry_run:
            results.append(
                ValidationResult(
                    filename=row.filename,
                    status="dry_run",
                    expected_name=row.expected_name,
                    expected_category=row.expected_category,
                    expected_brand=row.expected_brand,
                    expected_set=row.expected_set,
                    expected_year=row.expected_year,
                    expected_price_min=row.expected_price_min,
                    expected_price_max=row.expected_price_max,
                )
            )
            continue

        started_at = time.perf_counter()
        try:
            response = call_analyze(
                endpoint,
                build_payload(row=row, image_path=image_path),
                timeout_seconds=timeout_seconds,
            )
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            results.append(score_response(row, response, latency_ms=latency_ms))
        except Exception as exc:
            results.append(
                ValidationResult(
                    filename=row.filename,
                    status="failed",
                    expected_name=row.expected_name,
                    expected_category=row.expected_category,
                    expected_brand=row.expected_brand,
                    expected_set=row.expected_set,
                    expected_year=row.expected_year,
                    expected_price_min=row.expected_price_min,
                    expected_price_max=row.expected_price_max,
                    error=str(exc),
                )
            )

    metrics = calculate_metrics(results)
    write_reports(results, metrics, reports_dir, manifest_path=manifest_path, endpoint=endpoint)
    return {"rows": rows, "results": results, "metrics": metrics}


def build_payload(*, row: ManifestRow, image_path: Path) -> dict:
    image_bytes = image_path.read_bytes()
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "request": {
            "imagePath": str(image_path.resolve()),
            "imageSource": "validation",
            "requestedCategory": row.expected_category or None,
            "appVersion": "validation-lab",
            "deviceMetadata": {"source": "validation_lab"},
            "timestamp": timestamp,
        },
        "image": {
            "fileName": image_path.name,
            "mimeType": mime_type,
            "sizeBytes": len(image_bytes),
            "imageSource": "validation",
            "localFilePath": str(image_path.resolve()),
            "base64Image": base64.b64encode(image_bytes).decode("ascii"),
        },
    }


def call_analyze(endpoint: str, payload: dict, *, timeout_seconds: float) -> dict:
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8')}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach backend endpoint {endpoint}: {exc}") from exc

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Analyze endpoint returned a non-object response.")
    return parsed


def score_response(row: ManifestRow, response: dict, *, latency_ms: int) -> ValidationResult:
    key_attributes = _dict(response.get("keyAttributes"))
    market_summary = _dict(response.get("marketSummary"))
    diagnostics = _dict(response.get("diagnostics"))

    actual_name = _text(response.get("itemName"))
    actual_category = _text(response.get("category"))
    actual_brand = _text(key_attributes.get("brand"))
    actual_set = _text(key_attributes.get("setName") or key_attributes.get("series"))
    actual_year = _text(key_attributes.get("year"))
    estimated_value = _optional_float(response.get("estimatedValue"))

    return ValidationResult(
        filename=row.filename,
        status="passed",
        expected_name=row.expected_name,
        actual_name=actual_name,
        expected_category=row.expected_category,
        actual_category=actual_category,
        expected_brand=row.expected_brand,
        actual_brand=actual_brand,
        expected_set=row.expected_set,
        actual_set=actual_set,
        expected_year=row.expected_year,
        actual_year=actual_year,
        expected_price_min=row.expected_price_min,
        expected_price_max=row.expected_price_max,
        actual_estimated_value=estimated_value,
        confidence=_optional_int(response.get("confidence")),
        latency_ms=latency_ms,
        fallback_reason=_text(
            diagnostics.get("pricingFallbackReason")
            or market_summary.get("fallbackReason")
        ),
        category_match=category_matches(row.expected_category, actual_category),
        name_keyword_match=name_keyword_matches(row.expected_name, actual_name),
        set_match=optional_text_matches(row.expected_set, actual_set),
        year_match=optional_text_matches(row.expected_year, actual_year),
        price_in_range=price_in_range(
            estimated_value,
            row.expected_price_min,
            row.expected_price_max,
        ),
    )


def category_matches(expected: str, actual: str) -> bool:
    if not expected.strip():
        return False
    return _normalize(expected) == _normalize(actual)


def name_keyword_matches(expected: str, actual: str) -> bool:
    tokens = [
        token
        for token in _normalize(expected).replace("-", " ").split()
        if len(token) >= 3
    ]
    if not tokens:
        return False
    actual_normalized = _normalize(actual)
    matches = sum(1 for token in tokens if token in actual_normalized)
    return matches / len(tokens) >= 0.6


def optional_text_matches(expected: str, actual: str) -> bool | None:
    if not expected.strip():
        return None
    return _normalize(expected) in _normalize(actual)


def price_in_range(value: float | None, minimum: float | None, maximum: float | None) -> bool | None:
    if minimum is None or maximum is None:
        return None
    if value is None:
        return False
    return minimum <= value <= maximum


def calculate_metrics(results: list[ValidationResult]) -> dict:
    analyzed = [result for result in results if result.status == "passed"]
    return {
        "total_images": len(results),
        "analyzed_images": len(analyzed),
        "failed_image_count": len([result for result in results if result.status in {"failed", "missing_image"}]),
        "category_accuracy": _ratio([result.category_match for result in analyzed]),
        "name_keyword_match_accuracy": _ratio([result.name_keyword_match for result in analyzed]),
        "set_match_accuracy": _ratio([result.set_match for result in analyzed if result.set_match is not None]),
        "year_match_accuracy": _ratio([result.year_match for result in analyzed if result.year_match is not None]),
        "price_in_expected_range": _ratio([result.price_in_range for result in analyzed if result.price_in_range is not None]),
        "average_latency_ms": round(
            sum(result.latency_ms or 0 for result in analyzed) / len(analyzed),
            2,
        )
        if analyzed
        else 0,
        "low_confidence_count": len(
            [result for result in analyzed if result.confidence is not None and result.confidence < 70]
        ),
    }


def write_reports(
    results: list[ValidationResult],
    metrics: dict,
    reports_dir: Path,
    *,
    manifest_path: Path,
    endpoint: str,
) -> None:
    csv_path = reports_dir / "latest_validation_results.csv"
    markdown_path = reports_dir / "latest_validation_report.md"
    fieldnames = list(ValidationResult(filename="", status="").to_csv_row().keys())

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(result.to_csv_row())

    markdown_path.write_text(
        _format_markdown_report(results, metrics, manifest_path=manifest_path, endpoint=endpoint),
        encoding="utf-8",
    )


def _format_markdown_report(
    results: list[ValidationResult],
    metrics: dict,
    *,
    manifest_path: Path,
    endpoint: str,
) -> str:
    lines = [
        "# CollectIQ AI Validation Lab Report",
        "",
        f"- Generated: {datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        f"- Manifest: {manifest_path}",
        f"- Endpoint: {endpoint}",
        "",
        "## Metrics",
        "",
        f"- Total images: {metrics['total_images']}",
        f"- Analyzed images: {metrics['analyzed_images']}",
        f"- Failed image count: {metrics['failed_image_count']}",
        f"- Category accuracy: {_percent(metrics['category_accuracy'])}",
        f"- Name keyword match accuracy: {_percent(metrics['name_keyword_match_accuracy'])}",
        f"- Set match accuracy: {_percent(metrics['set_match_accuracy'])}",
        f"- Year match accuracy: {_percent(metrics['year_match_accuracy'])}",
        f"- Price in expected range: {_percent(metrics['price_in_expected_range'])}",
        f"- Average latency: {metrics['average_latency_ms']} ms",
        f"- Low-confidence count: {metrics['low_confidence_count']}",
        "",
        "## Results",
        "",
        "| File | Status | Expected | Actual | Category | Confidence | Latency | Error |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            "| {file} | {status} | {expected} | {actual} | {category} | {confidence} | {latency} | {error} |".format(
                file=_md(result.filename),
                status=_md(result.status),
                expected=_md(result.expected_name),
                actual=_md(result.actual_name),
                category=_md(result.actual_category),
                confidence=_md(_blank_if_none(result.confidence)),
                latency=_md(_blank_if_none(result.latency_ms)),
                error=_md(result.error),
            )
        )
    lines.append("")
    lines.append("Automated tests must run in mock/default mode. Real OpenAI/eBay validation is manual/local only.")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and run CollectIQ AI validation datasets.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--manifest", default="")
    prepare.add_argument("--image-folder", default="")
    prepare.add_argument("--kaggle-path", default="")
    prepare.add_argument("--hugging-face-export", default="")
    prepare.add_argument("--output-manifest", required=True)
    prepare.add_argument("--copy-images-to", default="")

    run = subparsers.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--image-dir", required=True)
    run.add_argument("--reports-dir", required=True)
    run.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "prepare":
        rows = prepare_manifest(
            manifest_path=_optional_path(args.manifest),
            image_folder=_optional_path(args.image_folder),
            kaggle_path=_optional_path(args.kaggle_path),
            hugging_face_export=_optional_path(args.hugging_face_export),
            output_manifest=Path(args.output_manifest),
            copy_images_to=_optional_path(args.copy_images_to),
        )
        print(f"Prepared manifest with {len(rows)} rows: {args.output_manifest}")
        return 0

    result = run_validation(
        manifest_path=Path(args.manifest),
        image_dir=Path(args.image_dir),
        reports_dir=Path(args.reports_dir),
        endpoint=args.endpoint,
        timeout_seconds=args.timeout,
        dry_run=args.dry_run,
    )
    metrics = result["metrics"]
    print(
        "Validation complete: "
        f"{metrics['analyzed_images']} analyzed, "
        f"{metrics['failed_image_count']} failed/missing."
    )
    print(f"Reports written to: {args.reports_dir}")
    return 0


def _canonical_key(value: str) -> str:
    output = []
    for char in value:
        if char.isupper() and output:
            output.append("_")
        output.append(char.lower())
    return "".join(output).replace("-", "_").replace(" ", "_")


def _optional_path(value: str) -> Path | None:
    return Path(value) if value else None


def _optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    parsed = _optional_float(value)
    return None if parsed is None else int(parsed)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _normalize(value: str) -> str:
    return " ".join(_text(value).lower().split())


def _ratio(values: list[bool]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def _percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _blank_if_none(value) -> str:
    return "" if value is None else str(value)


def _md(value) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
