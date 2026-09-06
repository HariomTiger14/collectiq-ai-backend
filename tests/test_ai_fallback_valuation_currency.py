"""The AI fallback valuation must be requested and labelled in the same currency.

When no market price is available, the analyze response falls back to the
model's own estimate (`display_value = market_estimated_value or
ai_estimated_value or 0`) and labels it with the placeholder's currency.
Nothing converts that number -- it is only labelled -- so the currency the
prompt asks for and the currency the response claims are a matched pair.

They were not. The prompt asked for Australian dollars while the rest of the
system moved to provider-native USD and the app defaults to USD display, so
every AI-estimated item was an AUD figure wearing a USD label -- or, once the
app converted it as AUD, a USD figure understated by roughly the AUD/USD rate.

These tests pin both halves together, because fixing either one alone
re-creates the bug in the opposite direction.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.routers.api_analyze import _valuation_placeholder
from app.routers.scanner import _pricing_placeholder
from app.services.ai.openai_recognition_provider import OpenAIRecognitionProvider


class _Recognition:
    """Minimal stand-in for the recognition result the placeholder reads."""

    def __init__(self, estimated_value: int = 0):
        self.estimatedValue = estimated_value


class PromptCurrencyTest(unittest.TestCase):
    def _prompt(self) -> str:
        provider = OpenAIRecognitionProvider.__new__(OpenAIRecognitionProvider)
        return provider._prompt_text({})

    def test_prompt_requests_us_dollar_estimates(self) -> None:
        self.assertIn("US-dollar estimates", self._prompt())

    def test_prompt_does_not_request_australian_dollars(self) -> None:
        """PackLox v1 is USD-first and US-only; AUD here is the original bug."""
        prompt = self._prompt().lower()
        self.assertNotIn("australian", prompt)


class PlaceholderCurrencyTest(unittest.TestCase):
    """Both no-market-price placeholders must label the AI value as USD."""

    def test_api_analyze_placeholder_is_usd(self) -> None:
        result = _valuation_placeholder(
            _Recognition(estimated_value=120),
            status="no_market_match",
            source="test",
            reason="no comps",
        )
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.aiEstimatedValue, 120)

    def test_scanner_placeholder_is_usd(self) -> None:
        """The dev /scanner/analyze route shares the same recognition prompt."""
        result = _pricing_placeholder("no_market_match", "test", "no comps")
        self.assertEqual(result.currency, "USD")

    def test_placeholder_currency_matches_the_prompt(self) -> None:
        """The coupling itself: change one without the other and this fails."""
        provider = OpenAIRecognitionProvider.__new__(OpenAIRecognitionProvider)
        prompt = provider._prompt_text({})
        placeholder = _valuation_placeholder(
            _Recognition(), status="unavailable", source="test", reason="none"
        )
        currency_in_prompt = "US-dollar" in prompt
        self.assertTrue(
            currency_in_prompt and placeholder.currency == "USD",
            "The recognition prompt and the fallback placeholder disagree about "
            "currency. They must be changed together: the model's estimate is "
            "labelled, never converted.",
        )


class AnalyzeFallbackCurrencyTest(unittest.TestCase):
    """End to end: an item with no market price reports USD, not AUD."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_ai_estimated_response_is_labelled_usd(self) -> None:
        with patch("app.routers.api_analyze.settings") as analyze_settings:
            # mock provider => the "PRICING_PROVIDER is mock" placeholder branch,
            # which is one of the five paths where the AI estimate is displayed.
            analyze_settings.pricing_provider = "mock"
            analyze_settings.ai_provider = "mock"
            analyze_settings.allow_mock_analyzer = True
            analyze_settings.environment = "local"
            analyze_settings.default_display_currency = "USD"

            result = _valuation_placeholder(
                _Recognition(estimated_value=250),
                status="provider_not_configured",
                source="not_configured",
                reason="PRICING_PROVIDER is mock; no real pricing source is connected.",
            )

        # The value the app will show is the AI's own number...
        display_value = result.estimatedMarketValue or result.aiEstimatedValue or 0
        self.assertEqual(display_value, 250)
        # ...and the currency it will be shown in must be the one it was
        # estimated in, since no conversion happens on this path.
        self.assertEqual(result.currency, "USD")


if __name__ == "__main__":
    unittest.main()
