from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import UPLOAD_DIR, settings
from app.routers import auth, health


app = FastAPI(
    title="CollectIQ AI Backend",
    version=settings.version,
    description="Local backend for CollectIQ AI scanner workflows.",
)

_allow_origins = list(settings.cors_allowed_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials="*" not in _allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.include_router(health.router)
app.include_router(auth.router)

from app.routers import api_analyze, portfolio, scanner

app.include_router(api_analyze.root_router)
app.include_router(api_analyze.router)
app.include_router(scanner.router)
app.include_router(portfolio.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail},
    )
