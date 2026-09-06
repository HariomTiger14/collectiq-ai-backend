"""Role-specific permission enforcement on admin routes.

Before this, most admin-console write actions sat behind a broad admin/job
token, so any authenticated admin identity could perform any of them and the
only thing separating a viewer from an owner was the portal hiding buttons.
Hiding a button is not authorisation: the route was still reachable with the
same credentials.

These tests pin the backend half. The denial cases deliberately patch nothing
but auth -- `require_admin_permission` raises inside the dependency, before
the handler runs, so a 403 here proves the route is closed rather than proving
a mock was not called. The allow cases stub the service the handler reaches,
so a non-403 proves the permission gate opened rather than proving the
underlying feature works (that is covered by each feature's own tests).

Roles come from the Supabase profile path, not the static token, because the
static admin token holds FULL_ADMIN_PERMISSIONS by definition and therefore
cannot demonstrate a denial.
"""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin_auth import FULL_ADMIN_PERMISSIONS, ROLE_PERMISSIONS


class _SummaryStub:
    """Mimics the dataclass summaries these job handlers call `.to_dict()` on."""

    def __init__(self, payload: dict):
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload


class _MockResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@contextmanager
def as_role(role: str):
    """Authenticate every request in the block as a Supabase admin with `role`.

    The static token path is disabled (admin_import_token = "") so the request
    is forced down the profile-role branch -- otherwise every role would
    resolve to FULL_ADMIN_PERMISSIONS and no denial could ever be observed.
    """
    with patch("app.routers.admin_auth.settings") as auth_settings, patch(
        "app.routers.admin_auth.httpx.get"
    ) as get_request:
        auth_settings.admin_import_token = ""
        auth_settings.admin_job_token = ""
        auth_settings.supabase_url = "https://packlox.supabase.co"
        auth_settings.supabase_anon_key = "anon-key"
        auth_settings.supabase_service_role_key = "service-role"
        auth_settings.admin_profile_table = "profiles"
        get_request.side_effect = lambda *a, **k: (
            _MockResponse(200, {"id": "admin-user", "email": "admin@packlox.com"})
            if "/auth/v1/user" in a[0]
            else _MockResponse(200, [{"id": "admin-user", "role": role}])
        )
        yield


@contextmanager
def as_job_token(token: str = "job-token"):
    """Authenticate as the scheduled-job identity (cron's credential)."""
    with patch("app.routers.admin_auth.settings") as auth_settings:
        auth_settings.admin_job_token = token
        auth_settings.admin_import_token = token
        yield


AUTH = {"Authorization": "Bearer supabase-session"}
JOB_HEADERS = {"X-Admin-Token": "job-token"}

# (method, path, required permission). Every route changed by this work.
PROTECTED_ROUTES: list[tuple[str, str, str]] = [
    # pricing:write -- human review-queue actions
    ("post", "/admin/pricing/review-queue/item-1/reviewed", "pricing:write"),
    ("post", "/admin/pricing/review-queue/item-1/override", "pricing:write"),
    ("patch", "/admin/pricing/review-queue/item-1/assignment", "pricing:write"),
    ("post", "/admin/pricing/review-queue/item-1/retry", "pricing:write"),
    ("post", "/admin/pricing/review-queue-actions/reviewed", "pricing:write"),
    ("post", "/admin/pricing/review-queue-actions/retry", "pricing:write"),
    # scans:write -- scan failure triage
    ("post", "/admin/scans/failures/scan-1/reviewed", "scans:write"),
    ("post", "/admin/scans/failures/scan-1/resolved", "scans:write"),
    ("post", "/admin/scans/failures/scan-1/retry", "scans:write"),
    ("post", "/admin/scans/failure-actions/reviewed", "scans:write"),
    ("post", "/admin/scans/failure-actions/resolved", "scans:write"),
    # catalog:write
    ("patch", "/admin/catalog/catalog-1", "catalog:write"),
    # users:write -- portfolio, data requests, support writes
    ("patch", "/admin/portfolio/items/item-1", "users:write"),
    ("post", "/admin/data-requests/request-1/process", "users:write"),
    ("post", "/admin/support/tickets/ticket-1/reply", "users:write"),
    ("post", "/admin/support/tickets/ticket-1/status", "users:write"),
    ("post", "/admin/support/messages/message-1/attachments", "users:write"),
    # imports:run
    ("post", "/admin/pricecharting/import", "imports:run"),
    # push:write -- reaches real devices, cannot be recalled
    ("post", "/admin/push/test", "push:write"),
    ("post", "/admin/push/test-price-alert", "push:write"),
    ("post", "/admin/push/broadcast", "push:write"),
    ("post", "/admin/push/users/user-1/send", "push:write"),
    ("post", "/admin/push/devices/device-1/disable", "push:write"),
    # admin:read -- console reads that used to demand a job token
    ("get", "/admin/push/history", "admin:read"),
    ("get", "/admin/push/audience", "admin:read"),
    ("get", "/admin/data-requests", "admin:read"),
    ("get", "/admin/support/tickets", "admin:read"),
    ("get", "/admin/support/tickets/ticket-1", "admin:read"),
]

