import argparse
import base64
import json
import mimetypes
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/analyze"


def build_payload(
    *,
    image_path: Path,
    image_source: str,
    requested_category: str | None,
) -> dict:
    resolved_path = image_path.resolve()
    image_bytes = resolved_path.read_bytes()
    mime_type = mimetypes.guess_type(resolved_path.name)[0] or "image/jpeg"
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return {
        "request": {
            "imagePath": str(resolved_path),
            "imageSource": image_source,
            "requestedCategory": requested_category,
            "appVersion": "validation-script",
            "deviceMetadata": {
                "source": "backend/scripts/validate_real_analysis.py",
            },
            "timestamp": timestamp,
        },
        "image": {
            "fileName": resolved_path.name,
            "mimeType": mime_type,
            "sizeBytes": len(image_bytes),
            "imageSource": image_source,
            "localFilePath": str(resolved_path),
            "base64Image": base64.b64encode(image_bytes).decode("ascii"),
        },
    }


def call_analyze(endpoint: str, payload: dict, *, timeout_seconds: float) -> tuple[dict, int]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    started_at = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8")
        raise RuntimeError(f"Backend returned HTTP {exc.code}: {response_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach backend endpoint {endpoint}: {exc}") from exc

    latency_ms = int((time.perf_counter() - started_at) * 1000)
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Backend returned non-JSON response.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Backend returned an unexpected response shape.")
    return parsed, latency_ms


def summarize_response(payload: dict, *, request_latency_ms: int) -> dict:
    market_summary = _as_dict(payload.get("marketSummary"))
    diagnostics = _as_dict(payload.get("diagnostics"))
    alternatives = _as_list(payload.get("alternatives"))
    image_quality_warnings = _as_list(payload.get("imageQualityIssues"))

    return {
        "itemName": _text(payload.get("itemName")),
        "category": _text(payload.get("category")),
        "confidence": payload.get("confidence"),
        "confidenceLevel": _text(
            payload.get("confidenceLevel") or diagnostics.get("confidenceLevel")
        ),
        "estimatedValue": payload.get("estimatedValue"),
        "valueRange": f"{payload.get('lowEstimate')} - {payload.get('highEstimate')}",
        "pricingSource": ", ".join(_as_list(market_summary.get("sources")))
        or _text(diagnostics.get("pricingProvider")),
        "fallbackUsed": _yes_no(diagnostics.get("pricingFallbackUsed")),
        "fallbackReason": _text(diagnostics.get("pricingFallbackReason")),
        "requestLatencyMs": request_latency_ms,
        "totalAnalyzeLatencyMs": diagnostics.get("totalLatencyMs"),
        "aiLatencyMs": diagnostics.get("aiLatencyMs"),
        "pricingLatencyMs": diagnostics.get("pricingProviderLatencyMs"),
        "imageQualityWarnings": image_quality_warnings,
        "alternatives": [
            {
                "title": _text(match.get("title")),
                "category": _text(match.get("category")),
                "confidence": match.get("confidence"),
            }
            for match in alternatives
            if isinstance(match, dict)
        ],
    }


def format_summary(summary: dict) -> str:
    lines = [
        "CollectIQ AI Real Analysis Validation",
        "--------------------------------------",
        f"Item name: {summary['itemName']}",
        f"Category: {summary['category']}",
        f"Confidence: {summary['confidence']} ({summary['confidenceLevel']})",
        f"Estimated value: {summary['estimatedValue']}",
        f"Value range: {summary['valueRange']}",
        f"Pricing source: {summary['pricingSource']}",
        f"Fallback used: {summary['fallbackUsed']}",
        f"Fallback reason: {summary['fallbackReason'] or 'None'}",
        f"Request latency: {summary['requestLatencyMs']} ms",
        f"Total analyze latency: {summary['totalAnalyzeLatencyMs']} ms",
        f"AI latency: {summary['aiLatencyMs']} ms",
        f"Pricing latency: {summary['pricingLatencyMs']} ms",
        "Image quality warnings:",
    ]
    warnings = summary["imageQualityWarnings"]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None")

    lines.append("Alternatives:")
    alternatives = summary["alternatives"]
    if alternatives:
        lines.extend(
            f"- {alt['title']} ({alt['category']}, confidence={alt['confidence']})"
            for alt in alternatives
        )
    else:
        lines.append("- None")

    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate CollectIQ AI backend analysis with a local image.",
    )
    parser.add_argument("image", help="Path to local jpg/jpeg/png image.")
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"Backend analyze endpoint. Default: {DEFAULT_ENDPOINT}",
    )
    parser.add_argument(
        "--image-source",
        default="validation",
        help="Image source label to send in the request.",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Optional requested collectible category.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="Request timeout in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    image_path = Path(args.image)
    if not image_path.exists() or not image_path.is_file():
        print(f"Image not found: {image_path}", file=sys.stderr)
        return 2

    try:
        payload = build_payload(
            image_path=image_path,
            image_source=args.image_source,
            requested_category=args.category,
        )
        response, latency_ms = call_analyze(
            args.endpoint,
            payload,
            timeout_seconds=args.timeout,
        )
        print(format_summary(summarize_response(response, request_latency_ms=latency_ms)))
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _text(value) -> str:
    return "" if value is None else str(value)


def _yes_no(value) -> str:
    return "yes" if value is True else "no"


if __name__ == "__main__":
    raise SystemExit(main())
