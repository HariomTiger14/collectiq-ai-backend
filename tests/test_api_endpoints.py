import json
import base64
import os
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings, parse_cors_allowed_origins
from app.main import app
from app.services.ai.gemini_recognition_provider import GeminiRecognitionProvider
from app.services.ai.openai_recognition_provider import OpenAIRecognitionProvider
from app.services.analyzer.backend_analyzer_service import BackendAnalyzerService
from app.services.analyzer.errors import AnalyzerPipelineError
from app.services.analyzer.providers import (
    AutoAnalyzerProvider,
    FallbackAnalyzerProvider,
    MockAnalyzerProvider,
)
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    MarketComparableSale,
    PricingProviderError,
    PricingProviderUnavailableError,
    PricingResult,
    utc_timestamp,
)
from app.services.health.health_check_service import HealthCheckService
from app.services.health.providers import HealthCheckResult


class ApiEndpointsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "healthy")
        self.assertTrue(payload["services"]["api"])
        self.assertTrue(payload["services"]["supabase"])
        self.assertTrue(payload["services"]["analyzer"])
        self.assertIn("timestamp", payload)
        self.assertIn("latency", payload)
        self.assertIn("checks", payload)

    def test_health_does_not_expose_secrets(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = json.dumps(payload)
        self.assertNotIn("OPENAI_API_KEY", serialized)
        self.assertNotIn("api_key", serialized.lower())
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("token", serialized.lower())

    def test_health_returns_quickly_without_gemini_credentials(self) -> None:
        providers = [
            _StaticHealthProvider("api", True, True),
            _StaticHealthProvider("supabase", True, False),
            _StaticHealthProvider("analyzer", True, True),
        ]
        started_at = time.perf_counter()

        with patch(
            "app.routers.health.HealthCheckService",
            return_value=HealthCheckService(providers=providers),
        ), patch("app.routers.health.settings") as health_settings:
            health_settings.environment = "sit"
            health_settings.version = "0.1.0"
            response = self.client.get("/health")

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed_ms, 250)

    def test_version(self) -> None:
        response = self.client.get("/version")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload.keys()),
            {"application", "environment", "version", "commit", "buildTime"},
        )
        self.assertEqual(payload["application"], "PackLox API")
        self.assertIn("version", payload)
        self.assertIn("environment", payload)
        self.assertIn("commit", payload)
        self.assertIn("buildTime", payload)
        serialized = json.dumps(payload)
        self.assertNotIn("api_key", serialized.lower())
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("token", serialized.lower())

    def test_version_returns_quickly_without_gemini_credentials(self) -> None:
        started_at = time.perf_counter()

        with patch("app.routers.health.settings") as health_settings:
            health_settings.application_name = "PackLox API"
            health_settings.environment = "sit"
            health_settings.version = "0.1.0"
            health_settings.commit = "test-commit"
            health_settings.build_time = "2026-07-06T00:00:00Z"
            response = self.client.get("/version")

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed_ms, 250)

    def test_version_metadata_uses_explicit_env_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ENVIRONMENT": "sit",
                "APP_VERSION": "2.0.0",
                "COMMIT_SHA": "explicit-commit",
                "BUILD_TIME": "2026-07-06T00:00:00Z",
            },
            clear=True,
        ):
            settings = Settings()

        self.assertEqual(settings.environment, "sit")
        self.assertEqual(settings.version, "2.0.0")
        self.assertEqual(settings.commit, "explicit-commit")
        self.assertEqual(settings.build_time, "2026-07-06T00:00:00Z")

    def test_version_metadata_uses_render_commit_before_git_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {"RENDER_GIT_COMMIT": "render-commit"},
            clear=True,
        ), patch("app.core.config._git_commit_sha") as git_commit:
            settings = Settings()

        self.assertEqual(settings.commit, "render-commit")
        git_commit.assert_not_called()

    def test_version_metadata_uses_git_commit_or_unknown_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "app.core.config._git_commit_sha",
            return_value="git-commit",
        ):
            self.assertEqual(Settings().commit, "git-commit")

        with patch.dict(os.environ, {}, clear=True), patch(
            "app.core.config._git_commit_sha",
            return_value=None,
        ):
            self.assertEqual(Settings().commit, "unknown")

    def test_version_metadata_build_time_fallback_exists(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            build_time = Settings().build_time

        self.assertNotEqual(build_time, "unknown")
        datetime.fromisoformat(build_time)

    def test_scanner_analyze_happy_path(self) -> None:
        response = self.client.post(
            "/scanner/analyze",
            files={"image": ("card.png", b"image-bytes", "image/png")},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["filename"].endswith(".png"))
        self.assertIn("/uploads/", payload["imageUrl"])
        self.assertTrue(payload["title"])
        self.assertIn(
            payload["category"],
            [
                "Pokemon Card",
                "Sports Card",
                "Trading Card",
                "Coin",
                "Toy/Figure",
                "Action Figure",
                "Comic",
                "Comic Book",
                "Stamp",
                "Retro Game",
                "Vintage Toy",
            ],
        )
        self.assertGreaterEqual(payload["confidence"], 0)
        self.assertLessEqual(payload["confidence"], 100)
        self.assertGreater(payload["estimatedValue"], 0)
        self.assertTrue(payload["condition"])
        self.assertTrue(payload["recommendation"])
        self.assertTrue(payload["description"])
        self.assertTrue(payload["detectedObjects"])
        self.assertEqual(payload["aiProvider"], "mock")
        self.assertGreater(payload["processingTimeMs"], 0)
        self.assertTrue(payload["primaryMatch"])
        self.assertEqual(len(payload["alternativeMatches"]), 3)
        self.assertTrue(payload["confidenceExplanation"])
        self.assertTrue(payload["detectionQuality"])
        self.assertTrue(payload["aiReasoning"])
        self.assertIn("year", payload)
        self.assertIn("brand", payload)
        self.assertIn("manufacturer", payload)
        self.assertIn("setName", payload)
        self.assertIn("series", payload)
        self.assertIn("country", payload)
        self.assertIn("estimated_value_low", payload)
        self.assertIn("estimated_value_high", payload)
        self.assertIn("playerOrCharacter", payload)
        self.assertIn("material", payload)
        self.assertIn("notes", payload)
        self.assertIn("pricing", payload)
        pricing = payload["pricing"]
        self.assertEqual(pricing["estimatedMarketValue"], 0)
        self.assertEqual(pricing["lowEstimate"], 0)
        self.assertEqual(pricing["highEstimate"], 0)
        self.assertEqual(payload["estimated_value_low"], pricing["lowEstimate"])
        self.assertEqual(payload["estimated_value_high"], pricing["highEstimate"])
        self.assertEqual(pricing["currency"], "AUD")
        self.assertTrue(pricing["pricingSource"])
        self.assertGreaterEqual(pricing["pricingConfidence"], 0)
        self.assertLessEqual(pricing["pricingConfidence"], 100)
        self.assertTrue(pricing["lastUpdated"])

    def test_api_analyze_happy_path(self) -> None:
        response = self.client.post("/api/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["id"].startswith("backend-"))
        self.assertTrue(payload["itemName"])
        self.assertEqual(payload["title"], payload["itemName"])
        self.assertTrue(payload["category"])
        self.assertIn("attributes", payload)
        self.assertIn("rawProviderPayload", payload)
        self.assertGreater(payload["estimatedValue"], 0)
        self.assertEqual(payload["valuationStatus"], "provider_not_configured")
        self.assertEqual(payload["valuationSource"], "not_configured")
        self.assertEqual(payload["lowEstimate"], 0)
        self.assertEqual(payload["highEstimate"], 0)
        self.assertGreaterEqual(payload["confidence"], 0)
        self.assertLessEqual(payload["confidence"], 100)
        self.assertTrue(payload["condition"])
        self.assertTrue(payload["marketTrend"])
        self.assertIsInstance(payload["keyAttributes"], dict)
        self.assertTrue(payload["aiReview"]["primaryMatch"])
        self.assertTrue(payload["aiReview"]["confidenceExplanation"])
        self.assertEqual(len(payload["alternatives"]), 3)
        self.assertTrue(payload["recommendation"])
        self.assertIn("marketSummary", payload)
        self.assertEqual(payload["marketSummary"]["salesCount"], 0)
        self.assertEqual(len(payload["comparableSales"]), 0)
        self.assertEqual(payload["timestamp"], "2026-06-30T09:00:00Z")
        self.assertIn(payload["confidenceLevel"], ["High", "Medium", "Low"])
        self.assertIsInstance(payload["fieldConfidence"], dict)
        self.assertIn("itemName", payload["fieldConfidence"])
        self.assertIsInstance(payload["lowConfidenceReasons"], list)
        self.assertIsInstance(payload["imageQualityIssues"], list)
        self.assertIsInstance(payload["scanRecommendations"], list)
        self.assertIn("diagnostics", payload)
        diagnostics = payload["diagnostics"]
        self.assertEqual(diagnostics["aiProvider"], "mock")
        self.assertIn("pricingProvider", diagnostics)
        self.assertIn("pricingFallbackUsed", diagnostics)
        self.assertIn("totalLatencyMs", diagnostics)
        self.assertIn(diagnostics["confidenceLevel"], ["High", "Medium", "Low"])

    def test_root_analyze_uses_final_contract(self) -> None:
        response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["id"].startswith("backend-"))
        self.assertTrue(payload["itemName"])
        self.assertTrue(payload["category"])
        self.assertGreater(payload["estimatedValue"], 0)
        self.assertTrue(payload["recommendation"])
        self.assertEqual(payload["timestamp"], "2026-06-30T09:00:00Z")
        self.assertEqual(payload["diagnostics"]["aiProvider"], "mock")

    def test_root_analyze_error_maps_to_final_contract(self) -> None:
        payload = _api_analyze_payload()
        payload["image"]["fileName"] = "card.gif"
        payload["image"]["mimeType"] = "image/gif"

        response = self.client.post("/analyze", json=payload)

        self.assertEqual(response.status_code, 415)
        error = response.json()["error"]
        self.assertEqual(error["code"], "unsupported_media_type")
        self.assertFalse(error["retryable"])

    def test_api_analyze_openai_without_key_returns_safe_error(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=OpenAIRecognitionProvider(api_key=""),
        ):
            response = self.client.post(
                "/api/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "provider_unavailable")
        self.assertIn("OPENAI_API_KEY", error["message"])
        self.assertFalse(error["retryable"])

    def test_scanner_analyze_openai_without_key_returns_safe_error(self) -> None:
        with patch(
            "app.routers.scanner.get_ai_recognition_provider",
            return_value=OpenAIRecognitionProvider(api_key=""),
        ):
            response = self.client.post(
                "/scanner/analyze",
                files={"image": ("card.png", b"image-bytes", "image/png")},
            )

        self.assertEqual(response.status_code, 501)
        self.assertFalse(response.json()["success"])
        self.assertIn("OPENAI_API_KEY", response.json()["error"])

    def test_cors_config_loads_from_env_value(self) -> None:
        origins = parse_cors_allowed_origins(
            "https://collectiq-sit.example.com, http://localhost:8000"
        )

        self.assertEqual(
            origins,
            ("https://collectiq-sit.example.com", "http://localhost:8000"),
        )

    def test_cors_default_allows_sit_admin_and_localhost(self) -> None:
        origins = parse_cors_allowed_origins()

        self.assertIn("https://sit.packlox.com", origins)
        self.assertIn("https://admin.packlox.com", origins)
        self.assertIn("http://localhost:3000", origins)

    def test_api_analyze_openai_success_returns_contract_response(self) -> None:
        provider = OpenAIRecognitionProvider(
            api_key="test-key",
            client=_FakeOpenAIClient(
                response=_FakeOpenAIResponse(
                    body={"output_text": json.dumps(_openai_output())}
                )
            ),
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/api/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["itemName"], "1999 Pokemon Charizard Holo")
        self.assertEqual(payload["category"], "Pokemon Card")
        self.assertEqual(payload["estimated_value"], payload["estimatedValue"])
        self.assertEqual(payload["currency"], "AUD")
        self.assertIsInstance(payload["tags"], list)
        self.assertIsInstance(payload["attributes"], dict)
        self.assertIsInstance(payload["images"], list)
        self.assertIsInstance(payload["rawProviderPayload"], dict)
        self.assertEqual(payload["confidence"], 94)
        self.assertEqual(payload["aiReview"]["primaryMatch"], payload["itemName"])
        self.assertEqual(len(payload["alternatives"]), 3)
        self.assertEqual(payload["marketSummary"]["salesCount"], 0)
        self.assertEqual(payload["valuationStatus"], "provider_not_configured")
        self.assertEqual(payload["comparableSales"], [])
        self.assertEqual(payload["confidenceLevel"], "High")
        self.assertEqual(payload["fieldConfidence"]["itemName"], 96)
        self.assertEqual(payload["imageQualityIssues"], ["none"])
        self.assertIn("Use bright", payload["scanRecommendations"][0])

        self.assertIsNotNone(provider._client.last_request)
        request_json = provider._client.last_request["json"]
        self.assertEqual(request_json["model"], "gpt-4.1-mini")
        prompt_text = request_json["input"][0]["content"][0]["text"]
        self.assertIn("requestedCategory=Pokemon Card", prompt_text)
        self.assertIn("Return confidence for each extracted field", prompt_text)
        self.assertIn("glare/reflections", prompt_text)
        image_content = request_json["input"][0]["content"][1]
        self.assertTrue(image_content["image_url"].startswith("data:image/jpeg;base64,"))

    def test_backend_analyzer_sit_mock_with_real_key_selects_auto_provider(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.settings",
            SimpleNamespace(
                ai_provider="mock",
                environment="sit",
                gemini_api_key="",
                openai_api_key="test-key",
                allow_mock_analyzer=False,
            ),
        ):
            provider = BackendAnalyzerService()._resolve_provider()

        self.assertIsInstance(provider, AutoAnalyzerProvider)

    def test_backend_analyzer_sit_mock_without_real_key_returns_config_error(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.settings",
            SimpleNamespace(
                ai_provider="mock",
                environment="sit",
                gemini_api_key="",
                openai_api_key="",
                allow_mock_analyzer=False,
            ),
        ):
            with self.assertRaises(AnalyzerPipelineError) as context:
                BackendAnalyzerService()._resolve_provider()

        self.assertEqual(context.exception.code, "AI_PROVIDER_NOT_CONFIGURED")

    def test_backend_analyzer_sit_mock_allowed_only_when_explicit(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.settings",
            SimpleNamespace(
                ai_provider="mock",
                environment="sit",
                gemini_api_key="",
                openai_api_key="",
                allow_mock_analyzer=True,
            ),
        ):
            provider = BackendAnalyzerService()._resolve_provider()

        self.assertIsInstance(provider, MockAnalyzerProvider)

    def test_backend_analyzer_gemini_provider_selects_fallback_chain(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.settings",
            SimpleNamespace(
                ai_provider="gemini",
                environment="sit",
                gemini_api_key="test-key",
                openai_api_key="",
                allow_mock_analyzer=False,
            ),
        ):
            provider = BackendAnalyzerService()._resolve_provider()

        self.assertIsInstance(provider, FallbackAnalyzerProvider)
        self.assertEqual(provider.selection_diagnostics["requestedProvider"], "gemini")
        self.assertEqual(
            provider.selection_diagnostics["preferredOrder"],
            ["gemini", "mock"],
        )

    def test_api_analyze_auto_provider_uses_gemini_before_openai(self) -> None:
        provider = AutoAnalyzerProvider(
            providers=[
                GeminiRecognitionProvider(
                    api_key="gemini-key",
                    client=_FakeGeminiClient(
                        response=_FakeOpenAIResponse(
                            body=_gemini_response(_openai_output(title="Gemini Card"))
                        )
                    ),
                ),
                OpenAIRecognitionProvider(
                    api_key="test-key",
                    client=_FakeOpenAIClient(
                        response=_FakeOpenAIResponse(
                            body={"output_text": json.dumps(_openai_output())}
                        )
                    ),
                ),
            ]
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["diagnostics"]["aiProvider"], "gemini")
        self.assertEqual(
            payload["rawProviderPayload"]["providerSelection"]["selectedProvider"],
            "gemini",
        )
        self.assertNotIn("mockSelection", payload["rawProviderPayload"])
        self.assertEqual(payload["itemName"], "Gemini Card")

    def test_api_analyze_auto_provider_uses_openai_when_gemini_unavailable(self) -> None:
        provider = AutoAnalyzerProvider(
            providers=[
                GeminiRecognitionProvider(api_key=""),
                OpenAIRecognitionProvider(
                    api_key="test-key",
                    client=_FakeOpenAIClient(
                        response=_FakeOpenAIResponse(
                            body={"output_text": json.dumps(_openai_output())}
                        )
                    ),
                ),
            ]
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["diagnostics"]["aiProvider"], "openai")
        self.assertEqual(
            payload["rawProviderPayload"]["providerSelection"]["selectedProvider"],
            "openai",
        )

    def test_api_analyze_real_provider_rejects_missing_image_bytes(self) -> None:
        gemini_client = _FakeGeminiClient(
            response=_FakeOpenAIResponse(
                body=_gemini_response(_openai_output(title="Should Not Run"))
            )
        )
        provider = AutoAnalyzerProvider(
            providers=[
                GeminiRecognitionProvider(
                    api_key="gemini-key",
                    client=gemini_client,
                )
            ],
            allow_mock_fallback=False,
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "INVALID_IMAGE_PAYLOAD")
        self.assertIsNone(gemini_client.last_request)

    def test_api_analyze_auto_provider_handles_unknown_low_confidence(self) -> None:
        provider = AutoAnalyzerProvider(
            providers=[
                GeminiRecognitionProvider(
                    api_key="gemini-key",
                    client=_FakeGeminiClient(
                        response=_FakeOpenAIResponse(
                            body=_gemini_response(
                                _openai_output(
                                    title="Unknown collectible",
                                    category="Other",
                                    confidence=42,
                                    estimated_value=0,
                                    condition="Unknown",
                                    image_quality_issues=["blurry image"],
                                    low_confidence_reasons=[
                                        "The image is too blurry to verify fine details."
                                    ],
                                )
                            )
                        )
                    ),
                ),
            ]
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["itemName"], "Unknown collectible")
        self.assertEqual(payload["confidenceLevel"], "Low")
        self.assertEqual(payload["confidence"], 42)
        self.assertIn("blurry image", payload["imageQualityIssues"])
        self.assertEqual(payload["diagnostics"]["aiProvider"], "gemini")

    def test_api_analyze_auto_provider_returns_error_without_mock_fallback(self) -> None:
        provider = AutoAnalyzerProvider(
            providers=[
                GeminiRecognitionProvider(api_key=""),
                OpenAIRecognitionProvider(api_key=""),
            ],
            allow_mock_fallback=False,
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "AI_PROVIDER_NOT_CONFIGURED")
        provider_errors = error["details"]["providerErrors"]
        self.assertTrue(
            any(
                message.startswith("gemini:AIProviderNotConfiguredError")
                for message in provider_errors
            ),
        )
        self.assertTrue(
            any(
                message.startswith("openai:AIProviderNotConfiguredError")
                for message in provider_errors
            ),
        )

    def test_api_analyze_auto_provider_can_fallback_to_mock_when_explicit(self) -> None:
        provider = AutoAnalyzerProvider(
            providers=[
                GeminiRecognitionProvider(api_key=""),
                OpenAIRecognitionProvider(api_key=""),
            ],
            allow_mock_fallback=True,
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["diagnostics"]["aiProvider"], "mock")
        self.assertEqual(
            payload["rawProviderPayload"]["providerSelection"]["selectedProvider"],
            "mock",
        )

    def test_api_analyze_gemini_mapping_handles_missing_fields_safely(self) -> None:
        provider = FallbackAnalyzerProvider(
            requested_provider="gemini",
            providers=[
                GeminiRecognitionProvider(
                    api_key="gemini-key",
                    client=_FakeGeminiClient(
                        response=_FakeOpenAIResponse(
                            body=_gemini_response(
                                {
                                    "title": "Partially visible collectible",
                                    "category": "Other",
                                    "confidence": 38,
                                }
                            )
                        )
                    ),
                )
            ],
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["itemName"], "Partially visible collectible")
        self.assertEqual(payload["category"], "Other")
        self.assertEqual(payload["confidence"], 38)
        self.assertEqual(payload["condition"], "Unknown")
        self.assertEqual(payload["diagnostics"]["aiProvider"], "gemini")

    def test_api_analyze_accepts_multi_image_payload(self) -> None:
        gemini_client = _FakeGeminiClient(
            response=_FakeOpenAIResponse(
                body=_gemini_response(
                    _openai_output(
                        title="Australian $2 Coin",
                        category="Coin",
                        confidence=82,
                        estimated_value=12,
                        condition="Circulated",
                    )
                    | {
                        "faceValue": 2,
                        "estimatedMarketValue": 12,
                        "valuationConfidence": 70,
                        "scanRecommendations": [],
                    }
                )
            )
        )
        provider = FallbackAnalyzerProvider(
            requested_provider="gemini",
            providers=[
                GeminiRecognitionProvider(api_key="gemini-key", client=gemini_client)
            ],
        )
        payload = _api_analyze_payload(base64_image=True)
        payload["image"]["imageRole"] = "front"
        payload["images"] = [
            payload["image"],
            {
                **payload["image"],
                "fileName": "coin-back.jpg",
                "localFilePath": "/local/app/path/coin-back.jpg",
                "imageRole": "back",
            },
        ]

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post("/analyze", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["itemName"], "Australian $2 Coin")
        self.assertEqual(body["faceValue"], 2)
        self.assertIsNone(body["estimatedMarketValue"])
        self.assertEqual(body["aiEstimatedValue"], 12)
        self.assertEqual(body["valuationStatus"], "provider_not_configured")
        self.assertEqual(body["rawProviderPayload"]["photosUsed"], 2)
        self.assertEqual(body["rawProviderPayload"]["photoRoles"], ["front", "back"])
        sent_parts = gemini_client.last_request["json"]["contents"][0]["parts"]
        image_parts = [part for part in sent_parts if "inline_data" in part]
        self.assertEqual(len(image_parts), 2)

    def test_api_analyze_single_coin_image_recommends_reverse(self) -> None:
        provider = FallbackAnalyzerProvider(
            requested_provider="gemini",
            providers=[
                GeminiRecognitionProvider(
                    api_key="gemini-key",
                    client=_FakeGeminiClient(
                        response=_FakeOpenAIResponse(
                            body=_gemini_response(
                                _openai_output(
                                    title="Australian $2 Coin",
                                    category="Coin",
                                    confidence=58,
                                    estimated_value=0,
                                    condition="Unknown",
                                )
                                | {
                                    "faceValue": 2,
                                    "estimatedMarketValue": 0,
                                    "valuationConfidence": 35,
                                    "scanRecommendations": [
                                        "Add a reverse/back photo to verify the coin."
                                    ],
                                }
                            )
                        )
                    ),
                )
            ],
        )
        payload = _api_analyze_payload(base64_image=True)
        payload["image"]["imageRole"] = "front"

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post("/analyze", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["faceValue"], 2)
        self.assertEqual(body["estimatedValue"], 0)
        self.assertIsNone(body["estimatedMarketValue"])
        self.assertEqual(body["valuationStatus"], "provider_not_configured")
        self.assertIn("reverse", " ".join(body["scanRecommendations"]).lower())

    def test_api_analyze_response_contract_keys_remain_unchanged(self) -> None:
        response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json().keys()),
            {
                "id",
                "itemName",
                "title",
                "category",
                "manufacturer",
                "year",
                "series",
                "variant",
                "estimatedValue",
                "estimated_value",
                "currency",
                "tags",
                "description",
                "attributes",
                "images",
                "rawProviderPayload",
                "faceValue",
                "estimatedMarketValue",
                "aiEstimatedValue",
                "valuationStatus",
                "valuationSource",
                "askingPriceWarning",
                "valuationConfidence",
                "lowEstimate",
                "highEstimate",
                "confidence",
                "condition",
                "marketTrend",
                "keyAttributes",
                "aiReview",
                "alternatives",
                "recommendation",
                "marketSummary",
                "comparableSales",
                "imageUrl",
                "timestamp",
                "fieldConfidence",
                "confidenceLevel",
                "lowConfidenceReasons",
                "imageQualityIssues",
                "scanRecommendations",
                "diagnostics",
            },
        )

    def test_api_analyze_successful_identification_without_pricing_provider(self) -> None:
        response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["valuationStatus"], "provider_not_configured")
        self.assertEqual(payload["valuationSource"], "not_configured")
        self.assertGreater(payload["aiEstimatedValue"], 0)
        self.assertIsNone(payload["estimatedMarketValue"])
        self.assertEqual(payload["marketSummary"]["salesCount"], 0)
        self.assertEqual(payload["comparableSales"], [])

    def test_api_analyze_market_estimated_value_from_pricing_provider(self) -> None:
        with patch("app.routers.api_analyze.settings", _settings_with_pricing("ebay")), patch(
            "app.routers.api_analyze.get_pricing_provider",
            return_value=_FakePricingProvider(),
        ):
            response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["valuationStatus"], "market_estimated")
        self.assertEqual(payload["valuationSource"], "eBay sold comps")
        self.assertEqual(payload["estimatedMarketValue"], 42)
        self.assertEqual(payload["estimatedValue"], 42)
        self.assertEqual(payload["marketSummary"]["salesCount"], 1)

    def test_api_analyze_no_market_match_status(self) -> None:
        with patch("app.routers.api_analyze.settings", _settings_with_pricing("ebay")), patch(
            "app.routers.api_analyze.get_pricing_provider",
            return_value=_FailingPricingProvider(EmptyMarketDataError("no comps")),
        ):
            response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["valuationStatus"], "no_market_match")
        self.assertEqual(payload["valuationSource"], "ebay")
        self.assertIsNone(payload["estimatedMarketValue"])

    def test_api_analyze_pricing_lookup_failed_status(self) -> None:
        with patch("app.routers.api_analyze.settings", _settings_with_pricing("ebay")), patch(
            "app.routers.api_analyze.get_pricing_provider",
            return_value=_FailingPricingProvider(PricingProviderError("upstream failed")),
        ):
            response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["valuationStatus"], "lookup_failed")
        self.assertEqual(payload["valuationSource"], "ebay")
        self.assertIsNone(payload["estimatedMarketValue"])

    def test_api_analyze_pricing_provider_not_configured_status(self) -> None:
        with patch("app.routers.api_analyze.settings", _settings_with_pricing("ebay")), patch(
            "app.routers.api_analyze.get_pricing_provider",
            return_value=_FailingPricingProvider(
                PricingProviderUnavailableError("EBAY_ACCESS_TOKEN is missing")
            ),
        ):
            response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["valuationStatus"], "provider_not_configured")
        self.assertEqual(payload["valuationSource"], "ebay")

    def test_api_analyze_openai_invalid_json_returns_safe_error(self) -> None:
        provider = OpenAIRecognitionProvider(
            api_key="test-key",
            client=_FakeOpenAIClient(
                response=_FakeOpenAIResponse(body={"output_text": "not json"})
            ),
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/api/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["code"], "provider_unavailable")
        self.assertTrue(error["retryable"])

    def test_api_analyze_openai_timeout_returns_safe_error(self) -> None:
        provider = OpenAIRecognitionProvider(
            api_key="test-key",
            client=_FakeOpenAIClient(exception=httpx.TimeoutException("slow")),
        )

        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            return_value=provider,
        ):
            response = self.client.post(
                "/api/analyze",
                json=_api_analyze_payload(base64_image=True),
            )

        self.assertEqual(response.status_code, 504)
        error = response.json()["error"]
        self.assertEqual(error["code"], "timeout")
        self.assertTrue(error["retryable"])

    def test_golden_manifest_structure(self) -> None:
        manifest_path = Path(__file__).parent / "test_assets" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(manifest), 3)
        for entry in manifest:
            self.assertTrue(entry["imageFilename"])
            self.assertTrue(entry["filename"])
            self.assertTrue(entry["expectedCategory"])
            self.assertTrue(entry["expectedItem"])
            self.assertIsInstance(entry["expectedItemKeywords"], list)
            self.assertTrue(entry["expectedBrandOrFranchise"])
            self.assertIsInstance(entry["pricingExpected"], bool)
            confidence_range = entry["expectedConfidenceRange"]
            self.assertGreaterEqual(confidence_range["min"], 0)
            self.assertLessEqual(confidence_range["max"], 100)
            self.assertLessEqual(confidence_range["min"], confidence_range["max"])

    def test_api_analyze_missing_image(self) -> None:
        payload = _api_analyze_payload()
        payload["request"]["imagePath"] = ""

        response = self.client.post("/api/analyze", json=payload)

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_image")
        self.assertFalse(error["retryable"])

    def test_api_analyze_invalid_image(self) -> None:
        payload = _api_analyze_payload()
        payload["image"]["base64Image"] = base64.b64encode(b"not-an-image").decode("ascii")

        response = self.client.post("/api/analyze", json=payload)

        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_image")
        self.assertFalse(error["retryable"])

    def test_api_analyze_unsupported_type(self) -> None:
        payload = _api_analyze_payload()
        payload["image"]["fileName"] = "card.gif"
        payload["image"]["mimeType"] = "image/gif"

        response = self.client.post("/api/analyze", json=payload)

        self.assertEqual(response.status_code, 415)
        error = response.json()["error"]
        self.assertEqual(error["code"], "unsupported_media_type")
        self.assertFalse(error["retryable"])

    def test_api_analyze_oversized_image_metadata(self) -> None:
        payload = _api_analyze_payload()
        payload["image"]["sizeBytes"] = 10 * 1024 * 1024 + 1

        response = self.client.post("/api/analyze", json=payload)

        self.assertEqual(response.status_code, 413)
        error = response.json()["error"]
        self.assertEqual(error["code"], "image_too_large")
        self.assertFalse(error["retryable"])

    def test_api_analyze_unsupported_category(self) -> None:
        payload = _api_analyze_payload()
        payload["request"]["requestedCategory"] = "Fine Art"

        response = self.client.post("/api/analyze", json=payload)

        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_image")
        self.assertFalse(error["retryable"])

    def test_api_analyze_server_error(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            side_effect=RuntimeError("provider failed"),
        ):
            response = self.client.post(
                "/api/analyze",
                json=_api_analyze_payload(),
            )

        self.assertEqual(response.status_code, 500)
        error = response.json()["error"]
        self.assertEqual(error["code"], "unknown")
        self.assertTrue(error["retryable"])

    def test_root_analyze_multipart_upload(self) -> None:
        response = self.client.post(
            "/analyze",
            data={
                "imageSource": "camera",
                "requestedCategory": "coin",
                "timestamp": "2026-06-30T09:00:00Z",
            },
            files={"image": ("coin.png", _valid_png_bytes(), "image/png")},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["category"], "Coin")
        self.assertEqual(payload["rawProviderPayload"]["provider"], "mock")
        self.assertIn("validate_image", payload["rawProviderPayload"]["pipelineStages"])
        selection = payload["rawProviderPayload"]["mockSelection"]
        self.assertEqual(selection["seedSource"], "base64Image")
        self.assertEqual(selection["byteLength"], len(_valid_png_bytes()))

    def test_root_analyze_camera_style_uploads_do_not_always_return_charizard(self) -> None:
        titles = set()
        for index, image_bytes in enumerate(
            [
                b"\xff\xd8\xff\xe0\x01\x02\xff\xd9",
                b"\xff\xd8\xff\xe0\x09\x08\xff\xd9",
                b"\xff\xd8\xff\xe0abc123\xff\xd9",
            ]
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "imageSource": "camera",
                    "timestamp": f"2026-07-06T00:00:0{index}Z",
                },
                files={"image": ("camera_scan.jpg", image_bytes, "image/jpeg")},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            titles.add(payload["itemName"])
            self.assertEqual(
                payload["rawProviderPayload"]["mockSelection"]["seedSource"],
                "base64Image",
            )

        self.assertGreater(len(titles), 1)
        self.assertNotEqual(titles, {"1999 Pokemon Charizard Holo"})

    def test_root_analyze_provider_unavailable(self) -> None:
        with patch(
            "app.services.analyzer.backend_analyzer_service.BackendAnalyzerService._resolve_provider",
            side_effect=ValueError("Unsupported AI_PROVIDER 'broken'."),
        ):
            response = self.client.post("/analyze", json=_api_analyze_payload())

        self.assertEqual(response.status_code, 503)
        error = response.json()["error"]
        self.assertEqual(error["code"], "provider_unavailable")
        self.assertTrue(error["retryable"])

    def test_scanner_analyze_invalid_extension(self) -> None:
        response = self.client.post(
            "/scanner/analyze",
            files={"image": ("card.gif", b"image-bytes", "image/gif")},
        )

        self.assertEqual(response.status_code, 415)
        self.assertFalse(response.json()["success"])

    def test_scanner_analyze_oversized_image(self) -> None:
        response = self.client.post(
            "/scanner/analyze",
            files={
                "image": (
                    "large.png",
                    b"0" * (10 * 1024 * 1024 + 1),
                    "image/png",
                )
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.json()["success"])

    def test_get_portfolio(self) -> None:
        response = self.client.get("/portfolio")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIsInstance(response.json()["items"], list)

    def test_post_portfolio(self) -> None:
        response = self.client.post(
            "/portfolio",
            json={"id": "test-item", "data": {"title": "Test Card"}},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["item"]["id"], "test-item")
        self.assertEqual(payload["item"]["data"]["title"], "Test Card")

    def test_delete_portfolio(self) -> None:
        self.client.post(
            "/portfolio",
            json={"id": "delete-me", "data": {"title": "Delete Card"}},
        )

        response = self.client.delete("/portfolio/delete-me")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["deletedId"], "delete-me")


