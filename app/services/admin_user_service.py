from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.services.admin_audit_service import AdminAuditError, AdminAuditService
from app.services.subscription.subscription_service import (
    SubscriptionService,
    SubscriptionServiceError,
)


# Matches the mobile app's SupabaseCloudProfileSyncService.bucketName default
# (packlox-mobile-final/lib/core/cloud/supabase/supabase_cloud_profile_sync_service.dart).
# The bucket is private (the app itself only ever reads via an authenticated
# .download() call), so admin needs a signed URL rather than a public one.
AVATAR_STORAGE_BUCKET = "collectiq-portfolio-images"


class AdminUserServiceError(Exception):
    """Raised when admin user data cannot be read safely."""


class AdminUserService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminUserRepository | None" = None,
        subscription_service: SubscriptionService | None = None,
        audit_service: AdminAuditService | None = None,
    ) -> None:
        self._repository = repository or SupabaseAdminUserRepository()
        self._subscription_service = subscription_service or SubscriptionService()
        self._audit_service = audit_service or AdminAuditService()

    def list_users(self, *, query: str | None = None, limit: int = 50, page: int = 1) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        users, has_more = self._repository.list_users(query=query, limit=limit, page=page)
        return {
            "success": True,
            "query": query or "",
            "page": page,
            "count": len(users),
            "hasMore": has_more,
            "users": users,
        }

    def get_user_detail(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        user = self._repository.get_user_detail(user_id)
        user["subscription"] = self._safe_entitlement(user_id)
        user["scanUsage"] = self._safe_scan_usage(user_id)
        return {
            "success": True,
            "user": user,
        }

    def update_collector_profile(
        self,
        *,
        user_id: str,
        display_name: str | None = None,
        country_code: str | None = None,
        preferred_currency: str | None = None,
    ) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        fields: dict[str, Any] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if country_code is not None:
            fields["country_code"] = country_code
        if preferred_currency is not None:
            fields["preferred_currency"] = preferred_currency
        if not fields:
            raise AdminUserServiceError("No editable profile fields were supplied.")
        profile = self._repository.update_collector_profile(user_id=user_id, fields=fields)
        return {"success": True, "userId": user_id, "collectorProfile": _compact_collector_profile(profile)}

    def update_wishlist_entry(self, *, user_id: str, item_id: str, status: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        entry = self._repository.update_wishlist_entry(user_id=user_id, item_id=item_id, status=status)
        return {"success": True, "userId": user_id, "itemId": item_id, "entry": _compact_wishlist_entry(entry)}

    def delete_wishlist_entry(self, *, user_id: str, item_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        # Soft delete, matching the app's own delete semantics for this table
        # (see collector_wishlist_entries: `deleted` flag, never a hard DELETE)
        # so a stray sync from the device can't resurrect the entry.
        self._repository.delete_wishlist_entry(user_id=user_id, item_id=item_id)
        return {"success": True, "userId": user_id, "itemId": item_id, "deleted": True}

    def update_price_alert(self, *, user_id: str, alert_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        allowed = {"enabled", "status", "target_amount", "percentage", "message"}
        payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not payload:
            raise AdminUserServiceError("No editable alert fields were supplied.")
        alert = self._repository.update_price_alert(user_id=user_id, alert_id=alert_id, fields=payload)
        return {"success": True, "userId": user_id, "alertId": alert_id, "alert": _compact_price_alert(alert)}

    def delete_price_alert(self, *, user_id: str, alert_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        # Soft delete, matching the app's own delete semantics for this table
        # (status='paused', enabled=false) rather than a hard DELETE.
        self._repository.update_price_alert(
            user_id=user_id, alert_id=alert_id, fields={"status": "paused", "enabled": False}
        )
        return {"success": True, "userId": user_id, "alertId": alert_id, "deleted": True}

    def override_subscription(self, *, user_id: str, plan: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        try:
            entitlement = self._subscription_service.verify_and_grant(
                user_id=user_id,
                plan=plan,
                source="admin_override",
                purchase_token=None,
            )
        except SubscriptionServiceError as error:
            raise AdminUserServiceError(str(error)) from error
        return {"success": True, "userId": user_id, "subscription": entitlement}

    def reset_scan_usage(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        try:
            result = self._subscription_service.reset_scan_usage(user_id)
        except SubscriptionServiceError as error:
            raise AdminUserServiceError(str(error)) from error
        return {
            "success": True,
            "scanUsage": {
                **result,
                "limit": settings.subscription_free_monthly_scan_limit,
            },
        }

    def _safe_entitlement(self, user_id: str) -> dict[str, Any]:
        try:
            return self._subscription_service.get_entitlement(user_id)
        except SubscriptionServiceError:
            return {"plan": "unknown", "status": "unknown", "source": "unknown", "currentPeriodEnd": None}

    def _safe_scan_usage(self, user_id: str) -> dict[str, Any]:
        try:
            return self._subscription_service.get_scan_usage(
                user_id,
                free_monthly_limit=settings.subscription_free_monthly_scan_limit,
            )
        except SubscriptionServiceError:
            return {
                "used": None,
                "limit": settings.subscription_free_monthly_scan_limit,
                "periodStart": None,
            }

    def resend_confirmation(self, *, email: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.resend_confirmation(email=email)
        return {"success": True, "email": email, "status": "confirmation_requested"}

    def force_logout(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.force_logout(user_id)
        return {"success": True, "userId": user_id, "status": "logout_requested"}

    def disable_user(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.update_auth_user(user_id, {"ban_duration": "876000h"})
        return {"success": True, "userId": user_id, "status": "disabled"}

    def enable_user(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.update_auth_user(user_id, {"ban_duration": "none"})
        return {"success": True, "userId": user_id, "status": "enabled"}

    def update_admin_role(self, *, user_id: str, role: str, is_admin: bool) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        profile = self._repository.update_profile_role(user_id=user_id, role=role, is_admin=is_admin)
        return {
            "success": True,
            "userId": user_id,
            "role": profile.get("role") or role,
            "isAdmin": bool(profile.get("is_admin", is_admin)),
            "profile": profile,
        }

    def list_team(self) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        from app.routers.admin_auth import ROLE_PERMISSIONS  # local import: avoids a router->service->router cycle at module load time

        profiles = self._repository.list_elevated_profiles()
        audit = self._audit_service
        members = []
        for profile in profiles:
            user_id = str(profile.get("id") or "")
            if not user_id:
                continue
            auth_user = self._repository._get_auth_user(user_id) or {}
            email = str(auth_user.get("email") or "")
            collector_profile = self._repository._optional_collector_profile(user_id)
            role = str(profile.get("role") or "user")
            members.append(
                {
                    "id": user_id,
                    "email": email,
                    "displayName": collector_profile.get("display_name") or email or "Unknown",
                    "role": role,
                    "isAdmin": bool(profile.get("is_admin", False)),
                    "permissions": sorted(ROLE_PERMISSIONS.get(role, set())),
                    "lastAdminAction": _safe_last_audit_event(audit, actor=email),
                    "addedAt": _safe_first_role_grant(audit, target_id=user_id),
                }
            )
        return {"success": True, "count": len(members), "members": members}

    def invite_team_member(self, *, email: str, role: str, is_admin: bool) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        auth_user = self._repository.invite_user(email=email)
        user_id = str(auth_user.get("id") or "")
        if not user_id:
            raise AdminUserServiceError("Supabase invite response did not include a user id.")
        profile = self._repository.upsert_profile_role(user_id=user_id, role=role, is_admin=is_admin)
        return {
            "success": True,
            "userId": user_id,
            "email": email,
            "role": profile.get("role") or role,
            "isAdmin": bool(profile.get("is_admin", is_admin)),
        }


def _safe_last_audit_event(audit: AdminAuditService, *, actor: str) -> dict[str, Any] | None:
    # A team member with no email (invite still pending, or the auth
    # lookup failed) has nothing to query by -- avoid matching every
    # actor="" event in the log, which would misattribute someone else's
    # action.
    if not actor:
        return None
    try:
        payload = audit.list_events(actor=actor, limit=1)
    except AdminAuditError:
        return None
    events = payload.get("events") or []
    if not events:
        return None
    event = events[0]
    return {"action": event.get("action"), "createdAt": event.get("createdAt")}


def _safe_first_role_grant(audit: AdminAuditService, *, target_id: str) -> str | None:
    # profiles has no created_at/added_at column (confirmed: the only
    # migration touching it just adds role/is_admin) -- this is a
    # best-effort proxy using the earliest role-change audit event for
    # this user, not a guaranteed-accurate "added" date. Accounts granted
    # a role before audit logging existed (or outside this endpoint) will
    # have no such event and show nothing here, rather than a fabricated
    # date.
    if not target_id:
        return None
    try:
        payload = audit.list_events(
            target_id=target_id, action="admin_users.role_updated", status="success", limit=1, oldest_first=True,
        )
    except AdminAuditError:
        return None
    events = payload.get("events") or []
    return events[0].get("createdAt") if events else None


class SupabaseAdminUserRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
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
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def list_users(self, *, query: str | None, limit: int, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
        normalized_query = (query or "").strip().lower()
        params = {"per_page": str(limit), "page": str(max(1, page))}
        if normalized_query and "@" in normalized_query:
            params["email"] = normalized_query

        payload = self._request("GET", "/auth/v1/admin/users", params=params)
        raw_users = _users_from_payload(payload)
        # GoTrue's admin list-users endpoint doesn't return a total count, so
        # "has another page" is inferred from getting a full page back — a
        # partial/empty page means this was the last one. A free-text (non-
        # email) query only filters within the fetched page, since GoTrue has
        # no server-side substring search; use `@`-prefixed email search in
        # the global search bar to reach a specific user beyond page one.
        has_more = len(raw_users) >= limit
        if normalized_query and "@" not in normalized_query:
            raw_users = [
                user
                for user in raw_users
                if normalized_query in str(user.get("id") or "").lower()
                or normalized_query in str(user.get("email") or "").lower()
            ]
        raw_users = raw_users[:limit]

        # Enrich the whole page with 6 batched requests total instead of up
        # to 6 sequential requests PER user — this is the fix for a 50-user
        # page previously making ~300 round-trips to Supabase.
        user_ids = [str(u.get("id")) for u in raw_users if u.get("id")]
        profiles_by_id = self._batch_profiles_by_id(
            user_ids, table=settings.admin_profile_table or "profiles", id_column="id",
        )
        collector_profiles_by_id = self._batch_profiles_by_id(
            user_ids, table="collector_profiles", id_column="user_id",
        )
        portfolio_counts = self._batch_counts("portfolio_items", user_ids)
        scan_counts = self._batch_counts("scan_analysis_events", user_ids)
        device_counts = self._batch_counts("push_device_registrations", user_ids)
        subscriptions_by_user = self._batch_first_row_by_user("user_subscriptions", user_ids)

        return [
            self._admin_user_from_auth_user(
                user,
                profile=profiles_by_id.get(str(user.get("id")), {}),
                collector_profile=collector_profiles_by_id.get(str(user.get("id")), {}),
                portfolio_count=portfolio_counts.get(str(user.get("id")), 0),
                scan_count=scan_counts.get(str(user.get("id")), 0),
                device_count=device_counts.get(str(user.get("id")), 0),
                subscription_row=subscriptions_by_user.get(str(user.get("id")), {}),
            )
            for user in raw_users
        ], has_more

    def get_user_detail(self, user_id: str) -> dict[str, Any]:
        auth_user = self._get_auth_user(user_id)
        if auth_user is None:
            raise AdminUserServiceError(f"Admin user {user_id} was not found.")
        summary = self._admin_user_from_auth_user(auth_user)
        portfolio_items = self._optional_rows("portfolio_items", user_id, limit=10)
        scan_events = self._optional_rows("scan_analysis_events", user_id, limit=10)
        price_alerts = self._optional_rows("price_alerts", user_id, limit=20)
        push_devices = self._optional_rows("push_device_registrations", user_id, limit=20)
        wishlist_entries = self._optional_wishlist(user_id, limit=20)
        valuation_history = self._optional_valuation_history(user_id, limit=30)
        # The snapshot rows only ever stored portfolio_item_id, not the
        # item's title, so the valuation history panel showed no way to
        # tell which item each entry belonged to. History spans more items
        # over time than the 10 "recent" portfolio_items fetched above
        # cover, so this looks titles up directly by the ids actually
        # referenced in the history rows.
        history_item_ids = list({
            str(row["portfolio_item_id"])
            for row in valuation_history
            if row.get("portfolio_item_id")
        })
        history_item_titles = (
            self._batch_profiles_by_id(history_item_ids, table="portfolio_items", id_column="id")
            if history_item_ids
            else {}
        )
        push_deliveries = self._optional_push_deliveries(user_id, limit=10)
        pricing_review_items = [
            item
            for item in portfolio_items
            if bool(item.get("needs_review") or item.get("requires_review"))
            or _pricing_confidence(item) < 70
            or _pricing_value(item) <= 0
        ]
        total_value = sum(_pricing_value(item) for item in portfolio_items)
        collector_profile = self._optional_collector_profile(user_id)
        compact_collector_profile = _compact_collector_profile(collector_profile)
        compact_collector_profile["avatarSignedUrl"] = self._signed_avatar_url(
            collector_profile.get("avatar_path")
        )
        return {
            **summary,
            "profile": self._optional_profile(user_id),
            "collectorProfile": compact_collector_profile,
            "portfolioValue": round(total_value, 2),
            "recentPortfolioItems": [_compact_portfolio_item(item) for item in portfolio_items],
            "recentScans": [_compact_scan_event(event) for event in scan_events],
            "pricingReviewItems": [_compact_portfolio_item(item) for item in pricing_review_items[:10]],
            "priceAlerts": [_compact_price_alert(alert) for alert in price_alerts],
            "pushDevices": [_compact_push_device(device) for device in push_devices],
            "wishlistEntries": [_compact_wishlist_entry(entry) for entry in wishlist_entries],
            "valuationHistory": [
                _compact_valuation_snapshot(row, item_titles=history_item_titles) for row in valuation_history
            ],
            "pushDeliveries": [_compact_push_delivery(row) for row in push_deliveries],
        }

    def _admin_user_from_auth_user(
        self,
        user: dict[str, Any],
        *,
        profile: dict[str, Any] | None = None,
        collector_profile: dict[str, Any] | None = None,
        portfolio_count: int | None = None,
        scan_count: int | None = None,
        device_count: int | None = None,
        subscription_row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = str(user.get("id") or "")
        email = str(user.get("email") or "")
        # Two distinct profile tables, deliberately queried separately:
        # `profiles` (admin_profile_table) holds console role/is_admin only;
        # `collector_profiles` is what the mobile app actually writes
        # (display name, avatar, country, currency). Reading displayName from
        # `profiles` was a real bug — that table is never populated by the
        # app, so every real collector's name showed blank in admin.
        #
        # Every pre-fetched value below is optional: the single-user detail
        # path (get_user_detail) calls this with nothing pre-fetched and
        # falls back to one-off requests, since it's inherently a single
        # user and there's no N+1 concern there. The list path
        # (list_users) fetches all of this in one batch per field across
        # the whole page and passes it in, so a 50-user page load doesn't
        # turn into ~300 sequential Supabase requests.
        if profile is None:
            profile = self._optional_profile(user_id)
        if collector_profile is None:
            collector_profile = self._optional_collector_profile(user_id)
        if portfolio_count is None:
            portfolio_count = self._optional_count("portfolio_items", user_id)
        if scan_count is None:
            scan_count = self._optional_count("scan_analysis_events", user_id)
        if device_count is None:
            device_count = self._optional_count("push_device_registrations", user_id)
        if subscription_row is None:
            subscription_rows = self._optional_rows("user_subscriptions", user_id, limit=1)
            subscription_row = subscription_rows[0] if subscription_rows else {}
        return {
            "id": user_id,
            "email": email,
            "createdAt": user.get("created_at"),
            "lastSignInAt": user.get("last_sign_in_at"),
            "emailConfirmedAt": user.get("email_confirmed_at") or user.get("confirmed_at"),
            "authStatus": _auth_status(user),
            "displayName": collector_profile.get("display_name") or "",
            "portfolioCount": portfolio_count,
            "scanCount": scan_count,
            "pushDeviceCount": device_count,
            "plan": subscription_row.get("plan") or "free",
            "planStatus": subscription_row.get("status") or "active",
            # Console access (role/isAdmin) is unrelated to the subscription
            # plan above — an admin can be on any plan. Sourced from the same
            # profile row already fetched for displayName, so this costs no
            # extra request.
            "role": profile.get("role") or "user",
            "isAdmin": bool(profile.get("is_admin", False)),
            "lastActivityAt": _latest_text(
                user.get("last_sign_in_at"),
                collector_profile.get("updated_at"),
            ),
        }

    def _get_auth_user(self, user_id: str) -> dict[str, Any] | None:
        payload = self._request("GET", f"/auth/v1/admin/users/{user_id}")
        if isinstance(payload, dict) and payload.get("id"):
            return payload
        return None

    def resend_confirmation(self, *, email: str) -> None:
        self._request(
            "POST",
            "/auth/v1/resend",
            json_payload={"type": "signup", "email": email},
        )

    def force_logout(self, user_id: str) -> None:
        self._request("POST", f"/auth/v1/admin/users/{user_id}/logout")

    def update_auth_user(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PUT",
            f"/auth/v1/admin/users/{user_id}",
            json_payload=payload,
        )
        if not isinstance(response, dict):
            raise AdminUserServiceError("Supabase admin user update response shape was invalid.")
        return response

    def update_profile_role(self, *, user_id: str, role: str, is_admin: bool) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/rest/v1/{settings.admin_profile_table or 'profiles'}",
            params={"id": f"eq.{user_id}", "select": "*"},
            json_payload={"role": role, "is_admin": is_admin},
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminUserServiceError("Supabase admin profile update response shape was invalid.")

    def list_elevated_profiles(self) -> list[dict[str, Any]]:
        # "Elevated access" = role != the default "user", OR is_admin is
        # set directly (belt-and-braces in case a row has is_admin=true
        # without role having been set to "admin" to match).
        payload = self._request(
            "GET",
            f"/rest/v1/{settings.admin_profile_table or 'profiles'}",
            params={"or": "(role.neq.user,is_admin.eq.true)", "select": "id,role,is_admin", "order": "role.asc"},
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict) and row.get("id")]

    def invite_user(self, *, email: str) -> dict[str, Any]:
        # GoTrue's admin invite endpoint creates the auth.users row and
        # emails the person a signup link -- no admin-set password to
        # generate or communicate out-of-band.
        payload = self._request(
            "POST",
            "/auth/v1/invite",
            json_payload={"email": email},
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise AdminUserServiceError("Supabase invite response shape was invalid.")
        return payload

    def upsert_profile_role(self, *, user_id: str, role: str, is_admin: bool) -> dict[str, Any]:
        # Upsert, not a plain PATCH like update_profile_role -- right after
        # an invite there's a race between GoTrue creating the auth.users
        # row and any DB trigger that creates the matching profiles row. A
        # PATCH would silently match zero rows if the profile doesn't
        # exist yet.
        payload = self._request(
            "POST",
            f"/rest/v1/{settings.admin_profile_table or 'profiles'}",
            json_payload={"id": user_id, "role": role, "is_admin": is_admin},
            extra_headers={"Prefer": "return=representation,resolution=merge-duplicates"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminUserServiceError("Supabase admin profile upsert response shape was invalid.")

    def _optional_profile(self, user_id: str) -> dict[str, Any]:
        if not user_id:
            return {}
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{settings.admin_profile_table or 'profiles'}",
                params={"id": f"eq.{user_id}", "select": "*", "limit": "1"},
            )
        except AdminUserServiceError:
            return {}
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return {}

    def _optional_count(self, table: str, user_id: str) -> int | None:
        # Uses PostgREST's exact-count header instead of fetching rows to
        # count them client-side — the old approach pulled up to 1000 full
        # rows per user just to call len() on the result, which meant a
        # 50-user list page could trigger hundreds of oversized requests.
        if not user_id:
            return None
        try:
            response = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={"user_id": f"eq.{user_id}", "select": "user_id", "limit": "1"},
                extra_headers={"Prefer": "count=exact"},
                return_response=True,
            )
        except AdminUserServiceError:
            return None
        content_range = response.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
        return int(total) if total.isdigit() else None

    @staticmethod
    def _in_filter(user_ids: list[str]) -> str:
        # PostgREST's "in" operator: in.(id1,id2,...). No quoting needed —
        # UUIDs never contain commas or parentheses.
        return "in.(" + ",".join(user_ids) + ")"

    def _batch_profiles_by_id(self, user_ids: list[str], *, table: str, id_column: str) -> dict[str, dict[str, Any]]:
        # One request across the whole page instead of one per user. Used
        # for both `profiles` (keyed by id) and `collector_profiles` (keyed
        # by user_id) — same shape, different key column.
        if not user_ids:
            return {}
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={id_column: self._in_filter(user_ids), "select": "*"},
            )
        except AdminUserServiceError:
            return {}
        if not isinstance(payload, list):
            return {}
        return {
            str(row.get(id_column)): row
            for row in payload
            if isinstance(row, dict) and row.get(id_column)
        }

    def _batch_counts(self, table: str, user_ids: list[str]) -> dict[str, int]:
        # One request across the whole page — fetches only the user_id
        # column (not full rows) for every matching row, then counts them
        # per user in Python. Cheaper than N separate count=exact requests
        # and cheaper than fetching full rows, at the cost of one response
        # sized to the page's total row count in that table (still far
        # smaller than fetching up to 1000 full rows per user).
        if not user_ids:
            return {}
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={"user_id": self._in_filter(user_ids), "select": "user_id", "limit": "10000"},
            )
        except AdminUserServiceError:
            return {}
        if not isinstance(payload, list):
            return {}
        counts: dict[str, int] = {}
        for row in payload:
            if isinstance(row, dict) and row.get("user_id"):
                key = str(row["user_id"])
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _batch_first_row_by_user(self, table: str, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        # One request across the whole page, keeping only the first row
        # seen per user_id (matches the single-user `_optional_rows(...,
        # limit=1)` semantics this replaces for the list endpoint).
        if not user_ids:
            return {}
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={"user_id": self._in_filter(user_ids), "select": "*"},
            )
        except AdminUserServiceError:
            return {}
        if not isinstance(payload, list):
            return {}
        first_by_user: dict[str, dict[str, Any]] = {}
        for row in payload:
            if isinstance(row, dict) and row.get("user_id"):
                key = str(row["user_id"])
                first_by_user.setdefault(key, row)
        return first_by_user

    def _optional_rows(self, table: str, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        if not user_id:
            return []
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "*",
                    "limit": str(limit),
                    "order": "updated_at.desc.nullslast,created_at.desc.nullslast",
                },
            )
        except AdminUserServiceError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _optional_collector_profile(self, user_id: str) -> dict[str, Any]:
        # collector_profiles is the table the mobile app actually writes
        # (display name, avatar, country, currency) — distinct from the
        # admin_profile_table (`profiles`) used for console role/is_admin.
        # Its primary key is `user_id`, not `id`.
        if not user_id:
            return {}
        try:
            payload = self._request(
                "GET",
                "/rest/v1/collector_profiles",
                params={"user_id": f"eq.{user_id}", "select": "*", "limit": "1"},
            )
        except AdminUserServiceError:
            return {}
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return {}

    def _signed_avatar_url(self, avatar_path: str | None, *, expires_in: int = 3600) -> str | None:
        if not avatar_path:
            return None
        try:
            payload = self._request(
                "POST",
                f"/storage/v1/object/sign/{AVATAR_STORAGE_BUCKET}/{avatar_path}",
                json_payload={"expiresIn": expires_in},
            )
        except AdminUserServiceError:
            return None
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

    def _optional_wishlist(self, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        if not user_id:
            return []
        try:
            payload = self._request(
                "GET",
                "/rest/v1/collector_wishlist_entries",
                params={
                    "user_id": f"eq.{user_id}",
                    "deleted": "eq.false",
                    "select": "*",
                    "limit": str(limit),
                    "order": "updated_at.desc.nullslast",
                },
            )
        except AdminUserServiceError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _optional_valuation_history(self, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        if not user_id:
            return []
        try:
            payload = self._request(
                "GET",
                "/rest/v1/portfolio_valuation_snapshots",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "*",
                    "limit": str(limit),
                    "order": "priced_at.desc",
                },
            )
        except AdminUserServiceError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _optional_push_deliveries(self, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        if not user_id:
            return []
        try:
            payload = self._request(
                "GET",
                "/rest/v1/push_notification_deliveries",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "*",
                    "limit": str(limit),
                    "order": "created_at.desc",
                },
            )
        except AdminUserServiceError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def update_collector_profile(self, *, user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            "/rest/v1/collector_profiles",
            params={"user_id": f"eq.{user_id}", "select": "*"},
            json_payload=fields,
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminUserServiceError("Supabase collector profile update response shape was invalid.")

    def update_wishlist_entry(self, *, user_id: str, item_id: str, status: str) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            "/rest/v1/collector_wishlist_entries",
            params={"user_id": f"eq.{user_id}", "portfolio_item_id": f"eq.{item_id}", "select": "*"},
            json_payload={"status": status},
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminUserServiceError(f"Wishlist entry {item_id} was not found for this user.")

    def delete_wishlist_entry(self, *, user_id: str, item_id: str) -> None:
        payload = self._request(
            "PATCH",
            "/rest/v1/collector_wishlist_entries",
            params={"user_id": f"eq.{user_id}", "portfolio_item_id": f"eq.{item_id}", "select": "*"},
            json_payload={"deleted": True},
            extra_headers={"Prefer": "return=representation"},
        )
        if not (isinstance(payload, list) and payload):
            raise AdminUserServiceError(f"Wishlist entry {item_id} was not found for this user.")

    def update_price_alert(self, *, user_id: str, alert_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            "/rest/v1/price_alerts",
            params={"user_id": f"eq.{user_id}", "id": f"eq.{alert_id}", "select": "*"},
            json_payload=fields,
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminUserServiceError(f"Price alert {alert_id} was not found for this user.")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        return_response: bool = False,
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
            if return_response:
                return response
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AdminUserServiceError("Supabase admin user request failed.") from error
        finally:
            if should_close:
                client.close()


def _users_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_users = payload.get("users", [])
    elif isinstance(payload, list):
        raw_users = payload
    else:
        raise AdminUserServiceError("Supabase admin users response shape was invalid.")
    if not isinstance(raw_users, list):
        raise AdminUserServiceError("Supabase admin users response shape was invalid.")
    return [user for user in raw_users if isinstance(user, dict)]


def _auth_status(user: dict[str, Any]) -> str:
    if user.get("deleted_at"):
        return "deleted"
    if user.get("banned_until"):
        return "banned"
    if user.get("email_confirmed_at") or user.get("confirmed_at"):
        return "confirmed"
    return "unconfirmed"


def _latest_text(*values: Any) -> str | None:
    strings = [str(value) for value in values if value]
    return max(strings) if strings else None


def _resolve_pricing_dict(row: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    # The scheduled repricing sweep (batch_repricing_service.py) only ever
    # writes pricing into raw_json.pricing -- never the top-level pricing/
    # data columns these admin reads originally checked. Any item priced by
    # that sweep (valuationStatus "market_estimated"/"catalog_lookup") was
    # silently computing as $0/0% confidence here despite being correctly
    # priced -- live-confirmed via a real user whose valuation history
    # showed real recent prices while this page's totals showed $0.
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else data.get("pricing")
    if not isinstance(pricing, dict):
        pricing = raw.get("pricing")
    return pricing if isinstance(pricing, dict) else {}


def _compact_portfolio_item(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pricing = _resolve_pricing_dict(row, data)
    return {
        "id": row.get("id") or data.get("id"),
        "title": (
            row.get("title")
            or row.get("item_name")
            or data.get("title")
            or data.get("itemName")
            or "Untitled"
        ),
        "category": row.get("category") or data.get("category") or "Unknown",
        "price": _pricing_value(row),
        "currency": pricing.get("currency") or row.get("currency") or data.get("currency") or "USD",
        "confidence": _pricing_confidence(row),
        "needsReview": bool(row.get("needs_review") or data.get("needsReview") or data.get("requiresReview")),
        "updatedAt": row.get("updated_at") or data.get("updatedAt") or data.get("lastUpdated"),
    }


def _compact_scan_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    return {
        "id": row.get("id") or payload.get("id"),
        "title": row.get("title") or payload.get("title") or payload.get("itemName") or "Unknown scan",
        "status": row.get("status") or payload.get("status") or "unknown",
        "provider": row.get("provider") or payload.get("provider") or payload.get("aiProvider") or "Unknown",
        "confidence": row.get("confidence") or payload.get("confidence"),
        "createdAt": row.get("created_at") or payload.get("createdAt"),
    }


def _compact_price_alert(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "itemTitle": row.get("item_title") or "Item",
        "portfolioItemId": row.get("portfolio_item_id"),
        "ruleType": row.get("rule_type"),
        "targetAmount": row.get("target_amount"),
        "percentage": row.get("percentage"),
        "enabled": bool(row.get("enabled")),
        "status": row.get("status") or "unknown",
        "triggeredAt": row.get("triggered_at"),
        "updatedAt": row.get("updated_at"),
    }


def _compact_push_device(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "platform": row.get("platform") or row.get("device_type") or "unknown",
        "enabled": bool(row.get("enabled")),
        "status": row.get("status") or ("enabled" if row.get("enabled") else "disabled"),
        "lastSeenAt": row.get("last_seen_at"),
        "updatedAt": row.get("updated_at"),
    }


def _compact_collector_profile(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "displayName": row.get("display_name") or "",
        "avatarPath": row.get("avatar_path"),
        "countryCode": row.get("country_code"),
        "preferredCurrency": row.get("preferred_currency"),
        "updatedAt": row.get("updated_at"),
    }


def _compact_wishlist_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "portfolioItemId": row.get("portfolio_item_id"),
        "title": row.get("title") or "Item",
        "category": row.get("category") or "Unknown",
        "status": row.get("status") or "owned",
        "updatedAt": row.get("updated_at"),
    }


def _compact_valuation_snapshot(
    row: dict[str, Any], *, item_titles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item = (item_titles or {}).get(str(row.get("portfolio_item_id")))
    item_title = (item.get("title") or item.get("item_name")) if item else None
    return {
        "id": row.get("id"),
        "portfolioItemId": row.get("portfolio_item_id"),
        "itemTitle": item_title or "Deleted or unknown item",
        "valueAud": row.get("value_aud"),
        "displayString": row.get("display_string"),
        "valuationStatus": row.get("valuation_status"),
        "valuationStrategy": row.get("valuation_strategy"),
        "pricedAt": row.get("priced_at"),
    }


def _compact_push_delivery(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "priceAlertId": row.get("price_alert_id"),
        "portfolioItemId": row.get("portfolio_item_id"),
        "title": row.get("title") or "Notification",
        "body": row.get("body"),
        "status": row.get("status") or "unknown",
        "errorMessage": row.get("error_message"),
        "sentAt": row.get("sent_at"),
        "createdAt": row.get("created_at"),
    }


def _pricing_value(row: dict[str, Any]) -> float:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    pricing = _resolve_pricing_dict(row, data)
    value = (
        pricing.get("estimatedMarketValue")
        or pricing.get("marketValue")
        or pricing.get("value")
        or row.get("estimated_value")
        or row.get("estimated_value_low")
        or data.get("estimatedMarketValue")
        or raw.get("estimatedValue")
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _pricing_confidence(row: dict[str, Any]) -> int:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pricing = _resolve_pricing_dict(row, data)
    value = (
        pricing.get("pricingConfidence")
        or pricing.get("confidence")
        or row.get("pricing_confidence")
        or data.get("pricingConfidence")
    )
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 <= numeric <= 1:
        numeric *= 100
    return max(0, min(100, round(numeric)))
