import unittest
import base64
import json
from unittest.mock import patch

from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    catalog_history_change_hash,
    dedupe_catalog_rows,
    download_env_sources,
    load_rows_from_text,
    normalized_identity,
    parse_price_cents,
    source_timestamp,
    to_catalog_history_row,
    to_catalog_row,
)


class ImportPriceChartingCatalogTest(unittest.TestCase):
    def test_parse_price_cents_keeps_pricecharting_pennies(self) -> None:
        self.assertEqual(parse_price_cents("3325"), 3325)
        self.assertEqual(parse_price_cents("") , None)

    def test_parse_price_cents_converts_currency_strings(self) -> None:
        self.assertEqual(parse_price_cents("$33.25"), 3325)
        self.assertEqual(parse_price_cents("33.25"), 3325)
        self.assertEqual(parse_price_cents("1,234"), 1234)

    def test_to_catalog_row_maps_pricecharting_csv_fields(self) -> None:
        row = to_catalog_row(
            {
                "id": "12345",
                "product-name": "Mario Kart 8 Deluxe",
                "console-name": "Nintendo Switch",
                "loose-price": "3150",
                "cib-price": "3500",
                "new-price": "4009",
                "upc": "045496590475",
                "release-date": "2017-04-28",
            },
            source_file="price-guide.csv",
            source_downloaded_at="2026-07-25T00:00:00Z",
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["pricecharting_id"], "12345")
        self.assertEqual(row["product_name"], "Mario Kart 8 Deluxe")
        self.assertEqual(row["console_name"], "Nintendo Switch")
        self.assertEqual(row["category"], "Nintendo Switch")
        self.assertEqual(row["loose_price_cents"], 3150)
        self.assertEqual(row["cib_price_cents"], 3500)
        self.assertEqual(row["new_price_cents"], 4009)
        self.assertIsNone(row["graded_price_cents"])
        self.assertIsNone(row["asin"])
        self.assertEqual(row["release_date"], "2017-04-28")
        self.assertEqual(row["normalized_identity"], "mario kart 8 deluxe nintendo switch")

    def test_to_catalog_history_row_creates_current_scd2_version(self) -> None:
        catalog_row = to_catalog_row(
            {
                "id": "12345",
                "product-name": "Mario Kart 8 Deluxe",
                "console-name": "Nintendo Switch",
                "loose-price": "3150",
            },
            source_file="video_games.csv",
            source_downloaded_at="2026-07-25T10:15:00Z",
        )
        assert catalog_row is not None

        history_row = to_catalog_history_row(catalog_row)

        self.assertEqual(history_row["pricecharting_id"], "12345")
        self.assertEqual(history_row["valid_from"], "2026-07-25T10:15:00+00:00")
        self.assertIsNone(history_row["valid_to"])
        self.assertTrue(history_row["is_current"])
        self.assertEqual(len(history_row["change_hash"]), 64)

    def test_catalog_history_hash_changes_only_when_catalog_values_change(self) -> None:
        base_row = {
            "pricecharting_id": "12345",
            "product_name": "Mario Kart 8 Deluxe",
            "console_name": "Nintendo Switch",
            "loose_price_cents": 3150,
            "source_downloaded_at": "2026-07-25T00:00:00Z",
        }
        same_catalog_new_download = {
            **base_row,
            "source_downloaded_at": "2026-07-26T00:00:00Z",
        }
        changed_price = {**base_row, "loose_price_cents": 3299}

        self.assertEqual(
            catalog_history_change_hash(base_row),
            catalog_history_change_hash(same_catalog_new_download),
        )
        self.assertNotEqual(
            catalog_history_change_hash(base_row),
            catalog_history_change_hash(changed_price),
        )

    def test_source_timestamp_normalizes_to_utc_iso(self) -> None:
        self.assertEqual(
            source_timestamp("2026-07-25T10:15:00Z"),
            "2026-07-25T10:15:00+00:00",
        )

    def test_load_rows_from_text_parses_csv_download(self) -> None:
        rows = load_rows_from_text(
            "id,product-name,console-name,loose-price\n"
            "12345,Mario Kart 8 Deluxe,Nintendo Switch,3150\n"
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product-name"], "Mario Kart 8 Deluxe")

    def test_dedupe_catalog_rows_keeps_latest_row_for_same_product(self) -> None:
        rows = dedupe_catalog_rows(
            [
                {"pricecharting_id": "12345", "product_name": "Old"},
                {"pricecharting_id": "12345", "product_name": "New"},
                {"pricecharting_id": "67890", "product_name": "Other"},
            ]
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["product_name"], "New")
        self.assertEqual(rows[1]["product_name"], "Other")

    def test_download_env_sources_uses_configured_category_urls(self) -> None:
        transport = _FakeTransport(
            {
                "https://pricecharting.test/video-games.csv": (
                    "id,product-name,console-name,loose-price\n"
                    "12345,Mario Kart 8 Deluxe,Nintendo Switch,3150\n"
                ),
                "https://pricecharting.test/pokemon.csv": (
                    "id,product-name,console-name,loose-price\n"
                    "999,Charizard,Pokemon Cards,120000\n"
                ),
            }
        )

        with patch.dict(
            "os.environ",
            {
                "PRICECHARTING_CSV_VIDEO_GAMES_URL": "https://pricecharting.test/video-games.csv",
                "PRICECHARTING_CSV_POKEMON_URL": "https://pricecharting.test/pokemon.csv",
                "PRICECHARTING_CSV_MAGIC_URL": "",
                "PRICECHARTING_CSV_YUGIOH_URL": "",
                "PRICECHARTING_CSV_ONE_PIECE_URL": "",
            },
            clear=False,
        ), patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport

            sources = download_env_sources(timeout_seconds=1)

        self.assertEqual([source.name for source in sources], ["video_games.csv", "pokemon.csv"])
        self.assertEqual(sources[0].rows[0]["product-name"], "Mario Kart 8 Deluxe")
        self.assertEqual(sources[1].rows[0]["product-name"], "Charizard")

    def test_download_env_sources_can_filter_to_one_source(self) -> None:
        transport = _FakeTransport(
            {
                "https://pricecharting.test/video-games.csv": (
                    "id,product-name,console-name,loose-price\n"
                    "12345,Mario Kart 8 Deluxe,Nintendo Switch,3150\n"
                ),
            }
        )

        with patch.dict(
            "os.environ",
            {
                "PRICECHARTING_CSV_VIDEO_GAMES_URL": "https://pricecharting.test/video-games.csv",
                "PRICECHARTING_CSV_POKEMON_URL": "https://pricecharting.test/pokemon.csv",
            },
            clear=False,
        ), patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport

            sources = download_env_sources(timeout_seconds=1, source_filter="video_games")

        self.assertEqual([source.name for source in sources], ["video_games.csv"])
        self.assertEqual(sources[0].rows[0]["product-name"], "Mario Kart 8 Deluxe")

    def test_download_env_sources_requires_at_least_one_url(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PRICECHARTING_CSV_VIDEO_GAMES_URL": "",
                "PRICECHARTING_CSV_POKEMON_URL": "",
                "PRICECHARTING_CSV_MAGIC_URL": "",
                "PRICECHARTING_CSV_YUGIOH_URL": "",
                "PRICECHARTING_CSV_ONE_PIECE_URL": "",
            },
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                download_env_sources(timeout_seconds=1)

    def test_to_catalog_row_skips_rows_without_identity(self) -> None:
        self.assertIsNone(
            to_catalog_row(
                {"loose-price": "1234"},
                source_file="price-guide.csv",
                source_downloaded_at="2026-07-25T00:00:00Z",
            )
        )

    def test_normalized_identity_compacts_spacing(self) -> None:
        self.assertEqual(
            normalized_identity("  Mario   Kart 8 Deluxe ", " Nintendo Switch "),
            "mario kart 8 deluxe nintendo switch",
        )

    def test_supabase_client_rejects_anon_key_for_imports(self) -> None:
        with self.assertRaises(SystemExit) as context:
            SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("anon"),
                timeout_seconds=1,
            )

        self.assertIn("service_role", str(context.exception))
        self.assertIn("anon", str(context.exception))

    def test_supabase_client_syncs_scd2_history_only_for_changes(self) -> None:
        existing_hash = catalog_history_change_hash(
            {
                "product_name": "Unchanged",
                "console_name": "Pokemon Cards",
                "loose_price_cents": 1000,
            }
        )
        transport = _FakeSupabaseTransport(
            current_rows=[
                {"pricecharting_id": "1", "change_hash": existing_hash},
                {"pricecharting_id": "2", "change_hash": "old-hash"},
            ]
        )
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )

            inserted = client.sync_scd2_history_rows(
                [
                    {
                        "pricecharting_id": "1",
                        "product_name": "Unchanged",
                        "console_name": "Pokemon Cards",
                        "loose_price_cents": 1000,
                        "currency": "USD",
                        "normalized_identity": "unchanged pokemon cards",
                        "source_downloaded_at": "2026-07-25T00:00:00Z",
                    },
                    {
                        "pricecharting_id": "2",
                        "product_name": "Changed",
                        "console_name": "Pokemon Cards",
                        "loose_price_cents": 2500,
                        "currency": "USD",
                        "normalized_identity": "changed pokemon cards",
                        "source_downloaded_at": "2026-07-25T00:00:00Z",
                    },
                    {
                        "pricecharting_id": "3",
                        "product_name": "New",
                        "console_name": "Pokemon Cards",
                        "loose_price_cents": 500,
                        "currency": "USD",
                        "normalized_identity": "new pokemon cards",
                        "source_downloaded_at": "2026-07-25T00:00:00Z",
                    },
                ],
                batch_size=100,
            )

        self.assertEqual(inserted, 2)
        self.assertEqual(transport.closed_ids, ["2"])
        self.assertEqual([row["pricecharting_id"] for row in transport.inserted_rows], ["2", "3"])


