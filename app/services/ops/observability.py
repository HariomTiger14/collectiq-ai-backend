"""API-side observability: error capture + run ledger for HTTP-triggered jobs.

Two consumers:

  * app/main.py registers record_unhandled_error() from an exception
    handler, so every unhandled 5xx writes an ops_error_events row,
    fingerprinted by (source, route, error class) -- message deliberately
    excluded from the fingerprint so ids/values embedded in messages
    can't shatter one bug into a thousand "distinct" issues.
  * The five cron jobs that run as `curl` against admin endpoints
    (fx-rates, batch-reprice, price-alerts, promote-scan-derived,
    match-portfolio) have no Python process of their own to wrap with
    scripts/_ops_run_recorder.py -- the work happens inside the API. The
    recorded_admin_job() decorator gives their handlers the same
    ops_cron_runs rows the script crons get, so the portal's
    Scheduled-jobs page covers all thirteen jobs from one table.

The same prime constraint as the script-side recorder applies: a write
failure here must NEVER fail the request or the job. Every Supabase call
is best-effort.
"""

from __future__ import annotations

import functools
import hashlib
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from app.core.config import settings

_STACK_CAP = 4000


def record_unhandled_error(*, route: str, error: BaseException) -> None:
    stack = "".join(traceback.format_exception(error))[-_STACK_CAP:]
    error_class = type(error).__name__
    fingerprint = hashlib.md5(f"api|{route}|{error_class}".encode()).hexdigest()
    _post(
        "/rest/v1/ops_error_events",
        {
            "source": "api",
            "job_name": route,
            "error_class": error_class,
            "message": str(error)[:2000],
            "stack": stack,
            "fingerprint": fingerprint,
        },
        prefer="return=minimal",
    )


def recorded_admin_job(job_name: str) -> Callable:
    """Wraps an admin endpoint handler in an ops_cron_runs row."""

    def decorate(handler: Callable[..., Awaitable[Any]] | Callable[..., Any]) -> Callable:
        import inspect

        if inspect.iscoroutinefunction(handler):
            @functools.wraps(handler)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                run_id = _start_run(job_name)
                try:
                    result = await handler(*args, **kwargs)
                except BaseException as error:
                    _finish_run(run_id, "failed", error=error)
                    raise
                _finish_run(run_id, "succeeded", summary=_summary_from(result))
                return result
            return async_wrapper

        @functools.wraps(handler)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            run_id = _start_run(job_name)
            try:
                result = handler(*args, **kwargs)
            except BaseException as error:
                _finish_run(run_id, "failed", error=error)
                raise
            _finish_run(run_id, "succeeded", summary=_summary_from(result))
            return result
        return sync_wrapper

    return decorate


def _summary_from(result: Any) -> dict[str, Any] | None:
    # Endpoint responses are small JSON dicts already; keep the scalars
    # (counts, flags) and drop nested payloads so the ledger row stays
    # readable rather than mirroring whole responses.
    if not isinstance(result, dict):
        return None
    return {
        key: value
        for key, value in result.items()
        if isinstance(value, (int, float, bool, str)) and len(str(value)) <= 200
    } or None


def _start_run(job_name: str) -> str | None:
    rows = _post(
        "/rest/v1/ops_cron_runs",
        {"job_name": job_name, "status": "running", "context": {"via": "api"}},
        prefer="return=representation",
    )
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0].get("run_id")
    return None


def _finish_run(
    run_id: str | None,
    status: str,
    *,
    summary: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    if not run_id:
        return
    _request(
        "PATCH",
        f"/rest/v1/ops_cron_runs?run_id=eq.{run_id}",
        {
            "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "error": "".join(traceback.format_exception(error))[-_STACK_CAP:] if error else None,
        },
        prefer="return=minimal",
    )


def _post(path: str, payload: dict[str, Any], *, prefer: str) -> Any:
    return _request("POST", path, payload, prefer=prefer)


def _observability_writes_disabled() -> bool:
    """True when this process must not write to the ops tables.

    Tests exercise the admin endpoints through TestClient, which runs the
    real recorded_admin_job() decorator; with SUPABASE_* present in a
    developer's .env that wrote straight into PRODUCTION ops_cron_runs.
    Found live 2026-08-30: the Scheduled-jobs board showed fx-rates-refresh
    running "3 min ago" with a failure whose traceback was a local path
    and whose error message was the fixture string "boom" -- six of the
    fifteen recorded runs for that job were test artifacts, in the very
    table used to diagnose whether jobs are healthy.

    pytest sets PYTEST_CURRENT_TEST for the duration of every test, which
    makes this self-enforcing rather than something each test must
    remember to patch.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return os.getenv("OPS_OBSERVABILITY_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _request(method: str, path: str, payload: dict[str, Any], *, prefer: str) -> Any:
    if _observability_writes_disabled():
        return None
    supabase_url = (settings.supabase_url or "").strip().rstrip("/")
    service_key = (settings.supabase_service_role_key or "").strip()
    if not supabase_url or not service_key:
        return None
    try:
        response = httpx.request(
            method,
            f"{supabase_url}{path}",
            json=payload,
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
                "Prefer": prefer,
            },
            timeout=5,
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()
    except Exception:  # noqa: BLE001 -- observability must never break the request
        return None
