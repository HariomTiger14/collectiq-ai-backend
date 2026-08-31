"""GDPR/CCPA-style data requests: a user's own export/deletion requests.

Two audiences:
  - the mobile app (a signed-in user filing a request for themselves, via
    their own Supabase session bearer token)
  - the admin console (processing the queue: running the export bundler or
    the deletion job)

**Export** is a JSON bundle of every user-scoped table, uploaded to Supabase
Storage and handed back as a time-limited signed URL -- not a multi-file zip
with actual image binaries. Bundling real image files is a real,
consciously-scoped-out gap, not silently pretended away -- the export
includes each portfolio item's own stored image metadata/URLs exactly as the
app already wrote them, and `imagesIncluded` is `false` in the receipt so
nothing overclaims completeness.

**Deletion** hard-deletes every user-scoped row across the tables below, the
user's Storage objects, and finally the Supabase Auth user itself via the
Auth Admin API. `admin_audit_events` is deliberately EXCLUDED -- it is the
platform's own compliance record of admin actions (who did what, when), not
the user's personal data; erasing rows that merely reference the user as a
target would destroy the audit trail it exists to protect.

Deletion runs in two shapes:
  - **scheduled** (the app): the user confirms in-app, the row is written
    with status='scheduled' and a `scheduled_for` date, and nothing is
    touched until then. Signing back in cancels it. A cron purges what is
    due. Apple's guideline 5.1.1(v) requires deletion to be initiated in-app
    and to actually happen without a human approving it.
  - **immediate** (the admin console): the existing process_request path,
    unchanged.

Storage note: images ARE now deleted. Earlier revisions of this module said
the backend had no visibility into the app's bucket layout; that was wrong --
the app writes to the same `collectiq-portfolio-images` bucket this module
already uses, under `users/<user_id>/...` (see CloudStoragePaths in the
mobile repo). Support attachments live under `support-attachments/<message
_id>/` and are NOT user-prefixed, so they are resolved through the user's
tickets rather than by path.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.admin_audit_service import AdminAuditService

EXPORT_STORAGE_BUCKET = "collectiq-portfolio-images"
EXPORT_STORAGE_PREFIX = "data-exports"
EXPORT_URL_EXPIRES_SECONDS = 60 * 60 * 72  # 72h

# How long a user has to change their mind before a scheduled deletion is
# actually carried out. 30 days is what Instagram/X/LinkedIn use and what
# users have been trained to expect; short enough to still read as deletion.
DELETION_GRACE_PERIOD_DAYS = 30

# Everything the mobile app uploads lands under users/<user_id>/ in the same
# bucket (portfolio images, gallery variants, profile avatar) -- see
# CloudStoragePaths in the mobile repo. Deleting that prefix is what makes
# "your images are deleted" true.
USER_STORAGE_PREFIX = "users"
# Support attachments are keyed by message id, not user id, so they cannot be
# found by prefix -- they are resolved via the user's tickets instead.
SUPPORT_ATTACHMENT_PREFIX = "support-attachments"
# Supabase Storage list() is paginated; also caps a runaway walk.
_STORAGE_PAGE_SIZE = 100
_STORAGE_MAX_OBJECTS = 20_000

# Tables holding a LIST of rows per user (0..N), deleted/exported by user_id.
_USER_SCOPED_LIST_TABLES = (
    "portfolio_items",
    "collector_wishlist_entries",
    "price_alerts",
    "push_device_registrations",
    "portfolio_valuation_snapshots",
    "scan_analysis_events",
    "push_notification_deliveries",
)
# Tables holding at most ONE row per user, keyed by user_id.
_USER_SCOPED_SINGLE_ROW_TABLES = (
    "collector_profiles",
    "user_subscriptions",
)

# Deleted but NOT exported.
#
# support_tickets: the user's own support history. It carries user_id but has
# no FK to auth.users, so deleting the auth user does not cascade it -- it
# would have survived deletion entirely. Its support_messages cascade on
# ticket_id, and support_message_attachments cascade from those, so removing
# the ticket rows clears the whole thread.
#
# user_scan_usage: quota counters. This one DOES cascade (it declares
# `references auth.users (id) on delete cascade`), so it is already covered
# when the auth user goes -- listed explicitly anyway so deletion does not
# silently depend on an FK staying in place.
_DELETION_ONLY_TABLES = (
    "support_tickets",
    "user_scan_usage",
)

_ALL_DELETION_TABLES = (
    *_USER_SCOPED_LIST_TABLES,
    *_USER_SCOPED_SINGLE_ROW_TABLES,
    *_DELETION_ONLY_TABLES,
)


class DataRequestError(Exception):
    """Raised when a data request cannot be created or processed safely."""


class DataRequestUnauthorizedError(DataRequestError):
    """The caller's Supabase session is missing or invalid."""


