from pathlib import Path
from typing import Protocol

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.ai.openai_recognition_provider import OpenAIRecognitionProvider


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
