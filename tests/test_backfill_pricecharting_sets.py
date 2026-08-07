import json
import unittest
from unittest.mock import patch

import httpx

from scripts.backfill_pricecharting_sets import (
    SupabaseRegistryOpsClient,
    chunked,
    fetch_batch_csv,
    group_by_site,
    resolve_console_uids,
    write_catalog_rows,
)


SET_PAGE_HTML = """
<html><body><script>
    VGPC.console_uid = "G58495";
</script></body></html>
"""


class ChunkedTest(unittest.TestCase):
    def test_splits_into_even_and_remainder_chunks(self) -> None:
        self.assertEqual(chunked([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_rejects_non_positive_size(self) -> None:
        with self.assertRaises(ValueError):
            chunked([1], 0)


class GroupBySiteTest(unittest.TestCase):
    def test_groups_rows_by_source_site(self) -> None:
        rows = [
            {"source_site": "pricecharting", "registry_id": "1"},
            {"source_site": "sportscardspro", "registry_id": "2"},
            {"source_site": "pricecharting", "registry_id": "3"},
        ]
        grouped = group_by_site(rows)
        self.assertEqual({r["registry_id"] for r in grouped["pricecharting"]}, {"1", "3"})
        self.assertEqual({r["registry_id"] for r in grouped["sportscardspro"]}, {"2"})


class ResolveConsoleUidsTest(unittest.TestCase):
    def test_skips_rows_that_already_have_a_console_uid(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": "G1"}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, rows)
        self.assertEqual(failed, [])

    def test_resolves_and_records_newly_found_console_uids(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SET_PAGE_HTML))
        )
        registry_client = _RecordingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=False
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["console_uid"], "G58495")
        self.assertEqual(failed, [])
        self.assertEqual(len(registry_client.updated_batches), 1)
        self.assertEqual(registry_client.updated_batches[0][0]["console_uid"], "G58495")

    def test_dry_run_does_not_persist_resolved_console_uids(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SET_PAGE_HTML))
        )
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved[0]["console_uid"], "G58495")
        self.assertEqual(failed, [])

    def test_marks_a_page_with_no_console_uid_as_failed(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>no id here</html>"))
        )
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(failed), 1)

    def test_marks_a_fetch_error_as_failed(self) -> None:
        rows = [{"registry_id": "1", "url": "https://example.test/x", "console_uid": None}]
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        registry_client = _ExplodingRegistryClient()

        resolved, failed = resolve_console_uids(
            rows, http=http, registry_client=registry_client, sleep_seconds=0, dry_run=True
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(failed), 1)


