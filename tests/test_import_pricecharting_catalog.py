import unittest
import base64
import json
from unittest.mock import patch

import httpx

from scripts.import_pricecharting_catalog import (
    MAX_TIMEOUT_ATTEMPTS,
    PartialCatalogWriteError,
    SupabaseCatalogClient,
    CATALOG_METADATA_SIGNATURE_COLUMNS,
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


def _no_sleep_limiter():
    """A limiter whose waits are instant.

    Under pytest the shared limiter cannot reach the database, so it degrades
    to LOCAL pacing and sleeps the real 600-second CSV interval between the
    two category downloads these tests perform. Correct in production;
    absurd in a test, and it was costing 600s of every suite run."""
    from scripts._shared_rate_limiter import (
        CLASS_ESSENTIAL_CATALOG,
        PRICECHARTING_CSV,
        SharedRateLimiter,
    )

    return SharedRateLimiter(
        PRICECHARTING_CSV,
        slot_class=CLASS_ESSENTIAL_CATALOG,
        fallback_interval_seconds=600.0,
        sleep=lambda _seconds: None,
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

            sources = download_env_sources(
                timeout_seconds=1, csv_limiter=_no_sleep_limiter()
            )

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

            sources = download_env_sources(
                timeout_seconds=1,
                source_filter="video_games",
                csv_limiter=_no_sleep_limiter(),
            )

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
                download_env_sources(
                    timeout_seconds=1, csv_limiter=_no_sleep_limiter()
                )

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

    def test_supabase_client_syncs_scd2_history_only_for_metadata_changes(self) -> None:
        """Item 1 unchanged, item 2 renamed, item 3 new. Only 2 and 3
        get a version -- and only 2 needs its predecessor closed."""
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
        # The stored versions carry METADATA now, not just a hash: that is
        # what decides whether a version is written. Item 1's metadata
        # matches the incoming row, item 2's does not.
        transport = _FakeSupabaseTransport(
            current_rows=[
                {
                    "pricecharting_id": "1",
                    "change_hash": existing_hash,
                    **{column: unchanged_row.get(column)
                       for column in CATALOG_METADATA_SIGNATURE_COLUMNS},
                },
                {
                    "pricecharting_id": "2",
                    "change_hash": "old-hash",
                    "product_name": "Was Called Something Else",
                    "console_name": "Pokemon Cards",
                    "normalized_identity": "was called something else pokemon cards",
                },
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
            # The catalog gate reads METADATA now, not content_hash: gating on
            # a price-inclusive hash rewrote the search document on every price
            # move. Item 1's metadata matches the incoming row, item 2's does not.
            current_rows=[
                {
                    "pricecharting_id": "1",
                    **{c: unchanged_row.get(c) for c in CATALOG_METADATA_SIGNATURE_COLUMNS},
                },
                {"pricecharting_id": "2", "product_name": "Was Called Something Else",
                 "console_name": "Pokemon Cards"},
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
        # Persistently failing: one statement timeout is now retried and
        # recovered, so proving the loop CONTINUES past a dead sub-batch
        # needs one that stays dead through every attempt.
        transport = _FakeSupabaseTransport(
            current_rows=[],
            fail_on_post_call_index=1,
            fail_repeat=MAX_TIMEOUT_ATTEMPTS,
        )
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

    def test_the_snapshot_and_version_writes_are_timed_apart(self) -> None:
        """They shared one timer called scd2_insert until 2026-09-09.

        That was fine while a price change wrote both. After #213 one of them
        happens ~35,000 times a batch and the other ~800, so a single number
        averages away the thing you are trying to see.
        """
        row = _catalog_row("1", "Pikachu")
        transport = _FakeSupabaseTransport(current_rows=[])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.sync_scd2_history_rows([row], batch_size=100)
        self.assertGreater(client.phase_seconds["price_snapshot_insert"], 0.0,
                           "the snapshot write was not timed")
        self.assertGreater(client.phase_seconds["scd2_insert"], 0.0,
                           "the version write was not timed")

    def test_a_price_only_change_charges_only_the_snapshot_timer(self) -> None:
        """The common case after #213: no version written, so no version time."""
        row = _catalog_row("1", "Pikachu")
        frozen = {
            "pricecharting_id": "1",
            "change_hash": "x",
            **{column: row.get(column)
               for column in CATALOG_METADATA_SIGNATURE_COLUMNS},
        }
        stale_catalog = dict(frozen); stale_catalog["loose_price_cents"] = 1
        transport = _FakeSupabaseTransport(current_rows=[frozen],
                                           catalog_rows=[stale_catalog])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.sync_scd2_history_rows([row], batch_size=100)
        self.assertGreater(client.phase_seconds["price_snapshot_insert"], 0.0)
        self.assertEqual(client.phase_seconds["scd2_insert"], 0.0)
        self.assertEqual(client.phase_seconds["scd2_close"], 0.0)

    def test_closing_the_superseded_version_is_timed_on_its_own(self) -> None:
        """A metadata change costs a close AND an insert -- two round trips.

        Timed apart because they are different writes: the close is an UPDATE
        against a partial unique index on a 24 GB table, the insert is an
        append. Folding either into a neighbouring timer hides one of them.
        """
        row = _catalog_row("1", "Pikachu")
        renamed = dict(row); renamed["product_name"] = "Was Called This"
        stored = {
            "pricecharting_id": "1",
            "change_hash": "x",
            **{column: renamed.get(column)
               for column in CATALOG_METADATA_SIGNATURE_COLUMNS},
        }
        transport = _FakeSupabaseTransport(current_rows=[stored],
                                           catalog_rows=[stored])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.sync_scd2_history_rows([row], batch_size=100)
        self.assertEqual(transport.closed_ids, ["1"], "nothing was closed")
        self.assertGreater(client.phase_seconds["scd2_close"], 0.0,
                           "the close was not timed")
        self.assertGreater(client.phase_seconds["scd2_insert"], 0.0)

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
                "current_price_upsert": 0.0,
                "price_snapshot_insert": 0.0,
                "scd2_close": 0.0,
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
                {"pricecharting_id": "1",
                 **{c: unchanged_row.get(c) for c in CATALOG_METADATA_SIGNATURE_COLUMNS}},
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
        catalog_rows: list[dict[str, str]] | None = None,
        current_price_rows: list[dict[str, str]] | None = None,
        fail_on_post_call_index: int | None = None,
        fail_on_price_history_post: bool = False,
        fail_repeat: int = 1,
    ) -> None:
        self.current_rows = current_rows
        # The two baselines are now DIFFERENT tables and are allowed to
        # disagree -- that divergence is the whole point of the change, so
        # the fake has to be able to express it. Defaults to the SCD2 rows
        # so existing callers keep the old "they always agree" behaviour.
        self.catalog_rows = current_rows if catalog_rows is None else catalog_rows
        # Three baselines now, one per table that gets written. They are
        # allowed to disagree -- that is the point: the SCD2 row gates the
        # version, the catalog row gates the search document, the current
        # price row gates the price. Defaults to the SCD2 rows so callers
        # written before PR 4 keep their "they all agree" behaviour.
        self.current_price_rows = (
            current_rows if current_price_rows is None else current_price_rows)
        self.current_price_upserts: list[dict[str, object]] = []
        self.closed_ids: list[str] = []
        self.get_urls: list[str] = []
        self.inserted_rows: list[dict[str, object]] = []
        self.upserted_rows: list[dict[str, object]] = []
        self.price_history_rows: list[dict[str, object]] = []
        self._fail_on_post_call_index = fail_on_post_call_index
        # How many CONSECUTIVE posts fail from that index. A statement
        # timeout is retried now, so a single failure is recovered --
        # exhausting the retries takes MAX_TIMEOUT_ATTEMPTS of them.
        self._fail_repeat = fail_repeat
        self._fail_on_price_history_post = fail_on_price_history_post
        self._post_call_count = 0

    def get(self, url: str, **kwargs):
        self.get_urls.append(url)
        if url.endswith("/pricecharting_current_price"):
            return _FakeSupabaseResponse(self.current_price_rows)
        if url.endswith("/pricecharting_catalog"):
            return _FakeSupabaseResponse(self.catalog_rows)
        return _FakeSupabaseResponse(self.current_rows)

    def patch(self, url: str, **kwargs):
        pricecharting_filter = kwargs["params"]["pricecharting_id"]
        ids = pricecharting_filter.removeprefix("in.(").removesuffix(")").split(",")
        self.closed_ids.extend([item_id for item_id in ids if item_id])
        return _FakeSupabaseResponse()

    def post(self, url: str, **kwargs):
        rows = kwargs.get("json", [])
        if url.endswith("/pricecharting_current_price"):
            self.current_price_upserts.extend(rows)
            return _FakeSupabaseResponse()
        if url.endswith("/pricecharting_price_history"):
            if self._fail_on_price_history_post:
                return _FailingSupabaseResponse()
            self.price_history_rows.extend(rows)
            # return=representation: echo the rows as "all inserted".
            return _FakeSupabaseResponse(rows)
        call_index = self._post_call_count
        self._post_call_count += 1
        if (
            self._fail_on_post_call_index is not None
            and self._fail_on_post_call_index
            <= call_index
            < self._fail_on_post_call_index + self._fail_repeat
        ):
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
    """Which events produce an SCD2 version, a price snapshot, both, or neither.

    Rewritten 2026-09-09. A price move no longer writes a 1,427-byte SCD2
    version -- it writes a 188-byte snapshot and nothing else. SCD2 became
    catalog/metadata history; pricecharting_price_history is the price
    timeline the chart reads (#212).

    The two baselines now come from DIFFERENT tables and are allowed to
    disagree: metadata is compared against the stored SCD2 version, prices
    against the current pricecharting_catalog row. That is not an
    implementation detail -- with prices frozen in SCD2, comparing against it
    would report "changed" every run forever and write one redundant snapshot
    per item per day. Several tests below exist only to pin that.
    """

    def _run_sync(self, rows, current_rows, *, catalog_rows=None,
                  current_price_rows=None, fail_price_history=False):
        transport = _FakeSupabaseTransport(
            current_rows=current_rows,
            catalog_rows=catalog_rows,
            current_price_rows=current_price_rows,
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
        """A stored SCD2 current version, shaped like the real lookup.

        It carries the metadata columns because those now decide whether a
        version is written; prices are carried too, but only so a test can
        deliberately freeze them and prove the gate no longer reads them.
        """
        current = {
            "pricecharting_id": row["pricecharting_id"],
            "change_hash": catalog_history_change_hash(row),
            "currency": row.get("currency") or "USD",
            **{col: row.get(col) for col in CATALOG_METADATA_SIGNATURE_COLUMNS},
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

    def test_A_price_only_change_writes_a_snapshot_and_no_version(self) -> None:
        """The change this PR exists for: 188 bytes instead of 1,427 + 1,427."""
        row = _catalog_row("1", "Pikachu")
        current = self._current_from(row, loose_price_cents=555)
        transport, client, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 0)        # NO SCD2 version
        self.assertEqual(len(transport.closed_ids), 0)           # nothing closed
        self.assertEqual(len(transport.price_history_rows), 1)   # snapshot only
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
        transport, client, error = self._run_sync([row], [current], catalog_rows=[current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)        # SCD2 version
        self.assertEqual(len(transport.price_history_rows), 0)   # no snapshot
        self.assertEqual(client.price_history_stats["attempted"], 0)

    def test_C_price_and_metadata_change_writes_both(self) -> None:
        row = _catalog_row("1", "Pikachu")
        old = dict(row); old["product_name"] = "Old Name"
        current = self._current_from(old, loose_price_cents=555)
        transport, _, error = self._run_sync([row], [current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)        # metadata moved
        self.assertEqual(len(transport.price_history_rows), 1)   # and so did price

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
        transport, _, error = self._run_sync([row], [current], catalog_rows=[current])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)        # the one-time wave
        self.assertEqual(len(transport.price_history_rows), 0)   # prices never moved

    def test_G_a_frozen_scd2_price_does_not_drive_the_snapshot_gate(self) -> None:
        """The defect this design avoids, stated as a test.

        Once price-only changes stop writing versions, the SCD2 row's prices
        stop advancing. If the snapshot gate still read them, an item whose
        price moved once would compare as "changed" against that stale value
        on every subsequent run and mint a redundant snapshot per day --
        roughly 12M rows a day into the table the backfill just filled.

        Here SCD2 is frozen at 555 while the catalog already holds today's
        1000. Nothing has actually changed since the last run, so nothing
        should be written.
        """
        row = _catalog_row("1", "Pikachu")
        frozen_scd2 = self._current_from(row, loose_price_cents=555)
        price_now = self._current_from(row)            # already at 1000
        transport, _, error = self._run_sync(
            [row], [frozen_scd2], catalog_rows=[frozen_scd2],
            current_price_rows=[price_now])
        self.assertIsNone(error)
        self.assertEqual(len(transport.price_history_rows), 0,
                         "snapshot written from a stale SCD2 price")
        self.assertEqual(len(transport.inserted_rows), 0)

    def test_H_a_stable_price_on_consecutive_runs_writes_one_snapshot(self) -> None:
        """Run 1 records the move; run 2 sees the catalog caught up and stops.

        This is the same property as G from the caller's side, and it is the
        one that keeps the table's growth proportional to real price changes
        rather than to items x days.
        """
        row = _catalog_row("1", "Pikachu")
        yesterday = self._current_from(row, loose_price_cents=555)

        first, _, _ = self._run_sync([row], [yesterday], catalog_rows=[yesterday],
                                     current_price_rows=[yesterday])
        self.assertEqual(len(first.price_history_rows), 1)

        # The current-price upsert has since advanced that row to the new price.
        today = self._current_from(row)
        second, _, _ = self._run_sync([row], [yesterday], catalog_rows=[yesterday],
                                      current_price_rows=[today])
        self.assertEqual(len(second.price_history_rows), 0,
                         "a second snapshot for a price that did not move")

    def test_I_a_price_that_returns_to_the_frozen_value_is_still_recorded(self) -> None:
        """Why the change_hash short-circuit had to go.

        SCD2 froze at 555. The price moved away and has now come back to 555,
        so the full-signature change_hash matches the stored version exactly.
        The old code returned early on that equality and would have recorded
        nothing -- but against yesterday's catalog value of 900 this is a real
        price move, and the chart needs the point.
        """
        row = _catalog_row("1", "Pikachu")
        row["loose_price_cents"] = 555
        frozen_scd2 = self._current_from(row)                       # hash matches
        price_yesterday = self._current_from(row, loose_price_cents=900)
        transport, _, error = self._run_sync(
            [row], [frozen_scd2], catalog_rows=[frozen_scd2],
            current_price_rows=[price_yesterday])
        self.assertIsNone(error)
        self.assertEqual(len(transport.price_history_rows), 1)
        self.assertEqual(transport.price_history_rows[0]["loose_price_cents"], 555)
        self.assertEqual(len(transport.inserted_rows), 0, "metadata did not move")

    def test_J_a_brand_new_item_writes_both(self) -> None:
        row = _catalog_row("1", "Pikachu")
        transport, _, error = self._run_sync([row], [], catalog_rows=[])
        self.assertIsNone(error)
        self.assertEqual(len(transport.inserted_rows), 1)
        self.assertEqual(len(transport.price_history_rows), 1)
        self.assertEqual(len(transport.closed_ids), 0, "nothing to close")

    def test_K_the_metadata_gate_ignores_every_price_column(self) -> None:
        """Field by field, so a column added to the wrong tuple is caught."""
        row = _catalog_row("1", "Pikachu")
        for column in ("loose_price_cents", "cib_price_cents", "new_price_cents",
                       "graded_price_cents", "box_only_price_cents",
                       "manual_only_price_cents", "currency"):
            with self.subTest(column=column):
                self.assertNotIn(column, CATALOG_METADATA_SIGNATURE_COLUMNS)

    def test_L_the_metadata_gate_covers_every_non_price_signature_column(self) -> None:
        from scripts.import_pricecharting_catalog import (
            CATALOG_HISTORY_SIGNATURE_COLUMNS,
            PRICE_OBSERVATION_COLUMNS,
        )

        expected = {column for column in CATALOG_HISTORY_SIGNATURE_COLUMNS
                    if column not in PRICE_OBSERVATION_COLUMNS and column != "currency"}
        self.assertEqual(set(CATALOG_METADATA_SIGNATURE_COLUMNS), expected)
        self.assertIn("product_name", expected)
        self.assertIn("category", expected)
        self.assertIn("normalized_identity", expected)

    def test_M_a_price_move_writes_current_price_and_not_the_catalog(self) -> None:
        """The whole point of PR 4.

        catalog_upsert was 49.7% of a 350-set ingest -- rewriting 15 indexes
        and ~7 GB of GIN for a price change that moved no searchable text.
        """
        row = _catalog_row("1", "Pikachu")
        stored = self._current_from(row)                       # metadata matches
        yesterday = self._current_from(row, loose_price_cents=555)
        transport, client, error = self._run_sync(
            [row], [stored], catalog_rows=[stored], current_price_rows=[yesterday])
        self.assertIsNone(error)
        self.assertEqual(len(transport.current_price_upserts), 1)
        self.assertEqual(transport.current_price_upserts[0]["loose_price_cents"], 1000)
        self.assertEqual(len(transport.price_history_rows), 1)   # snapshot still written
        self.assertEqual(len(transport.inserted_rows), 0)        # no SCD2 version
        self.assertEqual(client.current_price_stats["priceChanged"], 1)

    def test_N_the_current_price_row_carries_the_browse_keys(self) -> None:
        """Without them the browse indexes on that table cannot be used."""
        row = _catalog_row("1", "Pikachu")
        yesterday = self._current_from(row, loose_price_cents=555)
        transport, _, _ = self._run_sync(
            [row], [], catalog_rows=[], current_price_rows=[yesterday])
        written = transport.current_price_upserts[0]
        self.assertEqual(written["category"], row["category"])
        self.assertIn("platform_group", written)
        self.assertEqual(written["currency"], "USD")
        self.assertIsNotNone(written["observed_at"])

    def test_O_a_rename_with_no_price_move_still_patches_the_browse_keys(self) -> None:
        """Bullet 2, and the easiest thing in this change to miss.

        A category change that leaves the price alone must still reach
        current_price. Otherwise Discover browses the old category until the
        next price tick -- which for a stable item may be never.
        """
        row = _catalog_row("1", "Pikachu")
        stale_keys = self._current_from(row)
        stale_keys["category"] = "Was A Different Category"
        transport, client, _ = self._run_sync(
            [row], [self._current_from(row)], catalog_rows=[self._current_from(row)],
            current_price_rows=[stale_keys])
        self.assertEqual(len(transport.current_price_upserts), 1,
                         "the stale browse key was never corrected")
        self.assertEqual(transport.current_price_upserts[0]["category"], row["category"])
        self.assertEqual(client.current_price_stats["browseKeysOnly"], 1)
        self.assertEqual(len(transport.price_history_rows), 0,
                         "no price moved, so no snapshot belongs in the chart")

    def test_P_nothing_changed_writes_nothing_anywhere(self) -> None:
        row = _catalog_row("1", "Pikachu")
        same = self._current_from(row)
        transport, _, _ = self._run_sync(
            [row], [same], catalog_rows=[same], current_price_rows=[same])
        self.assertEqual(transport.current_price_upserts, [])
        self.assertEqual(transport.price_history_rows, [])
        self.assertEqual(transport.inserted_rows, [])

    def test_Q_a_new_item_writes_everywhere(self) -> None:
        row = _catalog_row("1", "Pikachu")
        transport, _, _ = self._run_sync([row], [], catalog_rows=[], current_price_rows=[])
        self.assertEqual(len(transport.current_price_upserts), 1)
        self.assertEqual(len(transport.price_history_rows), 1)
        self.assertEqual(len(transport.inserted_rows), 1)

    def test_R_the_price_baseline_is_current_price_not_the_catalog(self) -> None:
        """The stale-baseline trap, one table along from #213.

        The catalog's cents freeze once this PR ships. A gate still reading
        them would report "changed" on every run forever and upsert every row
        every day -- 12M writes daily into the table built to avoid them.
        Here the catalog is frozen at 555 and current_price already says 1000.
        """
        row = _catalog_row("1", "Pikachu")
        frozen_catalog = self._current_from(row, loose_price_cents=555)
        price_now = self._current_from(row)
        transport, _, _ = self._run_sync(
            [row], [self._current_from(row)], catalog_rows=[frozen_catalog],
            current_price_rows=[price_now])
        self.assertEqual(transport.current_price_upserts, [],
                         "upserted from a frozen catalog price")
        self.assertEqual(transport.price_history_rows, [])

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


class TheCatalogIsLookedUpOncePerBatchTest(unittest.TestCase):
    """Two passes, one lookup.

    sync_scd2_history_rows and upsert_rows each fetched the same current
    catalog rows for the same batch. Measured on a real 350-set ingest
    (2026-09-09) the duplicate cost ~81s of 595s -- 14% -- for an answer
    already in hand. The second call arrived with the price baseline in #213
    and sat 140 lines away from the first, which is how it went unnoticed.
    """

    def _run_both_passes(self, rows, current_rows, catalog_rows=None):
        transport = _FakeSupabaseTransport(current_rows=current_rows,
                                           catalog_rows=catalog_rows)
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.sync_scd2_history_rows(rows, batch_size=100)
            client.upsert_rows(rows, batch_size=100)
        catalog_gets = [u for u in transport.get_urls
                        if u.endswith("/pricecharting_catalog")]
        return transport, client, catalog_gets

    def test_the_catalog_is_fetched_once_not_twice(self) -> None:
        row = _catalog_row("1", "Pikachu")
        _, _, catalog_gets = self._run_both_passes([row], [])
        self.assertEqual(len(catalog_gets), 1,
                         f"catalog fetched {len(catalog_gets)} times for one batch")

    def test_the_second_pass_is_recorded_as_reused(self) -> None:
        row = _catalog_row("1", "Pikachu")
        _, client, _ = self._run_both_passes([row], [])
        self.assertEqual(client.catalog_lookup_stats["fetched"], 1)
        self.assertEqual(client.catalog_lookup_stats["reused"], 1)

    def test_both_decisions_see_the_same_baseline(self) -> None:
        """An unchanged row must be skipped by the upsert using the hash the
        SCD2 pass already fetched -- not a second, possibly different one."""
        row = _catalog_row("1", "Pikachu")
        stored = {"pricecharting_id": "1",
                  **{c: row.get(c) for c in CATALOG_METADATA_SIGNATURE_COLUMNS}}
        transport, client, catalog_gets = self._run_both_passes([row], [], [stored])
        self.assertEqual(len(catalog_gets), 1)
        self.assertEqual(transport.upserted_rows, [],
                         "an unchanged row was upserted; the reused hash did not match")
        self.assertEqual(client.catalog_write_stats["skippedUnchanged"], 1)

    def test_a_changed_row_is_still_upserted_from_the_reused_hash(self) -> None:
        row = _catalog_row("1", "Pikachu")
        stale = {"pricecharting_id": "1", "product_name": "A Different Name",
                 "console_name": "Pokemon Cards"}
        transport, _, catalog_gets = self._run_both_passes([row], [], [stale])
        self.assertEqual(len(catalog_gets), 1)
        self.assertEqual(len(transport.upserted_rows), 1)

    def test_an_item_absent_from_the_catalog_is_remembered_as_absent(self) -> None:
        """"Looked up and not found" and "never looked up" are different.

        Collapsing them sends every new item back down the fetch path, which
        is the whole cost this removes -- new items are common in a batch.
        """
        row = _catalog_row("1", "Pikachu")
        transport, client, catalog_gets = self._run_both_passes([row], [], [])
        self.assertEqual(len(catalog_gets), 1)
        self.assertEqual(len(transport.upserted_rows), 1, "a new row was not written")
        self.assertEqual(client.catalog_lookup_stats["reused"], 1)

    def test_upsert_alone_still_fetches(self) -> None:
        """Other callers run upsert_rows without the SCD2 pass; the cache
        cannot answer for them and must not pretend to."""
        row = _catalog_row("1", "Pikachu")
        transport = _FakeSupabaseTransport(current_rows=[])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.upsert_rows([row], batch_size=100)
        self.assertEqual(
            len([u for u in transport.get_urls if u.endswith("/pricecharting_catalog")]), 1)
        self.assertEqual(client.catalog_lookup_stats["reused"], 0)

    def test_a_batch_the_cache_only_partly_covers_is_refetched(self) -> None:
        """All-or-nothing: mixing cached and fetched answers inside one batch
        is how a stale hash turns a changed row into a skipped one."""
        first = _catalog_row("1", "Pikachu")
        second = _catalog_row("2", "Squirtle")
        transport = _FakeSupabaseTransport(current_rows=[])
        with patch("scripts.import_pricecharting_catalog.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseCatalogClient(
                supabase_url="https://example.supabase.co",
                service_role_key=_fake_supabase_jwt("service_role"),
                timeout_seconds=1,
            )
            client.sync_scd2_history_rows([first], batch_size=100)   # covers id 1 only
            client.upsert_rows([first, second], batch_size=100)      # needs 1 and 2
        catalog_gets = [u for u in transport.get_urls
                        if u.endswith("/pricecharting_catalog")]
        self.assertEqual(len(catalog_gets), 2, "the uncovered batch was not refetched")
        self.assertEqual(client.catalog_lookup_stats["reused"], 0)



class TheCopyWriterCannotSilentlyUndoPR4Test(unittest.TestCase):
    """scripts/tier3_copy_writer.py is two PRs behind and must not run.

    Its merge still gates the catalog on a price-inclusive content_hash and
    writes an SCD2 version whenever change_hash differs. Running it would undo
    #213 and PR 4 at once: every price move would rewrite the 25 GB search
    document and mint a version, and pricecharting_current_price would never be
    written -- leaving Discover and catalog detail on prices frozen at whatever
    that run left behind.

    Nothing reaches it today (its only caller is a suspended cron, and the
    staged ingester hardcodes WRITE_REST). This keeps that true by
    construction rather than by memory.
    """

    def test_constructing_it_refuses(self) -> None:
        from scripts.tier3_copy_writer import CopyCatalogWriter

        with self.assertRaises(NotImplementedError) as caught:
            CopyCatalogWriter("postgresql://example/db")
        message = str(caught.exception)
        self.assertIn("current_price", message)
        self.assertIn("content_hash", message)

    def test_the_message_says_what_to_do_about_it(self) -> None:
        from scripts.tier3_copy_writer import CopyCatalogWriter

        with self.assertRaises(NotImplementedError) as caught:
            CopyCatalogWriter("postgresql://example/db")
        self.assertIn("import_pricecharting_catalog", str(caught.exception))
