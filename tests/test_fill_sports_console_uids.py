import unittest

import httpx

from scripts.fill_sports_console_uids import (
    POSTGREST_MAX_ROWS,
    SPORTS_CATEGORIES,
    build_slug_index,
    fetch_autocomplete,
    fetch_uidless_rows,
    plan_updates,
    selected_categories,
    write_updates,
)


def _entry(label: str, value: str) -> dict[str, str]:
    return {"label": label, "value": value}


SENTINEL = _entry("all", "")


class BuildSlugIndexTest(unittest.TestCase):
    def test_the_sentinel_all_row_is_dropped(self) -> None:
        """Every category's response opens with {"label":"all","value":""}."""
        index, ambiguous = build_slug_index(
            "baseball-cards", [SENTINEL, _entry("2026 Topps", "G91286")]
        )
        self.assertEqual(index, {"baseball-cards-2026-topps": "G91286"})
        self.assertEqual(ambiguous, set())

    def test_a_slug_claimed_by_two_uids_is_written_by_neither(self) -> None:
        """Last-write-wins would silently pick one at random.

        Two labels can derive the same slug ("Topps Chrome" and "Topps
        Chrome!"). Nothing in the response says which one a crawled registry row
        meant, and a uid on the wrong set sends that set's whole CSV family into
        the wrong registry row.
        """
        index, ambiguous = build_slug_index(
            "baseball-cards",
            [_entry("Topps Chrome", "G1"), _entry("Topps Chrome!", "G2")],
        )
        self.assertNotIn("baseball-cards-topps-chrome", index)
        self.assertEqual(ambiguous, {"baseball-cards-topps-chrome"})

    def test_the_same_uid_listed_twice_is_not_ambiguous(self) -> None:
        """Duplicate labels for one uid agree; there is nothing to choose."""
        index, ambiguous = build_slug_index(
            "baseball-cards",
            [_entry("Topps Chrome", "G1"), _entry("Topps  Chrome", "G1")],
        )
        self.assertEqual(index, {"baseball-cards-topps-chrome": "G1"})
        self.assertEqual(ambiguous, set())


