import unittest
from pathlib import Path

from app.services.ai.base_recognition_service import AIRecognitionProvider, AiProvider
from app.services.ai.mock_recognition_service import (
    MOCK_COLLECTIBLES,
    MockAiProvider,
    MockRecognitionProvider,
    MockRecognitionService,
)
from app.services.ai.openai_recognition_provider import (
    OpenAIRecognitionProvider,
    OpenAiVisionProvider,
)
from app.services.ai.provider_factory import get_ai_recognition_provider


class MockRecognitionProviderTest(unittest.TestCase):
    def test_recognize_returns_complete_mock_result(self) -> None:
        result = MockRecognitionProvider().recognize(Path("uploads/card.png"))

        self.assertIn(result.title, {item.title for item in MOCK_COLLECTIBLES})
        self.assertIn(
            result.category,
            {
                "Pokemon Card",
                "Sports Card",
                "Trading Card",
                "Coin",
                "Comic",
                "Toy/Figure",
            },
        )
        self.assertGreaterEqual(result.confidence, 0)
        self.assertLessEqual(result.confidence, 100)
        self.assertGreater(result.estimatedValue, 0)
        self.assertTrue(result.condition)
        self.assertTrue(result.recommendation)
        self.assertTrue(result.description)
        self.assertTrue(result.detectedObjects)
        self.assertEqual(result.aiProvider, "mock")
        self.assertGreater(result.processingTimeMs, 0)
        self.assertEqual(result.primaryMatch, result.title)
        self.assertEqual(len(result.alternativeMatches), 3)
        self.assertTrue(result.confidenceExplanation)
        self.assertTrue(result.detectionQuality)
        self.assertTrue(result.aiReasoning)
        self.assertTrue(
            any(
                [
                    result.year,
                    result.brand,
                    result.setName,
                    result.series,
                    result.cardNumber,
                    result.playerOrCharacter,
                    result.rarity,
                    result.estimatedGrade,
                    result.language,
                    result.edition,
                    result.country,
                    result.mint,
                    result.material,
                    result.notes,
                ]
            )
        )

    def test_recognize_returns_varied_mock_results(self) -> None:
        provider = MockRecognitionProvider()

        titles = {
            provider.recognize(Path(f"uploads/card-{index}.png")).title
            for index in range(8)
        }

        self.assertGreater(len(titles), 1)

    def test_backwards_compatible_mock_service_alias(self) -> None:
        self.assertIs(MockRecognitionService, MockRecognitionProvider)

    def test_product_provider_aliases(self) -> None:
        self.assertIs(AiProvider, AIRecognitionProvider)
        self.assertIs(MockAiProvider, MockRecognitionProvider)
        self.assertIs(OpenAiVisionProvider, OpenAIRecognitionProvider)


class ProviderFactoryTest(unittest.TestCase):
    def test_provider_factory_uses_mock_by_default(self) -> None:
        provider = get_ai_recognition_provider()

        self.assertIsInstance(provider, MockRecognitionProvider)

    def test_provider_factory_defaults_to_mock(self) -> None:
        provider = get_ai_recognition_provider("mock")

        self.assertIsInstance(provider, MockRecognitionProvider)

    def test_provider_factory_supports_openai_placeholder(self) -> None:
        provider = get_ai_recognition_provider("openai")

        self.assertIsInstance(provider, OpenAIRecognitionProvider)

    def test_provider_factory_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            get_ai_recognition_provider("unknown")


if __name__ == "__main__":
    unittest.main()