class FetchBatchCsvTest(unittest.TestCase):
    def test_builds_comma_separated_console_uids_param(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, text="id,console-name\n1,Foo\n")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        text = fetch_batch_csv(http, base_url="https://example.test", token="tok", console_uids=["G1", "G2"])

        self.assertEqual(text, "id,console-name\n1,Foo\n")
        self.assertEqual(seen["params"]["t"], "tok")
        self.assertEqual(seen["params"]["console-uids"], "G1,G2")

    def test_returns_none_on_http_error(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        text = fetch_batch_csv(http, base_url="https://example.test", token="tok", console_uids=["G1"])
        self.assertIsNone(text)


class WriteCatalogRowsTest(unittest.TestCase):
    def test_returns_true_when_both_writes_succeed(self) -> None:
        client = _FakeCatalogClient()
        result = write_catalog_rows(client, [{"pricecharting_id": "1"}], batch_size=50)

        self.assertTrue(result)
        self.assertEqual(client.history_calls, 1)
        self.assertEqual(client.upsert_calls, 1)

    def test_a_systemexit_from_the_catalog_write_does_not_propagate(self) -> None:
        client = _FakeCatalogClient(raise_on="upsert", error=SystemExit("statement timeout"))
        result = write_catalog_rows(client, [{"pricecharting_id": "1"}], batch_size=50)

        self.assertFalse(result)

    def test_a_regular_exception_from_the_catalog_write_does_not_propagate(self) -> None:
        client = _FakeCatalogClient(raise_on="history", error=RuntimeError("boom"))
        result = write_catalog_rows(client, [{"pricecharting_id": "1"}], batch_size=50)

        self.assertFalse(result)


class SupabaseRegistryOpsClientTest(unittest.TestCase):
    def test_claim_rows_queries_unclaimed_or_expired_leases_and_marks_them_claimed(self) -> None:
        transport = _FakeRegistryOpsTransport(
            claimable_rows=[
                {"registry_id": "1", "source_site": "pricecharting", "url": "https://x/1", "console_uid": None, "failure_count": 0},
            ]
        )
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            rows = client.claim_rows(limit=10, lease_minutes=30, worker_id="worker-1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(transport.patched_claim["claimed_by"], "worker-1")
        self.assertIn("registry_id", transport.get_params["select"])

    def test_claim_rows_returns_empty_without_patching_when_nothing_available(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            rows = client.claim_rows(limit=10, lease_minutes=30, worker_id="worker-1")

        self.assertEqual(rows, [])
        self.assertIsNone(transport.patched_claim)

    def test_mark_failure_increments_failure_count_per_row(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.mark_failure([{"registry_id": "1", "failure_count": 2}])

        self.assertEqual(transport.patch_calls[0]["json"]["failure_count"], 3)
        self.assertEqual(transport.patch_calls[0]["json"]["last_fetch_status"], "error")
        self.assertEqual(transport.patch_calls[0]["params"]["registry_id"], "eq.1")

    def test_mark_failure_patches_each_row_individually(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.mark_failure(
                [
                    {"registry_id": "1", "failure_count": 0},
                    {"registry_id": "2", "failure_count": 5},
                ]
            )

        self.assertEqual(len(transport.patch_calls), 2)
        self.assertEqual(transport.patch_calls[1]["json"]["failure_count"], 6)
        self.assertEqual(transport.patch_calls[1]["params"]["registry_id"], "eq.2")

    def test_update_console_uids_patches_each_row_with_its_own_value(self) -> None:
        transport = _FakeRegistryOpsTransport(claimable_rows=[])
        with patch("scripts.backfill_pricecharting_sets.httpx.Client") as client_class:
            client_class.return_value.__enter__.return_value = transport
            client = SupabaseRegistryOpsClient(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                timeout_seconds=1,
            )
            client.update_console_uids(
                [
                    {"registry_id": "1", "console_uid": "G1"},
                    {"registry_id": "2", "console_uid": "G2"},
                ]
            )

        self.assertEqual(len(transport.patch_calls), 2)
        self.assertEqual(transport.patch_calls[0]["json"], {"console_uid": "G1"})
        self.assertEqual(transport.patch_calls[0]["params"]["registry_id"], "eq.1")
        self.assertEqual(transport.patch_calls[1]["json"], {"console_uid": "G2"})


class _FakeCatalogClient:
    def __init__(self, *, raise_on: str | None = None, error: Exception | None = None) -> None:
        self.raise_on = raise_on
        self.error = error
        self.history_calls = 0
        self.upsert_calls = 0

    def sync_scd2_history_rows(self, rows, *, batch_size):
        self.history_calls += 1
        if self.raise_on == "history":
            raise self.error
        return len(rows)

    def upsert_rows(self, rows, *, batch_size):
        self.upsert_calls += 1
        if self.raise_on == "upsert":
            raise self.error
        return len(rows)


class _ExplodingRegistryClient:
    def update_console_uids(self, rows):
        raise AssertionError("registry client must not be called")


class _RecordingRegistryClient:
    def __init__(self) -> None:
        self.updated_batches: list[list[dict]] = []

    def update_console_uids(self, rows):
        self.updated_batches.append(list(rows))


class _FakeRegistryOpsResponse:
    def __init__(self, payload=None) -> None:
        self._payload = [] if payload is None else payload
        self.status_code = 200
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeRegistryOpsTransport:
    def __init__(self, *, claimable_rows: list[dict]) -> None:
        self.claimable_rows = claimable_rows
        self.get_params: dict = {}
        self.patch_calls: list[dict] = []

    @property
    def patched_claim(self):
        return self.patch_calls[-1]["json"] if self.patch_calls else None

    def get(self, url: str, **kwargs):
        self.get_params = kwargs.get("params", {})
        return _FakeRegistryOpsResponse(self.claimable_rows)

    def patch(self, url: str, **kwargs):
        self.patch_calls.append({"params": kwargs.get("params", {}), "json": kwargs.get("json", {})})
        return _FakeRegistryOpsResponse()


if __name__ == "__main__":
    unittest.main()
