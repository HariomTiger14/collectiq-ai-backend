# CollectIQ Backend SIT Deployment

Audit date: 2026-07-01

This guide prepares the FastAPI backend so **CollectIQ SIT** can call a real backend URL through `API_BASE_URL`.

Production remains disabled. Flutter must not call OpenAI directly. Real AI is enabled only by server-side backend environment variables.

## Backend Audit

Entrypoint:

- ASGI app: `backend/app/main.py`
- Root convenience module: `backend/main.py`
- Uvicorn target: `app.main:app`

Routes:

- `GET /health`
- `GET /version`
- `POST /scanner/analyze`
- `POST /api/analyze`
- `GET /portfolio`
- `POST /portfolio`
- `DELETE /portfolio/{item_id}`
- `GET /uploads/{path}`

Python/runtime:

- Docker image uses Python `3.12-slim`.
- Local development should use Python 3.11 or newer.
- Dependencies are listed in `backend/requirements.txt`.

Required packages:

- FastAPI
- Uvicorn
- httpx
- python-dotenv
- python-multipart

Default backend mode:

- `AI_PROVIDER=mock`
- `PRICING_PROVIDER=mock`

AI modes:

- `mock`: deterministic local backend recognition, no external secrets.
- `openai`: server-side OpenAI recognition, requires `OPENAI_API_KEY`.

Pricing modes:

- `mock`
- `ebay`
- `tcgplayer`
- `pricecharting`
- `aggregate`

Provider API keys must stay server-side.

## Environment Variables

Required for SIT mock:

```text
ENVIRONMENT=sit
BACKEND_ENV=sit
APP_VERSION=0.1.0
BACKEND_VERSION=0.1.0
COMMIT_SHA=<deployed git commit>
GIT_COMMIT=<deployed git commit>
BUILD_TIME=<ISO-8601 build timestamp>
PORT=8000
AI_PROVIDER=mock
PRICING_PROVIDER=mock
CORS_ALLOWED_ORIGINS=https://sit.packlox.com,https://admin.packlox.com,http://localhost:3000,http://127.0.0.1:3000
SUPABASE_URL=https://YOUR-SIT-PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<server-side service role key>
SUPABASE_ANON_KEY=YOUR_PUBLIC_ANON_KEY
SUPABASE_HEALTH_REQUIRED=true
```

Required for SIT OpenAI:

```text
ENVIRONMENT=sit
BACKEND_ENV=sit
PORT=8000
AI_PROVIDER=openai
OPENAI_API_KEY=<server-side secret>
PRICING_PROVIDER=mock
CORS_ALLOWED_ORIGINS=
```

Optional:

```text
APPLICATION_NAME=PackLox API
PUBLIC_API_URL=https://api-sit.packlox.com
PUBLIC_FRONTEND_URL=https://sit.packlox.com
HEALTH_TIMEOUT_SECONDS=3
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TIMEOUT_SECONDS=30
```

Do not commit `.env` files with real values.

Deployment variable aliases:

- `ENVIRONMENT` is preferred; `BACKEND_ENV` and `APP_ENV` remain supported.
- `APP_VERSION` is preferred; `BACKEND_VERSION` remains supported.
- `COMMIT_SHA` is preferred; `GIT_COMMIT` and `CF_PAGES_COMMIT_SHA` remain supported.
- `SUPABASE_SERVICE_ROLE_KEY` is used for server-side health checks when set.
- `SUPABASE_ANON_KEY` is accepted for public/anon health checks when a service role key is not set.

## Local Run

From the repository root:

```bat
backend\run_backend_local.bat
```

This starts mock AI and mock pricing on port `8000`.

Health check:

```text
http://127.0.0.1:8000/health
```

Version check:

```text
http://127.0.0.1:8000/version
```

## Health Monitoring API

`GET /health` verifies the API process, Supabase connectivity, and analyzer
availability through reusable health providers:

- `ApplicationHealthProvider`
- `SupabaseHealthProvider`
- `AnalyzerHealthProvider`

Healthy response:

```json
{
  "status": "healthy",
  "environment": "sit",
  "version": "0.1.0",
  "timestamp": "2026-07-05T00:00:00Z",
  "services": {
    "api": true,
    "supabase": true,
    "analyzer": true
  },
  "latency": {
    "api": 0,
    "supabase": 72,
    "analyzer": 0
  },
  "checks": []
}
```

If a required dependency is unhealthy, the endpoint returns HTTP `503` with
`status: "unhealthy"`. In `sit`, `staging`, and `production`, Supabase health is
required by default. Local development may leave Supabase unset unless
`SUPABASE_HEALTH_REQUIRED=true`.

The health check sequence is:

```text
Client -> FastAPI -> HealthCheckService -> SupabaseHealthProvider
                                  \-----> AnalyzerHealthProvider -> Response
```

`GET /version` returns non-secret deployment metadata:

```json
{
  "application": "PackLox API",
  "environment": "sit",
  "version": "0.1.0",
  "commit": "<git hash>",
  "buildTime": "<ISO-8601 build timestamp>"
}
```

