from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.health.providers import (
    ApplicationHealthProvider,
    AnalyzerHealthProvider,
    HealthCheckResult,
    HealthProvider,
    SupabaseHealthProvider,
)


@dataclass(frozen=True)
class HealthReport:
    healthy: bool
    timestamp: str
    checks: list[HealthCheckResult]

    @property
    def services(self) -> dict[str, bool]:
        return {check.name: check.healthy for check in self.checks}

    @property
    def latency(self) -> dict[str, int]:
        return {check.name: check.latency_ms for check in self.checks}


class HealthCheckService:
    def __init__(self, providers: list[HealthProvider] | None = None) -> None:
        self._providers = providers or [
            ApplicationHealthProvider(),
            SupabaseHealthProvider(),
            AnalyzerHealthProvider(),
        ]

    def run(self) -> HealthReport:
        checks = [provider.check() for provider in self._providers]
        required_checks_healthy = all(
            check.healthy for check in checks if check.required
        )
        return HealthReport(
            healthy=required_checks_healthy,
            timestamp=datetime.now(timezone.utc).isoformat(),
            checks=checks,
        )
