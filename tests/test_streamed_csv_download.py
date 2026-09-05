"""The streaming CSV path must be byte-for-byte equivalent to the in-memory
one, and must never leave a temp file behind.

A 300-set download-custom batch is ~228,000 rows / ~28 MB. Holding that as
buffered bytes AND as a str AND as a StringIO copy measured a 229 MB peak on
a 256 MB container, which is what capped batch size at 100. These tests pin
the two properties that make the file-based path safe to substitute:
identical parsed output, and no leaked files on any path out.
"""

import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from scripts.backfill_pricecharting_sets import (
    CsvDownload,
    _Counter,
    cleanup_csv_downloads,
    fetch_batch_csv,
    fetch_batch_csv_file,
)
from scripts.import_pricecharting_catalog import (
    iter_rows_from_file,
    load_rows_from_text,
)

MULTI_SET_CSV = (
    "id,console-name,product-name,loose-price\n"
    "1,Baseball Cards 2021 Panini Mosaic,\"Judge, Aaron #135\",$0.25\n"
    "2,Baseball Cards 2021 Panini Mosaic,Ohtani ⚾ #150,$12.00\n"
    "3,Football Cards 2022 Prizm,\"Smith, A. \"\"Ace\"\" #7\",$1.50\n"
)


def _tmp(text: str) -> Path:
    handle, name = tempfile.mkstemp(suffix=".csv")
    path = Path(name)
    with open(handle, "w", encoding="utf-8", newline="") as out:
        out.write(text)
    return path


class _FakeStream:
    """Minimal httpx.Client.stream() stand-in."""

    def __init__(self, body: bytes, status: int = 200, encoding: str = "utf-8"):
        self._body, self.status_code, self.encoding = body, status, encoding
        self.headers = {"content-type": "text/csv"}
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        self._read = True
        return self._body

    def iter_bytes(self):
        # Chunked deliberately: the point of the change is that the caller
        # never holds the whole body.
        for i in range(0, len(self._body), 8):
            yield self._body[i : i + 8]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(self.status_code),
            )


def _client(body="", status=200, error=None):
    client = mock.MagicMock(spec=httpx.Client)
    if error is not None:
        client.stream.side_effect = error
    else:
        client.stream.return_value = _FakeStream(body.encode("utf-8"), status)
    return client


def _fetch(client, **kw):
    return fetch_batch_csv_file(
        client,
        base_url="https://example.test",
        token="SECRET-TOKEN",
        console_uids=["G1", "G2"],
        **kw,
    )


class ParserEquivalenceTest(unittest.TestCase):
    def _assert_same(self, text):
        path = _tmp(text)
        try:
            self.assertEqual(list(iter_rows_from_file(path)), load_rows_from_text(text))
        finally:
            path.unlink(missing_ok=True)

    def test_multiple_sets_quoted_commas_and_unicode(self):
        self._assert_same(MULTI_SET_CSV)

    def test_empty_body(self):
        self._assert_same("")

    def test_headers_only(self):
        self._assert_same("id,product-name\n")

    def test_ragged_rows_behave_the_same_as_the_text_parser(self):
        """csv.DictReader pads short rows with None and collects extras under
        the restkey. Whatever it does, both paths must do it identically."""
        self._assert_same("id,name,price\n1,only-two\n2,a,b,c,d\n")

    def test_crlf_and_trailing_newline_variants(self):
        self._assert_same("id,name\r\n1,a\r\n2,b\r\n")
        self._assert_same("id,name\n1,a")