class PlanUpdatesTest(unittest.TestCase):
    """The slug shapes here are real, taken from uid-less baseball rows."""

    def _index(self) -> tuple[dict, dict]:
        index, ambiguous = build_slug_index(
            "baseball-cards",
            [SENTINEL,
             _entry("2023 Topps Pro Debut MiLB Legends", "G66199"),
             _entry("2023 Topps Pristine Let's Go", "G70001"),
             _entry("2024 Topps Allen & Ginter Cut Signature", "G70002")],
        )
        return {"baseball-cards": index}, {"baseball-cards": ambiguous}

    def test_a_matching_slug_gets_its_uid(self) -> None:
        index, ambiguous = self._index()
        rows = [{"registry_id": "r1", "category": "baseball-cards",
                 "slug": "baseball-cards-2023-topps-pro-debut-milb-legends",
                 "console_uid": None}]
        updates, counts = plan_updates(rows, index, ambiguous)
        self.assertEqual(updates, [{"registry_id": "r1", "console_uid": "G66199"}])
        self.assertEqual(counts["matched"], 1)

    def test_a_percent_encoded_apostrophe_stays_null(self) -> None:
        """The crawled slug keeps %27; _slugify collapses it to a hyphen.

        This row must produce no update at all -- not a near match, and above
        all not a second registry row. The registry's conflict key is
        (source_site, slug), so an INSERT here would duplicate the set and put
        two rows in the claim queue for one console_uid.
        """
        index, ambiguous = self._index()
        rows = [{"registry_id": "r2", "category": "baseball-cards",
                 "slug": "baseball-cards-2023-topps-pristine-let%27s-go",
                 "console_uid": None}]
        updates, counts = plan_updates(rows, index, ambiguous)
        self.assertEqual(updates, [])
        self.assertEqual(counts["unmatched"], 1)
        self.assertEqual(counts["matched"], 0)

    def test_a_literal_ampersand_stays_null(self) -> None:
        index, ambiguous = self._index()
        rows = [{"registry_id": "r3", "category": "baseball-cards",
                 "slug": "baseball-cards-2024-topps-allen-&-ginter-cut-signature",
                 "console_uid": None}]
        updates, counts = plan_updates(rows, index, ambiguous)
        self.assertEqual(updates, [])
        self.assertEqual(counts["unmatched"], 1)

    def test_an_ambiguous_slug_is_counted_apart_from_an_unmatched_one(self) -> None:
        """They are different failures and want different fixes."""
        index = {"baseball-cards": {}}
        ambiguous = {"baseball-cards": {"baseball-cards-topps-chrome"}}
        rows = [{"registry_id": "r4", "category": "baseball-cards",
                 "slug": "baseball-cards-topps-chrome", "console_uid": None},
                {"registry_id": "r5", "category": "baseball-cards",
                 "slug": "baseball-cards-nothing-like-this", "console_uid": None}]
        updates, counts = plan_updates(rows, index, ambiguous)
        self.assertEqual(updates, [])
        self.assertEqual(counts["ambiguous"], 1)
        self.assertEqual(counts["unmatched"], 1)

    def test_a_row_that_already_has_a_uid_is_never_rewritten(self) -> None:
        """The HTML scrape may have resolved it between the read and the write."""
        index, ambiguous = self._index()
        rows = [{"registry_id": "r6", "category": "baseball-cards",
                 "slug": "baseball-cards-2023-topps-pro-debut-milb-legends",
                 "console_uid": "G99999"}]
        updates, counts = plan_updates(rows, index, ambiguous)
        self.assertEqual(updates, [])
        self.assertEqual(counts["alreadySet"], 1)

    def test_a_slug_from_another_category_does_not_match(self) -> None:
        """G9157 is 'football-cards-2015-panini-donrus', not a baseball set.

        Indexes are per category on purpose: the derived slug carries the
        category prefix, so a football uid cannot land on a baseball row.
        """
        index, ambiguous = self._index()
        rows = [{"registry_id": "r7", "category": "football-cards",
                 "slug": "football-cards-2023-topps-pro-debut-milb-legends",
                 "console_uid": None}]
        updates, counts = plan_updates(rows, index, ambiguous)
        self.assertEqual(updates, [])
        self.assertEqual(counts["unmatched"], 1)

    def test_limit_caps_the_writes_but_not_the_report(self) -> None:
        """A bounded first run still has to report the true matched total."""
        index, ambiguous = self._index()
        rows = [{"registry_id": "r1", "category": "baseball-cards",
                 "slug": "baseball-cards-2023-topps-pro-debut-milb-legends",
                 "console_uid": None},
                {"registry_id": "r8", "category": "baseball-cards",
                 "slug": "baseball-cards-2023-topps-pristine-let-s-go",
                 "console_uid": None}]
        updates, counts = plan_updates(rows, index, ambiguous, limit=1)
        self.assertEqual(len(updates), 1)
        self.assertEqual(counts["matched"], 2)


class _FakeRegistryTransport:
    """Mirrors PostgREST's two behaviours that matter here: the silent 1,000-row
    cap, and that a PATCH filtered on console_uid=is.null matches nothing once
    the column is set."""

    def __init__(self, rows: list[dict], *, cap: int = POSTGREST_MAX_ROWS) -> None:
        self.rows = rows
        self.cap = cap
        self.patches: list[tuple[dict, dict]] = []
        self.get_calls: list[dict] = []

    def get(self, url, params=None, headers=None):
        self.get_calls.append(dict(params or {}))
        offset = int((params or {}).get("offset", 0))
        limit = min(int((params or {}).get("limit", self.cap)), self.cap)
        page = [row for row in self.rows if row.get("console_uid") is None]
        return httpx.Response(200, json=page[offset:offset + limit],
                              request=httpx.Request("GET", url))

    def patch(self, url, params=None, headers=None, json=None):
        self.patches.append((dict(params or {}), dict(json or {})))
        return httpx.Response(200, request=httpx.Request("PATCH", url))