if __name__ == "__main__":
    unittest.main()


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeTransport:
    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    def get(self, url: str, **kwargs):
        return _FakeResponse(self._responses[url])


class _FakeSupabaseResponse:
    def __init__(self, payload=None) -> None:
        self._payload = [] if payload is None else payload
        self.status_code = 200
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeSupabaseTransport:
    def __init__(self, *, current_rows: list[dict[str, str]]) -> None:
        self.current_rows = current_rows
        self.closed_ids: list[str] = []
        self.inserted_rows: list[dict[str, object]] = []

    def get(self, url: str, **kwargs):
        return _FakeSupabaseResponse(self.current_rows)

    def patch(self, url: str, **kwargs):
        pricecharting_filter = kwargs["params"]["pricecharting_id"]
        ids = pricecharting_filter.removeprefix("in.(").removesuffix(")").split(",")
        self.closed_ids.extend([item_id for item_id in ids if item_id])
        return _FakeSupabaseResponse()

    def post(self, url: str, **kwargs):
        self.inserted_rows.extend(kwargs.get("json", []))
        return _FakeSupabaseResponse()


def _fake_supabase_jwt(role: str) -> str:
    header = _b64_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64_json({"role": role})
    return f"{header}.{payload}.signature"


def _b64_json(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return encoded.rstrip("=")