# Routes a scheduler calls, which must keep accepting the job token.
JOB_ROUTES: list[tuple[str, str]] = [
    ("post", "/admin/pricing/reprice-all"),
    ("post", "/admin/push/price-alerts/evaluate"),
    ("post", "/admin/push/price-alerts/run"),
    ("post", "/admin/data-requests/purge-due"),
]

ALL_ROLES = ("viewer", "support", "pricing_reviewer", "admin", "owner", "super_admin")


class PermissionModelTest(unittest.TestCase):
    def test_push_write_is_a_full_admin_permission(self) -> None:
        self.assertIn("push:write", FULL_ADMIN_PERMISSIONS)

    def test_only_full_admin_roles_may_push(self) -> None:
        for role in ("admin", "owner", "super_admin"):
            with self.subTest(role=role):
                self.assertIn("push:write", ROLE_PERMISSIONS[role])
        for role in ("viewer", "support", "pricing_reviewer"):
            with self.subTest(role=role):
                self.assertNotIn("push:write", ROLE_PERMISSIONS[role])

    def test_viewer_holds_no_write_permission_at_all(self) -> None:
        writes = {p for p in FULL_ADMIN_PERMISSIONS if p.endswith(":write") or p == "imports:run"}
        self.assertEqual(ROLE_PERMISSIONS["viewer"] & writes, set())

    def test_support_cannot_touch_pricing_or_catalog(self) -> None:
        self.assertNotIn("pricing:write", ROLE_PERMISSIONS["support"])
        self.assertNotIn("catalog:write", ROLE_PERMISSIONS["support"])
        self.assertNotIn("imports:run", ROLE_PERMISSIONS["support"])

    def test_pricing_reviewer_cannot_touch_user_owned_data(self) -> None:
        self.assertNotIn("users:write", ROLE_PERMISSIONS["pricing_reviewer"])
        self.assertNotIn("scans:write", ROLE_PERMISSIONS["pricing_reviewer"])