class StreamingFetchTest(unittest.TestCase):
    def test_streamed_body_matches_the_in_memory_fetch_exactly(self):
        streamed = _fetch(_client(MULTI_SET_CSV))
        self.assertIsNotNone(streamed)
        try:
            on_disk = streamed.path.read_text(encoding="utf-8")
        finally:
            cleanup_csv_downloads([streamed])

        text_client = mock.MagicMock(spec=httpx.Client)
        text_client.get.return_value = httpx.Response(
            200, text=MULTI_SET_CSV, request=httpx.Request("GET", "https://example.test")
        )
        in_memory = fetch_batch_csv(
            text_client, base_url="https://example.test", token="t", console_uids=["G1"]
        )
        self.assertEqual(on_disk, in_memory)
        self.assertEqual(
            list(csv.DictReader(io.StringIO(on_disk))),
            list(csv.DictReader(io.StringIO(in_memory))),
        )

    def test_success_leaves_exactly_one_file_which_cleanup_removes(self):
        download = _fetch(_client(MULTI_SET_CSV))
        self.assertTrue(download.path.exists())
        cleanup_csv_downloads([download])
        self.assertFalse(download.path.exists())

    def test_cleanup_is_idempotent_and_tolerates_none(self):
        download = _fetch(_client(MULTI_SET_CSV))
        cleanup_csv_downloads([download, None])
        cleanup_csv_downloads([download, None])  # must not raise
        self.assertFalse(download.path.exists())

    def test_http_error_returns_none_records_status_and_leaves_no_file(self):
        before = set(Path(tempfile.gettempdir()).glob("pricecharting-batch-*.csv"))
        sink, rate, blocked = [], _Counter(), _Counter()
        result = _fetch(
            _client("nope", status=429),
            status_sink=sink,
            rate_limit_counter=rate,
            blocked_counter=blocked,
        )
        self.assertIsNone(result)
        self.assertEqual(sink, [429])
        self.assertEqual(rate.value, 1)
        after = set(Path(tempfile.gettempdir()).glob("pricecharting-batch-*.csv"))
        self.assertEqual(after - before, set())

    def test_403_feeds_the_blocked_counter_like_the_in_memory_path(self):
        sink, rate, blocked = [], _Counter(), _Counter()
        self.assertIsNone(
            _fetch(
                _client("denied", status=403),
                status_sink=sink,
                rate_limit_counter=rate,
                blocked_counter=blocked,
            )
        )
        self.assertEqual(blocked.value, 1)
        self.assertEqual(rate.value, 0)

    def test_transport_error_returns_none_and_leaves_no_file(self):
        before = set(Path(tempfile.gettempdir()).glob("pricecharting-batch-*.csv"))
        result = _fetch(_client(error=httpx.ConnectError("boom")))
        self.assertIsNone(result)
        after = set(Path(tempfile.gettempdir()).glob("pricecharting-batch-*.csv"))
        self.assertEqual(after - before, set())

    def test_the_token_never_reaches_stdout(self):
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _fetch(_client("nope", status=500))
        self.assertNotIn("SECRET-TOKEN", buffer.getvalue())


class IsolationCleanupTest(unittest.TestCase):
    def test_a_throttled_right_half_does_not_strand_the_left_halfs_files(self):
        """Isolation recurses; if the right half aborts, the left half's
        downloads are already on disk and nothing upstream will ever see
        them."""
        from scripts.refresh_sportscardspro_rotation import (
            _isolate_failed_batch,
            _ThrottleAbort,
        )

        made: list[Path] = []

        def fake_fetch(http, *, base_url, token, console_uids, rate_limit_counter,
                       blocked_counter, status_sink=None):
            # Order matters: the full batch must fail with a splittable
            # status FIRST, so isolation actually recurses. Only then does
            # the right half throttle, leaving the left half's file on disk.
            if len(console_uids) > 2:          # the whole batch, 503 -> split
                if status_sink is not None:
                    status_sink.append(503)
                return None
            if "G4" in console_uids:           # the right half, throttled
                if status_sink is not None:
                    status_sink.append(429)
                return None
            path = _tmp("id,name\n1,a\n")
            made.append(path)
            return CsvDownload(path, "utf-8")

        rows = [
            {"registry_id": f"r{i}", "console_uid": f"G{i}", "set_name": f"S{i}"}
            for i in range(1, 5)
        ]
        with mock.patch(
            "scripts.refresh_sportscardspro_rotation.fetch_batch_csv_file", fake_fetch
        ):
            with self.assertRaises(_ThrottleAbort):
                _isolate_failed_batch(
                    mock.MagicMock(spec=httpx.Client),
                    base_url="https://example.test",
                    token="t",
                    rows=rows,
                    sleep_seconds=0,
                    rate_limit_counter=_Counter(),
                    blocked_counter=_Counter(),
                    budget=[32],
                )
        self.assertTrue(made, "the left half should have downloaded something")
        for path in made:
            self.assertFalse(path.exists(), f"leaked {path}")


if __name__ == "__main__":
    unittest.main()
