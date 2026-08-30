"""Best-effort run ledger for cron scripts (ops_cron_runs).

Wraps a cron script's main() so every run writes a start row, then a
finish/fail update carrying the structured JSON summary the script
already produces. The admin portal's Scheduled-jobs page reads these
rows -- before this existed that page was a hardcoded mock, and the only
record of a run was Render's rotating log stream.

Design constraints, in order:

  1. NEVER break the job. Every write here is wrapped: if Supabase is
     down, the env is missing, or the insert fails, the script runs
     exactly as it did before this module existed. Observability that
     can take down the thing it observes is worse than none.
  2. Zero-friction adoption. A script opts in by wrapping its entry
     point:  raise SystemExit(run_with_recorder("job-name", main))
     and, where it builds its summary dict, calling report_summary(d).
     Scripts that never call report_summary still get status/duration.
  3. Failures are captured twice on purpose: the run row flips to
     'failed' with the traceback tail (so the run history shows it in
     place), AND an ops_error_events row is written (so the error feed
     groups it by fingerprint alongside API errors).
"""

import hashlib
import json
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

_STACK_CAP = 4000
_summary: dict[str, Any] | None = None


def report_summary(summary: dict[str, Any]) -> None:
    """Called by the wrapped script wherever it builds its result dict."""
    global _summary
    _summary = summary


def run_with_recorder(job_name: str, main: Callable[[], int]) -> int:
    recorder = _Recorder(job_name)
    recorder.start()
    try:
        exit_code = main()
    except BaseException as error:  # noqa: BLE001 -- re-raised below
        # SystemExit(0)/SystemExit(None) is a clean exit (argparse --help,
        # an early return through sys.exit()), not a failure.
        if isinstance(error, SystemExit) and (error.code in (0, None)):
            recorder.finish_succeeded(0)
        else:
            recorder.finish_failed(error)
        raise
    recorder.finish_succeeded(exit_code)
    return exit_code


class _Recorder:
    def __init__(self, job_name: str) -> None:
        self.job_name = job_name
        self.run_id: str | None = None
        self.supabase_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
        self.service_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    @property
    def _configured(self) -> bool:
        # Same guard as app/services/ops/observability.py: a test run must
        # never write into the production run ledger. pytest sets
        # PYTEST_CURRENT_TEST for every test, so this needs no
        # cooperation from individual tests.
        if os.getenv("PYTEST_CURRENT_TEST") or os.getenv(
            "OPS_OBSERVABILITY_DISABLED", ""
        ).strip().lower() in {"1", "true", "yes", "on"}:
            return False
        return bool(self.supabase_url and self.service_key)

    def start(self) -> None:
        if not self._configured:
            return
        try:
            payload = {
                "job_name": self.job_name,
                "status": "running",
                "context": {
                    "renderService": os.getenv("RENDER_SERVICE_NAME"),
                    "gitCommit": os.getenv("RENDER_GIT_COMMIT"),
                },
            }
            rows = self._request("POST", "/rest/v1/ops_cron_runs", payload,
                                 headers={"Prefer": "return=representation"})
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                self.run_id = rows[0].get("run_id")
        except Exception:  # noqa: BLE001 -- constraint 1
            self.run_id = None

    def finish_succeeded(self, exit_code: int | None) -> None:
        self._finish("succeeded", summary=_summary, error=None, exit_code=exit_code)

    def finish_failed(self, error: BaseException) -> None:
        stack = "".join(traceback.format_exception(error))[-_STACK_CAP:]
        self._finish("failed", summary=_summary, error=stack, exit_code=None)
        self._record_error_event(error, stack)

    def _finish(self, status: str, *, summary: dict[str, Any] | None,
                error: str | None, exit_code: int | None) -> None:
        if not self._configured or not self.run_id:
            return
        try:
            merged_summary = dict(summary) if summary else None
            if exit_code not in (None, 0):
                merged_summary = merged_summary or {}
                merged_summary["exitCode"] = exit_code
            self._request(
                "PATCH", f"/rest/v1/ops_cron_runs?run_id=eq.{self.run_id}",
                {
                    "status": status,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "summary": merged_summary,
                    "error": error,
                },
                headers={"Prefer": "return=minimal"},
            )
        except Exception:  # noqa: BLE001 -- constraint 1
            pass

    def _record_error_event(self, error: BaseException, stack: str) -> None:
        if not self._configured:
            return
        try:
            error_class = type(error).__name__
            fingerprint = hashlib.md5(
                f"cron|{self.job_name}|{error_class}".encode()
            ).hexdigest()
            self._request(
                "POST", "/rest/v1/ops_error_events",
                {
                    "source": "cron",
                    "job_name": self.job_name,
                    "error_class": error_class,
                    "message": str(error)[:2000],
                    "stack": stack,
                    "fingerprint": fingerprint,
                    "context": {"renderService": os.getenv("RENDER_SERVICE_NAME")},
                },
                headers={"Prefer": "return=minimal"},
            )
        except Exception:  # noqa: BLE001 -- constraint 1
            pass

    def _request(self, method: str, path: str, payload: dict[str, Any],
                 *, headers: dict[str, str]) -> Any:
        response = httpx.request(
            method,
            f"{self.supabase_url}{path}",
            json=payload,
            headers={
                "apikey": self.service_key,
                "Authorization": f"Bearer {self.service_key}",
                "Content-Type": "application/json",
                **headers,
            },
            timeout=10,
        )
        response.raise_for_status()
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return None


def dump_and_report(summary: dict[str, Any], **dumps_kwargs: Any) -> str:
    """Drop-in for json.dumps at a script's summary-print site: records the
    summary for the run ledger and returns the same JSON string the script
    was already printing."""
    report_summary(summary)
    dumps_kwargs.setdefault("indent", 2)
    return json.dumps(summary, **dumps_kwargs)
