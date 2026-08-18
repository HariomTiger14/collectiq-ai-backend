import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.catalog_image_flags_service import (
    CatalogImageFlagsError,
    UnknownCatalogImageCategoryError,
)


class AdminCatalogImageFlagsListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_configured_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = ""

            response = self.client.get("/admin/catalog-image-flags")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_rejects_invalid_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.get(
                "/admin/catalog-image-flags",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_returns_flags_payload_for_valid_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_catalog_image_flags.CatalogImageFlagsService"
        ) as service_cls:
            settings.admin_import_token = "secret-token"
            service_cls.return_value.list_flags.return_value = {
                "success": True,
                "flags": [{"category": "pokemon", "enabled": True, "updatedAt": None}],
            }

            response = self.client.get(
                "/admin/catalog-image-flags",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["flags"][0]["category"], "pokemon")

    def test_returns_503_when_service_raises_flags_error(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_catalog_image_flags.CatalogImageFlagsService"
        ) as service_cls:
            settings.admin_import_token = "secret-token"
            service_cls.return_value.list_flags.side_effect = CatalogImageFlagsError(
                "boom"
            )

            response = self.client.get(
                "/admin/catalog-image-flags",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "catalog_image_flags_unavailable"
        )


class AdminCatalogImageFlagsUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_configured_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = ""

            response = self.client.patch(
                "/admin/catalog-image-flags/pokemon", json={"enabled": False}
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_import_not_configured")

    def test_rejects_invalid_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.patch(
                "/admin/catalog-image-flags/pokemon",
                json={"enabled": False},
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_update_success_returns_flag_and_records_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_catalog_image_flags.CatalogImageFlagsService"
        ) as service_cls, patch(
            "app.routers.admin_catalog_image_flags.AdminAuditService"
        ) as audit_cls:
            settings.admin_import_token = "secret-token"
            service_cls.return_value.set_flag.return_value = {
                "success": True,
                "flag": {
                    "category": "pokemon",
                    "enabled": False,
                    "updatedAt": "2026-08-18T00:00:00+00:00",
                },
            }

            response = self.client.patch(
                "/admin/catalog-image-flags/pokemon",
                json={"enabled": False},
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["flag"]["category"], "pokemon")
        self.assertFalse(payload["flag"]["enabled"])

        service_cls.return_value.set_flag.assert_called_once_with(
            category="pokemon", enabled=False
        )
        audit_cls.return_value.record.assert_called_once()
        _, audit_kwargs = audit_cls.return_value.record.call_args
        self.assertEqual(audit_kwargs["action"], "admin_catalog_image_flags.toggled")
        self.assertEqual(audit_kwargs["status"], "success")
        self.assertEqual(audit_kwargs["target_type"], "catalog_image_flag")
        self.assertEqual(audit_kwargs["target_id"], "pokemon")
        self.assertEqual(audit_kwargs["metadata"], {"enabled": False})

    def test_update_unknown_category_returns_404_and_records_failure_audit(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_catalog_image_flags.CatalogImageFlagsService"
        ) as service_cls, patch(
            "app.routers.admin_catalog_image_flags.AdminAuditService"
        ) as audit_cls:
            settings.admin_import_token = "secret-token"
            service_cls.return_value.set_flag.side_effect = (
                UnknownCatalogImageCategoryError("'not-real' is not a known category.")
            )

            response = self.client.patch(
                "/admin/catalog-image-flags/not-real",
                json={"enabled": True},
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "unknown_catalog_image_category"
        )
        audit_cls.return_value.record.assert_called_once()
        _, audit_kwargs = audit_cls.return_value.record.call_args
        self.assertEqual(audit_kwargs["status"], "failure")
        self.assertEqual(audit_kwargs["target_id"], "not-real")

    def test_update_service_error_returns_503(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_catalog_image_flags.CatalogImageFlagsService"
        ) as service_cls, patch(
            "app.routers.admin_catalog_image_flags.AdminAuditService"
        ):
            settings.admin_import_token = "secret-token"
            service_cls.return_value.set_flag.side_effect = CatalogImageFlagsError(
                "boom"
            )

            response = self.client.patch(
                "/admin/catalog-image-flags/pokemon",
                json={"enabled": True},
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "catalog_image_flags_unavailable"
        )

    def test_update_requires_catalog_write_permission(self) -> None:
        # A Supabase-session admin whose role only grants viewer-level
        # permissions (no catalog:write) must be rejected with 403, even
        # though they are a valid, authenticated admin. Follows the same
        # Supabase session mocking pattern as test_admin_auth.py.
        with patch("app.routers.admin_auth.settings") as settings, patch(
            "app.routers.admin_auth.httpx.get"
        ) as get_request:
            settings.admin_import_token = ""
            settings.supabase_url = "https://packlox.supabase.co"
            settings.supabase_anon_key = "anon-key"
            settings.supabase_service_role_key = "service-role"
            settings.admin_profile_table = "profiles"
            get_request.side_effect = [
                _MockResponse(200, {"id": "viewer-user", "email": "viewer@example.com"}),
                _MockResponse(
                    200, [{"id": "viewer-user", "role": "viewer"}]
                ),
            ]

            response = self.client.patch(
                "/admin/catalog-image-flags/pokemon",
                json={"enabled": False},
                headers={"Authorization": "Bearer supabase-session"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"]["code"], "admin_permission_denied"
        )


class _MockResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


if __name__ == "__main__":
    unittest.main()
