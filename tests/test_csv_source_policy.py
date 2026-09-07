"""CSV downloads go to pricecharting.com, and must prove what they contain.

Both halves of this exist because of a failure that looked like success.

HOST. sportscardspro.com's /price-guide/download-custom returns a Cloudflare
403 challenge -- from Render since ~2026-08-31, and verified 2026-09-07 from
a laptop as well, so no host reaches it any more. The vendor confirmed the
CSV is identical on pricecharting.com, and a live probe confirmed a sports
console_uid resolves there (G24631 -> 200, 300 rows, one family
"Baseball Cards 1887 N172 Old Judge"). The routing change had been agreed and
tested before, and never reached the code -- seven call sites still pointed at
the blocked host -- which is the reason these tests assert the POLICY at every
call site rather than one line in the tier-3 script.

CONTENTS. download-custom does not validate its filter. Sending
`console_uids` (underscore) instead of `console-uids` (hyphen) returns HTTP
200, text/csv, 19.9 MB and 123,166 well-formed rows of the entire video-game
catalog for a request naming three baseball sets. Every downstream check
passes: the columns parse, the prices are real. Those rows would have been
written under the tier-3 source tag and the sports sets stamped refreshed.
test_the_real_wrong_parameter_response_is_refused reproduces that response
shape directly.
"""

import ast
import pathlib
import unittest

from scripts.csv_source_policy import (
    CSV_DOWNLOAD_BASE_URL,
    TRANSIENT_CSV_STATUSES,
    CsvFamilyMismatch,
    csv_base_url,
    normalize_family,
    validate_csv_families,
)


SCRIPTS = pathlib.Path("scripts")

# Every script that performs a /price-guide/download-custom call.
CSV_CALLERS = (
    "refresh_sportscardspro_rotation.py",
    "backfill_pricecharting_sets.py",
    "diagnose_price_overflow.py",
)


class HostPolicyTest(unittest.TestCase):
    def test_every_source_site_routes_csv_to_pricecharting(self) -> None:
        for site in ("sportscardspro", "pricecharting", None, "", "something-new"):
            with self.subTest(source_site=site):
                self.assertEqual(csv_base_url(site), "https://www.pricecharting.com")

    def test_the_blocked_host_is_never_returned(self) -> None:
        self.assertNotIn("sportscardspro", CSV_DOWNLOAD_BASE_URL)


class NoCsvCallerBypassesThePolicyTest(unittest.TestCase):
    """The defect was seven call sites, not one -- so assert all of them.

    A previous agreed switch was applied nowhere; patching only the tier-3
    line would have left refresh_small_sets and refresh_tracked_catalog_items
    still talking to a blocked host. This fails if a CSV caller goes back to
    indexing SOURCE_SITE_BASE_URLS directly.
    """

    def test_csv_callers_use_csv_base_url(self) -> None:
        for name in CSV_CALLERS:
            with self.subTest(script=name):
                source = (SCRIPTS / name).read_text()
                self.assertIn(
                    "csv_base_url(",
                    source,
                    f"{name} performs CSV downloads but does not use the host policy",
                )

    def test_no_csv_DOWNLOADING_function_resolves_its_own_host(self) -> None:
        """Precise by construction: only functions that actually fetch a CSV.

        A blanket textual ban on SOURCE_SITE_BASE_URLS[...] is wrong for
        backfill_pricecharting_sets.py, which legitimately keeps two of them
        for the /api/products search path -- that endpoint is not blocked and
        is out of scope. So this walks the AST and flags the map only inside a
        function that also performs a download-custom fetch.
        """
        csv_markers = ("fetch_batch_csv", "download-custom")
        for name in CSV_CALLERS:
            with self.subTest(script=name):
                tree = ast.parse((SCRIPTS / name).read_text())
                offenders = []
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    body = ast.dump(node)
                    if not any(marker in body for marker in csv_markers):
                        continue
                    for inner in ast.walk(node):
                        if (
                            isinstance(inner, ast.Subscript)
                            and isinstance(inner.value, ast.Name)
                            and inner.value.id == "SOURCE_SITE_BASE_URLS"
                        ):
                            offenders.append(f"{node.name}:{inner.lineno}")
                self.assertEqual(
                    offenders,
                    [],
                    f"{name} resolves a CSV host directly inside a "
                    f"CSV-downloading function at {offenders}; use csv_base_url()",
                )

    def test_the_api_search_scripts_are_deliberately_untouched(self) -> None:
        """/api/products is NOT blocked, and is out of scope for this change.

        Asserted so a later reader does not "finish the job" by moving the
        API callers too -- that would be an unverified change to a path that
        works, on a host whose sports coverage nobody has probed.
        """
        for name in ("refresh_small_sets.py", "refresh_tracked_catalog_items.py"):
            with self.subTest(script=name):
                source = (SCRIPTS / name).read_text()
                self.assertIn("SOURCE_SITE_BASE_URLS[", source)


