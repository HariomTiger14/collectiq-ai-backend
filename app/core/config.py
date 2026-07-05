import os
from dataclasses import dataclass
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
    environment: str = os.getenv(
        "ENVIRONMENT",
        os.getenv("BACKEND_ENV", os.getenv("APP_ENV", "local")),
    )
    application_name: str = os.getenv("APPLICATION_NAME", "PackLox API")
    version: str = os.getenv("APP_VERSION", os.getenv("BACKEND_VERSION", "0.1.0"))
    commit: str = os.getenv(
        "COMMIT_SHA",
        os.getenv("GIT_COMMIT", os.getenv("CF_PAGES_COMMIT_SHA", "unknown")),
    )
    build_time: str = os.getenv("BUILD_TIME", os.getenv("CF_PAGES_COMMIT_TIME", "unknown"))
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
