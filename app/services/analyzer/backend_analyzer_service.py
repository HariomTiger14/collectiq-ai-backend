from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import settings
from app.schemas.api_analysis import ApiAnalyzeRequest
from app.services.ai.base_recognition_service import RecognitionResult
from app.services.ai.provider_factory import get_ai_recognition_provider
from app.services.analyzer.image_validation import AnalyzerImageValidator
from app.services.analyzer.providers import (
    AutoAnalyzerProvider,
    BackendAnalyzerProvider,
    FallbackAnalyzerProvider,
    GeminiAnalyzerProvider,
    MockAnalyzerProvider,
    OpenAIAnalyzerProvider,
    recognize_with_legacy_provider,
)


@dataclass(frozen=True)
class AnalyzerPipelineResult:
    provider: object
    recognition: RecognitionResult
    request_metadata: dict
    image_payload: dict
    stages: list[str]


class BackendAnalyzerService:
    """Server-side analyzer boundary used by the production mobile contract."""

    def __init__(
        self,
        provider_factory: Callable[[str | None], object] | None = None,
        image_validator: AnalyzerImageValidator | None = None,
    ) -> None:
        self._provider_factory = provider_factory
        self._image_validator = image_validator or AnalyzerImageValidator()

    def analyze(self, payload: ApiAnalyzeRequest) -> AnalyzerPipelineResult:
        stages: list[str] = []
        request_metadata = self._normalize_request_metadata(payload)
        image_payload = self._normalize_image_metadata(payload)
        stages.extend(["validate_image", "normalize_image_metadata"])

        provider = self._resolve_provider()
        stages.append("call_provider")
        recognition = recognize_with_legacy_provider(
            provider,
            request_metadata=request_metadata,
            image_payload=image_payload,
        )
        recognition = self._normalize_provider_output(recognition)
        stages.extend(["normalize_provider_output", "assign_confidence"])
        return AnalyzerPipelineResult(
            provider=provider,
            recognition=recognition,
            request_metadata=request_metadata,
            image_payload=image_payload,
            stages=stages,
        )

    def _normalize_request_metadata(self, payload: ApiAnalyzeRequest) -> dict:
        request_metadata = _model_to_dict(payload.request)
        request_metadata["imageSource"] = (
            str(request_metadata.get("imageSource") or "unknown").strip() or "unknown"
        )
        request_metadata["requestedCategory"] = _optional_string(
            request_metadata.get("requestedCategory")
        )
        return request_metadata

    def _normalize_image_metadata(self, payload: ApiAnalyzeRequest) -> dict:
        image_payload = _model_to_dict(payload.image)
        normalized = self._image_validator.validate_metadata(image_payload)
        return normalized.to_api_payload()

    def _normalize_provider_output(
        self,
        recognition: RecognitionResult,
    ) -> RecognitionResult:
        if recognition.confidence < 0 or recognition.confidence > 100:
            return RecognitionResult(
                **{
                    **recognition.__dict__,
                    "confidence": max(0, min(100, int(recognition.confidence))),
                }
            )

        return recognition

    def _resolve_provider(self) -> BackendAnalyzerProvider | object:
        if self._provider_factory is not None:
            return self._provider_factory(None)

        selected_provider = settings.ai_provider.strip().lower()
        if selected_provider in {"auto", "real", "vision"}:
            return AutoAnalyzerProvider()
        if selected_provider == "gemini":
            return FallbackAnalyzerProvider(
                requested_provider="gemini",
                providers=[GeminiAnalyzerProvider()],
            )
        if selected_provider == "openai":
            return FallbackAnalyzerProvider(
                requested_provider="openai",
                providers=[OpenAIAnalyzerProvider()],
            )
        if (
            selected_provider == "mock"
            and settings.environment.strip().lower() == "sit"
            and (
                settings.gemini_api_key.strip()
                or settings.openai_api_key.strip()
            )
        ):
            return AutoAnalyzerProvider()
        if selected_provider == "mock":
            return MockAnalyzerProvider()

        return get_ai_recognition_provider(selected_provider)


def _model_to_dict(model) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _optional_string(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