class FamilyValidationTest(unittest.TestCase):
    def test_a_matching_single_set_passes(self) -> None:
        # The real probe: uid G24631, set_name "1887 N172 Old Judge",
        # returned console-name "Baseball Cards 1887 N172 Old Judge".
        validate_csv_families(
            {"Baseball Cards 1887 N172 Old Judge"},
            expected_set_names=["1887 N172 Old Judge"],
            requested_uid_count=1,
        )

    def test_the_video_game_probe_shape_also_passes(self) -> None:
        validate_csv_families(
            {"Comic Books '68 Compendium"},
            expected_set_names=["'68 Compendium"],
            requested_uid_count=1,
        )

    def test_case_and_whitespace_differences_do_not_fail_a_good_batch(self) -> None:
        validate_csv_families(
            {"  BASEBALL CARDS   1887 N172 Old Judge "},
            expected_set_names=["1887 n172 old judge"],
            requested_uid_count=1,
        )

    def test_several_matching_sets_pass(self) -> None:
        validate_csv_families(
            {"Baseball Cards 1909 E90-1 American Caramel", "Baseball Cards 1887 N172 Old Judge"},
            expected_set_names=["1887 N172 Old Judge", "1909 E90-1 American Caramel"],
            requested_uid_count=2,
        )

    def test_the_real_wrong_parameter_response_is_refused(self) -> None:
        """The landmine, at its measured shape: 3 uids in, 229 families out."""
        wrong_catalog = {f"Console {index}" for index in range(229)}
        with self.assertRaises(CsvFamilyMismatch) as caught:
            validate_csv_families(
                wrong_catalog,
                expected_set_names=[
                    "1887 N172 Old Judge",
                    "1909 Colgan's Chips Stars of the Diamond",
                    "1909 E90-1 American Caramel",
                ],
                requested_uid_count=3,
            )
        self.assertIn("cannot widen", str(caught.exception))
        self.assertEqual(caught.exception.requested_uid_count, 3)

    def test_a_small_wrong_catalog_is_refused_by_the_naming_check(self) -> None:
        """The count check alone cannot see this one.

        One uid requested, one family returned -- the counts agree perfectly.
        Only the names reveal that it is the wrong set entirely.
        """
        with self.assertRaises(CsvFamilyMismatch) as caught:
            validate_csv_families(
                {"Nintendo 64"},
                expected_set_names=["1887 N172 Old Judge"],
                requested_uid_count=1,
            )
        self.assertIn("match no requested set", str(caught.exception))
        self.assertIn("Nintendo 64", str(caught.exception).lower().title())

    def test_a_trailing_number_does_not_match_a_longer_one(self) -> None:
        """The substring accident: "Set 1" must not match "…Set 10".

        Bare containment accepted this, and real set names differ exactly
        this way ("2023 Panini Prizm 1" vs "… 10"), so it was a live route
        to writing one set's prices under another set's identity.
        """
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                {"Baseball Cards Set 10"},
                expected_set_names=["Set 1"],
                requested_uid_count=1,
            )

    def test_the_word_boundary_case_still_passes(self) -> None:
        validate_csv_families(
            {"Baseball Cards Set 10"},
            expected_set_names=["Set 10"],
            requested_uid_count=1,
        )
        validate_csv_families(
            {"Baseball Cards Set 1"}, expected_set_names=["Set 1"], requested_uid_count=1
        )

    def test_a_bare_prefix_overlap_is_not_a_match(self) -> None:
        """"Old Judge" is not "Old Judgement" -- and neither contains the
        other on a word boundary."""
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                {"Baseball Cards 1887 N172 Old Judgement"},
                expected_set_names=["1887 N172 Old Judge"],
                requested_uid_count=1,
            )

    def test_one_bad_family_among_good_ones_fails_the_whole_batch(self) -> None:
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                {"Baseball Cards 1887 N172 Old Judge", "Sega Genesis"},
                expected_set_names=["1887 N172 Old Judge", "1909 E90-1 American Caramel"],
                requested_uid_count=2,
            )

    def test_an_empty_csv_passes_because_there_is_nothing_to_write(self) -> None:
        validate_csv_families([], expected_set_names=["anything"], requested_uid_count=1)

    def test_validation_degrades_to_the_count_check_without_set_names(self) -> None:
        """Missing set_name must not disable the guard entirely."""
        validate_csv_families({"Whatever"}, expected_set_names=[], requested_uid_count=1)
        with self.assertRaises(CsvFamilyMismatch):
            validate_csv_families(
                {"A", "B", "C"}, expected_set_names=["", ""], requested_uid_count=1
            )

    def test_normalize_preserves_punctuation(self) -> None:
        """Punctuation distinguishes real sets -- stripping it would make two
        different sets compare equal, the wrong error for a guard whose job
        is telling sets apart."""
        self.assertEqual(normalize_family("  Colgan's   Chips "), "colgan's chips")
        self.assertNotEqual(normalize_family("E90-1"), normalize_family("E901"))


