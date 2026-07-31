import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_audit_service import clear_in_memory_audit_events


class AdminReportsAndActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        clear_in_memory_audit_events()

    def tearDown(self) -> None:
        clear_in_memory_audit_events()

    def test_reports_overview_returns_backend_summary(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_reports.AdminReportsService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.overview.return_value = {
                "success": True,
                "summary": {
                    "users": 3,
                    "pricingReview": 2,
                    "scanFailures": 1,
                    "auditEvents": 4,
                },
            }

            response = self.client.get(
                "/admin/reports/overview",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["users"], 3)

    def test_reports_export_returns_csv(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_reports.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.list_users.return_value = {
                "users": [{"id": "user-1", "email": "collector@example.com"}]
            }

            response = self.client.get(
                "/admin/reports/export?dataset=users",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("collector@example.com", response.text)

    def test_user_support_action_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.force_logout.return_value = {
                "success": True,
                "userId": "user-1",
                "status": "logout_requested",
            }

            response = self.client.post(
                "/admin/users/user-1/support/force_logout",
                headers={"Authorization": "Bearer secret-token"},
                json={},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.support.force_logout",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "logout_requested")
        self.assertEqual(audit_response.json()["events"][0]["targetId"], "user-1")

    def test_admin_role_update_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_users.AdminUserService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.update_admin_role.return_value = {
                "success": True,
                "userId": "user-1",
                "role": "support",
                "isAdmin": True,
            }

            response = self.client.patch(
                "/admin/users/user-1/role",
                headers={"Authorization": "Bearer secret-token"},
                json={"role": "support", "isAdmin": True},
            )
            audit_response = self.client.get(
                "/admin/audit/events?action=admin_users.role_updated",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "support")
        self.assertEqual(audit_response.json()["events"][0]["metadata"]["role"], "support")


if __name__ == "__main__":
    unittest.main()
