# PackLox Analyzer API Contract

Status: SIT production contract baseline

The mobile app calls only the PackLox backend. The app must never call OpenAI,
Gemini, pricing providers, or marketplace APIs directly, and must never ship
provider secret keys in `dart-define`, app storage, logs, or binaries.

## Architecture

```text
Flutter app
  |
  | POST /analyze
  v
PackLox backend
  |
  +-- BackendAnalyzerService
        |
        +-- MockAnalyzerProvider
        +-- OpenAIAnalyzerProvider placeholder
        +-- GeminiAnalyzerProvider placeholder
```

Legacy compatibility routes:

- `POST /api/analyze` remains an alias for the same backend contract.
- `POST /scanner/analyze` remains the older multipart scanner endpoint.

## Environment

Required SIT mobile values:

- `APP_ENV=sit`
- `SUPABASE_URL=<SIT Supabase project URL>`
- `SUPABASE_ANON_KEY=<SIT Supabase anon key>`
- `API_BASE_URL=http://<PC_LAN_IP>:<mock_api_port>`

Optional compatibility override:

- `AI_BACKEND_ANALYSIS_ENDPOINT_URL=http://<host>:<port>/analyze`

When `AI_BACKEND_ANALYSIS_ENDPOINT_URL` is omitted, the app derives the final
Analyzer endpoint as:

```text
<API_BASE_URL>/analyze
```

## POST /analyze

Content types:

- `application/json`
- `multipart/form-data`

Request body:

```json
{
  "request": {
    "imagePath": "/local/app/path/card.jpg",
    "imageSource": "camera",
    "requestedCategory": "Pokemon Card",
    "appVersion": "1.0.0",
    "deviceMetadata": {
      "platform": "android"
    },
    "timestamp": "2026-06-30T09:00:00Z"
  },
  "image": {
    "fileName": "card.jpg",
    "mimeType": "image/jpeg",
    "sizeBytes": 123456,
    "imageSource": "camera",
    "localFilePath": "/local/app/path/card.jpg",
    "base64Image": null,
    "base64Preview": null
  }
}
```

Required request fields:

- `request.imagePath`
- `request.imageSource`: `camera`, `gallery`, `sample`, or `unknown`
- `request.timestamp`: ISO-8601 timestamp
- `image.fileName`
- `image.mimeType`: `image/jpeg` or `image/png`
- `image.sizeBytes`: `1..10485760`
- `image.imageSource`
- `image.localFilePath`

Optional request fields:

- `request.requestedCategory`
- `request.appVersion`
- `request.deviceMetadata`
- `image.base64Image`
- `image.base64Preview`

Image validation:

- Metadata is always validated.
- If `image.base64Image` is supplied, backend validates decoded bytes.
- Multipart uploads validate actual uploaded bytes.
- Supported image content: JPEG and PNG.
- Maximum image size: 10 MB.
- Corrupt, empty, oversized, mismatched, or unreadable image payloads return a
  normalized error.

Multipart request fields:

- File field: `image`
- Optional fields: `imageSource`, `requestedCategory`, `appVersion`,
  `timestamp`, `imagePath`

Response body:

