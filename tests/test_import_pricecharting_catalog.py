import unittest
import base64
import json
from unittest.mock import patch

import httpx

from scripts.import_pricecharting_catalog import (
    PartialCatalogWriteError,
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

    def test_parse_price_cents_rejects_implausibly_large_values(self) -> None:
        # A malformed source field (e.g. a UPC/id landing in a price
        # column) must not be trusted as-is: it would overflow the
        # `integer` price_cents columns and fail the whole write batch.
        self.assertIsNone(parse_price_cents("4009902121"))
        self.assertIsNone(parse_price_cents("$4009902121.00"))

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
        self.assertEqual(len(row["content_hash"]), 64)

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
        unchanged_row = {
            "pricecharting_id": "1",
            "product_name": "Unchanged",
            "console_name": "Pokemon Cards",
            "loose_price_cents": 1000,
            "currency": "USD",
            "normalized_identity": "unchanged pokemon cards",
            "source_downloaded_at": "2026-07-25T00:00:00Z",
        }
        existing_hash = catalog_history_change_hash(
            unchanged_row
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
                    unchanged_row,
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

    def test_supabase_client_upserts_only_changed_catalog_rows(self) -> None:
        unchanged_row = to_catalog_row(
            {
                "id": "1",
                "product-name": "Unchanged",
                "console-name": "Pokemon Cards",
                "loose-price": "1000",
            },
            source_file="pokemon.csv",
            source_downloaded_at="2026-07-25T00:00:00Z",
        )
        changed_row = to_catalog_row(
            {
                "id": "2",
                "product-name": "Changed",
                "console-name": "Pokemon Cards",
                "loose-price": "2500",
            },
            source_file="pokemon.csv",
            source_downloaded_at="2026-07-25T00:00:00Z",
        )
        new_row = to_catalog_row(
            {
                "id": "3",
                "product-name": "New",
                "console-name": "Pokemon Cards",
                "loose-price": "500",
            },
            source_file="pokemon.csv",
            source_downloaded_at="2026-07-25T00:00:00Z",
        )
        assert unchanged_row is not None
        assert changed_row is not None
        assert new_row is not None
        transport = _FakeSupabaseTransport(
            current_rows=[
                {
                    "pricecharting_id": "1",
                    "content_hash": unchanged_row["content_hash"],
                },
                {"pricecharting_id": "2", "content_hash": "old-hash"},
            ]
        )
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )

            upserted = client.upsert_rows(
                [unchanged_row, changed_row, new_row],
                batch_size=100,
            )

        self.assertEqual(upserted, 2)
        self.assertEqual(
            [row["pricecharting_id"] for row in transport.upserted_rows],
            ["2", "3"],
        )

    def test_upsert_rows_continues_past_a_failing_subbatch(self) -> None:
        # Live-confirmed bug: a single sub-batch's Postgres statement timeout
        # used to abort the whole call, leaving every later sub-batch
        # unattempted even though it would have succeeded. batch_size=1 puts
        # each row in its own sub-batch so failing the 2nd POST call proves
        # the 3rd row still gets attempted afterward.
        rows = [
            _catalog_row("1", "First"),
            _catalog_row("2", "Second"),
            _catalog_row("3", "Third"),
        ]
        transport = _FakeSupabaseTransport(current_rows=[], fail_on_post_call_index=1)
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )

            with self.assertRaises(PartialCatalogWriteError) as context:
                client.upsert_rows(rows, batch_size=1)

        exc = context.exception
        self.assertEqual(exc.succeeded_count, 2)
        self.assertEqual(exc.failed_ids, ["2"])
        # Row 3's sub-batch ran after row 2's failed sub-batch -- proves the
        # loop didn't abort.
        self.assertEqual(
            [row["pricecharting_id"] for row in transport.upserted_rows],
            ["1", "3"],
        )

    def test_sync_scd2_history_rows_continues_past_a_failing_subbatch(self) -> None:
        rows = [
            _catalog_row("1", "First"),
            _catalog_row("2", "Second"),
            _catalog_row("3", "Third"),
        ]
        transport = _FakeSupabaseTransport(current_rows=[], fail_on_post_call_index=1)
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )

            with self.assertRaises(PartialCatalogWriteError) as context:
                client.sync_scd2_history_rows(rows, batch_size=1)

        exc = context.exception
        self.assertEqual(exc.succeeded_count, 2)
        self.assertEqual(exc.failed_ids, ["2"])
        self.assertEqual(
            [row["pricecharting_id"] for row in transport.inserted_rows],
            ["1", "3"],
        )

    def test_phase_seconds_starts_at_zero(self) -> None:
        client = SupabaseCatalogClient(
            supabase_url="https://example.supabase.co",
            service_role_key=_fake_supabase_jwt("service_role"),
            timeout_seconds=1,
        )

        self.assertEqual(
            client.phase_seconds,
            {
                "unchanged_detection": 0.0,
                "catalog_upsert": 0.0,
                "scd2_comparison": 0.0,
                "scd2_insert": 0.0,
            },
        )

    def test_upsert_rows_only_times_the_upsert_when_something_actually_changed(self) -> None:
        # All rows unchanged -- the hash-comparison lookup still runs (and
        # must be timed), but _upsert() is never called, so its timer must
        # stay untouched rather than reporting a phantom zero-row upsert.
        unchanged_row = to_catalog_row(
            {
                "id": "1",
                "product-name": "Unchanged",
                "console-name": "Pokemon Cards",
                "loose-price": "1000",
            },
            source_file="pokemon.csv",
            source_downloaded_at="2026-07-25T00:00:00Z",
        )
        assert unchanged_row is not None
        transport = _FakeSupabaseTransport(
            current_rows=[
                {"pricecharting_id": "1", "content_hash": unchanged_row["content_hash"]},
            ]
        )
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.upsert_rows([unchanged_row], batch_size=100)

        self.assertGreater(client.phase_seconds["unchanged_detection"], 0)
        self.assertEqual(client.phase_seconds["catalog_upsert"], 0.0)
        self.assertEqual(client.phase_seconds["scd2_comparison"], 0.0)
        self.assertEqual(client.phase_seconds["scd2_insert"], 0.0)

    def test_upsert_rows_times_both_phases_when_rows_change(self) -> None:
        rows = [_catalog_row("1", "First")]
        transport = _FakeSupabaseTransport(current_rows=[])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.upsert_rows(rows, batch_size=100)

        self.assertGreater(client.phase_seconds["unchanged_detection"], 0)
        self.assertGreater(client.phase_seconds["catalog_upsert"], 0)

    def test_sync_scd2_history_rows_times_comparison_and_insert_separately(self) -> None:
        rows = [_catalog_row("1", "First"), _catalog_row("2", "Second")]
        transport = _FakeSupabaseTransport(current_rows=[])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.sync_scd2_history_rows(rows, batch_size=100)

        self.assertGreater(client.phase_seconds["scd2_comparison"], 0)
        self.assertGreater(client.phase_seconds["scd2_insert"], 0)
        self.assertEqual(client.phase_seconds["unchanged_detection"], 0.0)
        self.assertEqual(client.phase_seconds["catalog_upsert"], 0.0)

    def test_phase_seconds_accumulate_across_multiple_calls(self) -> None:
        rows = [_catalog_row("1", "First")]
        transport = _FakeSupabaseTransport(current_rows=[])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.upsert_rows(rows, batch_size=100)
            after_first_call = client.phase_seconds["catalog_upsert"]
            client.upsert_rows(rows, batch_size=100)

        self.assertGreater(client.phase_seconds["catalog_upsert"], after_first_call)


