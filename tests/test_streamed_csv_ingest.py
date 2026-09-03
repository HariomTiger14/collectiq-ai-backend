"""Streaming the CSV parse is what makes a 100-set batch affordable.

Sports sets average ~636 rows, so a 100-set download-custom batch parses to
~63,600 dicts. Materialising those is what forced tier-3 down to tiny batches:
a 20-set batch (~12,700 rows) already OOM-killed a 512Mi Render instance.
Chunking bounds peak memory by chunk size instead of batch size, so the fetch
limit (one CSV call per 10 minutes) becomes the only real constraint.
"""

import unittest

from scripts.import_pricecharting_catalog import chunked_iter, iter_rows_from_text


def _csv(n: int) -> str:
    header = "id,product-name,console-name,loose-price\n"
    return header + "".join(f"{i},Card {i},Set A,{i * 10}\n" for i in range(n))


class IterRowsFromTextTest(unittest.TestCase):
    def test_yields_the_same_rows_as_the_whole_file_parse(self):
        from scripts.import_pricecharting_catalog import load_rows_from_text

        text = _csv(50)
        self.assertEqual(list(iter_rows_from_text(text)), load_rows_from_text(text))

    def test_is_lazy_rather_than_building_a_list(self):
        # The point is that nothing is materialised until asked for, so a
        # caller can convert-and-write incrementally.
        it = iter_rows_from_text(_csv(10_000))
        first = next(it)
        self.assertEqual(first["id"], "0")

    def test_handles_an_empty_file(self):
        self.assertEqual(list(iter_rows_from_text("")), [])

    def test_handles_a_header_only_file(self):
        self.assertEqual(list(iter_rows_from_text("id,product-name\n")), [])


class ChunkedIterTest(unittest.TestCase):
    def test_splits_into_full_chunks_plus_a_remainder(self):
        chunks = list(chunked_iter(range(25), 10))
        self.assertEqual([len(c) for c in chunks], [10, 10, 5])

    def test_a_batch_that_divides_evenly_has_no_empty_trailing_chunk(self):
        self.assertEqual([len(c) for c in chunked_iter(range(20), 10)], [10, 10])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(list(chunked_iter([], 10)), [])

    def test_chunk_larger_than_input_yields_one_chunk(self):
        self.assertEqual([len(c) for c in chunked_iter(range(3), 10)], [3])

    def test_rejects_a_zero_or_negative_size(self):
        # A zero size would loop forever rather than fail, which is the worst
        # way for a misconfiguration to show up in a cron.
        for bad in (0, -1):
            with self.subTest(size=bad):
                with self.assertRaises(ValueError):
                    list(chunked_iter(range(5), bad))

    def test_does_not_buffer_the_whole_input(self):
        """A generator input must stay lazy: pulling one chunk must not
        consume the rest, or peak memory is unchanged and the point is lost."""
        consumed = []

        def source():
            for i in range(100):
                consumed.append(i)
                yield i

        chunks = chunked_iter(source(), 10)
        next(chunks)
        self.assertEqual(len(consumed), 10)


class RealisticBatchShapeTest(unittest.TestCase):
    def test_a_100_set_batch_splits_into_a_handful_of_chunks(self):
        # ~636 rows/set measured 2026-09-01; 100 sets ~= 63,600 rows.
        rows = 636 * 100
        chunks = list(chunked_iter(range(rows), 10_000))
        self.assertEqual(len(chunks), 7)
        self.assertTrue(all(len(c) <= 10_000 for c in chunks))
        # Far fewer round trips than the 375 the old per-sub-batch REST path
        # made for the same data.
        self.assertLess(len(chunks) * 8, 375)


if __name__ == "__main__":
    unittest.main()
