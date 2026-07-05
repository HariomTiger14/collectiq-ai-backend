from dataclasses import dataclass, field


@dataclass(frozen=True)
class AnalyzerPipelineError(Exception):
    code: str
    message: str
    status_code: int
    retryable: bool = False
    details: dict = field(default_factory=dict)