def _api_analyze_payload(*, base64_image: bool = False) -> dict:
    image_payload = {
        "fileName": "card.jpg",
        "mimeType": "image/jpeg",
        "sizeBytes": 123456,
        "imageSource": "camera",
        "localFilePath": "/local/app/path/card.jpg",
    }
    if base64_image:
        image_payload["base64Image"] = base64.b64encode(_valid_jpeg_bytes()).decode("ascii")

    return {
        "request": {
            "imagePath": "/local/app/path/card.jpg",
            "imageSource": "camera",
            "requestedCategory": "Pokemon Card",
            "appVersion": "1.0.0",
            "deviceMetadata": {"platform": "android"},
            "timestamp": "2026-06-30T09:00:00Z",
        },
        "image": image_payload,
    }


def _valid_png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00"
    )


def _valid_jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _settings_with_pricing(provider: str):
    return SimpleNamespace(
        environment="test",
        ai_provider="mock",
        allow_mock_analyzer=True,
        pricing_provider=provider,
    )


class _FakePricingProvider:
    provider_name = "fake_market"

    def price(self, recognition) -> PricingResult:
        return PricingResult(
            estimatedMarketValue=42,
            lowEstimate=35,
            highEstimate=50,
            currency="AUD",
            pricingSource="eBay sold comps",
            pricingConfidence=82,
            lastUpdated=utc_timestamp(),
            valuationStatus="market_estimated",
            valuationSource="eBay sold comps",
            aiEstimatedValue=recognition.estimatedValue,
            comparableSales=[
                MarketComparableSale(
                    source="eBay sold comps",
                    title=f"{recognition.title} sold listing",
                    soldPrice=42,
                    currency="AUD",
                    soldDate="2026-07-01T00:00:00Z",
                    condition=recognition.condition,
                )
            ],
            providerDiagnostics={
                "providerCount": "1",
                "providers": "eBay sold comps",
                "comparableCount": "1",
            },
        )


