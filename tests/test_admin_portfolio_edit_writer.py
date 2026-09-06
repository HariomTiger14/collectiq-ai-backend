"""The admin portfolio item edit must write columns that actually exist.

`PATCH /admin/portfolio/items/{id}` failed on every call. The writer sent five
columns `portfolio_items` does not have -- `data`, `pricing`, `needs_review`,
`reviewed_at`, `review_status` -- so PostgREST rejected the row and the console
showed a generic failure. The real JSON column is `raw_json`.

Two things kept this invisible. The existing admin-portfolio tests exercise the
in-memory `portfolio_service`, never the Supabase repository, so they could not
see it. And the review-queue tests that DO exercise the Supabase path asserted
the phantom columns, because their fixtures were written to an imagined schema
rather than the real one -- so the broken writer had passing tests.

These tests assert against the real column set, verified live against the
production table on 2026-09-06:

    category, cloud_image_url, country, created_at, estimated_value_high,
    estimated_value_low, id, image_local_path, image_storage_path,
    last_synced_at, manufacturer, pricecharting_id,
    pricecharting_match_attempted_at, pricecharting_match_score,
    pricecharting_matched_at, raw_json, series, sync_status, title,
    updated_at, user_id, year

The hazard worth stating plainly: the old writer also recomputed
`estimated_value_low`/`_high` from the merged blob on EVERY call. On a
metadata-only edit with no usable pricing block that resolves to 0, so an
admin editing a note could have zeroed the item's value. The phantom columns
were the only thing preventing it -- fixing the names without removing the
recomputation would have turned a broken-but-safe route into a working-and-
destructive one.
"""

import unittest

import httpx

from app.services.admin_portfolio_service import AdminPortfolioService
from app.services.pricing.admin_review_queue_service import (
    SupabasePricingReviewQueueRepository,
)


# The columns portfolio_items genuinely has. Anything written outside this set
# is rejected by PostgREST.
REAL_COLUMNS = {
    "category", "cloud_image_url", "country", "created_at",
    "estimated_value_high", "estimated_value_low", "id", "image_local_path",
    "image_storage_path", "last_synced_at", "manufacturer", "pricecharting_id",
    "pricecharting_match_attempted_at", "pricecharting_match_score",
    "pricecharting_matched_at", "raw_json", "series", "sync_status", "title",
    "updated_at", "user_id", "year",
}

PHANTOM_COLUMNS = ("data", "pricing", "needs_review", "reviewed_at", "review_status")


class _FakeClient:
    """Returns a realistic portfolio_items row and records what was sent."""

    def __init__(self, row: dict | None = None):
        self.row = row or {
            "id": "item-1",
            "user_id": "collector-1",
            "title": "Raichu",
            "category": "Trading Card Game",
            "sync_status": "synced",
            "estimated_value_low": 1.5,
            "estimated_value_high": 17.84,
            "raw_json": {
                "title": "Raichu",
                "category": "Trading Card Game",
                "condition": "Unknown",
                "estimatedValue": 1.5,
                "pricing": {"estimatedMarketValue": 1.5, "currency": "USD"},
                "images": ["a.jpg", "b.jpg"],
                "syncStatus": "synced",
            },
        }
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        payload = [self.row] if method in ("GET", "PATCH") else []
        return httpx.Response(
            status_code=200,
            json=payload,
            request=httpx.Request(method, url),
        )

    @property
    def patch_body(self) -> dict:
        patches = [r for r in self.requests if r["method"] == "PATCH"]
        assert patches, "no PATCH was issued"
        return patches[-1]["json"]


def _service(client: _FakeClient) -> AdminPortfolioService:
    return AdminPortfolioService(
        repository=SupabasePricingReviewQueueRepository(
            supabase_url="https://supabase.test",
            service_role_key="service-role",
            client=client,
        )
    )


class PortfolioEditWritesRealColumnsTest(unittest.TestCase):
    def test_no_phantom_columns_are_ever_sent(self) -> None:
        """The exact defect: five columns that do not exist on the table."""
        client = _FakeClient()
        _service(client).update_item("item-1", {"adminNotes": "checked"}, actor="a@b.com")

        for phantom in PHANTOM_COLUMNS:
            with self.subTest(column=phantom):
                self.assertNotIn(phantom, client.patch_body)

    def test_every_written_column_exists_on_the_table(self) -> None:
        client = _FakeClient()
        _service(client).update_item(
            "item-1",
            {"category": "Sneakers", "condition": "Near Mint", "adminNotes": "note"},
            actor="a@b.com",
        )

        unknown = set(client.patch_body) - REAL_COLUMNS
        self.assertEqual(unknown, set(), f"wrote columns that do not exist: {unknown}")

    def test_metadata_edit_writes_raw_json_and_updated_at(self) -> None:
        client = _FakeClient()
        _service(client).update_item("item-1", {"adminNotes": "checked"}, actor="a@b.com")

        body = client.patch_body
        self.assertIn("raw_json", body)
        self.assertIn("updated_at", body)


