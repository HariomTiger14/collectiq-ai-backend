from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import settings


LOW_SCAN_CONFIDENCE_THRESHOLD = 70


class ScanFailureQueueError(Exception):
    """Raised when scan failure queue persistence cannot be read or written."""


class ScanFailureNotFoundError(Exception):
    """Raised when an admin action targets an unknown scan failure."""


class AdminScanFailureService:
    def __init__(
        self,
        *,
        repository: "SupabaseScanFailureRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseScanFailureRepository()

    def list_failures(self, *, reason: str = "all", limit: int = 50) -> dict[str, Any]:
        scans = (
            self._repository.list_scans(limit=max(limit, 200))
            if self._repository.is_configured
            else list(_IN_MEMORY_SCANS)
        )
        failures = [
            failure
            for scan in scans
            if (failure := _failure_from_scan(scan)) is not None
        ]
        if reason != "all":
            failures = [failure for failure in failures if reason in failure["reasons"]]
        failures.sort(key=_sort_key)
        limited = failures[:limit]
        return {
            "success": True,
            "filter": reason,
            "count": len(limited),
            "totalCount": len(failures),
            "thresholds": {"lowConfidence": LOW_SCAN_CONFIDENCE_THRESHOLD},
            "items": limited,
        }

    def get_failure_detail(self, scan_id: str) -> dict[str, Any]:
        scan = (
            self._repository.get_scan(scan_id)
            if self._repository.is_configured
            else _get_in_memory_scan(scan_id)
        )
        if scan is None:
            raise ScanFailureNotFoundError(f"Scan failure {scan_id} was not found.")
        failure = _failure_from_scan(scan) or {
            "id": _first_text(scan, "id", "scanId", "scan_id") or scan_id,
            "title": _first_text(scan, "title", "itemName", "item_name") or "Unknown scan",
            "reasonLabel": "Reviewed",
            "rawError": _safe_raw_error(scan),
            "createdAt": _first_text(scan, "createdAt", "created_at"),
            "updatedAt": _first_text(scan, "updatedAt", "updated_at"),
        }
        return {
            "success": True,
            "scanId": scan_id,
            "scan": {
                **failure,
                "rawScan": scan,
                "history": _scan_history(scan),
            },
        }

    def mark_reviewed(self, scan_id: str) -> dict[str, Any]:
        update = {
            "reviewStatus": "reviewed",
            "reviewedAt": _utc_now(),
            "needsReview": False,
        }
        scan = (
            self._repository.update_scan(scan_id, update)
            if self._repository.is_configured
            else _update_in_memory_scan(scan_id, update)
        )
        if scan is None:
            raise ScanFailureNotFoundError(f"Scan failure {scan_id} was not found.")
        return {"success": True, "scanId": scan_id, "reviewStatus": "reviewed"}

    def retry_analysis(self, scan_id: str) -> dict[str, Any]:
        scan = (
            self._repository.get_scan(scan_id)
            if self._repository.is_configured
            else _get_in_memory_scan(scan_id)
        )
        if scan is None:
            raise ScanFailureNotFoundError(f"Scan failure {scan_id} was not found.")
        if not _first_text(scan, "imageUrl", "image_url", "imagePath", "image_path"):
            raise ScanFailureQueueError("Original scan image reference is missing.")
        update = {
            "reviewStatus": "retry_requested",
            "retryRequestedAt": _utc_now(),
        }
        if self._repository.is_configured:
            self._repository.update_scan(scan_id, update)
        else:
            _update_in_memory_scan(scan_id, update)
        return {"success": True, "scanId": scan_id, "status": "retry_requested"}

    def resolve_failure(
        self,
        scan_id: str,
        *,
        category: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        resolved_at = _utc_now()
        update = {
            "reviewStatus": "resolved",
            "resolvedAt": resolved_at,
            "triageCategory": category,
            "resolutionNote": note.strip() if isinstance(note, str) and note.strip() else None,
            "needsReview": False,
        }
        scan = (
            self._repository.update_scan(scan_id, update)
            if self._repository.is_configured
            else _update_in_memory_scan(scan_id, update)
        )
        if scan is None:
            raise ScanFailureNotFoundError(f"Scan failure {scan_id} was not found.")
        return {
            "success": True,
            "scanId": scan_id,
            "reviewStatus": "resolved",
            "category": category,
            "note": update["resolutionNote"],
            "resolvedAt": resolved_at,
        }

    def bulk_mark_reviewed(self, scan_ids: list[str]) -> dict[str, Any]:
        return _bulk_result(scan_ids, self.mark_reviewed)

    def bulk_resolve_failures(
        self,
        scan_ids: list[str],
        *,
        category: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        return _bulk_result(
            scan_ids,
            lambda scan_id: self.resolve_failure(scan_id, category=category, note=note),
        )


class SupabaseScanFailureRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        table_name: str = "scan_analysis_events",
        timeout_seconds: float = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._table_name = table_name.strip() or "scan_analysis_events"
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def list_scans(self, *, limit: int = 200) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={
                "select": "*",
                "limit": str(limit),
                "order": "created_at.desc",
            },
        )
        if not isinstance(payload, list):
            raise ScanFailureQueueError("Supabase scan failure response shape was invalid.")
        return [_scan_from_row(row) for row in payload if isinstance(row, dict)]

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            f"/rest/v1/{self._table_name}",
            params={"id": f"eq.{scan_id}", "select": "*", "limit": "1"},
        )
        if not isinstance(payload, list):
            raise ScanFailureQueueError("Supabase scan failure response shape was invalid.")
        if not payload:
            return None
        row = payload[0]
        return _scan_from_row(row) if isinstance(row, dict) else None

    def update_scan(self, scan_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_scan(scan_id)
        if current is None:
            return None
        merged = {**current, **data}
        payload = self._request(
            "PATCH",
            f"/rest/v1/{self._table_name}",
            params={"id": f"eq.{scan_id}", "select": "*"},
            json_payload=_row_update_from_scan(merged),
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload:
            row = payload[0]
            return _scan_from_row(row) if isinstance(row, dict) else merged
        return merged

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
                json=json_payload,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ScanFailureQueueError("Supabase scan failure request failed.") from error
        finally:
            if should_close:
                client.close()


def seed_in_memory_scan(scan: dict[str, Any]) -> None:
    _IN_MEMORY_SCANS.insert(0, {"id": scan.get("id") or str(uuid4()), **scan})


def clear_in_memory_scans() -> None:
    _IN_MEMORY_SCANS.clear()


def _failure_from_scan(scan: dict[str, Any]) -> dict[str, Any] | None:
    confidence = _confidence(scan)
    status = (_first_text(scan, "status", "analysisStatus") or "completed").lower()
    error_code = _first_text(scan, "errorCode", "error_code")
    quality = (_first_text(scan, "detectionQuality", "imageQuality") or "").lower()
    pricing = scan.get("pricing") if isinstance(scan.get("pricing"), dict) else {}
    price_value = _number(_first_value(pricing, scan, "estimatedMarketValue", "value", "price"))

    reasons: list[str] = []
    if status in {"failed", "error", "provider_error"} or error_code:
        reasons.append("provider_error")
    if confidence is not None and confidence < LOW_SCAN_CONFIDENCE_THRESHOLD:
        reasons.append("low_confidence")
    if any(term in quality for term in ("blur", "dark", "poor", "bad", "low")):
        reasons.append("image_quality")
    if price_value is None or price_value <= 0:
        reasons.append("unpriced")
    if bool(scan.get("needsReview") or scan.get("needs_review")):
        reasons.append("needs_review")
    if not reasons or _first_text(scan, "reviewStatus", "review_status") in {"reviewed", "resolved"}:
        return None

    return {
        "id": _first_text(scan, "id", "scanId", "scan_id") or "unknown",
        "title": _first_text(scan, "title", "itemName", "item_name") or "Unknown scan",
        "category": _first_text(scan, "category") or "Unknown",
        "provider": _first_text(scan, "aiProvider", "ai_provider", "provider") or "Unknown",
        "confidence": confidence,
        "reasons": list(dict.fromkeys(reasons)),
        "reasonLabel": _reason_label(reasons),
        "failureReason": _first_text(scan, "failureReason", "errorMessage", "error_message")
        or _reason_label(reasons),
        "triageCategory": _first_text(scan, "triageCategory", "triage_category"),
        "resolutionNote": _first_text(scan, "resolutionNote", "resolution_note"),
        "rawError": _safe_raw_error(scan),
        "userId": _first_text(scan, "userId", "user_id"),
        "itemId": _first_text(scan, "itemId", "item_id", "portfolioItemId", "portfolio_item_id"),
        "imageUrl": _first_text(scan, "imageUrl", "image_url", "imagePath", "image_path"),
        "createdAt": _first_text(scan, "createdAt", "created_at"),
        "updatedAt": _first_text(scan, "updatedAt", "updated_at"),
    }


def _scan_from_row(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    return {
        **data,
        **{
            key: value
            for key, value in {
                "id": _first_value(row, "id", "scan_id"),
                "title": _first_value(row, "title", "item_name"),
                "category": _first_value(row, "category"),
                "status": _first_value(row, "status", "analysis_status"),
                "aiProvider": _first_value(row, "ai_provider", "provider"),
                "confidence": _first_value(row, "confidence"),
                "detectionQuality": _first_value(row, "detection_quality", "image_quality"),
                "errorCode": _first_value(row, "error_code"),
                "errorMessage": _first_value(row, "error_message"),
                "rawError": _first_value(row, "raw_error"),
                "userId": _first_value(row, "user_id"),
                "itemId": _first_value(row, "item_id", "portfolio_item_id"),
                "imageUrl": _first_value(row, "image_url"),
                "needsReview": _first_value(row, "needs_review"),
                "triageCategory": _first_value(row, "triage_category"),
                "resolutionNote": _first_value(row, "resolution_note"),
                "reviewStatus": _first_value(row, "review_status"),
                "resolvedAt": _first_value(row, "resolved_at"),
                "createdAt": _first_value(row, "created_at"),
                "updatedAt": _first_value(row, "updated_at"),
            }.items()
            if value not in (None, "")
        },
    }


def _row_update_from_scan(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": scan,
        "needs_review": bool(scan.get("needsReview") or scan.get("needs_review")),
        "review_status": scan.get("reviewStatus"),
        "reviewed_at": scan.get("reviewedAt"),
        "retry_requested_at": scan.get("retryRequestedAt"),
        "resolved_at": scan.get("resolvedAt"),
        "triage_category": scan.get("triageCategory"),
        "resolution_note": scan.get("resolutionNote"),
        "updated_at": _utc_now(),
    }


def _get_in_memory_scan(scan_id: str) -> dict[str, Any] | None:
    return next((scan for scan in _IN_MEMORY_SCANS if str(scan.get("id")) == scan_id), None)


def _update_in_memory_scan(scan_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    scan = _get_in_memory_scan(scan_id)
    if scan is None:
        return None
    scan.update(data)
    return scan


def _safe_raw_error(scan: dict[str, Any]) -> dict[str, Any]:
    raw = scan.get("rawError") or scan.get("raw_error")
    if isinstance(raw, dict):
        return {key: _redact_sensitive(value) for key, value in raw.items()}
    return {
        key: value
        for key, value in {
            "code": _first_text(scan, "errorCode", "error_code"),
            "message": _first_text(scan, "errorMessage", "error_message"),
            "provider": _first_text(scan, "aiProvider", "ai_provider", "provider"),
        }.items()
        if value
    }


def _scan_history(scan: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        ("retry_requested", _first_text(scan, "retryRequestedAt", "retry_requested_at")),
        ("reviewed", _first_text(scan, "reviewedAt", "reviewed_at")),
        ("resolved", _first_text(scan, "resolvedAt", "resolved_at")),
        ("updated", _first_text(scan, "updatedAt", "updated_at")),
        ("created", _first_text(scan, "createdAt", "created_at")),
    ]
    return [
        {"event": event, "createdAt": created_at}
        for event, created_at in candidates
        if created_at
    ]


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, str) and any(marker in value.lower() for marker in ("token", "key", "secret", "bearer")):
        return "[redacted]"
    if isinstance(value, dict):
        return {key: _redact_sensitive(child) for key, child in value.items()}
    return value


def _confidence(scan: dict[str, Any]) -> int | None:
    value = _first_value(scan, "confidence", "scanConfidence", "confidenceScore")
    number = _number(value)
    if number is None:
        return None
    if 0 <= number <= 1:
        number *= 100
    return max(0, min(100, round(number)))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_text(*sources: Any) -> str | None:
    value = _first_value(*sources)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(*sources: Any) -> Any:
    dicts = [source for source in sources if isinstance(source, dict)]
    keys = [source for source in sources if isinstance(source, str)]
    for source in dicts:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _reason_label(reasons: list[str]) -> str:
    labels = {
        "provider_error": "Provider error",
        "low_confidence": "Low confidence",
        "image_quality": "Image quality",
        "unpriced": "Unpriced",
        "needs_review": "Needs review",
    }
    return labels.get(reasons[0], "Needs review")


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    rank = {
        "provider_error": 0,
        "low_confidence": 1,
        "image_quality": 2,
        "unpriced": 3,
        "needs_review": 4,
    }
    confidence = item.get("confidence")
    return (
        rank.get(item["reasons"][0], 9),
        confidence if isinstance(confidence, int) else 101,
        item["title"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_IN_MEMORY_SCANS: list[dict[str, Any]] = []


def _bulk_result(ids: list[str], action) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for item_id in list(dict.fromkeys([str(value).strip() for value in ids if str(value).strip()])):
        try:
            results.append(action(item_id))
        except Exception as error:
            failures.append({"id": item_id, "error": str(error)})
    return {
        "success": not failures,
        "requested": len(ids),
        "completed": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
