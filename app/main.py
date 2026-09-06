from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import UPLOAD_DIR, settings
from app.services.ops.observability import (
    record_handled_server_error,
    record_unhandled_error,
)
from app.routers import (
    admin_audit,
    admin_catalog,
    admin_catalog_image_flags,
    admin_catalog_promotion,
    admin_fx_rates,
    admin_notes,
    admin_ops,
    admin_pricecharting,
    admin_portfolio,
    admin_portfolio_matching,
    admin_pricing,
    admin_reports,
    admin_scans,
    admin_users,
    api_analyze,
    api_subscription,
    auth,
    data_requests,
    health,
    client_telemetry,
    metadata,
    pricing,
    portfolio,
    push,
    scanner,
    search,
    subscription_webhooks,
    support,
)


app = FastAPI(
    title="CollectIQ AI Backend",
    version=settings.version,
    description="Local backend for CollectIQ AI scanner workflows.",
)

# ORDER HERE IS LOAD-BEARING, AND COUNTER-INTUITIVE. add_middleware() inserts
# at position 0, so the LAST registered middleware is the OUTERMOST. This
# catch-all is registered BEFORE CORSMiddleware precisely so that CORS ends up
# wrapping it -- which is what puts Access-Control-Allow-Origin on the 500 it
# returns.
#
# It exists because a handler registered with @app.exception_handler(Exception)
# is served by Starlette's ServerErrorMiddleware, which sits outside every user
# middleware including CORS. That response carried no CORS headers, so an
# unhandled backend error reached the admin console as a bare "Failed to
# fetch": a correct error envelope the browser was never permitted to read.
# Measured rather than reasoned about -- tests/test_error_visibility.py fails
# if this registration moves below add_middleware(CORSMiddleware).
@app.middleware("http")
async def unhandled_exception_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as error:  # noqa: BLE001 - deliberate catch-all
        route = (
            getattr(getattr(request, "scope", {}).get("route"), "path", None)
            or request.url.path
        )
        # Best-effort in the strict sense: if the recorder itself raises, that
        # exception would escape this middleware and be served by
        # ServerErrorMiddleware -- outside CORS -- reinstating the very bug
        # this middleware exists to fix. Observability must never take down
        # the thing it observes, and here it must not take down its own fix.
        try:
            record_unhandled_error(route=route, error=error)
        except Exception:  # noqa: BLE001 - observability is never load-bearing
            pass
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {
                    "code": "internal_error",
                    "message": "Internal server error.",
                    "retryable": True,
                },
            },
        )


_allow_origins = list(settings.cors_allowed_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials="*" not in _allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Scratch directory for in-flight scanner uploads. Files are deleted after
# analysis; nothing here is served over HTTP.
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin_audit.router)
app.include_router(admin_catalog.router)
app.include_router(admin_catalog_image_flags.router)
app.include_router(admin_catalog_promotion.router)
app.include_router(admin_fx_rates.router)
app.include_router(admin_notes.router)
app.include_router(admin_ops.router)
app.include_router(admin_pricecharting.router)
app.include_router(admin_portfolio.router)
app.include_router(admin_portfolio_matching.router)
app.include_router(admin_pricing.router)
app.include_router(admin_reports.router)
app.include_router(admin_scans.router)
app.include_router(admin_users.router)
app.include_router(data_requests.router)
app.include_router(support.router)
app.include_router(push.router)
app.include_router(api_analyze.root_router)
app.include_router(api_analyze.router)
app.include_router(api_subscription.router)
app.include_router(subscription_webhooks.router)
app.include_router(scanner.router)
app.include_router(portfolio.router)
app.include_router(pricing.router)
app.include_router(search.router)
app.include_router(metadata.router)
app.include_router(client_telemetry.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    # A deliberate 5xx is still a server-side failure and belongs in the ops
    # feed; 4xx is the caller being told no, which is not a defect and would
    # bury the feed in auth and not-found noise. Best-effort -- recording can
    # never change the response.
    if exc.status_code >= 500:
        route = (
            getattr(getattr(request, "scope", {}).get("route"), "path", None)
            or request.url.path
        )
        try:
            record_handled_server_error(
                route=route, status_code=exc.status_code, detail=exc.detail
            )
        except Exception:  # noqa: BLE001 - observability is never load-bearing
            pass
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail},
    )


# The catch-all lives in unhandled_exception_middleware near the top of this
# file, NOT here: an @app.exception_handler(Exception) response is produced
# by Starlette's ServerErrorMiddleware, which sits outside CORS, so the
# browser can never read it.
