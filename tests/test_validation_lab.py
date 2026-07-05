import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.prepare_validation_dataset import (
    ManifestRow,
    calculate_metrics,
    category_matches,
    load_manifest,
    name_keyword_matches,
    prepare_manifest,
    run_validation,
    score_response,
)


class ValidationLabTest(unittest.TestCase):
    def test_load_manifest_json_uses_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "manifest.json"
            manifest.write_text(
                """[
                  {
                    "filename": "card.jpg",
                    "expected_name": "1999 Pokemon Charizard Holo",
                    "expected_category": "Pokemon Card",
                    "expected_brand": "Pokemon",
                    "expected_set": "Base Set",
                    "expected_year": "1999",
                    "expected_price_min": 1000,
                    "expected_price_max": 3000,
                    "source": "user",
                    "license": "user-owned",
                    "notes": "test"
                  }
                ]""",
                encoding="utf-8",
            )

            rows = load_manifest(manifest)

        self.assertEqual(rows[0].filename, "card.jpg")
        self.assertEqual(rows[0].expected_category, "Pokemon Card")
        self.assertEqual(rows[0].expected_price_min, 1000)

    def test_load_manifest_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "manifest.csv"
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["filename", "expected_name", "expected_category"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "filename": "coin.jpg",
                        "expected_name": "1921 Morgan Silver Dollar",
                        "expected_category": "Coin",
                    }
                )

            rows = load_manifest(manifest)

        self.assertEqual(rows[0].filename, "coin.jpg")
        self.assertEqual(rows[0].expected_category, "Coin")

    def test_prepare_manifest_from_local_image_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            images = Path(temp_dir) / "images"
            manifest = Path(temp_dir) / "manifest.json"
            source.mkdir()
            (source / "card.jpg").write_bytes(b"image")
            (source / "ignore.txt").write_text("not image", encoding="utf-8")

            rows = prepare_manifest(
                image_folder=source,
                output_manifest=manifest,
                copy_images_to=images,
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].filename, "card.jpg")
            self.assertTrue((images / "card.jpg").exists())

    def test_scoring_helpers(self) -> None:
        self.assertTrue(category_matches("Pokemon Card", "Pokemon Card"))
        self.assertFalse(category_matches("Pokemon Card", "Coin"))
        self.assertTrue(
            name_keyword_matches(
                "1999 Pokemon Charizard Holo",
                "1999 Pokemon Base Set Charizard Holo",
            )
        )

    def test_score_response_and_metrics(self) -> None:
        row = ManifestRow(
            filename="card.jpg",
            expected_name="1999 Pokemon Charizard Holo",
            expected_category="Pokemon Card",
            expected_brand="Pokemon",
            expected_set="Base Set",
            expected_year="1999",
            expected_price_min=1000,
            expected_price_max=3000,
        )

        result = score_response(row, _sample_response(), latency_ms=123)
        metrics = calculate_metrics([result])

        self.assertTrue(result.category_match)
        self.assertTrue(result.name_keyword_match)
        self.assertTrue(result.price_in_range)
        self.assertEqual(metrics["category_accuracy"], 1)
        self.assertEqual(metrics["average_latency_ms"], 123)

    def test_run_validation_writes_reports_without_paid_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_dir = root / "images"
            reports_dir = root / "reports"
            manifest = root / "manifest.json"
            image_dir.mkdir()
            (image_dir / "card.jpg").write_bytes(b"image")
            manifest.write_text(
                """[
                  {
                    "filename": "card.jpg",
                    "expected_name": "1999 Pokemon Charizard Holo",
                    "expected_category": "Pokemon Card",
                    "expected_price_min": 1000,
                    "expected_price_max": 3000
                  }
                ]""",
                encoding="utf-8",
            )

            with patch(
                "scripts.prepare_validation_dataset.call_analyze",
                return_value=_sample_response(),
            ):
                output = run_validation(
                    manifest_path=manifest,
                    image_dir=image_dir,
                    reports_dir=reports_dir,
                    dry_run=False,
                )

            self.assertEqual(output["metrics"]["analyzed_images"], 1)
            self.assertTrue((reports_dir / "latest_validation_report.md").exists())
            self.assertTrue((reports_dir / "latest_validation_results.csv").exists())


def _sample_response() -> dict:
    return {
        "itemName": "1999 Pokemon Base Set Charizard Holo",
        "category": "Pokemon Card",
        "estimatedValue": 1850,
        "confidence": 94,
        "keyAttributes": {
            "brand": "Pokemon",
            "setName": "Base Set",
            "year": "1999",
        },
        "marketSummary": {"sources": ["Mock"]},
        "diagnostics": {"pricingFallbackReason": ""},
    }


if __name__ == "__main__":
    unittest.main()
