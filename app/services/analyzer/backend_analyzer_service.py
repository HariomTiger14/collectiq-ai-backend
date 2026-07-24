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
    image_payloads: list[dict]
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
        image_payloads = self._normalize_image_metadata(payload)
        image_payload = image_payloads[0]
        stages.extend(["validate_image", "normalize_image_metadata"])

        provider = self._resolve_provider()
        stages.append("call_provider")
        recognition = recognize_with_legacy_provider(
            provider,
            request_metadata=request_metadata,
            image_payload={**image_payload, "images": image_payloads},
        )
        recognition = self._normalize_provider_output(recognition)
        stages.extend(["normalize_provider_output", "assign_confidence"])
        return AnalyzerPipelineResult(
            provider=provider,
            recognition=recognition,
            request_metadata=request_metadata,
            image_payload=image_payload,
            image_payloads=image_payloads,
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

    def _normalize_image_metadata(self, payload: ApiAnalyzeRequest) -> list[dict]:
        images = [*payload.images]
        if payload.image is not None:
            images.insert(0, payload.image)

        normalized_payloads: list[dict] = []
        seen_keys: set[tuple[str, str | None]] = set()
        for image in images:
            image_payload = _model_to_dict(image)
            key = (
                str(image_payload.get("localFilePath") or ""),
                image_payload.get("imageRole"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            normalized = self._image_validator.validate_metadata(image_payload)
            normalized_payloads.append(normalized.to_api_payload())
        return normalized_payloads

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
        if selected_provider == "mock":
            if (
                settings.environment.strip().lower() != "local"
                and not getattr(settings, "allow_mock_analyzer", False)
            ):
                if settings.gemini_api_key.strip() or settings.openai_api_key.strip():
                    return AutoAnalyzerProvider(
                        providers=_configured_real_providers(),
                        requested_provider="auto",
                        confidence_threshold=_fallback_confidence_threshold(),
                        allow_mock_fallback=False,
                    )
                from app.services.analyzer.errors import AnalyzerPipelineError

                raise AnalyzerPipelineError(
                    status_code=503,
                    code="AI_PROVIDER_NOT_CONFIGURED",
                    message="Real AI provider credentials are required in SIT.",
                    retryable=False,
                    details={"providerErrors": ["gemini:missing", "openai:missing"]},
                )
            return MockAnalyzerProvider()
        if selected_provider == "openai":
            return OpenAIAnalyzerProvider()
        if selected_provider == "gemini":
            return AutoAnalyzerProvider(
                providers=[GeminiAnalyzerProvider(), MockAnalyzerProvider()],
                requested_provider="gemini",
                confidence_threshold=_fallback_confidence_threshold(),
                allow_mock_fallback=getattr(settings, "allow_mock_analyzer", False),
            )
        if selected_provider in {"auto", "gemini_openai", "gemini+openai"}:
            return AutoAnalyzerProvider(
                providers=_configured_real_providers(),
                requested_provider=selected_provider,
                confidence_threshold=_fallback_confidence_threshold(),
                allow_mock_fallback=getattr(settings, "allow_mock_analyzer", False),
            )

        return get_ai_recognition_provider(selected_provider)


def _model_to_dict(model) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _optional_string(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _fallback_confidence_threshold() -> int:
    return int(getattr(settings, "ai_fallback_confidence_threshold", 0) or 0)


def _configured_real_providers() -> list[BackendAnalyzerProvider]:
    providers: list[BackendAnalyzerProvider] = []
    if settings.gemini_api_key.strip():
        providers.append(GeminiAnalyzerProvider())
    if settings.openai_api_key.strip():
        providers.append(OpenAIAnalyzerProvider())
    if not providers:
        providers.extend([GeminiAnalyzerProvider(), OpenAIAnalyzerProvider()])
    return providers