## SIT Mock Run

```bat
backend\run_backend_sit_mock.bat
```

Use this before validating paid/external providers.

## SIT OpenAI Run

Set the key in the terminal or hosting provider secrets, then run:

```bat
set OPENAI_API_KEY=YOUR_SERVER_SIDE_KEY
backend\run_backend_sit_openai.bat
```

The script refuses to start OpenAI mode if `OPENAI_API_KEY` is missing.

## Docker

Build:

```bat
cd backend
docker build -t collectiq-backend-sit .
```

Run mock mode:

```bat
docker run --rm -p 8000:8000 ^
  -e BACKEND_ENV=sit ^
  -e AI_PROVIDER=mock ^
  -e PRICING_PROVIDER=mock ^
  collectiq-backend-sit
```

Run OpenAI mode:

```bat
docker run --rm -p 8000:8000 ^
  -e BACKEND_ENV=sit ^
  -e AI_PROVIDER=openai ^
  -e OPENAI_API_KEY=YOUR_SERVER_SIDE_KEY ^
  -e PRICING_PROVIDER=mock ^
  collectiq-backend-sit
```

## Smoke Test

With the backend running:

```bat
cd backend
py smoke_test_backend.py --base-url http://127.0.0.1:8000
```

If no sample image exists, the image upload test is skipped clearly. To test `/scanner/analyze`:

```bat
py smoke_test_backend.py --base-url http://127.0.0.1:8000 --image C:\path\to\card.jpg
```

## Hosting Options

### Render

Pros:

- Simple web service setup.
- Easy environment variables.
- Built-in HTTPS URL.
- Good fit for early SIT.

Cons:

- Free tiers can sleep.
- Cold starts can affect first scan.

Recommended for CollectIQ SIT because it is simple and gives a stable HTTPS `API_BASE_URL` quickly.

### Railway

Pros:

- Very quick Docker or Python deployment.
- Good logs and env var UX.

Cons:

- Usage billing can be less predictable for always-on services.

### Fly.io

Pros:

- Strong Docker/runtime control.
- Region placement options.

Cons:

- More operational setup than needed for first SIT.

## Recommended SIT Deployment: Render

Create a Render Web Service:

- Root directory: `backend`
- Runtime: Python or Docker
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Environment variables for mock SIT:

```text
ENVIRONMENT=sit
BACKEND_ENV=sit
AI_PROVIDER=mock
PRICING_PROVIDER=mock
CORS_ALLOWED_ORIGINS=https://sit.packlox.com,https://admin.packlox.com,http://localhost:3000,http://127.0.0.1:3000
SUPABASE_URL=https://YOUR-SIT-PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<server-side service role key>
SUPABASE_ANON_KEY=YOUR_PUBLIC_ANON_KEY
SUPABASE_HEALTH_REQUIRED=true
```

Environment variables for OpenAI SIT:

```text
BACKEND_ENV=sit
AI_PROVIDER=openai
OPENAI_API_KEY=<server-side secret>
PRICING_PROVIDER=mock
CORS_ALLOWED_ORIGINS=
```

After deploy, open:

```text
https://YOUR-BACKEND-HOST/health
```

The response should include:

- `status`
- `environment`
- `version`
- `timestamp`
- `services`
- `latency`
- `checks`

It must not include secrets.

## API_BASE_URL For CollectIQ SIT

Set `API_BASE_URL` in `config/sit.env` to the backend origin only:

```text
API_BASE_URL=https://YOUR-BACKEND-HOST
```

Do not include `/scanner/analyze` in `API_BASE_URL`.

The Flutter scanner path calls:

```text
POST {API_BASE_URL}/scanner/analyze
```

## Phone Browser Test

From the Android phone on the same network or internet connection:

1. Open Chrome.
2. Go to:

```text
https://YOUR-BACKEND-HOST/health
```

3. Confirm JSON appears.
4. Confirm `environment` is `sit`.
5. Confirm `ai_provider` is expected.

If the phone cannot open `/health`, CollectIQ SIT cannot call the backend either.

## Scanner Analyze Contract

Endpoint:

```text
POST /scanner/analyze
```

Request:

- Multipart form field: `image`
- Supported types: `jpg`, `jpeg`, `png`
- Max size: 10 MB

Response includes:

- `title`
- `category`
- `manufacturer`
- `series`
- `year`
- `country`
- `estimated_value_low`
- `estimated_value_high`
- `confidence`
- `description`
- `notes`

Existing app-compatible fields such as `brand`, `estimatedValue`, and nested `pricing` remain present.

## Safety Notes

- Keep `AI_PROVIDER=mock` until real provider validation is intentional.
- Keep `PRICING_PROVIDER=mock` unless validating server-side provider credentials.
- Never put `OPENAI_API_KEY` or pricing provider keys in Flutter dart-defines.
- Never commit `.env` files with real secrets.
- Production remains disabled by app configuration.
