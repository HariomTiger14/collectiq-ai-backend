import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.import_validation_dataset import import_dataset, load_metadata


class DatasetImporterTest(unittest.TestCase):
    def test_manifest_generation_from_sample_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "charizard.jpg").write_bytes(b"image")
            metadata = root / "metadata.csv"
            output = root / "generated_manifest.json"
            with metadata.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["filename", "title", "category", "brand", "set", "year"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "filename": "charizard.jpg",
                        "title": "1999 Pokemon Charizard Holo",
                        "category": "Pokemon Card",
                        "brand": "Pokemon",
                        "set": "Base Set",
                        "year": "1999",
                    }
                )

            summary = import_dataset(
                image_folder=images,
                metadata_path=metadata,
                output_manifest=output,
                source_name="csv-test",
                license_note="user-owned",
            )
            rows = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(len(summary.rows), 1)
        self.assertEqual(rows[0]["filename"], "charizard.jpg")
        self.assertEqual(rows[0]["expected_name"], "1999 Pokemon Charizard Holo")
        self.assertEqual(rows[0]["expected_category"], "Pokemon Card")
        self.assertEqual(rows[0]["expected_set"], "Base Set")

    def test_manifest_generation_from_sample_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "coin.png").write_bytes(b"image")
            metadata = root / "metadata.json"
            output = root / "generated_manifest.json"
            metadata.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "image_filename": "coin.png",
                                "item_name": "1921 Morgan Silver Dollar",
                                "type": "Coin",
                                "mint": "United States Mint",
                                "date": "1921-01-01",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            import_dataset(
                image_folder=images,
                metadata_path=metadata,
                output_manifest=output,
            )
            rows = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(rows[0]["expected_name"], "1921 Morgan Silver Dollar")
        self.assertEqual(rows[0]["expected_category"], "Coin")
        self.assertEqual(rows[0]["expected_brand"], "United States Mint")
        self.assertEqual(rows[0]["expected_year"], "1921")

    def test_missing_image_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metadata = root / "metadata.json"
            output = root / "generated_manifest.json"
            metadata.write_text(
                json.dumps([{"filename": "missing.jpg", "name": "Missing Card"}]),
                encoding="utf-8",
            )

            summary = import_dataset(
                image_folder=root / "images",
                metadata_path=metadata,
                output_manifest=output,
            )
            rows = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary.missing_images, 1)
        self.assertIn("Image file missing", rows[0]["notes"])

    def test_duplicate_filename_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "card.jpg").write_bytes(b"image")
            metadata = root / "metadata.csv"
            output = root / "generated_manifest.json"
            with metadata.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["filename", "name"])
                writer.writeheader()
                writer.writerow({"filename": "card.jpg", "name": "First"})
                writer.writerow({"filename": "card.jpg", "name": "Second"})

            summary = import_dataset(
                image_folder=images,
                metadata_path=metadata,
                output_manifest=output,
            )
            rows = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary.duplicate_filenames, 1)
        self.assertEqual(rows[0]["filename"], "card.jpg")
        self.assertEqual(rows[1]["filename"], "card-2.jpg")

    def test_unsupported_extension_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "card.gif").write_bytes(b"image")
            metadata = root / "metadata.json"
            output = root / "generated_manifest.json"
            metadata.write_text(
                json.dumps([{"filename": "card.gif", "name": "Animated Card"}]),
                encoding="utf-8",
            )

            summary = import_dataset(
                image_folder=images,
                metadata_path=metadata,
                output_manifest=output,
            )
            rows = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary.unsupported_images, 1)
        self.assertEqual(rows, [])

    def test_load_metadata_rejects_unknown_file_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "metadata.txt"
            metadata.write_text("bad", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_metadata(metadata)


if __name__ == "__main__":
    unittest.main()
