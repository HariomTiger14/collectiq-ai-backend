import unittest
import base64
import json
from unittest.mock import patch

import httpx

from scripts.import_pricecharting_catalog import (
    PartialCatalogWriteError,
    SupabaseCatalogClient,
    catalog_history_change_hash,
    compute_platform_group,
    dedupe_catalog_rows,
    download_env_sources,
    load_rows_from_text,
    normalized_identity,
    parse_price_cents,
    source_timestamp,
    to_catalog_history_row,
    to_catalog_row,
    to_catalog_row_from_api_product,
    to_price_observation_row,
    prices_differ,
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
        self.assertEqual(row["platform_group"], "nintendo")
        self.assertEqual(len(row["content_hash"]), 64)

    def test_compute_platform_group_matches_real_observed_naming(self) -> None:
        # Values sampled live from pricecharting_catalog -- region-prefixed
        # variants ("JP Playstation 4", "PAL Playstation 5") must match the
        # same group as the bare name.
        self.assertEqual(compute_platform_group("Playstation 4"), "playstation")
        self.assertEqual(compute_platform_group("JP Playstation 4"), "playstation")
        self.assertEqual(compute_platform_group("PAL Playstation 5"), "playstation")
        self.assertEqual(compute_platform_group("PSP"), "playstation")
        self.assertEqual(compute_platform_group("JP Xbox 360"), "xbox")
        self.assertEqual(compute_platform_group("Nintendo 64"), "nintendo")
        self.assertEqual(compute_platform_group("JP Nintendo Switch"), "nintendo")
        self.assertEqual(compute_platform_group("GameBoy Advance"), "nintendo")
        self.assertEqual(compute_platform_group("Sega Dreamcast"), "sega")
        self.assertEqual(compute_platform_group("Atari 400"), "atari")
        self.assertEqual(compute_platform_group("Atari ST"), "atari")
        self.assertEqual(compute_platform_group("Commodore 64"), "pc")
        self.assertEqual(compute_platform_group("Apple II"), "pc")

    def test_compute_platform_group_does_not_match_non_video_game_sets(self) -> None:
        # console_name is reused across every category -- a sports/comic/
        # funko set name must never be misclassified as a platform.
        self.assertIsNone(compute_platform_group("Baseball Cards 2019 Panini Donruss Optic"))
        self.assertIsNone(compute_platform_group("Comic Books Superman"))
        self.assertIsNone(compute_platform_group("Funko POP NFL"))
        self.assertIsNone(compute_platform_group(None))
        self.assertIsNone(compute_platform_group(""))

    def test_compute_platform_group_word_boundary_avoids_prior_collision_bug(self) -> None:
        # Regression: a bare substring "nes" match against console_name
        # once matched inside "Finest" (a card-set name), pulling sports
        # cards into a video-games filter. Word-boundary matching must not
        # repeat that.
        self.assertIsNone(compute_platform_group("Finest"))
        self.assertIsNone(compute_platform_group("Baseball Cards 2000 Finest Refractors"))
        self.assertEqual(compute_platform_group("NES"), "nintendo")

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
        fail_on_price_history_post: bool = False,
    ) -> None:
        self.current_rows = current_rows
        self.closed_ids: list[str] = []
        self.inserted_rows: list[dict[str, object]] = []
        self.upserted_rows: list[dict[str, object]] = []
        self.price_history_rows: list[dict[str, object]] = []
        self._fail_on_post_call_index = fail_on_post_call_index
        self._fail_on_price_history_post = fail_on_price_history_post
        self._post_call_count = 0

    def get(self, url: str, **kwargs):
        return _FakeSupabaseResponse(self.current_rows)

    def patch(self, url: str, **kwargs):
        pricecharting_filter = kwargs["params"]["pricecharting_id"]
        ids = pricecharting_filter.removeprefix("in.(").removesuffix(")").split(",")
        self.closed_ids.extend([item_id for item_id in ids if item_id])
        return _FakeSupabaseResponse()

    def post(self, url: str, **kwargs):
        rows = kwargs.get("json", [])
        if url.endswith("/pricecharting_price_history"):
            if self._fail_on_price_history_post:
                return _FailingSupabaseResponse()
            self.price_history_rows.extend(rows)
            # return=representation: echo the rows as "all inserted".
            return _FakeSupabaseResponse(rows)
        call_index = self._post_call_count
        self._post_call_count += 1
        if call_index == self._fail_on_post_call_index:
            return _FailingSupabaseResponse()
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