class _FailingPricingProvider:
    provider_name = "failing_market"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def price(self, recognition):
        raise self._error


def _openai_output(
    *,
    title: str = "1999 Pokemon Charizard Holo",
    category: str = "Pokemon Card",
    confidence: int = 94,
    estimated_value: int = 1850,
    condition: str = "Near Mint",
    low_confidence_reasons: list[str] | None = None,
    image_quality_issues: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "category": category,
        "confidence": confidence,
        "estimatedValue": estimated_value,
        "condition": condition,
        "recommendation": "Consider professional grading before selling.",
        "description": "Likely a Base Set Charizard holographic card.",
        "detectedObjects": ["Card", "Pokemon", "Charizard"],
        "fieldConfidence": {
            "itemName": 96,
            "category": 95,
            "brand": 92,
            "setName": 88,
            "year": 82,
            "cardNumber": 78,
            "condition": 70,
        },
        "confidenceLevel": (
            "High" if confidence >= 90 else "Medium" if confidence >= 70 else "Low"
        ),
        "lowConfidenceReasons": low_confidence_reasons or [],
        "imageQualityIssues": image_quality_issues or ["none"],
        "scanRecommendations": [
            "Use bright, even lighting.",
            "Keep the card fully inside the frame.",
        ],
        "primaryMatch": title,
        "alternativeMatches": [
            {
                "title": "2002 Pokemon Expedition Charizard",
                "category": "Pokemon Card",
                "confidence": 72,
                "reason": "Similar fire-type character and card framing.",
            },
            {
                "title": "2016 Pokemon Evolutions Charizard",
                "category": "Pokemon Card",
                "confidence": 68,
                "reason": "Modern reprint shares similar artwork cues.",
            },
            {
                "title": "Pokemon Charizard Promo Card",
                "category": "Pokemon Card",
                "confidence": 61,
                "reason": "Character match is strong, but set details differ.",
            },
        ],
        "confidenceExplanation": "High confidence from artwork and card layout.",
        "detectionQuality": "Good - card border and character are visible.",
        "aiReasoning": "The image matches Charizard TCG visual cues.",
        "year": "1999",
        "brand": "Pokemon",
        "setName": "Base Set",
        "series": "Pokemon TCG",
        "cardNumber": "4/102",
        "playerOrCharacter": "Charizard",
        "rarity": "Holo Rare",
        "estimatedGrade": "PSA 8-9",
        "language": "English",
        "edition": "Unlimited",
        "country": "United States",
        "mint": None,
        "material": "Cardstock",
        "notes": "Verify holo surface and centering before grading.",
    }


def _gemini_response(output: dict) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(output),
                        }
                    ]
                }
            }
        ]
    }


class _FakeOpenAIResponse:
    def __init__(self, *, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict:
        return self._body


class _FakeOpenAIClient:
    def __init__(
        self,
        *,
        response: _FakeOpenAIResponse | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.response = response
        self.exception = exception
        self.last_request: dict | None = None

    def post(self, url: str, **kwargs) -> _FakeOpenAIResponse:
        self.last_request = {"url": url, **kwargs}
        if self.exception is not None:
            raise self.exception
        if self.response is None:
            raise AssertionError("Fake OpenAI client requires a response.")
        return self.response


class _FakeGeminiClient(_FakeOpenAIClient):
    pass


class _StaticHealthProvider:
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
