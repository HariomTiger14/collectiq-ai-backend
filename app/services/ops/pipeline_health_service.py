"""Merges the three observability sources into the portal's job board.

For each of the thirteen scheduled jobs: the latest ops_cron_runs rows
(what ran, how long, what it reported), the admin_pipeline_health() RPC's
data-freshness probes (is the OUTPUT moving -- the signal that catches
quota-exhaustion-style failures that never throw), and a static cadence
table (how often each job is supposed to produce something). Status is
the pessimistic merge:

    failed  -- the most recent finished run failed
    stale   -- data freshness exceeded the job's threshold, even if runs
               "succeed" (the KicksDB quota exhaustion looked exactly
               like this: clean runs, dead output)
    running -- a run is currently in flight and nothing is stale
    ok      -- fresh data, last run succeeded
    unknown -- no run recorded AND no freshness probe covers the job
               (expected until the instrumented crons have run once)

Thresholds are ~1.5x each job's cadence: one missed run is jitter, two
is a problem. They live here, not in the portal, so alerts and the UI
can never disagree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

_HOUR = 3600.0

# job key -> (label, schedule description, staleness threshold seconds,
#             freshness extractor name)
JOB_DEFINITIONS: list[dict[str, Any]] = [
    {"job": "pricecharting-csv-refresh", "label": "Catalog bulk refresh (5 CSV categories)",
     "schedule": "daily 14:30", "staleAfter": 36 * _HOUR, "freshness": "csv"},
    {"job": "completed-categories-refresh", "label": "Completed categories refresh (coins/comics/funko/lego/lorcana)",
     "schedule": "daily 04:45", "staleAfter": 36 * _HOUR, "freshness": "completed"},
    {"job": "tier3-sportscardspro-rotation", "label": "Tier-3 sports rotation (all sports cards)",
     "schedule": "hourly :10", "staleAfter": 3 * _HOUR, "freshness": "tier3"},
    {"job": "small-sets-refresh", "label": "Tier-1 small sets refresh",
     "schedule": "hourly :40", "staleAfter": 3 * _HOUR, "freshness": "tier1"},
    {"job": "tracked-items-refresh", "label": "Tier-2 tracked items refresh",
     "schedule": "hourly :20", "staleAfter": 3 * _HOUR, "freshness": None},
    {"job": "kicksdb-catalog-refresh", "label": "KicksDB sneakers refresh",
     "schedule": "daily 04:15", "staleAfter": 36 * _HOUR, "freshness": "kicksdb"},
    {"job": "pricecharting-sets-backfill", "label": "Set backfill (new sets)",
     "schedule": "every 15 min", "staleAfter": None, "freshness": "backfill"},
    {"job": "pricecharting-sets-discover", "label": "Set discovery (weekly crawl)",
     "schedule": "weekly Sun 03:00", "staleAfter": 8 * 24 * _HOUR, "freshness": None},
    {"job": "batch-reprice", "label": "Batch reprice (portfolio valuations)",
     "schedule": "daily 16:00", "staleAfter": 30 * _HOUR, "freshness": None},
    {"job": "price-alerts-run", "label": "Price alerts (evaluate + push)",
     "schedule": "every 6 h", "staleAfter": 9 * _HOUR, "freshness": None},
    {"job": "match-portfolio-catalog", "label": "Portfolio-catalog matching",
     "schedule": "hourly", "staleAfter": 3 * _HOUR, "freshness": None},
    {"job": "fx-rates-refresh", "label": "FX rates refresh",
     "schedule": "daily 03:00", "staleAfter": 30 * _HOUR, "freshness": "fx"},
    {"job": "promote-scan-derived", "label": "Promote scan-derived catalog rows",
     "schedule": "daily 17:00", "staleAfter": 30 * _HOUR, "freshness": None},
]

_CSV_SOURCES = ["video_games.csv", "pokemon.csv", "magic.csv", "yugioh.csv", "one_piece.csv"]


class AdminOpsObservabilityError(Exception):
    pass


class AdminOpsObservabilityService:
    def __init__(self, repository: "SupabaseOpsRepository | None" = None) -> None:
        self._repository = repository or SupabaseOpsRepository()

    def pipeline_health(self) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminOpsObservabilityError("Supabase configuration is missing.")
        freshness = self._repository.pipeline_freshness()
        latest_runs = self._repository.latest_runs_per_job()
        error_count_24h = self._repository.error_count_last_24h()
        now = datetime.now(timezone.utc)

        pipelines = []
        for definition in JOB_DEFINITIONS:
            job = definition["job"]
            run = latest_runs.get(job)
            fresh = _freshness_for(definition["freshness"], freshness)
            status = _status_for(definition, run, fresh, now)
            pipelines.append({
                "job": job,
                "label": definition["label"],
                "schedule": definition["schedule"],
                "status": status,
                "lastRun": run,
                "freshness": fresh,
            })
        return {
            "success": True,
            "generatedAt": now.isoformat(),
            "pipelines": pipelines,
            "errors24h": error_count_24h,
        }

    def runs(self, *, job: str | None, limit: int) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminOpsObservabilityError("Supabase configuration is missing.")
        rows = self._repository.list_runs(job=job, limit=limit)
        return {"success": True, "runs": rows}

    def errors(self, *, fingerprint: str | None, limit: int) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminOpsObservabilityError("Supabase configuration is missing.")
        if fingerprint:
            return {"success": True, "occurrences": self._repository.list_error_occurrences(fingerprint, limit=limit)}
        return {"success": True, "groups": self._repository.grouped_errors(limit=limit)}


def _freshness_for(kind: str | None, freshness: dict[str, Any]) -> dict[str, Any] | None:
    if not kind or not freshness:
        return None
    csv_sources = freshness.get("csvSources") or {}
    if kind == "csv":
        # imported_at alone is a misleading probe: live data showed
        # yugioh.csv with a 4-day-old imported_at while writing 5k+
        # history rows a day (the hash-diffed upsert only touches
        # imported_at on certain writes). A source counts as ACTIVE if
        # its history moved in the last 24h -- that's the output signal
        # -- with imported_at only as the displayed timestamp. One dead
        # source among five must flag the job, so inactiveSources drives
        # the staleness decision, not the newest of the five.
        per_source = {src: csv_sources.get(src) for src in _CSV_SOURCES}
        inactive = [
            src for src, row in per_source.items()
            if not ((_get(row, "historyRows24h") or 0) > 0)
        ]
        return {
            "latestActivityAt": _newest([_get(v, "latestImportedAt") for v in per_source.values()]),
            "inactiveSources": inactive,
            "perSource": per_source,
        }
    if kind == "completed":
        row = csv_sources.get("pricecharting-completed-category-refresh")
        return {
            "latestActivityAt": _get(row, "latestImportedAt"),
            "historyRows24h": _get(row, "historyRows24h"),
            "inactiveSources": [] if (_get(row, "historyRows24h") or 0) > 0 else ["pricecharting-completed-category-refresh"],
        }
    if kind == "tier3":
        row = freshness.get("tier3") or {}
        return {
            "latestActivityAt": row.get("latestStampAt"),
            "stampedLastHour": row.get("stampedLastHour"),
            "rotationProgress": f"{row.get('stampedTotal')}/{row.get('rotationSize')}",
        }
    if kind == "tier1":
        row = freshness.get("tier1") or {}
        return {"latestActivityAt": row.get("latestCheckAt")}
    if kind == "kicksdb":
        row = freshness.get("kicksdb") or {}
        return {
            "latestActivityAt": row.get("latestUpdatedAt"),
            "rowsTouched24h": row.get("rowsTouched24h"),
            "totalRows": row.get("totalRows"),
        }
    if kind == "backfill":
        row = freshness.get("backfillQueue") or {}
        return {"queueNeverFetched": row.get("neverFetched"), "queueFailed": row.get("failed")}
    if kind == "fx":
        row = freshness.get("fxRates") or {}
        return {"latestActivityAt": row.get("latestFetchedAt"), "latestRateDate": row.get("latestRateDate")}
    return None


def _status_for(
    definition: dict[str, Any],
    run: dict[str, Any] | None,
    fresh: dict[str, Any] | None,
    now: datetime,
) -> str:
    if run and run.get("status") == "failed":
        return "failed"
    stale_after = definition.get("staleAfter")
    # A job with per-source activity tracking is stale the moment any of
    # its sources stops producing, regardless of how fresh the others are.
    if fresh and fresh.get("inactiveSources"):
        return "stale"
    latest_activity = _parse(fresh.get("latestActivityAt")) if fresh else None
    if stale_after and latest_activity and (now - latest_activity).total_seconds() > stale_after and "inactiveSources" not in (fresh or {}):
        return "stale"
    # Jobs with no data-freshness probe fall back to the run ledger for
    # staleness -- meaningful only once the instrumented build has run.
    if stale_after and latest_activity is None and run:
        finished = _parse(run.get("finished_at"))
        if finished and (now - finished).total_seconds() > stale_after:
            return "stale"
    if run and run.get("status") == "running":
        return "running"
    if run or latest_activity:
        return "ok"
    return "unknown"


def _get(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _newest(values: list[Any]) -> Any:
    parsed = [v for v in values if v]
    return max(parsed) if parsed else None


def _oldest(values: list[Any]) -> Any:
    parsed = [v for v in values if v]
    return min(parsed) if parsed else None


class SupabaseOpsRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 20,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key if service_role_key is not None else settings.supabase_service_role_key
        ).strip()
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def pipeline_freshness(self) -> dict[str, Any]:
        payload = self._request("POST", "/rest/v1/rpc/admin_pipeline_health", json_payload={})
        return payload if isinstance(payload, dict) else {}

    def latest_runs_per_job(self) -> dict[str, dict[str, Any]]:
        # Newest 200 rows cover the latest run of every job at any
        # realistic cadence mix (hourly x 13 jobs = a full day of runs);
        # first row seen per job_name wins because of the DESC order.
        rows = self._request("GET", "/rest/v1/ops_cron_runs", params={
            "select": "job_name,run_id,started_at,finished_at,status,summary,error,context",
            "order": "started_at.desc",
            "limit": "200",
        })
        latest: dict[str, dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("job_name") not in latest:
                latest[row["job_name"]] = row
        return latest

    def list_runs(self, *, job: str | None, limit: int) -> list[dict[str, Any]]:
        params = {
            "select": "run_id,job_name,started_at,finished_at,status,summary,error,context",
            "order": "started_at.desc",
            "limit": str(max(1, min(limit, 200))),
        }
        if job:
            params["job_name"] = f"eq.{job}"
        rows = self._request("GET", "/rest/v1/ops_cron_runs", params=params)
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def grouped_errors(self, *, limit: int) -> list[dict[str, Any]]:
        # PostgREST can't aggregate, so grouping happens here over the
        # newest window of events. 500 events is plenty for an error FEED
        # (it spans weeks at any healthy error rate; during an incident
        # the newest 500 are exactly the ones that matter).
        rows = self._request("GET", "/rest/v1/ops_error_events", params={
            "select": "fingerprint,occurred_at,source,job_name,error_class,message",
            "order": "occurred_at.desc",
            "limit": "500",
        })
        groups: dict[str, dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            fp = row.get("fingerprint") or ""
            group = groups.setdefault(fp, {
                "fingerprint": fp,
                "source": row.get("source"),
                "jobName": row.get("job_name"),
                "errorClass": row.get("error_class"),
                "latestMessage": row.get("message"),
                "latestAt": row.get("occurred_at"),
                "count": 0,
            })
            group["count"] += 1
        ordered = sorted(groups.values(), key=lambda g: g["latestAt"] or "", reverse=True)
        return ordered[:max(1, min(limit, 100))]

    def list_error_occurrences(self, fingerprint: str, *, limit: int) -> list[dict[str, Any]]:
        rows = self._request("GET", "/rest/v1/ops_error_events", params={
            "select": "event_id,occurred_at,source,job_name,error_class,message,stack,context",
            "fingerprint": f"eq.{fingerprint}",
            "order": "occurred_at.desc",
            "limit": str(max(1, min(limit, 100))),
        })
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def error_count_last_24h(self) -> int:
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            since = datetime.now(timezone.utc).timestamp() - 24 * 3600
            since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
            response = client.get(
                f"{self._supabase_url}/rest/v1/ops_error_events",
                params={"select": "event_id", "occurred_at": f"gt.{since_iso}", "limit": "1"},
                headers={**self._headers(), "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
            )
            content_range = response.headers.get("content-range", "")
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1]
                if total.isdigit():
                    return int(total)
            return 0
        except httpx.HTTPError:
            return 0
        finally:
            if should_close:
                client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                 json_payload: dict[str, Any] | None = None) -> Any:
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method, f"{self._supabase_url}{path}",
                params=params, json=json_payload, headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AdminOpsObservabilityError(f"Supabase ops request failed: {path}") from error
        finally:
            if should_close:
                client.close()