class ApiCategoryCanonicalizationTest(unittest.TestCase):
    """Step-1 regression suite for the cross-source category flap
    (2026-08-29 SCD2 audit): API paths attached a short `genre` while CSV
    paths fall back to console_name (the long, canonical form), so a card
    alternating between paths minted a fake SCD2 version per crossing."""

    CSV_ROW = {
        "id": "6870091",
        "product-name": "Luis Robert [Refractor]",
        "console-name": "Baseball Cards 2020 Topps Chrome Ben Baller",
        "loose-price": "12.34",
    }
    API_PRODUCT = {
        "id": "6870091",
        "product-name": "Luis Robert [Refractor]",
        "console-name": "Baseball Cards 2020 Topps Chrome Ben Baller",
        "genre": "Baseball Card",
        "loose-price": 1234,
    }

    def test_api_and_csv_paths_produce_the_same_category(self) -> None:
        csv_row = to_catalog_row(dict(self.CSV_ROW), "sportscardspro-set-backfill", "2026-08-29T00:00:00Z")
        api_row = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "2026-08-29T00:00:00Z")
        self.assertEqual(csv_row["category"], api_row["category"])

    def test_canonical_form_is_the_long_console_name_fallback(self) -> None:
        api_row = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "2026-08-29T00:00:00Z")
        self.assertEqual(api_row["category"], "Baseball Cards 2020 Topps Chrome Ben Baller")
        self.assertNotEqual(api_row["category"], "Baseball Card")

    def test_console_name_is_unchanged_and_keeps_set_detail(self) -> None:
        api_row = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "2026-08-29T00:00:00Z")
        self.assertEqual(api_row["console_name"], "Baseball Cards 2020 Topps Chrome Ben Baller")

    def test_canonical_category_is_inside_the_scd2_hash(self) -> None:
        # The hash both paths compute must be identical for the same logical
        # item -- proving canonicalization happens BEFORE hashing, not after.
        csv_row = to_catalog_row(dict(self.CSV_ROW), "sportscardspro-set-backfill", "2026-08-29T00:00:00Z")
        api_row = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "2026-08-29T00:00:00Z")
        self.assertEqual(csv_row["content_hash"], api_row["content_hash"])

    def test_alternating_paths_stay_hash_stable(self) -> None:
        # CSV -> API -> CSV -> API: after the first canonical version, no
        # crossing may change the hash again.
        hashes = [
            to_catalog_row(dict(self.CSV_ROW), "sportscardspro-set-backfill", "t1")["content_hash"],
            to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "t2")["content_hash"],
            to_catalog_row(dict(self.CSV_ROW), "sportscardspro-tier3-refresh", "t3")["content_hash"],
            to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tracked-refresh", "t4")["content_hash"],
        ]
        self.assertEqual(len(set(hashes)), 1)

    def test_a_genuine_category_change_still_versions(self) -> None:
        moved = dict(self.API_PRODUCT)
        moved["console-name"] = "Baseball Cards 2021 Topps Chrome"
        before = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "x", "t")["content_hash"]
        after = to_catalog_row_from_api_product(moved, "x", "t")["content_hash"]
        self.assertNotEqual(before, after)

    def test_unchanged_record_is_hash_identical_across_runs(self) -> None:
        first = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "run1")
        second = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "run2")
        self.assertEqual(first["content_hash"], second["content_hash"])

    def test_raw_payload_keeps_the_original_api_product_untouched(self) -> None:
        api_row = to_catalog_row_from_api_product(dict(self.API_PRODUCT), "sportscardspro-tier1-refresh", "t")
        self.assertEqual(api_row["raw_payload"].get("genre"), "Baseball Card")

    def test_product_without_console_name_keeps_its_genre(self) -> None:
        # Safety valve: nothing to fall back to -> better a short category
        # than none at all.
        bare = {"id": "1", "product-name": "Mystery", "genre": "Baseball Card", "loose-price": 100}
        row = to_catalog_row_from_api_product(bare, "sportscardspro-tier1-refresh", "t")
        self.assertEqual(row["category"], "Baseball Card")

    def test_browse_filters_match_the_canonical_long_form(self) -> None:
        # Category filtering everywhere is substring/ilike on keywords
        # (PRICECHARTING_CATEGORY_GROUPS / browse_category ladder); the
        # canonical long form must keep matching them.
        from app.services.pricing.catalog_search_service import PRICECHARTING_CATEGORY_GROUPS
        long_form = "Baseball Cards 2020 Topps Chrome Ben Baller"
        keywords = PRICECHARTING_CATEGORY_GROUPS["sports-cards"]
        self.assertTrue(any(kw.lower() in long_form.lower() for kw in keywords))


