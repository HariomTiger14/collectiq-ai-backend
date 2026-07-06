from pathlib import Path
import logging
import time
from typing import Protocol

from app.services.ai.base_recognition_service import RecognitionResult
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


class GeminiAnalyzerProvider:
    provider_name = "gemini"

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        raise NotImplementedError(
            "Gemini analyzer provider is a backend-only placeholder."
        )


class AutoAnalyzerProvider:
    provider_name = "auto"

    def __init__(
        self,
        *,
        vision_provider: BackendAnalyzerProvider | None = None,
        fallback_provider: MockAnalyzerProvider | None = None,
    ) -> None:
        self._vision_provider = vision_provider or OpenAIAnalyzerProvider()
        self._fallback_provider = fallback_provider or MockAnalyzerProvider()
        self._selected_provider_name = "auto"
        self._selected_model = "auto"
        self._fallback_reason: str | None = None
        self._selection_diagnostics: dict[str, object] = {
            "requestedProvider": "auto",
            "preferredOrder": ["openai", "mock"],
        }

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        started_at = time.perf_counter()
        try:
            recognition = self._vision_provider.recognize_api_payload(
                request_metadata=request_metadata,
                image_payload=image_payload,
            )
        except (
            AIProviderNotConfiguredError,
            OpenAIInvalidResponseError,
            OpenAIProviderError,
            OpenAITimeoutError,
        ) as exc:
            self._fallback_reason = type(exc).__name__
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Analyzer provider fallback selectedProvider=mock "
                    "fallbackReason=%s",
                    self._fallback_reason,
                )
            recognition = self._fallback_provider.recognize_api_payload(
                request_metadata=request_metadata,
                image_payload=image_payload,
            )
            self._selected_provider_name = self._fallback_provider.provider_name
            self._selected_model = getattr(self._fallback_provider, "_model", "mock")
            self._selection_diagnostics = {
                "requestedProvider": "auto",
                "selectedProvider": self._selected_provider_name,
                "fallbackReason": self._fallback_reason,
                "mockSelection": self._fallback_provider.selection_diagnostics,
            }
            return recognition

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        self._selected_provider_name = getattr(
            self._vision_provider,
            "provider_name",
            recognition.aiProvider,
        )
        self._selected_model = getattr(self._vision_provider, "_model", "unknown")
        self._selection_diagnostics = {
            "requestedProvider": "auto",
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

    @property
    def provider_name(self) -> str:
        return self._selected_provider_name

    @property
    def _model(self) -> str:
        return self._selected_model

    @property
    def selection_diagnostics(self) -> dict[str, object]:
        return self._selection_diagnostics


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