class RouteDenialTest(unittest.TestCase):
    """A role without the permission must be refused at the route."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_roles_without_the_permission_are_refused(self) -> None:
        for method, path, permission in PROTECTED_ROUTES:
            for role in ALL_ROLES:
                if permission in ROLE_PERMISSIONS[role]:
                    continue
                with self.subTest(route=f"{method.upper()} {path}", role=role):
                    with as_role(role):
                        response = getattr(self.client, method)(
                            path, headers=AUTH, json={}
                        )
                    self.assertEqual(
                        response.status_code,
                        403,
                        f"{method.upper()} {path} was reachable by {role}: {response.text}",
                    )
                    self.assertEqual(
                        response.json()["error"]["code"], "admin_permission_denied"
                    )

    def test_viewer_is_refused_every_write_route(self) -> None:
        writes = [r for r in PROTECTED_ROUTES if r[2] != "admin:read"]
        self.assertGreater(len(writes), 0)
        for method, path, _permission in writes:
            with self.subTest(route=f"{method.upper()} {path}"):
                with as_role("viewer"):
                    response = getattr(self.client, method)(path, headers=AUTH, json={})
                self.assertEqual(response.status_code, 403, response.text)

    def test_support_cannot_change_pricing_or_catalog_over_http(self) -> None:
        routes = [
            ("post", "/admin/pricing/review-queue/item-1/override"),
            ("post", "/admin/pricing/review-queue-actions/retry"),
            ("patch", "/admin/catalog/catalog-1"),
            ("post", "/admin/pricecharting/import"),
        ]
        for method, path in routes:
            with self.subTest(route=path):
                with as_role("support"):
                    response = getattr(self.client, method)(path, headers=AUTH, json={})
                self.assertEqual(response.status_code, 403, response.text)

    def test_pricing_reviewer_cannot_mutate_users_portfolio_support_or_data_requests(
        self,
    ) -> None:
        routes = [
            ("patch", "/admin/portfolio/items/item-1"),
            ("post", "/admin/data-requests/request-1/process"),
            ("post", "/admin/support/tickets/ticket-1/reply"),
            ("post", "/admin/support/tickets/ticket-1/status"),
            ("post", "/admin/scans/failures/scan-1/reviewed"),
        ]
        for method, path in routes:
            with self.subTest(route=path):
                with as_role("pricing_reviewer"):
                    response = getattr(self.client, method)(path, headers=AUTH, json={})
                self.assertEqual(response.status_code, 403, response.text)

    def test_push_send_routes_require_push_write(self) -> None:
        """The whole point of adding push:write: no lesser role may send."""
        push_routes = [r for r in PROTECTED_ROUTES if r[2] == "push:write"]
        self.assertEqual(len(push_routes), 5)
        for method, path, _ in push_routes:
            for role in ("viewer", "support", "pricing_reviewer"):
                with self.subTest(route=path, role=role):
                    with as_role(role):
                        response = getattr(self.client, method)(
                            path, headers=AUTH, json={}
                        )
                    self.assertEqual(response.status_code, 403, response.text)


class RouteAllowTest(unittest.TestCase):
    """A role holding the permission must get past the gate.

    Each case stubs the service the handler reaches, so the assertion is about
    the permission check opening -- not about the feature behind it.
    """

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _assert_passes_gate(self, response) -> None:
        self.assertNotIn(
            response.status_code, (401, 403), f"gate closed unexpectedly: {response.text}"
        )

    def test_admin_may_mark_a_review_queue_item_reviewed(self) -> None:
        with as_role("admin"), patch(
            "app.routers.admin_pricing.AdminPricingReviewQueueService"
        ) as service, patch("app.routers.admin_pricing.AdminAuditService"):
            service.return_value.mark_reviewed.return_value = {"success": True}
            response = self.client.post(
                "/admin/pricing/review-queue/item-1/reviewed", headers=AUTH, json={}
            )
        self._assert_passes_gate(response)

    def test_owner_may_mark_a_scan_failure_reviewed(self) -> None:
        with as_role("owner"), patch(
            "app.routers.admin_scans.AdminScanFailureService"
        ) as service, patch("app.routers.admin_scans.AdminAuditService"):
            service.return_value.mark_reviewed.return_value = {"success": True}
            response = self.client.post(
                "/admin/scans/failures/scan-1/reviewed", headers=AUTH, json={}
            )
        self._assert_passes_gate(response)

    def test_support_may_mark_a_scan_failure_reviewed(self) -> None:
        """support holds scans:write, so this must NOT be a denial."""
        with as_role("support"), patch(
            "app.routers.admin_scans.AdminScanFailureService"
        ) as service, patch("app.routers.admin_scans.AdminAuditService"):
            service.return_value.mark_reviewed.return_value = {"success": True}
            response = self.client.post(
                "/admin/scans/failures/scan-1/reviewed", headers=AUTH, json={}
            )
        self._assert_passes_gate(response)

    def test_pricing_reviewer_may_edit_catalog_metadata(self) -> None:
        """pricing_reviewer holds catalog:write."""
        with as_role("pricing_reviewer"), patch(
            "app.routers.admin_catalog.AdminCatalogService"
        ) as service, patch("app.routers.admin_catalog.AdminAuditService"):
            service.return_value.update_item.return_value = {"success": True}
            response = self.client.patch(
                "/admin/catalog/catalog-1",
                headers=AUTH,
                json={"note": "corrected title", "title": "Charizard"},
            )
        self._assert_passes_gate(response)

    def test_admin_may_send_a_push_broadcast(self) -> None:
        with as_role("admin"), patch(
            "app.routers.push.PriceAlertPushService"
        ) as service:
            service.return_value.dispatch_broadcast.return_value = {"success": True}
            response = self.client.post(
                "/admin/push/broadcast",
                headers=AUTH,
                json={"title": "t", "body": "b", "segment": "all"},
            )
        self._assert_passes_gate(response)

    def test_viewer_may_read_the_push_delivery_history(self) -> None:
        """admin:read routes must be reachable without a job token."""
        with as_role("viewer"), patch(
            "app.routers.push.PriceAlertPushService"
        ) as service:
            service.return_value.delivery_history.return_value = {"success": True}
            response = self.client.get("/admin/push/history", headers=AUTH)
        self._assert_passes_gate(response)

    def test_viewer_may_read_the_support_queue(self) -> None:
        with as_role("viewer"), patch("app.routers.support._service") as service:
            service.list_tickets.return_value = {"success": True, "tickets": []}
            response = self.client.get("/admin/support/tickets", headers=AUTH)
        self._assert_passes_gate(response)

    def test_viewer_may_read_the_data_request_queue(self) -> None:
        with as_role("viewer"), patch("app.routers.data_requests._service") as service:
            service.list_requests.return_value = {"success": True, "requests": []}
            response = self.client.get("/admin/data-requests", headers=AUTH)
        self._assert_passes_gate(response)

    def test_admin_may_process_a_data_request(self) -> None:
        with as_role("admin"), patch("app.routers.data_requests._service") as service:
            service.process_request.return_value = {"success": True}
            response = self.client.post(
                "/admin/data-requests/request-1/process", headers=AUTH, json={}
            )
        self._assert_passes_gate(response)

    def test_support_may_reply_to_a_ticket(self) -> None:
        with as_role("support"), patch("app.routers.support._service") as service:
            service.reply_as_admin.return_value = {"success": True}
            response = self.client.post(
                "/admin/support/tickets/ticket-1/reply",
                headers=AUTH,
                json={"body": "looking into it"},
            )
        self._assert_passes_gate(response)


class ScheduledJobRouteTest(unittest.TestCase):
    """Cron routes must keep working with the job token, unchanged."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_job_token_still_accepted_on_scheduled_routes(self) -> None:
        reprice_summary = {
            "scanned": 0,
            "repriced": 0,
            "unavailable": 0,
            "skipped": 0,
            "rateLimited": 0,
            "errors": [],
        }
        # (patch target, attribute, return value). The handlers read specific
        # keys off these payloads, so a bare MagicMock would fail response
        # validation and mask what this test is actually asserting.
        patches = {
            "/admin/pricing/reprice-all": (
                "app.routers.admin_pricing.BatchRepricingService",
                "reprice_all",
                _SummaryStub(reprice_summary),
            ),
            "/admin/push/price-alerts/evaluate": (
                "app.routers.push.PriceAlertEvaluationService",
                "evaluate_and_flag",
                _SummaryStub({"evaluated": 0, "triggered": 0, "rearmed": 0, "triggeredAlerts": []}),
            ),
            "/admin/push/price-alerts/run": (
                "app.routers.push.PriceAlertPushService",
                "dispatch_triggered_alerts",
                _SummaryStub({"scannedAlerts": 0, "attemptedDeliveries": 0}),
            ),
            "/admin/data-requests/purge-due": (
                "app.routers.data_requests._service",
                "purge_due",
                {"success": True},
            ),
        }
        for method, path in JOB_ROUTES:
            target, attr, result = patches[path]
            with self.subTest(route=path):
                with as_job_token(), patch(target) as service, patch(
                    "app.routers.admin_pricing.AdminAuditService"
                ), patch("app.routers.push.PriceAlertEvaluationService") as evaluator:
                    evaluator.return_value.evaluate_and_flag.return_value = _SummaryStub(
                        {"evaluated": 0, "triggered": 0, "rearmed": 0, "triggeredAlerts": []}
                    )
                    if target.endswith("_service"):
                        getattr(service, attr).return_value = result
                    else:
                        getattr(service.return_value, attr).return_value = result
                    response = getattr(self.client, method)(path, headers=JOB_HEADERS, json={})
                self.assertNotIn(
                    response.status_code,
                    (401, 403),
                    f"{path} rejected the job token: {response.text}",
                )

    def test_scheduled_routes_still_reject_an_unknown_token(self) -> None:
        for method, path in JOB_ROUTES:
            with self.subTest(route=path):
                with as_job_token():
                    response = getattr(self.client, method)(
                        path, headers={"X-Admin-Token": "wrong-token"}, json={}
                    )
                self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
