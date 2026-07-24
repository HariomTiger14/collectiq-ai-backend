import unittest
from unittest.mock import patch

import httpx

from app.services.health.health_check_service import HealthCheckService
from app.services.health.providers import (
    AnalyzerHealthProvider,
    ApplicationHealthProvider,
    HealthCheckResult,
    SupabaseHealthProvider,
)


class HealthServiceTest(unittest.TestCase):
    def test_application_provider_is_healthy(self) -> None:
        result = ApplicationHealthProvider().check()

        self.assertEqual(result.name, "api")
        self.assertTrue(result.healthy)
        self.assertTrue(result.required)

    def test_supabase_missing_optional_config_is_non_blocking(self) -> None:
        result = SupabaseHealthProvider(
            supabase_url="",
            anon_key="",
            required=False,
        ).check()

        self.assertTrue(result.healthy)
        self.assertFalse(result.required)
        self.assertEqual(result.details["configured"], "false")

    def test_supabase_missing_required_config_is_unhealthy(self) -> None:
        result = SupabaseHealthProvider(
            supabase_url="",
            anon_key="",
            required=True,
        ).check()

        self.assertFalse(result.healthy)
        self.assertTrue(result.required)

    def test_supabase_connectivity_uses_health_endpoint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/auth/v1/health")
            self.assertEqual(request.headers["apikey"], "anon-key")
            return httpx.Response(200, json={"status": "ok"})

        provider = SupabaseHealthProvider(
            supabase_url="https://example.supabase.co",
            anon_key="anon-key",
            required=True,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = provider.check()

        self.assertTrue(result.healthy)
        self.assertTrue(result.required)
        self.assertEqual(result.details["statusCode"], "200")
        self.assertEqual(result.details["credential"], "anon")

    def test_supabase_service_role_key_takes_precedence(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["apikey"], "service-role-key")
            return httpx.Response(200, json={"status": "ok"})

        provider = SupabaseHealthProvider(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role-key",
            anon_key="anon-key",
            required=True,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = provider.check()

        self.assertTrue(result.healthy)
        self.assertEqual(result.details["credential"], "service_role")

    def test_analyzer_openai_requires_key(self) -> None:
        result = AnalyzerHealthProvider().check()

        self.assertEqual(result.name, "analyzer")
        self.assertIn(result.healthy, {True, False})

    def test_analyzer_auto_is_healthy_when_gemini_is_configured(self) -> None:
        with patch("app.services.health.providers.settings") as mock_settings:
            mock_settings.ai_provider = "auto"
            mock_settings.gemini_api_key = "gemini-key"
            mock_settings.openai_api_key = ""

            result = AnalyzerHealthProvider().check()

        self.assertTrue(result.healthy)
        self.assertEqual(result.details["provider"], "auto")
        self.assertEqual(result.details["geminiConfigured"], "true")
        self.assertEqual(result.details["openaiConfigured"], "false")

    def test_analyzer_auto_is_unhealthy_without_live_provider_key(self) -> None:
        with patch("app.services.health.providers.settings") as mock_settings:
            mock_settings.ai_provider = "auto"
            mock_settings.gemini_api_key = ""
            mock_settings.openai_api_key = ""

            result = AnalyzerHealthProvider().check()

        self.assertFalse(result.healthy)
        self.assertEqual(result.details["configured"], "false")

    def test_service_reports_unhealthy_required_dependency(self) -> None:
        service = HealthCheckService(
            providers=[
                _StaticProvider("api", True, True),
                _StaticProvider("supabase", False, True),
                _StaticProvider("analyzer", True, True),
            ],
        )

        report = service.run()

        self.assertFalse(report.healthy)
        self.assertFalse(report.services["supabase"])
        self.assertIn("supabase", report.latency)


class _StaticProvider:
    def __init__(self, name: str, healthy: bool, required: bool) -> None:
        self.name = name
        self.required = required
        self._healthy = healthy

    def check(self) -> HealthCheckResult:
        return HealthCheckResult(
            name=self.name,
            healthy=self._healthy,
            required=self.required,
            latency_ms=1,
        )


if __name__ == "__main__":
    unittest.main()
