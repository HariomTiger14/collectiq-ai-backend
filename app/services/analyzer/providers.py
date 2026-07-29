from pathlib import Path
from typing import Protocol

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.ai.gemini_recognition_provider import GeminiRecognitionProvider
from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.ai.openai_recognition_provider import (
    AIProviderNotConfiguredError,
    OpenAIRecognitionProvider,
)
from app.services.analyzer.errors import AnalyzerPipelineError


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


class AutoAnalyzerProvider:
    provider_name = "auto"

    def __init__(
        self,
        *,
        providers: list[BackendAnalyzerProvider],
        requested_provider: str = "auto",
        confidence_threshold: int = 0,
        allow_mock_fallback: bool = False,
    ) -> None:
        self._providers = providers
        self._requested_provider = requested_provider
        self._confidence_threshold = max(0, min(100, confidence_threshold))
        self._allow_mock_fallback = allow_mock_fallback
        self._selection_diagnostics: dict[str, object] = {
            "requestedProvider": self._requested_provider,
            "preferredOrder": [item.provider_name for item in self._providers]
            + ([MockAnalyzerProvider.provider_name] if allow_mock_fallback else []),
        }

    def recognize_api_payload(
        self,
        *,
        request_metadata: dict,
        image_payload: dict,
    ) -> RecognitionResult:
        attempts: list[dict[str, object]] = []
        provider_errors: list[str] = []
        for provider in self._providers:
            try:
                result = provider.recognize_api_payload(
                    request_metadata=request_metadata,
                    image_payload=image_payload,
                )
                attempts.append(
                    {
                        "provider": provider.provider_name,
                        "status": "completed",
                        "confidence": result.confidence,
                    }
                )
                self._selection_diagnostics = {
                    "selectedProvider": provider.provider_name,
                    "requestedProvider": self._requested_provider,
                    "fallbackUsed": len(attempts) > 1,
                    "attempts": attempts,
                    "preferredOrder": [item.provider_name for item in self._providers],
                    "confidenceThreshold": self._confidence_threshold,
                }
                return result
            except AnalyzerPipelineError:
                raise
            except Exception as exc:
                provider_errors.append(
                    f"{provider.provider_name}:{exc.__class__.__name__}: {exc}"
                )
                attempts.append(
                    {
                        "provider": provider.provider_name,
                        "status": "failed",
                        "error": exc.__class__.__name__,
                    }
                )

        if self._allow_mock_fallback:
            mock = MockAnalyzerProvider()
            result = mock.recognize_api_payload(
                request_metadata=request_metadata,
                image_payload=image_payload,
            )
            attempts.append(
                {
                    "provider": mock.provider_name,
                    "status": "completed",
                    "confidence": result.confidence,
                }
            )
            self._selection_diagnostics = {
                "selectedProvider": mock.provider_name,
                "requestedProvider": self._requested_provider,
                "fallbackUsed": True,
                "attempts": attempts,
                "preferredOrder": [item.provider_name for item in self._providers]
                + [mock.provider_name],
            }
            return result

        raise _pipeline_error_from_provider_failures(provider_errors)

    @property
    def selection_diagnostics(self) -> dict[str, object]:
        return self._selection_diagnostics


FallbackAnalyzerProvider = AutoAnalyzerProvider


def _pipeline_error_from_provider_failures(
    provider_errors: list[str],
) -> AnalyzerPipelineError:
    combined_errors = " ".join(provider_errors).lower()

    if provider_errors and all(
        f":{AIProviderNotConfiguredError.__name__}:" in error
        for error in provider_errors
    ):
        return AnalyzerPipelineError(
            status_code=503,
            code="AI_PROVIDER_NOT_CONFIGURED",
            message="No configured AI provider could analyze this image.",
            retryable=False,
            details={"providerErrors": provider_errors},
        )

    if "timeout" in combined_errors or "timed out" in combined_errors:
        return AnalyzerPipelineError(
            status_code=504,
            code="AI_PROVIDER_TIMEOUT",
            message="AI image analysis timed out. Please retry the scan.",
            retryable=True,
            details={"providerErrors": provider_errors},
        )

    if (
        "status 429" in combined_errors
        or "resource_exhausted" in combined_errors
        or "quota" in combined_errors
        or "rate limit" in combined_errors
    ):
        return AnalyzerPipelineError(
            status_code=429,
            code="AI_PROVIDER_QUOTA_EXCEEDED",
            message="AI image analysis quota is temporarily exhausted.",
            retryable=True,
            details={"providerErrors": provider_errors},
        )

    if (
        "status 404" in combined_errors
        or "not found for api version" in combined_errors
        or "is not supported for generatecontent" in combined_errors
        or "no longer available to new users" in combined_errors
    ):
        return AnalyzerPipelineError(
            status_code=503,
            code="AI_PROVIDER_MODEL_UNAVAILABLE",
            message="The configured AI model is unavailable. Check the model setting.",
            retryable=False,
            details={"providerErrors": provider_errors},
        )

    if (
        "structured output must be a json object" in combined_errors
        or "structured output was not a json object" in combined_errors
        or "structured output was not valid json" in combined_errors
        or "invalidresponse" in combined_errors
    ):
        return AnalyzerPipelineError(
            status_code=502,
            code="AI_PROVIDER_INVALID_RESPONSE",
            message="AI image analysis returned an invalid structured response.",
            retryable=True,
            details={"providerErrors": provider_errors},
        )

    return AnalyzerPipelineError(
        status_code=503,
        code="AI_PROVIDER_UNAVAILABLE",
        message="AI image analysis is temporarily unavailable.",
        retryable=True,
        details={"providerErrors": provider_errors},
    )


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
