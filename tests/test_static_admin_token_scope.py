"""What the static ADMIN_IMPORT_TOKEN may and may not do.

It used to resolve to FULL_ADMIN_PERMISSIONS, which made it owner-equivalent.
Role-specific permissions (#196) hardened Supabase admin *sessions* only —
anyone holding this single shared env-var secret still bypassed the role model
entirely and could price items, edit users, resolve scans and push to every
registered device.

It now holds exactly what the documented operational tasks need:

    admin:read, audit:read, imports:run

ADMIN_JOB_TOKEN is deliberately unchanged. Cron routes authenticate with the
token itself rather than a permission, and narrowing it is a separate decision
with a different blast radius.

Nothing here rotates a token. This changes what the credential may do, not
what it is.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin_auth import (
    FULL_ADMIN_PERMISSIONS,
    ROLE_PERMISSIONS,
    STATIC_IMPORT_TOKEN_PERMISSIONS,
    _static_admin,
)
from tests.admin_auth_helpers import (
    ADMIN_HEADERS,
    console_admin,
    static_import_token,
    static_job_token,
)


# One route per permission the static token must NOT hold.
WRITE_ROUTES = [
    ("pricing:write", "post", "/admin/pricing/review-queue/item-1/reviewed"),
    ("catalog:write", "patch", "/admin/catalog/catalog-1"),
    ("users:write", "patch", "/admin/portfolio/items/item-1"),
    ("scans:write", "post", "/admin/scans/failures/scan-1/reviewed"),
    ("push:write", "post", "/admin/push/broadcast"),
]


class PermissionSetTest(unittest.TestCase):
    def test_static_token_holds_only_read_and_imports(self) -> None:
        self.assertEqual(
            STATIC_IMPORT_TOKEN_PERMISSIONS,
            {"admin:read", "audit:read", "imports:run"},
        )

    def test_static_token_is_no_longer_owner_equivalent(self) -> None:
        self.assertNotEqual(STATIC_IMPORT_TOKEN_PERMISSIONS, FULL_ADMIN_PERMISSIONS)
        self.assertTrue(STATIC_IMPORT_TOKEN_PERMISSIONS < FULL_ADMIN_PERMISSIONS)

    def test_static_token_holds_no_write_permission(self) -> None:
        for permission, _method, _path in WRITE_ROUTES:
            with self.subTest(permission=permission):
                self.assertNotIn(permission, STATIC_IMPORT_TOKEN_PERMISSIONS)

    def test_supabase_admin_roles_are_untouched(self) -> None:
        """#196's role model must be unaffected by narrowing the static token."""
        for role in ("admin", "owner", "super_admin"):
            with self.subTest(role=role):
                self.assertEqual(ROLE_PERMISSIONS[role], FULL_ADMIN_PERMISSIONS)

    def test_the_two_static_identities_are_separate(self) -> None:
        """The import and job tokens no longer share one identity."""
        imports = _static_admin(STATIC_IMPORT_TOKEN_PERMISSIONS)
        job = _static_admin(FULL_ADMIN_PERMISSIONS)
        self.assertEqual(imports["permissions"], sorted(STATIC_IMPORT_TOKEN_PERMISSIONS))
        self.assertEqual(job["permissions"], sorted(FULL_ADMIN_PERMISSIONS))
        self.assertNotEqual(imports["permissions"], job["permissions"])


class StaticTokenIsRefusedOnWritesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_every_write_route_refuses_the_static_token(self) -> None:
        for permission, method, path in WRITE_ROUTES:
            with self.subTest(permission=permission, route=path):
                with static_import_token():
                    response = getattr(self.client, method)(
                        path, headers=ADMIN_HEADERS, json={}
                    )
                self.assertEqual(
                    response.status_code,
                    403,
                    f"{method.upper()} {path} still reachable with the static token",
                )
                self.assertEqual(
                    response.json()["error"]["code"], "admin_permission_denied"
                )