class PriceHistoryDualWriteTest(unittest.TestCase):
    """Step-2 shadow-write behavior matrix: which events produce a legacy
    SCD2 version, a compact price observation, both, or neither."""

    def _run_sync(self, rows, current_rows, *, fail_price_history=False):
        transport = _FakeSupabaseTransport(
            current_rows=current_rows,
            fail_on_price_history_post=fail_price_history,
        )
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            error = None
            try:
                client.sync_scd2_history_rows(rows, batch_size=100)
            except PartialCatalogWriteError as exc:
                error = exc
        return transport, client, error

    @staticmethod
    def _current_from(row, **overrides):
        current = {
            "pricecharting_id": row["pricecharting_id"],
            "change_hash": catalog_history_change_hash(row),
            "currency": row.get("currency") or "USD",
            **{col: row.get(col) for col in (
                "loose_price_cents", "cib_price_cents", "new_price_cents",
                "graded_price_cents", "box_only_price_cents", "manual_only_price_cents",
            )},
        }
        current.update(overrides)
        if overrides:
            recomputed = dict(row)
            recomputed.update({k: v for k, v in overrides.items() if k != "change_hash"})
            current["change_hash"] = catalog_history_change_hash(recomputed)
        return current

    def test_A_price_only_change_writes_both(self) -> None:
        row = _catalog_row("1", "Pikachu")
        current = self._current_from(row, loose_price_cents=555)
        transport, client, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)        # legacy +1
        self.assertEqual(len(transport.price_history_rows), 1)   # price_history +1
        self.assertEqual(transport.price_history_rows[0]["loose_price_cents"], 1000)
        self.assertEqual(client.price_history_stats["inserted"], 1)

    def test_B_metadata_only_change_writes_legacy_only(self) -> None:
        row = _catalog_row("1", "Pikachu")
        # Same prices; stored current has a different (old) product name.
        old_named = dict(row); old_named["product_name"] = "Old Name"
        current = {
            "pricecharting_id": "1",
            "change_hash": catalog_history_change_hash(old_named),
            "currency": "USD",
            "loose_price_cents": row["loose_price_cents"],
            "cib_price_cents": row["cib_price_cents"],
            "new_price_cents": row["new_price_cents"],
            "graded_price_cents": row["graded_price_cents"],
            "box_only_price_cents": row["box_only_price_cents"],
            "manual_only_price_cents": row["manual_only_price_cents"],
        }
        transport, client, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)        # legacy +1
        self.assertEqual(len(transport.price_history_rows), 0)   # price_history +0
        self.assertEqual(client.price_history_stats["attempted"], 0)

    def test_C_price_and_metadata_change_writes_both(self) -> None:
        row = _catalog_row("1", "Pikachu")
        old = dict(row); old["product_name"] = "Old Name"
        current = self._current_from(old, loose_price_cents=555)
        transport, _, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)
        self.assertEqual(len(transport.price_history_rows), 1)

    def test_D_unchanged_input_writes_nothing(self) -> None:
        row = _catalog_row("1", "Pikachu")
        current = self._current_from(row)
        transport, client, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 0)
        self.assertEqual(len(transport.price_history_rows), 0)
        self.assertEqual(len(transport.closed_ids), 0)

    def test_E_category_canonicalization_only_writes_legacy_only(self) -> None:
        # The exact Step-1 wave shape: stored current holds the deprecated
        # short category, incoming row the canonical long form; prices equal.
        row = _catalog_row("1", "Pikachu")
        short_cat = dict(row); short_cat["category"] = "Pokemon Card"
        current = {
            "pricecharting_id": "1",
            "change_hash": catalog_history_change_hash(short_cat),
            "currency": "USD",
            **{c: row.get(c) for c in (
                "loose_price_cents", "cib_price_cents", "new_price_cents",
                "graded_price_cents", "box_only_price_cents", "manual_only_price_cents")},
        }
        transport, _, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)        # legacy +1 (the one-time wave)
        self.assertEqual(len(transport.price_history_rows), 0)   # price_history +0

    def test_F_retry_is_idempotent_at_the_database(self) -> None:
        # The DB enforces (pricecharting_id, observed_at) uniqueness with
        # ignore-duplicates; the writer must send the SAME observed_at for
        # the same input, making the retried insert a conflict-skip.
        row = _catalog_row("1", "Pikachu")
        first = to_price_observation_row(row)
        second = to_price_observation_row(dict(row))
        self.assertEqual(
            (first["pricecharting_id"], first["observed_at"]),
            (second["pricecharting_id"], second["observed_at"]),
        )
        # And the writer's Prefer header requests DB-level dedup:
        import inspect
        src = inspect.getsource(SupabaseCatalogClient._insert_price_observation_rows)
        self.assertIn("ignore-duplicates", src)
        self.assertIn("on_conflict", src)

    def test_G_sequential_price_changes_produce_ordered_observations(self) -> None:
        base = {
            "id": "1", "product-name": "Pikachu", "console-name": "Pokemon Cards",
        }
        row1 = to_catalog_row({**base, "loose-price": "1000"}, "pokemon.csv", "2026-08-01T00:00:00Z")
        row2 = to_catalog_row({**base, "loose-price": "1100"}, "pokemon.csv", "2026-08-02T00:00:00Z")
        obs1, obs2 = to_price_observation_row(row1), to_price_observation_row(row2)
        self.assertLess(obs1["observed_at"], obs2["observed_at"])
        self.assertNotEqual(obs1["loose_price_cents"], obs2["loose_price_cents"])
        self.assertNotEqual(obs1["observed_at"], obs2["observed_at"])  # distinct idempotency keys

    def test_H_null_transitions_count_as_price_changes(self) -> None:
        row = _catalog_row("1", "Pikachu")            # loose = 1000, others None
        current = self._current_from(row, loose_price_cents=None)  # was unpriced
        self.assertTrue(prices_differ(row, current))
        gone = dict(row); gone["loose_price_cents"] = None
        current2 = self._current_from(row)            # was 1000
        self.assertTrue(prices_differ(gone, current2))  # priced -> unpriced also observes

    def test_I_currency_and_source_are_preserved_on_observations(self) -> None:
        row = _catalog_row("1", "Pikachu")
        obs = to_price_observation_row(row)
        self.assertEqual(obs["currency"], "USD")
        self.assertEqual(obs["source_file"], "pokemon.csv")
        changed_currency = dict(row); changed_currency["currency"] = "AUD"
        self.assertTrue(prices_differ(changed_currency, self._current_from(row)))

    def test_J_price_history_failure_fails_the_batch_before_legacy_writes(self) -> None:
        # Ordering is the transaction strategy: observation insert runs
        # first, so its failure must leave legacy history COMPLETELY
        # untouched (clean retry redoes both sides).
        row = _catalog_row("1", "Pikachu")
        current = self._current_from(row, loose_price_cents=555)
        transport, client, error = self._run_sync([row], [current], fail_price_history=True)
        self.assertIsNotNone(error)                      # surfaced, not swallowed
        self.assertEqual(error.failed_ids, ["1"])
        self.assertEqual(len(transport.inserted_rows), 0)  # no legacy insert
        self.assertEqual(len(transport.closed_ids), 0)     # no legacy close
        self.assertEqual(client.price_history_stats["failed"], 1)
