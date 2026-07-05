from app.core.config import settings
from app.services.ai.base_recognition_service import AIRecognitionProvider
from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.ai.openai_recognition_provider import OpenAIRecognitionProvider


_mock_provider = MockRecognitionProvider()
_openai_provider = OpenAIRecognitionProvider()


def get_ai_recognition_provider(
    provider_name: str | None = None,
) -> AIRecognitionProvider:
    selected_provider = (provider_name or settings.ai_provider).strip().lower()

    if selected_provider == "mock":
        return _mock_provider

    if selected_provider == "openai":
        return _openai_provider

    raise ValueError(
        f"Unsupported AI_PROVIDER '{selected_provider}'. "
        "Supported providers: mock, openai."
    )


# Backwards-compatible alias for existing scanner imports.
get_ai_recognition_service = get_ai_recognition_provider
