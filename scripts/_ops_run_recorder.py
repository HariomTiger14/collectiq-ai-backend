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
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

_STACK_CAP = 4000
_summary: dict[str, Any] | None = None
# Set by run_with_recorder so a non-fatal DB failure deep in the write path
# can attach itself to the run that is currently in flight.
_active: "_Recorder | None" = None

# Postgres cancels a statement that exceeds statement_timeout with this
# SQLSTATE. PostgREST forwards it as an HTTP 500 whose JSON body carries the
# code, so it can be detected structurally rather than by matching prose.
SQLSTATE_STATEMENT_TIMEOUT = "57014"


def report_summary(summary: dict[str, Any]) -> None:
    """Called by the wrapped script wherever it builds its result dict."""
    global _summary
    _summary = summary


def run_with_recorder(job_name: str, main: Callable[[], int]) -> int:
    global _active
    recorder = _Recorder(job_name)
    _active = recorder
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


def sqlstate_of(body: str) -> str | None:
    """SQLSTATE from a PostgREST error body, read from the JSON `code` field.

    Structural rather than textual: PostgREST returns
    {"code":"57014","message":"canceling statement due to statement timeout"},
    so the code is authoritative and survives message wording changes. The
    prose fallback exists only for responses that are not JSON at all."""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    if "canceling statement due to statement timeout" in body:
        return SQLSTATE_STATEMENT_TIMEOUT
    return None


def build_db_failure_event(
    *,
    job_name: str,
    run_id: str | None,
    operation: str,
    row_count: int,
    sqlstate: str,
    status_code: int,
    body: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ops_error_events payload for one failed database write.

    Deliberately carries no catalogue rows: the diagnostic value is in WHICH
    operation failed, HOW MANY rows it was writing, and WHEN -- not in the
    contents. The message is scrubbed because PostgREST echoes the failing
    row in `details`, which for these tables includes product names."""
    fingerprint = hashlib.md5(
        f"db|{job_name}|{operation}|{sqlstate}".encode()
    ).hexdigest()
    return {
        "source": "cron",
        "job_name": job_name,
        "error_class": f"PostgresError{sqlstate}",
        "message": scrub_secrets(str(body))[:2000],
        "fingerprint": fingerprint,
        "context": {
            "runId": run_id,
            "operation": operation,
            "rowCount": row_count,
            "sqlstate": sqlstate,
            "httpStatus": status_code,
            "renderService": os.getenv("RENDER_SERVICE_NAME"),
            **(context or {}),
        },
    }


def record_db_timeout(
    *,
    operation: str,
    row_count: int,
    status_code: int,
    body: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """Record ONE ops_error_events row for a statement-timeout write failure.

    Called at the narrowest point -- inside the helper that owns the failing
    request -- so the exception can propagate through PartialCatalogWriteError
    and write_catalog_rows without any layer recording it again.

    Only 57014 is recorded. Every caught error becoming an event would turn
    the board into noise; this is the specific failure being diagnosed.
    Returns True when an event was written."""
    sqlstate = sqlstate_of(body)
    if sqlstate != SQLSTATE_STATEMENT_TIMEOUT:
        return False
    recorder = _active
    if recorder is None or not recorder._configured:
        return False
    try:
        recorder._request(
            "POST", "/rest/v1/ops_error_events",
            build_db_failure_event(
                job_name=recorder.job_name,
                run_id=recorder.run_id,
                operation=operation,
                row_count=row_count,
                sqlstate=sqlstate,
                status_code=status_code,
                body=body,
                context=context,
            ),
            headers={"Prefer": "return=minimal"},
        )
        return True
    except Exception:  # noqa: BLE001 -- observability must never break a job
        return False


# Credentials reach tracebacks through URLs: httpx puts the full request URL in
# HTTPStatusError, and the providers here authenticate with a query parameter
# rather than a header. A real example, ops_cron_runs 2026-08-29:
#
#   httpx.HTTPStatusError: Server error '503 Service Unavailable' for url
#   'https://www.pricecharting.com/price-guide/download-custom?t=<live token>'
#
# which stored a live API token in the ledger -- readable by anything with
# database access, and copied again into ops_error_events. Redacting here
# rather than per-script is deliberate: _redact_token() in
# backfill_pricecharting_sets.py covers only print() calls in that one file and
# needs the token passed in, so every other job leaked by default.
#
# Matches on the PARAMETER NAME rather than on known secret values, so it also
# covers credentials this module never sees.
_SECRET_QUERY_PARAMS = re.compile(
    r"([?&](?:t|key|token|api[-_]?key|apikey|access[-_]?token|password|secret)=)"
    r"""[^&\s"'>]+""",
    re.IGNORECASE,
)


def scrub_secrets(text: str) -> str:
    """Redact credential-bearing query parameters from arbitrary text."""
    if not text:
        return text
    return _SECRET_QUERY_PARAMS.sub(r"\1[REDACTED]", text)


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
        stack = scrub_secrets("".join(traceback.format_exception(error)))[-_STACK_CAP:]
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