class MetadataFieldsLandInRawJsonTest(unittest.TestCase):
    def test_admin_notes_edit_writes_raw_json_admin_notes(self) -> None:
        client = _FakeClient()
        _service(client).update_item(
            "item-1", {"adminNotes": "Verified from admin portal."}, actor="a@b.com"
        )
        self.assertEqual(
            client.patch_body["raw_json"]["adminNotes"], "Verified from admin portal."
        )

    def test_condition_edit_writes_raw_json_condition(self) -> None:
        client = _FakeClient()
        _service(client).update_item("item-1", {"condition": "Near Mint"}, actor="a@b.com")
        self.assertEqual(client.patch_body["raw_json"]["condition"], "Near Mint")

    def test_category_edit_writes_both_raw_json_and_the_top_level_column(self) -> None:
        """The table carries both, and readers disagree about which wins."""
        client = _FakeClient()
        _service(client).update_item("item-1", {"category": "Sneakers"}, actor="a@b.com")

        body = client.patch_body
        self.assertEqual(body["raw_json"]["category"], "Sneakers")
        self.assertEqual(body["category"], "Sneakers")

    def test_a_non_category_edit_does_not_touch_the_category_column(self) -> None:
        client = _FakeClient()
        _service(client).update_item("item-1", {"condition": "Mint"}, actor="a@b.com")
        self.assertNotIn("category", client.patch_body)

    def test_existing_raw_json_content_is_preserved(self) -> None:
        """raw_json is the app's full client model -- an edit must merge, not replace.

        The previous implementation merged into get_item()'s reconstruction,
        which surfaces only a handful of fields, and wrote that back. Anything
        the reconstruction did not reproduce -- images, sync bookkeeping --
        would have been dropped.
        """
        client = _FakeClient()
        _service(client).update_item("item-1", {"adminNotes": "note"}, actor="a@b.com")

        raw = client.patch_body["raw_json"]
        self.assertEqual(raw["images"], ["a.jpg", "b.jpg"])
        self.assertEqual(raw["syncStatus"], "synced")
        self.assertEqual(raw["title"], "Raichu")
        self.assertEqual(raw["pricing"]["estimatedMarketValue"], 1.5)


class MetadataEditNeverTouchesValuationTest(unittest.TestCase):
    """The hazard this fix exists to avoid, not just the broken column names."""

    def test_metadata_edit_does_not_write_value_columns(self) -> None:
        client = _FakeClient()
        _service(client).update_item(
            "item-1",
            {"category": "Sneakers", "condition": "Mint", "adminNotes": "note"},
            actor="a@b.com",
        )

        body = client.patch_body
        self.assertNotIn("estimated_value_low", body)
        self.assertNotIn("estimated_value_high", body)

    def test_metadata_edit_cannot_zero_a_value_when_pricing_is_missing(self) -> None:
        """The precise failure the old writer would have caused once fixed.

        With no usable pricing block, the old recomputation resolved to 0 and
        wrote it into the value columns. An admin editing a note would have
        zeroed the item.
        """
        client = _FakeClient(
            row={
                "id": "item-2",
                "user_id": "collector-1",
                "estimated_value_low": 42.0,
                "estimated_value_high": 99.0,
                "raw_json": {"title": "No Pricing Block", "estimatedValue": 99.0},
            }
        )
        _service(client).update_item("item-2", {"adminNotes": "note"}, actor="a@b.com")

        body = client.patch_body
        self.assertNotIn("estimated_value_low", body)
        self.assertNotIn("estimated_value_high", body)
        self.assertNotIn("estimatedValue", set(body) - {"raw_json"})
        # And the value inside raw_json is carried through untouched.
        self.assertEqual(body["raw_json"]["estimatedValue"], 99.0)


class FinancialAndWorkflowFieldsStayBlockedTest(unittest.TestCase):
    """Pre-existing protection (§5.2) must survive this change.

    Price/currency/confidence/provider/valuationStatus/reviewStatus have their
    own audited path -- the review queue's override, with a mandatory note.
    This general-purpose edit must remain unable to reach them.
    """

    def test_pricing_and_workflow_fields_in_the_request_are_ignored(self) -> None:
        client = _FakeClient()
        _service(client).update_item(
            "item-1",
            {
                "category": "Sneakers",
                "price": 9999,
                "currency": "AUD",
                "confidence": 12,
                "pricingProvider": "admin_override",
                "valuationStatus": "reviewed",
                "reviewStatus": "reviewed",
            },
            actor="a@b.com",
        )

        body = client.patch_body
        raw = body["raw_json"]
        # The one editable field did change...
        self.assertEqual(raw["category"], "Sneakers")
        # ...and none of the financial/workflow fields reached raw_json at all.
        for blocked in (
            "price", "currency", "confidence", "pricingProvider",
            "valuationStatus", "reviewStatus",
        ):
            with self.subTest(field=blocked):
                self.assertNotIn(blocked, raw)
        # The untouched pricing block is still the provider's.
        self.assertEqual(raw["pricing"]["currency"], "USD")
        self.assertEqual(raw["pricing"]["estimatedMarketValue"], 1.5)

    def test_admin_edit_stamps_who_and_when(self) -> None:
        client = _FakeClient()
        _service(client).update_item("item-1", {"adminNotes": "n"}, actor="ops@packlox.com")

        raw = client.patch_body["raw_json"]
        self.assertEqual(raw["adminLastEditedBy"], "ops@packlox.com")
        self.assertIn("adminLastEditedAt", raw)


if __name__ == "__main__":
    unittest.main()
