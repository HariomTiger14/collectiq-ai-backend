import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("packlox.health")


@dataclass(frozen=True)
class HealthCheckResult:
    name: str
    healthy: bool
    required: bool
    latency_ms: int
    message: str = ""
    details: dict[str, str] = field(default_factory=dict)


class HealthProvider(Protocol):
    name: str
    required: bool

    def check(self) -> HealthCheckResult:
        """Run a synchronous dependency health check."""
        ...


class ApplicationHealthProvider:
    name = "api"
    required = True

    def check(self) -> HealthCheckResult:
        started_at = time.perf_counter()
        return HealthCheckResult(
            name=self.name,
            healthy=True,
            required=self.required,
            latency_ms=_latency_ms(started_at),
            message="API is running.",
        )


class SupabaseHealthProvider:
    name = "supabase"

    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        anon_key: str | None = None,
        timeout_seconds: float | None = None,
        required: bool | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (supabase_url if supabase_url is not None else settings.supabase_url).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._anon_key = (anon_key if anon_key is not None else settings.supabase_anon_key).strip()
        self._timeout_seconds = timeout_seconds or settings.health_timeout_seconds
        self._client = client
        self.required = _is_required_for_environment() if required is None else required

    def check(self) -> HealthCheckResult:
        started_at = time.perf_counter()
        auth_key = self._service_role_key or self._anon_key
        if not self._supabase_url or not auth_key:
            return HealthCheckResult(
                name=self.name,
                healthy=not self.required,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message="Supabase configuration is missing.",
                details={"configured": "false"},
            )

        close_client = False
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self._timeout_seconds)
            close_client = True

        try:
            response = client.get(
                f"{self._supabase_url}/auth/v1/health",
                headers={"apikey": auth_key},
                timeout=self._timeout_seconds,
            )
            healthy = 200 <= response.status_code < 500
            return HealthCheckResult(
                name=self.name,
                healthy=healthy,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message=(
                    "Supabase health endpoint responded."
                    if healthy
                    else f"Supabase health check failed with status {response.status_code}."
                ),
                details={
                    "statusCode": str(response.status_code),
                    "configured": "true",
                    "credential": "service_role" if self._service_role_key else "anon",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("Supabase health check failed: %s", exc)
            return HealthCheckResult(
                name=self.name,
                healthy=False,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message="Supabase health check could not connect.",
                details={"configured": "true", "error": exc.__class__.__name__},
            )
        finally:
            if close_client:
                client.close()


class AnalyzerHealthProvider:
    name = "analyzer"
    required = True

    def check(self) -> HealthCheckResult:
        started_at = time.perf_counter()
        provider = settings.ai_provider.strip().lower()
        if provider == "mock":
            return HealthCheckResult(
                name=self.name,
                healthy=True,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message="Mock analyzer provider is available.",
                details={"provider": provider},
            )
        if provider in {"auto", "real", "vision"}:
            gemini_configured = bool(settings.gemini_api_key.strip())
            openai_configured = bool(settings.openai_api_key.strip())
            configured = gemini_configured or openai_configured
            return HealthCheckResult(
                name=self.name,
                healthy=configured,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message=(
                    "Auto analyzer provider is configured."
                    if configured
                    else "GEMINI_API_KEY or OPENAI_API_KEY is required when AI_PROVIDER=auto."
                ),
                details={
                    "provider": provider,
                    "configured": str(configured).lower(),
                    "geminiConfigured": str(gemini_configured).lower(),
                    "openaiConfigured": str(openai_configured).lower(),
                },
            )
        if provider == "gemini":
            configured = bool(settings.gemini_api_key.strip())
            return HealthCheckResult(
                name=self.name,
                healthy=configured,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message=(
                    "Gemini analyzer provider is configured."
                    if configured
                    else "GEMINI_API_KEY is required when AI_PROVIDER=gemini."
                ),
                details={"provider": provider, "configured": str(configured).lower()},
            )
        if provider == "openai":
            configured = bool(settings.openai_api_key.strip())
            return HealthCheckResult(
                name=self.name,
                healthy=configured,
                required=self.required,
                latency_ms=_latency_ms(started_at),
                message=(
                    "OpenAI analyzer provider is configured."
                    if configured
                    else "OPENAI_API_KEY is required when AI_PROVIDER=openai."
                ),
                details={"provider": provider, "configured": str(configured).lower()},
            )

        return HealthCheckResult(
            name=self.name,
            healthy=False,
            required=self.required,
            latency_ms=_latency_ms(started_at),
            message=f"Analyzer provider '{provider}' is unavailable.",
            details={"provider": provider or "unknown"},
        )


def _is_required_for_environment() -> bool:
    if settings.supabase_health_required:
        return True
    return settings.environment.strip().lower() in {"sit", "staging", "production", "prod"}


def _latency_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))