class StaticTokenKeepsItsOperationalSurfaceTest(unittest.TestCase):
    """Narrowing must not break the documented runbook."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_static_token_can_still_run_a_pricecharting_import(self) -> None:
        """imports:run — the PriceCharting import runbook uses this token."""
        with static_import_token(), patch(
            "app.routers.admin_pricecharting.AdminImportJobService"
        ) as jobs, patch(
            "app.routers.admin_pricecharting.download_env_sources", return_value=[]
        ):
            jobs.return_value.create_job.return_value = {"id": "job-1"}
            response = self.client.post(
                "/admin/pricecharting/import?dryRun=true", headers=ADMIN_HEADERS
            )

        self.assertEqual(response.status_code, 200, response.text)

    def test_static_token_can_still_read_the_console_surface(self) -> None:
        """admin:read — reading is the other half of what it is for."""
        with static_import_token(), patch(
            "app.routers.push.PriceAlertPushService"
        ) as service:
            service.return_value.delivery_history.return_value = {"success": True}
            response = self.client.get("/admin/push/history", headers=ADMIN_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)

    def test_static_token_can_still_read_the_audit_log(self) -> None:
        with static_import_token(), patch(
            "app.routers.admin_audit.AdminAuditService"
        ) as service:
            service.return_value.list_events.return_value = {"success": True, "events": []}
            response = self.client.get("/admin/audit/events", headers=ADMIN_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)


class ConsoleAdminIsUnaffectedTest(unittest.TestCase):
    """A real admin signing into the console keeps full capability."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_console_admin_still_passes_every_write_route(self) -> None:
        stubs = {
            "/admin/pricing/review-queue/item-1/reviewed": (
                "app.routers.admin_pricing.AdminPricingReviewQueueService", "mark_reviewed"),
            "/admin/catalog/catalog-1": (
                "app.routers.admin_catalog.AdminCatalogService", "update_item"),
            "/admin/portfolio/items/item-1": (
                "app.routers.admin_portfolio.AdminPortfolioService", "update_item"),
            "/admin/scans/failures/scan-1/reviewed": (
                "app.routers.admin_scans.AdminScanFailureService", "mark_reviewed"),
        }
        for permission, method, path in WRITE_ROUTES:
            if path not in stubs:
                continue
            target, attr = stubs[path]
            with self.subTest(permission=permission, route=path):
                with console_admin(), patch(target) as service, patch(
                    target.rsplit(".", 1)[0] + ".AdminAuditService"
                ):
                    getattr(service.return_value, attr).return_value = {"success": True}
                    response = getattr(self.client, method)(
                        path, headers=ADMIN_HEADERS, json={"note": "n", "title": "t"}
                    )
                self.assertNotIn(
                    response.status_code, (401, 403),
                    f"console admin was refused on {path}: {response.text}",
                )


class JobTokenIsUnchangedTest(unittest.TestCase):
    """Cron routes must keep working exactly as before."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_job_token_identity_keeps_full_permissions(self) -> None:
        """Pins the wiring, not just the helper.

        Asserting only that a job route is not refused cannot detect this:
        job routes check the token itself, never a permission, so the job
        identity could be silently narrowed and every route test would still
        pass. This calls the dependency and inspects what it returns.
        """
        from app.routers.admin_auth import require_admin_job_token

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = "job-token"
            identity = require_admin_job_token(
                x_admin_token="job-token", authorization=None
            )

        self.assertEqual(identity["permissions"], sorted(FULL_ADMIN_PERMISSIONS))
        self.assertTrue(identity["canWrite"])

    def test_import_token_identity_is_the_narrow_set(self) -> None:
        """The mirror of the above, through the real dependency."""
        from app.routers.admin_auth import require_admin_import_token

        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "import-token"
            identity = require_admin_import_token(
                x_admin_token="import-token", authorization=None
            )

        self.assertEqual(
            identity["permissions"], sorted(STATIC_IMPORT_TOKEN_PERMISSIONS)
        )

    def test_job_token_still_accepted_on_a_scheduled_route(self) -> None:
        with static_job_token(), patch(
            "app.routers.admin_pricing.BatchRepricingService"
        ) as service, patch("app.routers.admin_pricing.AdminAuditService"):
            summary = service.return_value.reprice_all.return_value
            summary.to_dict.return_value = {
                "scanned": 0, "repriced": 0, "unavailable": 0,
                "skipped": 0, "rateLimited": 0, "errors": [],
            }
            response = self.client.post(
                "/admin/pricing/reprice-all", headers=ADMIN_HEADERS
            )

        self.assertNotIn(response.status_code, (401, 403), response.text)


class PromoteScanDerivedMovedToJobTokenTest(unittest.TestCase):
    """The one route that made ADMIN_IMPORT_TOKEN load-bearing for automation.

    Its cron authenticated with the human import token by historical accident.
    ⚠️ The Render env var must be switched to ADMIN_JOB_TOKEN before that cron
    is unsuspended — the code change alone does not complete this.
    """

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_accepts_the_job_token(self) -> None:
        with static_job_token(), patch(
            "app.routers.admin_catalog_promotion.promote_scan_derived_rows"
        ) as promote:
            promote.return_value.to_dict.return_value = {"success": True}
            response = self.client.post(
                "/admin/catalog/promote-scan-derived?dryRun=true", headers=ADMIN_HEADERS
            )

        self.assertNotIn(response.status_code, (401, 403), response.text)

    def test_no_longer_accepts_the_import_token(self) -> None:
        with static_import_token():
            response = self.client.post(
                "/admin/catalog/promote-scan-derived?dryRun=true", headers=ADMIN_HEADERS
            )

        # The import token is not the job token, so it does not authenticate
        # here at all -- this is a 401, not a permission denial.
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