class FetchUidlessRowsTest(unittest.TestCase):
    def test_more_rows_than_the_postgrest_cap_are_all_read(self) -> None:
        """19,273 rows against a 1,000-row cap. A single request returns 200
        with 1,000 rows and no error, so an unpaginated read would silently
        leave 18,273 sets starved and look like it had finished."""
        rows = [{"registry_id": f"r{i}", "slug": f"s{i}",
                 "category": "baseball-cards", "console_uid": None}
                for i in range(2500)]
        fake = _FakeRegistryTransport(rows)
        got = fetch_uidless_rows(http=fake, url="https://x", key="k",
                                 categories=["baseball-cards"], page_size=900)
        self.assertEqual(len(got), 2500)
        # 900 + 900 + 700: the short third page is what ends the loop.
        self.assertEqual([call["offset"] for call in fake.get_calls],
                         ["0", "900", "1800"])

    def test_a_row_count_that_is_an_exact_multiple_costs_one_empty_page(self) -> None:
        """1,800 rows in pages of 900 gives two FULL pages. Stopping on a full
        page would be indistinguishable from stopping on a truncated one, so the
        loop pays for one empty read rather than guess."""
        rows = [{"registry_id": f"r{i}", "slug": f"s{i}",
                 "category": "baseball-cards", "console_uid": None}
                for i in range(1800)]
        fake = _FakeRegistryTransport(rows)
        got = fetch_uidless_rows(http=fake, url="https://x", key="k",
                                 categories=["baseball-cards"], page_size=900)
        self.assertEqual(len(got), 1800)
        self.assertEqual([call["offset"] for call in fake.get_calls],
                         ["0", "900", "1800"])

    def test_a_page_size_at_the_cap_is_refused(self) -> None:
        """At exactly 1,000 a full page and a truncated one are identical."""
        with self.assertRaises(ValueError) as ctx:
            fetch_uidless_rows(http=_FakeRegistryTransport([]), url="https://x",
                               key="k", categories=["baseball-cards"],
                               page_size=POSTGREST_MAX_ROWS)
        self.assertIn("cap", str(ctx.exception))


class WriteUpdatesTest(unittest.TestCase):
    def test_every_patch_is_filtered_on_the_column_it_writes(self) -> None:
        """The write gate reads the thing it writes. Without this filter a uid
        resolved by the HTML scrape after our read would be overwritten by a
        slug guess."""
        fake = _FakeRegistryTransport([])
        written = write_updates([{"registry_id": "r1", "console_uid": "G1"}],
                                http=fake, url="https://x", key="k")
        self.assertEqual(written, 1)
        params, body = fake.patches[0]
        self.assertEqual(params["console_uid"], "is.null")
        self.assertEqual(params["registry_id"], "eq.r1")
        self.assertEqual(params["source_site"], "eq.sportscardspro")
        self.assertEqual(body, {"console_uid": "G1"})

    def test_nothing_but_console_uid_is_ever_written(self) -> None:
        """No url rewrite, no last_fetch_status, no tier3_refreshed_at.

        The crawled url is the real one and 7% of slugs do not round-trip, so a
        derived url would point good rows at wrong pages. Every sports row is
        already last_fetch_status='success' -- the uid is the only missing gate.
        """
        fake = _FakeRegistryTransport([])
        write_updates([{"registry_id": "r1", "console_uid": "G1"}],
                      http=fake, url="https://x", key="k")
        _, body = fake.patches[0]
        self.assertEqual(list(body), ["console_uid"])

    def test_no_request_is_ever_a_post(self) -> None:
        """This PR does not insert. Autocomplete lists 80,256 sets against
        36,962 registry rows; inserting the difference is a separate decision."""
        fake = _FakeRegistryTransport([])
        self.assertFalse(hasattr(fake, "post"))
        write_updates([{"registry_id": "r1", "console_uid": "G1"}],
                      http=fake, url="https://x", key="k")


class FetchAutocompleteTest(unittest.TestCase):
    def test_the_404_page_is_not_mistaken_for_data(self) -> None:
        """/consoles-autocomplete/sports-cards returns 404 with
        content-type application/json and an HTML body. raise_for_status catches
        that one; this guards the shape if a wrong slug ever answers 200."""
        class _Fake:
            def get(self, url, headers=None):
                return httpx.Response(200, json={"error": "not found"},
                                      request=httpx.Request("GET", url))
        with self.assertRaises(ValueError) as ctx:
            fetch_autocomplete("sports-cards", http=_Fake(),
                               base_url="https://x")
        self.assertIn("list", str(ctx.exception))


class SelectedCategoriesTest(unittest.TestCase):
    def test_the_umbrella_slugs_are_not_categories(self) -> None:
        """Both 404. They are the reason this endpoint looked absent for sports."""
        for slug in ("sports-cards", "sportscardspro"):
            self.assertNotIn(slug, SPORTS_CATEGORIES)
            with self.assertRaises(ValueError):
                selected_categories(slug)

    def test_the_default_is_all_eight(self) -> None:
        self.assertEqual(selected_categories(None), SPORTS_CATEGORIES)
        self.assertEqual(len(SPORTS_CATEGORIES), 8)


if __name__ == "__main__":
    unittest.main()
