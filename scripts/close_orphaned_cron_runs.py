"""Close ops_cron_runs rows left in 'running' by a suspended or killed job.

The recorder writes a 'running' row at start and flips it at the end. A job
suspended mid-run, or killed by a deploy, never reaches the flip -- so the row
sits in 'running' forever. Four have been sitting since 2026-09-03.

They are not merely untidy. Every freshness check that asks "is anything
unfinished?" counts them, so a real stuck job is indistinguishable from this
residue -- which is how a genuine alarm gets ignored. The gate before the CSV
Storage switch had to name all four by hand each time to say "not a new
alarm"; that is the cost being removed.

Closed as 'failed', not deleted, and not 'succeeded':

  * deleted would erase the evidence that a run started and never finished,
    which is the only record that those jobs were interrupted;
  * 'succeeded' would be a lie, and would make the board show work that never
    completed as work that did;
  * ops_cron_runs_status_check permits only running / succeeded / failed, so
    'cancelled' is not available even though it describes this better. The
    error text carries the distinction instead.

Selection is by AGE, not by a hardcoded list of ids. A list would close these
four and leave the next four to be found by hand. The threshold is what makes
it safe: the longest-running job here takes ~41 minutes, so six hours cannot
reach a live run.

Default is dry-run. Pass --commit to write.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

# The longest job measured (~41 min for the five-CSV refresh) times ~9. A run
# still 'running' after six hours is not slow; its process is gone.
DEFAULT_MIN_AGE_HOURS = 6

CLOSE_STATUS = "failed"
CLOSE_ERROR = (
    "run never reported a result; closed by close_orphaned_cron_runs after "
    "exceeding the stale threshold. The job was suspended or killed mid-run -- "
    "this is not a failure the job itself detected."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually write; without it nothing changes")
    parser.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
                        help="only close runs older than this (default 6). The "
                             "guard against closing a job that is still working.")
    parser.add_argument("--job", default=None,
                        help="restrict to one job_name (default: all)")
    return parser.parse_args(argv)


def stale_cutoff(*, min_age_hours: float, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(hours=min_age_hours)


def is_orphaned(run: dict, *, cutoff: datetime) -> bool:
    """A row is orphaned if it never finished and started before the cutoff.

    Both conditions, always. Age alone would close a long-running job that is
    still working; 'running' alone would close one that started a minute ago.
    """
    if run.get("finished_at") is not None:
        return False
    if run.get("status") != "running":
        return False
    started = run.get("started_at")
    if not started:
        return False
    moment = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    return moment < cutoff


def _headers(key: str, **extra: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", **extra}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

    cutoff = stale_cutoff(min_age_hours=args.min_age_hours)
    params = {"select": "run_id,job_name,status,started_at,finished_at",
              "finished_at": "is.null", "order": "started_at.asc"}
    if args.job:
        params["job_name"] = f"eq.{args.job}"

    with httpx.Client(timeout=60) as client:
        response = client.get(f"{url}/rest/v1/ops_cron_runs",
                              params=params, headers=_headers(key))
        response.raise_for_status()
        unfinished = [row for row in response.json() if isinstance(row, dict)]
        orphaned = [row for row in unfinished if is_orphaned(row, cutoff=cutoff)]

        mode = "COMMIT" if args.commit else "DRY-RUN (nothing will be written)"
        print(f"{mode}: {len(unfinished)} unfinished row(s), "
              f"{len(orphaned)} older than {args.min_age_hours}h", flush=True)
        for row in unfinished:
            mark = "CLOSE" if row in orphaned else "leave"
            print(f"  {mark:5} {row['job_name']:30} {str(row['started_at'])[:19]} "
                  f"{row['run_id']}", flush=True)

        if not orphaned:
            print("nothing to close")
            return 0
        if not args.commit:
            print("dry run: pass --commit to write")
            return 0

        finished = datetime.now(timezone.utc).isoformat()
        for row in orphaned:
            patch = client.patch(
                f"{url}/rest/v1/ops_cron_runs",
                params={"run_id": f"eq.{row['run_id']}", "status": "eq.running"},
                headers=_headers(key, **{"Content-Type": "application/json",
                                         "Prefer": "return=minimal"}),
                json={"status": CLOSE_STATUS, "finished_at": finished,
                      "error": CLOSE_ERROR},
            )
            patch.raise_for_status()
            print(f"  closed {row['job_name']} {row['run_id']}", flush=True)
    print(f"closed {len(orphaned)} orphaned run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