class TransientStatusTest(unittest.TestCase):
    def test_upstream_failures_are_classified_transient(self) -> None:
        for status in (500, 502, 503, 504):
            self.assertIn(status, TRANSIENT_CSV_STATUSES)

    def test_throttle_and_block_are_not_transient(self) -> None:
        """429 and 403 have their own handling and must not be folded in."""
        for status in (403, 429):
            self.assertNotIn(status, TRANSIENT_CSV_STATUSES)


class TransientFailureDoesNotParkHealthySetsTest(unittest.TestCase):
    """A passing 503 must not permanently shrink the rotation queue.

    Before this change a status that was neither 429 nor 403 was read as
    "download-custom refused this specific console_uid" and sent the batch to
    _isolate_failed_batch, which calls record_tier3_failures() on each set
    that fails individually. During a transient outage every set fails, and
    three such records park a set out of the queue for good. The 503 observed
    on 2026-09-07 was transient -- the same request succeeded later -- so
    this path could have quietly deleted healthy sets from the rotation.

    Asserted against the source because the surrounding loop needs a live
    HTTP session, a registry client and a writer to execute.
    """

    def test_transient_statuses_are_handled_before_isolation(self) -> None:
        source = (SCRIPTS / "refresh_sportscardspro_rotation.py").read_text()
        guard = source.index("TRANSIENT_CSV_STATUSES")
        isolate = source.index("_isolate_failed_batch(")
        self.assertLess(
            guard,
            isolate,
            "the transient-status check must come BEFORE the isolation path, "
            "or a 503 will record tier-3 failures against healthy sets",
        )

    def test_the_transient_branch_does_not_record_failures(self) -> None:
        source = (SCRIPTS / "refresh_sportscardspro_rotation.py").read_text()
        start = source.index("if status in TRANSIENT_CSV_STATUSES:")
        branch = source[start : source.index("if throttled or not status:", start)]
        self.assertNotIn("record_tier3_failures", branch)
        self.assertIn("continue", branch)


class ValidationRunsBeforeAnyWriteTest(unittest.TestCase):
    """Order is the whole guarantee: validate, then write, then stamp.

    If validation ran after the write loop it would report a mismatch having
    already written the wrong catalog, and if it ran after the stamp the sets
    would be marked refreshed from bad data.
    """

    def _positions(self, name: str) -> tuple[int, int]:
        source = (SCRIPTS / name).read_text()
        return source.index("validate_csv_families("), source.index("def _iter_catalog_rows(")

    def test_tier3_validates_before_parsing_rows_for_write(self) -> None:
        validate_at, write_at = self._positions("refresh_sportscardspro_rotation.py")
        self.assertLess(validate_at, write_at)

    def test_backfill_validates_before_parsing_rows_for_write(self) -> None:
        validate_at, write_at = self._positions("backfill_pricecharting_sets.py")
        self.assertLess(validate_at, write_at)

    def test_tier3_does_not_stamp_refreshed_on_a_mismatch(self) -> None:
        source = (SCRIPTS / "refresh_sportscardspro_rotation.py").read_text()
        start = source.index("except CsvFamilyMismatch as exc:")
        branch = source[start : source.index("def _iter_catalog_rows(", start)]
        self.assertNotIn("mark_tier3_refreshed", branch)
        self.assertIn("continue", branch)

    def test_both_csv_writers_have_a_validation_gate(self) -> None:
        for name in ("refresh_sportscardspro_rotation.py", "backfill_pricecharting_sets.py"):
            with self.subTest(script=name):
                source = (SCRIPTS / name).read_text()
                self.assertIn("validate_csv_families(", source)
                self.assertIn("CsvFamilyMismatch", source)


