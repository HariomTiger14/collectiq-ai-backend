"""Measure whether a 100-set tier-3 batch fits in THIS instance's memory.

Run it in a Render shell. Laptop measurements are not evidence: a dev machine
has 48GB and no cgroup limit, so the only place the question can be answered
is on the plan.

Measures REAL process memory (ru_maxrss), not tracemalloc. The first two
versions of this probe used tracemalloc and were OOM-killed on a 256Mi
instance before producing any figure -- tracemalloc records a traceback per
allocation, so its own bookkeeping cost more than the workload it was
measuring. Measuring memory with a tool that doubles memory does not work on
the box where memory is the question.

Each scenario runs in a FRESH SUBPROCESS, because ru_maxrss is a high-water
mark that never falls: measuring several scenarios in one process would report
the largest for all of them.

Makes no network calls and writes nothing.

    python -m scripts.memory_probe
"""

import argparse
import os
import resource
import subprocess
import sys

ROWS = 63_600  # 100 sets x the 636 rows/set measured 2026-09-01
STAMP = "2026-09-02T00:00:00+00:00"
HEADER = "id,product-name,console-name,loose-price,cib-price,new-price,genre,release-date\n"


def _cgroup_limit_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path).read().strip()
        except OSError:
            continue
        if raw and raw != "max":
            value = int(raw)
            if value < (1 << 62):
                return value
    return None


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _peak_rss_bytes() -> int:
    # Linux reports kilobytes, macOS bytes.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def _build_csv() -> str:
    return HEADER + "".join(
        f"{i},Some Long Card Name Number {i} [Refractor] #{i},"
        f"Baseball Cards 2021 Panini Mosaic,{i},{i},{i},Baseball Card,2021-06-01\n"
        for i in range(ROWS)
    )


def _run_scenario(chunk_rows: int) -> int:
    """chunk_rows <= 0 means the old whole-batch path."""
    from scripts.import_pricecharting_catalog import (
        chunked_iter,
        iter_rows_from_text,
        load_rows_from_text,
        to_catalog_row,
    )

    csv_text = _build_csv()
    if chunk_rows <= 0:
        rows = [
            r
            for r in (to_catalog_row(x, "probe", STAMP) for x in load_rows_from_text(csv_text))
            if r is not None
        ]
        count = len(rows)
        del rows
    else:
        def gen():
            for raw in iter_rows_from_text(csv_text):
                row = to_catalog_row(raw, "probe", STAMP)
                if row is not None:
                    yield row

        count = 0
        for chunk in chunked_iter(gen(), chunk_rows):
            count += len(chunk)
            del chunk
    print(f"__PEAK__ {_peak_rss_bytes()} {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=int, help="internal: chunk size, <=0 for whole batch")
    args = parser.parse_args()

    if args.scenario is not None:
        return _run_scenario(args.scenario)

    limit = _cgroup_limit_bytes()
    print(f"instance : {os.getenv('RENDER_SERVICE_NAME', 'unknown')}")
    print(f"mem limit: {_mb(limit) if limit else 'unlimited / not reported'}")
    print(f"workload : {ROWS:,} rows (~100 sets)", flush=True)
    print(flush=True)

    scenarios = [(5_000, "chunked @  5,000"), (10_000, "chunked @ 10,000"),
                 (20_000, "chunked @ 20,000"), (0, "whole batch (old)")]
    best = None
    for chunk_rows, label in scenarios:
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.memory_probe", "--scenario", str(chunk_rows)],
            capture_output=True, text=True,
        )
        line = next((l for l in proc.stdout.splitlines() if l.startswith("__PEAK__")), None)
        if line is None:
            # Killed by the OOM reaper, which is the result for that scenario.
            print(f"  {label} : OOM-KILLED (rc={proc.returncode})", flush=True)
            continue
        peak = int(line.split()[1])
        verdict = ""
        if limit:
            verdict = "   OK" if peak < limit * 0.6 else "   TIGHT"
        print(f"  {label} : {_mb(peak)}{verdict}", flush=True)
        if chunk_rows > 0 and (best is None or peak < best[1]):
            best = (chunk_rows, peak)

    if limit and best:
        chunk_rows, peak = best
        print()
        print(f"  best chunked: {chunk_rows:,} rows at {_mb(peak)}, "
              f"{_mb(limit - peak)} headroom of {_mb(limit)}")
        print("  VERDICT: batch 100 is " + ("VIABLE" if peak < limit * 0.6 else "TOO TIGHT"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