def _catalog_row(pricecharting_id: str, name: str) -> dict:
    row = to_catalog_row(
        {
            "id": pricecharting_id,
            "product-name": name,
            "console-name": "Pokemon Cards",
            "loose-price": "1000",
        },
        source_file="pokemon.csv",
        source_downloaded_at="2026-07-25T00:00:00Z",
    )
    assert row is not None
    return row


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
    def __init__(
        self,
        *,
        current_rows: list[dict[str, str]],
        fail_on_post_call_index: int | None = None,
    ) -> None:
        self.current_rows = current_rows
        self.closed_ids: list[str] = []
        self.inserted_rows: list[dict[str, object]] = []
        self.upserted_rows: list[dict[str, object]] = []
        self._fail_on_post_call_index = fail_on_post_call_index
        self._post_call_count = 0

    def get(self, url: str, **kwargs):
        return _FakeSupabaseResponse(self.current_rows)

    def patch(self, url: str, **kwargs):
        pricecharting_filter = kwargs["params"]["pricecharting_id"]
        ids = pricecharting_filter.removeprefix("in.(").removesuffix(")").split(",")
        self.closed_ids.extend([item_id for item_id in ids if item_id])
        return _FakeSupabaseResponse()

    def post(self, url: str, **kwargs):
        call_index = self._post_call_count
        self._post_call_count += 1
        if call_index == self._fail_on_post_call_index:
            return _FailingSupabaseResponse()
        rows = kwargs.get("json", [])
        if url.endswith("/pricecharting_catalog_history"):
            self.inserted_rows.extend(rows)
        else:
            self.upserted_rows.extend(rows)
        return _FakeSupabaseResponse()


class _FailingSupabaseResponse:
    def __init__(self) -> None:
        self.status_code = 500
        self.text = '{"code":"57014","message":"canceling statement due to statement timeout"}'

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError("timeout", request=None, response=self)


def _fake_supabase_jwt(role: str) -> str:
    header = _b64_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64_json({"role": role})
    return f"{header}.{payload}.signature"


def _b64_json(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return encoded.rstrip("=")
