import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class AdminOpsSummaryEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.get(
                "/admin/ops/summary",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_returns_safe_ops_summary(self) -> None:
        health_report = SimpleNamespace(
            healthy=True,
            services={"api": True, "supabase": True, "analyzer": True},
            latency={"api": 12, "supabase": 24, "analyzer": 18},
            checks=[
                SimpleNamespace(
                    name="api",
                    healthy=True,
                    required=True,
                    latency_ms=12,
                    message="API is healthy.",
                )
            ],
        )
        pricing_payload = {
            "success": True,
            "status": "healthy",
            "summary": {"catalogConfigured": True},
            "providers": [],
            "pricecharting": {"sources": []},
        }

        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_ops.settings",
        ) as settings, patch(
            "app.routers.admin_ops.HealthCheckService",
        ) as health_factory, patch(
            "app.routers.admin_ops.PricingHealthService",
        ) as pricing_factory:
            auth_settings.admin_import_token = "secret-token"
            _configure_settings(settings)
            health_factory.return_value.run.return_value = health_report
            pricing_factory.return_value.health.return_value = pricing_payload

            response = self.client.get(
                "/admin/ops/summary",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["environment"]["name"], "sit")
        self.assertEqual(payload["health"]["services"]["api"], True)
        self.assertEqual(payload["pricing"]["status"], "healthy")
        self.assertEqual(payload["validation"]["status"], "not_run")
        readiness = {item["key"]: item for item in payload["readiness"]}
        self.assertTrue(readiness["admin_import_token"]["configured"])
        self.assertFalse(readiness["openai_api_key"]["configured"])
        serialized = response.text
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("service-role-secret", serialized)


class AdminOpsReadinessEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_import_token = "secret-token"

            response = self.client.get(
                "/admin/ops/readiness",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)

    def test_returns_readiness_without_touching_pricing_or_health_checks(self) -> None:
        # The whole point of this endpoint: it must not call
        # HealthCheckService or PricingHealthService at all — those are
        # what make /admin/ops/summary slow (a Supabase RPC observed
        # taking ~49s in production). If either factory gets called here,
        # this endpoint has regressed into doing the same expensive work.
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_ops.settings",
        ) as settings, patch(
            "app.routers.admin_ops.HealthCheckService",
        ) as health_factory, patch(
            "app.routers.admin_ops.PricingHealthService",
        ) as pricing_factory:
            auth_settings.admin_import_token = "secret-token"
            _configure_settings(settings)

            response = self.client.get(
                "/admin/ops/readiness",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        readiness = {item["key"]: item for item in payload["readiness"]}
        self.assertTrue(readiness["admin_import_token"]["configured"])
        self.assertFalse(readiness["openai_api_key"]["configured"])
        health_factory.assert_not_called()
        pricing_factory.assert_not_called()


def _configure_settings(settings) -> None:
    settings.environment = "sit"
    settings.application_name = "PackLox API"
    settings.version = "0.1.0"
    settings.commit = "abc123"
    settings.build_time = "2026-07-30T00:00:00+00:00"
    settings.public_api_url = "https://api-sit.packlox.com"
    settings.public_frontend_url = "https://sit.packlox.com"
    settings.admin_import_token = "secret-token"
    settings.admin_job_token = ""
    settings.supabase_url = "https://packlox.supabase.co"
    settings.supabase_service_role_key = "service-role-secret"
    settings.supabase_anon_key = ""
    settings.ai_provider = "auto"
    settings.gemini_api_key = "gemini-secret"
    settings.openai_api_key = ""
    settings.pricing_provider = "auto"
    settings.firebase_project_id = ""
    settings.firebase_service_account_json = ""
    settings.firebase_access_token = ""
    settings.pricecharting_shared_throttle_enabled = True
    settings.pricecharting_provider_min_interval_ms = 1000
    settings.pricing_provider_min_interval_ms = 250
    settings.pricing_cache_ttl_seconds = 900


if __name__ == "__main__":
    unittest.main()
