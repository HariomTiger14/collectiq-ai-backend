import argparse
import csv
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_OUTPUT_MANIFEST = Path("validation/manifests/generated_manifest.json")


@dataclass(frozen=True)
class ImportedManifestRow:
    filename: str
    expected_name: str = ""
    expected_category: str = ""
    expected_brand: str = ""
    expected_set: str = ""
    expected_year: str = ""
    source: str = ""
    license: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "expected_name": self.expected_name,
            "expected_category": self.expected_category,
            "expected_brand": self.expected_brand,
            "expected_set": self.expected_set,
            "expected_year": self.expected_year,
            "source": self.source,
            "license": self.license,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ImportSummary:
    rows: list[ImportedManifestRow]
    missing_images: int
    duplicate_filenames: int
    unsupported_images: int


def load_metadata(path: Path | None) -> list[dict]:
    if path is None:
        return []
    if not path.exists():
        raise FileNotFoundError(f"Metadata file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix == ".json":
        parsed = json.loads(path.read_text(encoding="utf-8"))
        rows = parsed.get("items", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            raise ValueError("JSON metadata must be a list or an object with an items list.")
        return [row for row in rows if isinstance(row, dict)]
    raise ValueError("Metadata must be CSV or JSON.")


def import_dataset(
    *,
    image_folder: Path | None,
    metadata_path: Path | None,
    output_manifest: Path,
    source_name: str = "",
    license_note: str = "",
    copy_images_to: Path | None = None,
) -> ImportSummary:
    image_lookup = _build_image_lookup(image_folder)
    metadata_rows = load_metadata(metadata_path)
    if not metadata_rows and image_folder:
        metadata_rows = [{"filename": image_path.name} for image_path in sorted(image_lookup.values())]

    used_filenames: set[str] = set()
    rows: list[ImportedManifestRow] = []
    missing_images = 0
    duplicate_filenames = 0
    unsupported_images = 0

    if copy_images_to:
        copy_images_to.mkdir(parents=True, exist_ok=True)

    for metadata in metadata_rows:
        normalized = {_canonical_key(key): value for key, value in metadata.items()}
        raw_filename = _pick_first(
            normalized,
            [
                "filename",
                "image",
                "image_filename",
                "file_name",
                "filepath",
                "file_path",
                "path",
            ],
        )
        image_path = _resolve_image_path(raw_filename, image_lookup, image_folder)
        notes = [_text(_pick_first(normalized, ["notes", "note", "description"]))]

        if image_path is None:
            missing_images += 1
            output_filename = Path(raw_filename).name if raw_filename else "missing-image"
            notes.append("Image file missing from local dataset folder.")
        elif image_path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            unsupported_images += 1
            continue
        else:
            output_filename = image_path.name

        final_filename = _dedupe_filename(output_filename, used_filenames)
        if final_filename != output_filename:
            duplicate_filenames += 1
            notes.append(f"Duplicate filename renamed from {output_filename}.")

        if copy_images_to and image_path and image_path.is_file():
            shutil.copy2(image_path, copy_images_to / final_filename)

        rows.append(
            ImportedManifestRow(
                filename=final_filename,
                expected_name=_expected_name(normalized),
                expected_category=_expected_category(normalized),
                expected_brand=_expected_brand(normalized),
                expected_set=_expected_set(normalized),
                expected_year=_expected_year(normalized),
                source=source_name or _text(_pick_first(normalized, ["source", "dataset"])),
                license=license_note or _text(_pick_first(normalized, ["license", "licence"])),
                notes=" ".join(note for note in notes if note).strip(),
            )
        )

    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text(
        json.dumps([row.to_dict() for row in rows], indent=2) + "\n",
        encoding="utf-8",
    )
    return ImportSummary(
        rows=rows,
        missing_images=missing_images,
        duplicate_filenames=duplicate_filenames,
        unsupported_images=unsupported_images,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import local/open dataset metadata into a validation manifest.",
    )
    parser.add_argument("--image-folder", default="", help="Local dataset image folder.")
    parser.add_argument("--metadata", default="", help="CSV or JSON metadata file.")
    parser.add_argument(
        "--output-manifest",
        default=str(DEFAULT_OUTPUT_MANIFEST),
        help=f"Output manifest. Default: {DEFAULT_OUTPUT_MANIFEST}",
    )
    parser.add_argument("--source-name", default="", help="Source label to store in manifest.")
    parser.add_argument("--license", default="", help="License note to store in manifest.")
    parser.add_argument(
        "--copy-images-to",
        default="",
        help="Optional local image copy target, usually validation/images.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    summary = import_dataset(
        image_folder=_optional_path(args.image_folder),
        metadata_path=_optional_path(args.metadata),
        output_manifest=Path(args.output_manifest),
        source_name=args.source_name,
        license_note=args.license,
        copy_images_to=_optional_path(args.copy_images_to),
    )
    print(f"Generated manifest: {args.output_manifest}")
    print(f"Rows: {len(summary.rows)}")
    print(f"Missing images: {summary.missing_images}")
    print(f"Duplicate filenames: {summary.duplicate_filenames}")
    print(f"Unsupported images skipped: {summary.unsupported_images}")
    return 0


def _build_image_lookup(image_folder: Path | None) -> dict[str, Path]:
    if image_folder is None or not image_folder.exists():
        return {}
    return {
        image_path.name.lower(): image_path
        for image_path in image_folder.rglob("*")
        if image_path.is_file()
    }


def _resolve_image_path(
    raw_filename: str,
    image_lookup: dict[str, Path],
    image_folder: Path | None,
) -> Path | None:
    if not raw_filename:
        return None
    raw_path = Path(raw_filename)
    if raw_path.is_absolute() and raw_path.exists():
        return raw_path
    if image_folder:
        candidate = image_folder / raw_filename
        if candidate.exists():
            return candidate
    return image_lookup.get(raw_path.name.lower())


def _dedupe_filename(filename: str, used: set[str]) -> str:
    candidate = Path(filename).name or "image"
    stem = Path(candidate).stem or "image"
    suffix = Path(candidate).suffix
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _expected_name(row: dict) -> str:
    return _pick_first(
        row,
        [
            "expected_name",
            "name",
            "title",
            "item_name",
            "label",
            "class",
            "card_name",
            "coin_name",
            "comic_title",
        ],
    )


def _expected_category(row: dict) -> str:
    return _pick_first(
        row,
        ["expected_category", "category", "type", "collectible_type", "label_category"],
    )


def _expected_brand(row: dict) -> str:
    return _pick_first(
        row,
        ["expected_brand", "brand", "franchise", "manufacturer", "publisher", "mint"],
    )


def _expected_set(row: dict) -> str:
    return _pick_first(
        row,
        ["expected_set", "set", "set_name", "series", "collection", "issue"],
    )


def _expected_year(row: dict) -> str:
    value = _pick_first(row, ["expected_year", "year", "release_year", "date", "issued"])
    digits = "".join(char for char in value if char.isdigit())
    return digits[:4] if len(digits) >= 4 else value


def _pick_first(row: dict, keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return _text(value)
    return ""


def _canonical_key(value: str) -> str:
    output = []
    for char in value:
        if char.isupper() and output:
            output.append("_")
        output.append(char.lower())
    return "".join(output).replace("-", "_").replace(" ", "_").replace(".", "_")


def _optional_path(value: str) -> Path | None:
    return Path(value) if value else None


def _text(value) -> str:
    return "" if value is None else str(value).strip()


if __name__ == "__main__":
    raise SystemExit(main())