```json
{
  "id": "backend-uuid",
  "itemName": "1999 Pokemon Charizard Holo",
  "title": "1999 Pokemon Charizard Holo",
  "category": "Pokemon Card",
  "manufacturer": "Pokemon",
  "year": "1999",
  "series": "Pokemon TCG",
  "variant": "Unlimited",
  "condition": "Near Mint",
  "confidence": 94,
  "estimatedValue": 1850,
  "estimated_value": 1850,
  "currency": "AUD",
  "tags": ["card", "pokemon"],
  "description": "Likely a Base Set Charizard holographic card.",
  "attributes": {
    "brand": "Pokemon",
    "setName": "Base Set"
  },
  "images": [],
  "rawProviderPayload": {},
  "lowEstimate": 1443,
  "highEstimate": 2257,
  "marketTrend": "Stable",
  "keyAttributes": {},
  "aiReview": {
    "primaryMatch": "1999 Pokemon Charizard Holo",
    "confidenceExplanation": "High confidence from visible details.",
    "detectionQuality": "Good",
    "reasoning": "Provider reasoning safe for client display."
  },
  "alternatives": [],
  "recommendation": "Review before saving.",
  "marketSummary": {},
  "comparableSales": [],
  "imageUrl": null,
  "timestamp": "2026-06-30T09:00:00Z",
  "fieldConfidence": {},
  "confidenceLevel": "High",
  "lowConfidenceReasons": [],
  "imageQualityIssues": [],
  "scanRecommendations": [],
  "diagnostics": {
    "aiProvider": "mock",
    "aiModel": "mock",
    "aiLatencyMs": 132,
    "pricingProvider": "mock",
    "pricingProviderCount": 1,
    "pricingFallbackUsed": false,
    "pricingCacheStatus": "miss",
    "pricingFreshness": "fresh",
    "confidenceLevel": "High",
    "totalLatencyMs": 155
  }
}
```

The current backend response preserves existing app fields such as `itemName`,
`marketSummary`, and `comparableSales`. The contract also reserves future
provider-neutral fields: `title`, `manufacturer`, `year`, `series`, `variant`,
`currency`, `tags`, `description`, `attributes`, `images`, and
`rawProviderPayload`.

## Errors

All non-2xx errors use this envelope:

```json
{
  "success": false,
  "error": {
    "code": "invalid_image",
    "message": "Image payload is invalid or unsupported.",
    "retryable": false,
    "details": {}
  }
}
```

Canonical backend/client error mapping:

- `timeout`: backend or provider timeout; retryable
- `network`: backend unreachable from app; retryable
- `invalid_image`: missing, empty, oversized, or unsupported image; not retryable
- `image_too_large`: image exceeds 10 MB; not retryable
- `unsupported_media_type`: image extension, MIME type, or byte signature is unsupported; not retryable
- `provider_unavailable`: AI/pricing provider unavailable or not configured; retryable except configuration failures
- `quota_exceeded`: provider quota/rate limit; not retried by mobile in the same request
- `authentication`: backend auth failure; not retried by mobile in the same request
- `unknown`: unexpected backend error; retryable when HTTP status is 5xx

Legacy backend codes may still be accepted by older app builds, but new backend
responses should use the canonical codes above.

## Timeout Behavior

Mobile Analyzer timeout:

- Controlled by `ANALYZER_TIMEOUT_SECONDS`
- Default: `30`

Backend provider timeout:

- Controlled by provider-specific backend environment variables, for example
  `OPENAI_TIMEOUT_SECONDS`
- Default OpenAI placeholder timeout: `30`

Timeouts return HTTP `504` with a retryable error.

## Retry Behavior

Mobile `AnalyzerService` retries only normalized retryable errors:

- `timeout`
- `network`
- `providerUnavailable`
- `unknown`

Mobile does not retry:

- `invalidImage`
- `quotaExceeded`
- `authentication`
- `cancelled`

Default mobile retry policy:

- `ANALYZER_MAX_ATTEMPTS=2`
- `200 ms` delay between attempts

## System Endpoints

`GET /health`

Returns non-secret runtime readiness:

```json
{
  "status": "ok",
  "environment": "sit",
  "ai_provider": "mock",
  "pricing_provider": "mock",
  "version": "0.1.0"
}
```

`GET /version`

Returns non-secret version metadata:

```json
{
  "version": "0.1.0",
  "environment": "sit"
}
```

## Local Backend Run

From the repository root on Windows:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:APP_ENV="sit"
$env:AI_PROVIDER="mock"
$env:PRICING_PROVIDER="mock"
py -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/version
```

Run backend endpoint tests:

```powershell
cd backend
py -m unittest tests.test_api_endpoints
```
