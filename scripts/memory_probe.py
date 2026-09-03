"""Measure whether a 100-set tier-3 batch fits in THIS instance's memory.

Run it in a Render shell. Laptop measurements are not evidence here: a dev
machine has 48GB and no cgroup limit, so the only way to know whether batch
100 survives on a 512Mi plan is to measure on the plan.

Reports the container's real memory limit, then peaks for the whole-batch
parse (what the rotation did before 2026-09-02) and the chunked parse that
replaced it, at a realistic 63,600 rows -- 100 sets x the 636 rows/set
measured on 2026-09-01.

Makes no network calls and writes nothing.
"""

import os
import tracemalloc

from scripts.import_pricecharting_catalog import (
    chunked_iter,
    iter_rows_from_text,
    load_rows_from_text,
    to_catalog_row,
)

ROWS = 63_600
STAMP = "2026-09-02T00:00:00+00:00"
HEADER = "id,product-name,console-name,loose-price,cib-price,new-price,genre,release-date\n"


def _cgroup_limit_bytes() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.max",                    # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ):
        try:
            raw = open(path).read().strip()
        except OSError:
            continue
        if raw and raw != "max":
            value = int(raw)
            # v1 reports an absurd sentinel when unlimited.
            if value < (1 << 62):
                return value
    return None


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> int:
    limit = _cgroup_limit_bytes()
    print(f"instance   : {os.getenv('RENDER_SERVICE_NAME', 'unknown')}")
    print(f"mem limit  : {_mb(limit) if limit else 'unlimited / not reported'}")

    csv_text = HEADER + "".join(
        f"{i},Some Long Card Name Number {i} [Refractor] #{i},"
        f"Baseball Cards 2021 Panini Mosaic,{i},{i},{i},Baseball Card,2021-06-01\n"
        for i in range(ROWS)
    )
    print(f"csv text   : {_mb(len(csv_text))} for {ROWS:,} rows (~100 sets)")

    def gen():
        for raw in iter_rows_from_text(csv_text):
            row = to_catalog_row(raw, "probe", STAMP)
            if row is not None:
                yield row

    # Chunked FIRST, smallest first, printed as we go.
    #
    # The first version of this probe measured the whole-batch parse first and
    # was OOM-killed on a 256Mi instance before reaching any chunked figure --
    # so it proved the old path dies here while telling us nothing about the
    # path that replaced it. Measure the safe cases first, flush each result,
    # and leave the one that might kill the process until last.
    print()
    results = {}
    for chunk_rows in (5_000, 10_000, 20_000):
        tracemalloc.start()
        for chunk in chunked_iter(gen(), chunk_rows):
            del chunk
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results[chunk_rows] = peak
        verdict = ""
        if limit:
            verdict = "  OK" if peak < limit * 0.5 else "  TIGHT"
        print(f"  chunked @ {chunk_rows:>6,} : {_mb(peak)}{verdict}", flush=True)

    print(flush=True)
    print("  now measuring the OLD whole-batch path -- if this is the last line", flush=True)
    print("  you see, it was OOM-killed, which is itself the result.", flush=True)
    tracemalloc.start()
    whole = [
        r
        for r in (to_catalog_row(x, "probe", STAMP) for x in load_rows_from_text(csv_text))
        if r is not None
    ]
    _, peak_whole = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del whole
    print(f"  whole batch (old)  : {_mb(peak_whole)}", flush=True)

    if limit:
        best = min(results.values())
        headroom = limit - best
        print()
        print(f"  headroom at best chunking   : {_mb(headroom)} of {_mb(limit)}")
        # tracemalloc counts Python allocations only -- the interpreter,
        # httpx and the Postgres driver sit on top, so treat anything under
        # roughly half the limit as the real danger zone.
        if best > limit * 0.5:
            print("  VERDICT: TIGHT -- batch 100 is risky here, lower --batch-size")
        else:
            print("  VERDICT: OK -- batch 100 fits with room for the runtime on top")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
