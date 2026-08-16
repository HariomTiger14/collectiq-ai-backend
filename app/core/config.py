import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


def _env_flag(name: str, default: str = "") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


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
        timezone.utc
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
        "https://sit.packlox.com,https://admin.packlox.com,https://packlox-admin-portal.hariomritesh.workers.dev",
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
    # Free-plan monthly /analyze cap enforced server-side (anti-abuse), giving a
    # hard, predictable cost ceiling. Matches the app's client-side limit.
    # Enforcement is fail-open.
    subscription_free_monthly_scan_limit: int = int(
        os.getenv("SUBSCRIPTION_FREE_MONTHLY_SCAN_LIMIT", "30")
    )
    supabase_health_required: bool = os.getenv(
        "SUPABASE_HEALTH_REQUIRED",
        "",
    ).strip().lower() in {"1", "true", "yes", "required"}
    ai_provider: str = os.getenv("AI_PROVIDER", "mock")
    allow_mock_analyzer: bool = os.getenv("ALLOW_MOCK_ANALYZER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    pricing_provider: str = os.getenv("PRICING_PROVIDER", "auto")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_timeout_seconds: float = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    gemini_timeout_seconds: float = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
    # The title-rescue pass (a second Gemini call attempted only when the
    # first pass returns an unrecognized item) is optional and already
    # fails open to the original result on any error. Giving it the full
    # primary-call budget let a slow rescue attempt roughly double a scan's
    # worst-case latency; bound it independently so it can't compound.
    gemini_rescue_timeout_seconds: float = float(
        os.getenv("GEMINI_RESCUE_TIMEOUT_SECONDS", "25")
    )
    ai_fallback_provider: str = os.getenv("AI_FALLBACK_PROVIDER", "openai")
    ai_fallback_confidence_threshold: int = int(
        os.getenv("AI_FALLBACK_CONFIDENCE_THRESHOLD", "70")
    )
    ebay_access_token: str = os.getenv("EBAY_ACCESS_TOKEN", "")
    ebay_client_id: str = os.getenv("EBAY_CLIENT_ID", "")
    ebay_client_secret: str = os.getenv("EBAY_CLIENT_SECRET", "")
    ebay_oauth_token_url: str = os.getenv(
        "EBAY_OAUTH_TOKEN_URL",
        "https://api.ebay.com/identity/v1/oauth2/token",
    )
    ebay_oauth_scope: str = os.getenv(
        "EBAY_OAUTH_SCOPE",
        "https://api.ebay.com/oauth/api_scope",
    )
    ebay_marketplace_insights_api_url: str = os.getenv(
        "EBAY_MARKETPLACE_INSIGHTS_API_URL",
        "",
    )
    ebay_partner_access_granted: bool = (
        os.getenv("EBAY_PARTNER_ACCESS_GRANTED", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
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
    kicksdb_api_key: str = os.getenv("KICKSDB_API_KEY", "")
    kicksdb_api_base: str = os.getenv(
        "KICKSDB_API_BASE",
        "https://api.kicks.dev",
    )
    kicksdb_timeout_seconds: float = float(
        os.getenv("KICKSDB_TIMEOUT_SECONDS", "10")
    )
    pricecharting_api_key: str = os.getenv("PRICECHARTING_API_KEY", "")
    pricecharting_api_base: str = os.getenv(
        "PRICECHARTING_API_BASE",
        "https://www.pricecharting.com",
    )
    pricecharting_timeout_seconds: float = float(
        os.getenv("PRICECHARTING_TIMEOUT_SECONDS", "10")
    )
    pricecharting_provider_min_interval_ms: int = int(
        os.getenv("PRICECHARTING_PROVIDER_MIN_INTERVAL_MS", "1000")
    )
    pricecharting_shared_throttle_enabled: bool = _env_flag(
        "PRICECHARTING_SHARED_THROTTLE_ENABLED",
        "true",
    )
    pricing_cache_ttl_seconds: int = int(os.getenv("PRICING_CACHE_TTL_SECONDS", "900"))
    pricing_provider_min_interval_ms: int = int(
        os.getenv("PRICING_PROVIDER_MIN_INTERVAL_MS", "250")
    )
    default_display_currency: str = os.getenv("DEFAULT_DISPLAY_CURRENCY", "AUD")
    fx_usd_to_aud: float = float(os.getenv("FX_USD_TO_AUD", "1.52"))
    fx_usd_to_cad: float = float(os.getenv("FX_USD_TO_CAD", "1.37"))
    fx_usd_to_gbp: float = float(os.getenv("FX_USD_TO_GBP", "0.78"))
    admin_import_token: str = os.getenv("ADMIN_IMPORT_TOKEN", "")
    admin_job_token: str = os.getenv("ADMIN_JOB_TOKEN", os.getenv("ADMIN_IMPORT_TOKEN", ""))
    admin_profile_table: str = os.getenv("ADMIN_PROFILE_TABLE", "profiles")
    firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", "")
    firebase_service_account_json: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    firebase_access_token: str = os.getenv("FIREBASE_ACCESS_TOKEN", "")

    # ---- Resend (support ticket reply notifications) ----
    # Separate from whatever SMTP provider Supabase Auth uses for its own
    # emails (password reset etc, configured in the Supabase dashboard, not
    # here) -- this is a direct application-level integration for arbitrary
    # transactional email the backend triggers itself.
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    resend_from_address: str = os.getenv("RESEND_FROM_ADDRESS", "PackLox Support <info@packlox.com>")

    # ---- Google Play (real subscription verification) ----
    google_play_package_name: str = os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "com.collectiq.ai")
    # Same raw-JSON-in-env-var convention as firebase_service_account_json above
    # -- a separate service account, scoped to the Android Publisher API only
    # (least privilege: it must not also carry Firebase messaging access).
    google_play_service_account_json: str = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
    # Matches GooglePlayBillingConfig's defaults in the mobile app
    # (google_play_billing_repository.dart) -- must stay in sync with
    # whatever COLLECTIQ_PRO_PRODUCT_ID/COLLECTIQ_PREMIUM_PRODUCT_ID the app
    # was built with, since this is how a verified purchase's productId
    # maps back to a plan name.
    google_play_pro_product_id: str = os.getenv("GOOGLE_PLAY_PRO_PRODUCT_ID", "collectiq_pro_monthly_test")
    google_play_premium_product_id: str = os.getenv(
        "GOOGLE_PLAY_PREMIUM_PRODUCT_ID", "collectiq_premium_monthly_test",
    )
    google_play_timeout_seconds: float = float(os.getenv("GOOGLE_PLAY_TIMEOUT_SECONDS", "10"))
    # Real-Time Developer Notifications arrive as an authenticated Pub/Sub
    # push request; the OIDC token's audience must match this webhook's own
    # public URL exactly, or a forged POST claiming to be Pub/Sub would be
    # accepted. Sourced from public_api_url by default since that's already
    # this backend's known external URL.
    google_play_rtdn_audience: str = os.getenv(
        "GOOGLE_PLAY_RTDN_AUDIENCE",
        os.getenv("PUBLIC_API_URL", "https://api-sit.packlox.com").rstrip("/") + "/subscription/webhooks/google",
    )

    # ---- Apple App Store (real subscription verification) ----
    apple_bundle_id: str = os.getenv("APPLE_BUNDLE_ID", "com.hariom.collectiqai")
    apple_issuer_id: str = os.getenv("APPLE_ISSUER_ID", "")
    apple_key_id: str = os.getenv("APPLE_KEY_ID", "")
    # The .p8 private key's raw PEM content (App Store Connect > Users and
    # Access > Keys), same "paste the whole file as one env var" convention
    # as the Google service account JSON above -- not a file path, since
    # this runs on Render where there's no persistent/mounted filesystem to
    # put a real key file on.
    apple_private_key: str = os.getenv("APPLE_PRIVATE_KEY", "")
    apple_app_apple_id: str = os.getenv("APPLE_APP_APPLE_ID", "")
    # Defaults match Google Play's product ids -- many apps reuse the same
    # identifier string across both stores for simplicity, but these are
    # independently configured in App Store Connect and can be overridden
    # separately once real Apple product ids are registered.
    apple_pro_product_id: str = os.getenv("APPLE_PRO_PRODUCT_ID", "collectiq_pro_monthly_test")
    apple_premium_product_id: str = os.getenv("APPLE_PREMIUM_PRODUCT_ID", "collectiq_premium_monthly_test")
    # "sandbox" until the app is actually live on the App Store -- matches
    # api_storekit_environment naming applestoreserverlibrary itself uses.
    apple_storekit_environment: str = os.getenv("APPLE_STOREKIT_ENVIRONMENT", "sandbox")


settings = Settings()
