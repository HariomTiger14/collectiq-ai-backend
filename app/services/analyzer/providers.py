from pathlib import Path
import logging
import re
import time
from typing import Protocol

from app.core.config import settings
from app.services.ai.base_recognition_service import RecognitionResult
from app.services.ai.gemini_recognition_provider import (
    GeminiInvalidResponseError,
    GeminiProviderError,
    GeminiRecognitionProvider,
    GeminiTimeoutError,
)
from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.ai.openai_recognition_provider import (
    AIProviderNotConfiguredError,
    OpenAIInvalidResponseError,
    OpenAIProviderError,
    OpenAIRecognitionProvider,
    OpenAITimeoutError,
)


logger = logging.getLogger("collectiq.analyzer.providers")


class BackendAnalyzerProvider(Protocol):
    provider_name: str

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        """Analyze a backend contract payload with server-side credentials."""
        ...


class MockAnalyzerProvider:
    provider_name = "mock"

    def __init__(self, delegate: MockRecognitionProvider | None = None) -> None:
        self._delegate = delegate or MockRecognitionProvider()

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        return self._delegate.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )

    @property
    def selection_diagnostics(self) -> dict[str, object]:
        return self._delegate.last_selection_diagnostics


class OpenAIAnalyzerProvider:
    provider_name = "openai"

    def __init__(self, delegate: OpenAIRecognitionProvider | None = None) -> None:
        self._delegate = delegate or OpenAIRecognitionProvider()

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        return self._delegate.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )

    @property
    def _model(self) -> str:
        return getattr(self._delegate, "_model", "openai")


class GeminiAnalyzerProvider:
    provider_name = "gemini"

    def __init__(self, delegate: GeminiRecognitionProvider | None = None) -> None:
        self._delegate = delegate or GeminiRecognitionProvider()

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        return self._delegate.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )

    @property
    def _model(self) -> str:
        return getattr(self._delegate, "_model", "gemini")


class FallbackAnalyzerProvider:
    provider_name = "auto"

    def __init__(
        self,
        *,
        requested_provider: str,
        providers: list[BackendAnalyzerProvider],
        fallback_provider: MockAnalyzerProvider | None = None,
    ) -> None:
        self._requested_provider = requested_provider
        self._providers = providers
        self._fallback_provider = fallback_provider or MockAnalyzerProvider()
        self._selected_provider_name = requested_provider
        self._selected_model = requested_provider
        self._fallback_reason: str | None = None
        self._selection_diagnostics: dict[str, object] = {
            "requestedProvider": requested_provider,
            "preferredOrder": [
                getattr(provider, "provider_name", "unknown")
                for provider in providers
            ]
            + ["mock"],
        }

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        errors: list[str] = []
        for provider in self._providers:
            started_at = time.perf_counter()
            try:
                recognition = provider.recognize_api_payload(
                    request_metadata=request_metadata,
                    image_payload=image_payload,
                )
            except _FALLBACK_PROVIDER_ERRORS as exc:
                provider_name = getattr(provider, "provider_name", "unknown")
                errors.append(f"{provider_name}:{_safe_exception_summary(exc)}")
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Analyzer provider failed provider=%s fallbackReason=%s",
                        provider_name,
                        _safe_exception_summary(exc),
                    )
                continue

            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self._selected_provider_name = getattr(
                provider,
                "provider_name",
                recognition.aiProvider,
            )
            self._selected_model = getattr(provider, "_model", "unknown")
            self._selection_diagnostics = {
                "requestedProvider": self._requested_provider,
                "selectedProvider": self._selected_provider_name,
                "model": self._selected_model,
                "analysisDurationMs": duration_ms,
                "confidence": recognition.confidence,
            }
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Analyzer provider selected=%s model=%s latencyMs=%s confidence=%s",
                    self._selected_provider_name,
                    self._selected_model,
                    duration_ms,
                    recognition.confidence,
                )
            return recognition

        self._fallback_reason = ";".join(errors) or "provider_unavailable"
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Analyzer provider fallback selectedProvider=mock fallbackReason=%s",
                self._fallback_reason,
            )
        recognition = self._fallback_provider.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )
        self._selected_provider_name = self._fallback_provider.provider_name
        self._selected_model = getattr(self._fallback_provider, "_model", "mock")
        self._selection_diagnostics = {
            "requestedProvider": self._requested_provider,
            "selectedProvider": self._selected_provider_name,
            "fallbackReason": self._fallback_reason,
            "mockSelection": self._fallback_provider.selection_diagnostics,
        }
        return recognition

    @property
    def provider_name(self) -> str:
        return self._selected_provider_name

    @property
    def _model(self) -> str:
        return self._selected_model

    @property
    def selection_diagnostics(self) -> dict[str, object]:
        return self._selection_diagnostics


class AutoAnalyzerProvider(FallbackAnalyzerProvider):
    def __init__(
        self,
        *,
        providers: list[BackendAnalyzerProvider] | None = None,
        fallback_provider: MockAnalyzerProvider | None = None,
    ) -> None:
        super().__init__(
            requested_provider="auto",
            providers=providers or [GeminiAnalyzerProvider(), OpenAIAnalyzerProvider()],
            fallback_provider=fallback_provider,
        )


_FALLBACK_PROVIDER_ERRORS = (
    AIProviderNotConfiguredError,
    GeminiInvalidResponseError,
    GeminiProviderError,
    GeminiTimeoutError,
    OpenAIInvalidResponseError,
    OpenAIProviderError,
    OpenAITimeoutError,
)


def _safe_exception_summary(exc: Exception) -> str:
    message = _sanitize_error_text(str(exc).strip())
    error_type = type(exc).__name__
    if not message:
        return error_type
    return f"{error_type}:{message}"


def _sanitize_error_text(message: str) -> str:
    sanitized = message
    for secret in (settings.gemini_api_key, settings.openai_api_key):
        if secret and len(secret) >= 8:
            sanitized = sanitized.replace(secret, "<redacted>")

    sanitized = re.sub(
        r"(?i)(key|api_key|token|access_token)=([^&\s]+)",
        r"\1=<redacted>",
        sanitized,
    )
    sanitized = re.sub(r"[A-Za-z0-9+/]{160,}={0,2}", "<redacted>", sanitized)
    return sanitized[:500]


def recognize_with_legacy_provider(
    provider,
    *,
    request_metadata: dict,
    image_payload: dict,
) -> RecognitionResult:
    if hasattr(provider, "recognize_api_payload"):
        return provider.recognize_api_payload(
            request_metadata=request_metadata,
            image_payload=image_payload,
        )

    return provider.recognize(Path(image_payload.get("localFilePath") or "uploads/mock.jpg"))
