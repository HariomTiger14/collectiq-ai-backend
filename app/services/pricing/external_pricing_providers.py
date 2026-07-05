from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    PricingProvider,
    PricingProviderUnavailableError,
    PricingResult,
)


class _FuturePricingProvider(PricingProvider):
    provider_name = "future"
    provider_label = "Future pricing provider"

    def price(self, recognition: RecognitionResult) -> PricingResult:
        raise PricingProviderUnavailableError(
            f"{self.provider_label} is not configured yet. "
            "Keep PRICING_PROVIDER=mock until backend credentials and API mapping are ready."
        )