class ScriptsStillParseTest(unittest.TestCase):
    def test_every_touched_script_is_valid_python(self) -> None:
        for name in CSV_CALLERS + ("csv_source_policy.py",):
            with self.subTest(script=name):
                ast.parse((SCRIPTS / name).read_text())




class WrongCatalogIsRefusedEndToEndTest(unittest.TestCase):
    """The unit tests prove the validator; this proves the ROTATION uses it.

    Runs the real main() loop with a fetch that returns the wrong catalog --
    the measured landmine shape, a large valid CSV of unrelated console-names
    -- and asserts that nothing is written and nothing is stamped refreshed.
    Without the gate this run writes rows and marks every set done, which is
    precisely the silent corruption the guard exists to prevent.
    """

    def _run(self, csv_body: str):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from scripts.backfill_pricecharting_sets import CsvDownload
        from scripts.refresh_sportscardspro_rotation import main

        class _Registry:
            def __init__(self, rows):
                self._rows = rows
                self.refreshed: list[str] = []
                self.failures: list[str] = []

            def fetch_rotation_rows(self, *, limit):
                return self._rows[:limit]

            def mark_tier3_refreshed(self, ids):
                self.refreshed.extend(ids)

            def record_tier3_failures(self, ids, *, error):
                self.failures.extend(ids)

        registry = _Registry(
            [
                {"registry_id": f"r{i}", "console_uid": f"G{i}", "set_name": f"Set {i}"}
                for i in range(2)
            ]
        )
        made: list[Path] = []

        def fake_fetch(*a, **kw):
            handle, name = tempfile.mkstemp(prefix="pricecharting-batch-", suffix=".csv")
            path = Path(name)
            with open(handle, "w", encoding="utf-8") as out:
                out.write(csv_body)
            made.append(path)
            return CsvDownload(path, "utf-8")

        writes: list[int] = []

        def fake_write(client, rows, *, batch_size, attempts, backoff_seconds):
            writes.append(len(rows))
            return True, 0

        mod = "scripts.refresh_sportscardspro_rotation"
        try:
            with mock.patch(f"{mod}.Tier3RegistryClient", return_value=registry), mock.patch(
                f"{mod}.fetch_batch_csv_file_with_retry", fake_fetch
            ), mock.patch(
                f"{mod}.write_catalog_rows_with_retry", fake_write
            ), mock.patch(
                f"{mod}.SupabaseCatalogClient"
            ) as catalog, mock.patch(
                f"{mod}.SharedRateLimiter"
            ) as limiter, mock.patch.dict(
                "os.environ",
                {"SUPABASE_URL": "https://x.test", "SUPABASE_SERVICE_ROLE_KEY": "k",
                 "PRICECHARTING_API_TOKEN": "t"},
            ):
                limiter.return_value.acquire.return_value = True
                # The run summary serialises these, so they have to be real.
                catalog.return_value.catalog_write_stats = {
                    "written": 0, "skippedUnchanged": 0, "failed": 0
                }
                catalog.return_value.price_history_stats = {
                    "attempted": 0, "inserted": 0, "duplicateSkipped": 0, "failed": 0
                }
                catalog.return_value.phase_seconds = {}
                main(["--batch-size", "2", "--max-requests", "1",
                      "--ingest-chunk-rows", "20"])
        finally:
            for path in made:
                path.unlink(missing_ok=True)
        return registry, writes

    def test_the_wrong_catalog_writes_nothing_and_stamps_nothing(self) -> None:
        wrong = "id,console-name,product-name,loose-price\n" + "".join(
            f"{i},Console {i % 229},Game {i},$1.00\n" for i in range(1, 400)
        )
        registry, writes = self._run(wrong)
        self.assertEqual(writes, [], "wrong-catalog rows reached the write path")
        self.assertEqual(registry.refreshed, [], "sets were stamped refreshed from a wrong CSV")

    def test_a_correct_catalog_still_writes_and_stamps(self) -> None:
        """The guard must not simply block everything."""
        good = "id,console-name,product-name,loose-price\n" + "".join(
            f"{i},Baseball Cards Set {i % 2},Card {i},$1.00\n" for i in range(1, 40)
        )
        registry, writes = self._run(good)
        self.assertTrue(writes, "a valid batch should have been written")
        self.assertEqual(sorted(registry.refreshed), ["r0", "r1"])


if __name__ == "__main__":
    unittest.main()