class DataRequestNotFoundError(DataRequestError):
    """No data request exists with the given id."""


class DataRequestService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        anon_key: str | None = None,
        client: httpx.Client | None = None,
        audit_service: AdminAuditService | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._anon_key = (
            anon_key if anon_key is not None else settings.supabase_anon_key
        ).strip()
        self._client = client or httpx.Client(timeout=30)
        self._audit_service = audit_service or AdminAuditService()

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    # -- auth (user-facing endpoints only) ---------------------------------

    def user_id_from_token(self, access_token: str) -> str:
        """Resolve the Supabase user id from a user's own bearer token."""
        token = (access_token or "").strip()
        if not token:
            raise DataRequestUnauthorizedError("Missing access token.")
        try:
            response = self._client.get(
                f"{self._supabase_url}/auth/v1/user",
                headers={"apikey": self._anon_key, "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            raise DataRequestError(f"Could not reach the auth service: {error}") from error
        if response.status_code != 200:
            raise DataRequestUnauthorizedError("Invalid or expired session.")
        user_id = (response.json() or {}).get("id")
        if not user_id:
            raise DataRequestUnauthorizedError("Could not resolve user.")
        return str(user_id)

    # -- user-facing ---------------------------------------------------------

    def create_request(self, *, user_id: str, request_type: str) -> dict[str, Any]:
        if request_type not in ("export", "deletion"):
            raise DataRequestError(f"Unknown request type: {request_type!r}")
        existing = self._request(
            "GET",
            "/rest/v1/data_requests",
            params={
                "user_id": f"eq.{user_id}",
                "type": f"eq.{request_type}",
                "status": "in.(open,processing)",
                "select": "id",
                "limit": "1",
            },
        )
        if isinstance(existing, list) and existing:
            raise DataRequestError(
                f"A {request_type} request is already open for this account."
            )
        rows = self._request(
            "POST",
            "/rest/v1/data_requests",
            json_payload={"user_id": user_id, "type": request_type, "status": "open"},
            extra_headers={"Prefer": "return=representation"},
        )
        if not isinstance(rows, list) or not rows:
            raise DataRequestError("Could not create the data request.")
        return _to_public(rows[0])

    def schedule_deletion(
        self,
        *,
        user_id: str,
        grace_period_days: int = DELETION_GRACE_PERIOD_DAYS,
    ) -> dict[str, Any]:
        """Start the grace period. Nothing is deleted here.

        Idempotent: confirming twice returns the deletion already scheduled
        rather than moving the date, so a double-tap cannot extend or shorten
        the window.
        """
        existing = self._scheduled_deletion_row(user_id)
        if existing is not None:
            return _to_public(existing)

        now = datetime.now(timezone.utc)
        scheduled_for = now + timedelta(days=grace_period_days)
        rows = self._request(
            "POST",
            "/rest/v1/data_requests",
            json_payload={
                "user_id": user_id,
                "type": "deletion",
                "status": "scheduled",
                "scheduled_for": scheduled_for.isoformat(),
            },
            extra_headers={"Prefer": "return=representation"},
        )
        if not isinstance(rows, list) or not rows:
            raise DataRequestError("Could not schedule the account deletion.")
        return _to_public(rows[0])

    def cancel_deletion(self, *, user_id: str) -> dict[str, Any]:
        """Cancel a pending deletion. This is what signing back in offers."""
        existing = self._scheduled_deletion_row(user_id)
        if existing is None:
            raise DataRequestNotFoundError(
                "No scheduled deletion to cancel for this account."
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self._request(
            "PATCH",
            "/rest/v1/data_requests",
            params={"id": f"eq.{existing['id']}"},
            json_payload={
                "status": "cancelled",
                "cancelled_at": now_iso,
                "updated_at": now_iso,
            },
            extra_headers={"Prefer": "return=representation"},
        )
        if not isinstance(rows, list) or not rows:
            raise DataRequestError("Could not cancel the account deletion.")
        return _to_public(rows[0])

    def deletion_status(self, user_id: str) -> dict[str, Any]:
        """What the app gates its launch screen on."""
        row = self._scheduled_deletion_row(user_id)
        if row is None:
            return {"scheduled": False, "scheduledFor": None, "requestId": None}
        return {
            "scheduled": True,
            "scheduledFor": row.get("scheduled_for"),
            "requestId": row.get("id"),
        }

    def _scheduled_deletion_row(self, user_id: str) -> dict[str, Any] | None:
        rows = self._request(
            "GET",
            "/rest/v1/data_requests",
            params={
                "user_id": f"eq.{user_id}",
                "type": "eq.deletion",
                "status": "eq.scheduled",
                "select": "*",
                "order": "requested_at.desc",
                "limit": "1",
            },
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    def list_my_requests(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._request(
            "GET",
            "/rest/v1/data_requests",
            params={
                "user_id": f"eq.{user_id}",
                "select": "*",
                "order": "requested_at.desc",
            },
        )
        return [_to_public(row) for row in rows] if isinstance(rows, list) else []

    # -- admin -----------------------------------------------------------

    def list_requests(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        params = {"select": "*", "order": "requested_at.desc", "limit": str(limit)}
        if status:
            params["status"] = f"eq.{status}"
        rows = self._request("GET", "/rest/v1/data_requests", params=params)
        requests = [_to_public(row) for row in rows] if isinstance(rows, list) else []
        return {
            "success": True,
            "requests": requests,
            "openCount": self._count(status="open"),
            "completed90d": self._count(
                status="completed",
                since=(datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                ).isoformat()),
                since_days=90,
            ),
            "avgDaysToClose": self._avg_days_to_close(),
        }

    def process_request(
        self,
        request_id: str,
        *,
        dry_run: bool = True,
        actor: str = "admin_token",
    ) -> dict[str, Any]:
        row = self._get_row(request_id)
        request_type = row.get("type")
        user_id = str(row.get("user_id") or "")
        if request_type == "export":
            result = self._process_export(row, dry_run=dry_run)
        elif request_type == "deletion":
            result = self._process_deletion(row, dry_run=dry_run)
        else:
            raise DataRequestError(f"Unknown request type: {request_type!r}")

        if not dry_run:
            self._audit_service.record(
                action=f"data_request.{request_type}_processed",
                status="success",
                target_type="user",
                target_id=user_id,
                actor=actor,
                metadata={"requestId": request_id, **result.get("receipt", {})},
            )
        return {"success": True, "dryRun": dry_run, "requestId": request_id, **result}

    def purge_due(
        self,
        *,
        limit: int = 50,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Carry out every scheduled deletion whose grace period has elapsed.

        Driven by the purge cron. One failing account must not stop the rest,
        so each is caught and reported individually; a failed row is left
        `scheduled` so the next run retries it rather than stranding a user
        half-deleted with no further attempts.
        """
        reference = now or datetime.now(timezone.utc)
        rows = self._request(
            "GET",
            "/rest/v1/data_requests",
            params={
                "type": "eq.deletion",
                "status": "eq.scheduled",
                "scheduled_for": f"lte.{reference.isoformat()}",
                "select": "*",
                "order": "scheduled_for.asc",
                "limit": str(limit),
            },
        )
        due = rows if isinstance(rows, list) else []

        processed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for row in due:
            request_id = str(row.get("id") or "")
            try:
                result = self._process_deletion(row, dry_run=dry_run)
                if not dry_run:
                    self._audit_service.record(
                        action="data_request.deletion_processed",
                        status="success",
                        target_type="user",
                        target_id=str(row.get("user_id") or ""),
                        actor="deletion_purge_cron",
                        metadata={
                            "requestId": request_id,
                            "scheduledFor": row.get("scheduled_for"),
                            **result.get("receipt", {}),
                        },
                    )
                processed.append({"requestId": request_id, **result})
            except DataRequestError as error:
                failed.append({"requestId": request_id, "error": str(error)})

        return {
            "success": True,
            "dryRun": dry_run,
            "dueCount": len(due),
            "processedCount": len(processed),
            "failedCount": len(failed),
            "processed": processed,
            "failed": failed,
        }

    # -- export --------------------------------------------------------

    def _process_export(self, row: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        user_id = str(row.get("user_id") or "")
        bundle = self._collect_user_data(user_id)
        counts = {table: len(rows) for table, rows in bundle.items()}
        if dry_run:
            return {"preview": {"tableCounts": counts, "imagesIncluded": False}}

        export_path = f"{EXPORT_STORAGE_PREFIX}/{user_id}/{row['id']}.json"
        self._upload_export_bundle(export_path, {
            "userId": user_id,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "imagesIncluded": False,
            "data": bundle,
        })
        signed_url = self._signed_export_url(export_path)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=EXPORT_URL_EXPIRES_SECONDS)
        self._request(
            "PATCH",
            "/rest/v1/data_requests",
            params={"id": f"eq.{row['id']}"},
            json_payload={
                "status": "completed",
                "completed_at": now.isoformat(),
                "export_path": export_path,
                "export_expires_at": expires_at.isoformat(),
                "raw_json": {"tableCounts": counts, "imagesIncluded": False},
                "updated_at": now.isoformat(),
            },
            extra_headers={"Prefer": "return=minimal"},
        )
        return {
            "downloadUrl": signed_url,
            "expiresAt": expires_at.isoformat(),
            "receipt": {"tableCounts": counts, "imagesIncluded": False},
        }

    def _collect_user_data(self, user_id: str) -> dict[str, list[dict[str, Any]]]:
        bundle: dict[str, list[dict[str, Any]]] = {}
        for table in _USER_SCOPED_LIST_TABLES:
            bundle[table] = self._fetch_all_for_user(table, user_id)
        for table in _USER_SCOPED_SINGLE_ROW_TABLES:
            rows = self._fetch_all_for_user(table, user_id, limit=1)
            bundle[table] = rows
        return bundle

    def _fetch_all_for_user(
        self, table: str, user_id: str, *, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        try:
            rows = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={"user_id": f"eq.{user_id}", "select": "*", "limit": str(limit)},
            )
        except DataRequestError:
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _upload_export_bundle(self, path: str, payload: dict[str, Any]) -> None:
        self._request(
            "POST",
            f"/storage/v1/object/{EXPORT_STORAGE_BUCKET}/{path}",
            raw_body=json.dumps(payload).encode("utf-8"),
            extra_headers={
                "Content-Type": "application/json",
                "x-upsert": "true",
            },
        )

    def _signed_export_url(self, path: str, *, expires_in: int = EXPORT_URL_EXPIRES_SECONDS) -> str | None:
        payload = self._request(
            "POST",
            f"/storage/v1/object/sign/{EXPORT_STORAGE_BUCKET}/{path}",
            json_payload={"expiresIn": expires_in},
        )
        if not isinstance(payload, dict):
            return None
        signed_path = payload.get("signedURL") or payload.get("signedUrl")
        if not isinstance(signed_path, str) or not signed_path:
            return None
        if signed_path.startswith("http"):
            return signed_path
        if not signed_path.startswith("/"):
            signed_path = "/" + signed_path
        if signed_path.startswith("/storage"):
            return f"{self._supabase_url}{signed_path}"
        return f"{self._supabase_url}/storage/v1{signed_path}"

    # -- deletion --------------------------------------------------------

    def _process_deletion(self, row: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        user_id = str(row.get("user_id") or "")
        counts = {}
        for table in _ALL_DELETION_TABLES:
            counts[table] = len(self._fetch_all_for_user(table, user_id))

        # Resolved before any row is deleted: attachment paths are only
        # reachable through the user's tickets, and deleting support_tickets
        # cascades those rows away. Collect first, delete second, or the
        # files are orphaned in the bucket with nothing left pointing at them.
        storage_paths = self._user_storage_paths(user_id)

        if dry_run:
            return {
                "preview": {
                    "tableCounts": counts,
                    "storageObjectCount": len(storage_paths),
                    "authAccountWillBeDeleted": True,
                    "auditEventsExcluded": True,
                },
            }

        deleted_counts: dict[str, int] = {}
        for table in _ALL_DELETION_TABLES:
            deleted_counts[table] = counts.get(table, 0)
            self._request(
                "DELETE",
                f"/rest/v1/{table}",
                params={"user_id": f"eq.{user_id}"},
                extra_headers={"Prefer": "return=minimal"},
            )

        storage_deleted = self._delete_storage_paths(storage_paths)
        auth_deleted = self._delete_auth_user(user_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        receipt = {
            "tableCounts": deleted_counts,
            "storageObjectsDeleted": storage_deleted,
            "authAccountDeleted": auth_deleted,
            "auditEventsExcluded": True,
        }
        self._request(
            "PATCH",
            "/rest/v1/data_requests",
            params={"id": f"eq.{row['id']}"},
            json_payload={
                "status": "completed",
                "completed_at": now_iso,
                "raw_json": receipt,
                "updated_at": now_iso,
            },
            extra_headers={"Prefer": "return=minimal"},
        )
        return {"receipt": receipt}

    # -- storage ---------------------------------------------------------

    def _user_storage_paths(self, user_id: str) -> list[str]:
        """Every Storage object belonging to this user, across both layouts.

        Two prefixes are user-keyed and can be walked directly; support
        attachments are keyed by message id, so they are resolved through the
        user's tickets instead.
        """
        safe_user = _safe_path_segment(user_id)
        paths: list[str] = []
        paths.extend(self._list_storage_objects(f"{USER_STORAGE_PREFIX}/{safe_user}"))
        paths.extend(self._list_storage_objects(f"{EXPORT_STORAGE_PREFIX}/{user_id}"))
        paths.extend(self._support_attachment_paths(user_id))
        # A prior export bundle can appear both by prefix walk and via
        # export_path; de-duplicate while keeping order stable for tests.
        seen: set[str] = set()
        unique: list[str] = []
        for path in paths:
            if path and path not in seen:
                seen.add(path)
                unique.append(path)
        return unique

    def _support_attachment_paths(self, user_id: str) -> list[str]:
        ticket_ids = [
            str(row["id"])
            for row in self._select(
                "support_tickets", {"user_id": f"eq.{user_id}", "select": "id"}
            )
            if row.get("id")
        ]
        if not ticket_ids:
            return []
        message_ids = [
            str(row["id"])
            for row in self._select(
                "support_messages",
                {"ticket_id": f"in.({','.join(ticket_ids)})", "select": "id"},
            )
            if row.get("id")
        ]
        if not message_ids:
            return []
        return [
            str(row["file_path"])
            for row in self._select(
                "support_message_attachments",
                {
                    "message_id": f"in.({','.join(message_ids)})",
                    "select": "file_path",
                },
            )
            if row.get("file_path")
        ]

    def _select(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        rows = self._request("GET", f"/rest/v1/{table}", params=params)
        return rows if isinstance(rows, list) else []

    def _list_storage_objects(self, prefix: str) -> list[str]:
        """Recursively list objects under a prefix.

        Supabase Storage list() is one directory level at a time and returns
        folders as entries with a null id, so this walks rather than assuming
        a flat namespace -- portfolio gallery variants nest a level deeper
        than the primary image (users/<id>/portfolio_images/<item>/00-front.jpg).
        """
        found: list[str] = []
        pending = [prefix.strip("/")]
        while pending:
            current = pending.pop()
            offset = 0
            while True:
                payload = self._request(
                    "POST",
                    f"/storage/v1/object/list/{EXPORT_STORAGE_BUCKET}",
                    json_payload={
                        "prefix": f"{current}/" if current else "",
                        "limit": _STORAGE_PAGE_SIZE,
                        "offset": offset,
                    },
                )
                entries = payload if isinstance(payload, list) else []
                for entry in entries:
                    name = (entry or {}).get("name")
                    if not name:
                        continue
                    full = f"{current}/{name}" if current else str(name)
                    # Folders come back with a null id and no metadata.
                    if entry.get("id") is None:
                        pending.append(full)
                    else:
                        found.append(full)
                if len(found) >= _STORAGE_MAX_OBJECTS:
                    return found[:_STORAGE_MAX_OBJECTS]
                if len(entries) < _STORAGE_PAGE_SIZE:
                    break
                offset += _STORAGE_PAGE_SIZE
        return found

    def _delete_storage_paths(self, paths: list[str]) -> int:
        """Delete objects in batches. Returns how many were requested.

        Storage failures must not abort the purge: the database rows and the
        auth user still have to go, and an orphaned image with no account and
        no row pointing at it is a far smaller problem than a half-deleted
        account that never completes.
        """
        if not paths:
            return 0
        deleted = 0
        for start in range(0, len(paths), _STORAGE_PAGE_SIZE):
            batch = paths[start : start + _STORAGE_PAGE_SIZE]
            try:
                self._request(
                    "DELETE",
                    f"/storage/v1/object/{EXPORT_STORAGE_BUCKET}",
                    json_payload={"prefixes": batch},
                )
                deleted += len(batch)
            except DataRequestError:
                continue
        return deleted

    def _delete_auth_user(self, user_id: str) -> bool:
        try:
            self._request(
                "DELETE",
                f"/auth/v1/admin/users/{user_id}",
            )
            return True
        except DataRequestError:
            return False

    # -- shared helpers --------------------------------------------------

    def _get_row(self, request_id: str) -> dict[str, Any]:
        rows = self._request(
            "GET",
            "/rest/v1/data_requests",
            params={"id": f"eq.{request_id}", "select": "*", "limit": "1"},
        )
        if not isinstance(rows, list) or not rows:
            raise DataRequestNotFoundError(f"Data request {request_id} was not found.")
        return rows[0]

    def _count(self, *, status: str | None = None, since: str | None = None, since_days: int | None = None) -> int:
        params: dict[str, str] = {"select": "id"}
        if status:
            params["status"] = f"eq.{status}"
        if since_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
            params["requested_at"] = f"gte.{cutoff}"
        try:
            response = self._request(
                "GET",
                "/rest/v1/data_requests",
                params=params,
                extra_headers={"Prefer": "count=exact", "Range": "0-0"},
                return_response=True,
            )
        except DataRequestError:
            return 0
        content_range = response.headers.get("content-range")
        return _total_from_content_range(content_range)

    def _avg_days_to_close(self) -> float | None:
        rows = self._request(
            "GET",
            "/rest/v1/data_requests",
            params={
                "status": "eq.completed",
                "select": "requested_at,completed_at",
                "order": "completed_at.desc",
                "limit": "200",
            },
        )
        if not isinstance(rows, list) or not rows:
            return None
        deltas = []
        for row in rows:
            try:
                requested = datetime.fromisoformat(str(row["requested_at"]).replace("Z", "+00:00"))
                completed = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            deltas.append((completed - requested).total_seconds() / 86400)
        if not deltas:
            return None
        return round(sum(deltas) / len(deltas), 1)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        return_response: bool = False,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
        }
        if json_payload is not None or raw_body is None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
                json=json_payload if raw_body is None else None,
                content=raw_body,
            )
            response.raise_for_status()
            if return_response:
                return response
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise DataRequestError(f"Supabase data-request call failed: {error}") from error


def _safe_path_segment(value: str) -> str:
    """Mirror of CloudStoragePaths.safePathSegment in the mobile app.

    The app lowercases and slugifies each path segment before uploading, so
    the backend has to apply the identical transform or it walks a prefix
    that does not exist. Supabase user ids are already lowercase hex with
    hyphens, which this leaves untouched -- it exists so the two stay in
    step if either side ever changes.
    """
    lowered = (value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]", "-", lowered)
    return re.sub(r"-+", "-", slug)


def _total_from_content_range(content_range: str | None) -> int:
    if not content_range or "/" not in content_range:
        return 0
    total = content_range.rsplit("/", 1)[-1]
    return int(total) if total.isdigit() else 0


def _to_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "userId": row.get("user_id"),
        "type": row.get("type"),
        "status": row.get("status"),
        "requestedAt": row.get("requested_at"),
        "completedAt": row.get("completed_at"),
        "exportPath": row.get("export_path"),
        "exportExpiresAt": row.get("export_expires_at"),
        "notes": row.get("notes"),
        "raw": row.get("raw_json") or {},
    }
