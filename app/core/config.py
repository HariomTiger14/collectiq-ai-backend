import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = PROJECT_ROOT / "uploads"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png"}
CHUNK_SIZE = 1024 * 1024

load_dotenv(BACKEND_ROOT / ".env")


def _first_env_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve_app_version() -> str:
    return _first_env_value("APP_VERSION", "BACKEND_VERSION") or "0.1.0"


def resolve_environment() -> str:
    return _first_env_value("ENVIRONMENT", "BACKEND_ENV", "APP_ENV") or "local"


def resolve_commit_sha() -> str:
    return (
        _first_env_value("COMMIT_SHA", "GIT_COMMIT", "CF_PAGES_COMMIT_SHA")
        or _first_env_value("RENDER_GIT_COMMIT", "RENDER_COMMIT_SHA")
        or _git_commit_sha()
        or "unknown"
    )


def resolve_build_time() -> str:
    return _first_env_value("BUILD_TIME", "CF_PAGES_COMMIT_TIME") or datetime.now(
        UTC
    ).isoformat()


def _git_commit_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    commit = result.stdout.strip()
    return commit or None


def parse_cors_allowed_origins(raw_value: str | None = None) -> tuple[str, ...]:
    value = raw_value if raw_value is not None else os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,"
        "http://localhost:3000,http://127.0.0.1:3000,"
        "https://sit.packlox.com,https://admin.packlox.com",
    )
    return tuple(
        origin.strip()
        for origin in value.split(",")
        if origin.strip()
    )


@dataclass(frozen=True)
class Settings:
    environment: str = field(default_factory=resolve_environment)
    application_name: str = os.getenv("APPLICATION_NAME", "PackLox API")
    version: str = field(default_factory=resolve_app_version)
    commit: str = field(default_factory=resolve_commit_sha)
    build_time: str = field(default_factory=resolve_build_time)
    public_api_url: str = os.getenv("PUBLIC_API_URL", "https://api-sit.packlox.com")
    public_frontend_url: str = os.getenv("PUBLIC_FRONTEND_URL", "https://sit.packlox.com")
    port: int = int(os.getenv("PORT", "8000"))
    cors_allowed_origins: tuple[str, ...] = parse_cors_allowed_origins()
    health_timeout_seconds: float = float(os.getenv("HEALTH_TIMEOUT_SECONDS", "3"))
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
    supabase_health_required: bool = os.getenv(
        "SUPABASE_HEALTH_REQUIRED",
        "",
    ).strip().lower() in {"1", "true", "yes", "required"}
    ai_provider: str = os.getenv("AI_PROVIDER", "mock")
    pricing_provider: str = os.getenv("PRICING_PROVIDER", "mock")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_timeout_seconds: float = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    gemini_timeout_seconds: float = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "30"))
    ebay_access_token: str = os.getenv("EBAY_ACCESS_TOKEN", "")
    ebay_browse_api_url: str = os.getenv(
        "EBAY_BROWSE_API_URL",
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
    )
    ebay_marketplace_id: str = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_AU")
    ebay_timeout_seconds: float = float(os.getenv("EBAY_TIMEOUT_SECONDS", "10"))
    tcgplayer_client_id: str = os.getenv("TCGPLAYER_CLIENT_ID", "")
    tcgplayer_client_secret: str = os.getenv("TCGPLAYER_CLIENT_SECRET", "")
    tcgplayer_api_base: str = os.getenv(
        "TCGPLAYER_API_BASE",
        "https://api.tcgplayer.com",
    )
    tcgplayer_timeout_seconds: float = float(
        os.getenv("TCGPLAYER_TIMEOUT_SECONDS", "10")
    )
    pricecharting_api_key: str = os.getenv("PRICECHARTING_API_KEY", "")
    pricecharting_api_base: str = os.getenv(
        "PRICECHARTING_API_BASE",
        "https://www.pricecharting.com",
    )
    pricecharting_timeout_seconds: float = float(
        os.getenv("PRICECHARTING_TIMEOUT_SECONDS", "10")
    )
    pricing_cache_ttl_seconds: int = int(os.getenv("PRICING_CACHE_TTL_SECONDS", "900"))
    pricing_provider_min_interval_ms: int = int(
        os.getenv("PRICING_PROVIDER_MIN_INTERVAL_MS", "250")
    )


settings = Settings()
